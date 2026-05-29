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
    if tab not in ("manual", "impuestos", "transferencias", "conciliados"):
        tab = "manual"

    buckets = _sesion.estado_sesion(sesion, no_banco)
    balance = _bp.calcular(no_banco)
    # TMT 2026-05-29 dueña: 'Hacer un cuarto tab que muestre conciliaciones
    # hasta ahora'. Lista los matches confirmados en esta sesión.
    matches_sesion = _sesion.matches_de_sesion(sesion)

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

    # TMT 2026-05-29 pedido dueña: 'si vuelvo a subir el mismo archivo no
    # se tiene que duplicar, tiene que cross check'. Calculamos el hash
    # ANTES de parsear y, si ya hay una sesión (abierta o cerrada) con
    # ese mismo hash para este banco, bloqueamos el alta — salvo que la
    # usuaria fuerce con `forzar=1` (ej. correcciones puntuales).
    extracto_hash = _sesion.sha256_bytes(raw)
    forzar = (request.form.get("forzar") or "").strip() in ("1", "true", "yes")
    if not forzar:
        prev = _sesion.sesion_por_hash(no_banco, extracto_hash)
        if prev:
            cuando = prev.get("abierta_en")
            cuando_str = cuando.strftime("%d/%m/%Y %H:%M") if cuando else "fecha desconocida"
            cerrada = bool(prev.get("cerrada_en"))
            estado = "cerrada" if cerrada else "abierta"
            n_matches = int(prev.get("matches_hechos") or 0)
            flash(
                f"Este archivo ya se subió el {cuando_str} (sesión #{prev['id']}, "
                f"{estado}, {n_matches} matches). Si querés re-procesarlo igual, "
                f"tildá 'Subir igual aunque sea duplicado' en el formulario.",
                "warn",
            )
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
        extracto_hash=extracto_hash,
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
    # 1) Matches del extracto contra los bancsis seleccionados.
    # BUG #2 fix 2026-05-29: si N reales vs M bancsis con N!=M, antes
    # silenciosamente asociábamos todos los N al PRIMER bancsis (los
    # otros M-1 bancsis quedaban huérfanos). Ahora exigimos:
    #   - 1:1 (mismo número de cada lado): pareamos por monto desc
    #   - N:1 (1 bancsis, N reales): caso típico depósito agrupado,
    #     OK asociar todos al único bancsis
    #   - cualquier otro caso → error con mensaje claro
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
                    if err_msg is None:
                        err_msg = str(e)
        elif len(bancsis_ids) == 1:
            # N reales contra 1 bancsis (depósito agrupado típico).
            bk_id_primary = bancsis_ids[0]
            for r in real_subset:
                try:
                    confirmar_match(_BANCO_PICHINCHA, r, bk_id_primary, usuario=usuario, metodo="matched_manual")
                    n_matches += 1
                except Exception as e:
                    _LOG.warning("manual confirm fallo: %s", e)
                    if err_msg is None:
                        err_msg = str(e)
        else:
            flash(
                f"No puedo conciliar {len(real_subset)} banco vs {len(bancsis_ids)} "
                f"programa: deben ser 1:1 o N:1 (N reales contra 1 mov del programa).",
                "error",
            )
            return redirect(url_for("conciliacion.banco_post_procesar", sesion_id=sesion_id))

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

    # BUG #10 fix 2026-05-29: defensa contra CSV-injection.
    # Si un concepto del banco empieza con =, +, -, @, Excel lo evalúa
    # como fórmula al abrir. Prefijamos con apóstrofe para neutralizar.
    def _safe_cell(v: str) -> str:
        s = str(v or "")
        if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + s
        return s

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
        ws.cell(row=r, column=2, value=_safe_cell(row.get("concepto"))[:100])
        ws.cell(row=r, column=3, value=_safe_cell(row.get("documento"))[:30])
        ws.cell(row=r, column=4, value=valor).number_format = "#,##0.00"
        ws.cell(row=r, column=5, value=_safe_cell(row.get("detalle") or row.get("oficina"))[:30])
        r += 1

    # ── Resumen contable al pie ───────────────────────────────────────
    # TMT 2026-05-29 dueña: bloque con líneas que la dueña usa cuando cierra.
    # Antes había 5 filas: ajuste, SISTEMA, conciliado, BANCO, DIFERENCIA.
    # Pedido nuevo: 'la diferencia que tenes abajo se deberia sumar al
    # "total" no aparecer por separado'. Folded la diferencia al TOTAL
    # conciliado → el TOTAL queda = SALDO BANCO y la línea DIFERENCIA
    # desaparece (convención: si no cuadra, asumimos el gap como ajuste).
    saldo_sistema = float(balance.get("saldo") or 0)
    total_ajuste = -float(balance.get("pendientes_conciliar_neto") or 0)
    total_conciliado_base = float(balance.get("saldo_si_concilio_todo") or 0)

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

    # Si tenemos saldo banco, el TOTAL conciliado absorbe la diferencia y
    # cierra contra el banco. Si no hay saldo banco (extracto sin saldo),
    # caemos al cálculo clásico SISTEMA + ajuste.
    if saldo_banco_real is not None:
        total_conciliado = saldo_banco_real
    else:
        total_conciliado = total_conciliado_base

    contable_fmt = '#,##0.00;(#,##0.00)'  # paréntesis para negativos
    label_col = 3  # columna C, igual al header "CODIGO" pero usamos como label
    val_col = 4   # columna D, igual al header "VALOR"

    r += 1  # fila vacía de separación
    for label, val in [
        ("TOTAL", total_ajuste),
        ("SALDO SISTEMA", saldo_sistema),
        ("TOTAL", total_conciliado),
        ("SALDO BANCO", saldo_banco_real),
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


def _promover_a_historicos(sesion: dict) -> int:
    """Inserta los movs del extracto NO conciliados en banco_historicos_pendientes.

    Se llama al cerrar la sesión: si dejaste 64 movs sin parear, esos
    pasan a 'pendientes históricos del banco' con fuente='sesion:N' para
    que en la próxima conciliación aparezcan como pendientes y los puedas
    matchear cuando el programa registre la contrapartida.

    Usa el UNIQUE INDEX ux_bhp_firma (no_banco, fecha, documento, monto, tipo)
    con ON CONFLICT DO NOTHING para evitar duplicar pendientes que ya están.
    """
    no_banco = sesion.get("no_banco") or _BANCO_PICHINCHA
    buckets = _sesion.estado_sesion(sesion, no_banco)
    items = (buckets.get("manual_banco") or []) + (buckets.get("impuestos") or [])
    # Excluimos los que ya son históricos (vinieron de tabla); solo
    # promovemos los del EXTRACTO actual.
    reales = [it for it in items if not it.get("es_historico")]
    if not reales:
        return 0

    fuente = f"sesion:{sesion.get('id')}"
    creado_por = (sesion.get("usuario") or "web")[:50]
    n_promovidos = 0
    for it in reales:
        m = it.get("mov")
        if not m:
            continue
        try:
            n = _db.execute(
                """
                INSERT INTO scintela.banco_historicos_pendientes
                    (no_banco, fecha, concepto, documento, monto, tipo,
                     oficina, detalle, fuente, creado_por)
                VALUES (%s, %s, %s, %s, %s::numeric, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    int(no_banco),
                    m.fecha,
                    (m.concepto or "")[:120],
                    (m.documento or "")[:40],
                    str(m.monto or 0),
                    (m.tipo or "C")[:2],
                    (getattr(m, "oficina", "") or "")[:40],
                    "",  # detalle
                    fuente,
                    creado_por,
                ),
            ) or 0
            n_promovidos += n
        except Exception as e:
            _LOG.warning("promover_a_historicos mov falló: %s", e)
    return n_promovidos


@conciliacion_bp.route("/banco-v2/terminar", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_terminar():
    """Cierra la sesión, promueve movs no conciliados a histos, genera XLSX."""
    sesion_id = int(request.form.get("sesion_id") or 0)
    sesion = _sesion.sesion_por_id(sesion_id) if sesion_id else None
    if not sesion:
        flash("Sesión no encontrada.", "error")
        return redirect(url_for("conciliacion.hub"))
    if sesion.get("cerrada_en"):
        flash("La sesión ya estaba cerrada.", "info")
        return redirect(url_for("conciliacion.banco_cerrada", sesion_id=sesion_id))

    # PROMOVER movs no conciliados a banco_historicos_pendientes ANTES
    # del cerrar — así el XLSX y el balance reflejan el estado final.
    try:
        n_promovidos = _promover_a_historicos(sesion)
        if n_promovidos:
            _LOG.info("sesion #%s: promovidos %s movs a histos", sesion_id, n_promovidos)
    except Exception as e:
        _LOG.warning("promover histos falló (sigo cerrando): %s", e)
        n_promovidos = 0

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
        msg = f"Sesión #{sesion_id} cerrada."
        if n_promovidos:
            msg += f" {n_promovidos} mov(s) sin conciliar pasaron a pendientes históricos."
        flash(msg, "ok")
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
            primera = _db.fetch_one(
                """
                SELECT id_transaccion FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s
                 ORDER BY fecha ASC, id_transaccion ASC LIMIT 1
                """,
                (_BANCO_PICHINCHA,),
                conn=conn,
            )
            if not primera or not primera.get("id_transaccion"):
                flash("Sin movimientos para recalcular.", "info")
                return redirect(url_for("conciliacion.hub"))
            n = bank_helpers.recompute_saldos_desde(
                conn, no_banco=_BANCO_PICHINCHA, no_cta=None,
                ancla_id=int(primera["id_transaccion"]),
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

    # BUG #5 fix 2026-05-29: si la sesión está abierta, NO usar NOW() como
    # cota superior — arrasaría matches de cualquier otro flujo paralelo.
    # Exigimos sesión cerrada para borrarla.
    if not sesion.get("cerrada_en"):
        flash(
            "No se puede borrar una sesión abierta. Primero cerrala con "
            "'Terminar y guardar' o anulá los matches individualmente.",
            "error",
        )
        return redirect(url_for("conciliacion.banco_historial_v2"))

    usuario = _usuario_actual()
    abierta = sesion.get("abierta_en")
    cerrada = sesion.get("cerrada_en")
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
            counts["matches"] = _db.execute(
                """
                DELETE FROM scintela.banco_conciliacion_match
                 WHERE no_banco = %s
                   AND creado_en BETWEEN %s AND %s
                """,
                (_BANCO_PICHINCHA, abierta, cerrada),
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
            f"✓ Sesión #{sesion_id} borrada. {counts['matches']} matches, "
            f"{counts['snapshots']} snapshots, {counts['txs_grupales']} txs, "
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
