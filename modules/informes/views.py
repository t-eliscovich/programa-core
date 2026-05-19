"""Informes gerenciales — read-only v1."""
import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response

from . import queries

informes_bp = Blueprint(
    "informes",
    __name__,
    template_folder="templates",
)


def _safe(fn, default):
    """Run a query; on error return (default, error_message)."""
    try:
        return fn(), None
    except Exception as e:
        return default, str(e)


@informes_bp.route("/balance")
@requiere_login
@requiere_permiso("informes.ver")
def balance():
    # Provisiones diarias automáticas (replica MENU.PRG L282-333).
    # Idempotente — sólo aplica si HOY > última fecha guardada y no es
    # domingo. Si falla, no rompe el balance — la migración de la tabla
    # sistema_meta puede no haber corrido todavía (decorador defensivo).
    #
    # ?forzar_provisiones=1 → corre UNA aplicación extra incluso si ya
    # se corrió hoy. Útil para emparejar contra dBase cuando estábamos
    # atrasados N días: cargar la URL con ese param N veces. NO usar
    # sin saber lo que hacés (cada click duplica los montos).
    forzar = request.args.get("forzar_provisiones") in ("1", "true", "yes")
    try:
        prov_result = queries.correr_provisiones_diarias(forzar=forzar)
    except Exception as e:  # noqa: BLE001
        prov_result = {"aplicado": False, "error": str(e)}

    # ITEM #5 — Auto-cierre de stock mensual (replica MENU.PRG L246-263).
    # Idempotente. Si ya se cerró el mes destino, no hace nada. Si falla,
    # no rompe el balance — la tabla scintela.sistema_meta puede no estar
    # inicializada todavía. Decorador defensivo.
    try:
        from modules.iniciales.views import auto_cerrar_mes_si_corresponde
        auto_cerrar_mes_si_corresponde()
    except Exception as e:  # noqa: BLE001
        {"aplicado": False, "error": str(e)}

    data, error = _safe(queries.informe_balance, {})
    return render_template(
        "informes/balance.html",
        b=data, error=error, provisiones=prov_result,
    )


# Feature A — tab Compras en /informes/balance (TMT 2026-05-19 v6).
@informes_bp.route("/balance/compras")
@requiere_login
@requiere_permiso("informes.ver")
def balance_compras():
    """Drill-down de compras del período. Reuse de /informes/balance."""
    from datetime import date as _date
    hoy = _date.today()
    try:
        anio = int(request.args.get("anio") or hoy.year)
    except (TypeError, ValueError):
        anio = hoy.year
    try:
        mes = int(request.args.get("mes") or hoy.month)
    except (TypeError, ValueError):
        mes = hoy.month
    mes = max(1, min(mes, 12))
    prov = (request.args.get("prov") or "").strip().upper() or None
    try:
        num_v = int(request.args.get("v") or 0) or None
    except (TypeError, ValueError):
        num_v = None
    try:
        data = queries.compras_del_periodo(
            anio=anio, mes=mes, prov=prov, num_v=num_v,
        )
        error = None
    except Exception as e:  # noqa: BLE001
        data, error = {"filas": [], "total_importe": 0, "total_kg": 0,
                       "n_filas": 0, "prov_options": [],
                       "anio": anio, "mes": mes,
                       "prov_actual": prov, "num_v_actual": num_v}, str(e)
    return render_template(
        "informes/balance_compras.html",
        data=data, anio=anio, mes=mes,
        prov=prov, num_v=num_v, error=error,
    )


# Feature B — matriz 12m (TMT 2026-05-19 v6).
@informes_bp.route("/informes/historico-12m")
@requiere_login
@requiere_permiso("informes.ver")
def historico_12m():
    """Matriz comparativa últimos N meses (default 12)."""
    try:
        n = int(request.args.get("n") or 12)
    except (TypeError, ValueError):
        n = 12
    n = max(1, min(n, 24))
    try:
        data = queries.historico_12m_matriz(meses_atras=n)
        error = None
    except Exception as e:  # noqa: BLE001
        data, error = {"meses": [], "lineas": [],
                       "snapshots_existentes": 0, "meses_total": n,
                       "meses_sin_snap": []}, str(e)
    return render_template(
        "informes/historico_12m.html",
        data=data, n=n, error=error,
    )


