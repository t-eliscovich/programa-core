"""Rutas del módulo conciliación."""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from modules.conciliacion import queries
from modules.conciliacion.matcher import matchear
from modules.conciliacion.matcher_depositos import (
    matchear_depositos,
    transacciones_en_rango,
)
from modules.conciliacion.parser import parse_csv
from modules.conciliacion.parser_xlsx import parse_xlsx
from modules.conciliacion.parser_banco import parse_banco_xlsx
from modules.conciliacion.matcher_banco import (
    matchear_extracto_banco,
    confirmar_match,
    confirmar_bancsis_only,
    confirmar_real_only,
    crear_transaccion_desde_real,
    match_manual,
    romper_match,
    historial as historial_matches,
    candidatos_match_manual,
)

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
        desde = min(fechas) - timedelta(days=3) if fechas else date.today() - timedelta(days=45)
        hasta = max(fechas) if fechas else date.today()

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
            usuario=request.remote_user or "conciliacion",
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
        fechas = [d.fecha for d in deps if d.fecha]
        if not fechas:
            flash("Ningún depósito del Excel tiene fecha válida.", "warn")
            return redirect(url_for("conciliacion.depositos"))
        desde = min(fechas) - timedelta(days=30)
        hasta = max(fechas) + timedelta(days=30)
        if hasta > date.today():
            hasta = date.today()

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
            usuario=(request.remote_user or "conciliacion"),
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
                usuario=(request.remote_user or "conciliacion"),
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


def _render_demo():
    """Renderea banco_resultado.html con datos sample para QA del diseño.

    TMT 2026-05-23 — temporal. Permite ver la pantalla del rediseño sin
    necesidad de subir un xlsx real. Borrar este branch cuando esté validado.
    """
    from modules.conciliacion.categorizar import categorizar, GRUPO_LABEL
    import json as _json

    def _cat(concepto, tipo):
        c = categorizar(concepto, tipo)
        return {
            "codigo": c.codigo, "grupo": c.grupo, "label": c.label,
            "abrev": c.abrev, "cliente": "", "descripcion": "", "fuente": "regex",
        }

    sample_matches = []
    sample_real = [
        {"fecha": "2026-05-14", "concepto": "TRANSFERENCIA DIRECTA DE AGUILAR RUIZ JACQUELINE DEL CARMEN",
         "documento": "53051839", "monto": 2517.51, "tipo": "C",
         "codigo": "001045", "oficina": "AG. NORTE",
         "cat": _cat("TRANSFERENCIA DIRECTA DE AGUILAR RUIZ JACQUELINE DEL CARMEN", "C") | {"cliente": "AGUILAR RUIZ JACQUELINE"}},
        {"fecha": "2026-05-14", "concepto": "DEPOSITO EFECTIVIZADO",
         "documento": "45212368", "monto": 871.22, "tipo": "C",
         "codigo": "001010", "oficina": "MERCADO CENTRAL",
         "cat": _cat("DEPOSITO EFECTIVIZADO", "C")},
        {"fecha": "2026-05-15", "concepto": "2605150C3EHP-INTELA C-PAG-ANTICIPO",
         "documento": "44669482", "monto": 15900.00, "tipo": "D",
         "codigo": "001045", "oficina": "AG. NORTE",
         "cat": _cat("2605150C3EHP-INTELA C-PAG-ANTICIPO", "D")},
        {"fecha": "2026-05-15", "concepto": "2605150C3DLS-INTELA C-PAG-DHL",
         "documento": "44652718", "monto": 460.26, "tipo": "D",
         "codigo": "001045", "oficina": "AG. NORTE",
         "cat": _cat("2605150C3DLS-INTELA C-PAG-DHL", "D")},
        {"fecha": "2026-05-16", "concepto": "Comision por servicio bancario",
         "documento": "10001", "monto": 8.50, "tipo": "D",
         "codigo": "001045", "oficina": "AG. NORTE",
         "cat": _cat("Comision por servicio bancario", "D")},
        {"fecha": "2026-05-16", "concepto": "1 ch.LTM",
         "documento": "0", "monto": 381.36, "tipo": "C",
         "codigo": "001045", "oficina": "AG. NORTE",
         "cat": _cat("1 ch.LTM", "C") | {"cliente": "LTM"}},
    ]
    sample_bancsis = [
        {"id_transaccion": 9999, "fecha": "2026-05-15", "concepto": "Pago proveedor LTM",
         "documento": "CH", "importe": 1200.00, "numreferencia": "5500", "tipo_real": "D",
         "prov": "LTM", "prov_nombre": "Almacen Lopez Martinez",
         "cat": _cat("Pago proveedor LTM", "D") | {"cliente": "Almacen Lopez Martinez"}},
        {"id_transaccion": 10001, "fecha": "2026-05-16", "concepto": "Dep. JTX",
         "documento": "DE", "importe": 800.00, "numreferencia": "", "tipo_real": "C",
         "prov": "JTX", "prov_nombre": "Juan Toledo Xavier",
         "cat": _cat("Dep. JTX", "C") | {"cliente": "Juan Toledo Xavier"}},
    ]
    data = {
        "no_banco": _BANCO_PICHINCHA,
        "matches": sample_matches,
        "real_only": sample_real,
        "bancsis_only": sample_bancsis,
        "saldo_real_final": 18118.00,
        "saldo_real_fecha": "2026-05-16",
        "saldo_bancsis_final": 17518.00,
        "saldo_bancsis_fecha": "2026-05-16",
        "total_real_only_signed": -13624.39,
        "total_bancsis_only_signed": -400.00,
        "extracto_desde": "2026-05-14",
        "extracto_hasta": "2026-05-16",
        "ventana_dias": 2,
        "bancsis_cargados": 8,
    }
    kpis = _calc_kpis(data)
    return render_template(
        "conciliacion/banco_resultado.html",
        data=data,
        matches_json=_json.dumps(sample_matches),
        real_only_json=_json.dumps(sample_real),
        bancsis_only_json=_json.dumps(sample_bancsis),
        no_banco=_BANCO_PICHINCHA,
        banco_nombre="PICHINCHA",
        **kpis,
    )


