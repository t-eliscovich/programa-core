"""Queries del calendario de cobranzas.

Idea: agrupar por día lo que hay que cobrar en los próximos N días.
Dos fuentes:
  - `scintela.cheque` con `fechad` en el rango y `stat='Z'` (en cartera, no
    depositado) — son los cheques posfechados que llegan a su fecha de
    depósito.
  - `scintela.factura` con `vencimiento` en el rango y `saldo > 0` —
    facturas que vencen y todavía están sin cobrar.

El orden por dentro de cada día: cheques primero (más certeza de cobro),
después facturas (depende del cliente).
"""
from __future__ import annotations

from datetime import date, timedelta

import db
from filters import today_ec


def cheques_proximos(dias_atras: int = 7, dias_adelante: int = 30) -> list[dict]:
    """Cheques posfechados en cartera que vencen en el rango."""
    desde = today_ec() - timedelta(days=int(dias_atras))
    hasta = today_ec() + timedelta(days=int(dias_adelante))
    return db.fetch_all(
        """
        SELECT ch.id_cheque,
               ch.no_cheque,
               ch.fecha,
               ch.fechad,
               -- TMT 2026-05-17: fechad_original NULL = sin postergar.
               ch.fechad_original,
               ch.fecha_postergacion,
               ch.importe,
               ch.codigo_cli,
               ch.banco,
               ch.no_banco,
               COALESCE(c.nombre, '(sin nombre)') AS cliente,
               COALESCE(c.telefono, '')           AS telefono,
               COALESCE(c.correo, '')             AS correo,
               COALESCE(c.stop, 'N')              AS stop
        FROM scintela.cheque ch
        LEFT JOIN scintela.cliente c ON c.codigo_cli = ch.codigo_cli
        WHERE ch.stat = 'Z'
          AND ch.fechad BETWEEN %s AND %s
        ORDER BY ch.fechad ASC, ch.importe DESC
        """,
        (desde, hasta),
    ) or []


def facturas_proximas(dias_atras: int = 7, dias_adelante: int = 30) -> list[dict]:
    """Facturas vivas con vencimiento en el rango."""
    desde = today_ec() - timedelta(days=int(dias_atras))
    hasta = today_ec() + timedelta(days=int(dias_adelante))
    return db.fetch_all(
        """
        SELECT f.id_factura,
               f.numf,
               f.numf_completo,
               f.fecha,
               f.vencimiento,
               f.importe,
               f.saldo,
               f.codigo_cli,
               COALESCE(c.nombre, '(sin nombre)') AS cliente,
               COALESCE(c.telefono, '')           AS telefono,
               COALESCE(c.correo, '')             AS correo,
               COALESCE(c.stop, 'N')              AS stop
        FROM scintela.factura f
        LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
        WHERE COALESCE(f.saldo, 0) > 0
          AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
          AND f.vencimiento BETWEEN %s AND %s
        ORDER BY f.vencimiento ASC, f.saldo DESC
        """,
        (desde, hasta),
    ) or []


def agenda_dias(dias_atras: int = 7, dias_adelante: int = 30) -> list[dict]:
    """Agrupado por día (ASC), cada día con sus cheques + facturas + total."""
    cheques = cheques_proximos(dias_atras, dias_adelante)
    facturas = facturas_proximas(dias_atras, dias_adelante)

    por_dia: dict = {}
    for ch in cheques:
        d = ch.get("fechad")
        if not d:
            continue
        por_dia.setdefault(d, {"fecha": d, "cheques": [], "facturas": []})
        por_dia[d]["cheques"].append(ch)
    for fa in facturas:
        d = fa.get("vencimiento")
        if not d:
            continue
        por_dia.setdefault(d, {"fecha": d, "cheques": [], "facturas": []})
        por_dia[d]["facturas"].append(fa)

    # Calcular totales y orden cronológico
    out = []
    for d in sorted(por_dia.keys()):
        bloque = por_dia[d]
        total_ch = sum(float(c.get("importe") or 0) for c in bloque["cheques"])
        total_fa = sum(float(f.get("saldo")  or 0) for f in bloque["facturas"])
        bloque["total_cheques"]  = total_ch
        bloque["total_facturas"] = total_fa
        bloque["total_dia"]      = total_ch + total_fa
        bloque["es_pasado"]      = d < today_ec()
        bloque["es_hoy"]         = d == today_ec()
        bloque["dias_relativo"]  = (d - today_ec()).days
        out.append(bloque)
    return out


