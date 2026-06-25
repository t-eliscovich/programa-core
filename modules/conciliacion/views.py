"""Rutas del módulo conciliación."""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from filters import today_ec


def _usuario_actual() -> str:
    """Username del user logueado, fallback 'conciliacion' si no hay g.user.

    TMT 2026-05-26 dueña: el historial decía 'conciliacion' como usuario
    en lugar de quien realmente confirmó (ej. 'alex'). Centralizamos
    para que TODAS las rutas del módulo usen g.user.username.
    """
    try:
        u = (g.user or {}).get("username") if g.user else None
        return u or (request.remote_user or "conciliacion")
    except Exception:
        return request.remote_user or "conciliacion"

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from modules.conciliacion import queries
from modules.conciliacion.matcher import matchear
from modules.conciliacion.matcher_banco import (
    candidatos_match_manual,
    confirmar_bancsis_only,
    confirmar_match,
    confirmar_real_only,
    crear_transaccion_agrupada_desde_reals,
    crear_transaccion_desde_real,
    match_manual,
    matchear_extracto_banco,
    romper_match_grupo,
)
from modules.conciliacion.matcher_banco import (
    historial as historial_matches,
)
from modules.conciliacion.matcher_banco import (
    movimientos_banco as movimientos_banco_q,
)
from modules.conciliacion.matcher_depositos import (
    matchear_depositos,
    transacciones_en_rango,
)
from modules.conciliacion.parser import parse_csv
from modules.conciliacion.parser_banco import parse_banco_xlsx
from modules.conciliacion.parser_xlsx import parse_xlsx

conciliacion_bp = Blueprint(
    "conciliacion",
    __name__,
    url_prefix="/conciliacion",
    template_folder="templates",
)

_SESSION_KEY = "_conciliacion_sospechosos"
_SESSION_DEP = "_conciliacion_depositos_resultado"
_BANCO_PICHINCHA = 10  # /bancos/10 — confirmado 2026-05-22


