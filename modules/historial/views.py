"""Historial unificado de movimientos dobles."""

import re

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

import db
import labels as _LBL
from auth import requiere_login, tiene_permiso
from concepto_parser import parse_ref_anticipo
from error_messages import flash_exc
from exports import csv_response
from filters import money_es
from modules.cheques import queries as _cheques_q
from modules.compras import queries as _compras_q
from modules.gastos import queries as _gastos_q
from modules.posdat import queries as _posdat_q

from . import queries

historial_bp = Blueprint("historial", __name__, template_folder="templates")


def _ref_anticipo(concepto: str | None) -> str:
    """Sufijo con el número de importación del anticipo: `" 44/26"`, `" 44"`, `""`.

    Sale del MISMO concepto que muestra /dolares (`parse_ref_anticipo`), así
    que la celda del historial y la de la pantalla de anticipos dicen el mismo
    número. El año se abrevia a dos dígitos porque así lo escribe la persona
    que carga (`44/26`). Sin número reconocible devuelve "" y la etiqueta
    queda como estaba (`USD AI`).
    """
    ref = parse_ref_anticipo(concepto)
    numero = ref.get("numero")
    if numero is None:
        return ""
    anio = ref.get("anio")
    if anio:
        return f" {numero}/{int(anio) % 100:02d}"
    return f" {numero}"


@historial_bp.route("/operaciones")
@requiere_login
def operaciones():
    """Landing con cards para todas las operaciones (movimientos dobles).

    Cada card lleva al wizard correspondiente. Agrupa por categoría:
    Movimientos entre cuentas / Cheques / Compras y proveedores / Capital /
    Caja / Auditoría.

    Las cards muestran solo si el usuario tiene el permiso necesario.
    """
    # TMT 2026-07-23 (dueña): "que las facturas (y retenciones) que se cargan
    # solas se carguen cuando entro al programa, no necesariamente en /facturas".
    # Ésta es la landing (/ → dashboard → /operaciones), así que disparamos acá
    # la MISMA auto-carga del día que ya corre al abrir /facturas. Idempotente,
    # fail-soft y con throttle interno (una corrida/minuto/proceso), así que no
    # encima ni pesa aunque también se abra /facturas. Sólo si puede crear facturas.
    try:
        from auth import tiene_permiso as _tp
        if _tp("facturas.crear"):
            from modules.facturas.views import _auto_cargar_facturas_hoy
            _ac = _auto_cargar_facturas_hoy()
            if _ac.get("cargadas") or _ac.get("ret"):
                _m = []
                if _ac.get("cargadas"):
                    _m.append(f"Se cargaron solas {_ac['cargadas']} facturas de hoy desde Asinfo.")
                if _ac.get("ret"):
                    _m.append(f"Retenciones aplicadas: {_ac['ret']}.")
                flash(" ".join(_m), "ok")
    except Exception:  # noqa: BLE001 -- nunca romper la landing por la auto-carga
        pass

    # TMT 2026-08-13 (dueña): *"cómo podríamos ir teniendo cantidad kg
    # vendidos en el día y que se vaya acumulando"* → el recuadro de ventas de
    # hoy, acá arriba. Se pinta con el número ya calculado (para que esté a la
    # vista al abrir) y después se refresca solo contra
    # /facturas/totales-hoy.json. Mismo universo que la campanita y el cierre.
    ventas_hoy = None
    if tiene_permiso("facturas.ver"):
        try:
            from modules.facturas.views import resumen_hoy
            # Sin Asinfo en el render (ver resumen_hoy): el despachado lo trae
            # el fetch de /facturas/totales-hoy.json apenas carga la pantalla.
            ventas_hoy = resumen_hoy(con_despacho=False)
        except Exception:  # noqa: BLE001 -- la landing nunca se cae por esto
            ventas_hoy = None

    aviso_migracion = None
    try:
        kpis = queries.conteos()
    except Exception as e:
        msg = str(e).lower()
        if "mov_doble" in msg and ("does not exist" in msg or "no existe" in msg or "relation" in msg):
            aviso_migracion = (
                "Tip: aún no se aplicó la migración del historial "
                "(scintela.mov_doble). Las operaciones funcionan igual, "
                "pero no se registran en el historial unificado hasta "
                "correr: python scripts/migrate.py"
            )
        kpis = {}
    return render_template(
        "historial/operaciones.html",
        kpis=kpis,
        aviso_migracion=aviso_migracion,
        ventas_hoy=ventas_hoy,
    )


