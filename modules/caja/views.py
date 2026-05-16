"""Caja — libro de caja en efectivo."""
from datetime import datetime

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
from exports import csv_response
from parsers import parse_date, parse_monto

from . import queries

caja_bp = Blueprint("caja", __name__, template_folder="templates")


@caja_bp.route("/caja/_api/preview-concepto")
@requiere_login
@requiere_permiso("caja.ver")
def preview_concepto():
    """JSON: previsualiza el side effect que dispararía un concepto.

    Usado por el form de nuevo movimiento de caja para mostrar al usuario
    qué movimiento doble se va a crear (ej. "+ entrada en Pichincha").
    """
    import concepto_parser, db as _db
    concepto = (request.args.get("concepto") or "").strip()
    if not concepto:
        return {"descripcion": ""}
    provs_validos = {
        (r.get("codigo_prov") or "").strip().upper()
        for r in (_db.fetch_all(
            "SELECT codigo_prov FROM scintela.proveedor"
        ) or [])
    }
    bancos_map: dict = {}
    for b in _db.fetch_all(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco"
    ) or []:
        n = (b.get("nombre") or "").upper().strip()
        if "PICHINC" in n:
            bancos_map.setdefault("PICHINCHA", int(b["no_banco"]))
        if "INTER" in n:
            bancos_map.setdefault("INTERNACIONAL", int(b["no_banco"]))
    parsed = concepto_parser.parse_concepto(
        concepto,
        {"provs_validos": provs_validos, "bancos": bancos_map},
    )
    return {
        "tipo": parsed.get("tipo"),
        "descripcion": concepto_parser.descripcion_humana(parsed),
    }