@informes_bp.route("/balance/utilidad-debug")
@requiere_login
@requiere_permiso("informes.ver")
def utilidad_debug():
    """Diagnóstico para identificar por qué la UTILIDAD del balance no
    coincide con el dBase. Muestra:
      - Fila de scintela.historia usada como PATANT (todos los campos)
      - Componentes de PATR (subt, vsto, vqx, umaq, uact, uret, antic, totp)
      - 4 fórmulas alternativas con su resultado, para que el gerente
        identifique cuál da el número correcto.
    """
    import db
    data, error = _safe(queries.informe_balance, {})

    # Levantar la fila de historia que se usa como PATANT
    hist_row = db.fetch_one(
        """
        SELECT *
        FROM scintela.historia
        WHERE fecha < date_trunc('month', CURRENT_DATE)::date
        ORDER BY fecha DESC
        LIMIT 1
        """
    ) or {}

    # Levantar TODAS las filas de historia (últimas 24) para auditar qué
    # está cargado y comparar con el dBase. Usuario reportó 2026-05-06:
    # April=20,115,887, Marzo=176,556,980 (probable typo, debería ser
    # ~17,655,698 dado que April es 20M).
    hist_all = db.fetch_all(
        """
        SELECT fecha, patrimonio, ustock, uqui, usret, usuti, kvent, uvent
        FROM scintela.historia
        ORDER BY fecha DESC
        LIMIT 24
        """
    ) or []

    # Componentes de PATR
    componentes = {
        "subt":   float(data.get("subt") or 0),
        "vsto_display":  float(data.get("vsto") or 0),  # post-override
        "vqx":    float(data.get("vqx") or 0),
        "umaq":   float(data.get("umaq") or 0),
        "uact":   float(data.get("uact") or 0),
        "uret":   float(data.get("uret") or 0),
        "antic":  float(data.get("antic") or 0),
        "totp":   float(data.get("totp") or 0),
    }
    vsto_orig = float(hist_row.get("ustock") or 0)
    componentes["vsto_orig"] = vsto_orig

    # PATR alternativos
    patr_post = (
        componentes["subt"] + componentes["vsto_display"]
        + componentes["vqx"] + componentes["umaq"] + componentes["uact"]
        + componentes["uret"] + componentes["antic"] - componentes["totp"]
    )
    patr_pre = (
        componentes["subt"] + componentes["vsto_orig"]
        + componentes["vqx"] + componentes["umaq"] + componentes["uact"]
        + componentes["uret"] + componentes["antic"] - componentes["totp"]
    )

    patrimonio_hist = float(hist_row.get("patrimonio") or 0)
    usret_hist = float(hist_row.get("usret") or 0)
    usuti_hist = float(hist_row.get("usuti") or 0)

    # Fórmulas alternativas
    formulas = [
        {
            "label": "A) patr_pre_override − patrimonio_hist (= lo actual)",
            "patr": patr_pre, "patant": patrimonio_hist,
            "result": patr_pre - patrimonio_hist,
        },
        {
            "label": "B) patr_post_override − patrimonio_hist",
            "patr": patr_post, "patant": patrimonio_hist,
            "result": patr_post - patrimonio_hist,
        },
        {
            "label": "C) patr_pre_override − (patrimonio_hist − usret_hist)",
            "patr": patr_pre, "patant": patrimonio_hist - usret_hist,
            "result": patr_pre - (patrimonio_hist - usret_hist),
        },
        {
            "label": "D) patr_post_override − (patrimonio_hist − usret_hist)",
            "patr": patr_post, "patant": patrimonio_hist - usret_hist,
            "result": patr_post - (patrimonio_hist - usret_hist),
        },
        {
            "label": "E) usuti_hist (= utilidad guardada en cierre anterior, sin recalcular)",
            "patr": 0, "patant": 0,
            "result": usuti_hist,
        },
        {
            "label": "F) patr_pre_override − patrimonio_hist + usret_hist (= delta + retiros del cierre)",
            "patr": patr_pre, "patant": patrimonio_hist - usret_hist,
            "result": patr_pre - patrimonio_hist + usret_hist,
        },
        {
            "label": "G) patr_post_override − patrimonio_hist + usret_hist",
            "patr": patr_post, "patant": patrimonio_hist - usret_hist,
            "result": patr_post - patrimonio_hist + usret_hist,
        },
    ]

    return render_template(
        "informes/utilidad_debug.html",
        hist_row=hist_row,
        hist_all=hist_all,
        componentes=componentes,
        patr_pre=patr_pre, patr_post=patr_post,
        patrimonio_hist=patrimonio_hist,
        usret_hist=usret_hist, usuti_hist=usuti_hist,
        formulas=formulas,
        error=error,
    )


@informes_bp.route("/cartera")
@requiere_login
@requiere_permiso("informes.ver")
def cartera():
    filas, error = _safe(queries.cartera_por_cliente, [])
    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("codigo_cli", "Código"), ("nombre", "Cliente"),
                ("n_facturas", "# facturas"), ("saldo_total", "Saldo"),
                ("factura_mas_vieja", "Fact. más vieja"),
                ("vence_mas_viejo", "Vence más vieja"),
            ],
            filename="cartera_clientes.csv",
        )
    total = sum(float(r["saldo_total"] or 0) for r in filas)
    return render_template("informes/cartera.html", filas=filas, total=total, error=error)


@informes_bp.route("/deudas")
@requiere_login
@requiere_permiso("informes.ver")
def deudas():
    filas, error = _safe(queries.deudas_por_proveedor, [])
    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("codigo_prov", "Código"), ("nombre", "Proveedor"),
                ("n_posdats", "# posdatados"), ("saldo_total", "Saldo"),
                ("posdat_mas_vieja", "Posdat más vieja"),
                ("vence_mas_viejo", "Vence más vieja"),
            ],
            filename="deudas_proveedores.csv",
        )
    total = sum(float(r["saldo_total"] or 0) for r in filas)
    return render_template("informes/deudas.html", filas=filas, total=total, error=error)


