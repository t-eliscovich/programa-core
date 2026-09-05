"""Cómo se le CUENTAN los números al cliente: estados en su idioma, el próximo
vencimiento, las listas agrupadas por mes.

Acá no se calcula plata —los importes y saldos vienen de
`informes.queries.estado_cuenta_cliente`, la misma función que la oficina—.
Lo que se decide acá es la FRASE: "al día", "vence en 12 días", "vencida",
"a su favor". Nielsen, heurística 2: hablar el idioma del usuario. Un
cliente no sabe qué es un STAT ni un ACUM.
"""
from __future__ import annotations

from datetime import date, datetime
from itertools import groupby

#: Cuántos días antes del vencimiento la factura pasa de "al día" a
#: "vence en N días". Dos semanas: es el aviso, no la alarma.
DIAS_DE_AVISO = 15

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _fecha(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and len(v) >= 10:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def numero(v) -> float:
    """Un importe como float; lo que no es número vale cero (un None de la
    base, un texto raro). No se avisa: es formateo, no un cálculo de plata."""
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def es_negativa(f: dict) -> bool:
    """Una devolución o una nota de crédito: plata a favor del cliente."""
    return numero(f.get("importe")) < 0


def estado_de_factura(f: dict, hoy: date) -> dict:
    """``{"clase": "ok"|"warn"|"bad"|"", "texto": str, "dias": int|None}``."""
    if es_negativa(f):
        return {"clase": "", "texto": "a su favor", "dias": None}
    vence = _fecha(f.get("vencimiento"))
    if not vence:
        return {"clase": "ok", "texto": "al día", "dias": None}
    dias = (vence - hoy).days
    if dias < 0:
        n = -dias
        return {"clase": "bad", "texto": f"vencida hace {n} día{'s' if n != 1 else ''}", "dias": dias}
    if dias == 0:
        return {"clase": "warn", "texto": "vence hoy", "dias": 0}
    if dias <= DIAS_DE_AVISO:
        return {"clase": "warn", "texto": f"vence en {dias} día{'s' if dias != 1 else ''}", "dias": dias}
    return {"clase": "ok", "texto": "al día", "dias": dias}


def con_estado(facturas: list[dict], hoy: date) -> list[dict]:
    return [{**f, "estado_cliente": estado_de_factura(f, hoy)} for f in facturas]


def proximo_vencimiento(facturas: list[dict], hoy: date) -> dict | None:
    """La factura con saldo que vence antes (y no está vencida), o None.

    Lo que el cliente hoy tiene que deducir de la tabla, arriba y en una
    frase: "vence el 04/11: factura 181251 por 10.741,46".
    """
    vivas = []
    for f in facturas:
        if es_negativa(f):
            continue
        vence = _fecha(f.get("vencimiento"))
        saldo = numero(f.get("saldo"))
        if vence and saldo > 0 and vence >= hoy:
            vivas.append((vence, f))
    if not vivas:
        return None
    vence, f = min(vivas, key=lambda x: (x[0], str(x[1].get("numf"))))
    return {"fecha": vence, "dias": (vence - hoy).days, "factura": f}


def vencidas(facturas: list[dict]) -> list[dict]:
    return [f for f in facturas if (f.get("estado_cliente") or {}).get("clase") == "bad"]


def mes_de(v) -> str:
    d = _fecha(v)
    if not d:
        return "Sin fecha"
    return f"{MESES[d.month - 1].capitalize()} {d.year}"


def por_mes(items: list[dict], campo: str, importe: str = "importe") -> list[dict]:
    """``[{"mes": "Septiembre 2026", "items": [...], "total": float}, ...]``,
    del mes más nuevo al más viejo, respetando el orden de `items` adentro.

    Se agrupa sobre la lista YA ordenada (del más nuevo al más viejo): el
    groupby corta cada vez que cambia el mes, así que un orden mezclado daría
    el mismo mes dos veces. El que llama ordena; acá se agrupa.
    """
    grupos = []
    for mes, fs in groupby(items, key=lambda x: mes_de(x.get(campo))):
        fs = list(fs)
        total = 0.0
        for f in fs:
            total += numero(f.get(importe))
        grupos.append({"mes": mes, "items": fs, "total": total})
    return grupos


def ordenar_por_fecha(items: list[dict], campo: str, id_campo: str = "") -> list[dict]:
    """Del más nuevo al más viejo; a igual fecha, el id más alto primero."""
    def _k(x):
        d = _fecha(x.get(campo)) or date.min
        return (d, x.get(id_campo) or 0) if id_campo else d
    return sorted(items, key=_k, reverse=True)


def iniciales(nombre: str) -> str:
    partes = [p for p in (nombre or "").split() if p]
    if not partes:
        return "?"
    return "".join(p[0] for p in partes[:2]).upper()


def nombre_lindo(nombre: str) -> str:
    return " ".join(p.capitalize() for p in (nombre or "").split())
