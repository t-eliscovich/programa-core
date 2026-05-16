"""Listado y emisión de retenciones emitidas por clientes."""
import contextlib
from datetime import datetime

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
from parsers import parse_date, parse_int, parse_monto

from . import queries

retenciones_bp = Blueprint("retenciones", __name__, template_folder="templates")


@retenciones_bp.route("/retenciones/emitir", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("retenciones.emitir")
def emitir():
    """Emitir retención en la fuente contra una factura.

    GET con `?codigo_cli=X`: muestra facturas del cliente sin retención.
    POST: registra la retención (codigo_cli + numf + rete + fecha).
    """
    errores: list[str] = []
    form: dict = {}
    facturas_libres: list[dict] = []

    codigo_cli_preview = (request.args.get("codigo_cli") or "").strip().upper()

    if request.method == "GET":
        form["fecha"] = datetime.now().date().isoformat()
        form["codigo_cli"] = codigo_cli_preview
        if codigo_cli_preview:
            try:
                facturas_libres = queries.facturas_sin_retencion(codigo_cli_preview)
            except Exception:
                facturas_libres = []
        return render_template(
            "retenciones/emitir.html",
            form=form, errores=errores, facturas_libres=facturas_libres,
        )

    codigo_cli = (request.form.get("codigo_cli") or "").strip().upper()
    numf = parse_int(request.form.get("numf"))
    rete = parse_monto(request.form.get("rete"))
    fecha = parse_date(request.form.get("fecha"))

    if not codigo_cli:
        errores.append("Código de cliente requerido.")
    if numf is None or numf <= 0:
        errores.append("N° de factura requerido.")
    if rete is None or rete <= 0:
        errores.append("Valor retenido debe ser mayor que cero.")

    form.update({
        "codigo_cli": codigo_cli,
        "numf": request.form.get("numf"),
        "rete": request.form.get("rete"),
        "fecha": request.form.get("fecha"),
    })

    if errores:
        if codigo_cli:
            with contextlib.suppress(Exception):
                facturas_libres = queries.facturas_sin_retencion(codigo_cli)
        return render_template(
            "retenciones/emitir.html",
            form=form, errores=errores, facturas_libres=facturas_libres,
        ), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.emitir(
            codigo_cli=codigo_cli, numf=numf, rete=rete, fecha=fecha, usuario=usuario,
        )
        flash(
            f"Retención emitida (id {r.get('id_retencion')}) para factura {numf}.",
            "ok",
        )
        return redirect(url_for("retenciones.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template(
            "retenciones/emitir.html",
            form=form, errores=errores, facturas_libres=facturas_libres,
        ), 400
    except Exception as e:
        errores.append(f"No pude emitir la retención: {e}")
        return render_template(
            "retenciones/emitir.html",
            form=form, errores=errores, facturas_libres=facturas_libres,
        ), 500


@retenciones_bp.route("/retenciones/<int:id_retencion>/confirmar-anulacion", methods=["GET"])
@requiere_login
@requiere_permiso("retenciones.anular")
def confirmar_anulacion(id_retencion: int):
    """Paso 1 del 2-step: resumen + motivo antes de anular la retención.

    Aprovecha la pasada por acá (es el único "detail view" de retención)
    para registrarla en recientes.
    """
    r = queries.por_id(id_retencion)
    if not r:
        abort(404)
    # Recientes — best-effort, no rompe la pantalla si falla.
    try:
        from modules.recientes import queries as rec
        etiqueta = (
            f"Retención #{id_retencion} · {r.get('codigo_cli') or ''} · "
            f"{r.get('numf_completo') or r.get('numf') or ''}"
        )[:200]
        rec.registrar("retencion", id_retencion, etiqueta=etiqueta)
    except Exception:
        pass
    detalle = {
        "N° retención": r.get("id_retencion"),
        "Factura": r.get("numf_completo") or r.get("numf"),
        "Cliente": f"{r.get('codigo_cli')} — {r.get('cliente') or ''}",
        "Valor retenido": f"$ {r.get('rete') or 0}",
        "Fecha": (r.get("fecha").strftime("%d/%m/%Y") if r.get("fecha") else "—"),
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Anular retención #{id_retencion}",
        mensaje=(
            f"Vas a anular la retención #{id_retencion} "
            f"del cliente {r.get('codigo_cli')} por $ {r.get('rete') or 0}."
        ),
        detalle_registro=detalle,
        accion_url=url_for("retenciones.anular", id_retencion=id_retencion),
        volver_url=url_for("retenciones.lista"),
        motivo_requerido=True,
        confirm_label="Confirmar anulación",
    )


@retenciones_bp.route("/retenciones/<int:id_retencion>/anular", methods=["POST"])
@requiere_login
@requiere_permiso("retenciones.anular")
def anular(id_retencion: int):
    (request.form.get("motivo") or "").strip()  # opcional. TMT 2026-05-13.
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.anular(id_retencion, usuario=usuario)
        flash("Retención anulada.", "ok")
    except Exception as e:
        flash_exc("No pude anular", e)
    return redirect(url_for("retenciones.lista"))


@retenciones_bp.route("/retenciones")
@requiere_login
@requiere_permiso("retenciones.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    try:
        filas = queries.buscar(q, desde, hasta)
        error = None
    except Exception as e:
        filas, error = [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"),
                ("codigo_cli", "Cliente"),
                ("cliente", "Nombre"),
                ("numf", "N° fact"),
                ("numf_completo", "Factura SRI"),
                ("importe_fact", "Importe fact."),
                ("rete", "Retenido"),
                ("pct", "% ret"),
            ],
            filename="retenciones.csv",
        )

    total = sum(float(r["rete"] or 0) for r in filas)
    return render_template(
        "retenciones/lista.html",
        filas=filas, q=q, desde=desde, hasta=hasta,
        total=total, error=error,
    )