@historial_bp.route("/historial")
@requiere_login
def lista():
    """Timeline unificado de movimientos dobles.

    Filtros: ?tipo=... ?estado=activo|reverso|reversado ?desde=YYYY-MM-DD
    ?hasta=YYYY-MM-DD ?q=texto ?mis_origenes=1.

    TMT 2026-05-26 dueña: cuando vino con `mis_origenes=1` (vía
    /mi-historial), permitir acceso SIN `informes.ver` y filtrar la
    query a las secciones donde el user tiene .ver. Alex ve cheques+
    facturas+caja+bancos pero NO retiros. Sino exigir informes.ver
    como antes.
    """
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    tipo = request.args.get("tipo") or None
    estado = request.args.get("estado") or None
    q = (request.args.get("q") or "").strip() or None
    mis_origenes = request.args.get("mis_origenes") == "1"
    # TMT 2026-08-11 (dueña): la traza manda los `id_mov_doble` exactos de un
    # renglón para que la pantalla muestre ESOS movimientos y no el día entero.
    ids = []
    for _t in (request.args.get("ids") or "").split(","):
        try:
            ids.append(int(_t.strip()))
        except (TypeError, ValueError):
            continue

    # Mapeo origen_table → permiso requerido. Si el user tiene el permiso,
    # ese origen entra en su lista de visibles.
    _ORIGEN_PERMISO = {
        "caja": "caja.ver",
        "cheque": "cheques.ver",
        "factura": "facturas.ver",
        "transacciones_bancarias": "bancos.ver",
        "compra": "compras.ver",
        "posdat": "posdat.ver",
        "gasto": "gastos.ver",
        "xgast": "gastos.ver",
        "retiro": "retiros.ver",
        "capital": "capital.ver",
        "provision": "provisiones.ver",
        "activo": "activos.ver",
        # TMT 2026-07-08: los mov_doble de activos usan origen_table='activos'
        # (plural) — sin esta clave, /mi-historial los ocultaba a usuarios sin
        # wildcard (ej. Alex). Accionista/Admin no se ven afectados.
        "activos": "activos.ver",
    }

    # TMT 2026-07-11 (dueña): historial UNIFICADO — una sola página para todos.
    # Antes había /historial (exigía informes.ver) y /mi-historial
    # (=/historial?mis_origenes=1, filtrado por permisos). Ahora TODO usuario
    # logueado entra, pero:
    #   · con informes.ver (dueña/admin) → ve TODO (sin filtro).
    #   · sin informes.ver → ve sólo las secciones donde tiene .ver
    #     (ej. Alex: cheques+facturas+caja+bancos, NO retiros/capital).
    # El parámetro `mis_origenes` queda como no-op (compat con links viejos).
    from auth import tiene_permiso
    _ = mis_origenes  # retro-compat: ya no cambia el comportamiento
    if tiene_permiso("informes.ver"):
        origenes_permitidos = None
    else:
        origenes_permitidos = [
            origen for origen, perm in _ORIGEN_PERMISO.items()
            if tiene_permiso(perm)
        ]
    # TMT 2026-05-24 — Pedido dueña: "no scroll vertical". Default 25 filas
    # (entran en viewport); el usuario expande con ?limite=50/100/200/todo
    # desde el selector "Filas" en el form de filtros.
    try:
        limite_raw = request.args.get("limite") or "25"
        if limite_raw.lower() == "todo":
            limite = 5000
        else:
            limite = max(10, min(int(limite_raw), 5000))
    except (TypeError, ValueError):
        limite = 25

    # TMT 2026-07-11 (dueña): paginación con flechita. `pagina` es 1-based.
    # Pedimos `limite+1` filas para saber si hay una página siguiente sin un
    # COUNT extra. Si vuelven más de `limite`, recortamos y marcamos hay_mas.
    try:
        pagina = max(1, int(request.args.get("pagina") or "1"))
    except (TypeError, ValueError):
        pagina = 1
    offset = (pagina - 1) * limite

    try:
        filas = queries.listar(
            desde=desde,
            hasta=hasta,
            tipo=tipo,
            estado=estado,
            q=q,
            ids=ids or None,
            origenes_permitidos=origenes_permitidos,
            limite=limite + 1,
            offset=offset,
        )
        hay_mas = len(filas) > limite
        filas = filas[:limite]
        kpis = queries.conteos(desde=desde, hasta=hasta)
        error = None
    except Exception as e:
        msg = str(e).lower()
        if "mov_doble" in msg and ("does not exist" in msg or "no existe" in msg or "relation" in msg):
            # Tabla aún no creada — guiar al usuario a correr la migración.
            error = (
                "Falta correr la migración 0023 (tabla scintela.mov_doble no existe). "
                "Abrí una terminal en la carpeta del proyecto y corré: "
                "python scripts/migrate.py"
            )
        else:
            error = str(e)
        filas, kpis, hay_mas = [], {}, False

    # Enriquecer filas con label + links para el template.
    # TMT 2026-05-15: batch-lookup de no_cheque (scintela.cheque) y numf
    # (scintela.factura) para que las etiquetas digan "Cheque #001" en vez
    # de "Cheque #1905" (id interno).
    # TMT 2026-05-16: además, lookup de banco para transacciones_bancarias
    # (mostrar "Dep. Pichincha" en lugar de "Banco mov #5774", la dueña pidió
    # menos noise de IDs en el historial).
    id_cheques: set[int] = set()
    id_facturas: set[int] = set()
    id_txbanco: set[int] = set()
    id_compras: set[int] = set()
    id_posdats: set[int] = set()
    id_xgast: set[int] = set()
    id_dolares: set[int] = set()
    id_retiros: set[int] = set()
    for r in filas:
        ot, oid = r.get("origen_table"), r.get("origen_id")
        dt, did = r.get("destino_table"), r.get("destino_id")
        if ot == "cheque" and oid:
            id_cheques.add(int(oid))
        if dt == "cheque" and did:
            id_cheques.add(int(did))
        if ot == "compra" and oid:
            id_compras.add(int(oid))
        if dt == "compra" and did:
            id_compras.add(int(did))
        if ot == "factura" and oid:
            id_facturas.add(int(oid))
        if dt == "factura" and did:
            id_facturas.add(int(did))
        if ot == "transacciones_bancarias" and oid:
            id_txbanco.add(int(oid))
        if dt == "transacciones_bancarias" and did:
            id_txbanco.add(int(did))
        if ot == "posdat" and oid:
            id_posdats.add(int(oid))
        if dt == "posdat" and did:
            id_posdats.add(int(did))
        # 🚨 TMT 2026-08-09: *"Caja #896 → Gasto #553"*. Estas cuatro tablas no
        # tenían batch: la etiqueta caía al id interno, que no aparece en
        # ninguna pantalla. Medido: gastos 882 celdas, caja 590, USD 194,
        # retiros 80.
        if ot == "xgast" and oid:
            id_xgast.add(int(oid))
        if dt == "xgast" and did:
            id_xgast.add(int(did))
        if ot == "dolares" and oid:
            id_dolares.add(int(oid))
        if dt == "dolares" and did:
            id_dolares.add(int(did))
        if ot == "retiros" and oid:
            id_retiros.add(int(oid))
        if dt == "retiros" and did:
            id_retiros.add(int(did))
    # 🚨 TMT 2026-08-09: *"¿el número de cheque que se muestra es del cheque
    # real o del programa?"* — y era las dos cosas según la fila, que es lo
    # peor. La mitad de la cobranza NO son cheques (NB 90/91 depósito directo,
    # NB 99 efectivo) y no tienen número: ahí la pantalla escribía
    # "Cheque #102090" con el ID INTERNO, que se lee igual que un número de
    # cheque. *"Poné dep pich más que cheque #x"*.
    # `cheque_etiquetas` es TEXTO PARA LEER ("Cheque 102345", "Dep. Pich.");
    # `cheque_nos` sigue siendo sólo el número real, porque de ahí sale la URL
    # /cheques/<no> y un "Dep. Pich." en una URL es un 404.
    cheque_etiquetas: dict[int, str] = {}
    cheque_nos_reales: dict[int, str] = {}
    # 🚨 TMT 2026-08-18 (dueña, mirando /historial?pagina=3): *"nunca veo los
    # clientes en cheques y facturas, ponelos en algún lado"*. El cliente
    # estaba sólo como código metido adentro del concepto ("Factura 182101
    # HOM", "Cheque 4839 de IIA") — y en las 6.066 filas de aplicación a
    # factura el concepto va vacío a propósito (repetía las dos columnas de al
    # lado), así que ahí no estaba en ningún lado. Ahora tiene columna propia.
    cliente_de_cheque: dict[int, str] = {}
    cliente_de_factura: dict[int, str] = {}
    # 🚨 TMT 2026-08-10: la columna TIPO decía "Cheque: alta" al lado de un
    # origen que decía "Dep. Pich." — la fila se contradecía sola. La
    # partición es la que ya usa la cobranza (`SQL_ES_CHEQUE`): NB 90/91 es
    # depósito directo y NB 99 es efectivo. Medido: 856 altas y 1.373
    # aplicaciones a factura que NO son un cheque.
    cheque_no_es_cheque: set[int] = set()
    if id_cheques:
        placeholder = ",".join(["%s"] * len(id_cheques))
        rows_ch = (
            db.fetch_all(
                f"SELECT c.id_cheque, COALESCE(c.no_cheque::text, '') AS no_cheque, "
                f"       COALESCE(c.no_banco, 0) AS no_banco, "
                f"       COALESCE(c.codigo_cli, '') AS codigo_cli, "
                f"       COALESCE(b.nombre, '') AS banco_nombre "
                f"  FROM scintela.cheque c "
                f"  LEFT JOIN scintela.banco b ON b.no_banco = c.no_banco "
                f" WHERE c.id_cheque IN ({placeholder})",
                tuple(id_cheques),
            )
            or []
        )
        for rc in rows_ch:
            idc = int(rc["id_cheque"])
            _cli = (rc.get("codigo_cli") or "").strip().upper()
            if _cli:
                cliente_de_cheque[idc] = _cli
            no = (rc.get("no_cheque") or "").strip()
            if no and no != "0":
                cheque_nos_reales[idc] = no
            if int(rc.get("no_banco") or 0) in (90, 91, 99):
                cheque_no_es_cheque.add(idc)
            # La MISMA función que usa el alta para escribir el concepto.
            cheque_etiquetas[idc] = _cheques_q.etiqueta_cobro(rc) or f"Cheque #{idc}"
    factura_labels: dict[int, str] = {}
    if id_facturas:
        # TMT 2026-05-15: cast a text — `numf` puede ser INTEGER en data
        # legacy, y COALESCE(integer, '') crashea con InvalidTextRepresentation.
        placeholder = ",".join(["%s"] * len(id_facturas))
        rows_f = (
            db.fetch_all(
                f"SELECT id_factura, COALESCE(numf::text, '') AS numf, "
                f"COALESCE(codigo_cli, '') AS codigo_cli "
                f"FROM scintela.factura WHERE id_factura IN ({placeholder})",
                tuple(id_facturas),
            )
            or []
        )
        for rf in rows_f:
            _cli = (rf.get("codigo_cli") or "").strip().upper()
            if _cli:
                cliente_de_factura[int(rf["id_factura"])] = _cli
            num = (rf.get("numf") or "").strip()
            # 🚨 538 facturas importadas del dBase tienen `numf = 0`: escribir
            # "Factura 0" es mostrar un número que no existe (el chequeo del
            # "0" estaba en link_origen pero no acá). TMT 2026-08-09.
            if num in ("", "0"):
                num = "s/n"
            factura_labels[int(rf["id_factura"])] = num
    # TMT 2026-08-03 (dueña, mismo pedido que ya se hizo con facturas): la
    # compra tiene UN número visible — el `numero` de la pantalla Compras. El
    # historial mostraba "Compra #473", que es el id INTERNO y no aparece en
    # ninguna otra pantalla, así que no había forma de saber cuál era.
    # OJO: esto cambia sólo la ETIQUETA. La URL sigue yendo por id_compra,
    # porque un `numero` puede coincidir con el id de OTRA compra.
    compra_numeros: dict[int, str] = {}
    compra_etiquetas: dict[int, str] = {}
    compra_conceptos: dict[int, str] = {}
    if id_compras:
        placeholder = ",".join(["%s"] * len(id_compras))
        rows_c = (
            db.fetch_all(
                f"SELECT c.id_compra, COALESCE(c.numero::text, '') AS numero, "
                f"       c.fecha, COALESCE(c.tipo, '') AS tipo, "
                f"       COALESCE(c.concepto, '') AS concepto, "
                f"       COALESCE(c.codigo_prov, '') AS prov, "
                f"       COALESCE(pr.nombre, '') AS proveedor "
                f"  FROM scintela.compra c "
                f"  LEFT JOIN scintela.proveedor pr ON pr.codigo_prov = c.codigo_prov "
                f" WHERE c.id_compra IN ({placeholder})",
                tuple(id_compras),
            )
            or []
        )
        for rc in rows_c:
            idc = int(rc["id_compra"])
            num = (rc.get("numero") or "").strip()
            # La referencia sin el día pegado del dBase (ver compras.referencia).
            ref = _compras_q.referencia(rc.get("concepto"), rc.get("fecha"))
            compra_conceptos[idc] = " · ".join(
                x for x in (_LBL.TIPOS_COMPRA_LABEL.get((rc.get("tipo") or "").upper()),
                            (f"fact. {ref}" if ref else "")) if x)
            # El mismo "SY · Seyquin Cia.Ltda." que arma el posdatado.
            prov = _posdat_q.nombre_visible(
                {"prov": rc.get("prov"), "proveedor": rc.get("proveedor")})
            if num and num != "0":
                # 🚨 El N° NO va en la etiqueta: es un correlativo interno que
                # la lista de /compras ni siquiera muestra (TMT 2026-08-09).
                compra_numeros[idc] = prov or f"#{idc}"
                continue
            # 232 compras (todas dbf-import) no tienen número: se las nombra
            # por su proveedor y su referencia, no por el id interno.
            resto = " · ".join(x for x in (prov, ref) if x)
            if resto:
                compra_etiquetas[idc] = f"Compra {resto}"
    # TMT 2026-08-07 (dueña) — el posdatado tiene UN número visible (`num`, el
    # de la columna N° de /posdat) y el historial mostraba el id interno. Peor:
    # el CONCEPTO del movimiento ya venía escrito con el `num`, así que la fila
    # tenía dos "#134" que no eran el mismo número. Sólo cambia la ETIQUETA —
    # la URL sigue yendo por id_posdat, igual que compras.
    #
    # 🚨 TMT 2026-08-09, sobre dos ediciones de importe de Andrés: *"quiero que
    # me diga es sueldos, si no cómo sé? a mí posdat 133 no me dice nada"*. Los
    # posdatados de provisión (YY/RT) NO tienen `num` —es 0—, así que caían al
    # id interno. Ahora la etiqueta lleva el NOMBRE: el proveedor, o el
    # concepto del posdatado cuando no hay proveedor cargado.
    posdat_etiquetas: dict[int, str] = {}
    posdat_nombres: dict[int, str] = {}
    if id_posdats:
        placeholder = ",".join(["%s"] * len(id_posdats))
        rows_pd = (
            db.fetch_all(
                f"SELECT p.id_posdat, COALESCE(p.num::text, '') AS num, "
                f"       COALESCE(p.concepto, '') AS concepto, "
                f"       COALESCE(p.prov, '') AS prov, "
                f"       COALESCE(pr.nombre, '') AS proveedor "
                f"  FROM scintela.posdat p "
                f"  LEFT JOIN scintela.proveedor pr ON pr.codigo_prov = p.prov "
                f" WHERE p.id_posdat IN ({placeholder})",
                tuple(id_posdats),
            )
            or []
        )
        for rp in rows_pd:
            idp = int(rp["id_posdat"])
            # La MISMA función que usa el audit al grabar el concepto: si se
            # escribieran por separado, la fila diría dos cosas distintas del
            # mismo posdatado.
            etiqueta = _posdat_q.etiqueta(rp)
            if etiqueta:
                posdat_etiquetas[idp] = etiqueta
            nombre = _posdat_q.nombre_visible(rp)
            if nombre:
                posdat_nombres[idp] = nombre
    banco_labels: dict[int, str] = {}
    banco_nos: dict[int, int] = {}
    if id_txbanco:
        placeholder = ",".join(["%s"] * len(id_txbanco))
        rows_b = (
            db.fetch_all(
                f"SELECT t.id_transaccion, t.documento, t.no_banco, "
                f"       COALESCE(b.nombre, '') AS nombre "
                f"  FROM scintela.transacciones_bancarias t "
                f"  LEFT JOIN scintela.banco b ON b.no_banco = t.no_banco "
                f" WHERE t.id_transaccion IN ({placeholder})",
                tuple(id_txbanco),
            )
            or []
        )
        for rb in rows_b:
            # TMT 2026-08-07: el `no_banco` para poder armar /bancos/<no>?id=<id>.
            # La pantalla de movimientos bancarios es POR BANCO, así que sin este
            # dato no hay URL posible — y era la única razón por la que el destino
            # de más volumen del historial (1.862 movs) no tenía link.
            if rb.get("no_banco") is not None:
                banco_nos[int(rb["id_transaccion"])] = int(rb["no_banco"])
            nm = (rb.get("nombre") or "").strip().title()  # PICHINCHA → Pichincha
            (rb.get("documento") or "").upper().strip()
            # Etiqueta corta — la dueña pidió "Pichincha", no "Banco mov #X".
            # Si el banco no tiene nombre, fallback al id.
            if nm:
                banco_labels[int(rb["id_transaccion"])] = nm
            else:
                banco_labels[int(rb["id_transaccion"])] = f"Banco #{rb['id_transaccion']}"

    # 🚨 TMT 2026-08-09: *"quiero que se vea concepto de gasto, a qué V1, V2,
    # etc fue aplicado"*. El `num` del gasto NO es un correlativo: es la
    # CATEGORÍA V1..V9 del legacy, la que arma GTEJ/GTIN/GGF en el balance. Un
    # "Gasto #553" era el id interno, y un "Gasto #7" se leía como el gasto
    # número siete cuando quería decir V7.
    gasto_etiquetas: dict[int, str] = {}
    if id_xgast:
        placeholder = ",".join(["%s"] * len(id_xgast))
        rows_g = (
            db.fetch_all(
                f"SELECT id_xgast, num FROM scintela.xgast "
                f"WHERE id_xgast IN ({placeholder})",
                tuple(id_xgast),
            )
            or []
        )
        for rg in rows_g:
            cat = _gastos_q.nombre_categoria(rg.get("num"))
            if cat:
                gasto_etiquetas[int(rg["id_xgast"])] = f"Gasto {cat}"
    # El anticipo en dólares se identifica por su CUENTA (la columna `cta`,
    # que es por la que filtra /dolares); el retiro, por el Cód. de 2 letras
    # de /retiros.
    # 🚨 TMT 2026-08-18 (dueña, mirando tres filas seguidas del mismo día):
    # *"poner el numero de AI"*. Las tres decían `USD AI` en Origen — la misma
    # celda para tres anticipos DISTINTOS (44/26, 33/26, 28), y la única forma
    # de distinguirlos era leer el concepto. El número de importación va
    # pegado a la cuenta, tal cual está escrito en el concepto del anticipo
    # (`44/26`, o sólo `44` si ese concepto todavía no tiene el año).
    dolares_etiquetas: dict[int, str] = {}
    if id_dolares:
        placeholder = ",".join(["%s"] * len(id_dolares))
        for rd in (db.fetch_all(
                f"SELECT id_dolares, COALESCE(cta, '') AS cta, "
                f"COALESCE(concepto, '') AS concepto FROM scintela.dolares "
                f"WHERE id_dolares IN ({placeholder})", tuple(id_dolares)) or []):
            cta = (rd.get("cta") or "").strip().upper()
            if cta:
                dolares_etiquetas[int(rd["id_dolares"])] = (
                    f"USD {cta}{_ref_anticipo(rd.get('concepto'))}"
                )
    retiro_etiquetas: dict[int, str] = {}
    if id_retiros:
        placeholder = ",".join(["%s"] * len(id_retiros))
        for rr in (db.fetch_all(
                f'SELECT id_retiro, COALESCE("de", \'\') AS de FROM scintela.retiros '
                f"WHERE id_retiro IN ({placeholder})", tuple(id_retiros)) or []):
            de = (rr.get("de") or "").strip().upper()
            if de:
                retiro_etiquetas[int(rr["id_retiro"])] = f"Retiro {de}"

    # El nombre del cliente, en un solo viaje para toda la página. La columna
    # muestra el CÓDIGO; el nombre es el tooltip.
    cliente_nombres: dict[str, str] = {}
    _codigos_cli = {c for c in
                    list(cliente_de_cheque.values()) + list(cliente_de_factura.values())
                    if c}
    if _codigos_cli:
        for rn in (db.fetch_all(
                "SELECT UPPER(TRIM(codigo_cli)) AS cod, COALESCE(nombre, '') AS nombre "
                "  FROM scintela.cliente "
                " WHERE UPPER(TRIM(codigo_cli)) = ANY(%s)",
                (sorted(_codigos_cli),)) or []):
            nombre = (rn.get("nombre") or "").strip()
            if nombre:
                cliente_nombres[rn["cod"]] = nombre

    def _cliente_de(r: dict) -> str:
        """Código de cliente de la fila, mirando origen y después destino.

        Un `cheque_aplicado_a_factura` lo tiene de los dos lados y es el
        mismo; un `cheque_creado` sólo en el origen; una `factura_emitida`, en
        los dos porque es self-ref. El orden importa únicamente para el cobro
        aplicado a una factura de OTRO código (los 7 pares de códigos
        duplicados abiertos): ahí manda el del cheque, que es con quien se
        hizo la operación.
        """
        for tabla, rid in ((r.get("origen_table"), r.get("origen_id")),
                           (r.get("destino_table"), r.get("destino_id"))):
            if not rid:
                continue
            if tabla == "cheque" and int(rid) in cliente_de_cheque:
                return cliente_de_cheque[int(rid)]
            if tabla == "factura" and int(rid) in cliente_de_factura:
                return cliente_de_factura[int(rid)]
        return ""

    def _override_label(t: str | None, rid, default_label: str) -> str:
        if t == "cheque" and rid and int(rid) in cheque_etiquetas:
            return cheque_etiquetas[int(rid)]
        if t == "factura" and rid and int(rid) in factura_labels:
            return f"Factura {factura_labels[int(rid)]}"
        if t == "transacciones_bancarias" and rid and int(rid) in banco_labels:
            return banco_labels[int(rid)]
        if t == "compra" and rid and int(rid) in compra_numeros:
            return f"Compra {compra_numeros[int(rid)]}"
        if t == "compra" and rid and int(rid) in compra_etiquetas:
            return compra_etiquetas[int(rid)]
        if t == "xgast" and rid and int(rid) in gasto_etiquetas:
            return gasto_etiquetas[int(rid)]
        if t == "dolares" and rid and int(rid) in dolares_etiquetas:
            return dolares_etiquetas[int(rid)]
        if t == "retiros" and rid and int(rid) in retiro_etiquetas:
            return retiro_etiquetas[int(rid)]
        return default_label

    # Mapeo id_factura → numf (solo si tiene numf válido) y id_cheque → no_cheque.
    # Estos diccionarios se pasan a link_origen/link_destino para que las URLs
    # usen el número real (visible al usuario) en lugar del id interno.
    factura_numfs = {
        k: v.lstrip("#")
        for k, v in factura_labels.items()
        if v and not v.startswith("#")  # solo cuando es numf real, no fallback "#id"
    }
    # Antes esto se deducía de la etiqueta ("si no empieza con # es un
    # número"): con "Dep. Pich." adentro, esa heurística armaba /cheques/Dep.
    # Pich. El número real ya viene aparte.
    cheque_nos = dict(cheque_nos_reales)
    # TMT 2026-07-14 (dueña "en origen podría decir de qué cuenta de OP"): batch
    # de las cuentas OP de los retiros presentes, para que el origen del retiro
    # OP diga "OP · <cuenta>" (ej "OP · AC 17-35") en vez de sólo "OP".
    _op_cuentas = queries.op_cuenta_por_retiro(
        [r.get("origen_id") for r in filas if (r.get("tipo") or "") == "retiro_op"]
    )
    for r in filas:
        r["label"] = queries.label(r.get("tipo") or "")
        # El TIPO no puede decir "Cheque" de algo que no lo es: el medio
        # exacto lo dice la columna Origen ("Dep. Pich.", "Efectivo"), así que
        # acá va la palabra genérica. "Cheque: alta" → "Cobro: alta".
        _oid = r.get("origen_id") if r.get("origen_table") == "cheque" else None
        if (_oid and int(_oid) in cheque_no_es_cheque
                and r["label"].startswith("Cheque")):
            r["label"] = "Cobro" + r["label"][len("Cheque"):]
        # TMT 2026-07-11 (dueña): no ofrecer "reversar" sobre un reverso (mov
        # terminal) — mostraba un modal que prometía facturas y no había.
        r["es_terminal"] = _es_terminal(r.get("tipo") or "")
        u, t = queries.link_origen(r, factura_numfs=factura_numfs, cheque_nos=cheque_nos,
                                   posdat_etiquetas=posdat_etiquetas, banco_nos=banco_nos)
        r["origen_url"] = u
        r["origen_label"] = _override_label(r.get("origen_table"), r.get("origen_id"), t)
        u, t = queries.link_destino(r, factura_numfs=factura_numfs, cheque_nos=cheque_nos,
                                    posdat_etiquetas=posdat_etiquetas, banco_nos=banco_nos)
        r["destino_url"] = u
        r["destino_label"] = _override_label(r.get("destino_table"), r.get("destino_id"), t)
        # row_url = a dónde va el click de la fila entera. Preferimos
        # origen_url; si no hay, destino_url.
        r["row_url"] = r["origen_url"] or r["destino_url"]
        # Columna CLIENTE: el CÓDIGO de 3 letras, que es como la dueña los
        # nombra y lo que se lee en el resto de las pantallas — *"nono codigo
        # del cliente nada mas no nombre entero"*. El nombre va en el tooltip.
        _cod_cli = _cliente_de(r)
        r["cliente_codigo"] = _cod_cli
        r["cliente_label"] = cliente_nombres.get(_cod_cli, "")
        r["cliente_url"] = f"/clientes/{_cod_cli}/cuenta" if _cod_cli else None
        # TMT 2026-07-09 (dueña): si el movimiento consolidó >1 item
        # (p.ej. "6 anticipo(s) → compra"), traer cada uno para poder
        # desplegarlos uno por uno en el historial.
        r["detalle"] = queries.detalle_consolidado(r.get("metadata"))
        # TMT 2026-07-14 (dueña "bastaba con que diga origen OP destino retiro"):
        # el mov_doble del retiro OP es self-ref sobre `retiros`, así que ambas
        # columnas salían "Retiro #N". Forzamos el ORIGEN a "OP" → se lee como
        # doble asiento OP → Retiro, sin bloque de explicación extra.
        # 🚨 TMT 2026-08-09: la columna CONCEPTO decía "Edit importe posdat
        # #133 152000.00 → 32000.00" — el id interno y la plata sin formato.
        # Las filas viejas ya están grabadas así, y son las que ella está
        # mirando: se reescribe al MOSTRAR, con el nombre del posdatado y los
        # importes que el propio movimiento guardó en su metadata.
        # 🚨 El concepto TAMBIÉN lleva el id adentro: "Cheque #102090 →
        # Factura #172730" y "Cheque # de TNZ" (1.410 filas medidas, sin
        # número porque no eran cheques). Se reescribe al mostrar con la misma
        # etiqueta de la columna ORIGEN, así la fila dice UNA sola cosa.
        # 🚨 TMT 2026-08-09, sobre "22758         7": *"no puede ser ese número
        # lo importante del movimiento"*. Era el N° de factura del proveedor
        # con el DÍA del mes pegado (formato de ancho fijo del dBase). Ahora la
        # fila dice QUÉ se compró y dónde mirar la factura: "Químicos ·
        # fact. 22758". La columna `compra.concepto` NO se toca — el dedup del
        # puente de fórmulas matchea por su primer token.
        # 🚨 TMT 2026-08-14, sobre el pase de la importación AC 38 (la fila
        # decía "Hilado · fact. 38"): *"acá quiero que diga AC y el número"*.
        # La regla de arriba se comía el nombre de la importación: el
        # `mov_doble` YA lo trae —`_nombrar_conversiones` lo escribe desde el
        # 31/07, por este mismo pedido— y acá se pisaba con el concepto de la
        # compra creada, que no nombra el anticipo por ningún lado.
        _cpt_compra = concepto_de_la_compra(r, compra_conceptos)
        if _cpt_compra:
            r["concepto"] = _cpt_compra
        _cid = r.get("origen_id") if r.get("origen_table") == "cheque" else None
        if _cid and r.get("concepto") and int(_cid) in cheque_etiquetas:
            _etq = cheque_etiquetas[int(_cid)]
            r["concepto"] = (r["concepto"]
                             .replace(f"Cheque #{int(_cid)}", _etq)
                             .replace("Cheque # ", _etq + " "))
        if (r.get("tipo") or "") == "posdat_edit_importe":
            _cpt = _concepto_edit_importe(r, posdat_nombres)
            if _cpt:
                r["concepto"] = _cpt
        if (r.get("tipo") or "") == "retiro_op":
            _cta = _op_cuentas.get(int(r["origen_id"])) if r.get("origen_id") else ""
            r["origen_label"] = ("OP · " + _cta) if _cta else "OP"
        # 🚨 TMT 2026-08-09: *"repetimos mucho sueldos"*. Un montón de
        # movimientos son de UN solo documento (la factura que se emite, el
        # cheque que se crea, el posdatado al que le editan el importe): el
        # mov_doble los guarda con el mismo origen y destino, y la grilla
        # escribía dos veces lo mismo al lado, comiéndose el ancho de lo que
        # sí informa. Medido: ~11.000 filas activas.
        # ⭐ Se compara la ETIQUETA, no el id: `retiro_op` es self-ref pero se
        # lee "OP · AC 17-35 → Retiro #17", que es justo lo que la dueña pidió
        # el 14/07 y no se toca. Y se marca en vez de borrar `destino_label`
        # porque el confirm de "deshacer reverso" lo nombra.
        # Lo último: la limpieza general del texto (después de los reemplazos
        # específicos, que ya pusieron el nombre del cheque o del posdatado).
        r["concepto"] = _concepto_legible(r.get("concepto"))
        # 🚨 Y una vez limpio, el concepto de "Cheque → Factura aplicada"
        # quedó diciendo exactamente lo mismo que las dos columnas de al lado
        # ("Dep. Pich. → Factura 172730"). Son 5.038 filas: la frase se va.
        # Tercera vez que aparece la misma regla hoy: repetir no informa.
        _o, _d = (r.get("origen_label") or ""), (r.get("destino_label") or "")
        if _o and _d and (r.get("concepto") or "").casefold() == \
                f"{_o} → {_d}".casefold():
            r["concepto"] = ""
        r["destino_repetido"] = bool(
            r.get("destino_label")
            and r.get("destino_label") == r.get("origen_label"))
        # 🚨 Y el caso hermano: "Compra SY · Seyquin Cia.Ltda. → Posdat SY ·
        # Seyquin Cia.Ltda.". No son la misma etiqueta, pero lo único distinto
        # es la primera palabra —qué es— y el resto es el mismo proveedor
        # escrito dos veces. Se deja el sustantivo solo: "→ Posdat".
        # Son 408 filas (compra_a_posdat 231 + gasto_a_posdat 177).
        if not r["destino_repetido"]:
            _o, _d = (r.get("origen_label") or ""), (r.get("destino_label") or "")
            _op, _, _oresto = _o.partition(" ")
            _dp, _, _dresto = _d.partition(" ")
            if _oresto and _oresto == _dresto:
                r["destino_label"] = _dp

    # ──────────────────────────────────────────────────────────────────
    # Construir `items` — una lista de "tarjetas" para el template.
    # Cada item es uno de:
    #   {"type": "batch", "batch_id": str, "count": int, "total": float,
    #    "fecha": date, "estado_global": str, "rows": [filas...]}
    #   {"type": "single", "row": fila}
    # Los hijos del batch se renderean adentro del card; las rows sueltas
    # van en su propio tbody. TMT 2026-05-16 — antes marcábamos head/child
    # en una lista plana y eso quedaba como filas separadas sin agrupación
    # visual fuerte. Ahora el template puede dibujar un card violeta por
    # batch con header propio.
    # ──────────────────────────────────────────────────────────────────
    from collections import defaultdict as _dd

    groups: dict = _dd(list)
    for r in filas:
        bid = r.get("batch_id")
        if bid:
            groups[str(bid)].append(r)

    # Helper para evitar doble-conteo en el total del batch.
    # Caso típico: /cheques/nuevo multi genera N cheque_creado + M
    # cheque_aplicado_a_factura. La suma literal cuenta dos veces la
    # plata (alta + aplicación a la misma plata). Mostramos sólo la
    # cuenta primaria (altas si existen, si no aplicaciones, si no todo).
    # TMT 2026-05-16: bug detectado por la dueña — batch de "5 movs · $6000"
    # cuando realmente entraron $4000 (2 cheques × $2000), las aplicaciones
    # sumaban otros $2000 de la misma plata.
    # TMT 2026-08-07 (dueña: *"poner en el historial cuando se cargan x
    # retenciones por x valor"*): la corrida de retenciones ya viene con
    # batch_id, así que cae acá. Se le pone nombre propio — "12 retenciones ·
    # $3.450,20" dice qué pasó; "12 movimientos" no.
    _NOMBRE_PRIMARIO = [
        ("cheque_creado", "cheque", "cheques"),
        ("retencion_asinfo_aplicada", "retención", "retenciones"),
        ("cheque_aplicado_a_factura", "aplicación", "aplicaciones"),
    ]

    def _primary_rows(siblings: list[dict]) -> tuple[list[dict], str]:
        for tipo_, sing, plur in _NOMBRE_PRIMARIO:
            elegidas = [x for x in siblings if x.get("tipo") == tipo_]
            if elegidas:
                return elegidas, (sing if len(elegidas) == 1 else plur)
        return siblings, ("movimiento" if len(siblings) == 1 else "movimientos")

    def _tipo_primario(por_tipo: dict) -> tuple[str | None, str]:
        """Igual que `_primary_rows` pero sobre el resumen que trae la base
        (para que el N y el $ sean los del batch ENTERO, no los de la página)."""
        for tipo_, sing, plur in _NOMBRE_PRIMARIO:
            if tipo_ in por_tipo:
                n = por_tipo[tipo_][0]
                return tipo_, (sing if n == 1 else plur)
        return None, ""

    try:
        resumenes = queries.resumen_batches(list(groups))
    except Exception:  # noqa: BLE001 -- el historial se abre igual
        resumenes = {}

    items: list[dict] = []
    seen_batches: set = set()
    for r in filas:
        bid = r.get("batch_id")
        if not bid:
            items.append({"type": "single", "row": r})
            continue
        bid_str = str(bid)
        if bid_str in seen_batches:
            continue  # ya emitido como parte del batch
        seen_batches.add(bid_str)
        siblings = groups[bid_str]
        # Determinar el "estado global" del batch — si todas activas,
        # activo; si todas reverso/reversado, reversado; si mezcla, mixto.
        estados = {x.get("estado") for x in siblings}
        if estados == {"activo"}:
            estado_global = "activo"
        elif estados <= {"reverso", "reversado"}:
            estado_global = "reversado"
        else:
            estado_global = "mixto"
        primary, label_primary = _primary_rows(siblings)
        # El N y el $ salen del batch ENTERO (query aparte), no de las filas
        # que entraron en esta página: si el batch es más grande que el
        # `limite`, contar la página daba un número más chico y creíble.
        resumen = resumenes.get(bid_str) or {}
        por_tipo = resumen.get("por_tipo") or {}
        tipo_prim, label_db = _tipo_primario(por_tipo)
        if tipo_prim:
            count_real, total_real = por_tipo[tipo_prim]
            label_primary = label_db
        elif resumen:
            count_real, total_real = resumen["n"], resumen["total"]
            label_primary = "movimiento" if count_real == 1 else "movimientos"
        else:  # sin resumen (batch de caja directa, o la query falló)
            count_real = len(primary)
            total_real = sum(float(x.get("importe") or 0) for x in primary)
        items.append(
            {
                "type": "batch",
                "batch_id": bid_str,
                # `count`/`total` = plata real (sin double-counting).
                "count": count_real,
                "total": total_real,
                "label_primary": label_primary,
                # `count_full` = N de mov_doble en el batch (para nota chiquita).
                "count_full": resumen.get("n", len(siblings)),
                "fecha": siblings[0].get("fecha_operacion"),
                "estado_global": estado_global,
                # ¿El botón "reversar todo el batch" sabe deshacer ESTOS tipos?
                # TMT 2026-08-07: agrupar las retenciones bajo una tarjeta les
                # sacaba el ↺ propio a las filas (los hijos del batch no traen
                # acciones) y las mandaba a un botón que aborta, porque el
                # reverso atómico sólo sabe de cheques. O sea: agrupar dejaba
                # una operación sin vuelta atrás. Cuando el batch no es
                # reversable de una, la tarjeta agrupa igual pero cada fila se
                # queda con su reverso individual.
                "reversable_en_batch": bool(
                    {x.get("tipo") for x in siblings} <= _TIPOS_BATCH_REVERSABLES
                ),
                "rows": siblings,
            }
        )

    if request.args.get("export") == "csv":
        # Aplanar metadata para CSV
        for r in filas:
            r["meta"] = str(r.get("metadata") or "")
        return csv_response(
            filas,
            columnas=[
                ("fecha_operacion", "Fecha"),
                ("tipo", "Tipo"),
                ("origen_table", "Origen tabla"),
                ("origen_id", "Origen id"),
                ("destino_table", "Destino tabla"),
                ("destino_id", "Destino id"),
                ("cliente_codigo", "Cliente"),
                ("cliente_label", "Cliente nombre"),
                ("importe", "Importe"),
                ("concepto", "Concepto"),
                ("estado", "Estado"),
                ("usuario", "Usuario"),
                ("id_reverso", "Reversado por id"),
                ("id_original", "Reversa al id"),
                ("meta", "Metadata"),
            ],
            filename=f"historial_{desde or 'all'}_{hasta or 'now'}.csv",
        )

    return render_template(
        "historial/lista.html",
        filas=filas,
        items=items,
        kpis=kpis,
        desde=desde,
        hasta=hasta,
        tipo=tipo,
        estado=estado,
        q=q or "",
        error=error,
        limite=limite,
        # TMT 2026-07-11 (dueña): paginación con flechita al pie.
        pagina=pagina,
        hay_mas=hay_mas,
        # TMT 2026-07-07 (dueña): pasar mis_origenes al template para que el
        # form de filtro y los links lo preserven → Alex puede filtrar/paginar
        # su historial sin perder el flag (sin él el gate lo bloqueaba).
        mis_origenes=mis_origenes,
        # TMT 2026-07-01: tipos que reversan INLINE (POST directo, sin wizard).
        tipos_inline=list(_PERMISO_REVERSO_INLINE.keys()),
    )


