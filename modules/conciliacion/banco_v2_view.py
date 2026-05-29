"""Endpoints v2 de conciliación bancaria — Reforma Sprint 1 (2026-05-28).

Pantalla post-procesar con 3 tabs (Manual, Impuestos, Transferencias),
balance Pichincha compact arriba sticky, sesión persistente y botón
Terminar y guardar abajo que genera el PDF de pendientes.

Coexiste con el viejo /conciliacion/hub mientras se valida. Una vez que
la dueña confirma, el alias /conciliacion/banco se reapunta a este flujo
y el viejo queda para borrar en sprint 2.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

from flask import (
    abort, flash, redirect, render_template, request, send_file, url_for,
)

import db as _db
from auth import requiere_login, requiere_permiso
from modules.conciliacion import sesion as _sesion
from modules.conciliacion import balance_pichincha as _bp
from modules.conciliacion.matcher_banco import (
    confirmar_match,
    crear_transaccion_agrupada_desde_reals,
)
from modules.conciliacion.parser_banco import parse_banco_xlsx
from modules.conciliacion.views import (
    conciliacion_bp,
    _usuario_actual,
    _BANCO_PICHINCHA,
)

_LOG = logging.getLogger("programa_core.conciliacion.banco_v2")

# Directorio para PDFs de sesiones cerradas. data/ está en el repo, en el
# server vive bajo C:\programa-core\data\.
_PDF_DIR = Path("data") / "conciliacion_pdfs"


def _migracion_lista_o_redirect():
    """Si la tabla banco_conciliacion_sesion no existe, mostrar flash claro
    y redirect en lugar de un 500 críptico.
    """
    if _sesion.tabla_existe():
        return None
    flash(
        "El flujo v2 necesita que se corra la migración 0060 en la DB. "
        "Avisame y la corro en CloudShell.",
        "warn",
    )
    return redirect(url_for("conciliacion.hub"))


# ─── Pantalla principal post-procesar ─────────────────────────────────


@conciliacion_bp.route("/banco-v2", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_post_procesar():
    """Renderiza la pantalla con los 3 tabs.

    Requiere una sesión abierta. Si no hay, redirect a /conciliacion/ para
    subir el extracto.
    """
    r = _migracion_lista_o_redirect()
    if r: return r
    usuario = _usuario_actual()
    no_banco = _BANCO_PICHINCHA

    sesion_id_qs = request.args.get("sesion_id")
    sesion = None
    if sesion_id_qs:
        try:
            sesion = _sesion.sesion_por_id(int(sesion_id_qs))
        except (ValueError, TypeError):
            sesion = None
    if not sesion:
        sesion = _sesion.sesion_abierta(no_banco, usuario)
    if not sesion:
        flash("Subí un extracto para empezar la conciliación.", "info")
        return redirect(url_for("conciliacion.hub"))

    if sesion.get("cerrada_en"):
        # Si llegan con ?sesion_id de una ya cerrada, mandalos al detail.
        return redirect(url_for("conciliacion.banco_cerrada", sesion_id=sesion["id"]))

    tab = (request.args.get("tab") or "manual").lower()
    if tab not in ("manual", "impuestos", "transferencias"):
        tab = "manual"

    buckets = _sesion.estado_sesion(sesion, no_banco)
    balance = _bp.calcular(no_banco)

    return render_template(
        "conciliacion/banco_v2.html",
        sesion=sesion,
        tab_activo=tab,
        buckets=buckets,
        balance=balance,
        saldo_pc_actual=balance,        # alias por si algún include lo busca
        banco_nombre="Pichincha",
        modo="compact",
    )


# ─── Endpoint: crear sesión a partir del upload ───────────────────────


@conciliacion_bp.route("/banco-v2/crear-sesion", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_crear_sesion():
    """Recibe el xlsx, parsea, abre sesión y redirect al post-procesar.

    Se usa en lugar de /conciliacion/hub POST para entrar al flujo v2.
    """
    r = _migracion_lista_o_redirect()
    if r: return r
    usuario = _usuario_actual()
    no_banco = _BANCO_PICHINCHA

    f = request.files.get("archivo")
    if not f or not f.filename:
        flash("Falta el archivo.", "error")
        return redirect(url_for("conciliacion.hub"))

    raw = f.read()
    if not raw:
        flash("El archivo vino vacío.", "error")
        return redirect(url_for("conciliacion.hub"))

    try:
        movs = parse_banco_xlsx(raw)
    except Exception as e:
        _LOG.exception("parser falló: %s", e)
        flash(f"No pude parsear el extracto: {e}", "error")
        return redirect(url_for("conciliacion.hub"))
    if not movs:
        flash("El extracto no trajo movimientos.", "warn")
        return redirect(url_for("conciliacion.hub"))

    sesion_id = _sesion.crear_sesion(
        no_banco=no_banco,
        usuario=usuario,
        movs=movs,
        extracto_hash=_sesion.sha256_bytes(raw),
        extracto_nombre=f.filename,
    )
    flash(
        f"Sesión #{sesion_id} abierta — {len(movs)} movimientos del extracto.",
        "ok",
    )
    return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))


# ─── Endpoint Tab Manual: confirmar pares marcados ────────────────────


@conciliacion_bp.route("/banco-v2/manual/confirmar", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_manual_confirmar():
    """Confirma N matches manuales. Por ahora hace N:N por suma — si los
    montos cuadran, mapea 1:1 ordenado por monto; sino acepta el primer
    bancsis como representativo y los demás como sus pares por orden.
    """
    sesion_id = int(request.form.get("sesion_id") or 0)
    sesion = _sesion.sesion_por_id(sesion_id) if sesion_id else None
    if not sesion or sesion.get("cerrada_en"):
        flash("Sesión inválida o cerrada.", "error")
        return redirect(url_for("conciliacion.hub"))

    real_ids_csv = (request.form.get("real_ids") or "").strip()
    hist_ids_csv = (request.form.get("hist_ids") or "").strip()
    bancsis_ids_csv = (request.form.get("bancsis_ids") or "").strip()
    try:
        real_idxs = [int(x) for x in real_ids_csv.split(",") if x.strip()]
        hist_ids = [int(x) for x in hist_ids_csv.split(",") if x.strip()]
        bancsis_ids = [int(x) for x in bancsis_ids_csv.split(",") if x.strip()]
    except ValueError:
        flash("IDs inválidos.", "error")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))

    if (not real_idxs and not hist_ids) or not bancsis_ids:
        flash("Marcá al menos un mov de cada lado.", "warn")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))

    movs = _sesion.cargar_movs(sesion)
    real_subset = [movs[i] for i in real_idxs if 0 <= i < len(movs)]

    n_matches = 0
    usuario = _usuario_actual()
    # 1) Matches del extracto contra el primer bancsis (o 1:1 si cuentan igual).
    if real_subset and bancsis_ids:
        if len(real_subset) == len(bancsis_ids):
            real_sorted = sorted(real_subset, key=lambda r: float(r.monto or 0), reverse=True)
            bk_sorted = sorted(bancsis_ids, reverse=True)
            for r, bk_id in zip(real_sorted, bk_sorted):
                try:
                    confirmar_match(_BANCO_PICHINCHA, r, bk_id, usuario=usuario, metodo="matched_manual")
                    n_matches += 1
                except Exception as e:
                    _LOG.warning("manual confirm falló: %s", e)
        else:
            bk_id_primary = bancsis_ids[0]
            for r in real_subset:
                try:
                    confirmar_match(_BANCO_PICHINCHA, r, bk_id_primary, usuario=usuario, metodo="matched_manual")
                    n_matches += 1
                except Exception as e:
                    _LOG.warning("manual confirm fallo: %s", e)

    # 2) Históricos seleccionados → marcar conciliados (apuntan al primer bancsis).
    n_hist = 0
    if hist_ids:
        try:
            bk_id_primary = bancsis_ids[0] if bancsis_ids else None
            n_hist = _db.execute(
                """
                UPDATE scintela.banco_historicos_pendientes
                   SET conciliado_en = CURRENT_TIMESTAMP,
                       conciliado_por = %s,
                       conciliado_match_id = %s
                 WHERE id = ANY(%s)
                   AND conciliado_en IS NULL
                """,
                (usuario[:50], bk_id_primary, hist_ids),
            ) or 0
        except Exception as e:
            _LOG.warning("conciliar históricos falló: %s", e)

    total = n_matches + n_hist
    _sesion.incrementar_matches(sesion_id, total)
    parts = []
    if n_matches: parts.append(f"{n_matches} match(es) del extracto")
    if n_hist: parts.append(f"{n_hist} histórico(s)")
    flash(" + ".join(parts) + " conciliado(s)." if parts else "Sin cambios.", "ok" if parts else "warn")
    return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id, tab="manual"))


# ─── Endpoint Tab Impuestos ───────────────────────────────────────────


@conciliacion_bp.route("/banco-v2/impuestos/confirmar", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_impuestos_confirmar():
    """Toma los reals de impuestos seleccionados → crea UNA tx BANCSIS
    agrupada y concilia los N reals contra ese mismo id_transaccion.
    """
    sesion_id = int(request.form.get("sesion_id") or 0)
    sesion = _sesion.sesion_por_id(sesion_id) if sesion_id else None
    if not sesion or sesion.get("cerrada_en"):
        flash("Sesión inválida o cerrada.", "error")
        return redirect(url_for("conciliacion.hub"))

    try:
        real_idxs = [int(x) for x in (request.form.get("real_idxs") or "").split(",") if x.strip()]
    except ValueError:
        real_idxs = []
    if not real_idxs:
        flash("No marcaste ningún movimiento.", "warn")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id, tab="impuestos"))

    movs = _sesion.cargar_movs(sesion)
    real_subset = [movs[i] for i in real_idxs if 0 <= i < len(movs)]
    if not real_subset:
        flash("Los movimientos seleccionados ya no existen en la sesión.", "error")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id, tab="impuestos"))

    fecha_str = (request.form.get("fecha") or "").strip()
    concepto = (request.form.get("concepto") or "").strip() or None
    prov = (request.form.get("prov") or "").strip() or None
    try:
        fecha = date.fromisoformat(fecha_str) if fecha_str else None
    except ValueError:
        fecha = None

    try:
        result = crear_transaccion_agrupada_desde_reals(
            no_banco=_BANCO_PICHINCHA,
            reals=real_subset,
            fecha=fecha,
            concepto=concepto,
            prov=prov,
            usuario=_usuario_actual(),
        )
        n_matches = int(result.get("n_matches") or len(real_subset))
        _sesion.incrementar_matches(sesion_id, n_matches)
        flash(
            f"Movimiento agrupado creado por ${result.get('monto_neto', 0):,.2f}. "
            f"{n_matches} match(es) conciliados.",
            "ok",
        )
    except Exception as e:
        _LOG.exception("impuestos confirmar falló: %s", e)
        flash(f"Error al crear el movimiento agrupado: {e}", "error")

    return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id, tab="impuestos"))


# ─── Endpoint Tab Transferencias ──────────────────────────────────────


@conciliacion_bp.route("/banco-v2/transferencias/confirmar", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_transferencias_confirmar():
    """Confirma N pares de la PASS 0. Cada par_idx referencia la posición
    en buckets['transferencias']. Re-corremos el matcher (idempotente) y
    aceptamos los pares marcados.
    """
    sesion_id = int(request.form.get("sesion_id") or 0)
    sesion = _sesion.sesion_por_id(sesion_id) if sesion_id else None
    if not sesion or sesion.get("cerrada_en"):
        flash("Sesión inválida o cerrada.", "error")
        return redirect(url_for("conciliacion.hub"))

    try:
        par_idxs = [int(x) for x in (request.form.get("par_idxs") or "").split(",") if x.strip()]
    except ValueError:
        par_idxs = []
    if not par_idxs:
        flash("No marcaste ningún par.", "warn")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id, tab="transferencias"))

    buckets = _sesion.estado_sesion(sesion, _BANCO_PICHINCHA)
    matches = buckets.get("transferencias") or []
    if not matches:
        flash("Los matches ya no están disponibles — algo cambió desde que cargaste la página.", "warn")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id, tab="transferencias"))

    n_done = 0
    usuario = _usuario_actual()
    for i in par_idxs:
        if i < 0 or i >= len(matches):
            continue
        m = matches[i]
        try:
            confirmar_match(
                no_banco=_BANCO_PICHINCHA,
                real=m.real,
                id_transaccion=m.bancsis.id_transaccion,
                estado="matched",
                usuario=usuario,
                metodo="matched_auto",
            )
            n_done += 1
        except Exception as e:
            _LOG.warning("transferencia confirm falló: %s", e)

    _sesion.incrementar_matches(sesion_id, n_done)
    flash(f"{n_done} transferencia(s) conciliada(s) por documento.", "ok")
    return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id, tab="transferencias"))


# ─── Terminar y guardar + PDF ─────────────────────────────────────────


def _generar_pdf_pendientes(sesion: dict, balance: dict) -> str | None:
    """Genera el PDF formato hoja FEB con los DEPÓSITOS PENDIENTES.

    Pendientes = movs del banco que quedaron sin conciliar al cerrar la
    sesión. Excluye impuestos/comisiones (no son "depósitos pendientes").
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )
    except ImportError:
        _LOG.warning("reportlab no instalado — saltando PDF")
        return None

    no_banco = sesion.get("no_banco") or _BANCO_PICHINCHA
    buckets = _sesion.estado_sesion(sesion, no_banco)
    items = buckets.get("manual_banco") or []

    _PDF_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = _PDF_DIR / f"sesion_{sesion['id']}.pdf"

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    elements = []
    title = Paragraph(
        f"<b>DEPÓSITOS PENDIENTES</b><br/>"
        f"<font size=9 color='#666'>Pichincha · Sesión #{sesion['id']} · "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}</font>",
        styles["Normal"],
    )
    elements.append(title)
    elements.append(Spacer(1, 0.2 * inch))

    rows = [["FECHA", "DETALLE", "CODIGO", "VALOR", "DETALLE"]]
    total = 0.0
    for it in items:
        m = it["mov"]
        if (m.tipo or "").upper() != "C":
            # El xlsx muestra solo entradas (depósitos pendientes). Open Q
            # #4 — default C; si la dueña pide ambos, sacar este filtro.
            continue
        valor = float(m.monto or 0)
        total += valor
        rows.append([
            m.fecha.strftime("%d/%m/%Y") if m.fecha else "",
            (m.concepto or "")[:60],
            (m.documento or "")[:15],
            f"{valor:,.2f}",
            "",
        ])
    rows.append(["", "", "", f"Total: {total:,.2f}", ""])

    tbl = Table(rows, colWidths=[0.9 * inch, 4 * inch, 1.1 * inch, 1 * inch, 0.9 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("BACKGROUND", (0, -1), (-1, -1), colors.lightgrey),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    elements.append(tbl)
    doc.build(elements)
    return str(pdf_path)


@conciliacion_bp.route("/banco-v2/terminar", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_terminar():
    """Cierra la sesión, genera el PDF, redirect al detail."""
    sesion_id = int(request.form.get("sesion_id") or 0)
    sesion = _sesion.sesion_por_id(sesion_id) if sesion_id else None
    if not sesion:
        flash("Sesión no encontrada.", "error")
        return redirect(url_for("conciliacion.hub"))
    if sesion.get("cerrada_en"):
        flash("La sesión ya estaba cerrada.", "info")
        return redirect(url_for("conciliacion.banco_cerrada", sesion_id=sesion_id))

    pdf_path = None
    balance = _bp.calcular(sesion.get("no_banco") or _BANCO_PICHINCHA)
    try:
        pdf_path = _generar_pdf_pendientes(sesion, balance)
    except Exception as e:
        _LOG.warning("PDF falló (sigo cerrando sesión sin PDF): %s", e)

    ok = _sesion.cerrar_sesion(sesion_id, _usuario_actual(), pdf_path=pdf_path)

    # Snapshot saldo al cierre (igual que /banco/deshacer).
    try:
        from modules.conciliacion import saldo_snapshot as _ss
        _ss.snapshot(
            _BANCO_PICHINCHA,
            "sesion_cerrada",
            evento_ref=sesion_id,
            usuario=_usuario_actual(),
            descripcion=f"cierre sesión #{sesion_id}",
        )
    except Exception:
        pass

    if ok:
        flash(f"Sesión #{sesion_id} cerrada. PDF guardado.", "ok")
    else:
        flash("No se pudo cerrar la sesión (¿ya estaba cerrada?).", "warn")
    return redirect(url_for("conciliacion.banco_cerrada", sesion_id=sesion_id))


@conciliacion_bp.route("/banco-v2/cerrada/<int:sesion_id>", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_cerrada(sesion_id: int):
    sesion = _sesion.sesion_por_id(sesion_id)
    if not sesion:
        abort(404)
    return render_template(
        "conciliacion/banco_v2_cerrada.html",
        sesion=sesion,
    )


@conciliacion_bp.route("/banco-v2/pdf/<int:sesion_id>", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_pdf(sesion_id: int):
    sesion = _sesion.sesion_por_id(sesion_id)
    if not sesion or not sesion.get("pdf_path"):
        abort(404)
    path = Path(sesion["pdf_path"])
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        abort(404)
    return send_file(
        str(path),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"conciliacion_sesion_{sesion_id}.pdf",
    )


# ─── Historial ────────────────────────────────────────────────────────


@conciliacion_bp.route("/banco-v2/historial", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_historial_v2():
    r = _migracion_lista_o_redirect()
    if r: return r
    sesiones = _sesion.listar_sesiones(no_banco=_BANCO_PICHINCHA, limit=200)
    return render_template(
        "conciliacion/banco_v2_historial.html",
        sesiones=sesiones,
    )
