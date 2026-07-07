"""/importaciones — importaciones de Asinfo cruzadas con compras del programa.

Modelo v2 (TMT 2026-07-06 dueña): sin flujo "Pagar" ni predicción de costo.
Los ANTICIPOS (≈90% del valor) se cargan acá como movimientos (ND automática
en Pichincha); el RESTANTE se carga por /compras como compra normal al
proveedor. Valor del stock de cada importación = Σ anticipos.
"""
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
from filters import today_ec
from parsers import parse_date, parse_int, parse_monto

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
        rows = [r for r in rows if r.get("fuente")]
    elif estado == "sin_match":
        rows = [r for r in rows if r.get("codigo") and not r.get("fuente")]
    elif estado == "sin_codigo":
        rows = [r for r in rows if not r.get("codigo")]
    if recep == "recibida":
        rows = [r for r in rows if r.get("recibida")]
    elif recep == "pendiente":
        rows = [r for r in rows if not r.get("recibida")]

    total = len(rows)
    con_codigo = sum(1 for r in rows if r.get("codigo"))
    con_match = sum(1 for r in rows if r.get("fuente"))
    sin_codigo = total - con_codigo
    recibidas = sum(1 for r in rows if r.get("recibida"))
    pendientes = total - recibidas
    importe_programa = sum(
        r["importe_programa"] for r in rows if r.get("importe_programa")
    )
    # Σ anticipos pagados = valor del stock de las importaciones (modelo v2).
    anticipos_total = sum(float(r.get("anticipo_aplicado") or 0) for r in rows)

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
                "anticipos": (
                    round(float(r.get("anticipo_aplicado") or 0), 2)
                    if r.get("anticipo_aplicado") else ""
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
                ("anticipos", "Anticipos (US) = valor stock"),
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
        anticipos_total=anticipos_total,
        q=q,
        estado=estado,
        recep=recep,
        hoy=today_ec().isoformat(),
        error=error,
    )


def _volver():
    """Vuelve a /importaciones preservando los filtros actuales."""
    args = {
        k: request.form.get(k)
        for k in ("q", "estado", "recep")
        if request.form.get(k)
    }
    return redirect(url_for("importaciones.lista", **args))


def _prov_num():
    prov = (request.form.get("prov") or "").strip().upper()
    numero = parse_int(request.form.get("numero"))
    return prov, numero


def _im():
    return (request.form.get("im_numero") or "").strip()


@importaciones_bp.route("/importaciones/recibir", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def recibir():
    """Recibe la importación: los kg entran al stock.

    Modelo v2 (TMT 2026-07-06): recibir NO genera deuda ni pide costo — el
    valor del stock de la importación es Σ anticipos y el restante se carga
    por /compras.
    """
    from modules.importaciones import pago as _pago

    prov, numero = _prov_num()
    im = _im()
    kg = parse_monto(request.form.get("kg"))
    if not im:
        flash("Importación inválida (falta el número IM-).", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        _pago.set_recepcion(im, prov, numero, kg=kg, usuario=usuario)
        flash(f"Importación {im} recibida: {kg or 0:,.0f} kg al stock.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude registrar la recepción", e)
    return _volver()


@importaciones_bp.route("/importaciones/deshacer-recepcion", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def deshacer_recepcion():
    """Revierte la recepción (vuelve a 'en tránsito', saca los kg del stock).
    Los anticipos (movimientos + ND) no se tocan — se deshacen con su ✕."""
    from modules.importaciones import pago as _pago

    prov, numero = _prov_num()
    im = _im()
    if not im:
        flash("Importación inválida (falta el número IM-).", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        _pago.deshacer_recepcion(im, prov, numero, usuario=usuario)
        flash(f"Recepción de {im} deshecha (vuelve a en tránsito).", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude deshacer la recepción", e)
    return _volver()


@importaciones_bp.route("/importaciones/movimiento", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def movimiento_agregar():
    """Registra un ANTICIPO como MOVIMIENTO (mig 0113).

    TMT 2026-07-06 (dueña): muchos anticipos por importación, nada se pisa;
    Σ anticipos = valor del stock. Cada anticipo genera AUTOMÁTICAMENTE su ND
    en Pichincha (la pantalla avisa para que no la carguen a mano otra vez).
    La UI solo carga anticipos — el restante va por /compras.
    """
    from modules.importaciones import pago as _pago

    prov, _numero = _prov_num()
    im = _im()
    monto = parse_monto(request.form.get("monto_mov"))
    fecha = parse_date(request.form.get("fecha_mov"))
    nota = (request.form.get("nota_mov") or "").strip()
    if not im:
        flash("Importación inválida (falta el número IM-).", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = _pago.agregar_movimiento(
            im, "anticipo", monto, fecha=fecha, nota=nota, prov=prov, usuario=usuario,
        )
        msg = f"Anticipo de $ {float(monto or 0):,.2f} registrado en {im}."
        if r.get("id_transaccion"):
            msg += (
                f" Se generó SOLA la ND #{r['id_transaccion']} en Pichincha — "
                "no la cargues a mano en el banco."
            )
        if r.get("anticipo_aplicado") is not None:
            msg += f" Σ anticipos (valor stock): $ {float(r['anticipo_aplicado']):,.2f}."
        flash(msg, "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude registrar el anticipo", e)
    return _volver()


@importaciones_bp.route("/importaciones/movimiento/deshacer", methods=["POST"])
@requiere_login
@requiere_permiso("compras.editar")
def movimiento_deshacer():
    """✕ de un movimiento: borra el anticipo Y compensa su ND con una NC
    en Pichincha (par atómico, mov_doble de auditoría)."""
    from modules.importaciones import pago as _pago

    id_mov = parse_int(request.form.get("id_mov"))
    if not id_mov:
        flash("Movimiento inválido.", "warn")
        return _volver()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = _pago.deshacer_movimiento(id_mov, usuario=usuario)
        msg = f"Anticipo de $ {float(r.get('monto') or 0):,.2f} borrado de {r.get('im_numero')}."
        if r.get("id_transaccion_reverso"):
            msg += (
                f" Su ND quedó compensada con la NC #{r['id_transaccion_reverso']} "
                "en Pichincha."
            )
        else:
            msg += " (Sin ND automática linkeada — si hiciste la ND a mano, resolvela en el banco.)"
        if r.get("anticipo_aplicado") is not None:
            msg += f" Σ anticipos ahora: $ {float(r['anticipo_aplicado']):,.2f}."
        flash(msg, "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude deshacer el movimiento", e)
    return _volver()
