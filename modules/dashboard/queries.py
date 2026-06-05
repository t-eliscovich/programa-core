"""Dashboard queries — cheap, cached numbers for the front page.

Important schema facts (verified against intela12042026.sql):
    - scintela.banco      has columns (no_banco, nombre)  —  NOT nombre_banco
    - scintela.factura    joins clients by codigo_cli, NOT id_cliente
    - scintela.cheque     joins clients by codigo_cli
    - scintela.compra     has NO saldo column — deudas son vía posdat
    - scintela.posdat     tiene banc (no banco); banc<>9 son pasivos
"""

import db
from filters import today_ec


def kpis_dueno() -> dict:
    """Números clave para el dueño: cartera, deudas (posdat), saldos bancos."""
    cartera = db.fetch_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS total
        FROM scintela.factura
        WHERE COALESCE(saldo, 0) > 0
          AND (stat IS NULL OR stat IN ('Z','A','',' '))
        """
    )
    # Deudas = pasivos en posdat (PRG: SUM ALL IMPORTE TO TOTP FOR BANC # 9).
    # Excluye anuladas (soft-delete, migración 0027).
    deudas = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total
        FROM scintela.posdat
        WHERE COALESCE(banc, 0) <> 9
          AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
    )
    # Bancos — sólo los que tienen saldo distinto de cero. Un banco inactivo
    # en el dashboard del Dueño es ruido; en el módulo /bancos se puede ver
    # la lista completa con `?todos=1`. Mismo criterio que `informe_balance()`.
    bancos_todos = db.fetch_all(
        """
        SELECT b.no_banco, b.nombre,
               COALESCE((
                 SELECT t.saldo
                 FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 1
               ), 0) AS saldo
        FROM scintela.banco b
        ORDER BY b.no_banco
        """
    )
    bancos = [r for r in bancos_todos if round(float(r.get("saldo") or 0), 2) != 0]
    bancos_ocultos = len(bancos_todos) - len(bancos)
    total_bancos = sum((r.get("saldo") or 0) for r in bancos)
    return {
        "cartera": cartera["total"] if cartera else 0,
        "deudas": deudas["total"] if deudas else 0,
        "bancos": bancos,
        "bancos_ocultos": bancos_ocultos,
        "total_bancos": total_bancos,
    }


def top_deudores(limite: int = 10) -> list[dict]:
    """Top clientes por saldo pendiente. Join por codigo_cli (no id_cliente)."""
    return db.fetch_all(
        """
        SELECT f.codigo_cli,
               COALESCE(c.nombre, '(sin nombre)') AS nombre,
               COALESCE(SUM(f.saldo), 0) AS total
        FROM scintela.factura f
        LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
        WHERE COALESCE(f.saldo, 0) > 0
          AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
        GROUP BY f.codigo_cli, c.nombre
        ORDER BY total DESC
        LIMIT %s
        """,
        (limite,),
    )


def flujo_30_dias() -> list[dict]:
    """Para el Dueño — flujo de los últimos 30 días, más viejo primero (gráfico)."""
    return db.fetch_all(
        """
        SELECT fecha, saldo, pichincha, inter, cheques, facturas
        FROM scintela.flujo
        WHERE fecha >= CURRENT_DATE - INTERVAL '30 days'
        ORDER BY fecha ASC
        """
    )


# --- Gerente ---------------------------------------------------------------

def cobranza_semana() -> dict:
    """Cobranza esperada esta semana (facturas vencen hoy..+7d)."""
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS total,
               COUNT(*)                AS n
        FROM scintela.factura
        WHERE COALESCE(saldo, 0) > 0
          AND vencimiento BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days'
          AND (stat IS NULL OR stat IN ('Z','A','',' '))
        """
    )
    return {"total": float((row or {}).get("total") or 0),
            "n": int((row or {}).get("n") or 0)}