@informes_bp.route("/_diag/stock")
@requiere_login
@requiere_permiso("informes.ver")
def diag_stock():
    """TMT 2026-05-18 — diagnóstico del flujo de stock terminado.

    Muestra las queries crudas que alimentan /stock para entender por qué
    Terminado=0. Pensado para que la dueña abra la URL una vez y mande
    screenshot — más eficiente que pelear con SSM PowerShell quoting.
    """
    from datetime import date as _date

    import db
    y = _date.today().year

    def _safe_q(sql, params=()):
        try:
            return db.fetch_all(sql, params) or []
        except Exception as e:
            return [{"error": str(e)}]

    tinto = _safe_q("""
        SELECT EXTRACT(MONTH FROM fecha)::int AS mes,
               COUNT(*) AS n,
               SUM(COALESCE(kg, 0))::int AS kg_col,
               SUM(COALESCE(kgn, 0))::int AS kgn_col,
               SUM(COALESCE(toper,0)+COALESCE(jersey,0)+COALESCE(pique,0)
                 + COALESCE(messi,0)+COALESCE(james,0)+COALESCE(franela,0)
                 + COALESCE(otros,0)+COALESCE(j3,0)+COALESCE(jlyc,0)
                 + COALESCE(flyc,0)+COALESCE(falso,0)+COALESCE(kiana,0))::int AS suma_indiv
          FROM scintela.tinto
         WHERE EXTRACT(YEAR FROM fecha) = %s
         GROUP BY 1 ORDER BY 1
    """, (y,))

    iniciales = _safe_q("""
        SELECT yy, mesnum, hilado, tejido, terminado, vq,
               um, uk, uf, uq
          FROM scintela.iniciales
         WHERE yy = %s
         ORDER BY mesnum
    """, (y,))

    facturas_mes = _safe_q("""
        SELECT EXTRACT(MONTH FROM fecha)::int AS mes,
               COUNT(*) AS n,
               SUM(COALESCE(kg, 0))::int AS kg
          FROM scintela.factura
         WHERE EXTRACT(YEAR FROM fecha) = %s
           AND COALESCE(stat, '') <> 'X'
         GROUP BY 1 ORDER BY 1
    """, (y,))

    compras_tipo = _safe_q("""
        SELECT UPPER(TRIM(COALESCE(tipo, ''))) AS tipo,
               COUNT(*) AS n,
               SUM(COALESCE(kg, 0))::int AS kg,
               SUM(COALESCE(importe, 0))::int AS importe
          FROM scintela.compra
         WHERE EXTRACT(YEAR FROM fecha) = %s
           AND COALESCE(stat, '') != 'Y'
         GROUP BY 1 ORDER BY 1
    """, (y,))

    return render_template("informes/diag_stock.html",
                           anio=y,
                           tinto=tinto, iniciales=iniciales,
                           facturas_mes=facturas_mes, compras_tipo=compras_tipo)


@informes_bp.route("/snapshot-mes", methods=["POST"])
@requiere_login
@requiere_permiso("informes.ver")
def snapshot_mes():
    """Cierra snapshot mensual en scintela.historia para el mes indicado.

    POST con form (anio, mes). Idempotente.
    """
    from datetime import date as _date
    try:
        anio = int(request.form.get("anio") or _date.today().year)
        mes  = int(request.form.get("mes")  or _date.today().month)
    except (TypeError, ValueError):
        flash("Parámetros inválidos.", "error")
        return redirect(url_for("informes.fuentes_y_usos"))
    mes = max(1, min(mes, 12))
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.crear_snapshot_historia(anio, mes, usuario=usuario)
        if r.get("aplicado"):
            flash(f"Snapshot {mes:02d}/{anio} creado.", "ok")
        else:
            flash(r.get("razon", "Nada que hacer."), "info")
    except Exception as e:
        flash_exc("Snapshot falló", e)
    return redirect(url_for("informes.fuentes_y_usos", anio=anio, mes=mes))


@informes_bp.route("/snapshot-backfill", methods=["POST"])
@requiere_login
@requiere_permiso("informes.ver")
def snapshot_backfill():
    """Backfill: crea snapshots para los últimos N meses (default 3).

    POST con form (meses=N). Idempotente.
    """
    from datetime import date as _date
    try:
        n = int(request.form.get("meses") or 3)
    except (TypeError, ValueError):
        n = 3
    # TMT 2026-05-19 v6 — Feature B permite hasta 12 meses (antes 12 cap).
    n = max(1, min(n, 12))
    hoy = _date.today()
    aplicados, saltados = [], []
    usuario = (g.user or {}).get("username", "web")
    for i in range(1, n + 1):
        # Mes pasado i: retrocedemos i meses desde el primero del mes actual
        m = hoy.month - i
        a = hoy.year
        while m < 1:
            m += 12
            a -= 1
        try:
            r = queries.crear_snapshot_historia(a, m, usuario=usuario)
            (aplicados if r.get("aplicado") else saltados).append(f"{m:02d}/{a}")
        except Exception as e:
            saltados.append(f"{m:02d}/{a} (error: {e})")
    if aplicados:
        flash(f"Backfilled: {', '.join(aplicados)}.", "ok")
    if saltados:
        flash(f"Salteados (ya existían o error): {', '.join(saltados)}.", "info")
    return redirect(url_for("informes.fuentes_y_usos"))


@informes_bp.route("/fuentes-y-usos")
@requiere_login
@requiere_permiso("informes.ver")
def fuentes_y_usos():
    """Cuadro de Fuentes y Usos en un rango DESDE-HASTA (mensual).

    Pedido dueña 2026-05-19 (docx "Para Claude 2", item 14): seleccionar
    DESDE-HASTA y mostrar 2 columnas con totales iguales (réplica de
    INFORMES.PRG::PROCEDURE FUENTES L1654-1727). Granularidad: mensual,
    porque la data viene de scintela.historia (un snapshot por mes).
    """
    from datetime import date
    hoy = date.today()

    def _p(k, default):
        try:
            return int(request.args.get(k) or default)
        except (TypeError, ValueError):
            return default

    # Default: ventana de 1 mes terminando en mes actual (compatible con
    # comportamiento anterior cuando solo había un picker).
    hasta_anio = _p("hasta_anio", _p("anio", hoy.year))
    hasta_mes  = _p("hasta_mes",  _p("mes",  hoy.month))
    desde_anio = _p("desde_anio", hasta_anio if hasta_mes > 1 else hasta_anio - 1)
    desde_mes  = _p("desde_mes",  hasta_mes - 1 if hasta_mes > 1 else 12)
    hasta_mes  = max(1, min(hasta_mes, 12))
    desde_mes  = max(1, min(desde_mes, 12))

    try:
        data = queries.fuentes_y_usos(
            desde_anio=desde_anio, desde_mes=desde_mes,
            hasta_anio=hasta_anio, hasta_mes=hasta_mes,
        )
    except Exception as e:
        data = {
            "anio_ini": desde_anio, "mes_ini": desde_mes,
            "anio": hasta_anio, "mes": hasta_mes,
            "fuentes": [], "usos": [],
            "total_fuentes": 0, "total_usos": 0,
            "delta_liquido": 0, "delta_banco": 0,
            "h_ini": {}, "h_fin": {},
            "error": str(e),
        }
    return render_template(
        "informes/fuentes_usos.html",
        data=data,
        # Para back-compat con el template (siguen existiendo `anio`/`mes`
        # como los del HASTA, además de los explícitos `desde_*`/`hasta_*`).
        anio=hasta_anio, mes=hasta_mes,
        desde_anio=desde_anio, desde_mes=desde_mes,
        hasta_anio=hasta_anio, hasta_mes=hasta_mes,
    )


