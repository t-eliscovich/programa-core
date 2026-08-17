"""/pedidos — qué pidieron los clientes y todavía no se despachó.

Una pestaña por tipo de tela, un bloque por tela y una fila por color, con
lo pedido, lo que hay en bodega, lo que se está tinturando y lo que falta.
El detalle de un color muestra QUIÉN lo pidió y qué órdenes están en curso.

Rutas:
  GET /pedidos                  (facturas.ver)
  GET /pedidos/color/<codigo>   (facturas.ver)

Va en el menú debajo de Factura Proforma (dueña 2026-08-17): es información
comercial —de quién es cada pedido—, no un informe de stock.
"""
from __future__ import annotations

from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso

from . import service

pedidos_bp = Blueprint("pedidos", __name__, template_folder="templates")


@pedidos_bp.route("/pedidos")
@requiere_login
@requiere_permiso("facturas.ver")
def lista():
    filas, disponible = service.pendientes()
    categorias = service.por_categoria(filas)

    # La pestaña por defecto es la que MÁS falta, no la primera alfabética:
    # la pantalla se abre en el problema.
    pedida = (request.args.get("cat") or "").strip()
    nombres = [c["categoria"] for c in categorias]
    activa = pedida if pedida in nombres else (nombres[0] if nombres else "")

    telas = service.por_tela(filas, activa) if activa else []
    resumen = next((c for c in categorias if c["categoria"] == activa), None)

    return render_template(
        "pedidos/lista.html",
        disponible=disponible,
        categorias=categorias,
        activa=activa,
        telas=telas,
        resumen=resumen,
        total_faltan=round(sum(c["faltan_kg"] for c in categorias), 1),
        total_colores_faltan=sum(c["colores_faltan"] for c in categorias),
        dias_pedido_max=service.DIAS_PEDIDO_MAX,
        dias_produccion_viva=service.DIAS_PRODUCCION_VIVA,
        kg_por_rollo=service.KG_POR_ROLLO,
    )


@pedidos_bp.route("/pedidos/color/<codigo>")
@requiere_login
@requiere_permiso("facturas.ver")
def color(codigo: str):
    """Detalle de un color: quién lo pidió y qué se está tinturando.

    No devuelve 404 cuando el código no existe: si Asinfo no contesta no
    podemos distinguir "no existe" de "no pude preguntar", y un 404 ahí
    mentiría. Muestra la ficha vacía con el aviso.
    """
    ficha, pedidos, ordenes, disponible = service.detalle_color(codigo)
    return render_template(
        "pedidos/color.html",
        codigo=codigo.strip().upper(),
        ficha=ficha,
        pedidos=pedidos,
        ordenes=ordenes,
        repetidos=service.repetidos(pedidos),
        disponible=disponible,
        kg_por_rollo=service.KG_POR_ROLLO,
    )
