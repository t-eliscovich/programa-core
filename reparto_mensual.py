"""Cómo se reparte un monto MENSUAL entre los días del mes.

Dos cosas del programa gastan un monto fijo por mes y lo van cargando de a
poco, día por día, para que la utilidad baje parejo en vez de pegar un salto
una vez por mes:

- La **depreciación** de los activos fijos (`scintela.activos.cuota`).
- Las **provisiones** que engordan las deudas YY/RT (`scintela.provisiones`).

Las dos venían con un reparto torcido, heredado del programa viejo:

- Los activos dividían **siempre entre 30**, con tope (`min(día, 30) / 30`).
  En un mes de 31 días la cuota se terminaba el 30 y el 31 no movía nada; en
  febrero el mes cerraba en 28/30 = 93,3% y el 6,7% que faltaba saltaba el
  1 de marzo.
- Las provisiones sólo corrían de **lunes a viernes**, así que el gasto del
  mes dependía de cuántos días hábiles cayeran: un mes de 23 hábiles gastaba
  un 10% más que uno de 21.

Pedido de Tamara (25/08/2026): *"que se calcule y se incremente todos los
días. Mismo monto mensual, pero si es 100 entonces cada día de un mes de 31
se sube 100/31, si es 30 entonces 100/30, contando sábados y domingos"*.

Desde el **1 de septiembre de 2026** las dos se reparten igual: el monto del
mes dividido por los **días reales del mes** (31, 30, 29 o 28), todos los
días. El total del mes no cambia — cambia en cuántos pedazos se parte, y
ahora es el mismo todos los meses.

El corte existe para no mover hacia atrás ningún mes ya vivido, y para que
el cambio entre sin escalón: septiembre tiene 30 días, así que para los
activos las dos fórmulas dan exactamente lo mismo.

Este archivo es el único lugar donde vive la regla del lado de Python. Del
lado de la base vive en `scintela.coef_amortizacion(date)` (migración 0221),
que hace la misma cuenta para los activos.
"""
from __future__ import annotations

import calendar
from datetime import date

#: Desde este día se reparte por los días reales del mes.
CORTE_DIAS_REALES = date(2026, 9, 1)

#: Días hábiles que tiene un mes en promedio (261 al año ÷ 12). Es el
#: divisor con el que venían calculadas las provisiones cuando sólo corrían
#: de lunes a viernes, y el que usamos para pasar sus cuotas diarias viejas
#: a monto mensual sin cambiar lo que se viene gastando por año.
DIAS_HABILES_PROMEDIO = 21.75


def dias_del_mes(fecha: date) -> int:
    """Días que tiene el mes de `fecha` (28, 29, 30 o 31)."""
    return calendar.monthrange(fecha.year, fecha.month)[1]


# ---------------------------------------------------------------- activos

def coef_activos(fecha: date) -> float:
    """Qué parte de la cuota del mes de un activo ya corrió al día `fecha`.

    Va de 0 a 1. El día del cierre da 1 — con la regla nueva, exacto en
    todos los meses.
    """
    if fecha >= CORTE_DIAS_REALES:
        return fecha.day / dias_del_mes(fecha)
    return min(fecha.day, 30) / 30.0


def deprec_del_dia(cuota_mensual, fecha: date) -> float:
    """Cuánto deprecia en el día `fecha` un activo de cuota mensual dada."""
    divisor = dias_del_mes(fecha) if fecha >= CORTE_DIAS_REALES else 30
    return float(cuota_mensual or 0) / divisor


def coef_activos_sql(expr_fecha: str = "(CURRENT_TIMESTAMP - INTERVAL '5 hours')::date") -> str:
    """`coef_activos` para meter adentro de un SELECT.

    `expr_fecha` es una expresión SQL que da un `date`; por defecto, hoy en
    Ecuador (UTC−5). Devuelve una llamada a la función de la base, así la
    regla no queda copiada en cada consulta.
    """
    return f"scintela.coef_amortizacion({expr_fecha})"


# ------------------------------------------------------------ provisiones

def provision_corre(fecha: date) -> bool:
    """Si una provisión suma su cuota en el día `fecha`.

    Desde el corte suma todos los días, sábados y domingos incluidos. Antes,
    sólo de lunes a viernes (como venía del programa viejo).
    """
    if fecha >= CORTE_DIAS_REALES:
        return True
    return fecha.weekday() < 5


def cuota_del_dia(cuota_mensual, fecha: date) -> float:
    """Cuánto suma en el día `fecha` una provisión de cuota mensual dada.

    Desde el corte: el mensual dividido por los días del mes. Antes: el
    mensual dividido por los días hábiles promedio, que devuelve la misma
    cuota diaria con la que venían cargadas.
    """
    divisor = dias_del_mes(fecha) if fecha >= CORTE_DIAS_REALES else DIAS_HABILES_PROMEDIO
    return float(cuota_mensual or 0) / divisor