def _cat_to_dict(cat) -> dict:
    return {
        "codigo": getattr(cat, "codigo", "OTRO"),
        "grupo": getattr(cat, "grupo", "OTRO"),
        "label": getattr(cat, "label", "Sin categorizar"),
        "cliente": getattr(cat, "cliente", "") or "",
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
                },
                "score": m.score,
                "razon": m.razon,
                "es_exacto": m.score < 0.01,
                "cat": _cat_to_dict(_cats_match[i]) if i < len(_cats_match) and _cats_match[i] else _cat_to_dict(None),
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
                "cat": _cat_to_dict(_cats_bancsis[i]) if i < len(_cats_bancsis) and _cats_bancsis[i] else _cat_to_dict(None),
            }
            for i, b in enumerate(res.bancsis_only)
        ],
        "extracto_desde": res.extracto_desde.isoformat() if res.extracto_desde else None,
        "extracto_hasta": res.extracto_hasta.isoformat() if res.extracto_hasta else None,
        "ventana_dias": res.ventana_dias,
        "bancsis_cargados": res.bancsis_cargados,
        "saldo_real_final": float(res.saldo_real_final),
        "saldo_real_fecha": res.saldo_real_fecha.isoformat() if res.saldo_real_fecha else None,
        "saldo_bancsis_final": float(res.saldo_bancsis_final),
        "saldo_bancsis_fecha": res.saldo_bancsis_fecha.isoformat() if res.saldo_bancsis_fecha else None,
        "total_real_only_signed": float(res.total_real_only_signed),
        "total_bancsis_only_signed": float(res.total_bancsis_only_signed),
    }


