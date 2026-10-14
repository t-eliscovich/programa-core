"""Las importaciones que ofrece el cuadro celeste de /dolares (y /compras/nueva).

Sale de `views.api_importaciones_abiertas` para poder usarlo también desde el
alta del anticipo, que tiene que saber si la importación YA LLEGÓ.

🚨 Tamara 14/10/2026 (traza 14:09, −74.601): Andrés cargó $ 71.021,05 como
"ANTICIPO AC 49/26" cuando quería la AI 49. La AC 49 había llegado el 09/10 y
ya tenía su compra: un anticipo a una importación recibida no queda como
anticipo (el sintético de la traza lo tapa y `autobap` lo pasa a compra), así
que la plata salió del banco y la utilidad bajó, sin un solo cartel.
*"Tiene que haber un cartel más informativo — ojo, esta compra ya llegó"*.

Lo legítimo sigue pasando: el flete, el CAE y el saldo llegan hasta meses
después de la recepción. Por eso no se prohíbe: se avisa y se pide confirmar.
"""
from __future__ import annotations

import re

from . import service


def _fila(r: dict) -> dict:
    comp = r.get("compra") or {}
    return {
        "im_numero": r.get("im_numero"),
        "codigo": r.get("codigo"),
        "prov": (r.get("prov") or "").strip().upper(),
        "numero": r.get("numero"),
        # Tamara 2026-09-21: la letra pegada al número ("AC 83A") viaja al
        # concepto del anticipo, para que se llame igual que en la Nota.
        "sufijo": r.get("sufijo"),
        "nota": r.get("nota"),
        "fecha": str(r.get("fecha") or ""),
        # TMT 2026-07-29: año de la importación (el de la CAMPAÑA de la nota,
        # o el de la fecha si no la trae). El picker de /dolares lo escribe en
        # el concepto del anticipo como `58/26`.
        "anio": service._anio_de(r).get("anio"),
        "kg": r.get("kg"),
        "anticipos": float(r.get("anticipo_aplicado") or 0)
                     if r.get("anticipo_aplicado") is not None else
                     float((r.get("anticipo") or {}).get("importe_total") or 0),
        "compras_n": int(comp.get("n") or 0),
        "compras_usd": float(comp.get("importe_total") or 0),
        # Tamara 14/10/2026: lo que dispara el aviso "ya llegó".
        "recibida": bool(r.get("recibida")),
        "fecha_recepcion": str(r.get("fecha_recepcion") or "")[:10],
    }


def _de_la_campana(out: list[dict]) -> tuple[int | None, list[dict]]:
    """Sólo las del año más alto del proveedor, en orden numérico.

    Tamara 2026-08-20: *"no debería buscar en años anteriores"*. Va por el año
    más alto de los DATOS y no por el reloj: en enero el año del calendario
    todavía no tiene ninguna importación.
    """
    top = max((r["anio"] for r in out if r.get("anio")), default=None)
    if top is not None:
        out = [r for r in out if r.get("anio") == top]
    # TMT 2026-07-23 (dueña): orden numérico, para escanear.
    out.sort(key=lambda x: (x.get("numero") is None, x.get("numero") or 0))
    return top, out


def _todas() -> list[dict]:
    try:
        rows = service.importaciones_con_cruce(limite=400)
    except Exception:  # noqa: BLE001
        return []
    return [_fila(r) for r in rows if r.get("prov")]


def del_proveedor(prov: str, filas: list[dict] | None = None) -> tuple[int | None, list[dict]]:
    """(año, importaciones) de un proveedor — lo que lista el picker."""
    prov = (prov or "").strip().upper()
    filas = _todas() if filas is None else filas
    return _de_la_campana([f for f in filas if f["prov"] == prov])


def sin_llegar_con_numero(numero: int, excluir: str = "") -> list[dict]:
    """Las importaciones de OTROS proveedores con ese número que no llegaron.

    Es la pregunta "¿era la AI 49?" del aviso: el error del 14/10 fue el
    proveedor, no el número.
    """
    excluir = (excluir or "").strip().upper()
    filas = _todas()
    out: list[dict] = []
    for prov in sorted({f["prov"] for f in filas} - {excluir}):
        _, suyas = del_proveedor(prov, filas)
        out += [f for f in suyas if f.get("numero") == numero and not f["recibida"]]
    return out


_NUM_ANIO = re.compile(r"(\d{1,6})[A-Za-z]?\s*(?:/\s*(\d{2,4}))?")


def ya_llego(prov: str, concepto: str) -> dict | None:
    """La importación a la que apunta el anticipo, si ya llegó. None si no.

    Mismo cruce que el picker: el primer número del concepto, y el año si lo
    trae (`49/26`); sin año, la campaña más alta del proveedor. Fail-soft: si
    Asinfo no contesta, no frena la carga.
    """
    m = _NUM_ANIO.search(concepto or "")
    if not prov or not m:
        return None
    numero = int(m.group(1))
    anio = None
    if m.group(2):
        a = int(m.group(2))
        anio = a + 2000 if a < 100 else a
    filas = [f for f in _todas() if f["prov"] == (prov or "").strip().upper()]
    if anio is None:
        _, filas = _de_la_campana(filas)
    else:
        filas = [f for f in filas if f.get("anio") == anio]
    for f in filas:
        if f.get("numero") == numero and f["recibida"]:
            return f
    return None
