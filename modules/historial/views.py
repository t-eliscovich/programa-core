"""Historial unificado de movimientos dobles."""

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
from auth import requiere_login, tiene_permiso
from error_messages import flash_exc
from exports import csv_response

from . import queries

historial_bp = Blueprint("historial", __name__, template_folder="templates")


@historial_bp.route("/operaciones")
@requiere_login
def operaciones():
    """Landing con cards para todas las operaciones (movimientos dobles).

    Cada card lleva al wizard correspondiente. Agrupa por categoría:
    Movimientos entre cuentas / Cheques / Compras y proveedores / Capital /
    Caja / Auditoría.

    Las cards muestran solo si el usuario tiene el permiso necesario.
    """
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
    for r in filas:
        ot, oid = r.get("origen_table"), r.get("origen_id")
        dt, did = r.get("destino_table"), r.get("destino_id")
        if ot == "cheque" and oid:
            id_cheques.add(int(oid))
        if dt == "cheque" and did:
            id_cheques.add(int(did))
        if ot == "factura" and oid:
            id_facturas.add(int(oid))
        if dt == "factura" and did:
            id_facturas.add(int(did))
        if ot == "transacciones_bancarias" and oid:
            id_txbanco.add(int(oid))
        if dt == "transacciones_bancarias" and did:
            id_txbanco.add(int(did))
    cheque_labels: dict[int, str] = {}
    if id_cheques:
        placeholder = ",".join(["%s"] * len(id_cheques))
        rows_ch = (
            db.fetch_all(
                f"SELECT id_cheque, COALESCE(no_cheque::text, '') AS no_cheque "
                f"FROM scintela.cheque WHERE id_cheque IN ({placeholder})",
                tuple(id_cheques),
            )
            or []
        )
        for rc in rows_ch:
            no = (rc.get("no_cheque") or "").strip()
            cheque_labels[int(rc["id_cheque"])] = no or f"#{rc['id_cheque']}"
    factura_labels: dict[int, str] = {}
    if id_facturas:
        # TMT 2026-05-15: cast a text — `numf` puede ser INTEGER en data
        # legacy, y COALESCE(integer, '') crashea con InvalidTextRepresentation.
        placeholder = ",".join(["%s"] * len(id_facturas))
        rows_f = (
            db.fetch_all(
                f"SELECT id_factura, COALESCE(numf::text, '') AS numf "
                f"FROM scintela.factura WHERE id_factura IN ({placeholder})",
                tuple(id_facturas),
            )
            or []
        )
        for rf in rows_f:
            num = (rf.get("numf") or "").strip()
            factura_labels[int(rf["id_factura"])] = num or f"#{rf['id_factura']}"
    banco_labels: dict[int, str] = {}
    if id_txbanco:
        placeholder = ",".join(["%s"] * len(id_txbanco))
        rows_b = (
            db.fetch_all(
                f"SELECT t.id_transaccion, t.documento, "
                f"       COALESCE(b.nombre, '') AS nombre "
                f"  FROM scintela.transacciones_bancarias t "
                f"  LEFT JOIN scintela.banco b ON b.no_banco = t.no_banco "
                f" WHERE t.id_transaccion IN ({placeholder})",
                tuple(id_txbanco),
            )
            or []
        )
        for rb in rows_b:
            nm = (rb.get("nombre") or "").strip().title()  # PICHINCHA → Pichincha
            (rb.get("documento") or "").upper().strip()
            # Etiqueta corta — la dueña pidió "Pichincha", no "Banco mov #X".
            # Si el banco no tiene nombre, fallback al id.
            if nm:
                banco_labels[int(rb["id_transaccion"])] = nm
            else:
                banco_labels[int(rb["id_transaccion"])] = f"Banco #{rb['id_transaccion']}"

    def _override_label(t: str | None, rid, default_label: str) -> str:
        if t == "cheque" and rid and int(rid) in cheque_labels:
            return f"Cheque {cheque_labels[int(rid)]}"
        if t == "factura" and rid and int(rid) in factura_labels:
            return f"Factura {factura_labels[int(rid)]}"
        if t == "transacciones_bancarias" and rid and int(rid) in banco_labels:
            return banco_labels[int(rid)]
        return default_label

    # Mapeo id_factura → numf (solo si tiene numf válido) y id_cheque → no_cheque.
    # Estos diccionarios se pasan a link_origen/link_destino para que las URLs
    # usen el número real (visible al usuario) en lugar del id interno.
    factura_numfs = {
        k: v.lstrip("#")
        for k, v in factura_labels.items()
        if v and not v.startswith("#")  # solo cuando es numf real, no fallback "#id"
    }
    cheque_nos = {
        k: v
        for k, v in cheque_labels.items()
        if v and not v.startswith("#")
    }
    for r in filas:
        r["label"] = queries.label(r.get("tipo") or "")
        # TMT 2026-07-11 (dueña): no ofrecer "reversar" sobre un reverso (mov
        # terminal) — mostraba un modal que prometía facturas y no había.
        r["es_terminal"] = _es_terminal(r.get("tipo") or "")
        u, t = queries.link_origen(r, factura_numfs=factura_numfs, cheque_nos=cheque_nos)
        r["origen_url"] = u
        r["origen_label"] = _override_label(r.get("origen_table"), r.get("origen_id"), t)
        u, t = queries.link_destino(r, factura_numfs=factura_numfs, cheque_nos=cheque_nos)
        r["destino_url"] = u
        r["destino_label"] = _override_label(r.get("destino_table"), r.get("destino_id"), t)
        # row_url = a dónde va el click de la fila entera. Preferimos
        # origen_url; si no hay, destino_url.
        r["row_url"] = r["origen_url"] or r["destino_url"]
        # TMT 2026-07-09 (dueña): si el movimiento consolidó >1 item
        # (p.ej. "6 anticipo(s) → compra"), traer cada uno para poder
        # desplegarlos uno por uno en el historial.
        r["detalle"] = queries.detalle_consolidado(r.get("metadata"))
        # TMT 2026-07-14 (dueña "bastaba con que diga origen OP destino retiro"):
        # el mov_doble del retiro OP es self-ref sobre `retiros`, así que ambas
        # columnas salían "Retiro #N". Forzamos el ORIGEN a "OP" → se lee como
        # doble asiento OP → Retiro, sin bloque de explicación extra.
        if (r.get("tipo") or "") == "retiro_op":
            r["origen_label"] = "OP"

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
    def _primary_rows(siblings: list[dict]) -> tuple[list[dict], str]:
        creados = [x for x in siblings if x.get("tipo") == "cheque_creado"]
        if creados:
            return creados, "cheque" if len(creados) == 1 else "cheques"
        aplics = [x for x in siblings if x.get("tipo") == "cheque_aplicado_a_factura"]
        if aplics:
            return aplics, ("aplicación" if len(aplics) == 1 else "aplicaciones")
        return siblings, ("movimiento" if len(siblings) == 1 else "movimientos")

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
        items.append(
            {
                "type": "batch",
                "batch_id": bid_str,
                # `count`/`total` = plata real (sin double-counting).
                "count": len(primary),
                "total": sum(float(x.get("importe") or 0) for x in primary),
                "label_primary": label_primary,
                # `count_full` = N de mov_doble en el batch (para nota chiquita).
                "count_full": len(siblings),
                "fecha": siblings[0].get("fecha_operacion"),
                "estado_global": estado_global,
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
}

# Tipos que NO se reversan desde acá — el dispatcher muestra un toast
# claro al usuario con la ruta correcta. Audit 2026-05-13: estos handlers
# no deshacen completamente la operación (dejan saldos inconsistentes,
# compras fantasma, etc.). Los manejamos manualmente hasta que cada uno
# tenga un reverso atómico.
_REVERSO_BLOQUEADO = {}


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
    if r["estado"] in ("reverso", "reversado"):
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
    if r["estado"] in ("reverso", "reversado"):
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
