"""/importaciones — importaciones de Asinfo cruzadas con compras del programa.

Modelo v2 (TMT 2026-07-06 dueña): sin flujo "Pagar" ni predicción de costo.
Los ANTICIPOS (≈90% del valor) se cargan acá como movimientos (ND automática
en Pichincha); el RESTANTE se carga por /compras como compra normal al
proveedor. Valor del stock de cada importación = Σ anticipos.
"""
from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from filters import today_ec
from parsers import parse_date, parse_int, parse_monto

importaciones_bp = Blueprint(
    "importaciones",
    __name__,
    template_folder="templates",
)


@importaciones_bp.route("/importaciones")
@requiere_login
@requiere_permiso("stock.ver")
def lista():
    from modules.importaciones import service

    q = (request.args.get("q") or "").strip().upper()
    estado = (request.args.get("estado") or "").strip()  # "" | "match" | "sin_match" | "sin_codigo"
    recep = (request.args.get("recep") or "").strip()    # "" | "recibida" | "pendiente"
    # TMT 2026-07-09 (dueña): filtrar por MES/AÑO de la fecha recibida. El
    # input type=month da "YYYY-MM"; fecha_recepcion es "YYYY-MM-DD" → prefix.
    mes = (request.args.get("mes") or "").strip()        # "" | "YYYY-MM"

    error = None
    rows = []
    try:
        rows = service.importaciones_con_cruce()
        for r in rows:
            r["origen"] = "importacion"
    except Exception as e:  # noqa: BLE001
        error = str(e)

    # TMT 2026-07-30 (dueña): "la fábrica importa hilo pero también hace compras
    # locales; hay que sumarlas acá y la columna se llama IMPORTACIÓN/COMPRA".
    # Las locales son facturas de proveedor de Asinfo SIN importación con
    # recepción a la bodega 51. Fail-soft aparte: si ese bridge cae, las
    # importaciones se siguen viendo.
    from modules.compras_locales import service as _loc

    try:
        rows = rows + _loc.compras_locales_con_cruce()
    except Exception as e:  # noqa: BLE001
        error = error or str(e)

    # Estado del pasivo de las importaciones (las locales ya vienen con el
    # suyo): misma regla que la columna "Pagada" de /compras.
    for r in rows:
        if r.get("origen") != "importacion":
            continue
        est = (
            _loc.estado_pago_de_compras((r.get("compra") or {}).get("items") or [])
            if r.get("compra") else None
        )
        r["pagada"] = bool(est and est["pagada"])
        r["saldo"] = float(est["saldo"]) if est else 0.0
        r["parcial"] = bool(est and est["parcial"])

    # Más nuevas arriba, mezclando las dos clases por fecha.
    rows.sort(
        key=lambda r: (str(r.get("fecha") or ""), str(r.get("im_numero") or "")),
        reverse=True,
    )

    if q:
        rows = [
            r for r in rows
            if q in (r.get("proveedor") or "").upper()
            or q in (r.get("nota") or "").upper()
            or q in (r.get("codigo") or "").upper()
            or q in (r.get("im_numero") or "").upper()
        ]
    if estado == "match":
        rows = [r for r in rows if r.get("fuente")]
    elif estado == "sin_match":
        rows = [r for r in rows if r.get("codigo") and not r.get("fuente")]
    elif estado == "sin_codigo":
        rows = [r for r in rows if not r.get("codigo")]
    if recep == "recibida":
        rows = [r for r in rows if r.get("recibida")]
    elif recep == "pendiente":
        rows = [r for r in rows if not r.get("recibida")]
    if mes:
        # Filtra por mes/año de la fecha de recepción (solo recibidas la
        # tienen; las en tránsito quedan fuera). Prefix "YYYY-MM-".
        _pref = mes + "-"
        rows = [
            r for r in rows
            if (r.get("fecha_recepcion") or "").startswith(_pref)
        ]

    total = len(rows)
    con_codigo = sum(1 for r in rows if r.get("codigo"))
    con_match = sum(1 for r in rows if r.get("fuente"))
    sin_codigo = total - con_codigo
    recibidas = sum(1 for r in rows if r.get("recibida"))
    pendientes = total - recibidas
    importe_programa = sum(
        r["importe_programa"] for r in rows if r.get("importe_programa")
    )
    # Σ kg del filtro (dueña 2026-07-17: "la suma de los kg en KPIs así
    # cuando filtro sé cuánto es") — suma los kg recibidos de las filas
    # que quedaron después de aplicar los filtros.
    kg_total = round(sum(float(r["kg"]) for r in rows if r.get("kg")), 2)
    # TMT 2026-07-06 v3 (dueña: "ordená anticipos y compras, fijate que
    # sumen bien"): el ANTICIPO TOTAL de una importación = anticipos USD
    # matcheados de /dolares (r.anticipo.importe_total) + movimientos
    # cargados acá (r.anticipo_aplicado). Además, si VARIAS importaciones
    # comparten el mismo código (ej. "AC 19" partida en dos IM), el matcheo
    # por (prov, número) les asigna el MISMO anticipo USD a ambas → se marca
    # compartido y se cuenta UNA sola vez en el KPI (evita doble suma).
    _vistos_codigo: dict = {}
    for r in rows:
        _k = ((r.get("prov") or "").strip().upper(), r.get("numero"))
        if r.get("prov") and r.get("numero") is not None:
            _vistos_codigo[_k] = _vistos_codigo.get(_k, 0) + 1
    anticipos_total = 0.0
    _usd_contados: set = set()
    for r in rows:
        _usd = float((r.get("anticipo") or {}).get("importe_total") or 0)
        # NO volver a sumar los movimientos tipo 'anticipo': al cargarlos por esta
        # pantalla, pago.py TAMBIÉN inserta su fila viva en scintela.dolares (para
        # que cuenten en /dolares), así que YA están dentro de `_usd` (el cruce).
        # Sumarlos de nuevo duplicaba el anticipo (p.ej. 101.771 = 86.771 + 15.000).
        # Solo se agregan los 'pago' (parciales contra stock), que NO van a /dolares.
        _movs = round(
            sum(
                float(m.get("monto") or 0)
                for m in (r.get("movimientos") or [])
                if (m.get("tipo") or "").strip() != "anticipo"
            ),
            2,
        )
        _k = ((r.get("prov") or "").strip().upper(), r.get("numero"))
        r["codigo_compartido"] = bool(
            r.get("numero") is not None and _vistos_codigo.get(_k, 0) > 1
        )
        r["anticipo_usd_dolares"] = _usd
        r["anticipo_total"] = round(_usd + _movs, 2)
        anticipos_total += _movs
        if _usd and _k not in _usd_contados:
            anticipos_total += _usd
            _usd_contados.add(_k)
    anticipos_total = round(anticipos_total, 2)

    if request.args.get("export") == "csv":
        export_rows = [
            {
                "im_numero": r["im_numero"],
                "clase": "Compra local" if r.get("origen") == "compra" else "Importación",
                "fecha": r.get("fecha") or "",
                "fecha_recepcion": r.get("fecha_recepcion") or "",
                "recepcion": "Recibida" if r.get("recibida") else "Pendiente",
                "bod": r.get("bod") or "",
                "proveedor": r.get("proveedor") or "",
                "codigo": r.get("codigo") or "",
                "nota": r.get("nota") or "",
                "kg": round(r["kg"], 2) if r.get("kg") is not None else "",
                "total_asinfo": round(r.get("total_asinfo") or 0, 2),
                "fuente": (r.get("fuente") or "").capitalize(),
                "importe_programa": (
                    round(r["importe_programa"], 2) if r.get("importe_programa") else ""
                ),
                "anticipos": (
                    round(float(r.get("anticipo_aplicado") or 0), 2)
                    if r.get("anticipo_aplicado") else ""
                ),
            }
            for r in rows
        ]
        return csv_response(
            export_rows,
            columnas=[
                ("im_numero", "Importación/Compra"),
                ("clase", "Clase"),
                ("fecha", "Fecha"),
                ("fecha_recepcion", "Fecha Recepción"),
                ("recepcion", "Recepción"),
                ("bod", "Doc. Recepción"),
                ("proveedor", "Proveedor"),
                ("codigo", "Código programa"),
                ("nota", "Nota Asinfo"),
                ("kg", "Kg"),
                ("total_asinfo", "Total Asinfo (ref)"),
                ("fuente", "Fuente programa"),
                ("importe_programa", "Importe programa (US)"),
                ("anticipos", "Anticipos (US) = valor stock"),
            ],
            filename="importaciones_cruce.csv",
        )

    # Tarifario de las compras locales + qué está esperando para cargarse. Vive
    # en esta misma pantalla, plegado detrás de un "+" (dueña 2026-07-30: "las
    # tarifas en la misma pantalla de ingreso de hilo que tenga un + para que
    # aparezcan escondidas salvo que queramos editar, como en tejeduría").
    from modules.compras_locales import queries as _loc_q

    try:
        tarifas = _loc_q.listar_tarifas()
    except Exception:  # noqa: BLE001 -- fail-soft: la tabla puede no existir
        tarifas = []
    from modules.compras_locales import service as _loc_svc

    # "Recibidas sin cargar" = SOLO lo que el motor realmente va a intentar, o
    # sea después de la fecha de corte. Sin este filtro la lista traía 110
    # facturas de 2024/2025 que el motor nunca va a tocar, y el contador
    # "falta N" gritaba por tarifas que no hacen falta.
    pendientes_local = [
        r for r in rows
        if r.get("origen") == "compra"
        and r.get("recibida") and not r.get("compra")
        and float(r.get("kg") or 0) > 0
        and not _loc_svc._antes_del_corte(r.get("fecha_recepcion"))
    ]
    locales_sin_tarifa = sum(1 for r in pendientes_local if not r.get("tarifa"))

    return render_template(
        "importaciones/lista.html",
        rows=rows,
        tarifas=tarifas,
        pendientes_local=pendientes_local,
        locales_sin_tarifa=locales_sin_tarifa,
        total=total,
        con_codigo=con_codigo,
        con_match=con_match,
        sin_codigo=sin_codigo,
        recibidas=recibidas,
        pendientes=pendientes,
        importe_programa=importe_programa,
        anticipos_total=anticipos_total,
        kg_total=kg_total,
        q=q,
        estado=estado,
        recep=recep,
        mes=mes,
        hoy=today_ec().isoformat(),
        error=error,
    )


