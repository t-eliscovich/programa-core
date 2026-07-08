"""Stock inicial mensual (scintela.stock_inicial_mes).

Asinfo es LIVE-ONLY (SQL Server vía Metabase): no guarda histórico de saldos
de bodega por fecha. Para tener una línea base de "stock inicial" por mes hay
que TOMAR UNA FOTO del inventario live de Asinfo y persistirla en el Postgres
de PC. Este módulo hace exactamente eso:

    capturar(anio, mes, usuario)  — lee el inventario live de Asinfo
        (asinfo.service.inventario_por_etapa) y hace un upsert idempotente de
        las 5 etapas para (anio, mes). Fail-soft: si Asinfo no está disponible
        NO escribe nada y devuelve {"aplicado": False, "razon": "..."}.

    leer(anio, mes)               — devuelve {etapa: kg} de la foto guardada
        para ese mes (o {} si no hay).

    meses_capturados(limite)      — resumen por mes de las fotos guardadas,
        para la tabla de la vista.

Convención de etapas (idéntica a las claves de inventario_por_etapa()):
    'hilo' | 'tela_cruda' | 'terminada' | 'en_proceso_tc' | 'en_proceso_pt'
"""
from __future__ import annotations

import db

ETAPAS = ["hilo", "tela_cruda", "terminada", "en_proceso_tc", "en_proceso_pt"]

ETAPA_LABEL = {
    "hilo": "Hilo (bodega 51)",
    "tela_cruda": "Tela Cruda (bodega 52)",
    "terminada": "Terminada / PT (bodega 53)",
    "en_proceso_tc": "En proceso (Hilo → Tela Cruda)",
    "en_proceso_pt": "En proceso (Tela Cruda → Terminada)",
}


def capturar(anio: int, mes: int, usuario: str = "web") -> dict:
    """Toma la foto del inventario live de Asinfo y la persiste para (anio, mes).

    Idempotente vía UNIQUE(anio, mes, etapa) — re-capturar el mismo mes
    sobreescribe los kg con la foto actual (ON CONFLICT DO UPDATE).

    Fail-soft: si Asinfo no está disponible (bridge caído / sin config / las
    queries no devolvieron nada), NO escribe nada y devuelve aplicado=False.

    Devuelve dict:
        aplicado  — bool
        anio, mes — ints
        kg        — {etapa: kg} escrito (solo si aplicado)
        total     — suma de las 5 etapas
        razon     — texto explicativo (siempre)
    """
    anio = int(anio)
    mes = int(mes)
    if not (1 <= mes <= 12):
        return {"aplicado": False, "anio": anio, "mes": mes,
                "razon": "Mes fuera de rango (1-12)."}

    # Import local para no acoplar el arranque del módulo al bridge Asinfo.
    from modules.asinfo import service as asinfo_service

    try:
        inv = asinfo_service.inventario_por_etapa()
    except Exception as e:  # noqa: BLE001 — fail-soft total
        return {"aplicado": False, "anio": anio, "mes": mes,
                "razon": f"Asinfo no disponible ({e})."}

    if not inv or not inv.get("disponible"):
        return {"aplicado": False, "anio": anio, "mes": mes,
                "razon": "Asinfo no disponible — no se guardó nada."}

    kg = {etapa: float(inv.get(etapa) or 0) for etapa in ETAPAS}

    with db.tx() as conn:
        # Lock por período para evitar dos capturas concurrentes del mismo mes.
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"stock_inicial_mes:{anio:04d}-{mes:02d}",),
            conn=conn,
        )
        for etapa in ETAPAS:
            db.execute(
                """
                INSERT INTO scintela.stock_inicial_mes
                    (anio, mes, etapa, kg, usuario)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (anio, mes, etapa) DO UPDATE
                    SET kg = EXCLUDED.kg,
                        capturado_en = now(),
                        usuario = EXCLUDED.usuario
                """,
                (anio, mes, etapa, round(kg[etapa], 2), (usuario or "web")[:50]),
                conn=conn,
            )

    total = round(sum(kg.values()), 2)
    return {
        "aplicado": True,
        "anio": anio,
        "mes": mes,
        "kg": {e: round(v, 2) for e, v in kg.items()},
        "total": total,
        "razon": f"Foto de stock inicial {mes:02d}/{anio} guardada (total {total:,.0f} kg).",
    }


def leer(anio: int, mes: int) -> dict:
    """{etapa: kg} de la foto guardada para (anio, mes). {} si no hay."""
    rows = db.fetch_all(
        """
        SELECT etapa, kg
          FROM scintela.stock_inicial_mes
         WHERE anio = %s AND mes = %s
        """,
        (int(anio), int(mes)),
    ) or []
    return {str(r["etapa"]): float(r["kg"] or 0) for r in rows}


def meses_capturados(limite: int = 24) -> list[dict]:
    """Resumen por mes de las fotos guardadas (más recientes primero).

    Cada fila: anio, mes, total_kg, y las 5 etapas como columnas, más
    capturado_en (el más reciente del mes).
    """
    rows = db.fetch_all(
        """
        SELECT anio, mes,
               SUM(kg)                                       AS total_kg,
               MAX(capturado_en)                             AS capturado_en,
               SUM(kg) FILTER (WHERE etapa = 'hilo')          AS hilo,
               SUM(kg) FILTER (WHERE etapa = 'tela_cruda')    AS tela_cruda,
               SUM(kg) FILTER (WHERE etapa = 'terminada')     AS terminada,
               SUM(kg) FILTER (WHERE etapa = 'en_proceso_tc') AS en_proceso_tc,
               SUM(kg) FILTER (WHERE etapa = 'en_proceso_pt') AS en_proceso_pt
          FROM scintela.stock_inicial_mes
         GROUP BY anio, mes
         ORDER BY anio DESC, mes DESC
         LIMIT %s
        """,
        (int(limite),),
    ) or []
    out = []
    for r in rows:
        out.append({
            "anio": int(r["anio"]),
            "mes": int(r["mes"]),
            "total_kg": float(r["total_kg"] or 0),
            "capturado_en": r["capturado_en"],
            "hilo": float(r["hilo"] or 0),
            "tela_cruda": float(r["tela_cruda"] or 0),
            "terminada": float(r["terminada"] or 0),
            "en_proceso_tc": float(r["en_proceso_tc"] or 0),
            "en_proceso_pt": float(r["en_proceso_pt"] or 0),
        })
    return out
