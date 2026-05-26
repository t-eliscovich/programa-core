"""Auditoría de facturas PC sin match en Asinfo.

Después de correr el matcher normal (numf_completo / sufijo / heurístico),
quedan algunas huérfanas (PC tiene la factura, Asinfo no la matchea). Este
módulo expone una función que, para cada huérfana, busca en Asinfo los
top-K candidatos más cercanos en una ventana fecha ±30 días, ordenados por
una distancia heurística que pondera cliente, kg y usd.

Sirve para responder "¿por qué falta esta?":
    - cliente y kg coinciden, pero la fecha está 5 días corrida → drift
      de carga, probablemente debería matchear.
    - cliente coincide pero los importes están muy lejos → es otra factura.
    - no hay ningún candidato → la factura realmente no existe en Asinfo,
      probablemente fue anulada (marcar 'N') o nunca llegó.

Uso desde Flask:
    from modules.facturas import audit_asinfo
    huerfanas = audit_asinfo.auditar_huerfanas()

TMT 2026-05-22.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import db
from modules.asinfo import service as asinfo_service

_LOG = logging.getLogger("programa_core.facturas.audit")


# Sólo auditamos facturas POST cutoff Asinfo limpio.
_ASINFO_CUTOFF = date(2025, 1, 1)


def _huerfanas_pc(limite: int = 500) -> list[dict]:
    """PC: facturas eligibles para audit Asinfo.

    Criterios:
        - stat IN ('Z','A','T','X','N')  (todos los stats vivos)
        - fecha >= 2025-01-01            (Asinfo limpio sólo post cutoff)
        - kg != 0                        (NC financieras kg=0 no se matchean)

    No filtramos por numf_completo IS NULL acá — el matcher decide qué es
    huérfana después de cruzar contra Asinfo. Pero limitamos a las que no
    tienen numf_completo cargado, que son las candidatas verdaderas.
    """
    return db.fetch_all(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha,
               f.codigo_cli, COALESCE(c.nombre, '') AS cliente,
               f.kg, f.importe, f.abono, f.saldo, f.stat
        FROM scintela.factura f
        LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
        WHERE f.fecha >= %s
          AND (f.kg IS NOT NULL AND f.kg <> 0)
          AND (f.stat IS NULL OR f.stat IN ('Z','A','T','X','N','',' '))
          AND (f.numf_completo IS NULL OR f.numf_completo = ''
               OR f.numf IS NULL OR f.numf = 0)
          -- TMT 2026-05-26: excluir explícitamente marcadas (#DUP, #SIN_ASINFO).
          AND (f.numf_completo IS NULL OR NOT (f.numf_completo LIKE '#%%'))
        ORDER BY f.fecha DESC, f.numf DESC
        LIMIT %s
        """,
        (_ASINFO_CUTOFF, limite),
    )


def _score_candidato(pc: dict, ai: dict) -> float:
    """Distancia heurística PC↔Asinfo. Menor = mejor candidato.

    Pondera:
        - cliente: 0 si match, +50 si no.
        - kg:      |Δkg| / max(|kg PC|, 1).
        - usd:     |Δusd| / max(|usd PC|, 1).
        - fecha:   |días| / 30.

    Devuelve una distancia normalizada, sin máximo (puede ser >100 si todo
    está mal). Los candidatos < 1.0 son sospechosos de ser MATCH real.
    """
    cli_pc = (pc.get("codigo_cli") or "").strip().upper()
    cli_ai = (ai.get("cliente_codigo") or "").strip().upper()
    score = 0.0
    if cli_pc != cli_ai:
        score += 50.0

    kg_pc = abs(float(pc.get("kg") or 0))
    kg_ai = abs(float(ai.get("kg") or 0))
    score += abs(kg_pc - kg_ai) / max(kg_pc, 1.0)

    usd_pc = abs(float(pc.get("importe") or 0))
    usd_ai = abs(float(ai.get("usd") or 0))
    score += abs(usd_pc - usd_ai) / max(usd_pc, 1.0)

    f_pc = pc.get("fecha")
    f_ai = ai.get("fecha")
    if f_pc and f_ai:
        try:
            if isinstance(f_ai, str):
                from datetime import datetime as _dt
                f_ai = _dt.fromisoformat(str(f_ai)[:10]).date()
            score += abs((f_pc - f_ai).days) / 30.0
        except (ValueError, TypeError):
            score += 1.0
    else:
        score += 1.0
    return round(score, 3)


