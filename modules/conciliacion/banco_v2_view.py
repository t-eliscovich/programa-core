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
from datetime import date, datetime, timedelta, timezone


def _hora_ec_str(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Convertir datetime UTC → str en hora Ecuador (UTC-5)."""
    if value is None:
        return ""
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone(timedelta(hours=-5))).strftime(fmt)
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

# ── Defensas contra corrupción de saldo running ──────────────────────
# TMT 2026-05-29: el "BUG #2" reportado durante el E2E (saldo bajó $43K
# al anular tx de $29) en realidad reveló una cadena previamente corrupta.
# Ahora validamos pre/post cada mutación destructiva. Si detectamos
# descalce, log CRITICAL + opcionalmente recompute desde el inicio.

_SIGNOS_C = ("DE", "TR", "AC", "NC", "IN", "XX")
_SIGNOS_D = ("CH", "ND", "DB", "GS", "PA")


def _signed_delta(documento: str, importe: float) -> float:
    doc = (documento or "").upper()
    if doc in _SIGNOS_C:
        return importe
    if doc in _SIGNOS_D:
        return -importe
    return 0.0


def _verificar_cadena_saldos(no_banco: int, limit: int = 30, conn=None) -> dict:
    """Recorre las últimas N filas y valida saldo = saldo_prev + signed_delta.

    Returns:
        {ok: bool, ultimo_saldo: float, problemas: [...], n_chequeadas: int}
    """
    try:
        rows = _db.fetch_all(
            """
            SELECT id_transaccion, fecha, documento, importe, saldo
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s AND saldo IS NOT NULL
             ORDER BY fecha DESC, id_transaccion DESC LIMIT %s
            """,
            (int(no_banco), int(limit)),
            conn=conn,
        ) or []
    except Exception as e:
        return {"ok": False, "ultimo_saldo": None, "problemas": [{"error": str(e)}], "n_chequeadas": 0}

    rows = list(reversed(rows))  # ASC
    if not rows:
        return {"ok": True, "ultimo_saldo": 0.0, "problemas": [], "n_chequeadas": 0}

    problemas = []
    saldo_prev = None
    for r in rows:
        s = float(r["saldo"] or 0)
        delta = _signed_delta(r.get("documento"), float(r.get("importe") or 0))
        if saldo_prev is not None:
            esperado = round(saldo_prev + delta, 2)
            diff = round(s - esperado, 2)
            if abs(diff) > 0.01:
                problemas.append({
                    "id": r["id_transaccion"], "diff": diff,
                    "esperado": esperado, "grabado": s,
                })
        saldo_prev = s

    return {
        "ok": not problemas,
        "ultimo_saldo": saldo_prev,
        "problemas": problemas[:5],
        "n_chequeadas": len(rows),
    }


def _validar_y_loguear(no_banco: int, contexto: str, delta_esperado: float | None = None) -> bool:
    """Verifica cadena + loguea CRITICAL si está corrupta. Devuelve True si OK."""
    r = _verificar_cadena_saldos(no_banco)
    if not r["ok"]:
        _LOG.critical(
            "CADENA SALDOS CORRUPTA tras %s — banco=%s, problemas=%s",
            contexto, no_banco, r["problemas"],
        )
        return False
    if delta_esperado is not None:
        # Nada que validar acá sin saldo_anterior — se hace en el caller.
        pass
    return True

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
    """Renderiza la pantalla con los 4 tabs.

    TMT 2026-06-02 dueña: sesión nunca se cierra → siempre operamos contra
    la única sesión abierta del banco. Si no existe ninguna, redirect al
    landing para subir el primer extracto (o usar "Conciliar pendientes").
    """
    r = _migracion_lista_o_redirect()
    if r: return r
    no_banco = _BANCO_PICHINCHA

    sesion = _sesion.sesion_abierta(no_banco)
    if not sesion:
        flash("Subí un extracto para empezar la conciliación.", "info")
        return redirect(url_for("conciliacion.hub"))

    # TMT 2026-06-03: el contador sesion.matches_hechos venía desincronizado
    # con la realidad (decía 14 con 0 matches reales). Lo recomputamos live
    # desde banco_conciliacion_match. Single source of truth.
    try:
        _real_matches = _db.fetch_one(
            """
            SELECT COUNT(*) AS n FROM scintela.banco_conciliacion_match
             WHERE no_banco = %s AND deshecho_en IS NULL
            """,
            (no_banco,),
        ) or {}
        sesion = dict(sesion)
        sesion["matches_hechos"] = int(_real_matches.get("n") or 0)
    except Exception:
        pass

    tab = (request.args.get("tab") or "manual").lower()
    if tab not in ("manual", "impuestos", "transferencias", "conciliados"):
        tab = "manual"

    buckets = _sesion.estado_sesion(sesion, no_banco)
    balance = _bp.calcular(no_banco)
    # TMT 2026-06-02 dueña: 'si en el extracto tenemos 5k de diferencia'.
    # TMT 2026-06-03: balance_pichincha.calcular() ya incluye el extracto de
    # sesión en neto_pendientes_total. No re-enriquecemos acá — un solo lugar
    # para la math. La dueña pidió "totales que cierren de ambos lados".

    # TMT 2026-06-02 dueña: 'lo deberia implementar el usuario no? porque
    # por el excel no sabemos cual es el ultimo valor'. Prioridad:
    #   1. sesion.saldo_banco_objetivo (manual, escrito por la dueña).
    #   2. Auto-detect = max(fecha).saldo del payload (fallback frágil).
    saldo_manual = sesion.get("saldo_banco_objetivo")
    if saldo_manual is not None:
        try:
            balance["saldo_banco_real"] = float(saldo_manual)
            balance["saldo_banco_real_origen"] = "manual"
        except (TypeError, ValueError):
            balance["saldo_banco_real"] = None
    else:
        try:
            movs_s = _sesion.cargar_movs(sesion)
            con_fecha = [
                m for m in movs_s
                if getattr(m, "fecha", None) and getattr(m, "saldo", None) is not None
            ]
            if con_fecha:
                ult = max(con_fecha, key=lambda m: m.fecha)
                balance["saldo_banco_real"] = float(ult.saldo)
            elif movs_s:
                v = float(getattr(movs_s[-1], "saldo", None) or 0)
                balance["saldo_banco_real"] = v if v else None
            balance["saldo_banco_real_origen"] = "auto"
        except Exception:
            pass

    # Diferencia esperado vs real (si tenemos ambos).
    if balance.get("saldo_banco_real") is not None and balance.get("saldo_banco_esperado") is not None:
        balance["diferencia_no_clasificada"] = round(
            balance["saldo_banco_real"] - balance["saldo_banco_esperado"], 2
        )

    # TMT 2026-05-29 dueña: 'Hacer un cuarto tab que muestre conciliaciones
    # hasta ahora'. Lista los matches confirmados en esta sesión.
    matches_sesion = _sesion.matches_de_sesion(sesion)

    # TMT 2026-06-02 dueña: 'en esta tabla de conciliados, me tiene que
    # aparecer monto banco, monto programa, y tengo que ver que los totales
    # coincidan'. Enriquezco cada match con monto_prog_signed (el lado PC
    # con signo derivado del documento) y totalizo por lado.
    for m in matches_sesion:
        tipo = (m.get("real_tipo") or "").upper()
        # ¿Tiene lado banco? Sí si vino con real_monto (banco_only y matched).
        # Bancsis_only_ok no tiene lado banco.
        m["has_banco"] = m.get("real_monto") is not None
        try:
            monto_banco = float(m.get("real_monto") or 0)
        except (TypeError, ValueError):
            monto_banco = 0.0
        m["monto_banco_signed"] = monto_banco if tipo == "C" else (-monto_banco if tipo == "D" else 0.0)

        # ¿Tiene lado programa? Sí si el JOIN con transacciones_bancarias
        # devolvió una fila (tb.importe IS NOT NULL). Históricos pueden no
        # tenerlo si la dueña los marcó conciliados sin linkear a una tx PC.
        m["has_programa"] = m.get("tb_importe") is not None
        tb_doc = (m.get("tb_documento") or "").upper()
        try:
            tb_imp = float(m.get("tb_importe") or 0)
        except (TypeError, ValueError):
            tb_imp = 0.0
        # tb.importe en transacciones_bancarias es positivo; el signo lo da
        # documento. Importes ya signados (legacy) — si vienen negativos
        # los respetamos.
        if tb_imp < 0:
            m["monto_prog_signed"] = tb_imp
        else:
            m["monto_prog_signed"] = _signed_delta(tb_doc, tb_imp)
        # Diferencia por fila (banco − programa) — solo si AMBOS lados existen.
        if m["has_banco"] and m["has_programa"]:
            m["diff"] = round(m["monto_banco_signed"] - m["monto_prog_signed"], 2)
        else:
            m["diff"] = None  # un lado falta → no aplica diferencia

    # Totales agregados por lado.
    tot_banco_c = sum(float(m.get("real_monto") or 0) for m in matches_sesion if (m.get("real_tipo") or "").upper() == "C")
    tot_banco_d = sum(float(m.get("real_monto") or 0) for m in matches_sesion if (m.get("real_tipo") or "").upper() == "D")
    tot_prog_c = sum(m["monto_prog_signed"] for m in matches_sesion if m["monto_prog_signed"] > 0)
    tot_prog_d = sum(-m["monto_prog_signed"] for m in matches_sesion if m["monto_prog_signed"] < 0)
    conciliados_totales = {
        "banco_creditos": round(tot_banco_c, 2),
        "banco_debitos": round(tot_banco_d, 2),
        "banco_neto": round(tot_banco_c - tot_banco_d, 2),
        "prog_creditos": round(tot_prog_c, 2),
        "prog_debitos": round(tot_prog_d, 2),
        "prog_neto": round(tot_prog_c - tot_prog_d, 2),
        "diff_neto": round((tot_banco_c - tot_banco_d) - (tot_prog_c - tot_prog_d), 2),
    }

    return render_template(
        "conciliacion/banco_v2.html",
        sesion=sesion,
        tab_activo=tab,
        buckets=buckets,
        balance=balance,
        saldo_pc_actual=balance,        # alias por si algún include lo busca
        banco_nombre="Pichincha",
        modo="compact",
        matches_sesion=matches_sesion,
        conciliados_totales=conciliados_totales,
    )


# ─── Endpoint: crear sesión a partir del upload ───────────────────────


@conciliacion_bp.route("/banco-v2/crear-sesion", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_crear_sesion():
    """Recibe el xlsx, parsea, mergea en la sesión abierta del banco
    (o crea una si no hay) y redirect al post-procesar.

    TMT 2026-06-02 dueña: 'que compare numero de documento de banco y si
    ya esta en nuestra lista, no agregarse'. El dedupe es row-level por
    documento contra todo lo que ya tenemos (sesión + histos + matches).
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

    sesion_id, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=no_banco,
        usuario=usuario,
        movs=movs,
        extracto_hash=None,  # ya no se usa para dedupe
        extracto_nombre=f.filename,
    )
    if n_added and n_skipped:
        msg = f"Sesión #{sesion_id}: agregadas {n_added} filas nuevas, omitidas {n_skipped} duplicadas."
        cat = "ok"
    elif n_added:
        msg = f"Sesión #{sesion_id}: agregadas {n_added} filas nuevas."
        cat = "ok"
    elif n_skipped:
        msg = f"Sesión #{sesion_id}: las {n_skipped} filas del archivo ya estaban cargadas — nada nuevo para agregar."
        cat = "info"
    else:
        msg = f"Sesión #{sesion_id}: el archivo no trajo movimientos procesables."
        cat = "warn"
    flash(msg, cat)
    return redirect(url_for("conciliacion.banco_post_procesar"))


