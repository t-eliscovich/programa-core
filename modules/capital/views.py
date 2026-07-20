"""Capital — sólo Dueño."""

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
from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from filters import today_ec
from parsers import parse_date, parse_monto

from . import queries

capital_bp = Blueprint("capital", __name__, template_folder="templates")


@capital_bp.route("/capital/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("capital.crear")
def nuevo():
    errores: list[str] = []
    form: dict = {}
    if request.method == "GET":
        # TMT 2026-06-05 (bug hunt lente 3): today_ec(), no datetime.now() UTC.
        form["fecha"] = today_ec().isoformat()
        form["estado"] = queries.estado_actual() or {}
        return render_template("capital/nuevo.html", form=form, errores=errores)

    fecha = parse_date(request.form.get("fecha"))
    doc = (request.form.get("doc") or "").strip().upper()
    concepto = (request.form.get("concepto") or "").strip()
    importe = parse_monto(request.form.get("importe"))
    invanual = parse_monto(request.form.get("invanual"))
    capital = parse_monto(request.form.get("capital"))
    util = parse_monto(request.form.get("util"))
    patri = parse_monto(request.form.get("patri"))

    if fecha is None:
        errores.append("Fecha inválida.")
    if not doc:
        errores.append("Doc requerido.")
    if not concepto:
        errores.append("Concepto requerido.")
    if importe is None:
        errores.append("Importe requerido.")

    form.update({
        "fecha": request.form.get("fecha"),
        "doc": doc, "concepto": concepto,
        "importe": request.form.get("importe"),
        "invanual": request.form.get("invanual"),
        "capital": request.form.get("capital"),
        "util": request.form.get("util"),
        "patri": request.form.get("patri"),
        "estado": queries.estado_actual() or {},
    })

    if errores:
        return render_template("capital/nuevo.html", form=form, errores=errores), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:3].upper()
        r = queries.crear(
            fecha=fecha, doc=doc, concepto=concepto, importe=importe,
            invanual=invanual, capital=capital, util=util, patri=patri,
            clave=clave, usuario=usuario,
        )
        flash(f"Movimiento de capital registrado (id {r.get('id_capital')}).", "ok")
        return redirect(url_for("capital.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template("capital/nuevo.html", form=form, errores=errores), 400
    except Exception as e:
        errores.append(f"No pude registrar: {e}")
        return render_template("capital/nuevo.html", form=form, errores=errores), 500


@capital_bp.route("/capital/aportar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("capital.crear")
def aportar():
    """Aporte de capital con link a banco/caja — atómico.

    Inserta en scintela.capital + side-effect (caja entrada o banco DE).
    """
    errores: list[str] = []
    form: dict = {
        "fecha": today_ec().isoformat(),
        "importe": "",
        "cuenta": "pichincha",
        "socio": "",
        "concepto": "",
    }

    if request.method == "POST":
        fecha = parse_date(request.form.get("fecha")) or today_ec()
        importe = parse_monto(request.form.get("importe"))
        cuenta = (request.form.get("cuenta") or "").strip().lower()
        socio = (request.form.get("socio") or "").strip().upper()[:5] or None
        concepto = (request.form.get("concepto") or "").strip()

        form.update({
            "fecha": request.form.get("fecha") or fecha.isoformat(),
            "importe": request.form.get("importe"),
            "cuenta": cuenta,
            "socio": socio or "",
            "concepto": concepto,
        })

        if importe is None or importe <= 0:
            errores.append("Importe del aporte debe ser mayor que cero.")
        if cuenta not in queries.CUENTAS_APORTE:
            errores.append(
                f"Cuenta inválida. Elegí una de: {', '.join(queries.CUENTAS_APORTE)}."
            )

        if errores:
            return render_template("capital/aportar.html", form=form, errores=errores), 400

        try:
            usuario = (g.user or {}).get("username", "web")
            clave = (g.user or {}).get("clave") or usuario[:3].upper()
            r = queries.aportar(
                fecha=fecha, importe=importe, cuenta=cuenta,
                concepto=concepto, socio=socio,
                clave=clave, usuario=usuario,
            )
            flash(
                f"Aporte de $ {r['importe']:.2f} registrado. "
                f"Capital ahora: $ {r['capital_nuevo']:.2f}. "
                f"Side effect: {r['side_effect']['tipo']}.",
                "ok",
            )
            return redirect(url_for("capital.lista"))
        except ValueError as e:
            errores.append(str(e))
            return render_template("capital/aportar.html", form=form, errores=errores), 400
        except Exception as e:
            flash_exc("No pude registrar el aporte", e)
            return redirect(url_for("capital.lista"))

    return render_template("capital/aportar.html", form=form, errores=errores)


@capital_bp.route("/capital/retirar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("capital.crear")
def retirar():
    """Retiro de un socio con link a banco/caja — atómico.

    Inserta en scintela.retiros + side-effect (caja salida o banco CH).
    """
    errores: list[str] = []
    form: dict = {
        "fecha": today_ec().isoformat(),
        "importe": "",
        "cuenta": "pichincha",
        "socio": "",
        "concepto": "",
    }

    if request.method == "POST":
        fecha = parse_date(request.form.get("fecha")) or today_ec()
        importe = parse_monto(request.form.get("importe"))
        cuenta = (request.form.get("cuenta") or "").strip().lower()
        socio = (request.form.get("socio") or "").strip().upper()[:5]
        concepto = (request.form.get("concepto") or "").strip()

        form.update({
            "fecha": request.form.get("fecha") or fecha.isoformat(),
            "importe": request.form.get("importe"),
            "cuenta": cuenta,
            "socio": socio,
            "concepto": concepto,
        })

        # TMT 2026-07-20 (duena): negativo = APORTE de capital. Bloquea solo 0.
        if importe is None or float(importe) == 0:
            errores.append("Importe no puede ser cero (negativo = aporte de capital).")
        if not socio:
            errores.append("Socio requerido (RR/TMT/etc.).")
        if cuenta not in queries.CUENTAS_APORTE:
            errores.append(
                f"Cuenta inválida. Elegí una de: {', '.join(queries.CUENTAS_APORTE)}."
            )

        if errores:
            return render_template("capital/retirar.html", form=form, errores=errores), 400

        try:
            usuario = (g.user or {}).get("username", "web")
            clave = (g.user or {}).get("clave") or usuario[:3].upper()
            r = queries.retirar(
                fecha=fecha, importe=importe, cuenta=cuenta,
                socio=socio, concepto=concepto,
                clave=clave, usuario=usuario,
            )
            _etq = ("Aporte de capital (retiro negativo)"
                    if r["importe"] < 0 else "Retiro")
            flash(
                f"{_etq} de $ {abs(r['importe']):.2f} ({r['socio']}) registrado. "
                f"Side effect: {r['side_effect']['tipo']}.",
                "ok",
            )
            return redirect(url_for("capital.lista"))
        except ValueError as e:
            errores.append(str(e))
            return render_template("capital/retirar.html", form=form, errores=errores), 400
        except Exception as e:
            flash_exc("No pude registrar el retiro", e)
            return redirect(url_for("capital.lista"))

    return render_template("capital/retirar.html", form=form, errores=errores)


@capital_bp.route("/capital/aporte/<int:id_capital>/reversar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("capital.crear")
def reversar_aporte(id_capital: int):
    """Wizard de 2 pasos para reversar un aporte de capital.

    GET: muestra detalles + motivo (opcional).
    POST: ejecuta queries.reversar_aporte (atómico).
    TMT 2026-05-13.
    """
    cap = db.fetch_one(
        """
        SELECT id_capital, fecha, doc, concepto, importe, capital, util, patri
          FROM scintela.capital WHERE id_capital = %s
        """,
        (id_capital,),
    )
    if not cap:
        abort(404)
    if float(cap.get("importe") or 0) <= 0:
        flash(f"Aporte id={id_capital} no tiene importe positivo — "
              "probablemente ya está reversado.", "warn")
        return redirect(url_for("capital.lista"))

    if request.method == "POST":
        motivo = (request.form.get("motivo") or "").strip()
        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.reversar_aporte(
                id_capital=id_capital, motivo=motivo, usuario=usuario,
            )
            flash(
                f"Aporte id={id_capital} reversado. Cuenta {r['cuenta']} "
                f"compensada por $ {r['importe']:.2f}.",
                "ok",
            )
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("No pude reversar el aporte", e)
        return redirect(url_for("capital.lista"))

    detalle = {
        "ID aporte": id_capital,
        "Fecha":     cap.get("fecha"),
        "Concepto":  cap.get("concepto") or "—",
        "Importe":   f"$ {float(cap.get('importe') or 0):,.2f}",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Reversar aporte de capital id={id_capital}",
        mensaje=(
            "Vas a deshacer este aporte. Se insertará un asiento de capital "
            "compensatorio (negativo) y la cuenta donde entró la plata "
            "(caja/banco) recibirá un egreso del mismo importe. Atómico."
        ),
        detalle_registro=detalle,
        accion_url=url_for("capital.reversar_aporte", id_capital=id_capital),
        volver_url=url_for("capital.lista"),
        motivo_requerido=True,
        motivo_obligatorio=False,
        confirm_label="Confirmar reverso de aporte",
    )


@capital_bp.route("/capital/retiro/<int:id_retiro>/reversar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("capital.crear")
def reversar_retiro(id_retiro: int):
    """Wizard de 2 pasos para reversar un retiro de socio.

    GET: muestra detalles + motivo (opcional).
    POST: ejecuta queries.reversar_retiro (atómico).
    TMT 2026-05-13.
    """
    ret = db.fetch_one(
        """
        SELECT id_retiro, fecha, ret, de, nb, concepto
          FROM scintela.retiros WHERE id_retiro = %s
        """,
        (id_retiro,),
    )
    if not ret:
        abort(404)
    if float(ret.get("ret") or 0) == 0:
        flash(f"Retiro id={id_retiro} tiene importe 0 — nada que reversar.", "warn")
        return redirect(url_for("capital.lista"))

    if request.method == "POST":
        motivo = (request.form.get("motivo") or "").strip()
        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.reversar_retiro(
                id_retiro=id_retiro, motivo=motivo, usuario=usuario,
            )
            flash(
                f"Retiro id={id_retiro} reversado. Cuenta {r['cuenta']} "
                f"compensada por $ {r['importe']:.2f}.",
                "ok",
            )
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("No pude reversar el retiro", e)
        return redirect(url_for("capital.lista"))

    detalle = {
        "ID retiro": id_retiro,
        "Fecha":     ret.get("fecha"),
        "Socio":     ret.get("de") or "—",
        "Concepto":  ret.get("concepto") or "—",
        "Importe":   f"$ {float(ret.get('ret') or 0):,.2f}",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Reversar retiro de socio id={id_retiro}",
        mensaje=(
            "Vas a deshacer este retiro. Se insertará una fila de retiros "
            "compensatoria (importe negativo) y la cuenta de donde salió la "
            "plata (caja/banco) recibirá un ingreso del mismo importe. Atómico."
        ),
        detalle_registro=detalle,
        accion_url=url_for("capital.reversar_retiro", id_retiro=id_retiro),
        volver_url=url_for("capital.lista"),
        motivo_requerido=True,
        motivo_obligatorio=False,
        confirm_label="Confirmar reverso de retiro",
    )


@capital_bp.route("/capital")
@requiere_login
@requiere_permiso("capital.ver")
def lista():
    """Movimientos del dueño — capital + retiros unificados.

    El sidebar y el tablero apuntan acá con el label "Movimientos del dueño".
    Tabs: todos / aportes (capital) / retiros.
    """
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    # TMT 2026-05-18: default a "retiros" — la dueña abre esta pantalla
    # para ver retiros, no para auditar capital.
    filtro = (request.args.get("filtro") or "retiros").lower()
    if filtro not in ("todos", "aportes", "retiros"):
        filtro = "retiros"
    q = (request.args.get("q") or "").strip()
    try:
        filas = queries.movimientos_unificados(filtro, desde, hasta)
        if q:
            ql = q.lower()
            filas = [m for m in filas
                     if ql in (m.get("concepto") or "").lower()
                     or ql in (m.get("persona") or "").lower()
                     or ql in (m.get("doc") or "").lower()]
        conteos = queries.conteos_unificados(desde, hasta)
        estado = queries.estado_actual()
        error = None
    except Exception as e:
        filas, conteos, estado, error = [], {}, None, str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("tipo", "Tipo"),
                ("fecha", "Fecha"),
                ("doc", "Doc"),
                ("concepto", "Concepto"),
                ("persona", "Persona"),
                ("banco", "Banco"),
                ("importe", "Importe"),
                ("clave", "Clave"),
            ],
            filename=f"movimientos_dueno_{filtro}.csv",
        )

    return render_template(
        "capital/lista.html",
        filas=filas, estado=estado, conteos=conteos,
        filtro=filtro, desde=desde, hasta=hasta, q=q, error=error,
    )
