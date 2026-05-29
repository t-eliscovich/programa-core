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
from decimal import Decimal as _D
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

# Directorio para reportes de sesiones cerradas. data/ está en el repo,
# en el server vive bajo C:\programa-core\data\.
_PDF_DIR = Path("data") / "conciliacion_pdfs"  # legacy nombre, ahora xlsx


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

    # CRITICAL FIX 2026-05-29: el `idx` del bucket indexa en res.real_only
    # del matcher (movs filtrados, sin matches activos), NO en la lista
    # cruda del extracto. Re-correr el matcher y usar real_only[i] para
    # evitar agarrar un mov equivocado (= bug del 67K).
    from modules.conciliacion.matcher_banco import matchear_extracto_banco
    movs = _sesion.cargar_movs(sesion)
    try:
        res = matchear_extracto_banco(movs, no_banco=_BANCO_PICHINCHA)
        real_only = res.real_only or []
    except Exception as e:
        _LOG.exception("re-match para confirmar manual falló: %s", e)
        real_only = []
    real_subset = [real_only[i] for i in real_idxs if 0 <= i < len(real_only)]

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

    # CRITICAL FIX 2026-05-29: re-correr el matcher para que los idxs
    # apunten a res.real_only (filtrado), NO a la lista cruda del extracto.
    # Antes movs[i] devolvía un mov COMPLETAMENTE DISTINTO (depósitos por
    # $15K) en lugar del impuesto de \$0.05 → suma 67K en lugar de 14.
    from modules.conciliacion.matcher_banco import matchear_extracto_banco
    movs = _sesion.cargar_movs(sesion)
    try:
        res = matchear_extracto_banco(movs, no_banco=_BANCO_PICHINCHA)
        real_only = res.real_only or []
    except Exception as e:
        _LOG.exception("re-match para confirmar impuestos falló: %s", e)
        real_only = []
    real_subset = [real_only[i] for i in real_idxs if 0 <= i < len(real_only)]
    if not real_subset:
        flash("Los movimientos seleccionados ya no existen en la sesión.", "error")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id, tab="impuestos"))

    # Sanity check: si la suma supera $1000 lo más probable es que la
    # dueña seleccionó cosas grandes por error (default checked roto).
    # Pedimos confirmación adicional en backend levantando warning.
    total_signed = sum(float(r.monto) for r in real_subset if (r.tipo or '').upper()=='C') \
                 - sum(float(r.monto) for r in real_subset if (r.tipo or '').upper()=='D')
    if abs(total_signed) > 1000:
        _LOG.warning("Impuestos confirmar con neto inusual: %.2f n=%d", total_signed, len(real_subset))

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