def cobros_recientes(dias: int = 7) -> list[dict]:
    """Cobros recibidos en los últimos N días (scintela.cobro).

    Trae el nombre del cliente vía LEFT JOIN. Ordenado fecha DESC.
    """
    desde = today_ec() - timedelta(days=int(dias))
    return db.fetch_all(
        """
        SELECT co.id_cobro,
               co.fecha,
               co.codigo_cli,
               co.valor,
               co.banco,
               co.tipo_doc,
               co.no_docpago,
               co.id_factura,
               f2.numf                AS factura_numf,
               co.abono_total,
               COALESCE(c.nombre, '') AS cliente
        FROM scintela.cobro co
        LEFT JOIN scintela.cliente c ON c.codigo_cli = co.codigo_cli
        LEFT JOIN scintela.factura f2 ON f2.id_factura = co.id_factura
        WHERE co.fecha >= %s
        ORDER BY co.fecha DESC, co.id_cobro DESC
        """,
        (desde,),
    ) or []


def cobros_agenda(dias: int = 7) -> list[dict]:
    """Mismos cobros, agrupados por día con totales y flag es_hoy."""
    cobros = cobros_recientes(dias)
    por_dia: dict = {}
    for co in cobros:
        d = co.get("fecha")
        if not d:
            continue
        por_dia.setdefault(d, {"fecha": d, "cobros": [], "total": 0.0})
        por_dia[d]["cobros"].append(co)
        por_dia[d]["total"] += float(co.get("valor") or 0)
    out = []
    for d in sorted(por_dia.keys(), reverse=True):
        bloque = por_dia[d]
        bloque["es_hoy"] = d == today_ec()
        out.append(bloque)
    return out


def cobros_totales(dias: int = 7) -> dict:
    """KPIs del header: total general, cheques vs otros, clientes únicos."""
    cobros = cobros_recientes(dias)
    cheq_keys = {"CHE", "CHEQ", "CHEQUE"}
    total_general = sum(float(c.get("valor") or 0) for c in cobros)
    total_cheques = sum(
        float(c.get("valor") or 0)
        for c in cobros
        if (c.get("tipo_doc") or "").upper() in cheq_keys
        or "CHEQ" in (c.get("tipo_doc") or "").upper()
    )
    n_cheques = sum(
        1 for c in cobros
        if (c.get("tipo_doc") or "").upper() in cheq_keys
        or "CHEQ" in (c.get("tipo_doc") or "").upper()
    )
    return {
        "n_cobros":      len(cobros),
        "total_general": total_general,
        "total_cheques": total_cheques,
        "total_otros":   total_general - total_cheques,
        "n_cheques":     n_cheques,
        "n_clientes":    len({c.get("codigo_cli") for c in cobros if c.get("codigo_cli")}),
    }