# ─── Preview antes de confirmar match (tab Manual) ────────────────────


@conciliacion_bp.route("/banco-v2/preview", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_preview():
    """Vista previa de un match antes de confirmarlo.

    TMT 2026-05-29 dueña: 'necesito que haya un final view antes de
    apretar conciliar y display como cambiarian los movimientos'.

    Recibe los IDs/firmas que mandó el tab Manual, resuelve los items,
    valida signos/montos, y muestra ANTES vs DESPUÉS del balance Pichincha
    junto con la lista de side-effects que se aplicarán. La confirmación
    final POSTea al endpoint manual/confirmar con los mismos campos.
    """
    sesion_id = int(request.form.get("sesion_id") or 0)
    sesion = _sesion.sesion_por_id(sesion_id) if sesion_id else None
    if not sesion or sesion.get("cerrada_en"):
        flash("Sesión inválida o cerrada.", "error")
        return redirect(url_for("conciliacion.hub"))
    no_banco = _BANCO_PICHINCHA

    real_ids_csv = (request.form.get("real_ids") or "").strip()
    real_sigs_csv = (request.form.get("real_sigs") or "").strip()
    hist_ids_csv = (request.form.get("hist_ids") or "").strip()
    bancsis_ids_csv = (request.form.get("bancsis_ids") or "").strip()
    try:
        real_idxs = [int(x) for x in real_ids_csv.split(",") if x.strip()]
        hist_ids = [int(x) for x in hist_ids_csv.split(",") if x.strip()]
        bancsis_ids = [int(x) for x in bancsis_ids_csv.split(",") if x.strip()]
    except ValueError:
        flash("IDs inválidos.", "error")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))
    real_sigs = [s for s in real_sigs_csv.split("||") if s.strip()]

    # Resolver reales por firma (mismo método que manual_confirmar).
    movs = _sesion.cargar_movs(sesion)
    real_subset = []
    if real_sigs:
        sig_a_mov: dict[str, object] = {}
        for m in movs:
            key = "|".join([
                m.fecha.isoformat() if m.fecha else "",
                m.documento or "",
                f"{float(m.monto or 0):.2f}",
                m.tipo or "",
            ])
            sig_a_mov[key] = m
        real_subset = [sig_a_mov[s] for s in real_sigs if s in sig_a_mov]

    # Resolver históricos.
    hist_rows = []
    if hist_ids:
        try:
            hist_rows = _db.fetch_all(
                """
                SELECT id, fecha, concepto, documento, monto, tipo
                  FROM scintela.banco_historicos_pendientes
                 WHERE id = ANY(%s)
                """,
                (hist_ids,),
            ) or []
        except Exception:
            hist_rows = []

    # Resolver BANCSIS.
    bancsis_rows = []
    if bancsis_ids:
        try:
            bancsis_rows = _db.fetch_all(
                """
                SELECT tb.id_transaccion, tb.fecha, tb.documento, tb.importe,
                       tb.concepto, tb.prov, tb.numreferencia,
                       COALESCE(
                         (SELECT nombre FROM scintela.cliente
                           WHERE codigo_cli = tb.prov LIMIT 1), ''
                       ) AS prov_nombre
                  FROM scintela.transacciones_bancarias tb
                 WHERE tb.id_transaccion = ANY(%s)
                 ORDER BY tb.fecha, tb.id_transaccion
                """,
                (bancsis_ids,),
            ) or []
        except Exception:
            bancsis_rows = []

    # Validar signos.
    import bank_helpers as _bh
    _DOCS_CRED = ("DE", "TR", "XX", "NC", "IN", "AC")
    def _sign_bancsis(doc: str, imp: float) -> int:
        return 1 if _bh._signed_delta(doc, imp) >= 0 else -1
    def _sign_real(tipo: str) -> int:
        return 1 if (tipo or "").upper() == "C" else -1

    warnings: list[str] = []
    can_confirm = True

    banco_count = len(real_subset) + len(hist_rows)
    programa_count = len(bancsis_rows)

    if banco_count == 0 or programa_count == 0:
        flash("Faltan items de un lado.", "warn")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))

    # Totales por lado.
    banco_total_signed = sum(
        _sign_real(m.tipo) * float(m.monto or 0) for m in real_subset
    ) + sum(
        (1 if (h.get("tipo") or "").upper() == "C" else -1) * float(h.get("monto") or 0)
        for h in hist_rows
    )
    programa_total_signed = sum(
        _bh._signed_delta((b.get("documento") or ""), float(b.get("importe") or 0))
        for b in bancsis_rows
    )

    diff_total = round(banco_total_signed - programa_total_signed, 2)
    if abs(banco_total_signed) > 0.01 and abs(programa_total_signed) > 0.01:
        signo_banco = 1 if banco_total_signed > 0 else -1
        signo_prog = 1 if programa_total_signed > 0 else -1
        if signo_banco != signo_prog:
            warnings.append(
                f"Signos opuestos — banco {'+' if signo_banco > 0 else '−'} vs "
                f"programa {'+' if signo_prog > 0 else '−'}. Conciliar entradas con "
                f"salidas crea diferencia."
            )
            can_confirm = False
    if abs(diff_total) > 0.01:
        warnings.append(
            f"Montos no cuadran — diferencia ${diff_total:+,.2f}. "
            f"Si lo confirmás igual, esa diferencia queda como gap permanente."
        )

    # Cálculo del balance ANTES y DESPUÉS.
    balance_before = _bp.calcular(no_banco)

    # Deltas a aplicar.
    delta_pc_cred = 0.0   # créditos PC que dejan de ser pendientes
    delta_pc_deb = 0.0    # débitos PC que dejan de ser pendientes
    for b in bancsis_rows:
        doc = (b.get("documento") or "").upper()
        imp = float(b.get("importe") or 0)
        d = _bh._signed_delta(doc, imp)
        if d >= 0:
            delta_pc_cred += d
        else:
            delta_pc_deb += -d  # storage convention: debits positivos

    delta_banco_cred = 0.0
    delta_banco_deb = 0.0
    for h in hist_rows:
        m = float(h.get("monto") or 0)
        t = (h.get("tipo") or "").upper()
        if t == "C":
            delta_banco_cred += m
        else:
            delta_banco_deb += m

    n_after_pc = max(0, int(balance_before.get("n_pendientes_conciliar") or 0) - len(bancsis_rows))
    n_after_banco = max(0, int(balance_before.get("n_pendientes") or 0) - len(hist_rows))

    pc_cred_after = round(float(balance_before.get("pendientes_pc_creditos") or 0) - delta_pc_cred, 2)
    pc_deb_after = round(float(balance_before.get("pendientes_pc_debitos") or 0) - delta_pc_deb, 2)
    pc_neto_after = round(pc_cred_after - pc_deb_after, 2)
    saldo_si_concilio_after = round(float(balance_before.get("saldo") or 0) - pc_neto_after, 2)

    # TMT 2026-06-03 BUG FIX: usar *_total (incluye extracto sesión).
    # Antes: preview usaba pendientes_banco_creditos solo (histos), salto
    # de $475K en "saldo banco esperado".
    banco_cred_after = round(
        float(balance_before.get("pendientes_banco_total_creditos") or balance_before.get("pendientes_banco_creditos") or 0)
        - delta_banco_cred, 2,
    )
    banco_deb_after = round(
        float(balance_before.get("pendientes_banco_total_debitos") or balance_before.get("pendientes_banco_debitos") or 0)
        - delta_banco_deb, 2,
    )
    banco_neto_after = round(banco_cred_after - banco_deb_after, 2)
    saldo_banco_esperado_after = round(saldo_si_concilio_after + banco_neto_after, 2)
    n_after_banco = max(
        0,
        int(balance_before.get("n_pendientes_banco_total") or balance_before.get("n_pendientes") or 0)
        - len(hist_rows),
    )

    balance_after = {
        "saldo": float(balance_before.get("saldo") or 0),
        "pendientes_pc_creditos": pc_cred_after,
        "pendientes_pc_debitos": pc_deb_after,
        "pendientes_conciliar_neto": pc_neto_after,
        "n_pendientes_conciliar": n_after_pc,
        "saldo_si_concilio_todo": saldo_si_concilio_after,
        "pendientes_banco_creditos": banco_cred_after,
        "pendientes_banco_debitos": banco_deb_after,
        "pendientes_banco_total_creditos": banco_cred_after,
        "pendientes_banco_total_debitos": banco_deb_after,
        "neto_pendientes": banco_neto_after,
        "neto_pendientes_total": banco_neto_after,
        "n_pendientes": n_after_banco,
        "n_pendientes_banco_total": n_after_banco,
        "saldo_banco_esperado": saldo_banco_esperado_after,
    }

    return render_template(
        "conciliacion/banco_v2_preview.html",
        sesion=sesion,
        balance_before=balance_before,
        balance_after=balance_after,
        real_subset=real_subset,
        hist_rows=hist_rows,
        bancsis_rows=bancsis_rows,
        banco_total_signed=banco_total_signed,
        programa_total_signed=programa_total_signed,
        diff_total=diff_total,
        warnings=warnings,
        can_confirm=can_confirm,
        # Pass-through para que el submit final repita exactamente.
        real_ids_csv=real_ids_csv,
        real_sigs_csv=real_sigs_csv,
        hist_ids_csv=hist_ids_csv,
        bancsis_ids_csv=bancsis_ids_csv,
        sesion_id=sesion_id,
    )


