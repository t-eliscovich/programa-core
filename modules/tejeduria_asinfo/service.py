"""Match producción tejeduría (Asinfo) ↔ compras tipo K (Programa Core).

Reemplaza la carga MANUAL del dBase. Lado Asinfo:
`modules.asinfo.service.produccion_tejeduria_mes` (orden_fabricacion bodega 52).
Lado Programa Core: `scintela.compra` tipo K (lo que hoy se carga a mano).

Match:
  · FINO (de acá para adelante): la compra creada desde la tab estampa el OFT
    en `concepto` ('OFT-000038848') → cada OF sabe si ya tiene su compra.
  · APROX (lo viejo, sin OFT): se concilia por TEJEDOR y MES. Las fechas NO
    calzan exacto (la carga manual va con lag y fechas redondas), así que el
    match viejo es "masomenos" por tejedor, nunca por día.

Todo fail-soft: si Asinfo cae, `disponible=False` y la tab muestra un aviso.
"""
import re

import db
from modules.asinfo import service as asinfo_service

_OFT_RE = re.compile(r"OFT-\d+", re.IGNORECASE)


def _compras_k_por_prov(anio: int, mes: int) -> dict:
    """{codigo_prov: {kg, importe, n}} de scintela.compra tipo K del mes
    (kg>0, no anuladas)."""
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(TRIM(COALESCE(codigo_prov, ''))) AS cod,
                   COALESCE(SUM(kg), 0)      AS kg,
                   COALESCE(SUM(importe), 0) AS importe,
                   COUNT(*)                  AS n
              FROM scintela.compra
             WHERE UPPER(TRIM(COALESCE(tipo, ''))) = 'K'
               AND COALESCE(kg, 0) > 0
               AND COALESCE(stat, '') <> 'Y'
               AND EXTRACT(YEAR  FROM fecha) = %s
               AND EXTRACT(MONTH FROM fecha) = %s
             GROUP BY UPPER(TRIM(COALESCE(codigo_prov, '')))
            """,
            (int(anio), int(mes)),
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    return {
        r["cod"]: {
            "kg": float(r["kg"] or 0),
            "importe": float(r["importe"] or 0),
            "n": int(r["n"] or 0),
        }
        for r in rows
    }


def _ofts_estampadas() -> set:
    """OFT que ya figuran en el concepto de alguna compra tipo K (match fino:
    las cargadas desde esta tab)."""
    try:
        rows = db.fetch_all(
            """
            SELECT concepto
              FROM scintela.compra
             WHERE UPPER(TRIM(COALESCE(tipo, ''))) = 'K'
               AND COALESCE(stat, '') <> 'Y'
               AND concepto ILIKE '%%OFT-%%'
            """,
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return set()
    out: set = set()
    for r in rows:
        for m in _OFT_RE.findall(r.get("concepto") or ""):
            out.add(m.upper())
    return out


def _key(of: dict) -> str:
    """Clave estable del tejedor (codigo_prov, o '?'+label si es desconocido)."""
    return of["cod"] or ("?" + of["label"])


def resumen_mes(anio: int, mes: int) -> dict:
    """Resumen de la tab: producción Asinfo + match contra compras K.

    Devuelve:
        disponible, anio, mes, total_kg,
        tejedores: [{key, cod, label, es_intela, ofs, kg,
                     compra_kg, compra_importe, compra_n, falta_kg}]  (columnas + match)
        por_dia:   [{dia, kg:{key: kg}, total}]  (resumen diario, más nuevo arriba)
        pendientes:[of...] tercerizadas SIN OFT estampado (cargables)
    """
    prod = asinfo_service.produccion_tejeduria_mes(anio, mes)
    disponible = bool(prod.get("disponible"))
    ofs = prod.get("ofs", [])
    compras = _compras_k_por_prov(anio, mes) if disponible else {}
    estampadas = _ofts_estampadas() if disponible else set()

    # tejedores = columnas + match, ordenados por kg desc
    tej: dict = {}
    for of in ofs:
        k = _key(of)
        t = tej.setdefault(k, {
            "key": k, "cod": of["cod"], "label": of["label"],
            "es_intela": of["es_intela"], "ofs": 0, "kg": 0.0,
        })
        t["ofs"] += 1
        t["kg"] += of["kg"]
    tejedores = sorted(tej.values(), key=lambda x: -x["kg"])
    for t in tejedores:
        t["kg"] = round(t["kg"], 2)
        comp = compras.get(t["cod"], {}) if t["cod"] else {}
        t["compra_kg"] = round(comp.get("kg", 0.0), 2)
        t["compra_importe"] = round(comp.get("importe", 0.0), 2)
        t["compra_n"] = comp.get("n", 0)
        t["falta_kg"] = round(t["kg"] - comp.get("kg", 0.0), 2)

    # resumen diario (pivote por tejedor)
    dias: dict = {}
    for of in ofs:
        d = dias.setdefault(of["dia"], {"dia": of["dia"], "kg": {}, "total": 0.0})
        k = _key(of)
        d["kg"][k] = round(d["kg"].get(k, 0.0) + of["kg"], 2)
        d["total"] = round(d["total"] + of["kg"], 2)
    por_dia = sorted(dias.values(), key=lambda x: x["dia"], reverse=True)

    # pendientes de cargar = OFs tercerizadas sin OFT estampado en una compra
    pendientes = [
        of for of in ofs
        if not of["es_intela"] and of["numero"].upper() not in estampadas
    ]

    return {
        "disponible": disponible,
        "anio": prod.get("anio", anio),
        "mes": prod.get("mes", mes),
        "total_kg": prod.get("total_kg", 0.0),
        "tejedores": tejedores,
        "por_dia": por_dia,
        "pendientes": pendientes,
    }
