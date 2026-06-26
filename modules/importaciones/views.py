"""/importaciones — importaciones de Asinfo cruzadas con compras del programa."""
from __future__ import annotations

from flask import (
    Blueprint,
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
from parsers import parse_int, parse_monto

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
    pago = (request.args.get("pago") or "").strip()      # "" | "pendiente" | "contabilizada"

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
        rows = [r for r in rows if r.get("fuente")]
    elif estado == "sin_match":
        rows = [r for r in rows if r.get("codigo") and not r.get("fuente")]
    elif estado == "sin_codigo":
        rows = [r for r in rows if not r.get("codigo")]
    if recep == "recibida":
        rows = [r for r in rows if r.get("recibida")]
    elif recep == "pendiente":
        rows = [r for r in rows if not r.get("recibida")]
    # Filtro por estado de pago/contabilización (sólo aplica a las cruzadas).
    if pago == "pendiente":
        rows = [r for r in rows if r.get("fuente") and not r.get("contabilizada")]
    elif pago == "contabilizada":
        rows = [r for r in rows if r.get("fuente") and r.get("contabilizada")]

    # KPIs de pago (sobre el conjunto YA filtrado, como el resto de contadores).
    pend_pago = sum(1 for r in rows if r.get("fuente") and not r.get("contabilizada"))
    contab = sum(1 for r in rows if r.get("fuente") and r.get("contabilizada"))
    kg_pend_pago = sum(
        float(r.get("kg") or 0)
        for r in rows
        if r.get("fuente") == "compra" and not r.get("contabilizada")
    )

    total = len(rows)
    con_codigo = sum(1 for r in rows if r.get("codigo"))
    con_match = sum(1 for r in rows if r.get("fuente"))
    sin_codigo = total - con_codigo
    recibidas = sum(1 for r in rows if r.get("recibida"))
    pendientes = total - recibidas
    importe_programa = sum(
        r["importe_programa"] for r in rows if r.get("importe_programa")
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
                "kg": round(r["kg"], 2) if r.get("kg") is not None else "",
                "total_asinfo": round(r.get("total_asinfo") or 0, 2),
                "fuente": (r.get("fuente") or "").capitalize(),
                "importe_programa": (
                    round(r["importe_programa"], 2) if r.get("importe_programa") else ""
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
                ("kg", "Kg"),
                ("total_asinfo", "Total Asinfo (ref)"),
                ("fuente", "Fuente programa"),
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
        pend_pago=pend_pago,
        contab=contab,
        kg_pend_pago=kg_pend_pago,
        q=q,
        estado=estado,
        recep=recep,
        pago=pago,
        error=error,
    )


def _volver():
    """Vuelve a /importaciones preservando los filtros actuales."""
    args = {
        k: request.form.get(k)
        for k in ("q", "estado", "recep", "pago")
        if request.form.get(k)
    }
    return redirect(url_for("importaciones.lista", **args))


@importaciones_bp.route("/importaciones/contabilizar", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def contabilizar():
    """Marca/desmarca una importación como contabilizada (libera/retiene kilos).

    Por pantalla y reproducible: form con prov + numero + valor. La marca es
    lo que libera los kilos en TC/PT (todo-o-nada), aunque el pago sea parcial.
    """
    from modules.importaciones import pago as _pago

    prov = (request.form.get("prov") or "").strip().upper()
    numero = parse_int(request.form.get("numero"))
    quiere = (request.form.get("contabilizada") or "").strip() in ("1", "true", "on", "si")
    if not prov or numero is None:
        flash("Importación inválida (faltan proveedor o número).", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        _pago.set_contabilizada(prov, numero, quiere, usuario=usuario)
        flash(
            f"Importación {prov} {numero} "
            + ("contabilizada (kilos liberados)." if quiere else "marcada pendiente (kilos retenidos)."),
            "ok",
        )
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude actualizar la contabilización", e)
    return _volver()


@importaciones_bp.route("/importaciones/monto-pagado", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def monto_pagado():
    """Guarda el monto pagado de una importación (informativo, parcial OK)."""
    from modules.importaciones import pago as _pago

    prov = (request.form.get("prov") or "").strip().upper()
    numero = parse_int(request.form.get("numero"))
    monto = parse_monto(request.form.get("monto"))
    if not prov or numero is None:
        flash("Importación inválida (faltan proveedor o número).", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        _pago.set_monto_pagado(prov, numero, monto or 0, usuario=usuario)
        flash(f"Monto pagado de {prov} {numero} guardado: $ {monto or 0:,.2f}.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude guardar el monto pagado", e)
    return _volver()
