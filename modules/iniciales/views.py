"""Views de iniciales/metas mensuales."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from parsers import parse_int, parse_monto

from . import queries

iniciales_bp = Blueprint("iniciales", __name__, template_folder="templates")


@iniciales_bp.route("/iniciales")
@requiere_login
@requiere_permiso("iniciales.ver")
def lista():
    yy = parse_int(request.args.get("yy")) or queries.anio_actual()
    try:
        filas = queries.lista_anio(yy)
        anios = queries.anios_disponibles()
        # Si el año seleccionado no está en la lista, lo agregamos para el dropdown
        if yy not in anios:
            anios.insert(0, yy)
        error = None
    except Exception as e:
        filas, anios, error = [], [queries.anio_actual()], str(e)

    # Totales del año (suma de las 12 metas)
    total_kg = sum(float(f["kprog"] or 0) for f in filas)
    total_gasto = sum(float(f["gprog"] or 0) for f in filas)
    total_importe = sum(float(f["pretot"] or 0) for f in filas)

    return render_template(
        "iniciales/lista.html",
        filas=filas, yy=yy, anios=anios,
        total_kg=total_kg, total_gasto=total_gasto, total_importe=total_importe,
        error=error,
    )


@iniciales_bp.route("/iniciales/comparativo")
@requiere_login
@requiere_permiso("iniciales.ver")
def comparativo():
    yy = parse_int(request.args.get("yy")) or queries.anio_actual()
    try:
        filas = queries.comparativo_anio(yy)
        anios = queries.anios_disponibles()
        if yy not in anios:
            anios.insert(0, yy)
        error = None
    except Exception as e:
        filas, anios, error = [], [queries.anio_actual()], str(e)

    # Totales acumulados
    tot_kg_meta = sum(float(f["kg_meta"] or 0) for f in filas)
    tot_kg_real = sum(float(f["kg_real"] or 0) for f in filas)
    tot_imp_meta = sum(float(f["importe_meta"] or 0) for f in filas)
    tot_imp_real = sum(float(f["importe_real"] or 0) for f in filas)
    tot_gasto_meta = sum(float(f["gasto_meta"] or 0) for f in filas)
    tot_gasto_real = sum(float(f["gasto_real"] or 0) for f in filas)

    return render_template(
        "iniciales/comparativo.html",
        filas=filas, yy=yy, anios=anios,
        tot_kg_meta=tot_kg_meta, tot_kg_real=tot_kg_real,
        tot_imp_meta=tot_imp_meta, tot_imp_real=tot_imp_real,
        tot_gasto_meta=tot_gasto_meta, tot_gasto_real=tot_gasto_real,
        error=error,
    )


@iniciales_bp.route("/iniciales/nueva", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("iniciales.editar")
def nueva():
    errores: list[str] = []
    yy_default = parse_int(request.args.get("yy")) or queries.anio_actual()
    mes_default = parse_int(request.args.get("mes")) or 1

    if request.method == "GET":
        form = {
            "mesnum": mes_default, "yy": yy_default,
            "kprog": "", "gprog": "", "pretot": "",
            "hilado": "", "tejido": "", "terminado": "",
        }
        return render_template(
            "iniciales/form.html",
            form=form, errores=errores, modo="crear",
            meses=queries.MESES_ES,
        )

    mesnum = parse_int(request.form.get("mesnum"))
    yy = parse_int(request.form.get("yy"))
    kprog = parse_monto(request.form.get("kprog"))
    gprog = parse_monto(request.form.get("gprog"))
    pretot = parse_monto(request.form.get("pretot"))
    hilado = parse_monto(request.form.get("hilado"))
    tejido = parse_monto(request.form.get("tejido"))
    terminado = parse_monto(request.form.get("terminado"))

    if not mesnum or not (1 <= mesnum <= 12):
        errores.append("Mes inválido (1-12).")
    if not yy:
        errores.append("Año requerido.")

    form = {
        "mesnum": mesnum or mes_default, "yy": yy or yy_default,
        "kprog": request.form.get("kprog"), "gprog": request.form.get("gprog"),
        "pretot": request.form.get("pretot"),
        "hilado": request.form.get("hilado"), "tejido": request.form.get("tejido"),
        "terminado": request.form.get("terminado"),
    }

    if errores:
        return render_template(
            "iniciales/form.html", form=form, errores=errores, modo="crear",
            meses=queries.MESES_ES,
        ), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.crear(
            mesnum=mesnum, yy=yy,
            kprog=kprog, gprog=gprog, pretot=pretot,
            hilado=hilado, tejido=tejido, terminado=terminado,
            usuario=usuario,
        )
        flash(f"Meta de {queries.MESES_ES[mesnum-1]} {yy} creada.", "ok")
        return redirect(url_for("iniciales.lista", yy=yy))
    except ValueError as e:
        errores.append(str(e))
        return render_template(
            "iniciales/form.html", form=form, errores=errores, modo="crear",
            meses=queries.MESES_ES,
        ), 400
    except Exception as e:
        errores.append(f"No pude crear: {e}")
        return render_template(
            "iniciales/form.html", form=form, errores=errores, modo="crear",
            meses=queries.MESES_ES,
        ), 500


@iniciales_bp.route("/iniciales/<int:id_iniciales>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("iniciales.editar")
def editar(id_iniciales: int):
    fila = queries.por_id(id_iniciales)
    if not fila:
        abort(404)
    errores: list[str] = []

    if request.method == "GET":
        form = {
            "id_iniciales": fila["id_iniciales"],
            "mesnum": fila.get("mesnum"), "yy": fila.get("yy"),
            "kprog": fila.get("kprog") or "", "gprog": fila.get("gprog") or "",
            "pretot": fila.get("pretot") or "",
            "hilado": fila.get("hilado") or "", "tejido": fila.get("tejido") or "",
            "terminado": fila.get("terminado") or "",
        }
        return render_template(
            "iniciales/form.html", form=form, errores=errores, modo="editar",
            meses=queries.MESES_ES,
        )

    kprog = parse_monto(request.form.get("kprog"))
    gprog = parse_monto(request.form.get("gprog"))
    pretot = parse_monto(request.form.get("pretot"))
    hilado = parse_monto(request.form.get("hilado"))
    tejido = parse_monto(request.form.get("tejido"))
    terminado = parse_monto(request.form.get("terminado"))

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.editar(
            id_iniciales,
            kprog=kprog, gprog=gprog, pretot=pretot,
            hilado=hilado, tejido=tejido, terminado=terminado,
            usuario=usuario,
        )
        flash("Meta actualizada.", "ok")
        return redirect(url_for("iniciales.lista", yy=fila.get("yy")))
    except Exception as e:
        flash_exc("No pude actualizar", e)
        form = {
            "id_iniciales": id_iniciales,
            "mesnum": fila.get("mesnum"), "yy": fila.get("yy"),
            "kprog": request.form.get("kprog"), "gprog": request.form.get("gprog"),
            "pretot": request.form.get("pretot"),
            "hilado": request.form.get("hilado"), "tejido": request.form.get("tejido"),
            "terminado": request.form.get("terminado"),
        }
        return render_template(
            "iniciales/form.html", form=form, errores=errores, modo="editar",
            meses=queries.MESES_ES,
        ), 500


@iniciales_bp.route("/iniciales/cerrar-mes-auto", methods=["POST"])
@requiere_login
@requiere_permiso("iniciales.editar")
def cerrar_mes_auto():  # TMT 2026-05-15 (re-audit H): POST-only — antes
    # aceptaba GET y prefetchers/link previewers podían disparar el cierre.
    """ITEM #5 — Endpoint manual para forzar el cierre automático.

    Idempotente: si ya se cerró el mes destino, devuelve `aplicado=False`.
    Usado por:
      1. Llamada manual del usuario desde /iniciales.
      2. Hook automático en /informes/balance (ver auto_cerrar_mes_si_corresponde
         más abajo) cuando entra alguien el día 1 del mes.
    """
    try:
        usuario = (g.user or {}).get("username", "web")
        res = queries.cerrar_mes_auto(usuario=usuario)
        if res.get("aplicado"):
            flash(
                f"Cierre automático aplicado: copiado stock+precios al mes "
                f"{res['mes_destino']} (id #{res['id_iniciales_nuevo']}).",
                "ok",
            )
        else:
            flash(f"No se aplicó cierre: {res.get('razon')}", "info")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude cerrar el mes", e)
    return redirect(url_for("iniciales.lista"))


def auto_cerrar_mes_si_corresponde() -> dict:
    """Helper para llamar desde /informes/balance — idempotente, no rompe.

    Se invoca cada vez que alguien entra al balance. Si la fecha hoy
    pertenece a un mes NUEVO y el mes anterior nunca se cerró, ejecuta
    el cierre automático silenciosamente (no flashea).
    """
    try:
        return queries.cerrar_mes_auto(usuario="auto-balance")
    except Exception as e:  # noqa: BLE001
        return {"aplicado": False, "error": str(e)}