@informes_bp.route("/flujo")
@requiere_login
@requiere_permiso("informes.ver")
def flujo():
    dias = request.args.get("dias", default=30, type=int)
    filas, error = _safe(lambda: queries.flujo_ultimos_dias(dias), [])
    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"), ("cheques", "Cheques"), ("facturas", "Facturas"),
                ("pichincha", "Pichincha"), ("inter", "Internacional"),
                ("posdat1", "Pos.dat 1"), ("posdat2", "Pos.dat 2"),
                ("mprima", "M. prima"), ("gastos", "Gastos"),
                ("saldo", "Saldo"), ("pagos", "Pagos"),
                ("dolares", "Dólares"), ("usaldo", "USD saldo"),
            ],
            filename=f"flujo_{dias}d.csv",
        )
    return render_template("informes/flujo.html", filas=filas, dias=dias, error=error)


@informes_bp.route("/flujo/grafico")
@requiere_login
@requiere_permiso("informes.ver")
def flujo_grafico():
    """Gráfico de flujo de caja — la vista del gerente, con proyección.

    Equivalente moderno del GRAFICO del viejo dBase: muestra historia
    reciente + proyección a 365 días (postdatados, provisiones, pagos
    programados ya cargados en scintela.flujo).
    """
    # Default 70d para matchear el rango del chart dBase (May 11 → Jul 20).
    # El MIN del flujo cae a los 68-70 días.
    ventana = request.args.get("ventana", default=70, type=int)
    ventana = max(7, min(ventana, 365))  # clamp

    # Fuente del flujo:
    #   1. `flujo_calculado()` — proyección en vivo desde cheques+posdat+saldos.
    #      Es la fuente primaria desde 2026-04-29 (batch 19) porque la tabla
    #      legacy scintela.flujo nunca se carga en producción.
    #   2. `flujo_proyeccion()` — lee scintela.flujo (la tabla legacy alimentada
    #      por el dBase). Sólo si tenemos filas ahí Y `?fuente=tabla` explícito.
    #
    # Esto resuelve el bug histórico "el gráfico nunca muestra nada": ahora
    # arranca con un saldo bancario real y proyecta los cheques pendientes
    # de cobro y los posdat pendientes de pago.
    fuente = (request.args.get("fuente") or "calculado").lower()
    # Modo "peor caso": ignorar cheques en cartera (asumir que ninguno se
    # cobra). Útil cuando se sospecha que la cartera Z está stale.
    ignorar_cheques = request.args.get("ignorar_cheques") in ("1", "true", "yes", "on")
    if fuente == "tabla":
        filas, error = _safe(
            lambda: queries.flujo_proyeccion(dias_atras=14, dias_adelante=365),
            [],
        )
    else:
        filas, error = _safe(
            lambda: queries.flujo_calculado(
                dias_atras=14, dias_adelante=365,
                ignorar_cheques=ignorar_cheques,
            ),
            [],
        )

    # Pass dates as ISO strings — the JS parses them deterministically
    # instead of relying on the browser's Date(string) forgiveness.
    datos = [
        {
            "fecha":     r["fecha"].isoformat() if hasattr(r["fecha"], "isoformat") else r["fecha"],
            "saldo":     float(r["saldo"] or 0),
            "cheques":   float(r["cheques"] or 0),
            "facturas":  float(r["facturas"] or 0),
            "posdat1":   float(r["posdat1"] or 0),
            "posdat2":   float(r["posdat2"] or 0),
            "pichincha": float(r["pichincha"] or 0),
            "inter":     float(r["inter"] or 0),
            "mprima":    float(r["mprima"] or 0),
            "gastos":    float(r["gastos"] or 0),
            "pagos":     float(r["pagos"] or 0),
            "dolares":   float(r["dolares"] or 0),
        }
        for r in filas
    ]

    # Lista de posdat egresos para mostrar al lado del gráfico — ayuda al
    # gerente a saber QUÉ se está restando, no sólo el total agregado.
    posdat_egresos, _ = _safe(
        lambda: queries.posdat_egresos_proximos(dias_adelante=365), [],
    )
    egresos_lista = [
        {
            "fecha_efectiva": r["fecha_efectiva"].isoformat()
                if hasattr(r["fecha_efectiva"], "isoformat") else r["fecha_efectiva"],
            "fechad": r["fechad"].isoformat()
                if r.get("fechad") and hasattr(r["fechad"], "isoformat") else None,
            "prov":     r.get("prov") or "",
            "concepto": r.get("concepto") or "",
            "importe":  float(r.get("importe") or 0),
            "banc":     int(r.get("banc") or 0),
            "vencido":  bool(r.get("fechad") and r["fechad"] < date.today()),
        }
        for r in posdat_egresos
    ]

    # Plazos PLAZ.COBR / PLAZ.DEUDA — calculados server-side con la fórmula
    # de dBase (plazo otorgado ponderado por importe). El JS antes los
    # calculaba sobre la ventana del gráfico con `fecha-hoy`, lo cual no
    # representa el plazo real otorgado y daba números muy bajos (23/25 vs
    # 32.9/96.7 de dBase).
    plazos, _ = _safe(lambda: queries.plazos_dbase(), {"cobro": 0, "deuda": 0})

    return render_template(
        "informes/flujo_grafico.html",
        datos=datos,
        egresos_lista=egresos_lista,
        hoy=date.today().isoformat(),
        ventana_dias=ventana,
        ignorar_cheques=ignorar_cheques,
        plazos=plazos,
        error=error,
    )