def _signos_compatibles(pc: dict, ai: dict) -> bool:
    """Filtro grueso: PC kg>0 pide tipo positivo, PC kg<0 pide tipo negativo."""
    pc_kg = float(pc.get("kg") or 0)
    tipo = ai.get("tipo")
    POSITIVOS = ("FACTURA", "NTEN", "NC_FINANCIERA")
    NEGATIVOS = ("DEVOLUCION", "NCNT")
    if pc_kg > 0:
        return tipo in POSITIVOS
    elif pc_kg < 0:
        return tipo in NEGATIVOS
    return True  # kg=0 no filtra por signo


def auditar_huerfanas(top_k: int = 3, limite: int = 500) -> list[dict]:
    """Audit completo: para cada huérfana PC, devuelve top-K candidatos AI.

    Returns:
        Lista de dicts con:
            pc_factura       — fila de PC (id_factura, fecha, codigo_cli,
                               cliente, kg, importe, saldo, stat, numf,
                               numf_completo).
            candidatos       — lista de dicts ordenados por score asc:
                               { ai_numero, ai_tipo, ai_fecha,
                                 ai_cliente_codigo, ai_kg, ai_usd, score }
            mejor_score      — score del mejor candidato (None si lista vacía).
    """
    huerfanas = _huerfanas_pc(limite=limite)
    if not huerfanas:
        return []

    # Determinar el rango global de fechas para una sola llamada a Asinfo.
    fechas = [h["fecha"] for h in huerfanas if h.get("fecha")]
    if not fechas:
        return []
    mn = max(min(fechas) - timedelta(days=30), _ASINFO_CUTOFF)
    mx = max(fechas) + timedelta(days=30)
    asinfo_rows = asinfo_service.facturas_periodo(mn, mx)
    if not asinfo_rows:
        _LOG.warning("auditar_huerfanas: Asinfo devolvió 0 filas para %s..%s", mn, mx)
        return []

    # Index por cliente para acelerar (lookup por cliente_codigo).
    from collections import defaultdict
    por_cliente: dict[str, list[dict]] = defaultdict(list)
    for r in asinfo_rows:
        cli = (r.get("cliente_codigo") or "").strip().upper()
        if cli:
            por_cliente[cli].append(r)

    out: list[dict] = []
    for h in huerfanas:
        cli = (h.get("codigo_cli") or "").strip().upper()
        candidatos_pool: list[dict] = []
        # Universo 1: candidatos del mismo cliente (los mejor escorados).
        candidatos_pool.extend(por_cliente.get(cli, []))
        # Universo 2: si nada del mismo cliente, agregamos ventana fecha ±15d
        # de TODOS los clientes — quizás el codigo_cli está mal cargado en PC.
        if not candidatos_pool:
            f = h.get("fecha")
            if f:
                ventana_mn = f - timedelta(days=15)
                ventana_mx = f + timedelta(days=15)
                for r in asinfo_rows:
                    rf = r.get("fecha")
                    if not rf:
                        continue
                    try:
                        if isinstance(rf, str):
                            from datetime import datetime as _dt
                            rf_d = _dt.fromisoformat(str(rf)[:10]).date()
                        else:
                            rf_d = rf
                        if ventana_mn <= rf_d <= ventana_mx:
                            candidatos_pool.append(r)
                    except (ValueError, TypeError):
                        pass

        scored: list[tuple[float, dict]] = []
        for c in candidatos_pool:
            if not _signos_compatibles(h, c):
                continue
            s = _score_candidato(h, c)
            scored.append((s, c))
        scored.sort(key=lambda t: t[0])
        top = scored[:top_k]

        out.append({
            "pc_factura": h,
            "candidatos": [
                {
                    "ai_numero": c.get("numero"),
                    "ai_tipo": c.get("tipo"),
                    "ai_fecha": c.get("fecha"),
                    "ai_cliente_codigo": c.get("cliente_codigo"),
                    "ai_kg": c.get("kg"),
                    "ai_usd": c.get("usd"),
                    "score": score,
                }
                for score, c in top
            ],
            "mejor_score": top[0][0] if top else None,
        })

    # Orden: peor score primero (los que necesitan más atención).
    # Las que no tienen NINGÚN candidato (mejor_score=None) van al final.
    def _sort_key(item):
        s = item["mejor_score"]
        return (s is None, -(s or 0))
    out.sort(key=_sort_key)
    return out