def compras_pagar_semana() -> dict:
    """Posdatados a pagar esta semana (banc<>9, fechad hoy..+7d).

    Excluye anuladas (soft-delete, migración 0027).
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total,
               COUNT(*)                  AS n
        FROM scintela.posdat
        WHERE COALESCE(banc, 0) <> 9
          AND fechad BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days'
          AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
    )
    return {"total": float((row or {}).get("total") or 0),
            "n": int((row or {}).get("n") or 0)}


# --- Contabilidad -----------------------------------------------------------

def cheques_sin_depositar() -> dict:
    """Cheques en cartera (no depositados: stat Z, 1, 2, 3)."""
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total,
               COUNT(*)                  AS n
        FROM scintela.cheque
        WHERE stat IN ('Z','1','2','3')
        """
    )
    return {"total": float((row or {}).get("total") or 0),
            "n": int((row or {}).get("n") or 0)}


def cheques_rebotados_30d() -> dict:
    """Cheques rebotados en los últimos 30 días."""
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total,
               COUNT(*)                  AS n
        FROM scintela.cheque
        WHERE stat = 'R'
          AND fechaout >= CURRENT_DATE - INTERVAL '30 days'
        """
    )
    return {"total": float((row or {}).get("total") or 0),
            "n": int((row or {}).get("n") or 0)}


def facturas_vencidas() -> dict:
    """Facturas con saldo > 0 y vencimiento < hoy."""
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS total,
               COUNT(*)                AS n
        FROM scintela.factura
        WHERE COALESCE(saldo, 0) > 0
          AND vencimiento < CURRENT_DATE
          AND (stat IS NULL OR stat IN ('Z','A','',' '))
        """
    )
    return {"total": float((row or {}).get("total") or 0),
            "n": int((row or {}).get("n") or 0)}


# --- Ventas -----------------------------------------------------------------

def facturas_recientes(limite: int = 10) -> list[dict]:
    return db.fetch_all(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha,
               f.codigo_cli, COALESCE(c.nombre, '') AS cliente,
               f.importe, f.saldo, f.stat
        FROM scintela.factura f
        LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
        ORDER BY f.fecha DESC, f.id_factura DESC
        LIMIT %s
        """,
        (limite,),
    )


def cobros_semana() -> dict:
    """Cheques recibidos en los últimos 7 días (cobranza reciente)."""
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total,
               COUNT(*)                  AS n
        FROM scintela.cheque
        WHERE fecha >= CURRENT_DATE - INTERVAL '7 days'
        """
    )
    return {"total": float((row or {}).get("total") or 0),
            "n": int((row or {}).get("n") or 0)}


# --- Dueño: additional activity feed -----------------------------------------

