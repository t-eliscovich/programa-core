"""Producción de TERMINADO (Asinfo, bodega 53), día por día — solo lectura.

TMT 2026-08-04 (dueña: *"hacé una copia de tejeduría asinfo y ponéme un
terminado asinfo"*, sobre el alcance: **"no hay compras"**, y sobre el corte:
**"no quiero artículos, quiero cantidades por día, así entendemos por qué el
balance o la duda de utilidad"**).

Es la hermana de `modules/tejeduria_asinfo`, pero mucho más chica y con otro
foco:

    Tejeduría (bodega 52)              Terminado (bodega 53)
    ─────────────────────              ─────────────────────
    Hilo → Tela cruda                  Tela cruda → Producto terminado
    INTELA + 3 maquileros (RY/AP/UN)   Solo INTELA (todo `jproduccion`)
    Tarifas $/kg + auto-carga de       Nada de eso: si no hay tercero,
      compras tipo K + pasivo            no hay factura que cruzar
    Resumen POR TEJEDOR                Una fila POR DÍA

Por qué el día y no el artículo: la pregunta que la pantalla tiene que
contestar es *"¿por qué se movió la utilidad?"*, y la utilidad es Δ patrimonio.
Lo que la mueve en este paso son los kg de tela cruda que se comieron contra
los kg de terminado que salieron — la merma. Con el total del mes eso se ve
pero no se explica; con una fila por día se ve QUÉ DÍA pasó y se puede ir a
mirar ese día. El artículo no cambia ninguna decisión de balance.

Los kg salen de `asinfo_service.fabricacion_flujo_por_dia(53, …)`, que es la
MISMA query del informe Flujo de producción abierta por día: la suma de las
filas de acá da exactamente el número que muestra aquella pantalla.

Todo fail-soft: si Asinfo no contesta, `disponible=False` y la tab muestra el
aviso en vez de romperse.
"""
from modules.asinfo import service as asinfo_service

#: Bodega de Asinfo que produce el terminado. Constante y no parámetro: si
#: algún día hace falta otra, se agrega una pantalla, no un query param (que
#: se interpolaría en el SQL de Metabase).
BODEGA_TERMINADO = 53


def _fila(dia: str, n_ofs: int, consumido: float, producido: float) -> dict:
    merma_kg = consumido - producido
    return {
        "dia": dia,
        "n_ofs": n_ofs,
        "consumido": round(consumido, 2),
        "producido": round(producido, 2),
        "merma_kg": round(merma_kg, 2),
        # Sin crudo consumido no hay porcentaje posible. None y no 0.0: un
        # 0,00% se lee como "ese día no hubo merma", que es otra cosa.
        "merma_pct": (round(100.0 * merma_kg / consumido, 2)
                      if consumido > 0 else None),
    }


def resumen_mes(anio: int, mes: int) -> dict:
    """Todo lo que la tab necesita para un mes.

    Devuelve:
        {disponible, anio, mes,
         dias: [{dia, n_ofs, consumido, producido, merma_kg, merma_pct}],
         total: {dia:None, n_ofs, consumido, producido, merma_kg, merma_pct}}

    El TOTAL se calcula sumando las filas de días —no con una segunda query—
    justamente para que la fila de abajo no pueda contradecir a la tabla.
    """
    dias_raw = asinfo_service.fabricacion_flujo_por_dia(
        BODEGA_TERMINADO, anio, mes) or []
    dias = [
        _fila(str(d.get("dia") or ""), int(d.get("n_ofs") or 0),
              float(d.get("issued") or 0.0), float(d.get("fab") or 0.0))
        for d in dias_raw
    ]
    total = _fila(
        "",
        sum(d["n_ofs"] for d in dias),
        sum(d["consumido"] for d in dias),
        sum(d["producido"] for d in dias),
    )
    return {
        "disponible": bool(dias),
        "anio": anio,
        "mes": mes,
        "dias": dias,
        "total": total,
    }