# ---------------------------------------------------------------------------
# Compras LOCALES de hilo — tarifario y carga (TMT 2026-07-30, dueña)
# ---------------------------------------------------------------------------
@importaciones_bp.route("/importaciones/tarifas", methods=["POST"])
@requiere_login
@requiere_permiso("tarifas.editar")
def guardar_tarifas_locales():
    """Guarda la matriz de tarifas $/kg de las compras locales.

    Mismo patrón que /produccion-tejeduria-asinfo/tarifas: los inputs vienen
    como t_<i>_<campo> y sólo se escriben las filas con proveedor y tarifa. La
    fila en blanco del final sirve para agregar un producto nuevo.
    """
    from modules.compras_locales import queries as _loc_q

    usuario = (g.user or {}).get("username", "web")
    filas: list[dict] = []
    idxs = sorted({
        k.split("_")[1] for k in request.form
        if k.startswith("t_") and len(k.split("_")) >= 3
    })
    for i in idxs:
        raw = request.form.get(f"t_{i}_tarifa")
        cod = (request.form.get(f"t_{i}_cod_prov") or "").strip()
        if not cod:
            continue
        tarifa = parse_monto(raw)
        if (raw or "").strip() and tarifa is None:
            flash(f"No entendí la tarifa «{raw}» de {cod.upper()}. Usá formato 2,9500.",
                  "error")
            return redirect(url_for("importaciones.lista"))
        if tarifa is None:
            continue
        filas.append({
            "cod_prov": cod,
            "patron": request.form.get(f"t_{i}_patron"),
            "tarifa": tarifa,
            "nota": request.form.get(f"t_{i}_nota"),
        })
    try:
        res = _loc_q.guardar_tarifas(filas, usuario=usuario)
        flash(f"Tarifas guardadas ({res['guardadas']}).", "ok")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:  # noqa: BLE001
        flash_exc(e)
    return redirect(url_for("importaciones.lista"))