@conciliacion_bp.route("/", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def index():
    """GET = formulario de upload; POST = procesa el CSV.

    Resultado: se guarda la lista de sospechosos en `session` (sólo ids +
    flags, no objetos pesados) y se redirige al visor.
    """
    if request.method == "POST":
        f = request.files.get("estado_cuenta")
        if not f or not f.filename:
            flash("Subí un archivo CSV del banco.", "warn")
            return redirect(url_for("conciliacion.index"))
        raw = f.read()
        lineas = parse_csv(raw)
        if not lineas:
            flash("El CSV no tenía líneas válidas. ¿Lo exportaste con fechas DD/MM/YYYY?", "warn")
            return redirect(url_for("conciliacion.index"))

        # Rango: desde la fecha más antigua del CSV, hasta la más nueva.
        fechas = [ln.fecha for ln in lineas if ln.fecha]
        desde = min(fechas) - timedelta(days=3) if fechas else today_ec() - timedelta(days=45)
        hasta = max(fechas) if fechas else today_ec()

        cheques = queries.cheques_depositados_rango(desde, hasta)
        result = matchear(lineas, cheques, dias_sospecha=3)

        # Guardar sólo lo minimal en la session para el paso siguiente.
        session[_SESSION_KEY] = [
            {
                "id_cheque": s["id_cheque"],
                "no_cheque": s["no_cheque"],
                "importe": float(s["importe"]),
                "codigo_cli": s["codigo_cli"],
                "fechad": s["fechad"].isoformat() if s["fechad"] else None,
                "dias": s["dias_sin_match"],
                "razon": s["razon"],
            }
            for s in result.sospechosos
        ]

        return render_template(
            "conciliacion/resultado.html",
            matches=result.matches,
            sospechosos=result.sospechosos,
            n_lineas=len(lineas),
            desde=desde,
            hasta=hasta,
        )

    return render_template("conciliacion/upload.html")


@conciliacion_bp.route("/confirmar-rebote", methods=["POST"])
@requiere_login
@requiere_permiso("cheques.anular")
def confirmar_rebote():
    """Confirma UN cheque como rebotado — dispara `cheques.reversar()`.

    La contadora revisa la bandeja de sospechosos y confirma uno por uno. NO
    hacemos bulk-confirm a propósito: queremos que alguien mire y decida cada
    caso (a veces el banco está atrasado, no es un rebote).
    """
    from modules.cheques import queries as chq

    try:
        id_cheque = int(request.form["id_cheque"])
    except (KeyError, ValueError, TypeError):
        flash("Falta el id_cheque.", "error")
        return redirect(url_for("conciliacion.index"))

    motivo = (request.form.get("motivo") or "Rechazado por banco (conciliación)").strip()
    try:
        res = chq.reversar(
            id_cheque=id_cheque,
            motivo=motivo,
            usuario=_usuario_actual(),
        )
    except ValueError as e:
        flash_exc("No se pudo reversar el cheque", e)
        return redirect(url_for("conciliacion.index"))

    if res.get("stop_aplicado"):
        flash(f"Cheque #{id_cheque} reversado. Cliente a STOP por rebote real.", "ok")
    else:
        flash(f"Cheque #{id_cheque} reversado (sin side-effect de stop).", "ok")

    # Remover el cheque confirmado de la bandeja en session
    sospechosos = session.get(_SESSION_KEY, [])
    session[_SESSION_KEY] = [s for s in sospechosos if s.get("id_cheque") != id_cheque]

    return redirect(url_for("conciliacion.bandeja"))


@conciliacion_bp.route("/bandeja")
@requiere_login
@requiere_permiso("bancos.conciliar")
def bandeja():
    """Muestra la bandeja de sospechosos guardada en session."""
    sospechosos = session.get(_SESSION_KEY, [])
    return render_template("conciliacion/bandeja.html", sospechosos=sospechosos)


# ─── Hub (legacy menu — sustituido por la conciliación bancaria) ──────────
# TMT 2026-05-22 — la dueña pidió UNA SOLA pantalla. /hub ahora hace el
# upload+match bidireccional contra el extracto Pichincha. La vista nueva
# vive abajo bajo el nombre `hub()`. Esta nota queda como marcador.


# ─── Depósitos pendientes — el flow nuevo (xlsx + match contra banco) ──────
@conciliacion_bp.route("/depositos", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def depositos():
    """Upload de Excel con DEPÓSITOS PENDIENTES + match contra el banco.

    GET  → muestra el formulario de upload (paso 1 del wizard).
    POST → parsea el Excel, matchea contra `scintela.transacciones_bancarias`,
           guarda el resultado en session, y muestra la pantalla de resultados.
    """
    if request.method == "POST":
        f = request.files.get("archivo")
        if not f or not f.filename:
            flash("Subí un Excel (.xlsx) con los depósitos pendientes.", "warn")
            return redirect(url_for("conciliacion.depositos"))
        raw = f.read()
        try:
            deps = parse_xlsx(raw)
        except Exception as e:
            flash_exc("No se pudo leer el Excel", e)
            return redirect(url_for("conciliacion.depositos"))
        if not deps:
            flash("El Excel no contenía depósitos válidos. Revisá que tenga columnas FECHA y VALOR.", "warn")
            return redirect(url_for("conciliacion.depositos"))

        # Rango de fechas: ± 30 días del rango de los depósitos.
        # TMT decisión 2026-06-03: si TODO el xlsx es sin fecha, usamos rango
        # default (últimos 90 días) en vez de abortar. Las filas sin fecha
        # caen automáticamente a rojo en el matcher (ya tenía esa lógica).
        fechas = [d.fecha for d in deps if d.fecha]
        if not fechas:
            hasta = today_ec()
            desde = hasta - timedelta(days=90)
            flash(
                f"Atención: {len(deps)} depósito(s) sin fecha — se cargan con flag "
                "'sin fecha' y van a categoría roja por defecto.",
                "warn",
            )
        else:
            desde = min(fechas) - timedelta(days=30)
            hasta = max(fechas) + timedelta(days=30)
            if hasta > today_ec():
                hasta = today_ec()

        movs = transacciones_en_rango(desde, hasta)
        resultado = matchear_depositos(deps, movs, dias_tolerancia=5)

        # Persistir resultado minimal en session para acciones posteriores.
        # Incluye `firma_dep` para que las acciones manuales del usuario
        # se puedan correlacionar con la fila.
        filas_session = []
        for f in resultado.filas:
            firma = queries.firma_deposito(
                f.deposito.fecha,
                f.deposito.valor,
                f.deposito.codigo,
                f.deposito.concepto,
            )
            filas_session.append(
                {
                    "firma_dep": firma,
                    "fecha": (f.deposito.fecha.isoformat() if f.deposito.fecha else None),
                    # TMT 2026-06-03: flag explícito para que la UI pueda mostrar
                    # un badge "sin fecha" en vez de un "—" silencioso.
                    "sin_fecha": f.deposito.fecha is None,
                    "concepto": f.deposito.concepto,
                    "codigo": f.deposito.codigo,
                    "valor": float(f.deposito.valor),
                    "detalle": f.deposito.detalle,
                    "hoja": f.deposito.hoja,
                    "estado": f.estado,
                    "razon": f.razon,
                    "match_id": f.match.id_transaccion if f.match else None,
                    "match_fecha": (f.match.fecha.isoformat() if (f.match and f.match.fecha) else None),
                    "match_concepto": f.match.concepto if f.match else "",
                    "match_documento": f.match.documento if f.match else "",
                    "match_no_banco": f.match.no_banco if f.match else None,
                }
            )
        session[_SESSION_DEP] = {
            "filas": filas_session,
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
        }

        # Decoramos cada fila con el último estado manual (si lo hay).
        firmas = [x["firma_dep"] for x in filas_session]
        estado_manual = queries.estado_actual_depositos(firmas)
        for fila in filas_session:
            fila["estado_manual"] = estado_manual.get(fila["firma_dep"], {}).get("accion")

        return render_template(
            "conciliacion/depositos_resultado.html",
            resultado_session=session[_SESSION_DEP],
            desde=desde,
            hasta=hasta,
            n_filas=len(filas_session),
        )

    return render_template("conciliacion/depositos_upload.html")


@conciliacion_bp.route("/depositos/resultado")
@requiere_login
@requiere_permiso("bancos.conciliar")
def depositos_resultado():
    """Re-muestra el último resultado guardado en session (post-redirect)."""
    data = session.get(_SESSION_DEP)
    if not data:
        flash("No hay resultados guardados. Subí un Excel para comenzar.", "warn")
        return redirect(url_for("conciliacion.depositos"))

    # Re-fetchear estados manuales en cada vista (puede haber cambiado desde
    # otra pestaña / otro usuario).
    firmas = [f["firma_dep"] for f in data.get("filas") or [] if f.get("firma_dep")]
    estado_manual = queries.estado_actual_depositos(firmas)
    for fila in data.get("filas") or []:
        firma = fila.get("firma_dep")
        fila["estado_manual"] = estado_manual.get(firma, {}).get("accion") if firma else None

    return render_template(
        "conciliacion/depositos_resultado.html",
        resultado_session=data,
        desde=data.get("desde"),
        hasta=data.get("hasta"),
        n_filas=len(data.get("filas") or []),
    )


@conciliacion_bp.route("/depositos/limpiar", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def depositos_limpiar():
    """Borra el resultado guardado en session (botón 'Empezar de nuevo')."""
    session.pop(_SESSION_DEP, None)
    return redirect(url_for("conciliacion.depositos"))


# ─── Marcar UNA fila: ✅ confirmar o ❌ rechazar ────────────────────────────
@conciliacion_bp.route("/depositos/marcar", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def depositos_marcar():
    """Marca un depósito como confirmado/rechazado.

    Espera (form-encoded o JSON):
        firma_dep : str (la del session)
        accion    : 'confirmado' | 'rechazado' | 'pendiente'
        nota      : str (opcional)
    """
    data = session.get(_SESSION_DEP) or {}
    firmas = {f["firma_dep"]: f for f in (data.get("filas") or []) if f.get("firma_dep")}

    firma = (request.form.get("firma_dep") or "").strip()
    accion = (request.form.get("accion") or "").strip()
    nota = (request.form.get("nota") or "").strip()
    if not firma or firma not in firmas:
        flash("Fila no encontrada en la sesión actual. Subí el Excel de nuevo.", "warn")
        return redirect(url_for("conciliacion.depositos"))
    if accion not in ("confirmado", "rechazado", "pendiente"):
        flash(f"Acción inválida: {accion!r}", "error")
        return redirect(url_for("conciliacion.depositos_resultado"))

    fila = firmas[firma]
    from datetime import date as _date

    fdep = None
    if fila.get("fecha"):
        try:
            fdep = _date.fromisoformat(fila["fecha"])
        except ValueError:
            fdep = None
    try:
        queries.marcar_deposito(
            firma_dep=firma,
            fecha_dep=fdep,
            valor_dep=float(fila.get("valor") or 0),
            codigo_dep=fila.get("codigo") or "",
            concepto_dep=fila.get("concepto") or "",
            accion=accion,
            id_transaccion=fila.get("match_id"),
            nota=nota,
            usuario=_usuario_actual(),
        )
    except Exception as e:
        flash_exc("No se pudo marcar la fila", e)
        return redirect(url_for("conciliacion.depositos_resultado"))

    return redirect(url_for("conciliacion.depositos_resultado") + f"#fila-{firma}")


@conciliacion_bp.route("/depositos/confirmar-verdes", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def depositos_confirmar_verdes():
    """Confirma TODOS los matches verdes en bloque.

    Acción rápida: solo los que el sistema cree exact-match y que el
    usuario no haya rechazado previamente. Si alguno ya fue confirmado
    o rechazado manualmente, se respeta esa decisión.
    """
    data = session.get(_SESSION_DEP) or {}
    filas = data.get("filas") or []
    firmas = [f["firma_dep"] for f in filas if f.get("firma_dep")]
    estado_manual = queries.estado_actual_depositos(firmas)

    confirmados = 0
    saltados = 0
    for fila in filas:
        firma = fila.get("firma_dep")
        if not firma:
            continue
        if fila.get("estado") != "verde":
            continue
        if estado_manual.get(firma, {}).get("accion"):
            saltados += 1
            continue
        from datetime import date as _date

        fdep = None
        if fila.get("fecha"):
            try:
                fdep = _date.fromisoformat(fila["fecha"])
            except ValueError:
                pass
        try:
            queries.marcar_deposito(
                firma_dep=firma,
                fecha_dep=fdep,
                valor_dep=float(fila.get("valor") or 0),
                codigo_dep=fila.get("codigo") or "",
                concepto_dep=fila.get("concepto") or "",
                accion="confirmado",
                id_transaccion=fila.get("match_id"),
                nota="bulk: confirmar todos los verdes",
                usuario=_usuario_actual(),
            )
            confirmados += 1
        except Exception:
            pass
    flash(
        f"Confirmados {confirmados} matches verdes. ({saltados} ya tenían decisión previa, no se tocaron.)",
        "ok",
    )
    return redirect(url_for("conciliacion.depositos_resultado"))


# ═══════════════════════════════════════════════════════════════════════════
# /conciliacion/banco — TMT 2026-05-22
# Conciliación bidireccional: extracto Pichincha (.xlsx) ↔ BANCSIS.
# Muestra movimientos en REAL no en BANCSIS, BANCSIS no en REAL, matches.
# ═══════════════════════════════════════════════════════════════════════════


def _cat_to_dict(cat) -> dict:
    return {
        "codigo": getattr(cat, "codigo", "OTRO"),
        "grupo": getattr(cat, "grupo", "OTRO"),
        "label": getattr(cat, "label", "Sin categorizar"),
        "abrev": getattr(cat, "abrev", "?"),
        "cliente": getattr(cat, "cliente", "") or "",
        "cliente_nombre": getattr(cat, "cliente_nombre", "") or "",
        "descripcion": getattr(cat, "descripcion", "") or "",
        "fuente": getattr(cat, "fuente", "regex"),
    }


def _serialize_resultado_banco(res, no_banco: int) -> dict:
    """ConciliacionBanco → dict serializable para session."""
    # Las listas *_cats vienen del matcher con el mismo orden e índice.
    _cats_real = getattr(res, "real_only_cats", []) or [None] * len(res.real_only)
    _cats_bancsis = getattr(res, "bancsis_only_cats", []) or [None] * len(res.bancsis_only)
    _cats_match = getattr(res, "matches_cats", []) or [None] * len(res.matches)
    return {
        "no_banco": no_banco,
        "matches": [
            {
                "real": {
                    "fecha": m.real.fecha.isoformat() if m.real.fecha else None,
                    "concepto": m.real.concepto,
                    "documento": m.real.documento,
                    "monto": float(m.real.monto),
                    "tipo": m.real.tipo,
                    "codigo": m.real.codigo,
                    "oficina": m.real.oficina,
                },
                "bancsis": {
                    "id_transaccion": m.bancsis.id_transaccion,
                    "fecha": m.bancsis.fecha.isoformat() if m.bancsis.fecha else None,
                    "documento": m.bancsis.documento,
                    "concepto": m.bancsis.concepto,
                    "importe": m.bancsis.importe,
                    "numreferencia": m.bancsis.numreferencia,
                    "prov": m.bancsis.prov,
                    "prov_nombre": m.bancsis.prov_nombre,
                    "fecha_crea": m.bancsis.fecha_crea.isoformat() if m.bancsis.fecha_crea else None,
                    "doc_banco_rel": m.bancsis.doc_banco_rel,  # cheque.doc_banco editable inline
                },
                "score": m.score,
                "razon": m.razon,
                "es_exacto": m.score < 0.01,
                "cat": _cat_to_dict(_cats_match[i]) if i < len(_cats_match) and _cats_match[i] else _cat_to_dict(None),
                # TMT 2026-05-26 dueña: componentes para match N-a-1
                # (1 depósito banco ↔ N cheques programa del mismo día).
                "componentes": list(getattr(m, "componentes", []) or []),
            }
            for i, m in enumerate(res.matches)
        ],
        "real_only": [
            {
                "fecha": r.fecha.isoformat() if r.fecha else None,
                "concepto": r.concepto,
                "documento": r.documento,
                "monto": float(r.monto),
                "tipo": r.tipo,
                "codigo": r.codigo,
                "oficina": r.oficina,
                "cat": _cat_to_dict(_cats_real[i]) if i < len(_cats_real) and _cats_real[i] else _cat_to_dict(None),
                "sugerencias": (getattr(res, "sugerencias_real_only", {}) or {}).get(i, []),
            }
            for i, r in enumerate(res.real_only)
        ],
        "bancsis_only": [
            {
                "id_transaccion": b.id_transaccion,
                "fecha": b.fecha.isoformat() if b.fecha else None,
                "documento": b.documento,
                "concepto": b.concepto,
                "importe": b.importe,
                "numreferencia": b.numreferencia,
                "tipo_real": b.tipo_real,
                "prov": b.prov,
                "prov_nombre": b.prov_nombre,
                "es_agrupado": b.es_agrupado,
                "n_cheques": b.n_cheques,
                "fecha_crea": b.fecha_crea.isoformat() if b.fecha_crea else None,
                "doc_banco_rel": b.doc_banco_rel,  # cheque.doc_banco editable inline
                "cat": _cat_to_dict(_cats_bancsis[i]) if i < len(_cats_bancsis) and _cats_bancsis[i] else _cat_to_dict(None),
            }
            for i, b in enumerate(res.bancsis_only)
        ],
        "extracto_desde": res.extracto_desde.isoformat() if res.extracto_desde else None,
        "extracto_hasta": res.extracto_hasta.isoformat() if res.extracto_hasta else None,
        "ventana_dias": res.ventana_dias,
        "bancsis_cargados": res.bancsis_cargados,
        "n_agrupados": len(getattr(res, "bancsis_agrupados", []) or []),
        "n_cheques_agrupados": sum(b.n_cheques for b in (getattr(res, "bancsis_agrupados", []) or [])),
        "monto_agrupados": sum(float(b.importe) for b in (getattr(res, "bancsis_agrupados", []) or [])),
        "saldo_real_final": float(res.saldo_real_final),
        "saldo_real_fecha": res.saldo_real_fecha.isoformat() if res.saldo_real_fecha else None,
        "saldo_bancsis_final": float(res.saldo_bancsis_final),
        "saldo_bancsis_fecha": res.saldo_bancsis_fecha.isoformat() if res.saldo_bancsis_fecha else None,
        "total_real_only_signed": float(res.total_real_only_signed),
        "total_bancsis_only_signed": float(res.total_bancsis_only_signed),
    }


def _calc_kpis(data: dict) -> dict:
    """Métricas derivadas para el template a partir del dict serializado."""
    # Saldos absolutos del banco (referencia, ya no se muestran en cards)
    saldo_real = data.get("saldo_real_final") or 0
    saldo_bancsis = data.get("saldo_bancsis_final") or 0
    sum_real_only = data.get("total_real_only_signed") or 0

    # Filtramos bancsis_only por fecha en el rango del extracto: los movs
    # FUTUROS al extracto (cargados con ventana ±15d para matching con drift)
    # no deben inflar el KPI 'Movimientos programa'.
    extracto_desde = data.get("extracto_desde")
    extracto_hasta = data.get("extracto_hasta")
    bancsis_only = data.get("bancsis_only") or []
    bancsis_only_en_rango = []
    for b in bancsis_only:
        f = b.get("fecha")
        if extracto_desde and extracto_hasta and f:
            if extracto_desde <= f <= extracto_hasta:
                bancsis_only_en_rango.append(b)
        else:
            bancsis_only_en_rango.append(b)

    sum_bancsis_only_periodo = 0.0
    bancsis_only_agrupados = []
    bancsis_only_no_agrupados_en_rango = []
    for b in bancsis_only_en_rango:
        if b.get("es_agrupado"):
            bancsis_only_agrupados.append(b)
            continue  # agrupados NO se cuentan — son N cheques sumados
        bancsis_only_no_agrupados_en_rango.append(b)
        signo = 1 if b.get("documento") in ("DE", "TR", "XX", "NC", "IN", "AC") else -1
        sum_bancsis_only_periodo += signo * float(b.get("importe") or 0)

    sum_bancsis_only = data.get("total_bancsis_only_signed") or 0

    # KPIs de conciliación: cuánto se movió EN EL PERIODO en cada lado.
    def _suma_firmada_matches(side: str) -> float:
        total = 0.0
        for m in (data.get("matches") or []):
            if side == "real":
                signo = 1 if m.get("real", {}).get("tipo") == "C" else -1
                total += signo * float(m.get("real", {}).get("monto") or 0)
            else:
                signo = 1 if m.get("bancsis", {}).get("documento") in ("DE", "TR", "XX", "NC", "IN", "AC") else -1
                total += signo * float(m.get("bancsis", {}).get("importe") or 0)
        return total

    mov_banco_matches = _suma_firmada_matches("real")
    mov_programa_matches = _suma_firmada_matches("bancsis")
    mov_banco = mov_banco_matches + sum_real_only
    mov_programa = mov_programa_matches + sum_bancsis_only_periodo

    diff = mov_banco - mov_programa
    return {
        "n_match": len(data.get("matches") or []),
        "n_real_only": len(data.get("real_only") or []),
        "n_bancsis_only": len(data.get("bancsis_only") or []),
        "n_bancsis_only_periodo": len(bancsis_only_no_agrupados_en_rango),
        "n_bancsis_only_agrupados": len(bancsis_only_agrupados),
        "saldo_real": saldo_real,
        "saldo_bancsis": saldo_bancsis,
        "sum_real_only": sum_real_only,
        "sum_bancsis_only": sum_bancsis_only,
        "sum_bancsis_only_periodo": sum_bancsis_only_periodo,
        "mov_banco_matches": mov_banco_matches,
        "mov_programa_matches": mov_programa_matches,
        "mov_banco": mov_banco,
        "mov_programa": mov_programa,
        "diff": diff,
        "cuadra": abs(diff) < 5,
    }


@conciliacion_bp.route("/hub", methods=["GET", "POST"])
@conciliacion_bp.route("/banco", methods=["GET", "POST"])  # alias compat
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub():
    """GET = upload. POST = parsea + matchea + RENDER DIRECTO (sin session).

    No usamos session para el resultado porque con 250+ movimientos supera
    el límite de la cookie Flask (~4KB) y silently drops. En cambio:
        - El resultado se renderiza inmediatamente en el POST.
        - Los datos necesarios para los botones "Confirmar matches" /
          "Aceptar bancsis-only" viajan inline en hidden form fields.
    """
    if request.method == "GET":
        try:
            bancos = queries.bancos_disponibles()
        except Exception:
            bancos = []
        try:
            ultimos = queries.ultimos_extractos(limit=5)
        except Exception:
            ultimos = []
        try:
            uploads = queries.uploads_recientes(limit=15)
        except Exception as _e:
            import logging
            logging.getLogger("programa_core.conciliacion").exception(
                "uploads_recientes falló: %s", _e
            )
            uploads = []
        # TMT 2026-05-27 dueña: 'Antes de empezar una conciliacion nueva,
        # me tiene que decir el saldo a conciliar'. Resumen previo:
        # cuántos movs Pichincha quedan SIN conciliar (stat != '*' AND
        # no match en banco_conciliacion_match) y su monto neto signado.
        try:
            import db as _db
            saldo_pre = _db.fetch_one(
                """
                WITH conciliados_pc AS (
                    SELECT DISTINCT id_transaccion
                      FROM scintela.banco_conciliacion_match
                     WHERE no_banco = %(no_banco)s
                       AND (deshecho_en IS NULL)
                       AND id_transaccion IS NOT NULL
                )
                SELECT COUNT(*)                                                  AS n_pendientes,
                       COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                         THEN -t.importe ELSE t.importe END), 0) AS saldo_pendiente,
                       MIN(t.fecha)                                              AS fecha_min,
                       MAX(t.fecha)                                              AS fecha_max
                  FROM scintela.transacciones_bancarias t
                  LEFT JOIN conciliados_pc cp ON cp.id_transaccion = t.id_transaccion
                 WHERE t.no_banco = %(no_banco)s
                   AND TRIM(COALESCE(t.stat, '')) <> '*'
                   AND cp.id_transaccion IS NULL
                """,
                {"no_banco": _BANCO_PICHINCHA},
            ) or {}
        except Exception as _e:
            import logging
            logging.getLogger("programa_core.conciliacion").exception(
                "saldo_pre conciliar falló: %s", _e
            )
            saldo_pre = {}

        # TMT 2026-05-27 dueña: 'quiero saber si el saldo a conciliar es
        # igual del dbase y del programa'. Comparamos:
        # - dBase pendientes = movs importados del DBF (usuario_crea=
        #   'dbf-import') con stat <> '*'
        # - PC pendientes = TODOS los movs sin stat='*' (incluye los del
        #   DBF + los creados en PC vía UI/conciliacion)
        # Si dBase == PC → PC no creó movs extra → sync limpio. Si PC > dBase
        # → hay movs en PC que el dBase no conoce.
        # Saldo a conciliar — coherente con Card 1 (saldo_pre):
        #   dBase pendiente = stat<>'*' (lo que el DBF no marcó conciliado)
        #   PC pendiente    = stat<>'*' AND sin match PC
        #     (= lo que TODAVÍA hay que conciliar, igual que Card 1)
        try:
            saldo_conc_split = _db.fetch_one(
                """
                WITH conciliados_pc AS (
                    SELECT DISTINCT id_transaccion
                      FROM scintela.banco_conciliacion_match
                     WHERE no_banco = %(no_banco)s
                       AND (deshecho_en IS NULL)
                       AND id_transaccion IS NOT NULL
                )
                SELECT
                  -- dBase: stat<>'*' (independientemente de PC matches)
                  COUNT(*) FILTER (WHERE TRIM(COALESCE(t.stat,'')) <> '*')
                                                                       AS n_dbase,
                  COALESCE(SUM(CASE WHEN TRIM(COALESCE(t.stat,'')) <> '*'
                                    THEN CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                              THEN -t.importe ELSE t.importe END
                                    ELSE 0 END), 0)                    AS saldo_dbase,
                  -- PC: stat<>'*' AND sin match PC (= Card 1 saldo_pre)
                  COUNT(*) FILTER (WHERE TRIM(COALESCE(t.stat,'')) <> '*'
                                     AND cp.id_transaccion IS NULL)
                                                                       AS n_pc,
                  COALESCE(SUM(CASE WHEN TRIM(COALESCE(t.stat,'')) <> '*'
                                     AND cp.id_transaccion IS NULL
                                    THEN CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                              THEN -t.importe ELSE t.importe END
                                    ELSE 0 END), 0)                    AS saldo_pc
                  FROM scintela.transacciones_bancarias t
                  LEFT JOIN conciliados_pc cp ON cp.id_transaccion = t.id_transaccion
                 WHERE t.no_banco = %(no_banco)s
                """,
                {"no_banco": _BANCO_PICHINCHA},
            ) or {}
            sdb = float(saldo_conc_split.get("saldo_dbase") or 0)
            spc = float(saldo_conc_split.get("saldo_pc") or 0)
            saldo_conc_split["diff"] = round(spc - sdb, 2)
        except Exception as _e:
            import logging
            logging.getLogger("programa_core.conciliacion").exception(
                "saldo_conc_split falló: %s", _e
            )
            saldo_conc_split = {}

        # TMT 2026-05-27 dueña: 'necesito que encuentre si tenes el saldo
        # del banco para conciliar en la base de dbase y entonces entendamos
        # si estamos al dia'. El dBase tiene running balance (campo SALDO)
        # y la última tx CON stat='*' = último conciliado oficial del dBase.
        # Saldo PC al mismo corte = saldo_stored del último mov con la misma
        # fecha. Comparamos: si coinciden → PC y dBase sincronizados al día
        # del último conciliado dBase.
        try:
            saldo_dbase = _db.fetch_one(
                """
                SELECT t.fecha, t.saldo, t.id_transaccion
                  FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = %(no_banco)s
                   AND TRIM(COALESCE(t.stat, '')) = '*'
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 1
                """,
                {"no_banco": _BANCO_PICHINCHA},
            ) or {}
            # Saldo PC al cierre de la misma fecha del último conciliado dBase
            saldo_pc_al_corte = 0.0
            if saldo_dbase.get("fecha"):
                row_pc = _db.fetch_one(
                    """
                    SELECT t.saldo
                      FROM scintela.transacciones_bancarias t
                     WHERE t.no_banco = %(no_banco)s
                       AND t.fecha <= %(fecha)s::date
                     ORDER BY t.fecha DESC, t.id_transaccion DESC
                     LIMIT 1
                    """,
                    {"no_banco": _BANCO_PICHINCHA, "fecha": saldo_dbase["fecha"]},
                ) or {}
                saldo_pc_al_corte = float(row_pc.get("saldo") or 0)
            saldo_dbase["saldo_pc_al_corte"] = saldo_pc_al_corte
            saldo_dbase["diferencia"] = round(
                saldo_pc_al_corte - float(saldo_dbase.get("saldo") or 0), 2
            )
        except Exception as _e:
            import logging
            logging.getLogger("programa_core.conciliacion").exception(
                "saldo_dbase comparativo falló: %s", _e
            )
            saldo_dbase = {}

        # TMT 2026-05-27 dueña: 'no me muestra esos numeros'. Card extra
        # con saldo PC al ÚLTIMO mov registrado (no al último conciliado
        # dBase). Eso es lo que ella espera ver — el saldo PC actualizado
        # hoy, no atrasado al último conciliado dBase.
        saldo_pc_actual = {}
        try:
            row_actual = _db.fetch_one(
                """
                SELECT t.fecha, t.saldo, t.id_transaccion
                  FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = %(no_banco)s
                   AND t.saldo IS NOT NULL
                   -- TMT 2026-06-25 (Alex/Tamara: dif 51.788,80 vs dBase): el
                   -- saldo actual no debe salir de una fila POSTDATADA (fecha
                   -- futura) con saldo viejo. Última fila con fecha <= hoy.
                   AND t.fecha <= CURRENT_DATE
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 1
                """,
                {"no_banco": _BANCO_PICHINCHA},
            ) or {}
            saldo_pc_actual = {
                "saldo": float(row_actual.get("saldo") or 0),
                "fecha": row_actual.get("fecha"),
                "id_transaccion": row_actual.get("id_transaccion"),
            }
            # TMT 2026-05-28 dueña: 'cuando yo hago una conciliacion que se
            # actualicce, si no no'. El "Saldo a conciliar" estable lo
            # tomamos del último snapshot guardado por evento de conciliación
            # (banco_saldo_conc_snapshot). Si no hay ningún snapshot todavía,
            # fallback al saldo de cierre del día anterior.
            try:
                from modules.conciliacion import saldo_snapshot as _ss
                ult_snap = _ss.ultimo(_BANCO_PICHINCHA)
                if ult_snap and ult_snap.get("saldo_conc") is not None:
                    saldo_pc_actual["saldo_a_conciliar_estable"] = float(ult_snap["saldo_conc"])
                    saldo_pc_actual["snapshot_evento"] = ult_snap.get("evento_tipo")
                    saldo_pc_actual["snapshot_fecha"] = ult_snap.get("creado_en")
                else:
                    saldo_pc_actual["saldo_a_conciliar_estable"] = None
            except Exception:
                saldo_pc_actual["saldo_a_conciliar_estable"] = None
            # Snapshot de cierre ayer como fallback.
            try:
                row_cierre_ayer = _db.fetch_one(
                    """
                    SELECT t.fecha, t.saldo, t.id_transaccion
                      FROM scintela.transacciones_bancarias t
                     WHERE t.no_banco = %(no_banco)s
                       AND t.saldo IS NOT NULL
                       AND t.fecha < CURRENT_DATE
                     ORDER BY t.fecha DESC, t.id_transaccion DESC
                     LIMIT 1
                    """,
                    {"no_banco": _BANCO_PICHINCHA},
                ) or {}
                saldo_pc_actual["saldo_cierre_ayer"] = float(
                    row_cierre_ayer.get("saldo") or 0
                )
                saldo_pc_actual["fecha_cierre_ayer"] = row_cierre_ayer.get("fecha")
            except Exception:
                saldo_pc_actual["saldo_cierre_ayer"] = None
                saldo_pc_actual["fecha_cierre_ayer"] = None
            # Pendientes históricos no conciliados (suma signada — credit suma,
            # débito resta). Estos NO están en transacciones_bancarias.
            # TMT 2026-05-28 dueña: separar entradas/salidas en lugar de
            # mostrar solo el neto.
            row_pend = _db.fetch_one(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN tipo = 'C' THEN monto ELSE -monto END), 0) AS neto_pend,
                  COALESCE(SUM(CASE WHEN tipo = 'C' THEN monto ELSE 0 END), 0) AS sum_cred,
                  COALESCE(SUM(CASE WHEN tipo = 'D' THEN monto ELSE 0 END), 0) AS sum_deb,
                  COALESCE(SUM(CASE WHEN tipo = 'C' THEN 1 ELSE 0 END), 0) AS n_cred,
                  COALESCE(SUM(CASE WHEN tipo = 'D' THEN 1 ELSE 0 END), 0) AS n_deb,
                  COUNT(*) AS n_pend
                  FROM scintela.banco_historicos_pendientes
                 WHERE no_banco = %(no_banco)s
                   AND conciliado_en IS NULL
                """,
                {"no_banco": _BANCO_PICHINCHA},
            ) or {}
            saldo_pc_actual["neto_pendientes"] = float(row_pend.get("neto_pend") or 0)
            saldo_pc_actual["n_pendientes"] = int(row_pend.get("n_pend") or 0)
            saldo_pc_actual["pendientes_banco_creditos"] = round(
                float(row_pend.get("sum_cred") or 0), 2
            )
            saldo_pc_actual["pendientes_banco_debitos"] = round(
                float(row_pend.get("sum_deb") or 0), 2
            )
            saldo_pc_actual["n_pendientes_banco_cred"] = int(row_pend.get("n_cred") or 0)
            saldo_pc_actual["n_pendientes_banco_deb"] = int(row_pend.get("n_deb") or 0)

            # TMT 2026-05-28 dueña: 'Saldo si concilio todo' = saldo PC libros
            # − sum_signed(movs PC sin conciliar). Pichincha: ~$2.557K.
            # Movs PC pendientes — split credit/debit como históricos banco.
            row_pend_hoy = _db.fetch_one(
                """
                SELECT
                  COUNT(*) AS n,
                  COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                    THEN -t.importe ELSE t.importe END), 0) AS signed,
                  COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                    THEN 0 ELSE t.importe END), 0) AS sum_cred,
                  COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                    THEN t.importe ELSE 0 END), 0) AS sum_deb,
                  COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                    THEN 0 ELSE 1 END), 0) AS n_cred,
                  COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                    THEN 1 ELSE 0 END), 0) AS n_deb
                  FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = %(no_banco)s
                   AND TRIM(COALESCE(t.stat, '')) <> '*'
                   AND NOT EXISTS (
                       SELECT 1 FROM scintela.banco_conciliacion_match m
                        WHERE m.id_transaccion = t.id_transaccion
                          AND m.deshecho_en IS NULL
                   )
                """,
                {"no_banco": _BANCO_PICHINCHA},
            ) or {}
            saldo_pc_actual["n_pendientes_conciliar"] = int(
                row_pend_hoy.get("n") or 0
            )
            saldo_pc_actual["pendientes_conciliar_neto"] = round(
                float(row_pend_hoy.get("signed") or 0), 2
            )
            saldo_pc_actual["pendientes_pc_creditos"] = round(
                float(row_pend_hoy.get("sum_cred") or 0), 2
            )
            saldo_pc_actual["pendientes_pc_debitos"] = round(
                float(row_pend_hoy.get("sum_deb") or 0), 2
            )
            saldo_pc_actual["n_pendientes_pc_cred"] = int(row_pend_hoy.get("n_cred") or 0)
            saldo_pc_actual["n_pendientes_pc_deb"] = int(row_pend_hoy.get("n_deb") or 0)
            saldo_pc_actual["saldo_si_concilio_todo"] = round(
                saldo_pc_actual["saldo"]
                - saldo_pc_actual["pendientes_conciliar_neto"],
                2,
            )
            # TMT 2026-05-28 dueña: 'saldo banco esperado 2756619 no? si no
            # no da la cuenta'. La fórmula correcta es:
            #   esperado = saldo conciliado live + pendientes históricos banco
            # NO saldo PC libros + pendientes banco — eso ignoraba la resta
            # de los pendientes PC que ya están en libros pero el banco no vio.
            saldo_pc_actual["saldo_banco_esperado"] = round(
                saldo_pc_actual["saldo_si_concilio_todo"]
                + saldo_pc_actual["neto_pendientes"],
                2,
            )
            # TMT 2026-05-28 dueña: 'ver detalle que se abra en la misma pantalla'.
            # Listamos los movs PC sin conciliar para mostrarlos inline dentro del
            # card. Esto evita navegar a /bancos/10 y perder el contexto. Trae
            # los más recientes primero, limitamos a 30 para no inflar la página.
            try:
                saldo_pc_actual["pendientes_conciliar_rows"] = _db.fetch_all(
                    """
                    SELECT t.id_transaccion, t.fecha, t.documento, t.no_cheque,
                           t.concepto, t.importe,
                           CASE WHEN t.documento IN ('CH','ND','DB','GS','PA')
                                THEN -t.importe ELSE t.importe END AS importe_signed
                      FROM scintela.transacciones_bancarias t
                     WHERE t.no_banco = %(no_banco)s
                       AND TRIM(COALESCE(t.stat, '')) <> '*'
                       AND NOT EXISTS (
                           SELECT 1 FROM scintela.banco_conciliacion_match m
                            WHERE m.id_transaccion = t.id_transaccion
                              AND m.deshecho_en IS NULL
                       )
                     ORDER BY t.fecha DESC, t.id_transaccion DESC
                     LIMIT 30
                    """,
                    {"no_banco": _BANCO_PICHINCHA},
                ) or []
            except Exception:
                saldo_pc_actual["pendientes_conciliar_rows"] = []
        except Exception as _e:
            import logging
            logging.getLogger("programa_core.conciliacion").exception(
                "saldo_pc_actual falló: %s", _e
            )
            saldo_pc_actual = {}

        return render_template(
            "conciliacion/banco_upload.html",
            bancos=bancos,
            no_banco_default=_BANCO_PICHINCHA,
            ultimos=ultimos,
            uploads=uploads,
            saldo_pre=saldo_pre,
            saldo_dbase=saldo_dbase,
            saldo_conc_split=saldo_conc_split,
            saldo_pc_actual=saldo_pc_actual,
        )

    # ── POST ────────────────────────────────────────────────────────────
    # Banco elegido (default: Pichincha).
    try:
        no_banco = int(request.form.get("no_banco") or _BANCO_PICHINCHA)
    except (TypeError, ValueError):
        no_banco = _BANCO_PICHINCHA

    f = request.files.get("archivo")
    if not f or not f.filename:
        flash("Subí un archivo del banco (.xlsx / .xls).", "warn")
        return redirect(url_for("conciliacion.hub"))
    fname_lower = f.filename.lower()
    if not any(fname_lower.endswith(ext) for ext in (".xlsx", ".xls", ".xlsm", ".xlsb", ".ods")):
        flash(f"El archivo debe ser una planilla (.xlsx, .xls, .xlsm). Recibí: {f.filename}", "warn")
        return redirect(url_for("conciliacion.hub"))

    try:
        raw = f.read()
        movs_real = parse_banco_xlsx(raw)
    except Exception as e:
        flash_exc("No pude leer el archivo.", e)
        return redirect(url_for("conciliacion.hub"))

    if not movs_real:
        flash("El archivo no tiene movimientos parseables.", "warn")
        return redirect(url_for("conciliacion.hub"))

    # TMT 2026-05-26 dueña: registrar upload — fail-soft, no rompe el flow.
    try:
        import hashlib as _hashlib
        file_hash = _hashlib.sha256(raw).hexdigest()
        fechas = [m.fecha for m in movs_real if m.fecha]
        queries.registrar_upload(
            no_banco=no_banco,
            filename=f.filename,
            file_hash=file_hash,
            n_filas=len(movs_real),
            fecha_min=min(fechas) if fechas else None,
            fecha_max=max(fechas) if fechas else None,
            usuario=_usuario_actual(),
        )
    except Exception:
        pass  # tracking opcional, no bloquea

    # TMT 2026-05-27 dueña: 'necesito cargar unos movimientos que son los
    # historicos no conciliados de parte del banco, y deberian aparecer
    # siempre que hago la conciliacion, salvo que sean conciliados.'
    # Inyectamos pendientes historicos como movs_real adicionales antes
    # del matcher. El matcher los cruza con PC; los que matchean se
    # marcan conciliados en confirmar_match (dual-write a la tabla
    # historicos).
    try:
        from decimal import Decimal as _Dec_h

        import db as _db_h
        from modules.conciliacion.parser_banco import MovBanco as _MB_h
        _hist_rows = _db_h.fetch_all(
            """
            SELECT id, fecha, concepto, documento, monto, tipo, oficina, detalle, fuente
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
             ORDER BY fecha DESC, id DESC
            """,
            (no_banco,),
        ) or []
        n_hist_inyectados = 0
        for _h in _hist_rows:
            movs_real.append(_MB_h(
                fecha=_h.get("fecha"),
                concepto=str(_h.get("concepto") or ""),
                documento=str(_h.get("documento") or ""),
                monto=_Dec_h(str(_h.get("monto") or 0)),
                saldo=_Dec_h("0"),
                codigo=str(_h.get("oficina") or "")[:10],
                tipo=str(_h.get("tipo") or "C").upper(),
                oficina=str(_h.get("oficina") or ""),
            ))
            n_hist_inyectados += 1
        if n_hist_inyectados:
            import logging
            logging.getLogger("programa_core.conciliacion").info(
                "historicos pendientes inyectados: %s", n_hist_inyectados
            )
    except Exception as _e_hist:
        import logging
        logging.getLogger("programa_core.conciliacion").exception(
            "load historicos pendientes falló: %s", _e_hist
        )

    try:
        resultado = matchear_extracto_banco(movs_real, no_banco=no_banco)
    except Exception as e:
        # TMT 2026-05-23 — sin JSON inline en prod. Loguear y redirigir.
        import logging
        logging.getLogger("programa_core.conciliacion").exception(
            "matchear_extracto_banco falló (no_banco=%s, n_movs=%s)",
            no_banco, len(movs_real),
        )
        flash_exc("No se pudo conciliar contra el banco", e)
        return redirect(url_for("conciliacion.hub"))

    data = _serialize_resultado_banco(resultado, no_banco)
    kpis = _calc_kpis(data)
    banco_nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"

    # TMT 2026-05-26 dueña: pre-render del agrupado de comisiones/impuestos
    # POR DÍA (no un solo bloque). Cada día con ≥2 movs COMISION genera su
    # propio card. Si BANCSIS ya tiene un NC/ND del mismo día y monto =
    # neto, se marca como `ya_cargado=True` para que la dueña no duplique.
    # Fail-soft: si algo rompe, queda lista vacía.
    agrupados_comisiones_por_dia: list[dict] = []
    try:
        comisiones = [
            r for r in (data.get("real_only") or [])
            if (r.get("cat") or {}).get("grupo") == "COMISION"
        ]
        # Group by fecha (ISO string del serializer).
        por_dia: dict[str, list[dict]] = {}
        for r in comisiones:
            f = str(r.get("fecha") or "")
            if not f:
                continue
            por_dia.setdefault(f, []).append(r)

        # Para detectar "ya cargado": mirar bancsis_only del mismo día que
        # sea NC/ND con monto ≈ neto_dia.
        bancsis_periodo = (data.get("bancsis_only") or []) + [
            m.get("bancsis", {}) for m in (data.get("matches") or [])
        ]

        for fecha_dia, items in sorted(por_dia.items()):
            if len(items) < 2:
                continue  # 1 sola comisión no se agrupa
            sum_c = sum(float(it.get("monto") or 0) for it in items if (it.get("tipo") or "") == "C")
            sum_d = sum(float(it.get("monto") or 0) for it in items if (it.get("tipo") or "") == "D")
            neto = round(sum_c - sum_d, 2)
            if neto == 0:
                continue
            documento = "NC" if neto > 0 else "ND"
            abs_neto = abs(neto)
            # Detectar si BANCSIS del mismo día ya tiene un NC/ND con ese monto.
            ya_cargado_id = None
            for bk in bancsis_periodo:
                bk_fecha = str(bk.get("fecha") or "")
                bk_doc = (bk.get("documento") or "").upper()
                bk_imp = abs(float(bk.get("importe") or 0))
                if bk_fecha == fecha_dia and bk_doc in ("NC", "ND") and abs(bk_imp - abs_neto) < 0.01:
                    ya_cargado_id = bk.get("id_transaccion")
                    break
            try:
                dia_mostrar = f"{fecha_dia[8:10]}/{fecha_dia[5:7]}"
            except Exception:
                dia_mostrar = fecha_dia
            agrupados_comisiones_por_dia.append({
                "fecha": fecha_dia,
                "fecha_mostrar": dia_mostrar,
                "n": len(items),
                "neto": neto,
                "documento": documento,
                "concepto_default": f"Comisiones e impuestos {dia_mostrar}"[:50],
                "items": items,
                "ya_cargado": ya_cargado_id is not None,
                "ya_cargado_id": ya_cargado_id,
            })
    except Exception as _e:
        import logging
        logging.getLogger("programa_core.conciliacion").exception(
            "agrupados_comisiones_por_dia prep falló: %s", _e
        )
        agrupados_comisiones_por_dia = []

    # Compat: dejo el viejo `agrupado_comisiones` apuntando al primer día
    # para no romper el template existente mientras lo migro.
    agrupado_comisiones = agrupados_comisiones_por_dia[0] if agrupados_comisiones_por_dia else None

    # TMT 2026-05-27 dueña: "QUIERO HACER SUMA DEPOSITOS POR DIA BANCO VS
    # PROGRAMA. CUANDO ES DEPOSITO NO QUIERO ASIGNARSELOS A CLIENTES."
    # Resumen por día — entradas (Tipo C / tipo_real C) en ambos lados
    # sumadas, comparadas. Sin desglose por cliente.
    # TMT 2026-05-27 dueña v2: "dejame expandir día por día".
    # TMT 2026-05-27 dueña v3: "Las transferencias si iban en sugeridos!!!".
    # Sólo CHEQUES y EFECTIVO al panel — el resto (TR, NC) sigue por el
    # flujo normal de sugeridos/matches/solo banco/solo programa.
    _CATS_DAILY = {"ENTRADA_COBRO_CHEQUE", "ENTRADA_DEPOSITO_EFECTIVO"}
    _DOCS_DAILY = {"DE"}  # programa: documento de depósito banco

    # TMT 2026-05-27 dueña: "este porque no tiene nombre?" — "1 ch.CM1"
    # tiene código alfanumérico (CM1) que el regex de cliente_concepto
    # no extrae (sólo letras). Fallback genérico: si b.prov vacío,
    # extraemos del concepto cualquier token 2-5 alfanumérico que arranque
    # con letra después de "ch."/"dep ch."/"tr."/"nc.".
    import re as _re_local
    # Prefijo debe estar precedido por inicio o no-letra Y seguido de `.` o
    # espacio (no por letra). Evita matchear "nc" dentro de "transfereNCIA".
    _RE_CH_PROV_FALLBACK = _re_local.compile(
        r"(?:^|\W)(?:ch|tr|trf|nc|dep\s*ch)[\.\s]+([A-Za-z][A-Za-z0-9]{1,4})\b",
        _re_local.IGNORECASE,
    )

    def _prov_fallback(prov_db: str, concepto: str) -> str:
        if (prov_db or "").strip():
            return prov_db
        m = _RE_CH_PROV_FALLBACK.search(concepto or "")
        return m.group(1).upper() if m else ""
    depositos_por_dia: list[dict] = []
    try:
        banco_items_dia: dict[str, list[dict]] = {}
        prog_items_dia: dict[str, list[dict]] = {}
        # Banco real — real_only (entradas) + matches (banco side entradas)
        for r in (data.get("real_only") or []):
            if (r.get("tipo") or "").upper() != "C":
                continue
            if ((r.get("cat") or {}).get("codigo") or "") not in _CATS_DAILY:
                continue
            f = str(r.get("fecha") or "")
            if not f:
                continue
            banco_items_dia.setdefault(f, []).append({
                "fecha": f,
                "concepto": r.get("concepto") or "",
                "documento": r.get("documento") or "",  # doc id del banco real
                "monto": float(r.get("monto") or 0),
                "tipo": r.get("tipo") or "",
                "codigo": r.get("codigo") or "",
                "oficina": r.get("oficina") or "",
                "matched": False,
            })
        for m in (data.get("matches") or []):
            real = m.get("real") or {}
            if (real.get("tipo") or "").upper() != "C":
                continue
            if ((m.get("cat") or {}).get("codigo") or "") not in _CATS_DAILY:
                continue
            f = str(real.get("fecha") or "")
            if not f:
                continue
            banco_items_dia.setdefault(f, []).append({
                "fecha": f,
                "concepto": real.get("concepto") or "",
                "documento": real.get("documento") or "",  # doc id del banco real
                "monto": float(real.get("monto") or 0),
                "tipo": real.get("tipo") or "",
                "codigo": real.get("codigo") or "",
                "oficina": real.get("oficina") or "",
                "matched": True,
            })
        # Programa — bancsis_only entradas + matches bancsis entradas
        for b in (data.get("bancsis_only") or []):
            if (b.get("tipo_real") or "").upper() != "C":
                continue
            if (b.get("documento") or "").upper() not in _DOCS_DAILY:
                continue
            f = str(b.get("fecha") or "")
            if not f:
                continue
            prog_items_dia.setdefault(f, []).append({
                "fecha": f,
                "concepto": b.get("concepto") or "",
                "documento": b.get("documento") or "",          # tipo DE/CH
                "numreferencia": b.get("numreferencia") or "",  # doc id banco
                "doc_banco_rel": b.get("doc_banco_rel") or "",  # cheque.doc_banco fallback
                "importe": float(b.get("importe") or 0),
                "prov": _prov_fallback(b.get("prov") or "", b.get("concepto") or ""),
                "prov_nombre": b.get("prov_nombre") or "",
                "es_agrupado": bool(b.get("es_agrupado")),
                "n_cheques": int(b.get("n_cheques") or 0),
                "id_transaccion": b.get("id_transaccion"),  # para click-to-pair
                "fecha_crea": b.get("fecha_crea"),
                "matched": False,
            })
        for m in (data.get("matches") or []):
            b = m.get("bancsis") or {}
            real = m.get("real") or {}
            if (real.get("tipo") or "").upper() != "C":
                continue
            if (b.get("documento") or "").upper() not in _DOCS_DAILY:
                continue
            f = str(b.get("fecha") or "")
            if not f:
                continue
            prog_items_dia.setdefault(f, []).append({
                "fecha": f,
                "concepto": b.get("concepto") or "",
                "documento": b.get("documento") or "",
                "numreferencia": b.get("numreferencia") or "",
                "doc_banco_rel": b.get("doc_banco_rel") or "",
                "importe": float(b.get("importe") or 0),
                "prov": _prov_fallback(b.get("prov") or "", b.get("concepto") or ""),
                "prov_nombre": b.get("prov_nombre") or "",
                "es_agrupado": False,
                "n_cheques": 0,
                "id_transaccion": b.get("id_transaccion"),  # para click-to-pair
                "fecha_crea": b.get("fecha_crea"),
                "matched": True,
            })

        # TMT 2026-05-27 dueña: 'aca los tengo que ver cheque por cheque,
        # no puedo por lote'. REVERTIDO: agrupar por fecha_crea ≤120s
        # hacía lo opuesto a lo que se necesita en este panel — la lista
        # del banco viene cheque a cheque (26 filas), entonces el PC
        # también tiene que mostrarse cheque a cheque para poder parear
        # 1:1. El lote grouping queda solo en /bancos y /cheques donde
        # sí ayuda a reducir ruido.
        # Unión de fechas, orden ascendente
        for f in sorted(set(banco_items_dia) | set(prog_items_dia)):
            b_items = banco_items_dia.get(f, [])
            p_items = prog_items_dia.get(f, [])
            sb = round(sum(it["monto"] for it in b_items), 2)
            sp = round(sum(it["importe"] for it in p_items), 2)
            diff = round(sb - sp, 2)
            try:
                fm = f"{f[8:10]}/{f[5:7]}"
            except Exception:
                fm = f
            # TMT 2026-05-27 dueña: 'si hay match de montos, ponelo arriba
            # de todo asi lo veo rapido'. Marcamos amount_match cuando un
            # monto banco coincide con un monto PC (al centavo, ±$0.01).
            # Esos items van ARRIBA en cada columna.
            def _round2(x): return round(float(x or 0), 2)
            p_montos = set(_round2(p.get("importe")) for p in p_items)
            b_montos = set(_round2(b.get("monto")) for b in b_items)
            for it in b_items:
                it["amount_match"] = _round2(it.get("monto")) in p_montos
            for it in p_items:
                it["amount_match"] = _round2(it.get("importe")) in b_montos
            # Sort: amount_match primero (True > False), después por monto desc
            b_items.sort(key=lambda x: (not x.get("amount_match"), -float(x.get("monto") or 0)))
            p_items.sort(key=lambda x: (not x.get("amount_match"), -float(x.get("importe") or 0)))
            depositos_por_dia.append({
                "fecha": f,
                "fecha_mostrar": fm,
                "banco": sb,
                "programa": sp,
                "diff": diff,
                "n_banco": len(b_items),
                "n_programa": len(p_items),
                "cuadra": abs(diff) < 1.0,
                "banco_items": b_items,
                "programa_items": p_items,
            })
    except Exception as _e:
        import logging
        logging.getLogger("programa_core.conciliacion").exception(
            "depositos_por_dia prep falló: %s", _e
        )
        depositos_por_dia = []

    # Totales del panel — útiles para el header
    dep_total_banco = round(sum(d["banco"] for d in depositos_por_dia), 2)
    dep_total_prog = round(sum(d["programa"] for d in depositos_por_dia), 2)
    dep_total_diff = round(dep_total_banco - dep_total_prog, 2)
    dep_n_dias_cuadran = sum(1 for d in depositos_por_dia if d["cuadra"])

    # TMT 2026-05-27 dueña: "Las transferencias tienen que estar debajo
    # de los depositos, son distintas". Panel paralelo SOLO para
    # transferencias entrantes (cat=ENTRADA_COBRO_TRANSFERENCIA en banco
    # real; cualquier doc-crédito que NO sea DE en bancsis programa).
    # TMT 2026-05-27 v2: la dueña vio "0 movs programa" cuando el banco
    # tenía 108 TR — porque PC graba transferencias también como IN/AC/XX/NC.
    # Ampliamos el filtro: cualquier doc en _DOCS_CREDITO menos DE.
    _CATS_TRANSF = {"ENTRADA_COBRO_TRANSFERENCIA"}
    _DOCS_TRANSF = {"TR", "IN", "AC", "XX", "NC"}
    transferencias_por_dia: list[dict] = []
    try:
        banco_t: dict[str, list[dict]] = {}
        prog_t: dict[str, list[dict]] = {}
        for r in (data.get("real_only") or []):
            if (r.get("tipo") or "").upper() != "C":
                continue
            if ((r.get("cat") or {}).get("codigo") or "") not in _CATS_TRANSF:
                continue
            f = str(r.get("fecha") or "")
            if not f:
                continue
            banco_t.setdefault(f, []).append({
                "fecha": f,
                "concepto": r.get("concepto") or "",
                "documento": r.get("documento") or "",
                "monto": float(r.get("monto") or 0),
                "cliente_nombre": ((r.get("cat") or {}).get("cliente_nombre") or ""),
                "matched": False,
            })
        for m in (data.get("matches") or []):
            real = m.get("real") or {}
            if (real.get("tipo") or "").upper() != "C":
                continue
            if ((m.get("cat") or {}).get("codigo") or "") not in _CATS_TRANSF:
                continue
            f = str(real.get("fecha") or "")
            if not f:
                continue
            banco_t.setdefault(f, []).append({
                "fecha": f,
                "concepto": real.get("concepto") or "",
                "documento": real.get("documento") or "",
                "monto": float(real.get("monto") or 0),
                "cliente_nombre": ((m.get("cat") or {}).get("cliente_nombre") or ""),
                "matched": True,
            })
        # TMT 2026-05-27 dueña: 'SI EN TRANSFERENCIAS DEL PROGRAMA NO
        # TENEMOS LITERAL TRANSFERENCIAS MOSTREMOS MOVIMIENTOS QUE NO
        # MATCHEAMOS EN DEPOSITOS, SI NO COMO PUEDE SER QUE ESTE EN BANCO
        # Y NO EN EL RESTO'. Relajamos filtro: programa side acepta
        # CUALQUIER credit (no solo TR/IN/AC/XX/NC). Un TR del banco
        # puede pairearse contra un DE del PC.
        for b in (data.get("bancsis_only") or []):
            if (b.get("tipo_real") or "").upper() != "C":
                continue
            # SIN filtro de documento — cualquier credit unmatched
            f = str(b.get("fecha") or "")
            if not f:
                continue
            prog_t.setdefault(f, []).append({
                "fecha": f,
                "concepto": b.get("concepto") or "",
                "documento": b.get("documento") or "",
                "numreferencia": b.get("numreferencia") or "",
                "doc_banco_rel": b.get("doc_banco_rel") or "",
                "importe": float(b.get("importe") or 0),
                "prov": _prov_fallback(b.get("prov") or "", b.get("concepto") or ""),
                "prov_nombre": b.get("prov_nombre") or "",
                "es_agrupado": False,
                "n_cheques": 0,
                "id_transaccion": b.get("id_transaccion"),
                "matched": False,
            })
        for m in (data.get("matches") or []):
            b = m.get("bancsis") or {}
            real = m.get("real") or {}
            if (real.get("tipo") or "").upper() != "C":
                continue
            # SIN filtro de documento — cualquier credit match
            f = str(b.get("fecha") or "")
            if not f:
                continue
            prog_t.setdefault(f, []).append({
                "fecha": f,
                "concepto": b.get("concepto") or "",
                "documento": b.get("documento") or "",
                "numreferencia": b.get("numreferencia") or "",
                "doc_banco_rel": b.get("doc_banco_rel") or "",
                "importe": float(b.get("importe") or 0),
                "prov": _prov_fallback(b.get("prov") or "", b.get("concepto") or ""),
                "prov_nombre": b.get("prov_nombre") or "",
                "es_agrupado": False,
                "n_cheques": 0,
                "id_transaccion": b.get("id_transaccion"),
                "matched": True,
            })
        for f in sorted(set(banco_t) | set(prog_t)):
            bi = banco_t.get(f, [])
            pi = prog_t.get(f, [])
            sb = round(sum(it["monto"] for it in bi), 2)
            sp = round(sum(it["importe"] for it in pi), 2)
            diff = round(sb - sp, 2)
            try:
                fm = f"{f[8:10]}/{f[5:7]}"
            except Exception:
                fm = f
            # Amount match: subir arriba items con monto idéntico al otro lado
            def _r2(x): return round(float(x or 0), 2)
            pi_montos = set(_r2(p.get("importe")) for p in pi)
            bi_montos = set(_r2(b.get("monto")) for b in bi)
            for it in bi:
                it["amount_match"] = _r2(it.get("monto")) in pi_montos
            for it in pi:
                it["amount_match"] = _r2(it.get("importe")) in bi_montos
            bi.sort(key=lambda x: (not x.get("amount_match"), -float(x.get("monto") or 0)))
            pi.sort(key=lambda x: (not x.get("amount_match"), -float(x.get("importe") or 0)))
            transferencias_por_dia.append({
                "fecha": f, "fecha_mostrar": fm,
                "banco": sb, "programa": sp, "diff": diff,
                "n_banco": len(bi), "n_programa": len(pi),
                "cuadra": abs(diff) < 1.0,
                "banco_items": bi, "programa_items": pi,
            })
    except Exception as _e:
        import logging
        logging.getLogger("programa_core.conciliacion").exception(
            "transferencias_por_dia prep falló: %s", _e
        )
        transferencias_por_dia = []

    transf_total_banco = round(sum(d["banco"] for d in transferencias_por_dia), 2)
    transf_total_prog = round(sum(d["programa"] for d in transferencias_por_dia), 2)
    transf_total_diff = round(transf_total_banco - transf_total_prog, 2)
    transf_n_dias_cuadran = sum(1 for d in transferencias_por_dia if d["cuadra"])

    # TMT 2026-05-27 dueña: "Pone el id del documento porque asi cruzamos
    # de esa manera si hay. eso importa mas que fecha y que todo".
    # Cross-day match por doc id: si doc banco 14/05 aparece en programa
    # 15/05, lo marcamos como cruce. Sirve para "selección de días
    # alternativos".
    def _norm(s):
        return (str(s or "")).strip().lstrip("0")

    def _build_doc_fecha_map(items_lista: list[dict], doc_key: str, fecha_key: str = "fecha") -> dict:
        out: dict[str, str] = {}
        for it in items_lista:
            doc_n = _norm(it.get(doc_key) or "")
            if doc_n and doc_n not in out:
                out[doc_n] = it.get(fecha_key) or ""
        return out

    # Mapas globales doc -> fecha (depósitos + transferencias en una sola
    # bolsa porque los docs son únicos por banco)
    _all_banco_items = []
    _all_prog_items = []
    for d in depositos_por_dia + transferencias_por_dia:
        _all_banco_items.extend(d.get("banco_items", []) or [])
        _all_prog_items.extend(d.get("programa_items", []) or [])
    docs_prog_map = _build_doc_fecha_map(_all_prog_items, "numreferencia")
    docs_banco_map = _build_doc_fecha_map(_all_banco_items, "documento")

    # Anotar cada item con `cross_match_fecha` (la fecha donde el doc aparece
    # del OTRO lado, si existe). El template usa esto para pintar verde y
    # mostrar "↔ 15/05" cuando es cruce cross-day.
    for d in depositos_por_dia + transferencias_por_dia:
        for it in d.get("banco_items", []) or []:
            doc_n = _norm(it.get("documento") or "")
            f_otro = docs_prog_map.get(doc_n) if doc_n else None
            it["cross_match_fecha"] = f_otro if f_otro and f_otro != d.get("fecha") else None
            it["doc_match"] = bool(doc_n and doc_n in docs_prog_map)
        for it in d.get("programa_items", []) or []:
            doc_n = _norm(it.get("numreferencia") or "")
            f_otro = docs_banco_map.get(doc_n) if doc_n else None
            it["cross_match_fecha"] = f_otro if f_otro and f_otro != d.get("fecha") else None
            it["doc_match"] = bool(doc_n and doc_n in docs_banco_map)

    # Datalists para el modal de crear individual. Fail-soft también.
    try:
        from modules.autocomplete.queries import clientes_para_datalist, proveedores_para_datalist
        clientes_dl = clientes_para_datalist()
        proveedores_dl = proveedores_para_datalist()
    except Exception as _e:
        import logging
        logging.getLogger("programa_core.conciliacion").exception(
            "datalist fetch falló: %s", _e
        )
        clientes_dl = []
        proveedores_dl = []

    # TMT 2026-05-26 dueña: tracking del upload (fail-soft, no bloquea).
    # Esto es el INSERT que va a scintela.conciliacion_upload, separado del
    # render. Si la tabla todavía no existe, _bootstrap intenta crearla.
    # NO debe romper el flow de conciliación.

    # JSON inline (para botón "Confirmar matches" sin guardar en session).
    import json as _json
    matches_json = _json.dumps(data.get("matches") or [], separators=(",", ":"))
    real_only_json = _json.dumps(data.get("real_only") or [], separators=(",", ":"))
    bancsis_only_json = _json.dumps(data.get("bancsis_only") or [], separators=(",", ":"))
    agrupado_json = _json.dumps(agrupado_comisiones, separators=(",", ":")) if agrupado_comisiones else "null"

    # TMT 2026-05-27 dueña: "necesito primero el cruce, cruce exacto de
    # doc id... porque quizas hay una fecha en el banco y otra en el
    # programa". El matcher PASS 0 ya cruza por doc id sin importar
    # fecha — `razon` arranca con "P0·doc-banco". Extraigo esos matches
    # para el card prominente arriba: confirmación bulk de un saque.
    matches_p0 = []
    for m in (data.get("matches") or []):
        raz = (m.get("razon") or "")
        if not raz.startswith("P0·doc-banco"):
            continue
        real = m.get("real") or {}
        bk = m.get("bancsis") or {}
        f_real = str(real.get("fecha") or "")
        f_bk = str(bk.get("fecha") or "")
        date_diff = (f_real != f_bk) and bool(f_real) and bool(f_bk)
        matches_p0.append({
            "real_fecha": f_real,
            "real_documento": real.get("documento") or "",
            "real_concepto": real.get("concepto") or "",
            "real_monto": float(real.get("monto") or 0),
            "real_tipo": real.get("tipo") or "",
            "real_codigo": real.get("codigo") or "",
            "real_oficina": real.get("oficina") or "",
            "bk_fecha": f_bk,
            "bk_documento": bk.get("documento") or "",
            "bk_concepto": bk.get("concepto") or "",
            "bk_importe": float(bk.get("importe") or 0),
            "bk_numreferencia": bk.get("numreferencia") or "",
            "bk_prov": bk.get("prov") or "",
            "bk_prov_nombre": bk.get("prov_nombre") or "",
            "bk_id_transaccion": bk.get("id_transaccion"),
            "date_diff": date_diff,
            "razon": raz,
        })
    matches_p0_json = _json.dumps(
        [{"real": {"fecha": m["real_fecha"], "concepto": m["real_concepto"],
                   "documento": m["real_documento"], "monto": m["real_monto"],
                   "tipo": m["real_tipo"], "codigo": m["real_codigo"],
                   "oficina": m["real_oficina"]},
          "bancsis": {"id_transaccion": m["bk_id_transaccion"]}}
         for m in matches_p0],
        separators=(",", ":"),
    )

    # TMT 2026-05-27 dueña: 'necesito cargar unos movimientos que son los
    # historicos no conciliados de parte del banco'. Cargamos el listado
    # actual de pendientes históricos para mostrarlos en panel propio.
    historicos_pendientes = []
    try:
        import db as _db_hp
        historicos_pendientes = _db_hp.fetch_all(
            """
            SELECT id, fecha, concepto, documento, monto, tipo, oficina, detalle, fuente, creado_en
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
             ORDER BY fecha DESC, id DESC
            """,
            (no_banco,),
        ) or []
    except Exception as _e_hp:
        import logging
        logging.getLogger("programa_core.conciliacion").exception(
            "load historicos_pendientes (panel) falló: %s", _e_hp
        )
        historicos_pendientes = []

    # Stats para el header del panel
    hist_n = len(historicos_pendientes)
    hist_total_c = sum(float(h["monto"]) for h in historicos_pendientes if (h.get("tipo") or "C").upper() == "C")
    hist_total_d = sum(float(h["monto"]) for h in historicos_pendientes if (h.get("tipo") or "C").upper() == "D")

    # TMT 2026-05-27 dueña: 'despues de hacer la conciliacion, deberiamos
    # tener un resumen. banco inicial + mov conciliados +/- debitos
    # pendientes + depositos pendientes = final banco hoy'.
    # Resumen post-conciliación clásico: parto del saldo PC libros, le
    # quito los pendientes PC y le sumo los pendientes banco → tiene que
    # cuadrar con el saldo banco físico del extracto.
    resumen = {}
    try:
        _CRED_DOCS = ("DE", "TR", "XX", "NC", "IN", "AC")
        # Pendientes PC (bancsis_only en período)
        pc_pend_cred = 0.0
        pc_pend_deb = 0.0
        _ext_d = data.get("extracto_desde")
        _ext_h = data.get("extracto_hasta")
        for b in (data.get("bancsis_only") or []):
            f = b.get("fecha")
            if _ext_d and _ext_h and f and not (_ext_d <= f <= _ext_h):
                continue
            if b.get("es_agrupado"):
                continue  # agrupados son N cheques sumados — no double-count
            imp = float(b.get("importe") or 0)
            if (b.get("documento") or "").upper() in _CRED_DOCS:
                pc_pend_cred += imp
            else:
                pc_pend_deb += imp
        # Pendientes Banco (real_only)
        bco_pend_cred = 0.0
        bco_pend_deb = 0.0
        for r in (data.get("real_only") or []):
            mt = float(r.get("monto") or 0)
            if (r.get("tipo") or "").upper() == "C":
                bco_pend_cred += mt
            else:
                bco_pend_deb += mt
        saldo_pc = float(data.get("saldo_bancsis_final") or 0)
        saldo_bco = float(data.get("saldo_real_final") or 0)
        net_pend_pc = pc_pend_cred - pc_pend_deb       # signed
        net_pend_bco = bco_pend_cred - bco_pend_deb    # signed
        # TMT 2026-05-28 dueña: 'deberia ser 2557969' — el saldo
        # conciliado tiene que dar el MISMO número que /bancos/<no> y
        # /conciliacion/banco. Usamos la query canónica (toda la tabla,
        # restando los pendientes históricos) en vez de derivarlo del
        # bancsis_only del extracto, que solo ve los movs del rango.
        no_banco_for_calc = (
            data.get("no_banco")
            or (data.get("filtros") or {}).get("no_banco")
            or _BANCO_PICHINCHA
        )
        saldo_conciliado = saldo_pc - net_pend_pc  # fallback si la query falla
        try:
            import db as _db_canon
            row_p = _db_canon.fetch_one(
                "SELECT COALESCE(SUM(CASE WHEN t.documento IN ('CH','ND','DB','GS','PA') "
                "                  THEN -t.importe ELSE t.importe END), 0) AS signed "
                "FROM scintela.transacciones_bancarias t "
                "WHERE t.no_banco = %(no_banco)s "
                "  AND TRIM(COALESCE(t.stat, '')) <> '*' "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM scintela.banco_conciliacion_match m "
                "       WHERE m.id_transaccion = t.id_transaccion "
                "         AND m.deshecho_en IS NULL"
                "  )",
                {"no_banco": no_banco_for_calc},
            ) or {}
            pend_signed_canon = round(float(row_p.get("signed") or 0), 2)
            saldo_conciliado = round(saldo_pc - pend_signed_canon, 2)
        except Exception as _e_canon:
            import logging
            logging.getLogger("programa_core.conciliacion").exception(
                "saldo_conciliado canonical falló, fallback a delta: %s", _e_canon
            )
        # Saldo banco esperado = Saldo conciliado + net_pend_BCO
        # (al saldo conciliado le sumamos los movs banco que PC no tiene)
        saldo_bco_esperado = saldo_conciliado + net_pend_bco
        diff = round(saldo_bco - saldo_bco_esperado, 2)
        resumen = {
            "saldo_pc": round(saldo_pc, 2),
            "saldo_conciliado": round(saldo_conciliado, 2),
            "saldo_bco_extracto": round(saldo_bco, 2),
            "saldo_bco_esperado": round(saldo_bco_esperado, 2),
            "pc_pend_cred": round(pc_pend_cred, 2),
            "pc_pend_deb": round(pc_pend_deb, 2),
            "bco_pend_cred": round(bco_pend_cred, 2),
            "bco_pend_deb": round(bco_pend_deb, 2),
            "net_pend_pc": round(net_pend_pc, 2),
            "net_pend_bco": round(net_pend_bco, 2),
            "diff": diff,
            "cuadra": abs(diff) < 1.0,
            "fecha_pc": data.get("saldo_bancsis_fecha"),
            "fecha_bco": data.get("saldo_real_fecha"),
        }
    except Exception as _e:
        import logging
        logging.getLogger("programa_core.conciliacion").exception(
            "resumen post-conciliación falló: %s", _e
        )
        resumen = {}

    return render_template(
        "conciliacion/banco_resultado.html",
        data=data,
        matches_json=matches_json,
        real_only_json=real_only_json,
        bancsis_only_json=bancsis_only_json,
        agrupado_comisiones=agrupado_comisiones,
        agrupado_json=agrupado_json,
        depositos_por_dia=depositos_por_dia,
        dep_total_banco=dep_total_banco,
        dep_total_prog=dep_total_prog,
        dep_total_diff=dep_total_diff,
        dep_n_dias_cuadran=dep_n_dias_cuadran,
        transferencias_por_dia=transferencias_por_dia,
        transf_total_banco=transf_total_banco,
        transf_total_prog=transf_total_prog,
        transf_total_diff=transf_total_diff,
        transf_n_dias_cuadran=transf_n_dias_cuadran,
        clientes_dl=clientes_dl,
        proveedores_dl=proveedores_dl,
        matches_p0=matches_p0,
        matches_p0_json=matches_p0_json,
        resumen=resumen,
        historicos_pendientes=historicos_pendientes,
        hist_n=hist_n,
        hist_total_c=hist_total_c,
        hist_total_d=hist_total_d,
        no_banco=no_banco,
        banco_nombre=banco_nombre,
        **kpis,
    )


@conciliacion_bp.route("/banco/marcar-depositos-dia-conciliados", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def marcar_depositos_dia_conciliados():
    """TMT 2026-05-27 dueña: 'dejame ir seleccionando en la tabla y poner
    conciliado, asi podemos a esos depositos hacerle match y decir estos
    ya fueron conciliados'.

    Recibe fechas_json (lista de fechas ISO). Para cada fecha marca como
    conciliados (unilateral) TODOS los movimientos del banco Pichincha
    tipo C (entradas/depósitos) — tanto del lado programa (BANCSIS) como
    del lado banco real (cargado en session).

    Próximo render del extracto: esas fechas dejan de aparecer en el panel
    Depósitos por día y los movs pasan a Historial.
    """
    import json as _json
    from datetime import date as _date
    from decimal import Decimal as _Dec

    import db as _db
    from modules.conciliacion.matcher_banco import confirmar_bancsis_only, confirmar_real_only
    from modules.conciliacion.parser_banco import MovBanco as _MB

    raw = request.form.get("fechas_json") or "[]"
    try:
        fechas_raw = _json.loads(raw)
    except _json.JSONDecodeError:
        fechas_raw = []
    fechas: list[_date] = []
    for f in fechas_raw:
        try:
            fechas.append(_date.fromisoformat(str(f)))
        except (ValueError, TypeError):
            pass
    if not fechas:
        flash("No seleccionaste ningún día.", "warn")
        return redirect(url_for("conciliacion.hub"))

    no_banco = _BANCO_PICHINCHA
    usuario = _usuario_actual()
    n_prog, n_real, errores = 0, 0, 0

    # Lado PROGRAMA: marcar como bancsis_only_ok todas las entradas (Tipo C)
    # de las fechas seleccionadas, banco Pichincha.
    for fecha in fechas:
        try:
            rows = _db.fetch_all(
                """
                SELECT id_transaccion
                  FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s
                   AND fecha = %s
                   AND documento IN ('DE','TR','AC','NC')
                """,
                (no_banco, fecha),
            ) or []
            for r in rows:
                try:
                    confirmar_bancsis_only(no_banco, int(r["id_transaccion"]), usuario=usuario)
                    n_prog += 1
                except Exception:
                    errores += 1
        except Exception:
            errores += 1

    # Lado BANCO REAL: el form trae el detalle de cada día (dias_json) con
    # banco_items y programa_items — porque la session NO persiste el
    # resultado (extracto >4KB rompe cookie Flask). Persistimos cada mov
    # banco real como real_only_ok para que el próximo extracto NO lo
    # vuelva a mostrar (firma fecha+documento+monto+tipo en
    # banco_conciliacion_match).
    raw_dias = request.form.get("dias_json") or "[]"
    try:
        dias_data = _json.loads(raw_dias)
    except _json.JSONDecodeError:
        dias_data = []
    fechas_iso = {f.isoformat() for f in fechas}
    for d in dias_data:
        if str(d.get("fecha") or "") not in fechas_iso:
            continue
        for it in (d.get("banco_items") or []):
            try:
                real = _MB(
                    fecha=_date.fromisoformat(d["fecha"]),
                    concepto=it.get("concepto") or "",
                    documento=it.get("documento") or "",
                    monto=_Dec(str(it.get("monto") or 0)),
                    saldo=_Dec("0"),
                    codigo="",
                    tipo="C",
                    oficina="",
                )
                confirmar_real_only(no_banco, real, usuario=usuario)
                n_real += 1
            except Exception:
                errores += 1

    flash(
        f"Marcados {len(fechas)} días — {n_prog} del programa + {n_real} del banco pasaron a Historial.",
        "ok",
    )
    if errores:
        flash(f"{errores} no se pudieron persistir.", "warn")
    return redirect(url_for("conciliacion.hub"))


@conciliacion_bp.route("/hub/confirmar-matches", methods=["POST"])
@conciliacion_bp.route("/banco/confirmar-matches", methods=["POST"])  # alias compat
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_confirmar_matches():
    """Confirma TODOS los matches recibidos en hidden field matches_json."""
    import json as _json
    from datetime import date as _date
    from decimal import Decimal as _Dec

    from modules.conciliacion.parser_banco import MovBanco as _MB

    raw = request.form.get("matches_json") or "[]"
    try:
        matches = _json.loads(raw)
    except _json.JSONDecodeError:
        matches = []

    confirmados, errores = 0, 0
    usuario = _usuario_actual()
    for m in matches:
        try:
            r = m["real"]
            real = _MB(
                fecha=_date.fromisoformat(r["fecha"]) if r.get("fecha") else None,
                concepto=r.get("concepto") or "",
                documento=r.get("documento") or "",
                monto=_Dec(str(r.get("monto") or 0)),
                saldo=_Dec("0"),
                codigo=r.get("codigo") or "",
                tipo=r.get("tipo") or "",
                oficina=r.get("oficina") or "",
            )
            confirmar_match(
                no_banco=_BANCO_PICHINCHA,
                real=real,
                id_transaccion=m["bancsis"]["id_transaccion"],
                estado="matched",
                usuario=usuario,
            )
            confirmados += 1
        except Exception:
            errores += 1
    if confirmados:
        flash(f"Confirmados {confirmados} matches. No vuelven a aparecer.", "ok")
    if errores:
        flash(f"{errores} no se pudieron persistir.", "warn")
    return redirect(url_for("conciliacion.hub"))


@conciliacion_bp.route("/hub/selftest", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_selftest():
    """Self-test del matcher con txs reales del programa.

    Toma todas las txs Pichincha del día más reciente, genera un
    'extracto' sintético equivalente (mismo monto/tipo/fecha), y corre
    el matcher. Devuelve el desglose del cálculo.

    Si el matcher funciona perfecto, deberían quedar 0 sin match y
    Movimientos banco == Movimientos programa.
    """
    from datetime import date as _date
    from datetime import timedelta as _td

    import db as _db
    # Tomar últimos 5 días con txs Pichincha
    row = _db.fetch_one(
        "SELECT MAX(fecha) AS fmax FROM scintela.transacciones_bancarias WHERE no_banco = 10"
    )
    if not row or not row.get("fmax"):
        return {"error": "sin txs Pichincha"}, 500
    fmax = row["fmax"]
    dia_max = fmax if isinstance(fmax, _date) else _date.fromisoformat(str(fmax))
    dia_min = dia_max - _td(days=4)

    txs = _db.fetch_all(
        """
        SELECT id_transaccion, fecha, documento, concepto, importe, prov, numreferencia
          FROM scintela.transacciones_bancarias
         WHERE no_banco = 10
           AND fecha BETWEEN %s AND %s
         ORDER BY fecha, id_transaccion
        """,
        (dia_min, dia_max),
    ) or []
    if not txs:
        return {"error": f"sin txs entre {dia_min} y {dia_max}"}, 500

    # Generar MovBanco equivalentes (uno por tx del programa)
    from decimal import Decimal as _Dec

    from modules.conciliacion.parser_banco import MovBanco
    _CRED = ("DE", "TR", "XX", "NC", "IN", "AC")
    movs_real = []
    for t in txs:
        doc = (t.get("documento") or "").upper().strip()
        tipo = "C" if doc in _CRED else "D"
        movs_real.append(MovBanco(
            fecha=t["fecha"], concepto=t.get("concepto") or "",
            documento=str(t.get("numreferencia") or t["id_transaccion"]),
            monto=_Dec(str(t.get("importe") or 0)),
            saldo=_Dec("0"), codigo="001045", tipo=tipo, oficina="AG. NORTE",
        ))

    resultado = matchear_extracto_banco(movs_real, no_banco=10)
    data = _serialize_resultado_banco(resultado, 10)
    kpis = _calc_kpis(data)

    return {
        "fecha_test": str(dia_max),
        "n_txs_programa_simuladas": len(txs),
        "n_movs_banco_generados": len(movs_real),
        "matches_por_pasada": resultado.matches_por_pasada,
        "matches_total": len(resultado.matches),
        "real_only": len(resultado.real_only),
        "bancsis_only": len(resultado.bancsis_only),
        "bancsis_agrupados": len(resultado.bancsis_agrupados),
        "kpis": {
            "mov_banco": round(kpis["mov_banco"], 2),
            "mov_banco_matches": round(kpis["mov_banco_matches"], 2),
            "mov_programa": round(kpis["mov_programa"], 2),
            "mov_programa_matches": round(kpis["mov_programa_matches"], 2),
            "sum_real_only": round(kpis["sum_real_only"], 2),
            "sum_bancsis_only_periodo": round(kpis["sum_bancsis_only_periodo"], 2),
            "diff": round(kpis["diff"], 2),
        },
        "muestra_real_only": [
            {"fecha": str(r.fecha), "tipo": r.tipo, "monto": float(r.monto),
             "concepto": r.concepto[:50], "documento": r.documento}
            for r in resultado.real_only[:5]
        ],
        "muestra_bancsis_only": [
            {"fecha": str(b.fecha), "doc": b.documento, "importe": b.importe,
             "concepto": b.concepto[:50], "prov": b.prov, "es_agrupado": b.es_agrupado}
            for b in resultado.bancsis_only[:5]
        ],
    }


@conciliacion_bp.route("/hub/kpi-debug", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_kpi_debug():
    """Sube un xlsx y devuelve el desglose de los KPIs en JSON."""
    f = request.files.get("archivo")
    if not f or not f.filename:
        return {"error": "no file"}, 400
    raw = f.read()
    try:
        movs_real = parse_banco_xlsx(raw)
    except Exception as e:
        return {"error": str(e)}, 500
    no_banco = _BANCO_PICHINCHA
    try:
        resultado = matchear_extracto_banco(movs_real, no_banco=no_banco)
    except Exception as e:
        # TMT 2026-06-05 (bug hunt lente 8): antes devolvíamos el traceback
        # crudo al cliente (`tb: traceback.format_exc()...`). Eso filtraba
        # paths internos, nombres de variables y stack frames al frontend
        # — info-leak innecesario aún para usuarios con permiso
        # `conciliacion.ver`. Ahora logueamos server-side y devolvemos solo
        # el mensaje del error.
        import logging
        logging.getLogger(__name__).exception(
            "matchear_extracto_banco falló: no_banco=%s", no_banco,
        )
        return {"error": str(e)}, 500

    data = _serialize_resultado_banco(resultado, no_banco)
    kpis = _calc_kpis(data)

    # Desglose detallado
    matches = data.get("matches") or []
    sum_match_real = sum((1 if m["real"]["tipo"] == "C" else -1) * float(m["real"]["monto"]) for m in matches)
    sum_match_bancsis = sum(
        (1 if m["bancsis"]["documento"] in ("DE", "TR", "XX", "NC", "IN", "AC") else -1) * float(m["bancsis"]["importe"])
        for m in matches
    )

    bancsis_only = data.get("bancsis_only") or []
    n_agrup = sum(1 for b in bancsis_only if b.get("es_agrupado"))
    sum_agrup = sum(
        (1 if b["documento"] in ("DE", "TR", "XX", "NC", "IN", "AC") else -1) * float(b["importe"])
        for b in bancsis_only if b.get("es_agrupado")
    )
    sum_no_agrup_total = sum(
        (1 if b["documento"] in ("DE", "TR", "XX", "NC", "IN", "AC") else -1) * float(b["importe"])
        for b in bancsis_only if not b.get("es_agrupado")
    )
    en_rango = [b for b in bancsis_only if data.get("extracto_desde") <= (b.get("fecha") or "") <= data.get("extracto_hasta") and not b.get("es_agrupado")]
    sum_en_rango = sum(
        (1 if b["documento"] in ("DE", "TR", "XX", "NC", "IN", "AC") else -1) * float(b["importe"])
        for b in en_rango
    )

    return {
        "rango": {"desde": data.get("extracto_desde"), "hasta": data.get("extracto_hasta")},
        "n_matches": len(matches),
        "sum_match_real": round(sum_match_real, 2),
        "sum_match_bancsis": round(sum_match_bancsis, 2),
        "real_only": {
            "n": len(data.get("real_only") or []),
            "suma": round(data.get("total_real_only_signed") or 0, 2),
        },
        "bancsis_only": {
            "n_total": len(bancsis_only),
            "n_agrupados": n_agrup,
            "n_no_agrup_en_rango": len(en_rango),
            "suma_agrupados": round(sum_agrup, 2),
            "suma_no_agrup_total": round(sum_no_agrup_total, 2),
            "suma_no_agrup_en_rango": round(sum_en_rango, 2),
        },
        "kpis_calculados": {
            "mov_banco": round(kpis["mov_banco"], 2),
            "mov_programa": round(kpis["mov_programa"], 2),
            "diff": round(kpis["diff"], 2),
        },
        "muestra_bancsis_only": [
            {
                "fecha": b.get("fecha"), "documento": b.get("documento"),
                "importe": b.get("importe"), "prov": b.get("prov"),
                "concepto": (b.get("concepto") or "")[:50],
                "es_agrupado": b.get("es_agrupado"),
            }
            for b in bancsis_only[:20]
        ],
    }


@conciliacion_bp.route("/hub/diag-extract", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_diag_extract():
    """Saca códigos del concepto y verifica si matchean con clientes."""
    import db as _db
    # Top códigos extraídos del concepto (regex "ch.XXX")
    rows = _db.fetch_all(
        """
        WITH extr AS (
            SELECT tb.id_transaccion,
                   tb.concepto,
                   tb.prov AS prov_actual,
                   UPPER(TRIM((regexp_match(
                       tb.concepto,
                       '(?:^|\s)(?:\d+\s+)?(?:ch\.?|tr\.?|nc\.?|trf\.?|dep\.?\s*ch\.?)\s*([A-Za-z]{3,5})\b',
                       'i'
                   ))[1])) AS cod_extraido
              FROM scintela.transacciones_bancarias tb
             WHERE tb.no_banco = 10
               AND tb.fecha >= CURRENT_DATE - INTERVAL '30 days'
               AND tb.concepto IS NOT NULL
        )
        SELECT cod_extraido,
               COUNT(*) AS n,
               COUNT(*) FILTER (WHERE prov_actual IS NULL OR LENGTH(TRIM(prov_actual)) < 3) AS sin_prov,
               MAX(concepto) AS sample
          FROM extr
         WHERE cod_extraido IS NOT NULL
         GROUP BY cod_extraido
         ORDER BY n DESC
         LIMIT 30
        """
    ) or []
    codigos = [r["cod_extraido"] for r in rows if r["cod_extraido"]]
    clientes_map = {}
    if codigos:
        cli = _db.fetch_all(
            "SELECT UPPER(TRIM(codigo_cli)) AS k, nombre FROM scintela.cliente WHERE UPPER(TRIM(codigo_cli)) = ANY(%s)",
            (codigos,),
        ) or []
        clientes_map = {c["k"]: c["nombre"] for c in cli}

    # Total filas con prov NULL/chico y concepto matcheable
    stats = _db.fetch_one(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (
                   WHERE COALESCE(LENGTH(TRIM(prov)), 0) < 3
                     AND concepto ~* '\\m(?:ch\\.?|tr\\.?|nc\\.?)\\s*[A-Za-z]{3,5}\\M'
               ) AS sin_prov_pero_extraible
          FROM scintela.transacciones_bancarias
         WHERE no_banco = 10
           AND fecha >= CURRENT_DATE - INTERVAL '30 days'
        """
    ) or {}

    return {
        "stats": dict(stats),
        "top_codigos_extraidos_30d": [
            {
                "cod": r["cod_extraido"],
                "n_total": r["n"],
                "n_sin_prov": r["sin_prov"],
                "matchea_cliente": clientes_map.get(r["cod_extraido"]),
                "sample_concepto": (r.get("sample") or "")[:50],
            }
            for r in rows
        ],
    }


