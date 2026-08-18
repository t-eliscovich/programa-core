"""/inventario-rotativo — lo que se vende siempre y hay que tener.

Dos cortes del mismo dato: por COLOR (default, dueña 2026-08-18) y por tela.
El filtrado —estado, familia, búsqueda— se hace en el navegador: la consulta
a Asinfo tarda unos segundos y recargar la página entera para tildar un filtro
haría la pantalla incómoda justo en el uso normal.

Rutas:
  GET /inventario-rotativo         (stock.ver)
  GET /inventario-rotativo/excel   (stock.ver)

La hoja impresa sale de la MISMA pantalla (`?imprimir=1`): si fuera otra
plantilla, las dos se irían separando y lo que se lleva a planta dejaría de
ser lo que se mira. Mismo criterio que la hoja del portal de vendedores.

Va en el menú "Producción y stocks", arriba de Inventario.
"""
from __future__ import annotations

import io

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso
from filters import today_ec

from . import service

inventario_rotativo_bp = Blueprint(
    "inventario_rotativo", __name__, template_folder="templates"
)

#: El corte por COLOR primero: es como la dueña mira el inventario.
CORTES = ("color", "tela")
CORTES_LBL = {"color": "Color", "tela": "Tela"}


@inventario_rotativo_bp.route("/inventario-rotativo")
@requiere_login
@requiere_permiso("stock.ver")
def lista():
    filas, disponible = service.rotativo()

    corte = (request.args.get("ver") or "").strip()
    if corte not in CORTES:
        corte = CORTES[0]

    return render_template(
        "inventario_rotativo/lista.html",
        disponible=disponible,
        corte=corte,
        imprimir=request.args.get("imprimir") == "1",
        hoy=today_ec(),
        cortes=CORTES,
        cortes_lbl=CORTES_LBL,
        bloques=service.agrupar(filas, corte) if filas else [],
        resumen=service.resumen(filas),
        sem_rojo=service.SEM_ROJO,
        lead_semanas=service.LEAD_SEMANAS,
        semanas_minimas=service.SEMANAS_MINIMAS,
    )


#: Las columnas de la hoja y del Excel, en el mismo orden que la pantalla.
COLUMNAS = ("Color", "Tela", "Familia", "Por semana", "En bodega",
            "Pedido (informativo)", "En proceso", "Unidad", "Alcanza (sem)",
            "Falta", "Falta kg")


def _valores(f: dict) -> list:
    """Una fila del service → la fila del Excel, en la unidad que se lee."""
    return [
        f["color"], f["tela"], f["familia"],
        f["sem"], f["stock"], f["pedido"], f["proceso"], f["unidad"],
        # "no sé" (sin venta reciente) va vacío y no como -1: un número
        # negativo en una columna de semanas se lee como un dato, no como
        # un hueco.
        None if f["alcanza"] < 0 else f["alcanza"],
        f["falta"], f["falta_kg"],
    ]


@inventario_rotativo_bp.route("/inventario-rotativo/excel")
@requiere_login
@requiere_permiso("stock.ver")
def excel():
    """La misma tabla, en un xlsx. Una hoja por corte no: una sola, plana.

    Con las dos vistas mezcladas en un archivo habría que elegir cuál filtrar;
    plana se ordena y se filtra en Excel, que es lo que se hace con ella.
    """
    filas, disponible = service.rotativo()
    if not disponible:
        flash("No pude consultar Asinfo, así que no armé el Excel. "
              "Probá de nuevo en un minuto.", "error")
        return redirect(url_for("inventario_rotativo.lista"))

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        flash("openpyxl no instalado en el server — pedí al admin que lo instale.",
              "error")
        return redirect(url_for("inventario_rotativo.lista"))

    wb = Workbook()
    ws = wb.active
    ws.title = "Inventario rotativo"
    bold = Font(bold=True)
    relleno = PatternFill("solid", fgColor="E2E8F0")
    for i, h in enumerate(COLUMNAS, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = bold
        c.fill = relleno
        c.alignment = Alignment(horizontal="left")

    for fila, f in enumerate(filas, 2):
        for i, v in enumerate(_valores(f), 1):
            ws.cell(row=fila, column=i, value=v)

    for col, ancho in zip("ABCDEFGHIJK",
                          (10, 26, 14, 12, 12, 18, 12, 9, 14, 10, 12),
                          strict=True):
        ws.column_dimensions[col].width = ancho
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:K{max(len(filas) + 1, 2)}"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    nombre = f"inventario_rotativo_{today_ec():%Y_%m_%d}.xlsx"
    return Response(
        buf.getvalue(),
        mimetype=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