# ─── Auditar diferencia (root cause de la brecha PC vs banco real) ───


@conciliacion_bp.route("/banco-v2/auditar", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_auditar():
    """Audit page: muestra los componentes que generan la diferencia entre
    saldo banco esperado (cálculo) y saldo banco real (extracto).

    TMT 2026-05-29 dueña: 'no esta bien que tengamos diferencia' +
    'necesito que vos hagas la auditoria'. Lista TRES diagnósticos:
      1) Drift del saldo running PC (suma de movs vs último saldo).
      2) Matches confirmados con monto distinto entre real y BANCSIS.
      3) Pendientes de banco con monto raro o duplicados de firma.
    """
    no_banco = _BANCO_PICHINCHA
    balance = _bp.calcular(no_banco)

    # ── Check 1: walk-forward saldo running PC ─────────────────────
    # TMT 2026-05-29 dueña: 'esto sigue mal'. Bug en el auditor anterior:
    # asumía que TODAS las importes son positivas y aplicaba ±signo según
    # documento. Pero en la DB hay convención MIXTA — filas legacy del
    # dBase tienen importe ya signado (ej ND con importe=−40,775) y el
    # código nuevo usa importe absoluto. La fórmula simple daba falsos
    # positivos por miles. Fix: usar la misma función _signed_delta de
    # bank_helpers — fuente de verdad usada por trigger e insert.
    import bank_helpers as _bh
    filas_torcidas = []
    last_saldo = None
    try:
        rows_walk = _db.fetch_all(
            """
            SELECT id_transaccion, fecha, documento, importe, saldo, concepto
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s AND saldo IS NOT NULL
             ORDER BY fecha ASC, id_transaccion ASC
            """,
            (no_banco,),
        ) or []
        saldo_prev = None
        for r in rows_walk:
            s = float(r["saldo"] or 0)
            imp = float(r["importe"] or 0)
            doc = (r.get("documento") or "").upper()
            delta = _bh._signed_delta(doc, imp)
            if saldo_prev is not None:
                esperado = round(saldo_prev + delta, 2)
                diff = round(s - esperado, 2)
                if abs(diff) > 0.01:
                    filas_torcidas.append({
                        "id": int(r["id_transaccion"]),
                        "fecha": r["fecha"],
                        "documento": doc,
                        "importe": imp,
                        "delta_aplicado": round(delta, 2),
                        "saldo_grabado": s,
                        "saldo_esperado": esperado,
                        "diferencia": diff,
                        "concepto": (r.get("concepto") or "")[:50],
                    })
            saldo_prev = s
        last_saldo = saldo_prev
        # Top 200 desviaciones por magnitud (las más grandes primero).
        filas_torcidas.sort(key=lambda x: abs(x["diferencia"]), reverse=True)
        filas_torcidas = filas_torcidas[:200]
    except Exception as e:
        _LOG.warning("auditar walk-forward falló: %s", e)
    suma_diff_torcidas = round(sum(x["diferencia"] for x in filas_torcidas), 2)

    # ── Check 2: matches confirmados con drift de monto ────────────
    diff_matches = []
    sum_diff_matches = 0.0
    try:
        diff_matches = _db.fetch_all(
            """
            SELECT m.id, m.creado_en, m.real_fecha, m.real_documento,
                   m.real_monto, m.real_concepto,
                   tb.importe   AS tb_importe,
                   tb.documento AS tb_documento,
                   tb.concepto  AS tb_concepto,
                   ROUND(m.real_monto - tb.importe, 2) AS diferencia
              FROM scintela.banco_conciliacion_match m
              JOIN scintela.transacciones_bancarias tb
                ON tb.id_transaccion = m.id_transaccion
             WHERE m.no_banco = %s
               AND (m.deshecho_en IS NULL)
               AND m.id_transaccion IS NOT NULL
               AND ABS(m.real_monto - tb.importe) > 0.01
             ORDER BY ABS(m.real_monto - tb.importe) DESC
             LIMIT 200
            """,
            (no_banco,),
        ) or []
        sum_diff_matches = sum(float(r.get("diferencia") or 0) for r in diff_matches)
    except Exception as e:
        _LOG.warning("auditar diff_matches falló: %s", e)

    # ── Check 3: pendientes históricos con firmas duplicadas ───────
    duplicados_hist = []
    try:
        duplicados_hist = _db.fetch_all(
            """
            SELECT no_banco, fecha, COALESCE(documento, '') AS documento,
                   monto, tipo, COUNT(*) AS n, SUM(monto) AS suma
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
             GROUP BY no_banco, fecha, COALESCE(documento, ''), monto, tipo
            HAVING COUNT(*) > 1
             ORDER BY COUNT(*) DESC, ABS(monto) DESC
             LIMIT 100
            """,
            (no_banco,),
        ) or []
    except Exception as e:
        _LOG.warning("auditar duplicados_hist falló: %s", e)

    # ── Diferencia objetivo (lo que la dueña ve en la página) ──────
    # saldo_banco_esperado calculado vs saldo banco real del último extracto
    # de la sesión abierta (si existe).
    sesion = _sesion.sesion_abierta(no_banco, _usuario_actual())
    saldo_banco_real = None
    if sesion:
        try:
            movs_s = _sesion.cargar_movs(sesion)
            con_fecha = [
                m for m in movs_s
                if getattr(m, "fecha", None) and getattr(m, "saldo", None) is not None
            ]
            if con_fecha:
                ult = max(con_fecha, key=lambda m: m.fecha)
                saldo_banco_real = float(ult.saldo)
        except Exception:
            pass
    diferencia_objetivo = None
    if saldo_banco_real is not None and balance.get("saldo_banco_esperado") is not None:
        diferencia_objetivo = round(
            saldo_banco_real - balance["saldo_banco_esperado"], 2
        )

    return render_template(
        "conciliacion/auditar.html",
        balance=balance,
        saldo_banco_real=saldo_banco_real,
        diferencia_objetivo=diferencia_objetivo,
        filas_torcidas=filas_torcidas,
        suma_diff_torcidas=suma_diff_torcidas,
        last_saldo=last_saldo,
        diff_matches=diff_matches,
        sum_diff_matches=round(sum_diff_matches, 2),
        duplicados_hist=duplicados_hist,
    )


# ─── Reabrir sesión cerrada ──────────────────────────────────────────


# ─── Endpoint: setear saldo banco objetivo manual ─────────────────────


@conciliacion_bp.route("/banco-v2/set-saldo-objetivo", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_set_saldo_objetivo():
    """Guarda el saldo banco que la dueña lee del extracto o de la web del
    banco. TMT 2026-06-02 dueña: 'lo deberia implementar el usuario no?
    porque por el excel no sabemos cual es el ultimo valor'.

    Reemplaza el auto-detect por max(fecha) que era frágil cuando había
    múltiples movs el mismo día o varios uploads merged.
    """
    sesion_id = int(request.form.get("sesion_id") or 0)
    sesion = _sesion.sesion_por_id(sesion_id) if sesion_id else None
    if not sesion:
        flash("Sesión no encontrada.", "error")
        return redirect(url_for("conciliacion.hub"))

    raw = (request.form.get("saldo_objetivo") or "").strip()
    if raw == "" or raw.lower() in ("none", "null", "—"):
        # Clear → vuelve al auto-detect.
        try:
            _db.execute(
                """
                UPDATE scintela.banco_conciliacion_sesion
                   SET saldo_banco_objetivo = NULL
                 WHERE id = %s
                """,
                (sesion_id,),
            )
            flash("Saldo banco objetivo borrado — vuelve al auto-detect.", "ok")
        except Exception as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))

    # Limpia comas/separadores antes de parsear.
    try:
        cleaned = raw.replace(",", "").replace("$", "").strip()
        valor = float(cleaned)
    except (ValueError, TypeError):
        flash(f"Valor inválido: '{raw}'. Usá formato numérico (ej. 2846820.24).", "error")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))

    try:
        _db.execute(
            """
            UPDATE scintela.banco_conciliacion_sesion
               SET saldo_banco_objetivo = %s
             WHERE id = %s
            """,
            (valor, sesion_id),
        )
        flash(f"Saldo banco objetivo: ${valor:,.2f}", "ok")
    except Exception as e:
        flash(f"Error al guardar: {e}", "error")
    return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))


# ─── Endpoint: descartar movs banco (no necesitan conciliarse) ────────


