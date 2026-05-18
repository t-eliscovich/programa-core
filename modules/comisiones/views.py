"""Comisiones de vendedores — list + inline edit + detalle mensual."""
from datetime import date

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

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from parsers import parse_monto

from . import queries

comisiones_bp = Blueprint("comisiones", __name__, template_folder="templates")


def _yy_mm() -> tuple[int, int]:
    """Lee ?anio= y ?mes= del query string. Default: mes en curso."""
    hoy = date.today()
    try:
        yy = int(request.args.get("anio") or hoy.year)
    except (TypeError, ValueError):
        yy = hoy.year
    try:
        mm = int(request.args.get("mes") or hoy.month)
    except (TypeError, ValueError):
        mm = hoy.month
    mm = max(1, min(mm, 12))
    return yy, mm


@comisiones_bp.route("/comisiones")
@requiere_login
@requiere_permiso("informes.ver")
def lista():
    yy, mm = _yy_mm()
    try:
        filas = queries.lista(anio=yy, mes=mm)
        error = None
    except Exception as e:
        filas = []
        msg = str(e)
        # TMT 2026-05-18 — mensaje legible si la migración 0032 no corrió.
        if 'scintela.vendedor' in msg and 'does not exist' in msg:
            error = ("La tabla scintela.vendedor todavía no existe. "
                     "Aplicá la migración: python scripts/migrate.py")
        else:
            error = msg

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("codigo", "Código"),
                ("nombre", "Nombre"),
                ("pct_comision", "% comisión"),
                ("n_clientes", "N° clientes"),
                ("ventas_mes", "Ventas mes"),
                ("cobranzas_mes", "Cobranzas mes"),
                ("comision_mes", "Comisión mes"),
            ],
            filename=f"comisiones_{yy}_{mm:02d}.csv",
        )

    totales = {
        "cobranzas": sum(float(f.get("cobranzas_mes") or 0) for f in filas),
        "ventas":    sum(float(f.get("ventas_mes") or 0) for f in filas),
        "comision":  sum(float(f.get("comision_mes") or 0) for f in filas),
    }

    return render_template(
        "comisiones/lista.html",
        filas=filas, totales=totales,
        anio=yy, mes=mm, error=error,
    )


@comisiones_bp.route("/comisiones/<codigo>")
@requiere_login
@requiere_permiso("informes.ver")
def detalle(codigo: str):
    yy, mm = _yy_mm()
    v = queries.por_codigo(codigo)
    if not v:
        abort(404)
    try:
        cobranzas = queries.cobranzas_detalle(codigo, anio=yy, mes=mm)
        ventas    = queries.ventas_detalle(codigo, anio=yy, mes=mm)
        error = None
    except Exception as e:
        cobranzas, ventas, error = [], [], str(e)

    total_cobr = sum(float(r.get("importe") or 0) for r in cobranzas)
    total_vent = sum(float(r.get("importe") or 0) for r in ventas)
    pct = float(v.get("pct_comision") or 0)
    comision = round(total_cobr * pct / 100.0, 2)

    return render_template(
        "comisiones/detalle.html",
        vendedor=v, cobranzas=cobranzas, ventas=ventas,
        total_cobr=total_cobr, total_vent=total_vent,
        pct=pct, comision=comision,
        anio=yy, mes=mm, error=error,
    )


@comisiones_bp.route("/comisiones/<codigo>/pct", methods=["POST"])
@requiere_login
@requiere_permiso("informes.ver")
def actualizar_pct(codigo: str):
    """Inline edit del % desde la lista."""
    v = queries.por_codigo(codigo)
    if not v:
        abort(404)
    pct = parse_monto(request.form.get("pct_comision"))
    if pct is None or pct < 0 or pct > 100:
        flash("% inválido (debe ser 0-100).", "error")
        return redirect(url_for("comisiones.lista",
                                anio=request.form.get("anio"),
                                mes=request.form.get("mes")))
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.actualizar_pct(codigo, pct, usuario=usuario)
        flash(f"% de {codigo} actualizado a {pct}%.", "ok")
    except Exception as e:
        flash_exc("No pude actualizar", e)
    return redirect(url_for("comisiones.lista",
                            anio=request.form.get("anio"),
                            mes=request.form.get("mes")))


@comisiones_bp.route("/comisiones/<codigo>/nombre", methods=["POST"])
@requiere_login
@requiere_permiso("informes.ver")
def actualizar_nombre(codigo: str):
    """Inline edit del nombre desde la lista."""
    v = queries.por_codigo(codigo)
    if not v:
        abort(404)
    nombre = (request.form.get("nombre") or "").strip()
    if not nombre:
        flash("Nombre vacío.", "error")
        return redirect(url_for("comisiones.lista"))
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.actualizar_nombre(codigo, nombre, usuario=usuario)
        flash(f"Nombre de {codigo} actualizado.", "ok")
    except Exception as e:
        flash_exc("No pude actualizar", e)
    return redirect(url_for("comisiones.lista",
                            anio=request.form.get("anio"),
                            mes=request.form.get("mes")))