# ---------------------------------------------------------------------------
# Flujo — carga manual / CSV  (v1: la forma más rápida de poblar scintela.flujo
# sin necesidad de importar desde el dBase viejo ni correr scripts a mano).
# ---------------------------------------------------------------------------

_FLUJO_HEADERS = [
    "fecha", "saldo", "cheques", "facturas",
    "posdat1", "posdat2", "pichincha", "inter",
    "mprima", "gastos", "pagos", "dolares", "usaldo",
]


def _parse_fecha(value: str):
    """Accept 2026-04-16, 16/04/2026, 16-04-2026."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_monto(value):
    """Accept 1234.56, 1.234,56 (es-EC), empty → None."""
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    # es-EC: punto miles, coma decimal. Si hay coma, asumí ese formato.
    if "," in s and s.count(",") == 1:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


@informes_bp.route("/flujo/cargar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("informes.ver")
def flujo_cargar():
    """Carga manual o por CSV de la tabla scintela.flujo.

    - GET ?plantilla=1 → descarga un CSV template vacío.
    - GET              → muestra el formulario (manual + upload).
    - POST             → procesa CSV o una sola fila manual.
    """
    # --- CSV template download --------------------------------------------
    if request.method == "GET" and request.args.get("plantilla"):
        buf = io.StringIO()
        buf.write("\ufeff")
        w = csv.writer(buf, delimiter=";")
        w.writerow(_FLUJO_HEADERS)
        # Una fila de ejemplo para que el usuario vea el formato.
        w.writerow([
            date.today().isoformat(), "0", "0", "0",
            "0", "0", "0", "0", "0", "0", "0", "0", "0",
        ])
        return Response(
            buf.getvalue().encode("utf-8"),
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="flujo_plantilla.csv"',
            },
        )

    resultado = None
    errores: list[str] = []

    if request.method == "POST":
        usuario = (g.user or {}).get("username", "web")
        rows: list[dict] = []

        # Rama 1: upload de CSV.
        f = request.files.get("archivo")
        if f and f.filename:
            try:
                raw = f.stream.read().decode("utf-8-sig", errors="replace")
            except Exception as e:  # pragma: no cover — defensivo
                errores.append(f"No pude leer el archivo: {e}")
                raw = ""
            if raw:
                # Detectá ; o , como separador mirando la primera línea.
                first = raw.split("\n", 1)[0]
                delim = ";" if first.count(";") >= first.count(",") else ","
                reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
                for i, r in enumerate(reader, start=2):  # línea 1 = header
                    # Normalizá keys (lowercase, strip, quitar BOM residual).
                    rn = {(k or "").strip().lower().lstrip("\ufeff"): v for k, v in r.items()}
                    fecha = _parse_fecha(rn.get("fecha"))
                    if fecha is None:
                        errores.append(f"Línea {i}: fecha inválida ({rn.get('fecha')!r})")
                        continue
                    row: dict = {"fecha": fecha}
                    for col in queries.FLUJO_COLS:
                        if col in rn and rn[col] not in (None, ""):
                            monto = _parse_monto(rn[col])
                            if monto is None:
                                errores.append(
                                    f"Línea {i} col {col}: monto inválido ({rn[col]!r})"
                                )
                            else:
                                row[col] = monto
                    rows.append(row)

        # Rama 2: una sola fila manual.
        else:
            fecha = _parse_fecha(request.form.get("fecha"))
            if fecha is None:
                errores.append("Fecha requerida (formato AAAA-MM-DD o DD/MM/AAAA).")
            else:
                row = {"fecha": fecha}
                for col in queries.FLUJO_COLS:
                    val = request.form.get(col)
                    if val not in (None, ""):
                        monto = _parse_monto(val)
                        if monto is None:
                            errores.append(f"Monto inválido en {col}: {val!r}")
                        else:
                            row[col] = monto
                if fecha is not None:
                    rows.append(row)

        if rows and not errores:
            try:
                resultado = queries.upsert_flujo_rows(rows, usuario)
                flash(
                    f"Flujo: {resultado['inserted']} insertadas, "
                    f"{resultado['updated']} actualizadas.",
                    "ok",
                )
                return redirect(url_for("informes.flujo_grafico"))
            except Exception as e:
                errores.append(f"Error guardando: {e}")

    return render_template(
        "informes/flujo_cargar.html",
        headers=_FLUJO_HEADERS,
        cols=queries.FLUJO_COLS,
        errores=errores,
        resultado=resultado,
        hoy=date.today().isoformat(),
    )


@informes_bp.route("/ventas/multianual")
@requiere_login
@requiere_permiso("informes.ver")
def ventas_multianual():
    """Matriz ventas mes × año — replica MODIFICA.PRG PROCEDURE VENTAS L144-217.

    Default 4 años para alinear con la captura legacy (2020-21-22-23).
    """
    anios = request.args.get("anios", default=4, type=int)
    data, error = _safe(lambda: queries.ventas_multianual(anios), {})
    return render_template(
        "informes/ventas_multianual.html",
        data=data, anios=anios, error=error,
    )


@informes_bp.route("/ventas")
@requiere_login
@requiere_permiso("informes.ver")
def ventas():
    # TMT 2026-05-19 v8 — dueña: al clickear "Ventas" del balance quiere ver
    # la pantalla TINT.BAT del dBase (ranking clientes del mes). Por default
    # ahora redirigimos al ranking del mes; el listado multi-mes vive en
    # ventas_multianual (link sigue disponible desde ahí).
    from datetime import date as _date
    hoy = _date.today()
    try:
        anio = int(request.args.get("anio") or hoy.year)
    except (TypeError, ValueError):
        anio = hoy.year
    try:
        mes = int(request.args.get("mes") or hoy.month)
    except (TypeError, ValueError):
        mes = hoy.month
    mes = max(1, min(mes, 12))
    data, error = _safe(
        lambda: queries.ventas_clientes_del_mes(anio=anio, mes=mes), {},
    )
    return render_template(
        "informes/ventas_mes.html",
        data=data, anio=anio, mes=mes, error=error,
    )


@informes_bp.route("/ventas/listado-mensual")
@requiere_login
@requiere_permiso("informes.ver")
def ventas_listado_mensual():
    """Listado de ventas agregadas por mes (últimos N meses). Vivía en
    /informes/ventas; el URL canónico ahora muestra el ranking de clientes
    del mes (pantalla TINT.BAT). Esta vista queda como sub-ruta."""
    meses = request.args.get("meses", default=12, type=int)
    filas, error = _safe(lambda: queries.ventas_mensuales(meses), [])
    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("mes", "Mes"), ("n_facturas", "# facturas"),
                ("kg_total", "Kg"), ("importe_total", "Importe"),
                ("abonado_total", "Abonado"),
            ],
            filename=f"ventas_{meses}m.csv",
        )
    return render_template("informes/ventas.html", filas=filas, meses=meses, error=error)


@informes_bp.route("/gastos")
@requiere_login
@requiere_permiso("informes.ver")
def gastos():
    filas, error = _safe(queries.gastos_mes_corriente, [])
    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"), ("documento", "Doc"),
                ("concepto", "Concepto"), ("proveedor", "Proveedor"),
                ("banco", "Banco"), ("importe", "Importe"),
            ],
            filename="gastos_mes.csv",
        )
    total = sum(float(r["importe"] or 0) for r in filas)
    # Matriz V1-V9 (xgast por NUM) + amortizaciones por rubro.
    # Layout 3x3: filas = personal/servicios/otros, cols = tej/tinto/admin.
    # Coincide con la convención del PRG INFORMES.PRG líneas 211-217:
    #   GTEJ = V1+V2+V3 + DTJ
    #   GTIN = V4+V5+V6 + DCC
    #   GGF  = V7+V8+V9 + DEPRCAR
    v, _e = _safe(queries.gastos_xgast_v1_a_v9_mes, {})
    a, _e = _safe(queries.amortizaciones_mensuales, {})
    def gv(k):
        return float((v or {}).get(k) or 0)
    def ga(k):
        return float((a or {}).get(k) or 0)
    matriz = {
        "personal": {
            "tej":   gv("v1"), "tin":   gv("v4"), "adm":   gv("v7"),
        },
        "servicios": {
            "tej":   gv("v2"), "tin":   gv("v5"), "adm":   gv("v8"),
        },
        "otros": {
            "tej":   gv("v3"), "tin":   gv("v6"), "adm":   gv("v9"),
        },
    }
    # Totales por columna (V1+V2+V3 etc.) y los GTEJ/GTIN/GGF con amort.
    col_v = {
        "tej": gv("v1") + gv("v2") + gv("v3"),
        "tin": gv("v4") + gv("v5") + gv("v6"),
        "adm": gv("v7") + gv("v8") + gv("v9"),
    }
    col_amort = {"tej": ga("dtj"), "tin": ga("dcc"), "adm": ga("deprcar")}
    col_total = {k: col_v[k] + col_amort[k] for k in col_v}
    # Totales por fila (personal/servicios/otros — sin amort, son sólo V1-V9).
    fil_total = {
        "personal":  matriz["personal"]["tej"]  + matriz["personal"]["tin"]  + matriz["personal"]["adm"],
        "servicios": matriz["servicios"]["tej"] + matriz["servicios"]["tin"] + matriz["servicios"]["adm"],
        "otros":     matriz["otros"]["tej"]     + matriz["otros"]["tin"]     + matriz["otros"]["adm"],
    }
    suma_v_total = sum(col_v.values())
    suma_amort_total = sum(col_amort.values())
    suma_grand = sum(col_total.values())
    # TMT 2026-05-19 v5 — pedido dueña: banner "Sin clasificar" con link
    # al wizard. xgast.num NULL → no aparece en V1..V9 → invisible al ojo.
    # Mostrar al pie cuánta plata hay en ese limbo.
    sin_num_resumen = {"n": 0, "total": 0.0, "n_conceptos_unicos": 0}
    try:
        from modules.gastos import queries as _gq
        sin_num_resumen = _gq.xgast_sin_num_resumen()
    except Exception:
        pass

    return render_template(
        "informes/gastos.html",
        filas=filas, total=total, error=error,
        matriz=matriz, col_v=col_v, col_amort=col_amort, col_total=col_total,
        fil_total=fil_total,
        suma_v_total=suma_v_total, suma_amort_total=suma_amort_total,
        suma_grand=suma_grand,
        sin_num_resumen=sin_num_resumen,
    )


@informes_bp.route("/gastos/detalle/<int:num>")
@requiere_login
@requiere_permiso("informes.ver")
def gastos_detalle(num):
    """Drill-down de una categoría V1..V12 — DETALGAST del PRG.

    Lista las filas de `scintela.xgast` para esa categoría (mes en curso)
    agrupadas por concepto (EEQ/CMB/EMAAP/etc).
    """
    # TMT 2026-05-15: decisión #3 — antes era `abort(404)` para num fuera de
    # rango. La dueña pidió un 400 explícito con el rango válido y el valor
    # recibido, para que se entienda qué pasó al tipear una URL inválida.
    if num < 1 or num > 12:
        abort(400, description=f"categoría debe estar entre 1 y 12, recibido {num}")
    data, error = _safe(lambda: queries.gastos_detalle_categoria(num), {})
    return render_template(
        "informes/gastos_detalle.html",
        data=data, num=num, error=error,
    )


@informes_bp.route("/retiros")
@requiere_login
@requiere_permiso("informes.ver")
def retiros():
    dias = request.args.get("dias", default=180, type=int)
    filas, error = _safe(lambda: queries.retiros_recientes(dias), [])
    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"), ("banco", "Banco"),
                ("de", "De"), ("concepto", "Concepto"),
                ("ret", "Monto"),
            ],
            filename=f"retiros_{dias}d.csv",
        )
    total = sum(float(r["ret"] or 0) for r in filas)
    total_anual, _ = _safe(queries.retiros_total_anual, 0.0)
    return render_template(
        "informes/retiros.html",
        filas=filas, dias=dias, total=total, total_anual=total_anual,
        error=error,
    )


@informes_bp.route("/activos")
@requiere_login
@requiere_permiso("informes.ver")
def activos():
    filas, error = _safe(queries.activos_lista, [])
    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"), ("concepto", "Concepto"),
                ("tipo", "Tipo"), ("proveedor", "Proveedor"),
                ("inicial", "Inicial"), ("amortizac", "Amort. acum."),
                ("amortimes", "Amort. mes"), ("valor", "Valor neto"),
                ("cuota", "Cuota"), ("vida_util", "Vida útil"),
                ("ult_mes_amortizado", "Últ. mes amort."),
            ],
            filename="activos_fijos.csv",
        )
    return render_template("informes/activos.html", filas=filas, error=error)


@informes_bp.route("/historia/multianual")
@requiere_login
@requiere_permiso("informes.ver")
def historia_multianual():
    """Vista cruzada mes × año — replica INFORMES.PRG L1336-1550 modo '1/2/3'.

    Muestra los últimos N meses (default 12) con las métricas principales
    (patrimonio, ventas U$, utilidad U$, kg vendidos, stock MP+PT, etc.)
    desplegadas por año (corriente + 2 anteriores) y con la variación %
    año contra año. Útil para detectar tendencias estacionales.
    """
    meses = request.args.get("meses", default=12, type=int)
    data, error = _safe(lambda: queries.historia_multianual(meses), {})
    return render_template(
        "informes/historia_multianual.html",
        data=data, meses=meses, error=error,
    )


@informes_bp.route("/historia")
@requiere_login
@requiere_permiso("informes.ver")
def historia():
    filas, error = _safe(queries.historia_lista, [])
    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Mes"),
                ("stock", "Stock"), ("kcom", "Kg compra"), ("ktej", "Kg tejido"),
                ("ktin", "Kg tinto"), ("ustock", "U stock"), ("uqui", "U químicos"),
                ("kvent", "Kg venta"), ("uvent", "U venta"), ("costo", "Costo"),
                ("ucom", "U compra"), ("utej", "U tejido"), ("utin", "U tinto"),
                ("gasto", "Gasto mes"), ("gstotal", "Gasto total"),
                ("banco", "Banco"), ("cart", "Cartera"), ("deuda", "Deuda"),
                ("retiro", "Retiro"), ("patrimonio", "Patrimonio"),
                ("anticipos", "Anticipos"), ("dolar", "Dólar"),
                ("maquinaria", "Maquinaria"), ("realty", "Inmueble"),
                ("usret", "USD retiro"), ("usuti", "USD utilidad"),
            ],
            filename="historia_mensual.csv",
        )
    return render_template("informes/historia.html", filas=filas, error=error)


@informes_bp.route("/iniciales")
@requiere_login
@requiere_permiso("informes.ver")
def iniciales():
    anio = request.args.get("anio", type=int)
    filas, error = _safe(lambda: queries.iniciales_lista(anio), [])
    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("yy", "Año"), ("mesnum", "#"), ("mesnom", "Mes"),
                ("hilado", "Hilado"), ("tejido", "Tejido"), ("terminado", "Terminado"),
                ("vq", "VQ"), ("um", "UM"), ("uk", "UK"), ("uf", "UF"), ("uq", "UQ"),
                ("pre", "Precio"), ("kprog", "Kg prog."), ("gprog", "Gasto prog."),
                ("numnot", "# notas"), ("dificil", "Dificultad"),
                ("pretej", "Precio tej."), ("pretin", "Precio tin."),
                ("preadm", "Precio adm."), ("pretot", "Precio tot."),
            ],
            filename=f"iniciales_{anio or 'todos'}.csv",
        )
    return render_template("informes/iniciales.html", filas=filas, anio=anio, error=error)


@informes_bp.route("/estado-cuenta", methods=["GET"])
@requiere_login
@requiere_permiso("informes.ver")
def estado_cuenta_landing():
    """Landing/lookup page para estado de cuenta de cliente.

    Muestra top deudores (los candidatos más probables a mirar) + un buscador
    por código o nombre. Si el usuario envía ?codigo=XYZ lo redirige al
    estado de cuenta. Si busca por nombre, lista los matches.
    """
    codigo = (request.args.get("codigo") or "").strip().upper()
    if codigo:
        return redirect(url_for("informes.estado_cuenta", codigo_cli=codigo))

    busqueda = (request.args.get("q") or "").strip()
    matches: list[dict] = []
    if busqueda:
        matches, _ = _safe(lambda: queries.buscar_clientes(busqueda), [])

    top, error = _safe(queries.cartera_por_cliente, [])
    # top 10 deudores como atajos
    top = top[:10] if top else []
    return render_template(
        "informes/estado_cuenta_landing.html",
        top=top, matches=matches, q=busqueda, error=error,
    )


@informes_bp.route("/estado-cuenta/<codigo_cli>")
@requiere_login
@requiere_permiso("informes.ver")
def estado_cuenta(codigo_cli):
    codigo_up = codigo_cli.upper()
    data, error = _safe(lambda: queries.estado_cuenta_cliente(codigo_up), {})
    if not data or not data.get("cliente"):
        abort(404)
    try:
        from modules.recientes import queries as rec
        cli = data.get("cliente") or {}
        rec.registrar(
            "cliente", codigo_up,
            etiqueta=f"{codigo_up} — {cli.get('nombre') or ''}",
        )
    except Exception:
        pass
    return render_template("informes/estado_cuenta.html", data=data, error=error)


# ---------------------------------------------------------------------------
# Gastos forzados — endpoints JSON. Migración localStorage → DB.
# Pedido dueña 2026-05-19 v8: cargás en Chrome, abrís en Safari, no
# aparecía nada. Ahora la fuente de verdad es scintela.gasto_forzado.
# El JS del flujo_grafico.html llama a estos endpoints (en lugar de
# tocar localStorage) — ver bloque `gfLoad/gfSave` del template.
# ---------------------------------------------------------------------------

def _parse_fecha_iso(s: str):
    """YYYY-MM-DD → date, o None si no parsea."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_importe_payload(raw) -> float | None:
    """Acepta float/int/str con '.' o ','. Devuelve None si no parsea."""
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