def _calc_kpis(data: dict) -> dict:
    """Métricas derivadas para el template a partir del dict serializado."""
    saldo_real = data.get("saldo_real_final") or 0
    saldo_bancsis = data.get("saldo_bancsis_final") or 0
    sum_real_only = data.get("total_real_only_signed") or 0
    sum_bancsis_only = data.get("total_bancsis_only_signed") or 0
    esperado = saldo_bancsis + sum_real_only - sum_bancsis_only
    diff = saldo_real - esperado
    return {
        "n_match": len(data.get("matches") or []),
        "n_real_only": len(data.get("real_only") or []),
        "n_bancsis_only": len(data.get("bancsis_only") or []),
        "saldo_real": saldo_real,
        "saldo_bancsis": saldo_bancsis,
        "sum_real_only": sum_real_only,
        "sum_bancsis_only": sum_bancsis_only,
        "esperado": esperado,
        "diff": diff,
        "cuadra": abs(diff) < 100,
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
        # TMT 2026-05-23 — branch QA temporal. ?demo=1 renderea el resultado
        # con datos sample. Quitar cuando el rediseño esté validado.
        if request.args.get("demo") == "1":
            return _render_demo()
        bancos = queries.bancos_disponibles()
        ultimos = queries.ultimos_extractos(limit=5)
        return render_template(
            "conciliacion/banco_upload.html",
            bancos=bancos,
            no_banco_default=_BANCO_PICHINCHA,
            ultimos=ultimos,
        )

    # ── POST ────────────────────────────────────────────────────────────
    # Banco elegido (default: Pichincha).
    try:
        no_banco = int(request.form.get("no_banco") or _BANCO_PICHINCHA)
    except (TypeError, ValueError):
        no_banco = _BANCO_PICHINCHA

    f = request.files.get("archivo")
    if not f or not f.filename:
        flash("Subí un archivo .xlsx del banco.", "warn")
        return redirect(url_for("conciliacion.hub"))
    if not f.filename.lower().endswith(".xlsx"):
        flash("El archivo tiene que ser .xlsx.", "warn")
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

    # JSON inline (para botón "Confirmar matches" sin guardar en session).
    import json as _json
    matches_json = _json.dumps(data.get("matches") or [], separators=(",", ":"))
    real_only_json = _json.dumps(data.get("real_only") or [], separators=(",", ":"))
    bancsis_only_json = _json.dumps(data.get("bancsis_only") or [], separators=(",", ":"))

    return render_template(
        "conciliacion/banco_resultado.html",
        data=data,
        matches_json=matches_json,
        real_only_json=real_only_json,
        bancsis_only_json=bancsis_only_json,
        no_banco=no_banco,
        banco_nombre=banco_nombre,
        **kpis,
    )


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
    usuario = request.remote_user or "conciliacion"
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
    confirmar_bancsis_only(no_banco, idtx, usuario=(request.remote_user or "conciliacion"))
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


def _reconstruir_real(form) -> "MovBanco":
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
    try:
        res = crear_transaccion_desde_real(
            no_banco=no_banco,
            real=real,
            usuario=(request.remote_user or "conciliacion"),
            documento=doc_override,
        )
    except Exception as e:
        nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"
        flash_exc(f"No se pudo crear el movimiento en {nombre}", e)
        return redirect(url_for("conciliacion.hub"))
    nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"
    flash(
        f"Creado en {nombre} Programa: #{res['id_transaccion']} ({res['documento']}, "
        f"saldo nuevo $ {res['saldo_nuevo']:,.2f}).",
        "ok",
    )
    return redirect(url_for("conciliacion.hub"))


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
        usuario=(request.remote_user or "conciliacion"),
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
        usuario=(request.remote_user or "conciliacion"),
    )
    nombre = queries.nombre_banco(no_banco) or f"Banco {no_banco}"
    if n:
        flash(f"Match manual creado contra {nombre} Programa #{idtx}.", "ok")
    else:
        flash(f"No pude vincular contra {nombre} Programa #{idtx} (¿ya estaba conciliado?).", "warn")
    return redirect(url_for("conciliacion.hub"))


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


@conciliacion_bp.route("/banco/deshacer", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_deshacer():
    """Soft-undo de un match. La fila queda con deshecho_en + deshecho_por."""
    try:
        match_id = int(request.form.get("match_id") or 0)
    except (TypeError, ValueError):
        match_id = 0
    if match_id <= 0:
        flash("match_id inválido.", "error")
        return redirect(url_for("conciliacion.banco_historial"))
    n = romper_match(
        match_id=match_id,
        usuario=(request.remote_user or "conciliacion"),
    )
    if n:
        flash(f"Match #{match_id} deshecho. Vuelve a aparecer en el próximo extracto.", "ok")
    else:
        flash(f"No encontré el match #{match_id} (¿ya estaba deshecho?).", "warn")
    return redirect(url_for("conciliacion.banco_historial"))
