"""Vista de Stock — kg + $ del inventario por etapa."""
from flask import Blueprint, render_template

from auth import requiere_login, requiere_permiso

from . import queries

stock_bp = Blueprint("stock", __name__, template_folder="templates")


@stock_bp.route("/stock")
@requiere_login
@requiere_permiso("informes.ver")
def lista():
    """Pantalla principal de Stock.

    Comparte el permiso con /informes/balance porque el dato sale del
    mismo cálculo (informe_balance.stock). Si la dueña puede ver el
    balance, puede ver el stock; si no, tampoco.
    """
    try:
        stock = queries.resumen_stock()
        error = None
    except Exception as e:
        stock = {}
        error = str(e)

    try:
        compras_recientes = queries.compras_mes_por_tipo(meses_atras=3)
    except Exception:
        compras_recientes = []

    return render_template(
        "stock/lista.html",
        stock=stock,
        compras_recientes=compras_recientes,
        label_tipo=queries.label_tipo,
        error=error,
    )