# =====================================================================
# Reverso central — punto único para deshacer cualquier mov_doble activo.
# TMT 2026-05-13. Antes cada flujo tenía su propio botón disperso (caja,
# cheques, bancos). El dispatcher route según tipo al handler existente
# y, si no hay handler específico, redirige al caller con instrucciones.
# =====================================================================


# 🚨 TMT 2026-08-09, barriendo el historial entero: *"¿algo más? mostrame
# ejemplos"*. Medido sobre los 19.465 movimientos activos:
#   · 15.539 conceptos escriben "#" delante de un número que casi siempre es
#     el VISIBLE (numf, no_cheque) — el "#" lo hace pasar por id interno, que
#     es exactamente la confusión de hoy;
#   · 4.508 terminan con el importe entre paréntesis, en formato de afuera
#     ("(155.10)") y repitiendo la columna Importe;
#   · 5.402 arrancan con "[backfill]", que es el nombre del proceso que los
#     cargó, no información;
#   · 480 dicen "#0" (las facturas del dBase que llegaron sin número);
#   · 177 dicen "Gasto #1", donde el 1 es la CATEGORÍA V1, no un correlativo.
# Todo se limpia al MOSTRAR: la columna `concepto` de mov_doble es el audit y
# no se toca.
_RE_BACKFILL = re.compile(r"^\[backfill\]\s*")
_RE_CLASIFICAR = re.compile(r"^Clasificar caja #\d+ como gasto (V\d)\s*:\s*")
_RE_GASTO_DOC = re.compile(r"\bGasto #([1-9])\s+\S+\s+—\s+")
_RE_GASTO_CAT = re.compile(r"\bGasto #([1-9])\b")
_RE_SIN_NUMERO = re.compile(r"\b(Factura|Cheque|Compra) #0\b")
_RE_NUMERAL = re.compile(r"\b(factura|cheque|compra|posdat) #(\d+)", re.I)
_RE_IMPORTE_AL_FINAL = re.compile(r"\s*\(\d+(?:\.\d+)?\)\s*$")


