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

# TMT 2026-07-20 (duena): "esa pagina movimientos de capital, no esta en el
# menu, borremosla. no tenemos que tener basura". Se eliminaron /capital
# (lista), /capital/nuevo y /capital/aportar. El alta de aportes/retiros vive
# en /capital/retirar (botones en Dividendos /informes/retiros; aporte =
# retiro negativo, paridad dBase). reversar_retiro y reversar_aporte QUEDAN:
# el historial los referencia para deshacer movimientos (viejos incluidos).
capital_bp = Blueprint("capital", __name__, template_folder="templates")


@capital_bp.route("/capital/retirar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("capital.crear")
def retirar():
    """Retiro de un socio con link a banco/caja — atómico.

    Inserta en scintela.retiros + side-effect (caja salida o banco CH).
    """
    errores: list[str] = []
    # TMT 2026-07-20 (duena): ?modo=aporte — misma pantalla en "modo aporte":
    # el importe se escribe en POSITIVO y se guarda como retiro NEGATIVO
    # (paridad dBase). Botones en /informes/retiros (Dividendos).
    modo = (request.values.get("modo") or "").strip().lower()
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
        elif modo == "aporte" and float(importe) > 0:
            # En modo aporte se escribe en positivo; se guarda negativo.
            importe = -importe
        if not socio:
            errores.append("Socio requerido (RR/TMT/etc.).")
        if cuenta not in queries.CUENTAS_APORTE:
            errores.append(
                f"Cuenta inválida. Elegí una de: {', '.join(queries.CUENTAS_APORTE)}."
            )

        if errores:
            return render_template("capital/retirar.html", form=form, errores=errores, modo=modo), 400

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
            if modo == "aporte":
                return redirect(url_for("informes.retiros"))
            return redirect(url_for("informes.retiros"))
        except ValueError as e:
            errores.append(str(e))
            return render_template("capital/retirar.html", form=form, errores=errores, modo=modo), 400
        except Exception as e:
            flash_exc("No pude registrar el retiro", e)
            return redirect(url_for("informes.retiros"))

    return render_template("capital/retirar.html", form=form, errores=errores, modo=modo)


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
        return redirect(url_for("informes.retiros"))

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
        return redirect(url_for("informes.retiros"))

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
        volver_url=url_for("informes.retiros"),
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
        return redirect(url_for("informes.retiros"))

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
        return redirect(url_for("informes.retiros"))

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
        volver_url=url_for("informes.retiros"),
        motivo_requerido=True,
        motivo_obligatorio=False,
        confirm_label="Confirmar reverso de retiro",
    )
