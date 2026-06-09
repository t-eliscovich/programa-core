"""/importaciones — importaciones de Asinfo cruzadas con compras del programa."""
from __future__ import annotations

from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from exports import csv_response

importaciones_bp = Blueprint(
    "importaciones",
    __name__,
    template_folder="templates",
)


@importaciones_bp.route("/importaciones")
@requiere_login
@requiere_permiso("stock.ver")
def lista():
    from modules.importaciones import service

    q = (request.args.get("q") or "").strip().upper()
    estado = (request.args.get("estado") or "").strip()  # "" | "match" | "sin_match" | "sin_codigo"
    recep = (request.args.get("recep") or "").strip()    # "" | "recibida" | "pendiente"

    error = None
    rows = []
    try:
        rows = service.importaciones_con_cruce()
    except Exception as e:  # noqa: BLE001
        error = str(e)

    if q:
        rows = [
            r for r in rows
            if q in (r.get("proveedor") or "").upper()
            or q in (r.get("nota") or "").upper()
            or q in (r.get("codigo") or "").upper()
            or q in (r.get("im_numero") or "").upper()
        ]
    if estado == "match":
        rows = [r for r in rows if r.get("compra")]
    elif estado == "sin_match":
        rows = [r for r in rows if r.get("codigo") and not r.get("compra")]
    elif estado == "sin_codigo":
        rows = [r for r in rows if not r.get("codigo")]
    if recep == "recibida":
        rows = [r for r in rows if r.get("recibida")]
    elif recep == "pendiente":
        rows = [r for r in rows if not r.get("recibida")]

    total = len(rows)
    con_codigo = sum(1 for r in rows if r.get("codigo"))
    con_match = sum(1 for r in rows if r.get("compra"))
    sin_codigo = total - con_codigo
    recibidas = sum(1 for r in rows if r.get("recibida"))
    pendientes = total - recibidas
    importe_programa = sum(
        r["compra"]["importe_total"] for r in rows if r.get("compra")
    )

    if request.args.get("export") == "csv":
        export_rows = [
            {
                "im_numero": r["im_numero"],
                "fecha": r.get("fecha") or "",
                "fecha_recepcion": r.get("fecha_recepcion") or "",
                "recepcion": "Recibida" if r.get("recibida") else "Pendiente",
                "bod": r.get("bod") or "",
                "proveedor": r.get("proveedor") or "",
                "codigo": r.get("codigo") or "",
                "nota": r.get("nota") or "",
                "total_asinfo": round(r.get("total_asinfo") or 0, 2),
                "compra_programa": (
                    "; ".join(str(i) for i in r["compra"]["ids"]) if r.get("compra") else ""
                ),
                "importe_programa": (
                    round(r["compra"]["importe_total"], 2) if r.get("compra") else ""
                ),
            }
            for r in rows
        ]
        return csv_response(
            export_rows,
            columnas=[
                ("im_numero", "Importación"),
                ("fecha", "Fecha"),
                ("fecha_recepcion", "Fecha Recepción"),
                ("recepcion", "Recepción"),
                ("bod", "Doc. Recepción"),
                ("proveedor", "Proveedor"),
                ("codigo", "Código programa"),
                ("nota", "Nota Asinfo"),
                ("total_asinfo", "Total Asinfo (ref)"),
                ("compra_programa", "ID compra(s)"),
                ("importe_programa", "Importe programa (US)"),
            ],
            filename="importaciones_cruce.csv",
        )

    return render_template(
        "importaciones/lista.html",
        rows=rows,
        total=total,
        con_codigo=con_codigo,
        con_match=con_match,
        sin_codigo=sin_codigo,
        recibidas=recibidas,
        pendientes=pendientes,
        importe_programa=importe_programa,
        q=q,
        estado=estado,
        recep=recep,
        error=error,
    )
