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


def _serialize_resultado_banco(res, no_banco: int) -> dict:
    """ConciliacionBanco → dict serializable para session."""
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
                },
                "score": m.score,
                "razon": m.razon,
                "es_exacto": m.score < 0.01,
            }
            for m in res.matches
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
            }
            for r in res.real_only
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
            }
            for b in res.bancsis_only
        ],
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
        return render_template("conciliacion/banco_upload.html")

    # ── POST ────────────────────────────────────────────────────────────
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
        resultado = matchear_extracto_banco(movs_real, no_banco=_BANCO_PICHINCHA)
    except Exception as e:
        flash_exc("Falló el matching contra BANCSIS.", e)
        return redirect(url_for("conciliacion.hub"))

    data = _serialize_resultado_banco(resultado, _BANCO_PICHINCHA)
    kpis = _calc_kpis(data)

    # JSON inline (para botón "Confirmar matches" sin guardar en session).
    import json as _json
    matches_json = _json.dumps(data.get("matches") or [], separators=(",", ":"))

    return render_template(
        "conciliacion/banco_resultado.html",
        data=data,
        matches_json=matches_json,
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
        SELECT id_transaccion, fecha, documento, importe, concepto, no_banco
          FROM scintela.transacciones_bancarias
         WHERE fecha BETWEEN '2026-05-12' AND '2026-05-20'
         ORDER BY fecha DESC, id_transaccion DESC
         LIMIT 8
        """
    ) or []
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
            }
            for s in sample
        ],
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
    confirmar_bancsis_only(_BANCO_PICHINCHA, idtx, usuario=(request.remote_user or "conciliacion"))
    flash(f"BANCSIS #{idtx} aceptado.", "ok")
    return redirect(url_for("conciliacion.hub"))
