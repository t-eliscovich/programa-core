"""Vistas de activos fijos — listado + acción de amortización mensual."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response

from . import queries

activos_bp = Blueprint("activos", __name__, template_folder="templates")


@activos_bp.route("/activos")
@requiere_login
@requiere_permiso("activos.ver")
def lista():
    q = request.args.get("q", "").strip()
    tipo = (request.args.get("tipo") or "").strip().upper() or None
    solo_activos = request.args.get("solo_activos") == "1"

    try:
        filas = queries.buscar(q=q, tipo=tipo, solo_activos=solo_activos)
        resumen = queries.resumen()
        tipos = queries.tipos_disponibles()
        error = None
    except Exception as e:
        filas, resumen, tipos, error = [], {}, [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha",            "Fecha"),
                ("concepto",         "Concepto"),
                ("tipo",             "Tipo"),
                ("proveedor",        "Proveedor"),
                ("inicial",          "Valor inicial"),
                ("amortizac",        "Amort. acum."),
                ("amortimes",        "Cuota mensual"),
                ("valor_libros",     "Valor en libros"),
                ("pct_depreciado",   "% depreciado"),
                ("vida_util",        "Vida útil (m)"),
                ("ult_mes_amortizado", "Últ. mes amort."),
            ],
            filename="activos.csv",
        )

    return render_template(
        "activos/lista.html",
        filas=filas, q=q, tipo=tipo, solo_activos=solo_activos,
        resumen=resumen, tipos=tipos,
        error=error,
    )


@activos_bp.route("/activos/amortizar", methods=["POST"])
@requiere_login
@requiere_permiso("activos.amortizar")
def amortizar():
    """Corre `scintela.actualizar_amortizacion()` del mes actual.

    Idempotente: si ya corrió este mes, sale sin tocar nada.
    """
    try:
        usuario = (g.user or {}).get("username", "web")
        result = queries.correr_amortizacion(usuario=usuario)
        if result.get("ya_estaba"):
            flash(
                f"La amortización del mes {result['mes']} ya estaba aplicada — "
                "no se tocó ningún activo.",
                "info",
            )
        else:
            flash(
                f"Amortización del mes {result['mes']} ejecutada. "
                f"{result['filas_tocadas']} activo(s) actualizado(s).",
                "ok",
            )
    except Exception as e:
        flash_exc("No pude correr la amortización", e)
    return redirect(url_for("activos.lista"))
