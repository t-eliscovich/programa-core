"""Posdat — vista/CRUD de pasivos abiertos con proveedores."""

from datetime import datetime

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import db
from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from filters import today_ec
from parsers import parse_date, parse_int, parse_monto

from . import queries

posdat_bp = Blueprint("posdat", __name__, template_folder="templates")


@posdat_bp.route("/posdat/nueva", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("posdat.crear")
def nueva():
    errores: list[str] = []
    form: dict = {}
    if request.method == "GET":
        form["fecha"] = datetime.now().date().isoformat()
        form["tipo"] = "CM"
        form["num_sugerido"] = queries.proximo_num()
        # Restaurar campos via query string — si veníamos de crear un
        # proveedor nuevo, /proveedores/nuevo nos redirige acá con los
        # datos del form anterior. TMT 2026-05-13.
        for k in ("fecha", "fechad", "prov", "importe", "concepto", "tipo", "compr", "no_comp", "num"):
            if request.args.get(k):
                form[k] = request.args.get(k)
        return render_template("posdat/form.html", form=form, errores=errores, modo="crear")

    fecha = parse_date(request.form.get("fecha"))
    fechad = parse_date(request.form.get("fechad")) or fecha
    prov = (request.form.get("prov") or "").strip().upper()
    importe = parse_monto(request.form.get("importe"))
    concepto = (request.form.get("concepto") or "").strip()
    tipo = (request.form.get("tipo") or "").strip().upper()
    compr = (request.form.get("compr") or "").strip()
    no_comp = (request.form.get("no_comp") or "").strip()
    num = parse_int(request.form.get("num"))

    if fecha is None:
        errores.append("Fecha inválida.")
    if not prov:
        errores.append("Proveedor requerido.")
    elif not db.fetch_one(
        "SELECT 1 AS x FROM scintela.proveedor WHERE codigo_prov = %s",
        (prov,),
    ):
        # Proveedor no existe → flujo guiado a /proveedores/nuevo, mismo
        # patrón que compras.nueva. TMT 2026-05-13.
        _permisos = getattr(g, "permisos", set()) or set()
        if "proveedores.crear" in _permisos or "*" in _permisos:
            from urllib.parse import urlencode

            restore_args = {
                "fecha": request.form.get("fecha") or "",
                "fechad": request.form.get("fechad") or "",
                "prov": prov,
                "importe": request.form.get("importe") or "",
                "concepto": concepto or "",
                "tipo": tipo or "",
                "compr": compr or "",
                "no_comp": no_comp or "",
                "num": request.form.get("num") or "",
            }
            restore_args = {k: v for k, v in restore_args.items() if v}
            next_url = url_for("posdat.nueva") + "?" + urlencode(restore_args)
            flash(
                f"El proveedor {prov} no existe — completá los datos para "
                "crearlo y después seguís con el posdatado.",
                "warning",
            )
            return redirect(url_for("proveedores.nuevo", codigo=prov, next=next_url))
        errores.append(f"El proveedor {prov!r} no existe.")
    if importe is None or importe == 0:
        errores.append("Importe no puede ser cero.")
    if not concepto:
        errores.append("Concepto requerido.")

    form = {
        "fecha": request.form.get("fecha"),
        "fechad": request.form.get("fechad"),
        "prov": prov,
        "importe": request.form.get("importe"),
        "concepto": concepto,
        "tipo": tipo,
        "compr": compr,
        "no_comp": no_comp,
        "num": request.form.get("num"),
        "num_sugerido": queries.proximo_num(),
    }

    if errores:
        return render_template("posdat/form.html", form=form, errores=errores, modo="crear"), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.crear(
            fecha=fecha,
            fechad=fechad,
            prov=prov,
            importe=importe,
            concepto=concepto,
            tipo=tipo or None,
            compr=compr or None,
            no_comp=no_comp or None,
            num=num,
            usuario=usuario,
        )
        flash(f"Posdat {r.get('num')} creado.", "ok")
        return redirect(url_for("posdat.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template("posdat/form.html", form=form, errores=errores, modo="crear"), 400
    except Exception as e:
        errores.append(f"No pude crear: {e}")
        return render_template("posdat/form.html", form=form, errores=errores, modo="crear"), 500


@posdat_bp.route("/posdat/<int:id_posdat>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("posdat.editar")
def editar(id_posdat: int):
    pd = queries.por_id(id_posdat)
    if not pd:
        abort(404)
    errores: list[str] = []

    if request.method == "GET":
        # Recientes — best-effort, no rompe el form si falla.
        try:
            from modules.recientes import queries as rec

            etiqueta = (
                f"Posdat #{pd.get('num') or id_posdat} · {pd.get('prov') or ''} · {pd.get('concepto') or ''}"
            )[:200]
            rec.registrar("posdat", id_posdat, etiqueta=etiqueta)
        except Exception:  # noqa: BLE001
            # TMT 2026-05-15 (re-audit M2): logeamos sin re-raise.
            import logging as _lg

            _lg.getLogger(__name__).exception(
                "recientes.registrar(posdat, %s) falló",
                id_posdat,
            )
        form = {
            "id_posdat": pd["id_posdat"],
            "num": pd.get("num") or "",
            "fecha": (pd.get("fecha") and pd["fecha"].isoformat()) or "",
            "fechad": (pd.get("fechad") and pd["fechad"].isoformat()) or "",
            "prov": pd.get("prov") or "",
            "importe": pd.get("importe") or "",
            "concepto": pd.get("concepto") or "",
            "tipo": pd.get("tipo") or "",
            "compr": pd.get("compr") or "",
            "no_comp": pd.get("no_comp") or "",
            "banc": pd.get("banc") or 0,
        }
        return render_template("posdat/form.html", form=form, errores=errores, modo="editar")

    fechad = parse_date(request.form.get("fechad"))
    importe = parse_monto(request.form.get("importe"))
    concepto = (request.form.get("concepto") or "").strip()
    tipo = (request.form.get("tipo") or "").strip().upper()
    compr = (request.form.get("compr") or "").strip()
    no_comp = (request.form.get("no_comp") or "").strip()

    if not concepto:
        errores.append("Concepto requerido.")
    if importe is not None and importe == 0:
        errores.append("Importe no puede ser cero.")
    if errores:
        form = {"id_posdat": id_posdat, **request.form}
        return render_template("posdat/form.html", form=form, errores=errores, modo="editar"), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.editar(
            id_posdat,
            fechad=fechad,
            importe=importe,
            concepto=concepto,
            tipo=tipo or None,
            compr=compr or None,
            no_comp=no_comp or None,
            usuario=usuario,
        )
        flash("Posdat actualizado.", "ok")
        return redirect(url_for("posdat.lista"))
    except Exception as e:
        errores.append(f"No pude actualizar: {e}")
        form = {"id_posdat": id_posdat, **request.form}
        return render_template("posdat/form.html", form=form, errores=errores, modo="editar"), 500


@posdat_bp.route("/posdat/<int:id_posdat>/pagada", methods=["POST"])
@requiere_login
@requiere_permiso("posdat.editar")
def marcar_pagada(id_posdat: int):
    """DEPRECADO (TMT 2026-05-14, #4): redirige al wizard de emitir cheque.

    Originalmente esto seteaba `banc=N` sin generar el movimiento bancario
    ni el mov_doble — el banco no bajaba y el historial no veía nada. La
    forma correcta es `bancos.emitir_cheque(tipo='proveedor', id_posdat=X)`
    que hace todo atómicamente. Redirigimos al wizard preservando el id.
    """
    pd = queries.por_id(id_posdat)
    if not pd:
        abort(404)
    flash(
        "Para pagar una posdat usá el wizard de emitir cheque — el flujo "
        "viejo dejaba el banco sin actualizar.",
        "warning",
    )
    return redirect(url_for("bancos.emitir_cheque", id_posdat=id_posdat, prov=pd.get("prov") or ""))


@posdat_bp.route("/posdat/<int:id_posdat>/reabrir", methods=["POST"])
@requiere_login
@requiere_permiso("posdat.editar")
def reabrir(id_posdat: int):
    pd = queries.por_id(id_posdat)
    if not pd:
        abort(404)
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.reabrir(id_posdat, usuario=usuario)
        flash(f"Posdat {pd['num']} reabierto.", "ok")
    except Exception as e:
        flash_exc("No pude reabrir", e)
    return redirect(url_for("posdat.lista"))


@posdat_bp.route("/posdat/<int:id_posdat>/confirmar-anulacion", methods=["GET"])
@requiere_login
@requiere_permiso("posdat.anular")
def confirmar_anulacion(id_posdat: int):
    pd = queries.por_id(id_posdat)
    if not pd:
        abort(404)
    detalle = {
        "N°": pd.get("num"),
        "Proveedor": pd.get("prov") or "—",
        "Importe": f"$ {pd.get('importe') or 0}",
        "Vencimiento": (pd.get("fechad").strftime("%d/%m/%Y") if pd.get("fechad") else "—"),
        "Estado": "cerrada" if pd.get("banc") == 9 else "abierta",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Anular posdatado {pd.get('num')}",
        mensaje=(
            f"Vas a anular el posdatado N° {pd.get('num')} "
            f"del proveedor {pd.get('prov') or ''} por $ {pd.get('importe') or 0}. "
            f"La fila NO se borra (soft-delete) y queda registrada en /historial."
        ),
        detalle_registro=detalle,
        accion_url=url_for("posdat.anular", id_posdat=id_posdat),
        volver_url=url_for("posdat.lista"),
        motivo_requerido=True,
        # #22 (TMT 2026-05-14): el motivo es obligatorio — antes la vista
        # template decía "obligatorio" pero el handler aceptaba vacío.
        motivo_obligatorio=True,
        confirm_label="Confirmar anulación",
    )


@posdat_bp.route("/posdat/<int:id_posdat>/anular", methods=["POST"])
@requiere_login
@requiere_permiso("posdat.anular")
def anular(id_posdat: int):
    pd = queries.por_id(id_posdat)
    if not pd:
        abort(404)
    motivo = (request.form.get("motivo") or "").strip()
    # TMT 2026-05-21 dueña: motivo opcional sin minlen. Sigue logueando
    # usuario + timestamp en la bitácora.
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.anular(id_posdat, motivo=motivo, usuario=usuario)
        flash(f"Posdat {pd['num']} anulado.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude anular", e)
    return redirect(url_for("posdat.lista"))


@posdat_bp.route("/posdat/retiro-op", methods=["POST"])
@requiere_login
@requiere_permiso("posdat.editar")
def retiro_op():
    """Registra un retiro a accionistas contra el saldo OP ("banco USA").

    Espejo del retiro OP del dBase (RETIROS de='OP'), pero sin movimiento de
    banco: la plata sale de un banco en USA que no está en el programa. Baja
    el saldo OP y queda en /retiros. Por pantalla y reproducible.
    """
    from modules.retiros import queries as _ret

    monto = parse_monto(request.form.get("monto"))
    de = (request.form.get("de") or "OP").strip().upper() or "OP"
    fecha = parse_date(request.form.get("fecha")) or today_ec()
    concepto = (request.form.get("concepto") or "").strip() or None
    # Retiro desde una LÍNEA OP concreta (compra/posdat OP). line_key identifica
    # la línea; el retiro se imputa a ella para mostrar su saldo restante.
    line_key = (request.form.get("line_key") or "").strip() or None
    line_concepto = (request.form.get("line_concepto") or "").strip() or None
    if monto is None or monto <= 0:
        flash("El monto del retiro debe ser mayor que cero.", "warn")
        return redirect(url_for("posdat.lista"))
    try:
        usuario = (g.user or {}).get("username", "web")
        # Aviso (no bloqueo, criterio "PC no bloquea"): si supera el crédito OP
        # abierto en posdatados.
        try:
            saldo = _ret.saldo_op()
            credito = saldo.get("credito") or 0
            if monto > credito + 0.01:
                flash(
                    f"Ojo: el retiro (${monto:,.2f}) supera el crédito OP en "
                    f"posdatados (${credito:,.2f}). Se registró igual.",
                    "warn",
                )
        except Exception:  # noqa: BLE001
            pass
        r = _ret.crear_op(
            monto=monto, de=de, fecha=fecha, concepto=concepto, usuario=usuario,
            line_key=line_key, line_concepto=line_concepto,
        )
        flash(
            f"Retiro OP registrado: {r['de']} $ {r['monto']:,.2f} ({r['concepto']}). "
            f"Quedó en /retiros (igual que dBase).",
            "ok",
        )
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude registrar el retiro OP", e)
    return redirect(url_for("posdat.lista"))


@posdat_bp.route("/posdat/deshacer-retiro-op", methods=["POST"])
@requiere_login
@requiere_permiso("posdat.editar")
def deshacer_retiro_op():
    """Deshace un retiro OP imputado a una línea: borra el retiro (revierte el
    balance) y la imputación (la línea vuelve a subir). Por pantalla."""
    from modules.retiros import queries as _ret

    try:
        rid = int(request.form.get("id_op_retiro_linea") or 0)
    except (TypeError, ValueError):
        rid = 0
    if not rid:
        flash("Falta la imputación a deshacer.", "warn")
        return redirect(url_for("posdat.lista"))
    try:
        usuario = (g.user or {}).get("username", "web")
        r = _ret.deshacer_op(rid, usuario=usuario)
        flash(
            f"Retiro OP deshecho: ${r['monto']:,.2f}. La línea volvió a subir y "
            f"se revirtió el balance.",
            "ok",
        )
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude deshacer el retiro OP", e)
    return redirect(url_for("posdat.lista"))


@posdat_bp.route("/posdat")
@requiere_login
@requiere_permiso("posdat.ver")
def lista():
    # Persistir acumulación YY/RT antes de listar (dBase REPLACE DAILY).
    # Idempotente. TMT 2026-06-05.
    try:
        queries.persistir_acumulacion_yy()
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("programa_core.posdat").exception(
            "persistir_acumulacion_yy FALLÓ — Pasivos YY van a driftear vs dBase"
        )
    q = request.args.get("q", "").strip()
    prov = (request.args.get("prov") or "").strip().upper() or None
    # TMT 2026-05-20 v2 — vuelve default solo_abiertas=True (pedido
    # dueña: "aca solo hay que tomar banc=0"). Sin checkbox visible:
    # la app SIEMPRE filtra banc=0 desde la UI. El total del hero y
    # las filas son consistentes (= deuda viva, no pagada todavía).
    # ?abiertas=0 en URL sigue funcionando para ver TODAS legacy.
    solo_abiertas = request.args.get("abiertas") != "0"
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    # TMT 2026-05-20 — tab='posdatados' (default) excluye prov='YY';
    # tab='yy' solo trae los gastos forzados / provisiones.
    tab = (request.args.get("tab") or "posdatados").strip().lower()
    if tab not in ("posdatados", "yy"):
        tab = "posdatados"

    try:
        filas = queries.buscar(
            prov=prov,
            q=q,
            solo_abiertas=solo_abiertas,
            desde=desde,
            hasta=hasta,
            tab=tab,
        )
        # TMT 2026-05-19 — item 18: el resumen recibe los MISMOS filtros que
        # buscar() para que "X partidas" del hero coincida con las filas
        # visibles. Antes ignoraba q/desde/hasta/solo_abiertas y daba
        # contadores incongruentes.
        resumen = queries.resumen(
            prov=prov,
            q=q,
            solo_abiertas=solo_abiertas,
            desde=desde,
            hasta=hasta,
            tab=tab,
        )
        # TMT 2026-05-20 — conteos por tab para los badges del switcher.
        # Defensivo: si falla, dejamos 0 y la UI sigue.
        try:
            conteos_tab = {
                "posdatados": queries.resumen(
                    prov=prov,
                    q=q,
                    solo_abiertas=solo_abiertas,
                    desde=desde,
                    hasta=hasta,
                    tab="posdatados",
                ),
                "yy": queries.resumen(
                    prov=prov,
                    q=q,
                    solo_abiertas=solo_abiertas,
                    desde=desde,
                    hasta=hasta,
                    tab="yy",
                ),
            }
        except Exception:  # noqa: BLE001
            conteos_tab = {"posdatados": {}, "yy": {}}
        error = None
    except Exception as e:
        filas, resumen, conteos_tab, error = [], {}, {"posdatados": {}, "yy": {}}, str(e)

    if request.args.get("export") == "csv":
        # TMT 2026-05-20 — sale "proveedor" (nombre) del CSV, igual que
        # de la UI. Entra "cuota_mensual" (cuando el posdat matchea
        # contra una provisión). Mantiene paridad con las columnas
        # visibles en /posdat.
        return csv_response(
            filas,
            columnas=[
                ("num", "N°"),
                ("fecha", "Fecha"),
                ("fechad", "Venc."),
                ("prov", "Prov"),
                ("concepto", "Concepto"),
                ("importe", "Importe"),
                ("cuota_mensual", "Cuota mensual"),
                ("cuota_diaria", "Cuota diaria"),
                ("banc", "Banco"),
                ("clave", "Clave"),
            ],
            filename="posdat.csv",
        )

    # TMT 2026-05-20 v2 — totales de IMPORTE + CUOTA MENSUAL para el
    # header de la tab YY. Corrección sobre v1: la dueña pidió que los
    # KPIs sean de las columnas QUE YA TENEMOS visibles, no una
    # diaria derivada. → "Total Importe" (sum del column importe) y
    # "Total Cuota Mensual" (sum del column cuota_mensual). Coincide
    # con lo que se ve en la tabla.
    total_importe = 0.0
    total_cuota_mensual = 0.0
    total_cuota_diaria = 0.0
    if tab == "yy":
        for f in filas:
            total_importe += float(f.get("importe") or 0)
            total_cuota_mensual += float(f.get("cuota_mensual") or 0)
            total_cuota_diaria += float(f.get("cuota_diaria") or 0)
    # TMT 2026-05-28 sesión replanear: el KPI ahora es la PROYECCIÓN del
    # mes completo (25 días hábiles × cuota total), no el día_calendario
    # × cuota que daba números inflados (29 × 29539 = 856k cuando la
    # dueña esperaba ~600k). Con 25 días la proyección ≈ total YY actual.
    _dia_hoy = today_ec().day
    delta_dia_hoy = round(total_cuota_diaria, 2)
    acum_mes_hasta_hoy = round(total_cuota_diaria * 25, 2)

    # Saldo OP (over-price/aporte) para el panel + botón de retiro a accionistas.
    # Sólo se muestra en el tab posdatados. Best-effort: si falla, no rompe.
    saldo_op = None
    if tab == "posdatados":
        try:
            from modules.retiros import queries as _ret
            saldo_op = _ret.saldo_op()
        except Exception:  # noqa: BLE001
            saldo_op = None
        # TMT 2026-06-30 dueña: el retiro OP va EN la fila OP de la tabla de
        # Posdatados (no en una tabla aparte). Adjuntamos a cada fila OP su
        # line_key + restante (crédito − Σ imputado) + las imputaciones (con id
        # para deshacer). Restante baja al retirar; el balance pega una sola vez.
        try:
            from modules.retiros import queries as _ret
            for _f in filas:
                if (_f.get("prov") or "").strip().upper() == "OP":
                    _lk = f"P|{int(_f.get('num') or 0)}|{(_f.get('concepto') or '')}"
                    _imps = _ret.imputaciones_de_linea(_lk)
                    _cred = round(-float(_f.get("importe") or 0), 2)
                    _ret_sum = round(sum(i["monto"] for i in _imps), 2)
                    _f["op_line_key"] = _lk
                    _f["op_credito"] = _cred
                    _f["op_retirado"] = _ret_sum
                    _f["op_restante"] = round(_cred - _ret_sum, 2)
                    _f["op_imputaciones"] = _imps
        except Exception:  # noqa: BLE001
            pass

    # TMT 2026-05-29: no-store para forzar a que el browser/Caddy NO cacheen
    # la respuesta — el display-time depende de _hoy_ec() y cambia día a día.
    # Sin esto, F5 muestra valores cacheados aunque el server calcule bien.
    from flask import make_response as _mr
    resp = _mr(render_template(
        "posdat/lista.html",
        filas=filas,
        resumen=resumen,
        q=q,
        prov=prov,
        desde=desde,
        hasta=hasta,
        solo_abiertas=solo_abiertas,
        error=error,
        tab=tab,
        conteos_tab=conteos_tab,
        total_importe=total_importe,
        total_cuota_mensual=total_cuota_mensual,
        total_cuota_diaria=total_cuota_diaria,
        delta_dia_hoy=delta_dia_hoy,
        acum_mes_hasta_hoy=acum_mes_hasta_hoy,
        dia_del_mes=_dia_hoy,
        saldo_op=saldo_op,
        today_iso=today_ec().isoformat(),
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---------------------------------------------------------------------------
# JSON API — usado por el modal "Editar deudas" del /informes/flujo (ITEM #2).
# Replica MENU.PRG L725-803 PROCEDURE CAMBIA: el dBase permitía
# editar/agregar/borrar posdat banc=9 sin salir de la pantalla de flujo.
# Acá lo hacemos via fetch contra estos endpoints JSON.
# ---------------------------------------------------------------------------


def _posdat_to_dict(pd: dict) -> dict:
    """Serializa una fila de posdat a JSON-friendly dict para los _api."""
    if not pd:
        return {}
    return {
        "id_posdat": pd.get("id_posdat"),
        "num": pd.get("num"),
        "fecha": pd["fecha"].isoformat() if pd.get("fecha") else None,
        "fechad": pd["fechad"].isoformat() if pd.get("fechad") else None,
        "prov": pd.get("prov") or "",
        "proveedor": pd.get("proveedor") or "",
        "importe": float(pd.get("importe") or 0),
        "banc": int(pd.get("banc") or 0),
        "concepto": pd.get("concepto") or "",
    }


@posdat_bp.route("/posdat/_api/lista-flujo", methods=["GET"])
@requiere_login
@requiere_permiso("posdat.ver")
def api_lista_flujo():
    """Lista JSON de posdats activas usadas por el modal de /informes/flujo.

    Replica el filtro de PROCEDURE CAMBIA (MENU.PRG L732-733):
    `USE POSDAT INDEX PF; COPY TO FGASTOS FOR BANC=9`. Acá ampliamos a
    `banc IN (0, 9)` que es el mismo set que `/informes/flujo` proyecta
    como egresos. Excluye anuladas (soft-delete migración 0027).
    """
    try:
        filas = (
            db.fetch_all(
                """
            SELECT pd.id_posdat, pd.num, pd.fecha, pd.fechad, pd.prov, pd.importe,
                   pd.banc, pd.concepto,
                   COALESCE(p.nombre, '') AS proveedor
              FROM scintela.posdat pd
              LEFT JOIN scintela.proveedor p ON p.codigo_prov = pd.prov
             WHERE COALESCE(pd.banc, 0) IN (0, 9)
               AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
             ORDER BY pd.fechad NULLS LAST, pd.id_posdat
            """
            )
            or []
        )
        return jsonify(
            {
                "ok": True,
                "posdats": [_posdat_to_dict(r) for r in filas],
                "total": sum(float(r.get("importe") or 0) for r in filas),
            }
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@posdat_bp.route("/posdat/_api/<int:id_posdat>/editar", methods=["POST"])
@requiere_login
@requiere_permiso("posdat.editar")
def api_editar(id_posdat: int):
    """Edita importe/fechad/concepto desde el modal de flujo o desde el
    inline-edit de la lista de posdat. JSON in/out.

    TMT 2026-05-20 — acepta updates parciales: si el caller manda sólo
    `importe`, no se exige `concepto` (se conserva el de la fila). Si el
    caller manda concepto explícito, sigue valiendo la validación
    original. Pedido dueña: "dejame ingresar le total en posdatados".
    """
    pd = queries.por_id(id_posdat)
    if not pd:
        return jsonify({"ok": False, "error": "Posdat no encontrado."}), 404
    data = request.get_json(silent=True) or request.form
    fechad = parse_date(data.get("fechad"))
    importe = parse_monto(data.get("importe"))
    # `concepto` sólo se considera "intencional" si la key vino en el
    # payload — un null/empty explícito sigue siendo error.
    concepto_provided = "concepto" in (data.keys() if hasattr(data, "keys") else {})
    concepto_raw = data.get("concepto") if concepto_provided else None
    concepto = (concepto_raw or "").strip() if concepto_provided else None

    if importe is not None and importe == 0:
        return jsonify({"ok": False, "error": "Importe no puede ser cero."}), 400
    if concepto_provided and not concepto:
        return jsonify({"ok": False, "error": "Concepto vacío."}), 400
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.editar(
            id_posdat,
            fechad=fechad,
            importe=importe,
            concepto=concepto if concepto_provided else None,
            usuario=usuario,
        )
        actualizado = queries.por_id(id_posdat)
        return jsonify({"ok": True, "posdat": _posdat_to_dict(actualizado)})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"No pude actualizar: {e}"}), 500


@posdat_bp.route("/posdat/_api/<int:id_posdat>/anular", methods=["POST"])
@requiere_login
@requiere_permiso("posdat.anular")
def api_anular(id_posdat: int):
    """Soft-delete desde el modal. Requiere motivo >=10 chars.

    Si la posdat tiene banc<>0 (ya pagada con cheque), `queries.anular`
    levanta ValueError y devolvemos 400 — la UI tiene que mostrar el
    mensaje y mandar al usuario a reversar el cheque primero.
    """
    pd = queries.por_id(id_posdat)
    if not pd:
        return jsonify({"ok": False, "error": "Posdat no encontrado."}), 404
    data = request.get_json(silent=True) or request.form
    motivo = (data.get("motivo") or "").strip()
    # TMT 2026-05-20 v3 — para YY (cuotas mensuales) no exigimos motivo
    # largo: la dueña edita la grilla y elimina/agrega líneas como un
    # spreadsheet ("que no me bloquee"). Para posdat normales sigue
    # vigente el mínimo de 10 chars (audit de proveedores reales).
    es_yy = (pd.get("prov") or "").upper() == "YY"
    # TMT 2026-05-21 dueña: motivo opcional sin minlen.
    if es_yy and not motivo:
        motivo = "Eliminado desde lista YY"
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.anular(id_posdat, motivo=motivo, usuario=usuario)
        return jsonify({"ok": True, "id_posdat": id_posdat})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"No pude anular: {e}"}), 500


@posdat_bp.route("/posdat/_api/nuevo", methods=["POST"])
@requiere_login
@requiere_permiso("posdat.crear")
def api_nuevo():
    """Crea una posdat banc=0 (deuda viva) desde el modal de flujo.

    No exponemos `banc=9` aquí — para emitir un cheque hay que usar el
    wizard /bancos/emitir-cheque, que es atómico y registra el movimiento
    bancario. Esto solo permite cargar la deuda viva, igual que la opción
    "A" del dBase original (PROCEDURE CAMBIA L789-797).
    """
    data = request.get_json(silent=True) or request.form
    # TMT 2026-05-15 (re-audit H3): validamos fechas explícitamente. Antes,
    # un string mal formado silenciosamente caía a today() — el usuario
    # creía haber cargado la deuda con la fecha que tipeó.
    fecha_raw = (data.get("fecha") or "").strip()
    fechad_raw = (data.get("fechad") or "").strip()
    if fecha_raw:
        fecha = parse_date(fecha_raw)
        if fecha is None:
            return jsonify({"ok": False, "error": f"Fecha inválida: {fecha_raw!r}."}), 400
    else:
        fecha = today_ec()
    if fechad_raw:
        fechad = parse_date(fechad_raw)
        if fechad is None:
            return jsonify({"ok": False, "error": f"Fecha venc. inválida: {fechad_raw!r}."}), 400
    else:
        fechad = fecha
    prov = (data.get("prov") or "").strip().upper()
    importe = parse_monto(data.get("importe"))
    concepto = (data.get("concepto") or "").strip()
    if not prov:
        return jsonify({"ok": False, "error": "Proveedor requerido."}), 400
    # TMT 2026-05-20 v3 — YY (cuotas mensuales) puede crearse con
    # importe=0 y concepto vacío; la dueña los completa después inline.
    es_yy = prov == "YY"
    if importe is None:
        importe = 0.0 if es_yy else None
    if not es_yy and (importe is None or importe == 0):
        return jsonify({"ok": False, "error": "Importe no puede ser cero."}), 400
    if not concepto and not es_yy:
        return jsonify({"ok": False, "error": "Concepto requerido."}), 400
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.crear(
            fecha=fecha,
            fechad=fechad,
            prov=prov,
            importe=importe,
            concepto=concepto,
            usuario=usuario,
        )
        nuevo = queries.por_id(int(r.get("id_posdat") or 0)) if r else None
        return jsonify({"ok": True, "posdat": _posdat_to_dict(nuevo)})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"No pude crear: {e}"}), 500