#: Los movimientos cuyo concepto NO se reescribe con el de la compra: el suyo
#: nombra la importación ("AC 38 · 4 anticipo(s) → compra N° 10176") y el de la
#: compra ("Hilado · fact. 38") no dice de qué anticipo salió.
CONCEPTO_PROPIO = frozenset({
    "bap_anticipo_a_compra",
    "bap_anticipo_a_compra_reverso",
})


def concepto_de_la_compra(row: dict, compra_conceptos: dict) -> str:
    """El concepto de la compra que toca la fila, o "" si no corresponde pisar.

    🚨 TMT 2026-08-09, sobre el concepto "22758         7": *"no puede ser ese
    número lo importante del movimiento"*. Desde entonces la fila que toca una
    compra dice QUÉ se compró ("Químicos · fact. 22758").

    🚨 TMT 2026-08-14, sobre el pase de la importación AC 38, que salía
    "Hilado · fact. 38": *"acá quiero que diga AC y el número"*. Esa regla se
    comía el nombre de la importación — el `mov_doble` YA lo trae (lo escribe
    `_nombrar_conversiones` desde el 31/07, por este mismo pedido) y acá se
    pisaba con el de la compra creada, que no nombra el anticipo por ningún
    lado. El pase se queda con el suyo.
    """
    if (row.get("tipo") or "") in CONCEPTO_PROPIO:
        return ""
    kid = (row.get("origen_id") if row.get("origen_table") == "compra"
           else (row.get("destino_id") if row.get("destino_table") == "compra"
                 else None))
    return (compra_conceptos.get(int(kid)) or "") if kid else ""


