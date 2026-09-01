"""El cierre de mes automático (el cron SIEMPRE corre al día siguiente del
mes que cierra) tiene que usar el balance LIVE, no la rama `as_of` rota.

Incidente 2026-09-01: `_es_live` comparaba `mes == hoy.month`, que para el
cron mensual (invocado el día 1 del mes SIGUIENTE) da SIEMPRE False —
ningún cierre automático podía caer en la rama LIVE, pasara lo que pasara.
Julio "zafó" porque una foto diaria (rama SIEMPRE live) alcanzó a ocupar el
31/07 antes de que el cron intentara cerrar por as_of. Agosto no tuvo esa
suerte: cerró por as_of, que tiene la cartera reconstruida rota (usa el
saldo ACTUAL de la factura, no el saldo a la fecha de cierre) y
`_flujos_vivos_del_mes` devuelve `{}` (no hay `resultados` por esa rama) —
dejó agosto con cartera $4,09M en vez de $7,76M y utilidad NEGATIVA.

El fix: usar días de atraso (`hoy - fecha_snap`), no comparación de mes
calendario, con una ventana de gracia de 2 días — la misma aproximación
que ya había salvado a julio por casualidad, ahora sin depender de que
alguien visite la pantalla justo antes de medianoche.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from modules.informes import queries


class _StopAfterBalance(Exception):
    """Corta la ejecución justo después de elegir la rama del balance —
    no hace falta simular el resto de `crear_snapshot_historia` (INSERT,
    dolares, paquete PDF, etc.) para probar CUÁL rama se eligió."""


def _correr_hasta_elegir_balance(anio, mes, *, hoy, existe_cierre=False):
    llamadas = {"live": 0, "as_of": 0}

    def _fake_live():
        llamadas["live"] += 1
        raise _StopAfterBalance

    def _fake_as_of(fecha_snap):
        llamadas["as_of"] += 1
        raise _StopAfterBalance

    with patch.object(queries, "today_ec", return_value=hoy), \
         patch.object(queries.db, "fetch_one", return_value=(
             {"1": 1} if existe_cierre else None)), \
         patch.object(queries, "informe_balance", side_effect=_fake_live), \
         patch.object(queries, "informe_balance_as_of", side_effect=_fake_as_of):
        try:
            queries.crear_snapshot_historia(anio, mes, forzar=True)
        except _StopAfterBalance:
            pass
    return llamadas


def test_cierre_el_mismo_dia_usa_live():
    # 31/08, corriendo el 31/08 mismo (caso manual, mismo día).
    llamadas = _correr_hasta_elegir_balance(2026, 8, hoy=date(2026, 8, 31))
    assert llamadas == {"live": 1, "as_of": 0}


def test_cierre_un_dia_tarde_usa_live():
    # El caso real del incidente: cron del 01/09 cerrando agosto.
    llamadas = _correr_hasta_elegir_balance(2026, 8, hoy=date(2026, 9, 1))
    assert llamadas == {"live": 1, "as_of": 0}


def test_cierre_dos_dias_tarde_todavia_usa_live():
    llamadas = _correr_hasta_elegir_balance(2026, 8, hoy=date(2026, 9, 2))
    assert llamadas == {"live": 1, "as_of": 0}


def test_cierre_tres_dias_tarde_ya_usa_as_of():
    # Más allá de la ventana de gracia: as_of es la aproximación documentada
    # para backfills viejos, no el camino normal del cron.
    llamadas = _correr_hasta_elegir_balance(2026, 8, hoy=date(2026, 9, 3))
    assert llamadas == {"live": 0, "as_of": 1}


def test_backfill_muy_viejo_usa_as_of():
    llamadas = _correr_hasta_elegir_balance(2026, 5, hoy=date(2026, 9, 1))
    assert llamadas == {"live": 0, "as_of": 1}