@conciliacion_bp.route("/hub/diag-prov", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_diag_prov():
    """Diagnóstico de prov vs codigo_cli."""
    import db as _db
    # 1) Top 30 prov más usados en transacciones_bancarias (no vacíos)
    top = _db.fetch_all(
        """
        SELECT UPPER(TRIM(tb.prov)) AS prov, COUNT(*) AS n,
               MAX(tb.concepto) AS sample_concepto
          FROM scintela.transacciones_bancarias tb
         WHERE tb.no_banco = 10
           AND COALESCE(NULLIF(TRIM(tb.prov), ''), '') <> ''
           AND tb.fecha >= CURRENT_DATE - INTERVAL '60 days'
         GROUP BY UPPER(TRIM(tb.prov))
         ORDER BY n DESC
         LIMIT 30
        """
    ) or []
    # 2) De esos, cuáles matchean con scintela.cliente
    if top:
        codigos = [r["prov"] for r in top]
        matches = _db.fetch_all(
            "SELECT UPPER(TRIM(codigo_cli)) AS k, nombre FROM scintela.cliente WHERE UPPER(TRIM(codigo_cli)) = ANY(%s)",
            (codigos,),
        ) or []
        by_k = {m["k"]: m["nombre"] for m in matches}
    else:
        by_k = {}
    # 3) Conceptos de los que NO matchean — ver si tienen "ch.XXX" extraíble
    sample_no_match = []
    for r in top:
        if r["prov"] not in by_k:
            sample_no_match.append({
                "prov": r["prov"], "n": r["n"],
                "sample_concepto": (r.get("sample_concepto") or "")[:50],
                "nombre_cliente_match": None,
            })
    # 4) Cuántos clientes hay con codigo_cli que empieza con el prov no matcheado
    sugerencias = []
    for r in sample_no_match[:10]:
        like = r["prov"] + "%"
        cands = _db.fetch_all(
            "SELECT codigo_cli, nombre FROM scintela.cliente WHERE UPPER(TRIM(codigo_cli)) LIKE %s LIMIT 5",
            (like,),
        ) or []
        sugerencias.append({"prov": r["prov"], "candidatos": [{"codigo_cli": c["codigo_cli"], "nombre": c["nombre"]} for c in cands]})

    return {
        "top_30_prov_pichincha_60d": [
            {"prov": r["prov"], "n": r["n"], "sample": r.get("sample_concepto", "")[:50],
             "matchea_cliente": by_k.get(r["prov"])}
            for r in top
        ],
        "n_total_prov_unicos": len(top),
        "n_matchean_cliente": len(by_k),
        "fuzzy_sugerencias_top_10_no_match": sugerencias,
    }


@conciliacion_bp.route("/hub/diag", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_diag():
    """Diagnóstico: muestra bancos disponibles y conteo de transacciones por
    no_banco para mayo 2026. Sirve para confirmar que el matcher apunta al
    no_banco correcto.
    """
    import db as _db
    bancos = _db.fetch_all("SELECT no_banco, nombre FROM scintela.banco ORDER BY no_banco") or []
    counts = _db.fetch_all(
        """
        SELECT no_banco, COUNT(*) AS n,
               MIN(fecha) AS dmin, MAX(fecha) AS dmax,
               COUNT(*) FILTER (WHERE fecha BETWEEN '2026-05-01' AND '2026-05-31') AS n_mayo,
               COUNT(*) FILTER (WHERE fecha BETWEEN '2026-05-10' AND '2026-05-20') AS n_ventana
          FROM scintela.transacciones_bancarias
         GROUP BY no_banco
         ORDER BY no_banco
        """
    ) or []
    # Sample 5 transacciones del banco Pichincha en mayo
    sample = _db.fetch_all(
        """
        SELECT tb.id_transaccion, tb.fecha, tb.documento, tb.importe, tb.concepto,
               tb.no_banco, tb.prov, c.nombre AS prov_nombre, tb.numreferencia
          FROM scintela.transacciones_bancarias tb
          LEFT JOIN scintela.cliente c ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(tb.prov))
         WHERE tb.fecha BETWEEN '2026-05-12' AND '2026-05-20'
         ORDER BY tb.fecha DESC, tb.id_transaccion DESC
         LIMIT 8
        """
    ) or []
    # Cobertura de prov: cuántas tx tienen prov no-vacío y cuántas matchean cliente
    cobertura = _db.fetch_one(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(tb.prov),''),'') <> '') AS con_prov,
               COUNT(c.codigo_cli) AS con_cliente
          FROM scintela.transacciones_bancarias tb
          LEFT JOIN scintela.cliente c ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(tb.prov))
         WHERE tb.no_banco = 10
           AND tb.fecha BETWEEN '2026-05-01' AND '2026-05-31'
        """
    ) or {}
    return {
        "bancos": [{"no_banco": b["no_banco"], "nombre": b.get("nombre")} for b in bancos],
        "counts_por_banco": [
            {
                "no_banco": c["no_banco"],
                "total": c["n"],
                "fecha_min": c["dmin"].isoformat() if c.get("dmin") else None,
                "fecha_max": c["dmax"].isoformat() if c.get("dmax") else None,
                "n_mayo": c["n_mayo"],
                "n_ventana_12_20": c["n_ventana"],
            }
            for c in counts
        ],
        "sample_12_20_mayo": [
            {
                "id_transaccion": s["id_transaccion"],
                "fecha": s["fecha"].isoformat() if s.get("fecha") else None,
                "documento": s.get("documento"),
                "importe": float(s.get("importe") or 0),
                "concepto": (s.get("concepto") or "")[:60],
                "no_banco": s.get("no_banco"),
                "prov": s.get("prov"),
                "prov_nombre": s.get("prov_nombre"),
                "numreferencia": s.get("numreferencia"),
            }
            for s in sample
        ],
        "cobertura_prov_mayo_pichincha": {
            "total": int(cobertura.get("total") or 0),
            "con_prov_no_vacio": int(cobertura.get("con_prov") or 0),
            "matchea_cliente": int(cobertura.get("con_cliente") or 0),
        },
    }


@conciliacion_bp.route("/hub/aceptar-bancsis-only", methods=["POST"])
@conciliacion_bp.route("/banco/aceptar-bancsis-only", methods=["POST"])  # alias compat
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_aceptar_bancsis_only():
    """Acepta UN mov BANCSIS como diferencia legítima.

    Form: id_transaccion (int)
    """
    try:
        idtx = int(request.form.get("id_transaccion") or 0)
    except (TypeError, ValueError):
        idtx = 0
    if idtx <= 0:
        flash("id_transaccion inválido.", "error")
        return redirect(url_for("conciliacion.hub"))
    no_banco = _form_no_banco(request) or _BANCO_PICHINCHA
    confirmar_bancsis_only(no_banco, idtx, usuario=_usuario_actual())
    nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"
    flash(f"Movimiento #{idtx} de {nombre} Programa aceptado como diferencia legítima.", "ok")
    return redirect(url_for("conciliacion.hub"))


# ═══════════════════════════════════════════════════════════════════════════
# Fase B + D (2026-05-23) — endpoints adicionales
# ═══════════════════════════════════════════════════════════════════════════


def _form_no_banco(req) -> int | None:
    """Lee `no_banco` del form (form-encoded). None si no vino."""
    try:
        v = req.form.get("no_banco")
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def _reconstruir_real(form) -> MovBanco:
    """Reconstruye un MovBanco desde fields ocultos del template."""
    from datetime import date as _date
    from decimal import Decimal as _Dec

    from modules.conciliacion.parser_banco import MovBanco as _MB
    fecha_s = (form.get("real_fecha") or "").strip()
    return _MB(
        fecha=_date.fromisoformat(fecha_s) if fecha_s else None,
        concepto=(form.get("real_concepto") or "").strip(),
        documento=(form.get("real_documento") or "").strip(),
        monto=_Dec(form.get("real_monto") or "0"),
        saldo=_Dec("0"),
        codigo=(form.get("real_codigo") or "").strip(),
        tipo=(form.get("real_tipo") or "").strip().upper(),
        oficina=(form.get("real_oficina") or "").strip(),
    )


@conciliacion_bp.route("/banco/crear-bancsis", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_crear_bancsis():
    """Crea una tx en BANCSIS a partir de un real_only y la concilia.

    Form: real_fecha, real_concepto, real_documento, real_monto, real_tipo,
          real_codigo, real_oficina, no_banco, [documento_override].
    """
    no_banco = _form_no_banco(request) or _BANCO_PICHINCHA
    try:
        real = _reconstruir_real(request.form)
    except Exception as e:
        flash_exc("Datos del movimiento inválidos", e)
        return redirect(url_for("conciliacion.hub"))
    if not real.fecha or float(real.monto) == 0 or real.tipo not in ("C", "D"):
        flash("Faltan datos del movimiento (fecha/monto/tipo).", "warn")
        return redirect(url_for("conciliacion.hub"))
    doc_override = (request.form.get("documento_override") or "").strip().upper() or None
    # TMT 2026-05-26 dueña: campos extra del modal — prov (cliente/proveedor)
    # + concepto_override (texto editable). Pre-llenados por la heurística
    # del matcher (categorizar.cliente), la dueña puede sobreescribir.
    prov_input = (request.form.get("prov") or "").strip().upper() or None
    concepto_override = (request.form.get("concepto_override") or "").strip() or None
    # TMT 2026-05-26 dueña: si vino vía fetch (AJAX), devolver JSON en
    # lugar de redirect — así el frontend quita la fila sin recargar.
    is_ajax = request.headers.get("X-Requested-With") == "fetch"
    try:
        res = crear_transaccion_desde_real(
            no_banco=no_banco,
            real=real,
            usuario=_usuario_actual(),
            documento=doc_override,
            prov=prov_input,
            concepto_override=concepto_override,
        )
    except Exception as e:
        nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"
        if is_ajax:
            return {"ok": False, "error": f"No se pudo crear en {nombre}: {e}"}, 400
        flash_exc(f"No se pudo crear el movimiento en {nombre}", e)
        return redirect(url_for("conciliacion.hub"))
    nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"
    if is_ajax:
        return {
            "ok": True,
            "id_transaccion": res["id_transaccion"],
            "documento": res["documento"],
            "saldo_nuevo": res["saldo_nuevo"],
            "banco_nombre": nombre,
        }
    flash(
        f"Creado en {nombre} Programa: #{res['id_transaccion']} ({res['documento']}, "
        f"saldo nuevo $ {res['saldo_nuevo']:,.2f}).",
        "ok",
    )
    return redirect(url_for("conciliacion.hub"))


@conciliacion_bp.route("/banco/crear-bancsis-agrupado", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_crear_bancsis_agrupado():
    """Crea UNA tx BANCSIS con la suma de N reals y concilia N:1.

    TMT 2026-05-26 dueña: en lugar de clickear 30 veces para crear 30
    comisiones/impuestos chicos, viene pre-renderizado un card en el tab
    'Solo en banco' con la suma + un click de confirmación.

    Form:
        no_banco (int), fecha (ISO opcional, default = max(reals.fecha)),
        concepto (str opcional, default auto), prov (str ≤5 opcional),
        reals_json (JSON: lista de dicts con fields del MovBanco).

    Devuelve siempre JSON (este endpoint sólo se llama vía AJAX desde el
    card sticky).
    """
    import json as _json
    from datetime import date as _date
    from decimal import Decimal as _Dec

    from modules.conciliacion.parser_banco import MovBanco as _MB

    no_banco = _form_no_banco(request) or _BANCO_PICHINCHA
    raw = request.form.get("reals_json") or "[]"
    try:
        items = _json.loads(raw)
    except _json.JSONDecodeError:
        return {"ok": False, "error": "reals_json inválido"}, 400
    if not isinstance(items, list) or len(items) < 2:
        return {"ok": False, "error": "se necesitan al menos 2 movs para agrupar"}, 400

    reals: list[_MB] = []
    for it in items:
        try:
            fecha_s = (it.get("fecha") or "").strip()
            reals.append(_MB(
                fecha=_date.fromisoformat(fecha_s) if fecha_s else None,
                concepto=(it.get("concepto") or "").strip(),
                documento=(it.get("documento") or "").strip(),
                monto=_Dec(str(it.get("monto") or "0")),
                saldo=_Dec("0"),
                codigo=(it.get("codigo") or "").strip(),
                tipo=(it.get("tipo") or "").strip().upper(),
                oficina=(it.get("oficina") or "").strip(),
            ))
        except Exception as e:
            return {"ok": False, "error": f"item inválido: {e}"}, 400

    fecha_str = (request.form.get("fecha") or "").strip()
    fecha = _date.fromisoformat(fecha_str) if fecha_str else None
    concepto = (request.form.get("concepto") or "").strip() or None
    prov = (request.form.get("prov") or "").strip().upper() or None

    try:
        res = crear_transaccion_agrupada_desde_reals(
            no_banco=no_banco,
            reals=reals,
            fecha=fecha,
            concepto=concepto,
            prov=prov,
            usuario=_usuario_actual(),
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    except Exception as e:
        return {"ok": False, "error": f"Error al crear: {e}"}, 500

    nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"
    return {
        "ok": True,
        "id_transaccion": res["id_transaccion"],
        "documento": res["documento"],
        "monto_neto": float(res["monto_neto"]),
        "n_matches": res["n_matches"],
        "saldo_nuevo": res["saldo_nuevo"],
        "banco_nombre": nombre,
    }


@conciliacion_bp.route("/banco/aceptar-real-only", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_aceptar_real_only():
    """Marca un real_only como diferencia legítima (sin crear tx BANCSIS)."""
    no_banco = _form_no_banco(request) or _BANCO_PICHINCHA
    try:
        real = _reconstruir_real(request.form)
    except Exception as e:
        flash_exc("Datos del movimiento inválidos", e)
        return redirect(url_for("conciliacion.hub"))
    if not real.fecha or real.tipo not in ("C", "D"):
        flash("Faltan datos del movimiento (fecha/tipo).", "warn")
        return redirect(url_for("conciliacion.hub"))
    confirmar_real_only(
        no_banco=no_banco,
        real=real,
        usuario=_usuario_actual(),
    )
    nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"
    flash(f"Movimiento del extracto de {nombre} aceptado como diferencia legítima.", "ok")
    return redirect(url_for("conciliacion.hub"))


@conciliacion_bp.route("/banco/match-manual", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_match_manual():
    """Fuerza match REAL ↔ BANCSIS (modal de match manual)."""
    no_banco = _form_no_banco(request) or _BANCO_PICHINCHA
    try:
        idtx = int(request.form.get("id_transaccion") or 0)
    except (TypeError, ValueError):
        idtx = 0
    if idtx <= 0:
        flash("Falta el id del movimiento de Programa a vincular.", "error")
        return redirect(url_for("conciliacion.hub"))
    try:
        real = _reconstruir_real(request.form)
    except Exception as e:
        flash_exc("Datos del movimiento inválidos", e)
        return redirect(url_for("conciliacion.hub"))
    n = match_manual(
        no_banco=no_banco,
        real=real,
        id_transaccion=idtx,
        usuario=_usuario_actual(),
    )
    nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"
    if n:
        flash(f"Match manual creado contra {nombre} Programa #{idtx}.", "ok")
    else:
        flash(f"No pude vincular contra {nombre} Programa #{idtx} (¿ya estaba conciliado?).", "warn")
    return redirect(url_for("conciliacion.hub"))


@conciliacion_bp.route("/banco/historico/marcar-conciliado", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_historico_marcar_conciliado():
    """TMT 2026-05-27 dueña: 'el historico no me esta apareciendo como
    opcion de conciliar'. Botón por fila en el panel históricos para
    marcar como conciliado sin pasar por el matcher. Setea conciliado_en
    + conciliado_por; no crea fila en banco_conciliacion_match.
    """
    try:
        ids_raw = request.form.getlist("id_historico[]") or [request.form.get("id_historico")]
        ids = [int(x) for x in ids_raw if x]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        return {"ok": False, "msg": "id_historico vacío"}, 400
    import db as _db
    usuario = _usuario_actual()
    n = _db.execute(
        """
        UPDATE scintela.banco_historicos_pendientes
           SET conciliado_en = CURRENT_TIMESTAMP,
               conciliado_por = %s
         WHERE id = ANY(%s)
           AND conciliado_en IS NULL
        """,
        (usuario[:50], ids),
    )
    return {"ok": True, "n": n or 0, "msg": f"{n or 0} marcados conciliados"}


@conciliacion_bp.route("/banco/match-click", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_match_click():
    """Click-to-pair AJAX: 1 fila banco (form fields) + 1 id_transaccion PC.

    Recibe JSON o form-encoded:
      real_fecha, real_concepto, real_documento, real_monto, real_tipo,
      real_codigo, real_oficina, id_transaccion
    Devuelve JSON {ok, msg} para que el JS remueva las filas sin reload.

    TMT 2026-05-27 dueña: 'necesito hacer como un drag and drop para ver
    cuales matchean'. Click + click + Match.
    """
    no_banco = _form_no_banco(request) or _BANCO_PICHINCHA
    try:
        idtx = int(request.form.get("id_transaccion") or 0)
    except (TypeError, ValueError):
        idtx = 0
    if idtx <= 0:
        return {"ok": False, "msg": "id_transaccion inválido"}, 400
    try:
        real = _reconstruir_real(request.form)
    except Exception as e:
        return {"ok": False, "msg": f"datos real inválidos: {e}"}, 400
    n = match_manual(
        no_banco=no_banco,
        real=real,
        id_transaccion=idtx,
        usuario=_usuario_actual(),
    )
    if n:
        return {"ok": True, "msg": f"Match contra PC #{idtx} OK", "id_transaccion": idtx}
    return {"ok": False, "msg": f"No pude vincular PC #{idtx} (¿ya estaba conciliado?)"}


@conciliacion_bp.route("/banco/candidatos-match", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def hub_candidatos_match():
    """Devuelve JSON con candidatos BANCSIS para el modal de match manual.

    Query: no_banco, fecha, monto, tipo (C/D).
    """
    try:
        no_banco = int(request.args.get("no_banco") or _BANCO_PICHINCHA)
        fecha_s = (request.args.get("fecha") or "").strip()
        monto = float(request.args.get("monto") or 0)
        tipo = (request.args.get("tipo") or "").strip().upper()
        from datetime import date as _date
        fecha = _date.fromisoformat(fecha_s) if fecha_s else None
    except Exception as e:
        return {"error": str(e), "candidatos": []}, 400
    if not fecha or monto == 0 or tipo not in ("C", "D"):
        return {"error": "fecha/monto/tipo requeridos", "candidatos": []}, 400
    candidatos = candidatos_match_manual(
        no_banco=no_banco,
        fecha_real=fecha,
        monto_real=monto,
        tipo_real=tipo,
    )
    return {"candidatos": candidatos}


@conciliacion_bp.route("/banco/historial", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_historial():
    """Lista de conciliaciones realizadas, con botón para deshacer."""
    from datetime import date as _date
    bancos = queries.bancos_disponibles()
    no_banco_arg = request.args.get("no_banco")
    no_banco = None
    if no_banco_arg:
        try:
            no_banco = int(no_banco_arg)
        except (TypeError, ValueError):
            no_banco = None
    desde = request.args.get("desde")
    hasta = request.args.get("hasta")
    try:
        desde_d = _date.fromisoformat(desde) if desde else None
    except ValueError:
        desde_d = None
    try:
        hasta_d = _date.fromisoformat(hasta) if hasta else None
    except ValueError:
        hasta_d = None
    incluir_deshechos = request.args.get("deshechos") == "1"
    rows = historial_matches(
        no_banco=no_banco,
        desde=desde_d,
        hasta=hasta_d,
        incluir_deshechos=incluir_deshechos,
        limit=300,
    )
    return render_template(
        "conciliacion/banco_historial.html",
        rows=rows,
        bancos=bancos,
        no_banco=no_banco,
        desde=desde or "",
        hasta=hasta or "",
        incluir_deshechos=incluir_deshechos,
    )


@conciliacion_bp.route("/banco/movimientos", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_movimientos():
    """TODOS los movimientos del banco (el extracto importado) con su estado
    conciliado/pendiente. A diferencia del historial (solo lo conciliado), acá
    aparecen también los movimientos que nunca se conciliaron — ej. las
    transferencias del 18/06 que la dueña no encontraba. TMT 2026-06-23.
    """
    from datetime import date as _date
    bancos = queries.bancos_disponibles()
    no_banco_arg = request.args.get("no_banco")
    no_banco = None
    if no_banco_arg:
        try:
            no_banco = int(no_banco_arg)
        except (TypeError, ValueError):
            no_banco = None
    if no_banco is None:
        no_banco = 10  # Pichincha por defecto (Intela solo opera con Pichincha)
    desde = request.args.get("desde")
    hasta = request.args.get("hasta")
    try:
        desde_d = _date.fromisoformat(desde) if desde else None
    except ValueError:
        desde_d = None
    try:
        hasta_d = _date.fromisoformat(hasta) if hasta else None
    except ValueError:
        hasta_d = None
    estado = request.args.get("estado") or "todos"
    if estado not in ("todos", "pendientes", "conciliados"):
        estado = "todos"
    rows = movimientos_banco_q(
        no_banco=no_banco,
        desde=desde_d,
        hasta=hasta_d,
        estado=estado,
        limit=1000,
    )
    n_pend = sum(1 for r in rows if not r.get("conciliado"))
    return render_template(
        "conciliacion/banco_movimientos.html",
        rows=rows,
        bancos=bancos,
        no_banco=no_banco,
        desde=desde or "",
        hasta=hasta or "",
        estado=estado,
        n_pend=n_pend,
        n_total=len(rows),
    )


@conciliacion_bp.route("/banco/deshacer", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_deshacer():
    """Soft-undo de un match. La fila queda con deshecho_en + deshecho_por.

    Acepta `next` opcional (whitelist) para volver a la pantalla desde la
    que se disparó (ej. /conciliacion/cambios). Default: banco_historial.
    """
    try:
        match_id = int(request.form.get("match_id") or 0)
    except (TypeError, ValueError):
        match_id = 0
    # Whitelist de destinos. Evita open-redirect.
    _next = (request.form.get("next") or "").strip()
    if _next == "deshacer":
        back = url_for("conciliacion.banco_deshacer_v2")
    elif _next == "conciliados":
        # TMT 2026-06-03 duena: 'deshacer deberia estar en conciliados, no en
        # una pagina aparte'. Volver al tab Conciliados de la sesion.
        try:
            _sid = int(request.form.get("sesion_id") or 0)
        except (TypeError, ValueError):
            _sid = 0
        back = url_for("conciliacion.banco_post_procesar", sesion_id=_sid, tab="conciliados")
    else:
        back = url_for("conciliacion.banco_historial")
    if match_id <= 0:
        flash("match_id inválido.", "error")
        return redirect(back)
    # TMT 2026-06-03: deshacer GRUPAL — si el match tiene confirm_batch_id
    # (mig 0071), borra todos los matches del mismo batch atómicamente.
    n, batch_id = romper_match_grupo(
        match_id=match_id,
        usuario=_usuario_actual(),
    )
    if n:
        if batch_id and n > 1:
            flash(f"Conciliación deshecha — {n} matches del grupo liberados. Vuelven a aparecer en el próximo extracto.", "ok")
        else:
            flash(f"Match #{match_id} deshecho. Vuelve a aparecer en el próximo extracto.", "ok")
        try:
            from modules.conciliacion import saldo_snapshot as _ss
            _ss.snapshot(_BANCO_PICHINCHA, "match_deshecho",
                         evento_ref=match_id, usuario=_usuario_actual(),
                         descripcion=f"deshacer match #{match_id}")
        except Exception:
            pass
        # Decrementar matches_hechos de la sesión abierta (counter UX).
        try:
            import db as _db
            _db.execute(
                """
                UPDATE scintela.banco_conciliacion_sesion
                   SET matches_hechos = GREATEST(0, matches_hechos - 1)
                 WHERE no_banco = %s
                   AND usuario = %s
                   AND cerrada_en IS NULL
                """,
                (_BANCO_PICHINCHA, _usuario_actual()[:50]),
            )
        except Exception:
            pass
    else:
        flash(f"No encontré el match #{match_id} (¿ya estaba deshecho?).", "warn")
    return redirect(back)


# TMT 2026-05-27 dueña: 'borremos estos conciliados. que los que importen
# sean las de dbase'. Bulk soft-delete de todos los matches manuales PC
# (banco_conciliacion_match) para que la fuente de verdad pase a ser dBase
# stat='*'. Soft-delete = reversible (la fila queda con deshecho_en, se ve
# tildando "Mostrar deshechos").
@conciliacion_bp.route("/banco/deshacer-todos", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_deshacer_todos():
    """Soft-deshace TODOS los matches PC activos (opcional: filtrado por banco).

    Lo pidió la dueña explícitamente: la fuente de verdad de qué está
    conciliado debe ser el dBase (stat='*'), no los matches manuales PC.
    """
    # Banco opcional via form. Si no viene → todos los bancos.
    no_banco_arg = request.form.get("no_banco")
    no_banco = None
    if no_banco_arg:
        try:
            no_banco = int(no_banco_arg)
        except (TypeError, ValueError):
            no_banco = None
    usuario = _usuario_actual()
    import db as _db
    if no_banco is not None:
        # TMT 2026-05-28: bug coherencia — los histo apuntados por estos
        # matches quedaban marcados como conciliados. Limpiar primero.
        _db.execute(
            """
            UPDATE scintela.banco_historicos_pendientes h
               SET conciliado_en = NULL,
                   conciliado_por = NULL,
                   conciliado_match_id = NULL
              FROM scintela.banco_conciliacion_match m
             WHERE h.conciliado_match_id = m.id
               AND m.no_banco = %(no_banco)s
               AND m.deshecho_en IS NULL
            """,
            {"no_banco": no_banco},
        )
        n = _db.execute(
            """
            UPDATE scintela.banco_conciliacion_match
               SET deshecho_en  = CURRENT_TIMESTAMP,
                   deshecho_por = %(usuario)s
             WHERE no_banco = %(no_banco)s
               AND deshecho_en IS NULL
            """,
            {"no_banco": no_banco, "usuario": usuario[:50]},
        )
    else:
        _db.execute(
            """
            UPDATE scintela.banco_historicos_pendientes h
               SET conciliado_en = NULL,
                   conciliado_por = NULL,
                   conciliado_match_id = NULL
              FROM scintela.banco_conciliacion_match m
             WHERE h.conciliado_match_id = m.id
               AND m.deshecho_en IS NULL
            """,
        )
        n = _db.execute(
            """
            UPDATE scintela.banco_conciliacion_match
               SET deshecho_en  = CURRENT_TIMESTAMP,
                   deshecho_por = %(usuario)s
             WHERE deshecho_en IS NULL
            """,
            {"usuario": usuario[:50]},
        )

    # TMT 2026-05-28 dueña: 'los desconciliamos despues y siguen apareciendo
    # como conciliados'. El dual-write seteaba stat='*' en
    # transacciones_bancarias al conciliar; este bulk también lo limpia para
    # los movs que tienen un match PC deshacho (= PC fue quien seteó el '*').
    # Idempotente: corre sobre activos + históricos. Sin tocar los que NUNCA
    # pasaron por un match PC (esos vienen marcados desde dBase y mandan).
    _stat_params: dict = {}
    _stat_where = ""
    if no_banco is not None:
        _stat_where = " AND t.no_banco = %(no_banco)s "
        _stat_params["no_banco"] = no_banco
    _db.execute(
        f"""
        UPDATE scintela.transacciones_bancarias t
           SET stat = NULL
         WHERE TRIM(COALESCE(t.stat, '')) = '*'
           {_stat_where}
           AND EXISTS (
               SELECT 1 FROM scintela.banco_conciliacion_match m
                WHERE m.id_transaccion = t.id_transaccion
                  AND m.deshecho_en IS NOT NULL
           )
           AND NOT EXISTS (
               SELECT 1 FROM scintela.banco_conciliacion_match m2
                WHERE m2.id_transaccion = t.id_transaccion
                  AND m2.deshecho_en IS NULL
           )
        """,
        _stat_params,
    )
    if n:
        flash(
            f"Deshechos {n} matches PC. dBase (stat='*') queda como única "
            f"fuente de conciliados. Se ven con 'Mostrar deshechos'.",
            "ok",
        )
        try:
            from modules.conciliacion import saldo_snapshot as _ss
            _ss.snapshot(no_banco or _BANCO_PICHINCHA, "deshacer_todos",
                         evento_ref=f"n={n}", usuario=usuario,
                         descripcion=f"bulk deshacer {n} matches")
        except Exception:
            pass
    else:
        flash("No había matches activos para deshacer.", "warn")
    # Mantener filtros si vinieron en el form
    return redirect(url_for(
        "conciliacion.banco_historial",
        no_banco=no_banco_arg or "",
    ))


_STAT_FANTASMA_SQL = """
    UPDATE scintela.transacciones_bancarias t
       SET stat = NULL
     WHERE TRIM(COALESCE(t.stat, '')) = '*'
       AND EXISTS (
           SELECT 1 FROM scintela.banco_conciliacion_match m
            WHERE m.id_transaccion = t.id_transaccion
              AND m.deshecho_en IS NOT NULL
       )
       AND NOT EXISTS (
           SELECT 1 FROM scintela.banco_conciliacion_match m2
            WHERE m2.id_transaccion = t.id_transaccion
              AND m2.deshecho_en IS NULL
       )
