"""Posdat — vista/CRUD de pasivos abiertos con proveedores."""
from datetime import date as _date
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
        for k in ("fecha", "fechad", "prov", "importe", "concepto",
                  "tipo", "compr", "no_comp", "num"):
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
                "fecha":    request.form.get("fecha") or "",
                "fechad":   request.form.get("fechad") or "",
                "prov":     prov,
                "importe":  request.form.get("importe") or "",
                "concepto": concepto or "",
                "tipo":     tipo or "",
                "compr":    compr or "",
                "no_comp":  no_comp or "",
                "num":      request.form.get("num") or "",
            }
            restore_args = {k: v for k, v in restore_args.items() if v}
            next_url = url_for("posdat.nueva") + "?" + urlencode(restore_args)
            flash(
                f"El proveedor {prov} no existe — completá los datos para "
                "crearlo y después seguís con el posdatado.",
                "warning",
            )
            return redirect(
                url_for("proveedores.nuevo", codigo=prov, next=next_url)
            )
        errores.append(f"El proveedor {prov!r} no existe.")
    if importe is None or importe <= 0:
        errores.append("Importe debe ser mayor que cero.")
    if not concepto:
        errores.append("Concepto requerido.")

    form = {
        "fecha": request.form.get("fecha"),
        "fechad": request.form.get("fechad"),
        "prov": prov, "importe": request.form.get("importe"),
        "concepto": concepto, "tipo": tipo,
        "compr": compr, "no_comp": no_comp,
        "num": request.form.get("num"),
        "num_sugerido": queries.proximo_num(),
    }

    if errores:
        return render_template("posdat/form.html", form=form, errores=errores, modo="crear"), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.crear(
            fecha=fecha, fechad=fechad, prov=prov, importe=importe,
            concepto=concepto, tipo=tipo or None,
            compr=compr or None, no_comp=no_comp or None,
            num=num, usuario=usuario,
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
                f"Posdat #{pd.get('num') or id_posdat} · "
                f"{pd.get('prov') or ''} · {pd.get('concepto') or ''}"
            )[:200]
            rec.registrar("posdat", id_posdat, etiqueta=etiqueta)
        except Exception:  # noqa: BLE001
            # TMT 2026-05-15 (re-audit M2): logeamos sin re-raise.
            import logging as _lg
            _lg.getLogger(__name__).exception(
                "recientes.registrar(posdat, %s) falló", id_posdat,
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
    if importe is not None and importe <= 0:
        errores.append("Importe debe ser mayor que cero.")
    if errores:
        form = {"id_posdat": id_posdat, **request.form}
        return render_template("posdat/form.html", form=form, errores=errores, modo="editar"), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.editar(
            id_posdat,
            fechad=fechad, importe=importe,
            concepto=concepto, tipo=tipo or None,
            compr=compr or None, no_comp=no_comp or None,
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
    return redirect(
        url_for("bancos.emitir_cheque", id_posdat=id_posdat,
                prov=pd.get("prov") or "")
    )


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
    # #22 (TMT 2026-05-14): motivo obligatorio (>=10 chars). Inconsistencia
    # vs template viejo: el form pedía motivo pero el handler aceptaba vacío.
    if not motivo or len(motivo) < 10:
        flash(
            "Motivo de anulación obligatorio (al menos 10 caracteres). "
            "Dejá una traza clara del por qué.",
            "warning",
        )
        return redirect(url_for("posdat.confirmar_anulacion", id_posdat=id_posdat))
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.anular(id_posdat, motivo=motivo, usuario=usuario)
        flash(f"Posdat {pd['num']} anulado.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude anular", e)
    return redirect(url_for("posdat.lista"))


@posdat_bp.route("/posdat")
@requiere_login
@requiere_permiso("posdat.ver")
def lista():
    q = request.args.get("q", "").strip()
    prov = (request.args.get("prov") or "").strip().upper() or None
    solo_abiertas = request.args.get("abiertas") != "0"
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None

    try:
        filas = queries.buscar(
            prov=prov, q=q, solo_abiertas=solo_abiertas,
            desde=desde, hasta=hasta,
        )
        # TMT 2026-05-19 — item 18: el resumen recibe los MISMOS filtros que
        # buscar() para que "X partidas" del hero coincida con las filas
        # visibles. Antes ignoraba q/desde/hasta/solo_abiertas y daba
        # contadores incongruentes.
        resumen = queries.resumen(
            prov=prov, q=q, solo_abiertas=solo_abiertas,
            desde=desde, hasta=hasta,
        )
        error = None
    except Exception as e:
        filas, resumen, error = [], {}, str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("num", "N°"),
                ("fecha", "Fecha"),
                ("fechad", "Venc."),
                ("prov", "Prov"),
                ("proveedor", "Nombre"),
                ("concepto", "Concepto"),
                ("importe", "Importe"),
                ("banc", "Banco"),
                ("clave", "Clave"),
            ],
            filename="posdat.csv",
        )

    return render_template(
        "posdat/lista.html",
        filas=filas, resumen=resumen,
        q=q, prov=prov, desde=desde, hasta=hasta,
        solo_abiertas=solo_abiertas, error=error,
    )


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
        "num":       pd.get("num"),
        "fecha":     pd["fecha"].isoformat() if pd.get("fecha") else None,
        "fechad":    pd["fechad"].isoformat() if pd.get("fechad") else None,
        "prov":      pd.get("prov") or "",
        "proveedor": pd.get("proveedor") or "",
        "importe":   float(pd.get("importe") or 0),
        "banc":      int(pd.get("banc") or 0),
        "concepto":  pd.get("concepto") or "",
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
        filas = db.fetch_all(
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
        ) or []
        return jsonify({
            "ok": True,
            "posdats": [_posdat_to_dict(r) for r in filas],
            "total": sum(float(r.get("importe") or 0) for r in filas),
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@posdat_bp.route("/posdat/_api/<int:id_posdat>/editar", methods=["POST"])
@requiere_login
@requiere_permiso("posdat.editar")
def api_editar(id_posdat: int):
    """Edita importe/fechad/concepto desde el modal de flujo. JSON in/out."""
    pd = queries.por_id(id_posdat)
    if not pd:
        return jsonify({"ok": False, "error": "Posdat no encontrado."}), 404
    data = request.get_json(silent=True) or request.form
    fechad = parse_date(data.get("fechad"))
    importe = parse_monto(data.get("importe"))
    concepto = (data.get("concepto") or "").strip()
    if importe is not None and importe <= 0:
        return jsonify({"ok": False, "error": "Importe debe ser > 0."}), 400
    if not concepto:
        return jsonify({"ok": False, "error": "Concepto requerido."}), 400
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.editar(
            id_posdat,
            fechad=fechad, importe=importe, concepto=concepto,
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
    if len(motivo) < 10:
        return jsonify({
            "ok": False,
            "error": "Motivo obligatorio (mínimo 10 caracteres).",
        }), 400
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
            return jsonify({"ok": False,
                            "error": f"Fecha inválida: {fecha_raw!r}."}), 400
    else:
        fecha = _date.today()
    if fechad_raw:
        fechad = parse_date(fechad_raw)
        if fechad is None:
            return jsonify({"ok": False,
                            "error": f"Fecha venc. inválida: {fechad_raw!r}."}), 400
    else:
        fechad = fecha
    prov = (data.get("prov") or "").strip().upper()
    importe = parse_monto(data.get("importe"))
    concepto = (data.get("concepto") or "").strip()
    if not prov:
        return jsonify({"ok": False, "error": "Proveedor requerido."}), 400
    if importe is None or importe <= 0:
        return jsonify({"ok": False, "error": "Importe debe ser > 0."}), 400
    if not concepto:
        return jsonify({"ok": False, "error": "Concepto requerido."}), 400
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.crear(
            fecha=fecha, fechad=fechad, prov=prov, importe=importe,
            concepto=concepto, usuario=usuario,
        )
        nuevo = queries.por_id(int(r.get("id_posdat") or 0)) if r else None
        return jsonify({"ok": True, "posdat": _posdat_to_dict(nuevo)})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"No pude crear: {e}"}), 500