@informes_bp.route("/informes/flujo/gastos-forzados", methods=["GET"])
@requiere_login
@requiere_permiso("informes.ver")
def gastos_forzados_listar():
    try:
        items = queries.gastos_forzados_listar()
        return jsonify({"ok": True, "items": items})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@informes_bp.route("/informes/flujo/gastos-forzados", methods=["POST"])
@requiere_login
@requiere_permiso("informes.editar")
def gastos_forzados_crear():
    payload = request.get_json(silent=True) or {}
    fecha = _parse_fecha_iso(payload.get("fecha"))
    importe = _parse_importe_payload(payload.get("importe"))
    concepto = (payload.get("concepto") or "").strip()[:80]
    if not fecha or importe is None or importe <= 0:
        return jsonify({
            "ok": False,
            "error": "Datos inválidos: fecha (YYYY-MM-DD) y importe > 0 requeridos.",
        }), 400
    usuario = (g.user or {}).get("username", "web")
    try:
        item = queries.gasto_forzado_crear(
            fecha=fecha, importe=importe, concepto=concepto, usuario=usuario,
        )
        return jsonify({"ok": True, "item": item}), 201
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@informes_bp.route(
    "/informes/flujo/gastos-forzados/<int:id_gasto>", methods=["PUT", "PATCH"],
)
@requiere_login
@requiere_permiso("informes.editar")
def gastos_forzados_actualizar(id_gasto: int):
    payload = request.get_json(silent=True) or {}
    expected = payload.get("expected_version")
    if expected is None:
        return jsonify({"ok": False, "error": "expected_version requerido"}), 400
    try:
        expected_v = int(expected)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "expected_version inválido"}), 400
    fecha = _parse_fecha_iso(payload.get("fecha")) if "fecha" in payload else None
    importe = _parse_importe_payload(payload.get("importe")) if "importe" in payload else None
    concepto = (payload.get("concepto") or "").strip()[:80] if "concepto" in payload else None
    usuario = (g.user or {}).get("username", "web")
    try:
        r = queries.gasto_forzado_actualizar(
            id_gasto_forzado=id_gasto,
            expected_version=expected_v,
            fecha=fecha, importe=importe, concepto=concepto,
            usuario=usuario,
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    if not r.get("ok"):
        status = 409 if r.get("reason", "").startswith("version_conflict") else 404
        return jsonify(r), status
    return jsonify(r)


@informes_bp.route(
    "/informes/flujo/gastos-forzados/<int:id_gasto>", methods=["DELETE"],
)
@requiere_login
@requiere_permiso("informes.editar")
def gastos_forzados_eliminar(id_gasto: int):
    try:
        ok = queries.gasto_forzado_eliminar(id_gasto)
        if not ok:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@informes_bp.route(
    "/informes/flujo/gastos-forzados/importar", methods=["POST"],
)
@requiere_login
@requiere_permiso("informes.editar")
def gastos_forzados_importar():
    """One-time migration: el cliente envía el contenido de localStorage
    `flujo_gastos_forzados_v1` y los inserta en DB (dedup por
    fecha+importe+concepto)."""
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "items debe ser lista"}), 400
    usuario = (g.user or {}).get("username", "web")
    try:
        r = queries.gastos_forzados_importar_bulk(items, usuario=usuario)
        return jsonify({"ok": True, **r})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
