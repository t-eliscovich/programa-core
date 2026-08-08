"""Feriados de Ecuador (nacionales) + local de Quito, y días hábiles.

Federico 2026-08-08 — Para el kg de VENTA por RITMO de la fila Proyección del
balance (venta actual × días hábiles del mes / días hábiles transcurridos), un
"día hábil" es lunes a viernes MENOS los feriados. Federico pidió descontar los
feriados **nacionales de Ecuador Y el local de Quito** (Fundación de Quito).

Los feriados se calculan por año (fecha de observancia, ya con los TRASLADOS de
la Ley vigente), así que sirve para cualquier año sin tabla hardcodeada. Se
verificó que reproduce el calendario oficial 2026 (ver test_feriados()).

Reglas de traslado (Ministerio del Trabajo, feriados movibles):
    martes    → lunes anterior      (puente)
    miércoles → viernes de la semana
    jueves    → viernes de la semana
    sábado    → viernes anterior
    domingo   → lunes siguiente
    lunes/viernes → no se mueven
Los feriados de fecha fija religiosa/civil que NO se trasladan: Año Nuevo (1/1),
Día del Trabajo (1/5), Navidad (25/12), Viernes Santo y Carnaval (que por
definición ya caen viernes / lunes y martes).

Si dos feriados movibles caen en el mismo día observado (p.ej. Difuntos 2/11 y
Independencia de Cuenca 3/11 en 2026), el segundo se queda en su fecha original
en vez de pisar al primero — así el calendario da los dos días libres, igual que
el oficial.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache


def _pascua(anio: int) -> date:
    """Domingo de Pascua (rito occidental) — algoritmo de Meeus/Butcher."""
    a = anio % 19
    b = anio // 100
    c = anio % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(anio, mes, dia)


def _traslado(d: date) -> date:
    """Fecha de observancia de un feriado movible según la Ley vigente."""
    wd = d.weekday()  # lun=0 .. dom=6
    if wd == 1:       # martes → lunes anterior
        return d - timedelta(days=1)
    if wd == 2:       # miércoles → viernes de la semana
        return d + timedelta(days=2)
    if wd == 3:       # jueves → viernes de la semana
        return d + timedelta(days=1)
    if wd == 5:       # sábado → viernes anterior
        return d - timedelta(days=1)
    if wd == 6:       # domingo → lunes siguiente
        return d + timedelta(days=1)
    return d          # lunes / viernes → sin cambio


@lru_cache(maxsize=64)
def feriados_ec_quito(anio: int) -> frozenset[date]:
    """Conjunto de fechas NO laborables (observadas) de Ecuador + Quito en `anio`.

    Incluye los feriados nacionales de Ecuador y el local de Quito (Fundación de
    Quito, 6/12). Devuelve las fechas ya trasladadas — o sea, el día que la
    gente efectivamente descansa.
    """
    pascua = _pascua(anio)
    observados: set[date] = set()

    # 1) Fijos que NO se trasladan.
    for fijo in (
        date(anio, 1, 1),    # Año Nuevo
        date(anio, 5, 1),    # Día del Trabajo
        date(anio, 12, 25),  # Navidad
    ):
        observados.add(fijo)

    # 2) Carnaval (lunes y martes previos al Miércoles de Ceniza) y Viernes
    #    Santo — derivados de Pascua, ya caen en su día, sin traslado.
    observados.add(pascua - timedelta(days=48))  # Carnaval lunes
    observados.add(pascua - timedelta(days=47))  # Carnaval martes
    observados.add(pascua - timedelta(days=2))   # Viernes Santo

    # 3) Movibles con traslado. Orden cronológico: si el traslado cae sobre un
    #    feriado ya puesto, se deja la fecha original (para no perder un día).
    movibles = (
        date(anio, 5, 24),   # Batalla de Pichincha
        date(anio, 8, 10),   # Primer Grito de Independencia
        date(anio, 10, 9),   # Independencia de Guayaquil
        date(anio, 11, 2),   # Día de los Difuntos
        date(anio, 11, 3),   # Independencia de Cuenca
        date(anio, 12, 6),   # Fundación de Quito (local)
    )
    for original in movibles:
        obs = _traslado(original)
        if obs in observados:
            obs = original  # no pisar otro feriado → mantener día real
        observados.add(obs)

    return frozenset(observados)


def es_habil(d: date) -> bool:
    """True si `d` es día hábil: lunes-viernes y no feriado EC/Quito."""
    return d.weekday() < 5 and d not in feriados_ec_quito(d.year)


def dias_habiles_ec(desde: date, hasta: date) -> int:
    """Días hábiles (lun-vie sin feriados EC/Quito) entre `desde` y `hasta`, ambos inclusive."""
    if hasta < desde:
        return 0
    total = 0
    d = desde
    while d <= hasta:
        if es_habil(d):
            total += 1
        d += timedelta(days=1)
    return total


def test_feriados() -> None:  # pragma: no cover — verificación manual
    """Chequea que 2026 reproduce el calendario oficial y los conteos de agosto."""
    obs = feriados_ec_quito(2026)
    esperados = {
        date(2026, 1, 1),    # jue Año Nuevo
        date(2026, 2, 16),   # lun Carnaval
        date(2026, 2, 17),   # mar Carnaval
        date(2026, 4, 3),    # vie Viernes Santo
        date(2026, 5, 1),    # vie Trabajo
        date(2026, 5, 25),   # lun Pichincha (trasl. del dom 24)
        date(2026, 8, 10),   # lun Primer Grito
        date(2026, 10, 9),   # vie Guayaquil
        date(2026, 11, 2),   # lun Difuntos
        date(2026, 11, 3),   # mar Cuenca (queda en su día por colisión)
        date(2026, 12, 25),  # vie Navidad
        date(2026, 12, 7),   # lun Fundación Quito (trasl. del dom 6)
    }
    assert obs == esperados, sorted(obs ^ esperados)
    # Agosto 2026: 21 días L-V, menos lun 10 = 20 hábiles.
    assert dias_habiles_ec(date(2026, 8, 1), date(2026, 8, 31)) == 20
    # Transcurridos al sábado 8/8 = lun3..vie7 = 5.
    assert dias_habiles_ec(date(2026, 8, 1), date(2026, 8, 8)) == 5
    print("OK feriados 2026")


if __name__ == "__main__":  # pragma: no cover
    test_feriados()