@importaciones_bp.route("/importaciones/tarifas/<int:id_tarifa>/borrar",
                        methods=["POST"])
@requiere_login
@requiere_permiso("tarifas.editar")
def borrar_tarifa_local(id_tarifa: int):
    from modules.compras_locales import queries as _loc_q

    try:
        _loc_q.borrar_tarifa(id_tarifa)
        flash("Tarifa borrada.", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc(e)
    return redirect(url_for("importaciones.lista"))


@importaciones_bp.route("/importaciones/cargar-locales", methods=["POST"])
@requiere_login
@requiere_permiso("compras.crear")
def cargar_locales():
    """Carga ahora las compras locales recibidas que faltan.

    Es un ATAJO: el motor ya corre solo cada 30 minutos colgado del ciclo de
    autocarga. El botón sirve para no esperar. Las guardas son las mismas (la
    función es la misma), así que apretarlo dos veces no duplica nada.
    """
    from modules.compras_locales import service as _loc

    usuario = (g.user or {}).get("username", "web")
    clave = (g.user or {}).get("clave") or usuario[:3].upper()
    try:
        res = _loc.cargar_pendientes(usuario=usuario, clave=clave)
    except Exception as e:  # noqa: BLE001
        flash_exc(e)
        return redirect(url_for("importaciones.lista"))
    if res["creadas"]:
        flash(
            f"Cargué {res['creadas']} compra(s) local(es) de hilo por "
            f"$ {res['importe']:,.2f}.",
            "ok",
        )
    else:
        motivos = {d["motivo"] for d in res["detalle"] if not d.get("ok")}
        detalle = (" — " + "; ".join(sorted(motivos)[:3])) if motivos else ""
        flash(f"No había nada para cargar.{detalle}", "warn")
    return redirect(url_for("importaciones.lista"))


def _volver():
    """Vuelve a /importaciones preservando los filtros actuales."""
    args = {
        k: request.form.get(k)
        for k in ("q", "estado", "recep", "mes")
        if request.form.get(k)
    }
    return redirect(url_for("importaciones.lista", **args))


def _prov_num():
    prov = (request.form.get("prov") or "").strip().upper()
    numero = parse_int(request.form.get("numero"))
    return prov, numero


def _im():
    return (request.form.get("im_numero") or "").strip()


@importaciones_bp.route("/importaciones/recibir", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def recibir():
    """Recibe la importación: los kg entran al stock.

    Modelo v2 (TMT 2026-07-06): recibir NO genera deuda ni pide costo — el
    valor del stock de la importación es Σ anticipos y el restante se carga
    por /compras.
    """
    from modules.importaciones import pago as _pago

    prov, numero = _prov_num()
    im = _im()
    kg = parse_monto(request.form.get("kg"))
    if not im:
        flash("Importación inválida (falta el número IM-).", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        _pago.set_recepcion(im, prov, numero, kg=kg, usuario=usuario)
        flash(f"Importación {im} recibida: {kg or 0:,.0f} kg al stock.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude registrar la recepción", e)
    return _volver()


@importaciones_bp.route("/importaciones/deshacer-recepcion", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def deshacer_recepcion():
    """Revierte la recepción (vuelve a 'en tránsito', saca los kg del stock).
    Los anticipos (movimientos + ND) no se tocan — se deshacen con su ✕."""
    from modules.importaciones import pago as _pago

    prov, numero = _prov_num()
    im = _im()
    if not im:
        flash("Importación inválida (falta el número IM-).", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        _pago.deshacer_recepcion(im, prov, numero, usuario=usuario)
        flash(f"Recepción de {im} deshecha (vuelve a en tránsito).", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude deshacer la recepción", e)
    return _volver()


@importaciones_bp.route("/importaciones/movimiento", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def movimiento_agregar():
    """Registra un ANTICIPO como MOVIMIENTO (mig 0113).

    TMT 2026-07-06 (dueña): muchos anticipos por importación, nada se pisa;
    Σ anticipos = valor del stock. Cada anticipo genera AUTOMÁTICAMENTE su ND
    en Pichincha (la pantalla avisa para que no la carguen a mano otra vez).
    La UI solo carga anticipos — el restante va por /compras.
    """
    from modules.importaciones import pago as _pago

    prov, numero = _prov_num()
    im = _im()
    monto = parse_monto(request.form.get("monto_mov"))
    fecha = parse_date(request.form.get("fecha_mov"))
    nota = (request.form.get("nota_mov") or "").strip()
    if not im:
        flash("Importación inválida (falta el número IM-).", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = _pago.agregar_movimiento(
            im, "anticipo", monto, fecha=fecha, nota=nota, prov=prov,
            numero=numero, usuario=usuario,
        )
        msg = f"Anticipo de $ {float(monto or 0):,.2f} registrado en {im}."
        if r.get("id_transaccion"):
            msg += (
                f" Se generó SOLA la ND #{r['id_transaccion']} en Pichincha — "
                "no la cargues a mano en el banco."
            )
        if r.get("anticipo_aplicado") is not None:
            msg += f" Σ anticipos (valor stock): $ {float(r['anticipo_aplicado']):,.2f}."
        flash(msg, "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude registrar el anticipo", e)
    return _volver()


@importaciones_bp.route("/importaciones/movimiento/deshacer", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def movimiento_deshacer():
    """✕ de un movimiento: borra el anticipo Y compensa su ND con una NC
    en Pichincha (par atómico, mov_doble de auditoría)."""
    from modules.importaciones import pago as _pago

    id_mov = parse_int(request.form.get("id_mov"))
    if not id_mov:
        flash("Movimiento inválido.", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = _pago.deshacer_movimiento(id_mov, usuario=usuario)
        msg = f"Anticipo de $ {float(r.get('monto') or 0):,.2f} borrado de {r.get('im_numero')}."
        if r.get("id_transaccion_reverso"):
            msg += (
                f" Su ND quedó compensada con la NC #{r['id_transaccion_reverso']} "
                "en Pichincha."
            )
        else:
            msg += " (Sin ND automática linkeada — si hiciste la ND a mano, resolvela en el banco.)"
        if r.get("anticipo_aplicado") is not None:
            msg += f" Σ anticipos ahora: $ {float(r['anticipo_aplicado']):,.2f}."
        flash(msg, "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude deshacer el movimiento", e)
    return _volver()


# ---------------------------------------------------------------------------
# AUTOMÁTICO — conversión anticipo → compra BAP al recibir la importación.
# TMT 2026-07-29 (dueña): "ponelo en una campanita y hagámoslo automático".
# Ver modules/importaciones/autobap.py para los guards.
# ---------------------------------------------------------------------------

@importaciones_bp.route("/importaciones/automatico")
@requiere_login
@requiere_permiso("compras.crear")
def automatico():
    """Switch + preview de lo que convertiría + historial de lo convertido."""
    from . import autobap

    cfg = autobap.config()
    try:
        prev = autobap.pendientes(cfg)
    except Exception as e:  # noqa: BLE001
        prev = {"grupos": [], "total_usd": 0, "n_importaciones": 0,
                "frenos": [f"No pude calcular el preview: {e}"],
                "disponible": False}
    return render_template(
        "importaciones/automatico.html",
        cfg=cfg, prev=prev,
        historial=autobap.avisos(solo_no_leidos=False, limite=50),
    )


@importaciones_bp.route("/importaciones/automatico/switch", methods=["POST"])
@requiere_login
@requiere_permiso("compras.crear")
def automatico_switch():
    from . import autobap

    usuario = (getattr(g, "user", None) or {}).get("username", "web")
    activar = (request.form.get("activo") or "") == "1"
    try:
        cfg = autobap.set_activo(activar, usuario=usuario)
        if activar:
            flash(
                "Conversión automática PRENDIDA. Sólo mira recepciones "
                f"desde el {cfg.get('fecha_corte') or 'hoy'} (ese día "
                "incluido) — lo anterior queda como está.", "ok",
            )
        else:
            flash("Conversión automática APAGADA.", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude cambiar el switch", e)
    return redirect(url_for("importaciones.automatico"))


@importaciones_bp.route("/importaciones/automatico/topes", methods=["POST"])
@requiere_login
@requiere_permiso("compras.crear")
def automatico_topes():
    """Edita los topes por corrida desde la pantalla, sin deploy."""
    from . import autobap

    usuario = (getattr(g, "user", None) or {}).get("username", "web")
    try:
        cfg = autobap.set_topes(
            tope_importaciones=parse_int(request.form.get("tope_importaciones")),
            tope_usd=parse_monto(request.form.get("tope_usd")),
            usuario=usuario,
        )
        flash(
            f"Topes actualizados: {cfg['tope_importaciones']} importaciones y "
            f"US$ {cfg['tope_usd']:,.2f} por corrida.", "ok",
        )
    except (TypeError, ValueError) as e:
        flash(f"Topes inválidos: {e}", "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude guardar los topes", e)
    return redirect(url_for("importaciones.automatico"))


@importaciones_bp.route("/importaciones/automatico/correr", methods=["POST"])
@requiere_login
@requiere_permiso("compras.crear")
def automatico_correr():
    """«Convertir ahora» — el mismo motor, disparado a mano."""
    from . import autobap

    usuario = (getattr(g, "user", None) or {}).get("username", "web")
    try:
        r = autobap.correr(
            usuario=autobap.USUARIO_AUTOBAP,
            forzar_topes=(request.form.get("forzar") == "1"),
        )
        if r["convertidas"]:
            flash(
                f"Convertidas {r['convertidas']} importación(es) por "
                f"$ {r['importe']:,.2f} — quedaron en la campanita "
                f"(las hizo {usuario}).", "ok",
            )
        else:
            flash("No había nada para convertir.", "warn")
        for f in r.get("frenos", []):
            flash(f, "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude correr la conversión", e)
    return redirect(url_for("importaciones.automatico"))


@importaciones_bp.route("/importaciones/automatico/leidos", methods=["POST"])
@requiere_login
@requiere_permiso("compras.crear")
def avisos_leidos():
    """Marca todo como leído.

    TMT 2026-07-30 (dueña): "una vez que lo abro se marque leído solo" — la
    campanita lo llama por fetch al abrirse (`ajax=1`) y no espera nada, así
    que ahí contestamos 204 en vez de un redirect que el browser descartaría.
    """
    from . import autobap

    autobap.marcar_leidos()
    if request.form.get("ajax") == "1":
        return ("", 204)
    return redirect(request.referrer or url_for("importaciones.automatico"))


@importaciones_bp.route("/importaciones/_api/abiertas/<prov>")
@requiere_login
@requiere_permiso("compras.ver")
def api_importaciones_abiertas(prov):
    """Importaciones del proveedor SIN compra matcheada todavía.

    TMT 2026-07-06 (dueña): "cuando el proveedor es uno de importaciones,
    si cargo anticipos o compras debería mostrar cuál importación va a
    hacer match". Alimenta el picker de /compras/nueva: elegir una llena
    el CONCEPTO con el número de la Nota → el cruce (codigo_prov +
    concepto-numérico, ver service._buscar_compras) matchea seguro.
    """
    from modules.importaciones import service

    prov = (prov or "").strip().upper()
    if not prov:
        return {"importaciones": []}, 400
    try:
        rows = service.importaciones_con_cruce(limite=400)
    except Exception:  # noqa: BLE001
        return {"importaciones": []}
    out = []
    for r in rows:
        if (r.get("prov") or "").strip().upper() != prov:
            continue
        # TMT 2026-07-06 v2 (dueña): "sí podés matchear varias veces en
        # compras y anticipos" — se listan TODAS las importaciones del
        # proveedor; lo ya matcheado va como referencia, no como filtro.
        _comp = r.get("compra") or {}
        out.append({
            "im_numero": r.get("im_numero"),
            "codigo": r.get("codigo"),
            "numero": r.get("numero"),
            "nota": r.get("nota"),
            "fecha": str(r.get("fecha") or ""),
            # TMT 2026-07-29: año de la importación (el de la CAMPAÑA de la
            # nota, o el de la fecha si no la trae). El picker de /dolares lo
            # escribe en el concepto del anticipo como `58/26`, para que el
            # match no se pegue al mismo número de otra campaña.
            "anio": service._anio_de(r).get("anio"),
            "kg": r.get("kg"),
            "anticipos": float(r.get("anticipo_aplicado") or 0)
                         if r.get("anticipo_aplicado") is not None else
                         float((r.get("anticipo") or {}).get("importe_total") or 0),
            "compras_n": int(_comp.get("n") or 0),
            "compras_usd": float(_comp.get("importe_total") or 0),
        })
    # TMT 2026-07-23 (dueña): "mostrar en orden numérico, está difícil de
    # encontrar". Se conservan las 30 más recientes (relevancia) pero se
    # ordenan por número de AC ascendente para poder escanear la lista.
    out = out[:30]
    out.sort(key=lambda x: (x.get("numero") is None, x.get("numero") or 0))
    return {"prov": prov, "importaciones": out}