def _generar_xlsx_pendientes(sesion: dict, balance: dict) -> str | None:
    """Genera un XLSX formato hoja FEB con los DEPÓSITOS PENDIENTES.

    TMT 2026-05-29 dueña pasó el formato esperado: lista completa de
    banco_historicos_pendientes (todos los movs del banco que NO se
    pudieron conciliar contra el programa), ordenados por fecha asc,
    con columnas FECHA / DETALLE / CODIGO / VALOR / DETALLE-extra.

    Antes el XLSX traía solo los movs del extracto de la sesión actual
    (real_only del bucket Manual) — eso era el subconjunto chico de
    los pendientes del DÍA, no el listado completo del backlog histórico.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        _LOG.warning("openpyxl no instalado — saltando export")
        return None

    no_banco = sesion.get("no_banco") or _BANCO_PICHINCHA

    # Listado completo de pendientes del banco. ORDER BY fecha ASC para
    # que arranquen los más viejos primero (igual al formato Tamara
    # subió: marzo → abril → mayo).
    rows = []
    try:
        rows = _db.fetch_all(
            """
            SELECT fecha, concepto, documento, monto, oficina, detalle, tipo
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
             ORDER BY fecha ASC, id ASC
            """,
            (no_banco,),
        ) or []
    except Exception as e:
        _LOG.warning("xlsx historicos query falló: %s", e)

    _PDF_DIR.mkdir(parents=True, exist_ok=True)
    xlsx_path = _PDF_DIR / f"sesion_{sesion['id']}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "DEPÓSITOS PENDIENTES"

    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDDDDD")

    ws["A1"] = "DEPÓSITOS PENDIENTES"
    ws["A1"].font = Font(bold=True, size=14)
    ws.merge_cells("A1:E1")
    ws["A2"] = (
        f"Pichincha · Sesión #{sesion['id']} · "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"{len(rows)} movs"
    )
    ws.merge_cells("A2:E2")

    headers = ["FECHA", "DETALLE", "CODIGO", "VALOR", "DETALLE"]
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=4, column=col, value=h)
        c.font = bold
        c.fill = header_fill

    r = 5
    total = 0.0
    for row in rows:
        # Solo tipo C (entradas/depósitos pendientes). Si en el futuro
        # la dueña quiere las salidas también, sacar este filtro.
        if (row.get("tipo") or "C").upper() != "C":
            continue
        valor = float(row.get("monto") or 0)
        total += valor
        fecha = row.get("fecha")
        ws.cell(row=r, column=1, value=fecha.strftime("%d/%m/%Y") if fecha else "")
        ws.cell(row=r, column=2, value=(row.get("concepto") or "")[:100])
        ws.cell(row=r, column=3, value=(row.get("documento") or "")[:30])
        ws.cell(row=r, column=4, value=valor).number_format = "#,##0.00"
        ws.cell(row=r, column=5, value=(row.get("detalle") or row.get("oficina") or "")[:30])
        r += 1

    # ── Resumen contable al pie ───────────────────────────────────────
    # TMT 2026-05-29 dueña: bloque con 5 líneas que la dueña usa cuando
    # cierra. Formato (con paréntesis para negativos = convención contable):
    #   TOTAL         (16,832.20)   ← ajuste = -pendientes_conciliar_neto
    #   SALDO SISTEMA  2,359,683.11
    #   TOTAL          2,342,850.91 ← saldo_conciliado = SISTEMA + ajuste
    #   SALDO BANCO    2,342,707.24 ← último saldo del extracto subido
    #   DIFERENCIA       (143.67)   ← SALDO BANCO − TOTAL conciliado
    saldo_sistema = float(balance.get("saldo") or 0)
    total_ajuste = -float(balance.get("pendientes_conciliar_neto") or 0)
    total_conciliado = float(balance.get("saldo_si_concilio_todo") or 0)

    # Saldo banco "real" = último saldo del extracto subido (en el último
    # mov por fecha; si hay empate de fecha tomamos el último del archivo).
    saldo_banco_real = None
    try:
        movs_sesion = _sesion.cargar_movs(sesion)
        if movs_sesion:
            # Filtramos los que tienen fecha + saldo; tomamos el de fecha
            # más alta. Si no, dejamos el último por orden de aparición.
            con_fecha = [m for m in movs_sesion if getattr(m, "fecha", None) and getattr(m, "saldo", None) is not None]
            if con_fecha:
                ult = max(con_fecha, key=lambda m: m.fecha)
                saldo_banco_real = float(ult.saldo)
            else:
                ult = movs_sesion[-1]
                saldo_banco_real = float(getattr(ult, "saldo", None) or 0) or None
    except Exception as e:
        _LOG.warning("no pude leer último saldo banco del extracto: %s", e)

    diferencia = (saldo_banco_real - total_conciliado) if saldo_banco_real is not None else None

    contable_fmt = '#,##0.00;(#,##0.00)'  # paréntesis para negativos
    label_col = 3  # columna C, igual al header "CODIGO" pero usamos como label
    val_col = 4   # columna D, igual al header "VALOR"

    r += 1  # fila vacía de separación
    for label, val in [
        ("TOTAL", total_ajuste),
        ("SALDO SISTEMA", saldo_sistema),
        ("TOTAL", total_conciliado),
        ("SALDO BANCO", saldo_banco_real),
        ("DIFERENCIA", diferencia),
    ]:
        ws.cell(row=r, column=label_col, value=label).font = bold
        if val is not None:
            cell = ws.cell(row=r, column=val_col, value=val)
            cell.font = bold
            cell.number_format = contable_fmt
        else:
            ws.cell(row=r, column=val_col, value="—").font = bold
        r += 1

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 60
    ws.column_dimensions["C"].width = 15
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 12

    wb.save(str(xlsx_path))
    return str(xlsx_path)


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
        pdf_path = _generar_xlsx_pendientes(sesion, balance)
    except Exception as e:
        _LOG.warning("XLSX falló (sigo cerrando sesión sin reporte): %s", e)

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
    """Descarga el reporte de pendientes (XLSX, antes PDF). El endpoint
    se llama 'pdf' por compat hacia atrás — el contenido es xlsx ahora.
    """
    sesion = _sesion.sesion_por_id(sesion_id)
    if not sesion or not sesion.get("pdf_path"):
        abort(404)
    path = Path(sesion["pdf_path"])
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        abort(404)
    is_xlsx = str(path).lower().endswith(".xlsx")
    return send_file(
        str(path),
        mimetype=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                  if is_xlsx else "application/pdf"),
        as_attachment=True,
        download_name=f"conciliacion_sesion_{sesion_id}.{'xlsx' if is_xlsx else 'pdf'}",
    )


# ─── Historial ────────────────────────────────────────────────────────


@conciliacion_bp.route("/banco-v2/historial", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_historial_v2():
    r = _migracion_lista_o_redirect()
    if r: return r
    sesiones = _sesion.listar_sesiones(no_banco=_BANCO_PICHINCHA, limit=200)
    # Balance inicial/final por sesión (snapshots evento_tipo+evento_ref).
    saldo_ini, saldo_fin = {}, {}
    try:
        snap_rows = _db.fetch_all(
            """
            SELECT evento_tipo, evento_ref, saldo_conc
              FROM scintela.banco_saldo_conc_snapshot
             WHERE no_banco = %s
               AND evento_tipo IN ('sesion_abierta','sesion_cerrada')
            """,
            (_BANCO_PICHINCHA,),
        ) or []
        for sr in snap_rows:
            ref = str(sr.get("evento_ref") or "")
            val = float(sr.get("saldo_conc") or 0)
            if sr.get("evento_tipo") == "sesion_abierta":
                saldo_ini[ref] = val
            elif sr.get("evento_tipo") == "sesion_cerrada":
                saldo_fin[ref] = val
    except Exception:
        pass

    # Fallback: para sesiones viejas (anteriores al feature de snapshot
    # 'sesion_abierta'), buscar el snapshot más cercano por timestamp.
    # TMT 2026-05-29 dueña: 'ponele saldo final y saldo inicial' incluso
    # a sesiones pasadas. Aproximación: último snapshot ANTES de abierta_en
    # = saldo inicial; último snapshot ANTES (o IGUAL a) cerrada_en = saldo final.
    for s in sesiones:
        sid = str(s.get("id"))
        if sid not in saldo_ini and s.get("abierta_en"):
            try:
                row = _db.fetch_one(
                    """
                    SELECT saldo_conc FROM scintela.banco_saldo_conc_snapshot
                     WHERE no_banco = %s AND creado_en <= %s
                     ORDER BY creado_en DESC LIMIT 1
                    """,
                    (_BANCO_PICHINCHA, s["abierta_en"]),
                )
                if row and row.get("saldo_conc") is not None:
                    saldo_ini[sid] = float(row["saldo_conc"])
            except Exception:
                pass
        if sid not in saldo_fin and s.get("cerrada_en"):
            try:
                row = _db.fetch_one(
                    """
                    SELECT saldo_conc FROM scintela.banco_saldo_conc_snapshot
                     WHERE no_banco = %s AND creado_en <= %s
                     ORDER BY creado_en DESC LIMIT 1
                    """,
                    (_BANCO_PICHINCHA, s["cerrada_en"]),
                )
                if row and row.get("saldo_conc") is not None:
                    saldo_fin[sid] = float(row["saldo_conc"])
            except Exception:
                pass
        s["saldo_inicial"] = saldo_ini.get(sid)
        s["saldo_final"] = saldo_fin.get(sid)
    return render_template(
        "conciliacion/banco_v2_historial.html",
        sesiones=sesiones,
    )


# ─── Deshacer conciliados (pantalla minimalista) ──────────────────────


@conciliacion_bp.route("/banco-v2/deshacer", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_deshacer_v2():
    """Lista de matches activos + movs BANCSIS creados por conciliación.

    TMT 2026-05-29 dueña: 'no veo las dos entradas en 67k que quiero
    deshacer'. Las txs creadas por crear_transaccion_agrupada_desde_reals
    (impuestos/comisiones) son su propio objeto en transacciones_bancarias
    — anular el match no las borra. Pantalla muestra ambos:

      - Matches individuales (real_only conciliados contra un id_tx)
      - Grupos BANCSIS creados por la propia conciliación (las ND/NC
        del flujo de Impuestos): para borrarlos completamente.
    """
    matches = []
    grupos = []
    try:
        matches = _db.fetch_all(
            """
            SELECT m.id, m.creado_en, m.real_fecha, m.real_documento,
                   m.real_concepto, m.real_monto, m.real_tipo,
                   m.usuario, m.id_transaccion, m.metodo
              FROM scintela.banco_conciliacion_match m
             WHERE m.no_banco = %s
               AND m.deshecho_en IS NULL
             ORDER BY m.creado_en DESC
             LIMIT 500
            """,
            (_BANCO_PICHINCHA,),
        ) or []
    except Exception as e:
        _LOG.warning("listar matches activos falló: %s", e)

    try:
        grupos = _db.fetch_all(
            """
            SELECT t.id_transaccion, t.fecha, t.documento, t.importe,
                   t.concepto, t.saldo, t.usuario_crea,
                   COUNT(m.id) AS n_matches_total,
                   COUNT(m.id) FILTER (WHERE m.deshecho_en IS NULL) AS n_matches_activos
              FROM scintela.transacciones_bancarias t
              JOIN scintela.banco_conciliacion_match m ON m.id_transaccion = t.id_transaccion
             WHERE t.no_banco = %s
               AND m.metodo IN ('created_from_real_grouped','created_from_real')
             GROUP BY t.id_transaccion, t.fecha, t.documento, t.importe,
                      t.concepto, t.saldo, t.usuario_crea
            HAVING COUNT(m.id) FILTER (WHERE m.deshecho_en IS NULL) > 0
             ORDER BY t.fecha DESC, t.id_transaccion DESC
             LIMIT 200
            """,
            (_BANCO_PICHINCHA,),
        ) or []
    except Exception as e:
        _LOG.warning("listar grupos BANCSIS falló: %s", e)

    return render_template(
        "conciliacion/banco_v2_deshacer.html",
        matches=matches,
        grupos=grupos,
    )


@conciliacion_bp.route("/banco-v2/anular-grupo", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_anular_grupo():
    """Anula UNA tx BANCSIS creada por conciliación (impuestos / agrupado)
    + todos sus matches activos + revierte stat + recompute saldos.

    TMT 2026-05-29 dueña: 'si creamos por impuestos, si deshaces el
    movimiento también tiene que anular esa carga'.
    """
    try:
        id_tx = int(request.form.get("id_transaccion") or 0)
    except (TypeError, ValueError):
        id_tx = 0
    if id_tx <= 0:
        flash("ID de transacción inválido.", "error")
        return redirect(url_for("conciliacion.banco_deshacer_v2"))

    usuario = _usuario_actual()
    # 1) Validar que es realmente una tx de conciliación (no PC normal).
    tx_row = _db.fetch_one(
        """
        SELECT t.id_transaccion, t.fecha, t.documento, t.importe, t.concepto,
               (SELECT MIN(m.metodo)
                  FROM scintela.banco_conciliacion_match m
                 WHERE m.id_transaccion = t.id_transaccion) AS metodo
          FROM scintela.transacciones_bancarias t
         WHERE t.id_transaccion = %s AND t.no_banco = %s
        """,
        (id_tx, _BANCO_PICHINCHA),
    )
    if not tx_row:
        flash("No se encontró la transacción.", "error")
        return redirect(url_for("conciliacion.banco_deshacer_v2"))
    if (tx_row.get("metodo") or "") not in ("created_from_real", "created_from_real_grouped"):
        flash("Esta tx NO fue creada por conciliación — no la puedo anular desde acá.", "warn")
        return redirect(url_for("conciliacion.banco_deshacer_v2"))

    # 2+3) Hard-delete de matches + DELETE de tx + recompute, todo en una
    # sola transacción atómica. TMT 2026-05-29 dueña: 'sigo viendo los dos
    # de 67k' tras anular. Causa probable: la FK de banco_conciliacion_match
    # bloqueaba el DELETE de la fila tx. Soft-undo no es suficiente — al
    # quedar matches deshechos con id_transaccion FK válido, el DELETE de
    # la tx aborta por integrity. Hard-deleteamos los matches relacionados
    # antes del DELETE de la tx, en la misma db.tx().
    import bank_helpers
    try:
        with _db.tx() as conn:
            n_matches = _db.execute(
                """
                DELETE FROM scintela.banco_conciliacion_match
                 WHERE id_transaccion = %s
                """,
                (id_tx,),
                conn=conn,
            ) or 0
            n_del = _db.execute(
                "DELETE FROM scintela.transacciones_bancarias WHERE id_transaccion = %s AND no_banco = %s",
                (id_tx, _BANCO_PICHINCHA),
                conn=conn,
            ) or 0
            # Verificación post-delete dentro de la misma tx.
            still = _db.fetch_one(
                "SELECT 1 FROM scintela.transacciones_bancarias WHERE id_transaccion = %s",
                (id_tx,),
                conn=conn,
            )
            if still:
                raise RuntimeError(f"DELETE no efectivo — tx {id_tx} aún existe")

            # Walk-forward desde la siguiente fila.
            siguiente = _db.fetch_one(
                """
                SELECT id_transaccion FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s
                   AND (fecha > %s OR (fecha = %s AND id_transaccion > %s))
                 ORDER BY fecha ASC, id_transaccion ASC LIMIT 1
                """,
                (_BANCO_PICHINCHA, tx_row["fecha"], tx_row["fecha"], id_tx),
                conn=conn,
            )
            if siguiente and siguiente.get("id_transaccion"):
                try:
                    bank_helpers.recompute_saldos_desde(
                        conn,
                        no_banco=_BANCO_PICHINCHA,
                        no_cta=None,
                        ancla_id=int(siguiente["id_transaccion"]),
                    )
                except Exception as e:
                    _LOG.warning("recompute_saldos falló (sigue): %s", e)
            _LOG.info("anular_grupo tx=%s OK: %s matches borrados, %s tx borrada",
                      id_tx, n_matches, n_del)
    except Exception as e:
        _LOG.exception("anular tx %s falló: %s", id_tx, e)
        flash(f"Error al anular: {e}", "error")
        return redirect(url_for("conciliacion.banco_deshacer_v2"))

    # 4) Decrementar el contador matches_hechos de la sesión activa
    # (si existe) — el counter es running total y se descontrolaba al
    # anular. Lo bajamos por el número de matches que efectivamente
    # se borraron en el grupo.
    if n_matches > 0:
        try:
            _db.execute(
                """
                UPDATE scintela.banco_conciliacion_sesion
                   SET matches_hechos = GREATEST(0, matches_hechos - %s)
                 WHERE no_banco = %s
                   AND usuario = %s
                   AND cerrada_en IS NULL
                """,
                (int(n_matches), _BANCO_PICHINCHA, usuario[:50]),
            )
        except Exception:
            pass

    # 5) Snapshot del nuevo saldo a conciliar.
    try:
        from modules.conciliacion import saldo_snapshot as _ss
        _ss.snapshot(
            _BANCO_PICHINCHA, "grupo_anulado",
            evento_ref=str(id_tx), usuario=usuario,
            descripcion=f"anulada tx agrupada #{id_tx} ({n_matches} matches deshechos)",
        )
    except Exception:
        pass

    flash(
        f"Movimiento agrupado #{id_tx} anulado: tx borrada, "
        f"{n_matches} match(es) deshechos, saldos recalculados.",
        "ok",
    )
    return redirect(url_for("conciliacion.banco_deshacer_v2"))