def cobros_matriz_3_semanas(fecha_hasta: date | None = None) -> list[dict]:
    """Matriz de cobros últimas 3 semanas — replica MENU.PRG:1493-1522 (EFECT).

    TMT 2026-05-18 — REWRITE para fidelidad 1:1 con el PRG legacy.

    Procedure EFECT (MENU.PRG L1493-1522), lógica exacta:

        F1 = DATE() - 14         (luego rolled-back a lunes via DOW > 2)
        F2 = F1 + 7
        F3 = F1 + 14

        USE CHEQUES INDE CF
        SET FILT TO FECHAD - FECHING < 3
        SUM IMPORTE TO I1 FOR FECHAD>=F1 AND FECHAD<F2
        SUM IMPORTE TO I2 FOR FECHAD>=F2 AND FECHAD<F3
        SUM IMPORTE TO I3 FOR FECHAD>=F3

        USE F:\\STAND\\XCHEQUES        (cheques de la empresa / extra)
        SET FILT TO FECHAD - FECHING < 3
        (mismas sumas, → II1, II2, II3)

        I1 = I1 + II1, etc.
        DIAS = IIF(DOW(hoy)>1 AND DOW(hoy)<6, DOW(hoy)-1, 5)
        PROM = I1/5, I2/5, I3/DIAS

    En Programa Core:
      - `scintela.cheque`  ≈ CHEQUES (cheques recibidos de clientes)
      - `scintela.cheque.fechad`        ≈ FECHAD legacy
      - `scintela.cheque.fecha_recibido` ≈ FECHING legacy
      - El filtro `FECHAD - FECHING < 3` selecciona cheques que se
        depositan dentro de los 2 días de haberlos recibido
        — i.e., cobros casi al contado (no posdatados a largo plazo).
      - XCHEQUES legacy no existe como tabla separada en PC; los
        movimientos bancarios directos (depósitos cash, transferencias)
        viven en `transacciones_bancarias` con documento DE/AC.
        Los incluimos sin filtro de delay porque ya son cobros
        efectivamente acreditados.
    """
    hasta = fecha_hasta or today_ec()

    # Encontrar el lunes de la semana de `hasta`. Python weekday(): lun=0…dom=6.
    lunes_actual = hasta - timedelta(days=hasta.weekday())
    # Para que las 3 semanas sean ACTUAL + 2 ANTERIORES (paridad dBase L1494
    # "F1=DATE()-14"), arrancamos en lunes_actual - 14 días.
    semana_lunes = [
        lunes_actual - timedelta(days=14),
        lunes_actual - timedelta(days=7),
        lunes_actual,
    ]

    # 3 semanas × 7 días — fetch en una sola query agrupada por día.
    desde = semana_lunes[0]
    hasta_q = lunes_actual + timedelta(days=6)  # domingo de la semana actual

    rows = db.fetch_all(
        """
        WITH cobros AS (
            -- (1) Cheques cobrados al contado / casi al contado.
            -- PRG: SET FILT TO FECHAD - FECHING < 3
            -- + stat in (B,A,1,2,3): el cheque fue al banco (cobrado o intentado).
            SELECT ch.fechad AS fecha,
                   COALESCE(ch.importe, 0) AS importe
              FROM scintela.cheque ch
             WHERE ch.fechad BETWEEN %(desde)s AND %(hasta)s
               AND ch.fecha_recibido IS NOT NULL
               AND (ch.fechad - ch.fecha_recibido) < 3
               AND COALESCE(ch.stat, '') IN ('B', 'A', '1', '2', '3')

            UNION ALL

            -- (2) Equivalente PC de XCHEQUES legacy: entradas bancarias
            -- directas (depósito cash DE, acreditación AC) — cobros que
            -- no pasan por cheque cliente. Estos ya son efectivos por
            -- definición (entrada al banco confirmada).
            SELECT t.fecha AS fecha,
                   ABS(COALESCE(t.importe, 0)) AS importe
              FROM scintela.transacciones_bancarias t
             WHERE t.fecha BETWEEN %(desde)s AND %(hasta)s
               AND COALESCE(t.documento, '') IN ('DE', 'AC')
        )
        SELECT fecha, COALESCE(SUM(importe), 0) AS total
          FROM cobros
         GROUP BY fecha
         ORDER BY fecha
        """,
        {"desde": desde, "hasta": hasta_q},
    ) or []

    # Map fecha → total
    por_fecha: dict = {r["fecha"]: float(r["total"] or 0) for r in rows}

    hoy = today_ec()
    dias_keys = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado"]
    out: list[dict] = []
    for i, lunes in enumerate(semana_lunes):
        semana = {"semana": f"{lunes.isoformat()}",
                  "fecha_lunes": lunes,
                  "fecha_sabado": lunes + timedelta(days=5)}
        total = 0.0
        # Para el promedio, usamos sólo días hábiles transcurridos
        # (lun..sáb donde fecha <= hoy). Replica L1517 dBase:
        # DIAS=IIF(DOW(hoy)>1 AND DOW(hoy)<6, DOW(hoy)-1, 5).
        # Default 5 días (lun-vie); si es sábado/domingo, todos completos.
        n_dias_habiles_pasados = 0
        for d_idx, key in enumerate(dias_keys):
            d = lunes + timedelta(days=d_idx)
            v = por_fecha.get(d, 0.0)
            semana[key] = v
            total += v
            # cuenta como hábil si <=hoy y es lun-vie (idx 0..4)
            if d <= hoy and d_idx < 5:
                n_dias_habiles_pasados += 1
        semana["total_semana"]   = total
        # Si la semana ya pasó completa → /5. Si está en curso → /dias_habiles_pasados.
        if i < 2 or hoy.weekday() >= 5:  # semanas anteriores o ya es fin de sem
            divisor = 5
        else:
            divisor = max(1, n_dias_habiles_pasados)
        semana["prom_dia_habil"] = total / divisor if divisor else 0.0
        semana["dias_para_prom"] = divisor
        out.append(semana)
    return out


def totales_periodo(dias_atras: int = 7, dias_adelante: int = 30) -> dict:
    """Resumen de toda la ventana — KPIs para el header."""
    cheques = cheques_proximos(dias_atras, dias_adelante)
    facturas = facturas_proximas(dias_atras, dias_adelante)
    total_ch = sum(float(c.get("importe") or 0) for c in cheques)
    total_fa = sum(float(f.get("saldo")   or 0) for f in facturas)

    # Subset: solo lo que está vencido o vence hoy
    hoy = today_ec()
    vencidos_ch = sum(float(c.get("importe") or 0)
                      for c in cheques if c.get("fechad") and c["fechad"] <= hoy)
    vencidos_fa = sum(float(f.get("saldo") or 0)
                      for f in facturas if f.get("vencimiento") and f["vencimiento"] <= hoy)

    return {
        "n_cheques":        len(cheques),
        "n_facturas":       len(facturas),
        "total_cheques":    total_ch,
        "total_facturas":   total_fa,
        "total_general":    total_ch + total_fa,
        "vencidos_total":   vencidos_ch + vencidos_fa,
        "n_clientes":       len({c.get("codigo_cli") for c in cheques}
                              | {f.get("codigo_cli") for f in facturas}),
    }