@caja_bp.route("/caja/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("caja.crear")
def nuevo():
    errores: list[str] = []
    form: dict = {}

    # Conceptos más usados en el histórico — datalist para autocomplete.
    try:
        conceptos = queries.conceptos_frecuentes(limite=50)
    except Exception:
        conceptos = []

    if request.method == "GET":
        form["fecha"] = datetime.now().date().isoformat()
        # Prefijar el tipo según el botón clickeado: ?tipo=E (entrada) o S (salida).
        # Convención legacy dBase: E=entrada, S=salida (importe siempre positivo).
        # Default 'E' para que el form arranque en ingreso.
        tipo_inicial = (request.args.get("tipo") or "E").upper()
        if tipo_inicial not in ("E", "S"):
            tipo_inicial = "E"
        form["tipo"] = tipo_inicial
        form["saldo_actual"] = queries.saldo_actual()
        return render_template("caja/nuevo.html", form=form, errores=errores,
                               conceptos=conceptos)

    fecha = parse_date(request.form.get("fecha"))
    tipo = (request.form.get("tipo") or "").strip().upper()
    importe = parse_monto(request.form.get("importe"))
    concepto = (request.form.get("concepto") or "").strip()

    if fecha is None:
        errores.append("Fecha inválida.")
    if tipo not in ("E", "S"):
        errores.append("Tipo debe ser E (entrada/ingreso) o S (salida/egreso).")
    if importe is None or importe <= 0:
        errores.append("Importe debe ser mayor que cero.")
    if not concepto:
        errores.append("Concepto requerido.")

    form.update({
        "fecha": request.form.get("fecha"),
        "tipo": tipo,
        "importe": request.form.get("importe"),
        "concepto": concepto,
        "saldo_actual": queries.saldo_actual(),
    })

    if errores:
        return render_template("caja/nuevo.html", form=form, errores=errores,
                               conceptos=conceptos), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:3].upper()
        r = queries.crear(
            fecha=fecha, tipo=tipo, importe=importe,
            concepto=concepto, clave=clave, usuario=usuario,
        )
        flash(f"Movimiento de caja registrado (id {r.get('id_caja')}).", "ok")
        return redirect(url_for("caja.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template("caja/nuevo.html", form=form, errores=errores), 400
    except Exception as e:
        errores.append(f"No pude registrar: {e}")
        return render_template("caja/nuevo.html", form=form, errores=errores), 500


@caja_bp.route("/caja/<int:id_caja>/confirmar-reverso", methods=["GET"])
@requiere_login
@requiere_permiso("caja.crear")
def confirmar_reverso(id_caja: int):
    """Paso 1 del wizard: resumen + motivo antes de reversar caja.

    El reverso de caja puede arrastrar side-effects (banco, retiro,
    USD, compra). Pedirle el motivo a la dueña por escrito antes de
    ejecutar es la convención canónica del programa (mismo patrón
    que confirmar_anulacion / confirmar_reverso de cheques).
    TMT 2026-05-13.
    """
    from flask import abort
    m = queries.por_id(id_caja)
    if not m:
        abort(404)
    tipo_legible = {"E": "Entrada", "S": "Salida"}.get((m.get("tipo") or "").upper(), m.get("tipo") or "—")
    detalle = {
        "ID caja": m.get("id_caja"),
        "Fecha": (m.get("fecha").strftime("%d/%m/%Y") if m.get("fecha") else "—"),
        "Tipo": tipo_legible,
        "Importe": f"$ {m.get('importe') or 0:,.2f}",
        "Concepto": m.get("concepto") or "—",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Reversar movimiento de caja id={id_caja}",
        mensaje=(
            f"Vas a reversar el movimiento de caja id={id_caja} "
            f"({tipo_legible} $ {m.get('importe') or 0:,.2f}). "
            "Se crea un movimiento opuesto que compensa este — el original "
            "queda intacto en la historia."
        ),
        detalle_registro=detalle,
        accion_url=url_for("caja.reversar", id_caja=id_caja),
        volver_url=url_for("caja.lista"),
        motivo_requerido=True,
        motivo_obligatorio=True,  # reverso de caja exige motivo (puede arrastrar side effects)
        confirm_label="Confirmar reverso",
    )


@caja_bp.route("/caja/<int:id_caja>/reversar", methods=["POST"])
@requiere_login
@requiere_permiso("caja.crear")
def reversar(id_caja: int):
    """Crea un movimiento opuesto que compensa al id_caja indicado.

    No borra la fila original — conserva audit trail. Inserta una nueva
    con tipo invertido (E↔S), mismo importe, concepto "REVERSO id N — <motivo>".
    """
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Motivo requerido para reversar el movimiento.", "warn")
        return redirect(url_for("caja.confirmar_reverso", id_caja=id_caja))
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.reversar(id_caja, motivo=motivo, usuario=usuario)
        msg = (
            f"Movimiento id={r['id_caja_original']} reversado. "
            f"Nuevo id={r['id_caja_nuevo']} (tipo {r['tipo_nuevo']}, $ {r['importe']:.2f})."
        )
        se = r.get("side_effect_reversado")
        if se:
            tipo_legible = {
                "transfer_banco": "banco",
                "retiro_socio": "retiro de socio",
                "dolares": "USD",
                "compra_proveedor": "compra a proveedor",
            }.get(se.get("tipo"), se.get("tipo"))
            msg += f" También se reversó el side effect de {tipo_legible}."
        flash(msg, "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash(f"No pude reversar: {e}", "warn")
    return redirect(url_for("caja.lista"))


@caja_bp.route("/caja")
@requiere_login
@requiere_permiso("caja.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    try:
        filas = queries.movimientos(desde, hasta, q)
        resumen = queries.resumen()
        # Egresos del mes sin clasificar como gasto V1..V9 — pasamos sólo
        # el SET de ids; el botón "Clasificar" aparece inline en la fila
        # de cada egreso que está en este set (TMT 2026-05-15, opción B).
        from modules.gastos import queries as _gq
        try:
            egresos_sin_clasif = _gq.caja_egresos_sin_clasificar(limite=2000)
        except Exception:
            egresos_sin_clasif = []
        ids_sin_clasif = {int(e["id_caja"]) for e in egresos_sin_clasif}
        error = None
    except Exception as e:
        filas, resumen, error = [], {}, str(e)
        egresos_sin_clasif, ids_sin_clasif = [], set()

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"),
                ("tipo", "Tipo"),
                ("concepto", "Concepto"),
                ("importe", "Importe"),
                ("saldo", "Saldo"),
                ("clave", "Clave"),
                ("no_cheque", "N° cheque"),
                ("cliente", "Cliente"),
            ],
            filename="caja.csv",
        )

    return render_template(
        "caja/lista.html",
        filas=filas, resumen=resumen,
        q=q, desde=desde, hasta=hasta, error=error,
        ids_sin_clasif=ids_sin_clasif,
        n_sin_clasif=len(ids_sin_clasif),
    )
