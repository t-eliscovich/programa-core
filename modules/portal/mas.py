"""Lo que el cliente ve desde "Más": su año en kilos, sus pedidos, sus
datos. Rediseño 04/09/2026; el 09/09 la dueña sacó "cómo pagar", "avisar un
pago", "actividad" y "facturas pagadas" ("o no sirven o la info está en
otros lados").

Regla de la casa: el portal no calcula plata. Lo que suma sale de
`informes.queries`; acá se arma la pantalla y se guarda el AVISO de
corrección de datos (que nunca escribe en la ficha: lo atiende la oficina).
"""
from __future__ import annotations

import logging

from filters import today_ec

from . import presentacion

_LOG = logging.getLogger("programa_core.portal")



# ---------------------------------------------------------------------------
# Su año en kilos
# ---------------------------------------------------------------------------

#: Cuántos años para atrás se pueden elegir en "Su año en kilos".
ANIOS_PARA_ATRAS = 3


def anio_en_kilos(cod: str, anio: int | None = None) -> dict:
    """``{"meses": [{"mes": date, "etiqueta": "Sep", "kg": float,
    "importe": float, "pct": 0..100}], "kg": total, "importe": total,
    "max_kg": float, "anio": int | None, "anios": [int, …]}`` — los últimos
    12 meses, o un año calendario si `anio` viene (dueña 09/09/2026: "que
    puedan ver otros años"). Los meses en que no compró van en 0."""
    from datetime import date

    from modules.informes import queries as q

    hoy = today_ec()
    anios = list(range(hoy.year, hoy.year - ANIOS_PARA_ATRAS - 1, -1))
    if anio is not None and anio not in anios:
        anio = anios[0]
    if anio is None:
        filas = {f["mes"]: f for f in q.compras_por_mes_cliente(cod, 12)}
        meses = []
        y, m = hoy.year, hoy.month
        for _ in range(12):
            meses.append(date(y, m, 1))
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        meses.reverse()
    else:
        filas = {f["mes"]: f for f in q.compras_por_mes_cliente_anio(cod, anio)}
        meses = [date(anio, m, 1) for m in range(1, 13)]
    salida = []
    for d in meses:
        f = filas.get(d) or {}
        salida.append({"mes": d,
                       "etiqueta": presentacion.MESES[d.month - 1][:3].capitalize(),
                       "kg": presentacion.numero(f.get("kg")),
                       "importe": presentacion.numero(f.get("importe")),
                       "facturas": int(f.get("facturas") or 0)})
    max_kg = max((x["kg"] for x in salida), default=0.0)
    for x in salida:
        x["pct"] = round(100 * x["kg"] / max_kg) if max_kg > 0 else 0
    return {"meses": salida, "kg": sum(x["kg"] for x in salida),
            "importe": sum(x["importe"] for x in salida), "max_kg": max_kg,
            "anio": anio, "anios": anios}


def que_compro(cod: str, ruc: str, anio: int | None) -> dict:
    """Qué telas y colores compró en el año (Asinfo), para "Su año en kilos".
    Sin año elegido, el año en curso. Fail-soft: la pantalla lo dice."""
    from modules.asinfo import factura_lineas

    try:
        return factura_lineas.que_compro_en_el_anio(cod, ruc, anio or today_ec().year)
    except Exception as e:  # noqa: BLE001 -- el cuadro no puede tumbar la pantalla (09/09: un import mal escrito dio 500 en producción)
        _LOG.warning("portal: qué compró no disponible (%s)", e)
        return {"estado": "error", "telas": []}


# ---------------------------------------------------------------------------
# Sus pedidos (los mismos que ve su vendedor, filtrados por SU código)
# ---------------------------------------------------------------------------

def pedidos_de(cod: str) -> dict:
    """``{"ok": bool, "pedidos": [...], "etapas": {numero: ...}}``."""
    from modules.pedidos import service as pedidos_service

    try:
        todos, ok = pedidos_service.por_pedido()
    except Exception as e:  # noqa: BLE001 -- el puente no puede tumbar la pantalla
        _LOG.warning("portal: pedidos no disponibles (%s)", e)
        return {"ok": False, "pedidos": [], "etapas": {}}
    if not ok:
        return {"ok": False, "pedidos": [], "etapas": {}}
    cod = (cod or "").strip().upper()
    mios = [p for p in todos if (p.get("codigo_cliente") or "").strip().upper() == cod]
    etapas = {}
    if mios:
        try:
            from modules._lib import formulas_memos
            estados = formulas_memos.estados([p["numero"] for p in mios])
            activos = {n: v for n, v in estados.items() if v.get("estado") != "cancelado"}
            etapas = pedidos_service.etapas_por_pedido(mios, activos)
        except Exception as e:  # noqa: BLE001 -- sin etapas, la lista igual sirve
            _LOG.warning("portal: etapas de pedidos no disponibles (%s)", e)
    return {"ok": True, "pedidos": mios, "etapas": etapas}


# ---------------------------------------------------------------------------
# Pedir que corrijan sus datos
# ---------------------------------------------------------------------------

def pedir_correccion(cod: str, nombre: str, vend: str, texto: str) -> bool:
    """Un aviso a la campanita de la oficina (y a la del vendedor, que es la
    misma campanita), con lo que el cliente escribió. NO toca la ficha."""
    from modules.avisos import queries as avisos

    texto = (texto or "").strip()[:600]
    if not texto:
        return False
    return avisos.avisar(
        fuente="portal", nivel="ok",
        titulo=f"{cod} pide corregir sus datos",
        detalle=f"{presentacion.nombre_lindo(nombre)} (vendedor {vend or '—'}) escribió desde el portal:\n{texto}",
        url=f"/clientes/{cod}/editar")