def asociar(id_factura: int, numero_completo: str, usuario: str = "web") -> int:
    """Escribe numf_completo y numf (sufijo int) en una factura PC.

    Idempotente: si ya tiene numf_completo, lo sobreescribe.
    """
    if not numero_completo:
        return 0
    sufijo = numero_completo.split("-")[-1]
    try:
        numf_int = int(sufijo)
    except (ValueError, TypeError):
        numf_int = None

    # No tocamos numf si ya tiene un valor != 0 (puede que el usuario lo
    # haya tipeado correctamente y solo le falte el numf_completo).
    if numf_int is not None:
        res = db.execute(
            """
            UPDATE scintela.factura
               SET numf_completo = %s,
                   numf = CASE WHEN COALESCE(numf, 0) = 0 THEN %s ELSE numf END
             WHERE id_factura = %s
            """,
            (numero_completo, numf_int, id_factura),
        )
    else:
        res = db.execute(
            "UPDATE scintela.factura SET numf_completo = %s WHERE id_factura = %s",
            (numero_completo, id_factura),
        )
    _LOG.info("audit asociar id_factura=%s → %s (usuario=%s)", id_factura, numero_completo, usuario)
    return res


def asociar_batch(pares: list[tuple[int, str]], usuario: str = "web") -> dict:
    """Bulk: actualiza numf_completo y numf para muchos pares (id_factura, numero_completo).

    Lo hace en UNA sola conexión + transacción para no agotar el pool. Cada
    fila puede fallar individualmente; las que rompen quedan en `errores`.

    Returns:
        dict {actualizadas: int, errores: list[{id_factura, numero, error}]}
    """
    errores: list[dict] = []
    actualizadas = 0
    if not pares:
        return {"actualizadas": 0, "errores": []}

    with db.get_conn() as c:
        try:
            with c.cursor() as cur:
                for id_f, num in pares:
                    if not num:
                        continue
                    sufijo = num.split("-")[-1]
                    try:
                        numf_int: int | None = int(sufijo)
                    except (ValueError, TypeError):
                        numf_int = None
                    try:
                        if numf_int is not None:
                            cur.execute(
                                """
                                UPDATE scintela.factura
                                   SET numf_completo = %s,
                                       numf = CASE WHEN COALESCE(numf, 0) = 0
                                                   THEN %s ELSE numf END
                                 WHERE id_factura = %s
                                """,
                                (num, numf_int, id_f),
                            )
                        else:
                            cur.execute(
                                "UPDATE scintela.factura SET numf_completo = %s WHERE id_factura = %s",
                                (num, id_f),
                            )
                        actualizadas += cur.rowcount
                    except Exception as _e:
                        errores.append({
                            "id_factura": id_f,
                            "numero": num,
                            "error": f"{type(_e).__name__}: {_e}",
                        })
                        # Si un UPDATE falla en una tx, abortamos el resto.
                        # En PG, una excepción dentro de una tx la deja
                        # "aborted" y los próximos cursor.execute fallan.
                        # Mejor parar y commit lo de antes (NO, no se puede:
                        # la tx está abortada). Hay que rollback + reintentar.
                        c.rollback()
                        _LOG.warning("asociar_batch: %s — rolling back y abortando.", _e)
                        return {"actualizadas": 0, "errores": errores}
            c.commit()
            _LOG.info("asociar_batch: %s filas actualizadas (usuario=%s)", actualizadas, usuario)
        except Exception:
            c.rollback()
            raise
    return {"actualizadas": actualizadas, "errores": errores}