def actividad_reciente(limite: int = 8) -> list[dict]:
    """Unified recent-activity feed: newest facturas + newest cheques.

    Returns rows with keys (tipo, fecha, titulo, detalle, importe, url_key)
    sorted newest-first. url_key is a tuple the template can use with url_for.
    """
    facturas = db.fetch_all(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.codigo_cli,
               COALESCE(cli.nombre, '(sin nombre)') AS cliente,
               f.importe
        FROM scintela.factura f
        LEFT JOIN scintela.cliente cli ON cli.codigo_cli = f.codigo_cli
        ORDER BY f.fecha DESC, f.id_factura DESC
        LIMIT %s
        """,
        (limite,),
    )
    cheques = db.fetch_all(
        """
        SELECT c.id_cheque, c.no_cheque, c.fecha, c.codigo_cli,
               COALESCE(cli.nombre, '(sin nombre)') AS cliente,
               c.importe, c.stat
        FROM scintela.cheque c
        LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
        ORDER BY c.fecha DESC, c.id_cheque DESC
        LIMIT %s
        """,
        (limite,),
    )
    items = []
    for f in facturas:
        items.append({
            "tipo": "factura",
            "fecha": f["fecha"],
            "titulo": f"Factura {f.get('numf_completo') or f.get('numf')}",
            "detalle": f.get("cliente"),
            "importe": f.get("importe"),
            "id": f.get("id_factura"),
        })
    for c in cheques:
        items.append({
            "tipo": "cheque",
            "fecha": c["fecha"],
            "titulo": f"Cheque {c.get('no_cheque')}",
            "detalle": c.get("cliente"),
            "importe": c.get("importe"),
            "id": c.get("id_cheque"),
            "stat": c.get("stat"),
        })
    items.sort(key=lambda r: r.get("fecha") or "", reverse=True)
    return items[:limite]


def saldo_mes_en_curso() -> dict:
    """Running totals del mes actual.

    Reemplaza — para el mes vivo — el snapshot de `scintela.historia`, que
    sólo se actualiza a fin de mes. El gerente mira esto todas las mañanas.

    Devuelve: facturado, cobrado_cheques, neto, n_facturas, n_cheques,
    mes_desde (date del 1° del mes).
    """
    f = db.fetch_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
          FROM scintela.factura
         WHERE fecha >= date_trunc('month', CURRENT_DATE)
           AND (stat IS NULL OR stat IN ('Z','A','T','P','',' '))
        """
    ) or {}
    c = db.fetch_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
          FROM scintela.cheque
         WHERE fecha >= date_trunc('month', CURRENT_DATE)
           AND stat IN ('Z','1','2','3','A','D','P')
        """
    ) or {}
    facturado = float(f.get("total") or 0)
    cobrado = float(c.get("total") or 0)
    return {
        "facturado":       facturado,
        "cobrado_cheques": cobrado,
        "neto":            facturado - cobrado,
        "n_facturas":      int(f.get("n") or 0),
        "n_cheques":       int(c.get("n") or 0),
        "mes_desde":       today_ec().replace(day=1),
    }


def evolucion_cartera(meses: int = 12) -> list[dict]:
    """Snapshot mensual de cartera viva últimos N meses + valor de hoy.

    Lee de `scintela.historia` (snapshot mensual generado por el cierre).
    El último punto del chart es el saldo VIVO de hoy (calculado on-the-fly
    desde `factura.saldo`) para no esperar al cierre del mes para ver
    la tendencia actual.

    Devuelve filas en orden cronológico ASC (viejo → nuevo).
    """
    meses = max(1, min(int(meses or 12), 60))
    raw = db.fetch_all(
        """
        SELECT date_trunc('month', fecha)::date AS mes,
               COALESCE(cart, 0)                AS cartera,
               COALESCE(deuda, 0)               AS deuda
        FROM scintela.historia
        WHERE fecha >= CURRENT_DATE - (%s || ' months')::interval
        ORDER BY fecha ASC
        """,
        (meses,),
    ) or []
    # Cast a tipos JSON-safe — Postgres devuelve Decimal y date, json.dumps muere.
    historicos = [
        {
            "mes":     r["mes"].isoformat() if r.get("mes") else None,
            "cartera": float(r.get("cartera") or 0),
            "deuda":   float(r.get("deuda") or 0),
        }
        for r in raw
    ]
    # Punto "HOY" — cartera + deuda viva calculadas on-the-fly
    hoy_cartera = db.fetch_one(
        """
        SELECT COALESCE(SUM(saldo), 0) AS total
        FROM scintela.factura
        WHERE COALESCE(saldo, 0) > 0
          AND (stat IS NULL OR stat IN ('Z','A','',' '))
        """
    ) or {"total": 0}
    hoy_deuda = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total
        FROM scintela.posdat
        WHERE COALESCE(banc, 0) <> 9
          AND (anulada IS NOT TRUE OR anulada IS NULL)
        """
    ) or {"total": 0}
    historicos.append({
        "mes":     today_ec().replace(day=1).isoformat(),
        "cartera": float(hoy_cartera.get("total") or 0),
        "deuda":   float(hoy_deuda.get("total") or 0),
    })
    return historicos


def resumen_semana() -> dict:
    """Snapshot rápido: facturas emitidas, cheques recibidos, cheques rebotados (últ. 7d)."""
    f = db.fetch_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
        FROM scintela.factura
        WHERE fecha >= CURRENT_DATE - INTERVAL '7 days'
        """
    ) or {}
    c = db.fetch_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
        FROM scintela.cheque
        WHERE fecha >= CURRENT_DATE - INTERVAL '7 days'
        """
    ) or {}
    r = db.fetch_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
        FROM scintela.cheque
        WHERE stat = 'R' AND fechaout >= CURRENT_DATE - INTERVAL '7 days'
        """
    ) or {}
    return {
        "facturas": {"n": int(f.get("n") or 0), "total": float(f.get("total") or 0)},
        "cheques":  {"n": int(c.get("n") or 0), "total": float(c.get("total") or 0)},
        "rebotes":  {"n": int(r.get("n") or 0), "total": float(r.get("total") or 0)},
    }