def _concepto_legible(texto: str | None) -> str:
    """El concepto del audit, escrito como lo lee una persona."""
    t = (texto or "").strip()
    if not t:
        return t
    t = _RE_BACKFILL.sub("", t)
    t = _RE_CLASIFICAR.sub(r"Caja → Gasto \1 · ", t)
    t = _RE_GASTO_DOC.sub(r"Gasto V\1 · ", t)
    t = _RE_GASTO_CAT.sub(r"Gasto V\1", t)
    t = _RE_SIN_NUMERO.sub(r"\1 s/n ·", t)
    t = _RE_NUMERAL.sub(r"\1 \2", t)
    t = _RE_IMPORTE_AL_FINAL.sub("", t)
    return " ".join(t.split())


def _concepto_edit_importe(row: dict, nombres: dict) -> str:
    """"152.000,00 → 32.000,00" — de cuánto a cuánto quedó el posdatado.

    Sale de la METADATA del movimiento (`importe_prev` / `importe_nuevo`, que
    el audit guarda desde el día uno) y del nombre del posdatado. Si falta
    cualquiera de las dos cosas, devuelve "" y la fila muestra el concepto tal
    como se grabó: mejor el texto viejo que un renglón a medias.
    """
    import json as _json

    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:  # noqa: BLE001
            return ""
    if not isinstance(meta, dict):
        return ""
    prev, nuevo = meta.get("importe_prev"), meta.get("importe_nuevo")
    if prev is None or nuevo is None:
        return ""
    rid = row.get("origen_id") or row.get("destino_id")
    nombre = (nombres or {}).get(int(rid)) if rid else ""
    if not nombre:
        return ""
    # 🚨 TMT 2026-08-09, segunda pasada: *"repetimos mucho sueldos pero nunca
    # veo de cuánto a cuánto cambió"*. El nombre salía TRES veces en la misma
    # fila (origen, destino y concepto) y con eso el dato que importa —los dos
    # importes— no entraba en la columna: "47.900,00 → 100,…". El nombre ya
    # está en ORIGEN; acá van los números y nada más.
    return f"{money_es(prev)} → {money_es(nuevo)}"


def _es_terminal(tipo: str) -> bool:
    """Un mov es 'terminal' cuando NO se puede volver a reversar: los
    'reverso_*' ya SON el deshacer de algo (reversar un reverso no tiene
    sentido) y los movimientos directos de caja/banco no pasan por el
    dispatcher. Mismos excluidos que check_salud_dia.py / validar_reversos.py.
    TMT 2026-07-11 (dueña): 'reversar un reverso' mostraba un modal vacío."""
    tipo = tipo or ""
    return (
        tipo.startswith("reverso_")
        or (tipo.startswith("caja_") and tipo.endswith("_directo"))
        or (tipo.startswith("banco_") and tipo.endswith("_directo"))
    )