@conciliacion_bp.route("/banco-v2/descartar-banco", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_descartar():
    """Marca movs del lado banco como conciliados SIN contraparte PC.

    TMT 2026-06-02 dueña: 'si hay movimientos que no quiero conciliar,
    porque ya fueron conciliados o porque si del banco como hago? quizas
    tengo entrada y devolucion de un cheque'.

    Casos típicos:
      - Entrada $X + devolución $X del mismo cheque → ambos netean a 0,
        no hay contraparte PC.
      - Movs ya conciliados externamente (ej. desde dBase) que el sistema
        no detecta.
      - Cargos del banco que la dueña decide no trackear.

    Acepta hist_ids[] (histos backfilled) y real_sigs[] (movs del extracto
    de la sesión). Para hist: UPDATE conciliado_en. Para real: los inserta
    como histos con conciliado_en=NOW (queda registro auditable, y como
    están conciliados no aparecen como pendientes).
    """
    sesion_id = int(request.form.get("sesion_id") or 0)
    sesion = _sesion.sesion_por_id(sesion_id) if sesion_id else None
    if not sesion:
        flash("Sesión no encontrada.", "error")
        return redirect(url_for("conciliacion.hub"))

    hist_ids_csv = (request.form.get("hist_ids") or "").strip()
    real_sigs_csv = (request.form.get("real_sigs") or "").strip()
    motivo = (request.form.get("motivo") or "descartado-banco")[:50]

    try:
        hist_ids = [int(x) for x in hist_ids_csv.split(",") if x.strip()]
    except ValueError:
        hist_ids = []
    real_sigs = [s for s in real_sigs_csv.split("||") if s.strip()]

    if not hist_ids and not real_sigs:
        flash("No marcaste ningún mov para descartar.", "warn")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))

    usuario = _usuario_actual()
    no_banco = _BANCO_PICHINCHA
    n_hist = 0
    n_real = 0

    # 1) Marcar históricos directamente — UPDATE conciliado_en.
    if hist_ids:
        try:
            n_hist = _db.execute(
                """
                UPDATE scintela.banco_historicos_pendientes
                   SET conciliado_en = CURRENT_TIMESTAMP,
                       conciliado_por = %s
                 WHERE id = ANY(%s)
                   AND no_banco = %s
                   AND conciliado_en IS NULL
                """,
                (f"descart:{motivo}"[:50], hist_ids, no_banco),
            ) or 0
        except Exception as e:
            _LOG.exception("descartar histos falló: %s", e)
            flash(f"Error al descartar históricos: {e}", "error")
            return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))

    # 2) Para movs del extracto: PRIMERO intentamos marcar como conciliado
    # un histo existente que matchee firma (caso común: el extracto trajo
    # una fila que YA está en banco_historicos_pendientes como pendiente).
    # Si no hay, INSERTAMOS uno nuevo ya conciliado.
    # TMT 2026-06-02 fix: antes hacíamos solo INSERT ... ON CONFLICT DO
    # NOTHING, que silenciosamente saltaba cuando el histo ya estaba y no
    # actualizaba conciliado_en — la dueña veía el mov seguir pendiente.
    if real_sigs:
        movs = _sesion.cargar_movs(sesion)
        sig_a_mov = {}
        for m in movs:
            key = "|".join([
                m.fecha.isoformat() if m.fecha else "",
                m.documento or "",
                f"{float(m.monto or 0):.2f}",
                m.tipo or "",
            ])
            sig_a_mov[key] = m
        for sig in real_sigs:
            mov = sig_a_mov.get(sig)
            if not mov:
                continue
            try:
                # Intento 1: UPDATE del histo existente (pendiente) que matchee firma.
                rc = _db.execute(
                    """
                    UPDATE scintela.banco_historicos_pendientes
                       SET conciliado_en = CURRENT_TIMESTAMP,
                           conciliado_por = %s
                     WHERE no_banco = %s
                       AND fecha = %s
                       AND COALESCE(documento, '') = COALESCE(%s, '')
                       AND monto = %s::numeric
                       AND tipo = %s
                       AND conciliado_en IS NULL
                    """,
                    (
                        f"descart:{motivo}"[:50],
                        no_banco, mov.fecha,
                        (mov.documento or "")[:40],
                        str(mov.monto or 0),
                        (mov.tipo or "C")[:2],
                    ),
                ) or 0
                if rc > 0:
                    n_real += rc
                    continue
                # Intento 2: no había histo existente → INSERT uno ya conciliado.
                _db.execute(
                    """
                    INSERT INTO scintela.banco_historicos_pendientes
                        (no_banco, fecha, concepto, documento, monto, tipo,
                         oficina, detalle, fuente, creado_por,
                         conciliado_en, conciliado_por, codigo)
                    VALUES (%s, %s, %s, %s, %s::numeric, %s, %s, %s, %s, %s,
                            CURRENT_TIMESTAMP, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        no_banco, mov.fecha,
                        (mov.concepto or "")[:120],
                        (mov.documento or "")[:40],
                        str(mov.monto or 0),
                        (mov.tipo or "C")[:2],
                        (getattr(mov, "oficina", "") or "")[:40],
                        "",  # detalle
                        f"descart:sesion:{sesion_id}",
                        usuario[:50],
                        f"descart:{motivo}"[:50],
                        (getattr(mov, "codigo", "") or "")[:20],
                    ),
                )
                n_real += 1
            except Exception as e:
                _LOG.warning("descartar real_sig %s falló: %s", sig, e)

    total = n_hist + n_real
    flash(
        f"Descartados {total} movs ({n_hist} histos + {n_real} del extracto). "
        f"Marcados como conciliados sin contraparte PC. Motivo: {motivo}.",
        "ok" if total > 0 else "warn",
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
    real_sigs_csv = (request.form.get("real_sigs") or "").strip()
    hist_ids_csv = (request.form.get("hist_ids") or "").strip()
    bancsis_ids_csv = (request.form.get("bancsis_ids") or "").strip()
    try:
        real_idxs = [int(x) for x in real_ids_csv.split(",") if x.strip()]
        hist_ids = [int(x) for x in hist_ids_csv.split(",") if x.strip()]
        bancsis_ids = [int(x) for x in bancsis_ids_csv.split(",") if x.strip()]
    except ValueError:
        flash("IDs inválidos.", "error")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))
    real_sigs = [s for s in real_sigs_csv.split("||") if s.strip()]

    if (not real_idxs and not hist_ids) or not bancsis_ids:
        flash("Marcá al menos un mov de cada lado.", "warn")
        return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))

    # TMT 2026-05-29 dueña: 'manual me aparece el mensaje vas a conciliar y
    # cuando pongo ok dice sin cambios. ARREGLALO YA'.
    # Lecciones: el matcher re-corrido a veces NO contiene los movs reales
    # que la dueña seleccionó (PASS 0 los reclasifica a matches[], o el
    # orden cambia y el idx queda stale). Necesitamos un mecanismo DURABLE.
    #
    # Nueva estrategia (sigs como PRIMARIO, idx como fallback solo si el
    # front es viejo):
    #   1. Si hay real_sigs → resolver contra el payload CRUDO de la sesión.
    #      No depende del matcher; las firmas siempre encuentran su mov si
    #      existe en el extracto subido.
    #   2. Si NO hay real_sigs (cliente viejo, antes del deploy de hoy) →
    #      caer al matcher y al idx, como antes.
    movs = _sesion.cargar_movs(sesion)
    real_subset = []
    metodo_resolucion = "n/a"

    if real_sigs:
        sig_a_mov: dict[str, object] = {}
        for m in movs:
            key = "|".join([
                m.fecha.isoformat() if m.fecha else "",
                m.documento or "",
                f"{float(m.monto or 0):.2f}",
                m.tipo or "",
            ])
            sig_a_mov[key] = m
        real_subset = [sig_a_mov[s] for s in real_sigs if s in sig_a_mov]
        metodo_resolucion = f"firma ({len(real_subset)}/{len(real_sigs)})"

    if real_idxs and not real_subset:
        # Fallback: matcher + idx. Solo si las firmas no resolvieron nada.
        from modules.conciliacion.matcher_banco import matchear_extracto_banco
        try:
            res = matchear_extracto_banco(movs, no_banco=_BANCO_PICHINCHA)
            real_only = res.real_only or []
        except Exception as e:
            _LOG.exception("re-match para confirmar manual falló: %s", e)
            real_only = []
        real_subset = [real_only[i] for i in real_idxs if 0 <= i < len(real_only)]
        metodo_resolucion = f"idx ({len(real_subset)}/{len(real_idxs)} en real_only[{len(real_only)}])"

    _LOG.info(
        "manual confirm: real_idxs=%d real_sigs=%d hist=%d bancsis=%d resolución=%s subset=%d",
        len(real_idxs), len(real_sigs), len(hist_ids), len(bancsis_ids),
        metodo_resolucion, len(real_subset),
    )

    n_matches = 0
    err_msg: str | None = None
    usuario = _usuario_actual()
    # TMT 2026-06-02 dueña: 'los movimientos no tienen que ser 1:1' / 'puede
    # ser distinto'. Aceptamos cualquier N:M:
    #
    # Estrategia unificada:
    #   1. Min(N, M) pares 1:1 ordenados por monto desc (biggest↔biggest).
    #   2. Si sobran banco rows (N > M): cada uno se matchea contra el
    #      PRIMER PC. El unique index es por real_*, así que reusar el
    #      mismo PC con reales distintos es OK.
    #   3. Si sobran PC rows (M > N): se marcan stat='*' directamente
    #      (sin record en banco_conciliacion_match porque la firma real_*
    #      ya está tomada). Quedan conciliados visualmente.
    #
    # Esto cubre 1:1, N:1, 1:N, y N:M arbitrario.
    if real_subset and bancsis_ids:
        real_sorted = sorted(real_subset, key=lambda r: float(r.monto or 0), reverse=True)
        bk_sorted = sorted(bancsis_ids, reverse=True)
        n_pair = min(len(real_sorted), len(bk_sorted))

        # 1) Pares 1:1.
        for i in range(n_pair):
            try:
                confirmar_match(_BANCO_PICHINCHA, real_sorted[i], bk_sorted[i],
                                usuario=usuario, metodo="matched_manual")
                n_matches += 1
            except Exception as e:
                _LOG.warning("manual confirm par %d falló: %s", i, e)
                if err_msg is None:
                    err_msg = str(e)

        # 2) Banco extras (N > M): todos matcheados contra PC[0].
        for r in real_sorted[n_pair:]:
            try:
                confirmar_match(_BANCO_PICHINCHA, r, bk_sorted[0],
                                usuario=usuario, metodo="matched_manual")
                n_matches += 1
            except Exception as e:
                _LOG.warning("manual confirm extra banco falló: %s", e)
                if err_msg is None:
                    err_msg = str(e)

        # 3) PC extras (M > N): crear match records con real_*=NULL para
        # que aparezcan en el tab Conciliados con el lado programa visible.
        # Detectamos si existe la columna `metodo` (mig 0047). Hacemos
        # INSERT directo porque confirmar_match() requiere un MovBanco no-null.
        if len(bk_sorted) > n_pair:
            extras = [int(b) for b in bk_sorted[n_pair:]]
            tiene_metodo = False
            try:
                row = _db.fetch_one(
                    """
                    SELECT 1 FROM information_schema.columns
                     WHERE table_schema='scintela'
                       AND table_name='banco_conciliacion_match'
                       AND column_name='metodo'
                    """,
                )
                tiene_metodo = bool(row)
            except Exception:
                pass
            for bk_id in extras:
                try:
                    # TMT 2026-06-03: tx_firma se popula via SQL helper.
                    if tiene_metodo:
                        _db.execute(
                            """
                            INSERT INTO scintela.banco_conciliacion_match (
                                no_banco, estado, metodo,
                                id_transaccion, tx_firma, usuario
                            ) VALUES (%s, %s, %s, %s,
                                      scintela.compute_tx_firma(%s), %s)
                            """,
                            (_BANCO_PICHINCHA, 'matched', 'matched_manual',
                             bk_id, bk_id, usuario),
                        )
                    else:
                        _db.execute(
                            """
                            INSERT INTO scintela.banco_conciliacion_match (
                                no_banco, estado, id_transaccion, tx_firma, usuario
                            ) VALUES (%s, %s, %s,
                                      scintela.compute_tx_firma(%s), %s)
                            """,
                            (_BANCO_PICHINCHA, 'matched', bk_id, bk_id, usuario),
                        )
                    # Dual-write stat='*'.
                    _db.execute(
                        """
                        UPDATE scintela.transacciones_bancarias
                           SET stat = '*'
                         WHERE id_transaccion = %s AND no_banco = %s
                        """,
                        (bk_id, _BANCO_PICHINCHA),
                    )
                    n_matches += 1
                except Exception as e:
                    _LOG.warning("manual confirm match PC extra %s falló: %s", bk_id, e)
                    if err_msg is None:
                        err_msg = str(e)

    # 2) Históricos seleccionados → conciliarlos vía confirmar_match.
    # TMT 2026-05-29 dueña: 'HAY UN BUG' + 'NO ESTABAN CONCILIADOS'.
    # Bug real: el UPDATE viejo seteaba conciliado_match_id = bancsis_id
    # (id_transaccion), pero la columna tiene un FK a
    # banco_conciliacion_match.id — otro id totalmente distinto. Postgres
    # tiraba FK violation, la excepción se tragaba silenciosa, n_hist=0
    # siempre. La dueña veía "no hubo cambios" sin razón visible.
    #
    # Fix: tratar cada histórico como un mov real (mismo shape MovBanco)
    # y pasarlo por confirmar_match. Esto:
    #   - Inserta una fila en banco_conciliacion_match (ON CONFLICT NOTHING).
    #   - Marca stat='*' en transacciones_bancarias del BANCSIS pareado.
    #   - El side-effect del propio confirmar_match actualiza el histórico
    #     con conciliado_match_id correcto (= el match.id recién creado).
    # Resultado: la conciliación queda bien guardada, persiste cierre,
    # y aparece en el tab Conciliados.
    n_hist = 0
    if hist_ids:
        bk_id_primary = bancsis_ids[0] if bancsis_ids else None
        if not bk_id_primary:
            flash(
                "Para conciliar históricos hay que seleccionar UN movimiento "
                "del programa.",
                "warn",
            )
        else:
            try:
                hist_rows = _db.fetch_all(
                    """
                    SELECT id, no_banco, fecha, concepto, documento, monto, tipo,
                           oficina, detalle
                      FROM scintela.banco_historicos_pendientes
                     WHERE id = ANY(%s)
                    """,
                    (hist_ids,),
                ) or []
            except Exception as e:
                _LOG.exception("fetch historicos falló: %s", e)
                hist_rows = []
            from decimal import Decimal as _D
            from modules.conciliacion.parser_banco import MovBanco as _MB
            for h in hist_rows:
                try:
                    mov_h = _MB(
                        fecha=h.get("fecha"),
                        concepto=str(h.get("concepto") or ""),
                        documento=str(h.get("documento") or ""),
                        monto=_D(str(h.get("monto") or 0)),
                        saldo=_D("0"),
                        codigo=str(h.get("oficina") or "")[:10],
                        tipo=str(h.get("tipo") or "C").upper(),
                        oficina=str(h.get("oficina") or ""),
                    )
                    confirmar_match(
                        _BANCO_PICHINCHA, mov_h, bk_id_primary,
                        usuario=usuario, metodo="matched_historico",
                    )
                    n_hist += 1
                except Exception as e:
                    _LOG.warning("conciliar histórico id=%s falló: %s", h.get("id"), e)
                    if err_msg is None:
                        err_msg = str(e)

    total = n_matches + n_hist
    _sesion.incrementar_matches(sesion_id, total)
    parts = []
    if n_matches: parts.append(f"{n_matches} match(es) del extracto")
    if n_hist: parts.append(f"{n_hist} histórico(s)")
    if parts:
        flash(" + ".join(parts) + " conciliado(s).", "ok")
    else:
        # Diagnóstico verboso: si no hubo movs, decir EXACTO por qué para
        # que la dueña pueda reaccionar (no más "Sin cambios" silencioso).
        diag = (
            f"banco enviado={len(real_idxs)}+{len(hist_ids)}hist, "
            f"programa enviado={len(bancsis_ids)}, "
            f"resolución={metodo_resolucion}"
        )
        if real_subset and bancsis_ids and not n_matches:
            # Llegó al confirm_match pero todos fallaron (raro).
            flash(
                f"Se intentaron {len(real_subset)} match(es) pero todos "
                f"fallaron. {('Error: ' + err_msg) if err_msg else 'Sin error claro.'} "
                f"[{diag}]",
                "error",
            )
        elif real_idxs and not real_subset and not real_sigs:
            # Cliente viejo: idxs mandados sin firmas. Hard-refresh requerido.
            flash(
                f"Tu pantalla está vieja — recargá con Ctrl+Shift+R (o Cmd+Shift+R) "
                f"para que el form mande las firmas que el backend necesita. "
                f"[{diag}]",
                "error",
            )
        elif (real_idxs or real_sigs) and not real_subset:
            flash(
                f"No pude ubicar los {max(len(real_idxs), len(real_sigs))} mov(s) "
                f"del banco que seleccionaste — quizá ya estaban conciliados o el "
                f"matcher los re-clasificó. Recargá la página. [{diag}]",
                "error",
            )
        elif hist_ids and not n_hist:
            flash(
                f"Los {len(hist_ids)} histórico(s) seleccionado(s) no se "
                f"encontraron en la tabla (¿ids inválidos?). [{diag}]",
                "error",
            )
        elif err_msg:
            flash(f"No pude conciliar: {err_msg}. [{diag}]", "error")
        else:
            flash(f"Sin cambios. [{diag}]", "warn")
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


# ─── XLSX resumen de pendientes ───────────────────────────────────────
# TMT 2026-06-02 dueña: 'pero si quiero el resumen que imprimiamos!!'
# El XLSX que se generaba al cerrar sesión sigue disponible como descarga
# on-demand vía /banco-v2/resumen-xlsx. Genera el listado actual de
# pendientes (histos + extract no-matcheado) con el resumen contable
# al pie. No cierra ni promueve nada.


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
    rows: list[dict] = []
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

    # TMT 2026-06-02: incluir también los movs del extracto en la sesión
    # actual que NO están matcheados (antes se "promovían a histos" al
    # cerrar; con sesión continua no hay promoción, así que los traemos
    # de la sesión directamente). Dedupe por (fecha, documento) contra
    # los histos que ya pusimos.
    seen_keys = {
        (r.get("fecha"), (r.get("documento") or "").strip().upper())
        for r in rows
    }
    try:
        movs_s = _sesion.cargar_movs(sesion)
        if movs_s:
            from modules.conciliacion.matcher_banco import matchear_extracto_banco
            try:
                res = matchear_extracto_banco(movs_s, no_banco=no_banco)
                for m in (res.real_only or []):
                    key = (m.fecha, (m.documento or "").strip().upper())
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    rows.append({
                        "fecha": m.fecha,
                        "concepto": m.concepto or "",
                        "documento": m.documento or "",
                        "monto": float(m.monto or 0),
                        "oficina": getattr(m, "oficina", "") or "",
                        "detalle": "",
                        "tipo": (m.tipo or "C").upper(),
                    })
            except Exception as e:
                _LOG.warning("xlsx matcher inline falló: %s", e)
    except Exception as e:
        _LOG.warning("xlsx cargar movs sesion falló: %s", e)
    # Re-ordenar por fecha asc (los nuevos pueden venir desordenados).
    rows.sort(key=lambda r: (r.get("fecha") or date.min, str(r.get("documento") or "")))

    _PDF_DIR.mkdir(parents=True, exist_ok=True)
    xlsx_path = _PDF_DIR / f"sesion_{sesion['id']}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "MOVIMIENTOS PENDIENTES"

    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDDDDD")

    ws["A1"] = "MOVIMIENTOS PENDIENTES"
    ws["A1"].font = Font(bold=True, size=14)
    ws.merge_cells("A1:E1")
    ws["A2"] = (
        f"Pichincha · Sesión #{sesion['id']} · "
        f"{_hora_ec_str(datetime.now(), '%Y-%m-%d %H:%M')} · "
        f"{len(rows)} movs"
    )
    ws.merge_cells("A2:E2")

    headers = ["FECHA", "DETALLE", "CODIGO", "VALOR", "DETALLE"]
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=4, column=col, value=h)
        c.font = bold
        c.fill = header_fill

    # BUG #10 fix 2026-05-29: defensa contra CSV-injection.
    # Si un concepto del banco empieza con =, +, -, @, Excel lo evalúa
    # como fórmula al abrir. Prefijamos con apóstrofe para neutralizar.
    def _safe_cell(v: str) -> str:
        s = str(v or "")
        if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + s
        return s

    # TMT 2026-05-29 dueña: 'i am not seeing some of the expenses' (PAGO
    # SENAE, INTELA C-PAG-*, CHEQUE DEVUELTO). El filtro hard-coded
    # `tipo='C'` saltaba TODOS los débitos. Sacado: ahora se listan
    # créditos Y débitos juntos. Débitos se renderean con paréntesis
    # (convención contable) usando el number_format '#,##0.00;(#,##0.00)'.
    r = 5
    total = 0.0
    for row in rows:
        tipo = (row.get("tipo") or "C").upper()
        monto = float(row.get("monto") or 0)
        valor = monto if tipo == "C" else -monto  # negativos para débitos
        total += valor
        fecha = row.get("fecha")
        ws.cell(row=r, column=1, value=fecha.strftime("%d/%m/%Y") if fecha else "")
        ws.cell(row=r, column=2, value=_safe_cell(row.get("concepto"))[:100])
        ws.cell(row=r, column=3, value=_safe_cell(row.get("documento"))[:30])
        ws.cell(row=r, column=4, value=valor).number_format = "+#,##0.00;-#,##0.00;0.00"
        ws.cell(row=r, column=5, value=_safe_cell(row.get("detalle") or row.get("oficina"))[:30])
        r += 1

    # ── Resumen contable al pie ───────────────────────────────────────
    # TMT 2026-05-29 dueña — decisión final: layout viejo de 5 filas con
    # DIFERENCIA explícita. Razones:
    #   - Math cierra por construcción (AJUSTE + SISTEMA = TOTAL conciliado)
    #   - SALDO BANCO se imprime aparte como referencia externa
    #   - DIFERENCIA expone honestamente la brecha real-vs-cálculo
    #   - Si DIFERENCIA es chica (centavos) es ruido aceptable
    #   - Si DIFERENCIA es grande (>$1000) → auditar es el next step
    #
    # Layout final:
    #   AJUSTE              = pendientes_banco_creditos − pendientes_banco_debitos
    #   SALDO SISTEMA       = saldo conciliado (libros − pendientes_pc_neto)
    #   TOTAL               = SISTEMA + AJUSTE (cierra)
    #   SALDO BANCO         = último saldo del extracto subido
    #   DIFERENCIA          = SALDO BANCO − TOTAL (cero si todo conciliado)
    saldo_sistema = float(
        balance.get("saldo_si_concilio_todo")
        or balance.get("saldo") or 0
    )
    pendientes_banco_cred = float(balance.get("pendientes_banco_creditos") or 0)
    pendientes_banco_deb = float(balance.get("pendientes_banco_debitos") or 0)

    # Incluir real_only de la sesión actual (movs del extracto sin matchear)
    # para que el AJUSTE refleje TODO lo que cierra contra banco.
    saldo_banco_real = None
    try:
        movs_sesion = _sesion.cargar_movs(sesion)
        if movs_sesion:
            from modules.conciliacion.matcher_banco import matchear_extracto_banco
            try:
                res = matchear_extracto_banco(movs_sesion, no_banco=_BANCO_PICHINCHA)
                for m in (res.real_only or []):
                    monto_m = float(m.monto or 0)
                    if (m.tipo or "").upper() == "C":
                        pendientes_banco_cred += monto_m
                    else:
                        pendientes_banco_deb += monto_m
            except Exception:
                pass
            con_fecha = [
                m for m in movs_sesion
                if getattr(m, "fecha", None) and getattr(m, "saldo", None) is not None
            ]
            if con_fecha:
                ult = max(con_fecha, key=lambda m: m.fecha)
                saldo_banco_real = float(ult.saldo)
            else:
                ult = movs_sesion[-1]
                saldo_banco_real = float(getattr(ult, "saldo", None) or 0) or None
    except Exception as e:
        _LOG.warning("no pude leer movs sesion: %s", e)

    ajuste = round(pendientes_banco_cred - pendientes_banco_deb, 2)
    total_calc = round(saldo_sistema + ajuste, 2)
    diferencia = round(saldo_banco_real - total_calc, 2) if saldo_banco_real is not None else None

    label_col = 3
    val_col = 4

    r += 1  # fila vacía de separación
    # TMT 2026-05-29 dueña: 'puedes separar en ajuste los positivos y los
    # negativos?'. Ajuste = banco_cred − banco_deb; mostramos las dos
    # componentes y el neto antes del SISTEMA.
    rows_resumen = [
        ("+ Pendientes banco créditos", pendientes_banco_cred),
        ("− Pendientes banco débitos", -pendientes_banco_deb),
        ("AJUSTE", ajuste),
        ("SALDO SISTEMA (conciliado)", saldo_sistema),
        ("TOTAL", total_calc),
    ]
    if saldo_banco_real is not None:
        rows_resumen.append(("SALDO BANCO (extracto)", saldo_banco_real))
        rows_resumen.append(("DIFERENCIA", diferencia))

    for label, val in rows_resumen:
        ws.cell(row=r, column=label_col, value=label).font = bold
        if val is not None:
            cell = ws.cell(row=r, column=val_col, value=val)
            cell.font = bold
            # Signo + / − explícito, mismo formato que las filas de movs.
            cell.number_format = "+#,##0.00;-#,##0.00;0.00"
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


@conciliacion_bp.route("/banco-v2/resumen-xlsx", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_resumen_xlsx():
    """Genera y descarga el resumen XLSX de movimientos pendientes.

    TMT 2026-06-02 dueña: 'quiero el resumen que imprimiamos'. Reemplaza
    el viejo flujo "Terminar y guardar" → XLSX. Ahora es un download
    on-demand: NO cierra sesión, NO promueve nada a histos, solo genera
    el listado actual (histos sin conciliar + extracto no matcheado) con
    el resumen contable al pie. Se puede pedir cuantas veces se quiera.
    """
    no_banco = _BANCO_PICHINCHA
    sesion = _sesion.sesion_abierta(no_banco)
    if not sesion:
        flash("No hay sesión abierta — subí un extracto primero.", "info")
        return redirect(url_for("conciliacion.hub"))
    balance = _bp.calcular(no_banco)
    try:
        path = _generar_xlsx_pendientes(sesion, balance)
    except Exception as e:
        _LOG.exception("resumen xlsx falló: %s", e)
        flash(f"No pude generar el resumen: {e}", "error")
        return redirect(url_for("conciliacion.banco_post_procesar"))
    if not path:
        flash("openpyxl no disponible — no se pudo generar.", "error")
        return redirect(url_for("conciliacion.banco_post_procesar"))
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        abort(404)
    fname = f"conciliacion_pendientes_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        str(p),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=fname,
    )


# ─── Historial ────────────────────────────────────────────────────────


@conciliacion_bp.route("/banco-v2/recompute-saldos", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_recompute_saldos():
    """Recalcula la cadena de saldos completa desde la fila más temprana.

    Útil cuando se detecta descalce (por bugs históricos en
    crear/anular/insert). Walk-forward desde el primer mov del banco
    en Pichincha. Idempotente: si la cadena ya está OK, no cambia nada.
    """
    pre = _verificar_cadena_saldos(_BANCO_PICHINCHA)
    try:
        import bank_helpers
        with _db.tx() as conn:
            # TMT 2026-05-29 dueña: 'busca esos 9k de diferencia'. Bug encontrado:
            # recompute_saldos_desde filtra por id_transaccion >= ancla_id, pero
            # los movs no están en orden estricto por id (imports legacy de dBase
            # crearon filas con fecha tardía e id más bajo). Las filas con
            # fecha > ancla.fecha pero id < ancla.id quedaban FUERA del walk.
            # Fix: pasar ancla_fecha en lugar de ancla_id → filtra por fecha,
            # que sí es chronological y captura todas las filas.
            primera = _db.fetch_one(
                """
                SELECT fecha FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s AND fecha IS NOT NULL
                 ORDER BY fecha ASC LIMIT 1
                """,
                (_BANCO_PICHINCHA,),
                conn=conn,
            )
            if not primera or not primera.get("fecha"):
                flash("Sin movimientos para recalcular.", "info")
                return redirect(url_for("conciliacion.hub"))
            n = bank_helpers.recompute_saldos_desde(
                conn, no_banco=_BANCO_PICHINCHA, no_cta=None,
                ancla_fecha=primera["fecha"],
            )
    except Exception as e:
        _LOG.exception("recompute manual falló: %s", e)
        flash(f"Recalculo falló: {e}", "error")
        return redirect(url_for("conciliacion.hub"))

    post = _verificar_cadena_saldos(_BANCO_PICHINCHA)
    if pre.get("ok") and post.get("ok"):
        flash(
            f"✓ Cadena ya estaba coherente. Último saldo: ${post.get('ultimo_saldo', 0):,.2f}",
            "ok",
        )
    elif post.get("ok"):
        flash(
            f"✓ Cadena recalculada. Encontré {len(pre.get('problemas', []))} "
            f"descalces previos. Último saldo: ${post.get('ultimo_saldo', 0):,.2f}",
            "ok",
        )
    else:
        flash(
            f"⚠ Cadena recalculada pero siguen quedando {len(post.get('problemas', []))} "
            f"discrepancias. Revisar /banco-v2/verificar-saldos.",
            "warn",
        )
    return redirect(url_for("conciliacion.hub"))


@conciliacion_bp.route("/banco-v2/verificar-saldos", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_verificar_saldos():
    """Devuelve JSON con el estado de la cadena (para auditoría o UI)."""
    from flask import jsonify
    return jsonify(_verificar_cadena_saldos(_BANCO_PICHINCHA, limit=100))


@conciliacion_bp.route("/banco-v2/borrar-sesion", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_borrar_sesion():
    """Borra UNA sesión (la fila) + snapshots + matches + txs creadas
    durante esa sesión.

    Menos destructivo que reset-all: solo afecta UNA sesión a la vez.
    El borrar va por rango temporal (abierta_en..cerrada_en) — todos
    los matches y txs grupales creados dentro de ese rango se borran.
    Los pendientes históricos que se hayan conciliado en esa sesión
    vuelven a aparecer como pendientes.
    """
    try:
        sesion_id = int(request.form.get("sesion_id") or 0)
    except (TypeError, ValueError):
        sesion_id = 0
    if sesion_id <= 0:
        flash("ID de sesión inválido.", "error")
        return redirect(url_for("conciliacion.banco_historial_v2"))

    sesion = _sesion.sesion_por_id(sesion_id)
    if not sesion:
        flash("Sesión no encontrada.", "warn")
        return redirect(url_for("conciliacion.banco_historial_v2"))

    # TMT 2026-06-02 dueña: 'poneme boton anular conciliaciones y borrar'.
    # Sesión abierta también se puede borrar — usamos NOW() como cota
    # superior si no hay cerrada_en. Anula matches y limpia todo.
    usuario = _usuario_actual()
    abierta = sesion.get("abierta_en")
    cerrada = sesion.get("cerrada_en")
    if not cerrada:
        from datetime import datetime, timezone
        cerrada = datetime.now(timezone.utc)
    counts = {"matches": 0, "snapshots": 0, "txs_grupales": 0, "historicos_reset": 0}

    try:
        with _db.tx() as conn:
            # 1. Ids de txs creadas por conciliación dentro de esta sesión.
            ids_rows = _db.fetch_all(
                """
                SELECT DISTINCT t.id_transaccion
                  FROM scintela.transacciones_bancarias t
                  JOIN scintela.banco_conciliacion_match m
                       ON m.id_transaccion = t.id_transaccion
                 WHERE t.no_banco = %s
                   AND m.metodo IN ('created_from_real','created_from_real_grouped')
                   AND m.creado_en BETWEEN %s AND %s
                """,
                (_BANCO_PICHINCHA, abierta, cerrada),
                conn=conn,
            ) or []
            ids_grupales = [r["id_transaccion"] for r in ids_rows if r.get("id_transaccion")]

            # 2a. Borrar histos que ESTA sesión auto-promovió al cerrar.
            # BUG #4 fix 2026-05-29: filtrar por conciliado_en IS NULL para
            # NO borrar histos que después fueron conciliados en OTRA sesión
            # (perderíamos el rastro de un match legítimo).
            counts["historicos_promovidos_borrados"] = _db.execute(
                """
                DELETE FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s
                   AND fuente = %s
                   AND conciliado_en IS NULL
                """,
                (_BANCO_PICHINCHA, f"sesion:{sesion_id}"),
                conn=conn,
            ) or 0

            # 2b. Reset históricos QUE FUERON conciliados en este rango.
            counts["historicos_reset"] = _db.execute(
                """
                UPDATE scintela.banco_historicos_pendientes
                   SET conciliado_en = NULL,
                       conciliado_por = NULL,
                       conciliado_match_id = NULL
                 WHERE no_banco = %s
                   AND conciliado_en BETWEEN %s AND %s
                """,
                (_BANCO_PICHINCHA, abierta, cerrada),
                conn=conn,
            ) or 0

            # 3. Borrar matches creados durante esta sesión.
            # TMT 2026-05-29 dueña: 'si borro la conciliacion no se borraron
            # los matches'. Bug: el BETWEEN era estricto y a veces los
            # microsegundos de cerrada_en no atrapaban los últimos matches.
            # Fix: ventana ampliada con margen + capturar los id_transaccion
            # antes de borrar para resetear stat='*' del BANCSIS explícito
            # (incluso si vino de dbf-import).
            ids_bancsis_match = _db.fetch_all(
                """
                SELECT DISTINCT id_transaccion
                  FROM scintela.banco_conciliacion_match
                 WHERE no_banco = %s
                   AND creado_en >= %s - interval '5 seconds'
                   AND creado_en <= %s + interval '60 seconds'
                   AND id_transaccion IS NOT NULL
                """,
                (_BANCO_PICHINCHA, abierta, cerrada),
                conn=conn,
            ) or []
            ids_bancsis_sesion = [
                int(r["id_transaccion"]) for r in ids_bancsis_match
                if r.get("id_transaccion") is not None
            ]
            counts["matches"] = _db.execute(
                """
                DELETE FROM scintela.banco_conciliacion_match
                 WHERE no_banco = %s
                   AND creado_en >= %s - interval '5 seconds'
                   AND creado_en <= %s + interval '60 seconds'
                """,
                (_BANCO_PICHINCHA, abierta, cerrada),
                conn=conn,
            ) or 0
            # Reset stat='*' explícito para los BANCSIS que matcheaban con
            # esta sesión, sin importar el usuario_crea. La conciliación
            # fue borrada explícitamente — la marca tiene que irse también.
            counts["bancsis_stat_reset"] = 0
            if ids_bancsis_sesion:
                counts["bancsis_stat_reset"] = _db.execute(
                    """
                    UPDATE scintela.transacciones_bancarias
                       SET stat = NULL
                     WHERE no_banco = %s
                       AND id_transaccion = ANY(%s)
                       AND TRIM(COALESCE(stat, '')) = '*'
                       AND NOT EXISTS (
                           SELECT 1 FROM scintela.banco_conciliacion_match m
                            WHERE m.id_transaccion = scintela.transacciones_bancarias.id_transaccion
                              AND m.deshecho_en IS NULL
                       )
                    """,
                    (_BANCO_PICHINCHA, ids_bancsis_sesion),
                    conn=conn,
                ) or 0

            # 4. Borrar txs BANCSIS grupales + recompute.
            if ids_grupales:
                counts["txs_grupales"] = _db.execute(
                    "DELETE FROM scintela.transacciones_bancarias WHERE id_transaccion = ANY(%s) AND no_banco = %s",
                    (ids_grupales, _BANCO_PICHINCHA),
                    conn=conn,
                ) or 0
                try:
                    import bank_helpers
                    siguiente = _db.fetch_one(
                        """
                        SELECT id_transaccion FROM scintela.transacciones_bancarias
                         WHERE no_banco = %s
                         ORDER BY fecha ASC, id_transaccion ASC LIMIT 1
                        """,
                        (_BANCO_PICHINCHA,),
                        conn=conn,
                    )
                    if siguiente and siguiente.get("id_transaccion"):
                        bank_helpers.recompute_saldos_desde(
                            conn, no_banco=_BANCO_PICHINCHA, no_cta=None,
                            ancla_id=int(siguiente["id_transaccion"]),
                        )
                except Exception as e:
                    _LOG.warning("recompute_saldos en borrar-sesion falló: %s", e)

            # 5. Borrar snapshots de esta sesión.
            counts["snapshots"] = _db.execute(
                """
                DELETE FROM scintela.banco_saldo_conc_snapshot
                 WHERE no_banco = %s
                   AND (evento_ref = %s OR creado_en BETWEEN %s AND %s)
                """,
                (_BANCO_PICHINCHA, str(sesion_id), abierta, cerrada),
                conn=conn,
            ) or 0

            # 6. Borrar la fila de sesión.
            _db.execute(
                "DELETE FROM scintela.banco_conciliacion_sesion WHERE id = %s AND no_banco = %s",
                (sesion_id, _BANCO_PICHINCHA),
                conn=conn,
            )

            # 7. Reset stat='*' en movs PC fuera del dbf-import original.
            _db.execute(
                """
                UPDATE scintela.transacciones_bancarias
                   SET stat = NULL
                 WHERE no_banco = %s
                   AND TRIM(COALESCE(stat,'')) = '*'
                   AND COALESCE(usuario_crea,'') NOT IN ('dbf-import','asinfo-backfill')
                   AND NOT EXISTS (
                       SELECT 1 FROM scintela.banco_conciliacion_match m
                        WHERE m.id_transaccion = scintela.transacciones_bancarias.id_transaccion
                          AND m.deshecho_en IS NULL
                   )
                """,
                (_BANCO_PICHINCHA,),
                conn=conn,
            )

        # 8. Borrar el archivo XLSX si existe.
        if sesion.get("pdf_path"):
            try:
                path = Path(sesion["pdf_path"])
                if not path.is_absolute():
                    path = Path.cwd() / path
                if path.exists():
                    path.unlink()
            except Exception:
                pass

        _LOG.warning("BORRAR SESIÓN #%s por %s: %s", sesion_id, usuario, counts)
        flash(
            f"✓ Sesión #{sesion_id} borrada. "
            f"{counts['matches']} matches, "
            f"{counts.get('bancsis_stat_reset', 0)} BANCSIS desmarcados, "
            f"{counts['snapshots']} snapshots, "
            f"{counts['txs_grupales']} txs, "
            f"{counts['historicos_reset']} histo reseteados.",
            "ok",
        )
    except Exception as e:
        _LOG.exception("borrar sesión #%s falló: %s", sesion_id, e)
        flash(f"Borrar falló: {e}", "error")

    return redirect(url_for("conciliacion.banco_historial_v2"))


@conciliacion_bp.route("/banco-v2/reset-all", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_reset_all():
    """⚠️ NUCLEAR — borra TODAS las conciliaciones para arrancar de cero.

    TMT 2026-05-29 dueña: 'podes borrar todas las conciliaciones, quizas
    ponele un boton con 5 alertas por las dudas, asi hoy arrancamos de cero'.

    Hace, en una sola transacción atómica:
      1. DELETE de banco_conciliacion_match (matches + deshechos).
      2. DELETE de banco_saldo_conc_snapshot (snapshots).
      3. DELETE de banco_conciliacion_sesion (sesiones).
      4. DELETE de transacciones_bancarias creadas por conciliación
         (created_from_real / created_from_real_grouped).
      5. UPDATE banco_historicos_pendientes SET conciliado_en=NULL
         (vuelven a pendientes).
      6. UPDATE transacciones_bancarias SET stat=NULL WHERE stat='*'
         AND usuario_crea NOT IN ('dbf-import','asinfo-backfill')
         (movs PC vuelven a pendientes; los del DBF original mantienen
         su stat porque ya estaban conciliados antes del programa core).
      7. Snapshot 'reset_total' del nuevo saldo a conciliar.

    Frontend exige 5 confirmaciones (window.confirm) + 1 prompt textual
    'BORRAR TODO' antes de llegar acá.

    Requiere POST con `confirm_text=BORRAR TODO` para evitar accidentes
    si alguien dispara el endpoint sin pasar por el modal.
    """
    # BUG #11 fix 2026-05-29: comparación estricta, sin strip/upper —
    # exige el texto exacto y previene curl con espacios extra.
    confirm_text = request.form.get("confirm_text") or ""
    if confirm_text != "BORRAR TODO":
        flash("Reset cancelado — texto de confirmación incorrecto.", "error")
        return redirect(url_for("conciliacion.hub"))

    usuario = _usuario_actual()
    counts = {"matches": 0, "snapshots": 0, "sesiones": 0,
              "txs_grupales": 0, "historicos_reset": 0, "stat_reset": 0}
    try:
        with _db.tx() as conn:
            # 1. Ids de txs creadas por conciliación (para borrar tx + matches).
            ids_rows = _db.fetch_all(
                """
                SELECT DISTINCT t.id_transaccion
                  FROM scintela.transacciones_bancarias t
                  JOIN scintela.banco_conciliacion_match m
                       ON m.id_transaccion = t.id_transaccion
                 WHERE t.no_banco = %s
                   AND m.metodo IN ('created_from_real','created_from_real_grouped')
                """,
                (_BANCO_PICHINCHA,),
                conn=conn,
            ) or []
            ids_grupales = [r["id_transaccion"] for r in ids_rows if r.get("id_transaccion")]

            # 2a. Borrar todos los histos promovidos por sesiones del programa
            # (fuente LIKE 'sesion:%'). Los originales del banco se conservan.
            counts["historicos_promovidos_borrados"] = _db.execute(
                """
                DELETE FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %s AND fuente LIKE 'sesion:%%'
                """,
                (_BANCO_PICHINCHA,),
                conn=conn,
            ) or 0

            # 2b. Reset históricos pendientes originales del banco
            # (vuelven a no-conciliados).
            counts["historicos_reset"] = _db.execute(
                """
                UPDATE scintela.banco_historicos_pendientes
                   SET conciliado_en = NULL,
                       conciliado_por = NULL,
                       conciliado_match_id = NULL
                 WHERE no_banco = %s
                   AND conciliado_en IS NOT NULL
                """,
                (_BANCO_PICHINCHA,),
                conn=conn,
            ) or 0

            # 3. Borrar TODOS los matches (activos + deshechos).
            counts["matches"] = _db.execute(
                "DELETE FROM scintela.banco_conciliacion_match WHERE no_banco = %s",
                (_BANCO_PICHINCHA,),
                conn=conn,
            ) or 0

            # 4. Borrar TODAS las sesiones.
            counts["sesiones"] = _db.execute(
                "DELETE FROM scintela.banco_conciliacion_sesion WHERE no_banco = %s",
                (_BANCO_PICHINCHA,),
                conn=conn,
            ) or 0

            # 5. Borrar TODOS los snapshots (perderemos el histórico pero
            # es esperable cuando se hace 'arrancar de cero').
            counts["snapshots"] = _db.execute(
                "DELETE FROM scintela.banco_saldo_conc_snapshot WHERE no_banco = %s",
                (_BANCO_PICHINCHA,),
                conn=conn,
            ) or 0

            # 6. Borrar las txs BANCSIS que fueron creadas por conciliación.
            if ids_grupales:
                counts["txs_grupales"] = _db.execute(
                    "DELETE FROM scintela.transacciones_bancarias WHERE id_transaccion = ANY(%s) AND no_banco = %s",
                    (ids_grupales, _BANCO_PICHINCHA),
                    conn=conn,
                ) or 0
                # Recompute saldos desde el inicio de los borrados.
                try:
                    import bank_helpers
                    siguiente = _db.fetch_one(
                        """
                        SELECT id_transaccion FROM scintela.transacciones_bancarias
                         WHERE no_banco = %s
                         ORDER BY fecha ASC, id_transaccion ASC LIMIT 1
                        """,
                        (_BANCO_PICHINCHA,),
                        conn=conn,
                    )
                    if siguiente and siguiente.get("id_transaccion"):
                        bank_helpers.recompute_saldos_desde(
                            conn, no_banco=_BANCO_PICHINCHA, no_cta=None,
                            ancla_id=int(siguiente["id_transaccion"]),
                        )
                except Exception as e:
                    _LOG.warning("recompute_saldos en reset falló: %s", e)

            # 7. Reset stat='*' en movs PC que fueron marcados conciliados
            # por el flujo PC (NO los del dbf-import original).
            counts["stat_reset"] = _db.execute(
                """
                UPDATE scintela.transacciones_bancarias
                   SET stat = NULL
                 WHERE no_banco = %s
                   AND TRIM(COALESCE(stat,'')) = '*'
                   AND COALESCE(usuario_crea,'') NOT IN ('dbf-import','asinfo-backfill')
                """,
                (_BANCO_PICHINCHA,),
                conn=conn,
            ) or 0

        # Snapshot del nuevo saldo a conciliar (fuera de la tx).
        try:
            from modules.conciliacion import saldo_snapshot as _ss
            _ss.snapshot(
                _BANCO_PICHINCHA, "reset_total",
                evento_ref="reset", usuario=usuario,
                descripcion=f"reset total: {counts}",
            )
        except Exception:
            pass

        _LOG.warning("RESET TOTAL por %s: %s", usuario, counts)
        flash(
            f"✓ Reset completo. {counts['matches']} matches, "
            f"{counts['sesiones']} sesiones, {counts['snapshots']} snapshots, "
            f"{counts['txs_grupales']} txs, {counts['historicos_reset']} histo, "
            f"{counts['stat_reset']} stat reseteados.",
            "ok",
        )
    except Exception as e:
        _LOG.exception("RESET falló: %s", e)
        flash(f"Reset falló: {e}", "error")

    return redirect(url_for("conciliacion.hub"))


@conciliacion_bp.route("/banco-v2/historial", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_historial_v2():
    """Listado simple de sesiones para auditoría.

    TMT 2026-06-02: sesión no se cierra → la única fila 'activa' por
    banco es la sesión vigente. El resto son sesiones cerradas (legacy
    pre-mig-0062). Sin columnas de saldo inicial/final/diferencia: eso
    se ve live en la pantalla principal.
    """
    r = _migracion_lista_o_redirect()
    if r: return r
    sesiones = _sesion.listar_sesiones(no_banco=_BANCO_PICHINCHA, limit=200)
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

    # Pre-snapshot del último saldo y validación de cadena.
    pre = _verificar_cadena_saldos(_BANCO_PICHINCHA)
    saldo_pre = pre.get("ultimo_saldo")
    delta_esperado = -_signed_delta(tx_row.get("documento"), float(tx_row.get("importe") or 0))

    # 2+3) Hard-delete de matches + DELETE de tx + recompute, todo en una
    # sola transacción atómica.
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

    # 6) Validación post-anular: el saldo debe haber cambiado en el monto
    # esperado (delta_esperado). Si la diferencia con la realidad es
    # mayor al threshold, log CRITICAL — indica corrupción previa o
    # bug en recompute_saldos_desde.
    post = _verificar_cadena_saldos(_BANCO_PICHINCHA)
    saldo_post = post.get("ultimo_saldo")
    if not post.get("ok"):
        _LOG.critical(
            "ANULAR GRUPO tx=%s: cadena CORRUPTA post-anular. Pre OK=%s. "
            "Problemas=%s",
            id_tx, pre.get("ok"), post.get("problemas"),
        )
        flash(
            "⚠ Cadena de saldos descalibrada — corré recompute desde "
            "el botón 'Verificar y recalcular saldos'.",
            "warn",
        )
    elif saldo_pre is not None and saldo_post is not None:
        diff_real = round(saldo_post - saldo_pre, 2)
        if abs(diff_real - delta_esperado) > 0.5:
            _LOG.critical(
                "ANULAR GRUPO tx=%s: saldo cambió %.2f pero el delta "
                "esperado era %.2f (importe=%s, doc=%s). Pre OK=%s, Post OK=%s.",
                id_tx, diff_real, delta_esperado,
                tx_row.get("importe"), tx_row.get("documento"),
                pre.get("ok"), post.get("ok"),
            )
            flash(
                f"⚠ Saldo cambió ${diff_real:+,.2f} (esperado ${delta_esperado:+,.2f}). "
                f"Indica corrupción previa. Usá 'Verificar y recalcular saldos'.",
                "warn",
            )

    flash(
        f"Movimiento agrupado #{id_tx} anulado: tx borrada, "
        f"{n_matches} match(es) deshechos, saldos recalculados.",
        "ok",
    )
    return redirect(url_for("conciliacion.banco_deshacer_v2"))
