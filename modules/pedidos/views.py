"""/pedidos — qué pidieron los clientes y todavía no se despachó.

Cinco cortes del mismo dato, en el orden del Excel "Detalle de Pedidos" de la
dueña: Color · Tipo de tela · Acabado · Cliente · Categoría. El corte por
acabado no está todavía (el acabado es atributo de LOTE en Asinfo y el pedido
no tiene lote — no hay fuente limpia a nivel producto).

  Color      → una fila por color (código grande, nombre chico), con pedido,
               stock, producción y faltante; se abre a sus telas y clientes.
  Tipo tela  → una pestaña por familia, un bloque por tela, una fila por color.
  Cliente    → total pedido por cliente (mezcla unidades, como el Excel).
  Categoría  → total pedido por familia de tela.

Todo se lee en la unidad en que se pide: rollos ENTEROS para las telas,
unidades para cuellos y puños.

Rutas:
  GET /pedidos                  (facturas.ver)
  GET /pedidos/color/<codigo>   (facturas.ver)

Va en el menú debajo de Factura Proforma (dueña 2026-08-17).
"""
from __future__ import annotations

from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso

from . import service

pedidos_bp = Blueprint("pedidos", __name__, template_folder="templates")

#: Los cortes disponibles, EN EL ORDEN del Excel de la dueña. El primero es el
#: default: la pantalla abre en Color.
CORTES = ("color", "tela", "cliente", "categoria")
CORTES_LBL = {
    "color": "Color",
    "tela": "Tipo de tela",
    "cliente": "Cliente",
    "categoria": "Categoría",
}


@pedidos_bp.route("/pedidos")
@requiere_login
@requiere_permiso("facturas.ver")
def lista():
    filas, disponible = service.pendientes()
    categorias = service.por_categoria(filas)

    corte = (request.args.get("corte") or "").strip()
    if corte not in CORTES:
        corte = CORTES[0]

    ctx = dict(
        disponible=disponible,
        categorias=categorias,
        corte=corte,
        cortes=CORTES,
        cortes_lbl=CORTES_LBL,
        dias_pedido_max=service.DIAS_PEDIDO_MAX,
        dias_produccion_viva=service.DIAS_PRODUCCION_VIVA,
        kg_por_rollo=service.KG_POR_ROLLO,
        # se completan según el corte
        colores=[], telas=[], activa="", resumen=None,
        clientes=[], solo_faltan=False, cubiertas=[],
        pedidos_por_color={},
    )

    if not disponible or not categorias:
        return render_template("pedidos/lista.html", **ctx)

    if corte in ("color", "tela"):
        service.marcar_acabado(filas)   # el punto TUB/ABI; fail-soft

    if corte == "color":
        ctx["colores"] = service.por_color(filas)
        ctx["pedidos_por_color"] = service.pedidos_por_color()
    elif corte == "cliente":
        ctx["clientes"] = service.por_cliente()
    elif corte == "tela":
        # La pestaña por defecto es la que MÁS falta, no la primera alfabética.
        pedida = (request.args.get("cat") or "").strip()
        nombres = [c["categoria"] for c in categorias]
        activa = pedida if pedida in nombres else (nombres[0] if nombres else "")
        solo_faltan = request.args.get("falta") == "1"
        telas = service.por_tela(filas, activa, solo_faltan=solo_faltan) if activa else []
        for t in telas:
            service.en_unidad(t["filas"], "alt")
            t["faltan_d"], t["dec"], t["u"] = service.total_en_unidad(
                t["faltan_kg"], "alt",
                any(f["en_unidades"] for f in t["filas"]),
                next((f["un_por_kg"] for f in t["filas"] if f["un_por_kg"]), 0.0))
        ctx.update(
            activa=activa, telas=telas, solo_faltan=solo_faltan,
            cubiertas=service.telas_sin_faltante(filas, activa) if solo_faltan else [],
            resumen=next((c for c in categorias if c["categoria"] == activa), None),
            pedidos_por_color=service.pedidos_por_color(),
        )
    # corte == "categoria" usa `categorias`, ya calculado.

    return render_template("pedidos/lista.html", **ctx)


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