"""


@conciliacion_bp.route("/limpiar-stat-fantasma", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def limpiar_stat_fantasma():
    """Backfill one-shot: revierte stat='*' colgados de undos previos al fix.

    TMT 2026-05-28 dueña: 'el undo, lo tiene que sacar de conciliados'. Los
    undos hechos ANTES de que `romper_match` limpiara stat='*' dejaron filas
    fantasma-conciliadas en /bancos. Este endpoint barre todos los casos:
    stat='*' AND existe match deshacho AND no hay match activo → stat=NULL.
    Idempotente. Seguro de correr cualquier cantidad de veces.
    """
    import db as _db
    try:
        n = _db.execute(_STAT_FANTASMA_SQL)
        if n:
            flash(
                f"Limpiados {n} mov(s) que quedaron con flag conciliado tras "
                f"un undo. Refrescá /bancos para verlos sin la marca.",
                "ok",
            )
        else:
            flash("No había fantasmas para limpiar.", "info")
    except Exception as e:
        flash_exc("No pude correr el limpiador de stat fantasma", e)
    return redirect(url_for("conciliacion.banco_deshacer_v2"))


# ============================================================
# Hoja de conciliación imprimible — TMT 2026-05-28 (formato T-account).
# Dueña pidió el reporte clásico: saldo inicial + conciliados − conciliados
# = saldo conciliado + pendientes − pendientes = saldo final ≈ banco.
# ============================================================
@conciliacion_bp.route("/imprimir-banco", methods=["GET"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def imprimir_banco():
    from modules.conciliacion.hoja_queries import hoja_conciliacion

    # Defaults: banco Pichincha; rango = mes en curso.
    hoy = today_ec()
    try:
        no_banco = int(request.args.get("no_banco") or _BANCO_PICHINCHA)
    except (TypeError, ValueError):
        no_banco = _BANCO_PICHINCHA

    def _parse(arg: str, default: date) -> date:
        v = (request.args.get(arg) or "").strip()
        if not v:
            return default
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return default

    mes_ini = hoy.replace(day=1)
    desde = _parse("desde", mes_ini)
    hasta = _parse("hasta", hoy)
    if hasta < desde:
        desde, hasta = hasta, desde

    hoja = hoja_conciliacion(no_banco, desde, hasta)

    return render_template(
        "conciliacion/hoja.html",
        hoja=hoja,
        no_banco=no_banco,
        desde=desde,
        hasta=hasta,
    )