def _row_md(id_mov_doble: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_mov_doble, tipo, origen_table, origen_id,
               destino_table, destino_id, importe, concepto, fecha_operacion,
               estado, id_reverso, id_original, metadata, usuario
          FROM scintela.mov_doble
         WHERE id_mov_doble = %s
        """,
        (id_mov_doble,),
    )


@historial_bp.route("/historial/<int:id_mov_doble>/reverso-preview", methods=["GET"])
@requiere_login
def reverso_preview(id_mov_doble: int):
    """Contenido de la confirmación de reverso en JSON, para el modal in-page.

    Texto corto + lista ADAPTATIVA de movimientos afectados. Reemplaza al
    `confirm()` nativo y a la página-wizard: el frente abre un cartel, y al
    aceptar postea a `accion_url` volviendo a la misma página. TMT 2026-07-11.
    """
    from flask import jsonify
    r = _row_md(id_mov_doble)
    if not r or id_mov_doble <= 0:
        return jsonify({"ok": False, "error": "Movimiento no encontrado."}), 404
    if r["estado"] in ("reverso", "reversado"):
        return jsonify({"ok": False, "error": f"Ya está {r['estado']}."}), 409
    if _es_terminal(r.get("tipo") or ""):
        return jsonify({
            "ok": False,
            "error": "Este movimiento ya es un reverso — no se reversa de nuevo.",
        }), 409

    tipo = r.get("tipo") or ""
    importe = float(r.get("importe") or 0)
    concepto = (r.get("concepto") or "").strip()
    label = concepto or tipo.replace("_", " ")

    detalle = {"Importe": f"$ {importe:,.2f}"}
    if concepto:
        detalle["Concepto"] = concepto

    titulo = f"Reversar — {label[:60]}"
    mensaje = ("Se crea el movimiento opuesto que lo compensa; "
               "el original queda en la historia.")

    # Lista adaptativa. El ÚNICO reverso inline de cheques desde el historial
    # es 'cheque_aplicado_a_factura' → DESAPLICAR esa aplicación (idéntico al
    # botón "Desaplicar" de la ficha): esa factura vuelve a cartera, sin tocar
    # al cliente ni el estado del cheque. NO es un rebote. El cartel describe
    # EXACTAMENTE lo que hace el POST (antes prometía rebote y listaba de más).
    # TMT 2026-07-11 (dueña, quality check).
    movimientos: list[dict] = []
    titulo_movs = None
    if r.get("tipo") == "cheque_aplicado_a_factura" and r.get("origen_id") and r.get("destino_id"):
        ch = db.fetch_one(
            "SELECT no_cheque FROM scintela.cheque WHERE id_cheque = %s",
            (r["origen_id"],),
        ) or {}
        no_ch = ch.get("no_cheque") or f"#{r['origen_id']}"
        fac = db.fetch_one(
            "SELECT COALESCE(numf::text, '') AS numf FROM scintela.factura WHERE id_factura = %s",
            (r["destino_id"],),
        ) or {}
        numf = (fac.get("numf") or "").strip() or f"#{r['destino_id']}"
        # Sólo las aplicaciones de ESTE cheque a ESTA factura (lo que desaplica
        # el POST). Se suman en UNA línea — un cheque puede aplicarse en partes
        # a la misma factura (no hay UNIQUE(cheque, factura)).
        apps = db.fetch_all(
            "SELECT importe, fechaing FROM scintela.chequesxfact "
            "WHERE id_cheque = %s AND id_fact = %s",
            (r["origen_id"], r["destino_id"]),
        ) or []
        _monto_desaplica = sum(float(a.get("importe") or 0) for a in apps)
        _fecha = max((a["fechaing"] for a in apps if a.get("fechaing")), default=None)
        movimientos = [{
            "texto": f"Factura {numf}",
            "importe": _monto_desaplica,
            "detalle": (_fecha.strftime("%d/%m/%Y") if _fecha else ""),
        }] if apps else []
        detalle = {
            "Cheque": f"N° {no_ch}",
            "Factura": numf,
            "Vuelve a cartera": f"$ {_monto_desaplica:,.2f}",
        }
        if movimientos:
            titulo_movs = "Vuelve a cartera"
        titulo = f"Desaplicar cheque {no_ch}"
        mensaje = (
            f"Se deshace la aplicación a la factura {numf}: vuelve a cartera. "
            "Equivale a 'Desaplicar' en la ficha del cheque; no afecta al "
            "cliente ni cambia el estado del cheque."
        )

    # TMT 2026-07-14 (dueña "sin 100 explicaciones"): reverso simple del retiro OP.
    if tipo == "retiro_op":
        titulo = "Reversar retiro OP"
        mensaje = "Se borra el retiro y la línea OP vuelve a subir su restante."

    # Siempre posteamos a reverso-inline: ejecuta los tipos atómicos en el acto
    # y, para los complejos, redirige al wizard de siempre (fallback graceful).
    accion_url = url_for("historial.reversar_mov_inline", id_mov_doble=id_mov_doble)

    return jsonify({
        "ok": True,
        "titulo": titulo,
        "mensaje": mensaje,
        "detalle": detalle,
        "movimientos": movimientos,
        "titulo_movimientos": titulo_movs,
        "accion_url": accion_url,
        "confirm_label": "Reversar",
    })


# Mapeo tipo → (endpoint_de_confirmacion, kwargs builder).
# El builder recibe la fila mov_doble y devuelve dict de kwargs para url_for.
#
# AUDIT 2026-05-13: cada entrada acá tiene que reversar TODO lo que hizo
# el alta — saldos, side-effects, tablas linked. Los handlers que NO
# cumplen están comentados con una nota _BLOQUEADO_ y disparan un mensaje
# al usuario explicando dónde reversar manualmente. La regla es:
# **mejor mostrar mensaje claro que romper saldos silenciosamente**.
_REVERSO_DISPATCH = {
    # ── OK validados ─────────────────────────────────────────────────
    # Cheque emitido por bancos — bancos.reversar_cheque_emitido sí
    # inserta ND compensatorio Y reabre posdat / inserta caja S /
    # inserta retiro negativo / marca xgast='Y'. Verificado audit #1-4.
    "cheque_emitido_proveedor": (
        "bancos.reversar_cheque_emitido",
        lambda r: {"id_transaccion": r["origen_id"]},
    ),
    "cheque_emitido_retiro": (
        "bancos.reversar_cheque_emitido",
        lambda r: {"id_transaccion": r["origen_id"]},
    ),
    "cheque_emitido_caja": (
        "bancos.reversar_cheque_emitido",
        lambda r: {"id_transaccion": r["origen_id"]},
    ),
    "cheque_emitido_gasto": (
        "bancos.reversar_cheque_emitido",
        lambda r: {"id_transaccion": r["origen_id"]},
    ),
    # TMT 2026-07-08 (dueña "todo reversible"): cheque emitido SIN side-effect
    # (tipo 'otro' o proveedor sin posdat) y anticipo USD. reversar_cheque_emitido
    # compensa el CH con NC; para anticipo_usd además anula la fila dolares.
    "cheque_emitido_otro": (
        "bancos.reversar_cheque_emitido",
        lambda r: {"id_transaccion": r["origen_id"]},
    ),
    "cheque_emitido_anticipo_usd": (
        "bancos.reversar_cheque_emitido",
        lambda r: {"id_transaccion": r["origen_id"]},
    ),
    # Caja con side effect — caja.reversar deshace los side-effects vía
    # aplicar_side_effect(inverso=True). Verificado audit #5,6,7,9,10.
    "caja_s_to_transfer_banco": ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]}),
    "caja_s_to_retiro_socio": ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]}),
    "caja_s_to_dolares": ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]}),
    # caja → compra_proveedor: ahora anula la compra original en lugar de
    # crear una compensación negativa. TMT 2026-05-13.
    "caja_s_to_compra_proveedor": ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]}),
    "caja_e_to_transfer_banco": ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]}),
    "caja_e_to_dolares": ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]}),
    # Caja simple — sin side effect, sólo compensación en caja. Verificado #11,12.
    "caja_e_simple": ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]}),
    "caja_s_simple": ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]}),
    # Caja CB (cobro con cheque) — ahora marca nota en el cheque + caja S
    # compensa. La dueña tiene que revisar el stat del cheque manualmente
    # (no sabemos el stat previo al cobro). TMT 2026-05-13.
    "caja_cb_simple": ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]}),
    # Compra a crédito sin pago — anular borra posdat. No hay side-effect
    # bancario que reversar. Verificado audit #20.
    "compra_a_posdat": ("compras.confirmar_anulacion", lambda r: {"id_compra": r["origen_id"]}),
    # Endoso de cheque — wizard dedicado nuevo. Anula la compra hermana
    # + restaura cheque a Z + linkea mov_doble. TMT 2026-05-13.
    "endoso_cheque_a_proveedor": (
        "cheques.confirmar_reverso_endoso",
        lambda r: {"id_cheque": r["origen_id"]},
    ),
    # Factura emitida/devolución — ahora facturas.anular linkea mov_doble
    # como reversado. TMT 2026-05-13.
    "factura_emitida": ("facturas.confirmar_anulacion", lambda r: {"id_factura": r["origen_id"]}),
    "factura_devolucion": ("facturas.confirmar_anulacion", lambda r: {"id_factura": r["origen_id"]}),
    # Gastos — anular marca stat='Y' + linkea mov_doble. La compensación
    # del pago (caja/banco) si era pagado al contado queda a cargo del
    # usuario en el módulo correspondiente. TMT 2026-05-13.
    "gasto_simple": ("gastos.confirmar_anulacion", lambda r: {"id_xgast": r["origen_id"]}),
    "gasto_a_posdat": ("gastos.confirmar_anulacion", lambda r: {"id_xgast": r["origen_id"]}),
    "gasto_pagado_caja": ("gastos.confirmar_anulacion", lambda r: {"id_xgast": r["origen_id"]}),
    "gasto_pagado_pichincha": ("gastos.confirmar_anulacion", lambda r: {"id_xgast": r["origen_id"]}),
    "gasto_pagado_internacional": ("gastos.confirmar_anulacion", lambda r: {"id_xgast": r["origen_id"]}),
    # Compras pagadas — compras.anular ahora compensa caja/banco/dolares
    # según cuenta_pagada + id_transaccion. TMT 2026-05-13.
    "compra_pagada_caja": ("compras.confirmar_anulacion", lambda r: {"id_compra": r["origen_id"]}),
    "compra_pagada_pichincha": ("compras.confirmar_anulacion", lambda r: {"id_compra": r["origen_id"]}),
    "compra_pagada_internacional": ("compras.confirmar_anulacion", lambda r: {"id_compra": r["origen_id"]}),
    "compra_pago_parcial": ("compras.confirmar_anulacion", lambda r: {"id_compra": r["origen_id"]}),
    "compra_anticipo_dolares": ("compras.confirmar_anulacion", lambda r: {"id_compra": r["origen_id"]}),
    "compra_saldo_a_posdat": ("compras.confirmar_anulacion", lambda r: {"id_compra": r["origen_id"]}),
    "compra_backfill": ("compras.confirmar_anulacion", lambda r: {"id_compra": r["origen_id"]}),
    # Aplicación de cheque a factura — desaplicar granular (sin reversar
    # el cheque entero). Borra la chequesxfact específica y recalcula
    # factura.abono/saldo/stat. TMT 2026-05-13.
    "cheque_aplicado_a_factura": (
        "cheques.confirmar_desaplicar",
        lambda r: {"id_cheque": r["origen_id"], "id_factura": r["destino_id"]},
    ),
    # Transferencia banco↔banco — wizard nuevo, NC en origen + CH en
    # destino, atómico. TMT 2026-05-13.
    "transfer_banco_banco": ("bancos.reversar_transferencia", lambda r: {"id_mov_doble": r["id_mov_doble"]}),
    # Conversión BAP anticipo USD → compra — wizard que restaura los anticipos
    # a vivos + borra la compra creada, atómico. TMT 2026-06-26.
    "bap_anticipo_a_compra": (
        "dolares.reversar_conversion",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    # Activación de maquinaria — restaura anticipos consumidos + borra las
    # cuotas (posdat) + borra la máquina, atómico. TMT 2026-07-08 (dueña).
    "activacion_maquinaria": (
        "activos.reversar_activacion",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    # Capital aporte/retiro — wizards nuevos atómicos. TMT 2026-05-13.
    "aporte_capital_a_caja": ("capital.reversar_aporte", lambda r: {"id_capital": r["origen_id"]}),
    "aporte_capital_a_pichincha": ("capital.reversar_aporte", lambda r: {"id_capital": r["origen_id"]}),
    "aporte_capital_a_internacional": ("capital.reversar_aporte", lambda r: {"id_capital": r["origen_id"]}),
    "retiro_socio_de_caja": ("capital.reversar_retiro", lambda r: {"id_retiro": r["origen_id"]}),
    "retiro_socio_de_pichincha": ("capital.reversar_retiro", lambda r: {"id_retiro": r["origen_id"]}),
    "retiro_socio_de_internacional": ("capital.reversar_retiro", lambda r: {"id_retiro": r["origen_id"]}),
    # Cheque creado (alta) — mapea a anular_error_carga, que borra el
    # cheque + sus aplicaciones + posdat hermana atómicamente.
    # TMT 2026-05-15: agregado para deshacer creaciones erradas desde
    # el historial (el flujo de multi-cheque puede dejar 4 cheques
    # colgados que la usuaria necesita anular todos juntos).
    "cheque_creado": ("cheques.anular_error_carga", lambda r: {"id_cheque": r["origen_id"]}),
    "cheque_anticipo_espejo": ("cheques.anular_error_carga", lambda r: {"id_cheque": r["origen_id"]}),
    # Clasificación de caja S como gasto V1..V9 — el reverso desclasifica
    # el xgast (lo anula) y deja la caja S libre para re-asignar. NO toca
    # la caja S misma (la plata sí salió). TMT 2026-05-16: handler agregado
    # para cerrar el único tipo huérfano del dispatcher (113 filas activas
    # eran de este tipo, sin botón de reverso). Cierra el flow que la dueña
    # más usaba diariamente y antes no se podía deshacer.
    "caja_s_to_xgast": ("gastos.confirmar_desclasificar", lambda r: {"id_xgast": r["destino_id"]}),
    # TMT 2026-05-19 — movimientos bancarios simples (DE / NC / ND) creados
    # desde la action bar de /bancos. Cada uno compensa con doc de signo
    # opuesto vía bancos.reversar_movimiento_simple:
    #   deposito (DE)     → CH
    #   nota_credito (NC) → CH
    #   nota_debito (ND)  → NC
    "deposito": ("bancos.confirmar_reverso_movimiento_simple", lambda r: {"id_mov_doble": r["id_mov_doble"]}),
    "nota_credito": (
        "bancos.confirmar_reverso_movimiento_simple",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    "nota_debito": (
        "bancos.confirmar_reverso_movimiento_simple",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    # TMT 2026-07-29 — el chequeo de salud los listaba como "sin reverso
    # automatizado" y era mentira: los dos tenían wizard de confirmación
    # propio desde antes, sólo faltaba enchufarlos acá. `cheque_depositado`
    # era el tipo con MÁS movs activos del sistema (913).
    #
    # deshacer_deposito devuelve el cheque a cartera (Z) y BAJA el depósito
    # del banco por su importe (si era el único cheque del depósito, borra el
    # movimiento). No es rebote: no toca al cliente.
    "cheque_depositado": (
        "cheques.deshacer_deposito",
        lambda r: {"id_cheque": r["origen_id"]},
    ),
    # Protesto marcado por error (B→1). TMT 2026-08-13 (dueña: "protesté por
    # confusión … y sigue protestado"). El cheque vuelve a su estado anterior y
    # la ND del protesto + su gasto se cancelan con NC. Va por id_mov_doble
    # porque la metadata de ESE protesto es la que dice de dónde venía: un
    # cheque puede protestarse, re-depositarse y protestarse de nuevo.
    "cheque_devuelto": (
        "cheques.deshacer_devuelto",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    # Gasto V1..V9 creado desde un movimiento de banco. Misma forma exacta que
    # `caja_s_to_xgast`: destino_id ES el id_xgast. Desclasificar anula el
    # gasto y deja el movimiento de banco libre para re-asignar — NO toca el
    # movimiento bancario, que es real.
    "banco_clasificado_gasto": (
        "gastos.confirmar_desclasificar",
        lambda r: {"id_xgast": r["destino_id"]},
    ),
    # Edición de abono a mano (sin cheque). El wizard vuelve al abono que
    # guardó la metadata y recomputa saldo/stat. Toma el id del MOV, no de la
    # factura: una misma factura puede tener varias ediciones y hay que
    # deshacer la que se eligió, no la última.
    "factura_abono_manual": (
        "facturas.reversar_abono_manual",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    # Cobro de cheque en EFECTIVO (banco=99): el alta del cheque mete una
    # fila 'E' en caja y el cheque queda stat 'C'. El deshacer correcto es
    # anular el cheque por error de carga, que para stat 'C' inserta la 'S'
    # compensatoria en caja (ver la tabla de `anular_por_error_de_carga`).
    # NO va por `caja.confirmar_reverso`: ese busca el mov_doble por
    # (origen_table='caja', origen_id) y acá el origen es el CHEQUE, así que
    # no encontraría nada, se caería al re-parse del concepto y podría
    # inventar un side-effect que nunca existió — el bug #3 del 05/06.
    "cheque_efectivo_to_caja": (
        "cheques.anular_error_carga",
        lambda r: {"id_cheque": r["origen_id"]},
    ),
    # Cheque de cartera cancelado (X) porque un anticipo lo cubría. El wizard
    # lo devuelve a su stat anterior y restaura la posdat hermana que la
    # cancelación había borrado; el espejo NB=98 (saldo a favor) NO se ajusta
    # solo y la pantalla lo dice con números.
    "cheque_cancelado_por_anticipo": (
        "cheques.reversar_cancelacion_anticipo",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    # Cheque anulado por "error de carga". TMT 2026-07-30 (dueña: "tiene que
    # tener pantalla") — era el único del módulo sin vuelta atrás: el ↺ se
    # mostraba igual y el dispatcher contestaba "aún no tiene reverso
    # automatizado". El wizard devuelve el cheque a su estado anterior,
    # compensa la ND / el movimiento de caja con su opuesta y re-aplica las
    # facturas desde el snapshot.
    "reverso_cheque_administrativo": (
        "cheques.deshacer_anulacion_error_carga",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    # Cambio de estado de una factura (Z/A/T, por el selector de la fila).
    # TMT 2026-08-03 (dueña: *"totalicé mal y no me deja volver"*) — estos dos
    # estaban en _REVERSO_BLOQUEADO, mandándola al selector del estado de
    # cuenta; pero una factura en 'T' SALE del listado y queda detrás del botón
    # "Ver totalizadas": el consejo era cierto y la dejaba trabada igual. El
    # mov snapshotea (stat, abono, saldo) previos ⇒ el reverso es EXACTO: el
    # wizard lo muestra con números y lo aplica.
    "factura_stat_cambio": (
        "informes.factura_cambio_stat_confirmar",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    "factura_cerrada_a_t": (
        "informes.factura_cambio_stat_confirmar",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
    # TOTALIZAR estado de cuenta. TMT 2026-08-07 (dueña, sobre el ↺ de #22176):
    # nació irreversible y el dispatcher contestaba "aún no tiene reverso
    # automatizado". Desde este commit el mov snapshotea `antes`/`despues`/
    # `links` ⇒ el reverso es EXACTO. Los totalizar viejos (sin snapshot) los
    # frena el propio wizard con un mensaje que lo explica.
    "totalizar_estado_cuenta": (
        "informes.totalizar_reverso_confirmar",
        lambda r: {"id_mov_doble": r["id_mov_doble"]},
    ),
}

# Tipos que NO se reversan desde acá — el dispatcher muestra un toast
# claro al usuario con la ruta correcta. Audit 2026-05-13: estos handlers
# no deshacen completamente la operación (dejan saldos inconsistentes,
# compras fantasma, etc.). Los manejamos manualmente hasta que cada uno
# tenga un reverso atómico.
#
# TMT 2026-07-29 — este dict estuvo VACÍO desde el audit de mayo, así que su
# mensaje ("Reverso no automatizado para este tipo. {mensaje}") nunca se
# mostró y todos estos tipos caían en el genérico "Tipo 'X' aún no tiene
# reverso automatizado" — que es FALSO: cada uno de los de abajo SÍ se
# deshace, sólo que desde otra pantalla. Eran 521 movs activos a los que la
# app le decía a la dueña que no había vuelta atrás cuando sí había.
#
# El reverso de estos vive en un POST (por período o por lote), no en un GET
# con id, así que el dispatcher no puede redirigir: lo correcto es decir
# dónde está el botón.
# Movimientos que SON un reverso y aun así se pueden deshacer. Un reverso
# normalmente es terminal —deshacer un reverso sería re-hacer la operación— pero
# la anulación por error de carga es distinta: no revierte una operación real,
# marca un cheque como mal cargado. Si el error fue anularlo, tiene que haber
# vuelta atrás. TMT 2026-07-30 (dueña: "tiene que tener pantalla"); el caso
# testigo es el anticipo de CJE #100934, que hubo que re-crear entero.
_REVERSO_DE_REVERSO_OK = {"reverso_cheque_administrativo"}

_REVERSO_BLOQUEADO = {
    # TMT 2026-08-07: `retencion_asinfo_aplicada` salió de acá. El consejo era
    # "deshacelas por período", y sirve para una corrida entera que salió mal —
    # pero para UNA retención aplicada por error deshacer el mes se lleva
    # puestas todas las que sí estaban bien. Ahora tiene reverso propio
    # (`retenciones.queries.desaplicar_por_mov`), que va por id_mov_doble
    # porque el reverso por número no encuentra las facturas de origen dBase
    # (numf_completo NULL).
    "dolares_anticipo": (
        "Deshacelo desde /dolares con la ✕ del anticipo #{origen_id}. "
        "Cancela el anticipo y revierte también el movimiento bancario "
        "linkeado."
    ),
    "anticipo_neteado": (
        "Es parte de un neteo. Se deshace completo desde el estado de "
        "cuenta del cliente → «Deshacer neteo», que restaura los cheques y "
        "los anticipos del batch juntos. Deshacer sólo esta pata dejaría "
        "el neteo a medias."
    ),
    "neteo_estado_cuenta": (
        "Es el evento del neteo. Deshacelo desde el estado de cuenta del "
        "cliente → «Deshacer neteo»: restaura los cheques y anticipos del "
        "batch y re-aplica lo que se había desaplicado."
    ),
    "importacion_anticipo": (
        "Deshacelo desde /importaciones con la ✕ del movimiento: borra el "
        "anticipo y su ND del banco como par atómico, y recalcula el "
        "anticipo aplicado de la importación."
    ),
    # `factura_cerrada_a_t` y `factura_stat_cambio` VIVÍAN acá, con el mensaje
    # "volvé a cambiarlo desde el selector del estado de cuenta". Pasaron al
    # dispatch el 2026-08-03: ver el comentario en _REVERSO_DISPATCH.
}


# TMT 2026-07-01 (dueña): el reverso ya NO exige informes.ver. Es un
# dispatcher que redirige al wizard de cada tipo, y ESE wizard tiene su
# propio @requiere_permiso (cheques.aplicar, compras.anular, etc.). Pedir
# informes.ver acá bloqueaba a Alex (que puede aplicar/anular cheques pero
# NO tiene informes.ver) de reversar sus propias cobranzas. Ahora el gate
# real es el permiso de la OPERACIÓN, no el de Informes.
@historial_bp.route("/historial/<int:id_mov_doble>/reverso", methods=["GET"])
@requiere_login
def reversar_mov(id_mov_doble: int):
    """Dispatcher central: route al wizard de reverso correspondiente.

    Mira el `tipo` del mov_doble y redirige a su endpoint de confirmación.
    Si no hay handler para ese tipo, muestra un aviso explicando dónde
    hacerlo manualmente.
    """
    if id_mov_doble <= 0:
        # Fila legacy de caja/banco (id sintético negativo, sin mov_doble):
        # el reverso unificado no la maneja. Guiar en vez de 404.
        flash(
            "Ese movimiento es un depósito/caja antiguo que no se reversa desde "
            "el historial. Reversalo desde la ficha del cheque o desde el banco.",
            "warn",
        )
        return redirect(request.referrer or url_for("historial.lista"))
    r = _row_md(id_mov_doble)
    if not r:
        abort(404)
    if (r["estado"] in ("reverso", "reversado")
            and not (r["estado"] == "reverso"
                     and (r.get("tipo") or "") in _REVERSO_DE_REVERSO_OK)):
        flash(
            f"Este movimiento ya está {r['estado']} — no se puede volver a reversar.",
            "warn",
        )
        return redirect(url_for("historial.lista"))

    tipo = r.get("tipo") or ""
    # Si está bloqueado explícitamente — mostrar mensaje específico.
    if tipo in _REVERSO_BLOQUEADO:
        mensaje = _REVERSO_BLOQUEADO[tipo].format(
            origen_id=r.get("origen_id") or "?",
            destino_id=r.get("destino_id") or "?",
        )
        flash(f"Reverso no automatizado para este tipo. {mensaje}", "warn")
        return redirect(url_for("historial.lista"))

    handler = _REVERSO_DISPATCH.get(tipo)
    # TMT 2026-07-08 (dueña "todo reversible"): caja.reversar re-deriva el
    # side-effect desde la propia fila de caja (NO depende del tipo del
    # mov_doble), así que CUALQUIER 'caja_*' se reversa seguro por acá. Cubre
    # los combos que faltaban en el dispatch por mismatch de string
    # (caja_s_to_gasto, caja_e_to_retiro_socio, caja_e_to_compra_proveedor…).
    if not handler and tipo.startswith("caja_"):
        handler = ("caja.confirmar_reverso", lambda r: {"id_caja": r["origen_id"]})
    if not handler:
        # Sin handler específico Y no en lista de bloqueados — guía genérica.
        sugerencia = {
            "transfer_usd_cuenta_cuenta": "Reversa desde /dolares.",
            "gasto_simple": "Reversa desde /gastos (anular).",
            "gasto_a_posdat": "Reversa desde /gastos (anular).",
            "gasto_pagado_caja": "Reversa desde /gastos (anular).",
            "gasto_pagado_pichincha": "Reversa desde /gastos (anular).",
            "gasto_pagado_internacional": "Reversa desde /gastos (anular).",
        }.get(tipo, f"Tipo '{tipo}' aún no tiene reverso automatizado.")
        flash(
            f"Reverso no disponible desde acá. {sugerencia}",
            "warn",
        )
        return redirect(url_for("historial.lista"))

    endpoint, kwargs_fn = handler
    try:
        kwargs = kwargs_fn(r)
        return redirect(url_for(endpoint, **kwargs))
    except Exception as e:
        flash_exc("No pude armar el reverso", e)
        return redirect(url_for("historial.lista"))


# =====================================================================
# Reverso INLINE (sin ir a otra pantalla) — TMT 2026-07-01 (dueña).
# La dueña: "cuando pongo reversa no me lleves a otra pantalla". Para los
# tipos con un reverso atómico simple, el botón "↺ reversar" del historial
# hace un POST directo acá, ejecuta el reverso y vuelve a la MISMA lista
# (referrer). El permiso se chequea por la OPERACIÓN, no por informes.ver,
# así Alex (cheques.aplicar) puede reversar sus cobranzas sin ver Informes.
# =====================================================================

# tipo → permiso de la operación (el que se necesita para reversarla inline).
_PERMISO_REVERSO_INLINE = {
    "cheque_aplicado_a_factura": "cheques.aplicar",
    # Deshacer UNA retención aplicada por error. Mismo permiso que aplicarlas.
    "retencion_asinfo_aplicada": "facturas.crear",
    "retencion_movida_del_abono": "facturas.crear",
    # TMT 2026-07-06 (Andrés): "Tipo 'retiro_op' aún no tiene reverso
    # automatizado" — ahora sí: deshacer_op (borra retiro + imputación y
    # devuelve el monto a la fila posdat OP si el retiro la había bajado).
    "retiro_op": "posdat.editar",
}


def _next_seguro() -> str:
    """URL de retorno tras el reverso: el `next` del form si es interno,
    sino el referrer del historial, sino la lista."""
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    ref = request.referrer or ""
    if "/historial" in ref:
        return ref
    return url_for("historial.lista")


@historial_bp.route("/historial/<int:id_mov_doble>/reverso-inline", methods=["POST"])
@requiere_login
def reversar_mov_inline(id_mov_doble: int):
    """Reverso directo (sin wizard) para tipos atómicos simples.

    Hoy: cheque_aplicado_a_factura → desaplicar_factura. Para cualquier otro
    tipo, cae al dispatcher de wizard (reversar_mov). El permiso lo da la
    operación (no informes.ver).
    """
    if id_mov_doble <= 0:
        flash(
            "Ese movimiento es un depósito/caja antiguo que no se reversa desde "
            "el historial. Reversalo desde la ficha del cheque o desde el banco.",
            "warn",
        )
        return redirect(_next_seguro())
    r = _row_md(id_mov_doble)
    if not r:
        abort(404)
    if (r["estado"] in ("reverso", "reversado")
            and not (r["estado"] == "reverso"
                     and (r.get("tipo") or "") in _REVERSO_DE_REVERSO_OK)):
        flash(
            f"Este movimiento ya está {r['estado']} — no se puede volver a reversar.",
            "warn",
        )
        return redirect(_next_seguro())

    tipo = r.get("tipo") or ""
    permiso = _PERMISO_REVERSO_INLINE.get(tipo)
    if not permiso:
        # Sin handler inline — mandamos al dispatcher de wizard de siempre.
        return redirect(url_for("historial.reversar_mov", id_mov_doble=id_mov_doble))

    # Gate por la OPERACIÓN (mismo criterio "404 si no tenés permiso").
    if not tiene_permiso(permiso):
        return render_template("404.html"), 404

    usuario = (g.user or {}).get("username", "web")

    # TMT 2026-08-07: la retención tiene su propio reverso atómico, que
    # restaura abono/retención/saldo/stat del snapshot y borra la fila de
    # scintela.retencion. Maneja su tx (no entra al db.tx() genérico de abajo).
    if tipo in ("retencion_asinfo_aplicada", "retencion_movida_del_abono"):
        from modules.retenciones import queries as _ret_q
        try:
            res = _ret_q.desaplicar_por_mov(id_mov_doble, usuario=usuario)
            flash(
                f"Retención deshecha: la factura {res['numf']} "
                f"({res['codigo_cli']}) vuelve a deber "
                f"$ {res['saldo_restaurado']:,.2f} y queda en "
                f"{res['stat_restaurado']}. (Si estaba separada del abono, la "
                f"plata volvió a Abonado.)", "ok")
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("No pude deshacer la retención (rollback total)", e)
        return redirect(_next_seguro())

    # TMT 2026-07-06: retiro_op tiene reverso atómico propio — deshacer_op
    # maneja su propia transacción (no entra al db.tx() genérico de abajo).
    if tipo == "retiro_op":
        from modules.retiros import queries as _ret_q
        try:
            _imp = db.fetch_one(
                "SELECT id_op_retiro_linea FROM scintela.op_retiro_linea "
                "WHERE id_retiro = %s ORDER BY id_op_retiro_linea DESC LIMIT 1",
                (int(r.get("origen_id") or 0),),
            )
            if not _imp:
                flash(
                    "No encuentro la imputación de ese retiro OP (¿ya fue "
                    "deshecho, o el retiro se cargó sin línea?). Si el retiro "
                    "sigue en la fila OP de /posdat, deshacelo con el ✕ de ahí.",
                    "warn",
                )
                return redirect(_next_seguro())
            res = _ret_q.deshacer_op(int(_imp["id_op_retiro_linea"]), usuario=usuario)
            flash(
                f"Retiro OP reversado: $ {res['monto']:,.2f}. La cuenta OP "
                f"volvió a subir y el retiro se borró de /retiros.",
                "ok",
            )
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("No pude reversar el retiro OP (rollback total)", e)
        return redirect(_next_seguro())

    from modules.cheques import queries as _ch_q

    try:
        with db.tx() as conn:
            if tipo == "cheque_aplicado_a_factura":
                res = _ch_q.desaplicar_factura(
                    id_cheque=int(r["origen_id"]),
                    id_factura=int(r["destino_id"]),
                    usuario=usuario,
                    motivo="reverso inline desde historial",
                    conn=conn,
                )
        flash(
            f"Movimiento reversado: {r.get('concepto') or ('#' + str(id_mov_doble))}.",
            "ok",
        )
    except Exception as e:
        flash_exc("No pude reversar el movimiento (rollback total)", e)

    return redirect(_next_seguro())


@historial_bp.route("/historial/<int:id_mov_doble>/deshacer-reverso", methods=["POST"])
@requiere_login
def deshacer_reverso(id_mov_doble: int):
    """Deshace un reverso de cheque emitido hecho por ERROR (des-reverso).

    Revive el cheque original, restaura el gasto/posdat/caja/retiro que el
    reverso había deshecho, y elimina la NC compensatoria del banco. Gateado
    por `cheques.anular` (misma operación que reversar). TMT 2026-07-24: Alex
    reversó cheques que creyó duplicados y no lo eran.
    """
    if not tiene_permiso("cheques.anular"):
        return render_template("404.html"), 404
    r = _row_md(id_mov_doble)
    if not r:
        abort(404)
    if (r.get("tipo") or "") != "reverso_cheque_emitido":
        flash(
            "Ese movimiento no es un reverso de cheque emitido — no se puede "
            "deshacer con esta acción.",
            "warn",
        )
        return redirect(_next_seguro())
    usuario = (g.user or {}).get("username", "web")
    motivo = (request.form.get("motivo") or "error de carga").strip()
    from modules.bancos import queries as _bq
    try:
        res = _bq.deshacer_reverso_cheque_emitido(
            id_mov_doble_reverso=id_mov_doble, motivo=motivo, usuario=usuario,
        )
        flash(
            f"Reverso deshecho: el cheque volvió a estar activo "
            f"(${res['importe']:,.2f}). Se eliminó la NC #{res['id_nc_borrada']} "
            f"del banco y se restauró el movimiento original.",
            "ok",
        )
    except Exception as e:
        flash_exc("No pude deshacer el reverso (rollback total)", e)
    return redirect(_next_seguro())


# =====================================================================
# Reverso ATÓMICO de batch — TMT 2026-05-15.
#
# Cuando una operación de UI generó >1 mov_doble (multi-cheque aplicado a
# varias facturas, transferencia que cruzó múltiples destinos, etc.), todos
# comparten un `batch_id` UUID. Este endpoint los reversa JUNTOS dentro de
# una sola transacción: si cualquiera falla, rollback total.
#
# Por ahora soporta tipos:
#   - cheque_creado
#   - cheque_aplicado_a_factura
#   - cheque_anticipo_espejo
# (que son los que genera /cheques/nuevo en modo multi-cheque). Para otros
# tipos en un batch, el endpoint aborta con un mensaje claro.
# =====================================================================

# Tipos que sabemos reversar atómicamente desde batch (sin pasar por wizard).
_TIPOS_BATCH_REVERSABLES = {
    "cheque_creado",
    "cheque_aplicado_a_factura",
    "cheque_anticipo_espejo",
    # Activaciones viejas quedaron con batch_id (1 fila) — el botón que ve la
    # dueña es el de batch. TMT 2026-07-08. Las nuevas ya no llevan batch_id.
    "activacion_maquinaria",
}


# TMT 2026-07-01 (dueña, review accesos Alex): el reverso de batch tampoco es
# "Informes" — reversa cobranzas multi-cheque (operaciones de cheques). Antes
# pedía informes.ver y frenaba a Alex. Ahora gatea por el permiso de CADA
# operación del batch (cheques.aplicar / cheques.anular), que Alex sí tiene.
_PERMISO_REVERSO_BATCH = {
    "cheque_aplicado_a_factura": "cheques.aplicar",
    "cheque_creado": "cheques.anular",
    "cheque_anticipo_espejo": "cheques.anular",
    "activacion_maquinaria": "activos.crear",
}


@historial_bp.route("/historial/batch/<batch_id>/reverso", methods=["GET", "POST"])
@requiere_login
def reversar_batch(batch_id: str):
    """Reverso atómico de TODAS las filas de un batch_id.

    GET: muestra confirmación con resumen del batch + textarea de motivo.
    POST: ejecuta los reversos dentro de una sola transacción.
    """
    import mov_doble as _md

    # 1. Leer las filas del batch. Sin tx — solo lectura.
    rows = _md.buscar_por_batch(batch_id=batch_id, incluir_reversos=False)
    if not rows:
        flash("Este batch no existe o ya está totalmente reversado.", "warn")
        return redirect(url_for("historial.lista"))

    # 2. Validar que todos los tipos sean reversables atómicamente.
    tipos_en_batch = {r.get("tipo") for r in rows}
    no_soportados = tipos_en_batch - _TIPOS_BATCH_REVERSABLES
    if no_soportados:
        flash(
            "Reverso de batch no disponible: contiene tipos sin handler atómico "
            f"({', '.join(sorted(no_soportados))}). Reversá las filas una por una "
            "desde sus wizards correspondientes.",
            "warn",
        )
        return redirect(url_for("historial.lista"))

    # Gate por la OPERACIÓN: el usuario tiene que tener el permiso de cada tipo
    # del batch (no informes.ver). Mismo criterio "404 si no tenés permiso".
    _perms_necesarios = {
        _PERMISO_REVERSO_BATCH.get(t) for t in tipos_en_batch
    } - {None}
    if any(not tiene_permiso(pm) for pm in _perms_necesarios):
        return render_template("404.html"), 404

    if request.method == "GET":
        # TMT 2026-07-08 (dueña: "mostrame qué anticipos borra y qué cuotas
        # borra"). Para las filas de activación traemos el DETALLE real de los
        # anticipos que vuelven a vivos y las cuotas (posdatados) que se
        # eliminan, leyendo los ids del metadata. Best-effort: si falla, la
        # pantalla sigue mostrando el resumen.
        import json as _json
        for r in rows:
            if r.get("tipo") != "activacion_maquinaria":
                continue
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except Exception:  # noqa: BLE001
                    meta = {}
            ids_ant = [int(x) for x in (meta.get("ids_anticipos") or []) if x]
            ids_pos = [int(x) for x in (meta.get("ids_posdat") or []) if x]
            r["_anticipos"] = []
            r["_cuotas"] = []
            try:
                if ids_ant:
                    ph = ", ".join(["%s"] * len(ids_ant))
                    r["_anticipos"] = db.fetch_all(
                        f"SELECT id_dolares, cta, concepto, importe, "
                        f"COALESCE(st,'') AS st FROM scintela.dolares "
                        f"WHERE id_dolares IN ({ph}) ORDER BY id_dolares",
                        tuple(ids_ant),
                    ) or []
                if ids_pos:
                    ph = ", ".join(["%s"] * len(ids_pos))
                    r["_cuotas"] = db.fetch_all(
                        f"SELECT id_posdat, num, fechad, importe, concepto, "
                        f"COALESCE(banc,0) AS banc FROM scintela.posdat "
                        f"WHERE id_posdat IN ({ph}) ORDER BY fechad, id_posdat",
                        tuple(ids_pos),
                    ) or []
            except Exception:  # noqa: BLE001
                pass
        return render_template(
            "historial/batch_reverso_confirmar.html",
            batch_id=batch_id,
            rows=rows,
            total=sum(float(r.get("importe") or 0) for r in rows),
        )

    # POST → ejecutar reverso atómico.
    motivo = (request.form.get("motivo") or "").strip()
    # TMT 2026-05-21 dueña: motivo opcional sin minlen — antes pedía 10+
    # caracteres obligatorios.

    usuario = (g.user or {}).get("username", "web")

    # Import acá para evitar import circular en módulo.
    from modules.cheques import queries as _ch_q

    try:
        with db.tx() as conn:
            # Orden INVERSO de creación — primero deshacer las aplicaciones,
            # después anular el cheque (si anulamos el cheque antes, las
            # aplicaciones ya no estarían "vivas" para desaplicar).
            rows_sorted = sorted(
                rows,
                key=lambda r: int(r["id_mov_doble"]),
                reverse=True,
            )

            for r in rows_sorted:
                tipo = r.get("tipo")
                if tipo == "cheque_aplicado_a_factura":
                    _ch_q.desaplicar_factura(
                        id_cheque=int(r["origen_id"]),
                        id_factura=int(r["destino_id"]),
                        usuario=usuario,
                        motivo=f"{motivo} (batch {batch_id[:8]})",
                        conn=conn,
                    )
                elif tipo == "cheque_creado":
                    # Las aplicaciones ya se desaplicaron arriba — acá podemos
                    # anular el cheque entero limpio. anular_por_error_de_carga
                    # también borra la posdat hermana si era postdatado.
                    _ch_q.anular_por_error_de_carga(
                        int(r["origen_id"]),
                        motivo=f"{motivo} (batch {batch_id[:8]})",
                        usuario=usuario,
                        conn=conn,
                    )
                elif tipo == "cheque_anticipo_espejo":
                    # El espejo se anula con el cheque padre (anular_por_error_de_carga
                    # ya cascadea). Saltamos acá.
                    continue
                elif tipo == "activacion_maquinaria":
                    from modules.activos import queries as _aq
                    _aq.reversar_activacion(
                        int(r["id_mov_doble"]),
                        motivo=f"{motivo} (batch {batch_id[:8]})",
                        usuario=usuario,
                        conn=conn,
                    )

            flash(
                f"Batch reversado: {len(rows)} movimientos anulados juntos.",
                "ok",
            )
    except Exception as e:
        flash_exc("No pude reversar el batch (rollback total)", e)

    return redirect(url_for("historial.lista"))
