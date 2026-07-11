"""Regresión bug #1/#6 — `today_ec()` fecha en hora Ecuador (UTC-5), no en UTC.

El server corre en UTC (~5h adelante de Ecuador). `date.today()` del server
salta al día siguiente después de las ~19h de Ecuador, y eso fechaba
transacciones (caja, facturas, cheques, bancos) con el día de MAÑANA. El fix
(centralizar `today_ec()`) se deployó en junio 2026 pero nunca tuvo test.

Este fija el comportamiento: de noche en Ecuador, la fecha NO salta de día.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import filters


class _FakeDatetime:
    """datetime con `now()` fijo — para simular la hora del server (UTC)."""

    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def _freeze_utc(monkeypatch, dt_utc: datetime):
    _FakeDatetime._fixed = dt_utc
    monkeypatch.setattr(filters, "datetime", _FakeDatetime)


def test_noche_ecuador_no_salta_de_dia(monkeypatch):
    # 2026-06-04 23:30 Ecuador == 2026-06-05 04:30 UTC.
    # date.today() del server (UTC) daría 05/06 (MAÑANA, el bug).
    # today_ec() debe devolver 04/06 (hoy Ecuador).
    _freeze_utc(monkeypatch, datetime(2026, 6, 5, 4, 30, tzinfo=timezone.utc))
    assert filters.today_ec() == date(2026, 6, 4)


def test_medianoche_pasada_ecuador(monkeypatch):
    # 2026-06-05 00:30 Ecuador == 2026-06-05 05:30 UTC → 05/06 en ambos.
    _freeze_utc(monkeypatch, datetime(2026, 6, 5, 5, 30, tzinfo=timezone.utc))
    assert filters.today_ec() == date(2026, 6, 5)


def test_dia_ecuador_coincide(monkeypatch):
    # 2026-06-04 12:00 Ecuador == 2026-06-04 17:00 UTC → 04/06 en ambos.
    _freeze_utc(monkeypatch, datetime(2026, 6, 4, 17, 0, tzinfo=timezone.utc))
    assert filters.today_ec() == date(2026, 6, 4)


def test_fin_de_mes_de_noche_no_adelanta(monkeypatch):
    # 2026-05-31 22:00 Ecuador == 2026-06-01 03:00 UTC. El bug adelantaba el
    # cierre de mes: today_ec() debe seguir en MAYO.
    _freeze_utc(monkeypatch, datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc))
    assert filters.today_ec() == date(2026, 5, 31)


def test_override_tiene_prioridad():
    tok = filters.set_today_override(date(2020, 1, 1))
    try:
        assert filters.today_ec() == date(2020, 1, 1)
    finally:
        filters.reset_today_override(tok)
