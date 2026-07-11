"""Filtro de monto unificado en Facturas (dueña 2026-07-11).

Un solo campo `monto`:
  · entero "729"          → dólar entero: 729.00 ≤ importe < 730.00.
  · con centavos "729,51" → exacto (banda ±medio centavo por floats).

El view deriva monto_min/monto_max y se los pasa a queries.buscar(). Fijamos
esa derivación acá.
"""

from __future__ import annotations

from unittest.mock import patch

from modules.facturas import queries as fq


def _login(app, fake_db, permisos=("informes.ver", "facturas.ver")):
    rid = fake_db.add_role("Facturas", list(permisos))
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _capturar_monto(app, fake_db, monto):
    """Devuelve (monto_min, monto_max) que el view le pasó a buscar()."""
    c = _login(app, fake_db)
    calls = []

    def _spy(*a, **kw):
        calls.append(kw)
        return []

    with patch.object(fq, "buscar", _spy):
        c.get(f"/facturas?monto={monto}")
    con_monto = [k for k in calls if k.get("monto_min") is not None]
    assert con_monto, f"buscar() nunca recibió monto_min para monto={monto!r}"
    k = con_monto[0]
    return k["monto_min"], k["monto_max"]


def test_entero_es_bucket_de_dolar(app, fake_db):
    lo, hi = _capturar_monto(app, fake_db, "729")
    assert abs(lo - 729.0) < 1e-6
    assert 729.99 <= hi < 730.0            # incluye 729.10 y 729.51, excluye 730.00


def test_con_centavos_es_exacto(app, fake_db):
    lo, hi = _capturar_monto(app, fake_db, "729,51")
    assert lo < 729.51 < hi                 # banda angosta alrededor del valor
    assert (hi - lo) < 0.02                  # ±medio centavo, no un bucket entero


def test_otro_dolar_no_trae_vecinos(app, fake_db):
    lo, hi = _capturar_monto(app, fake_db, "728")
    assert abs(lo - 728.0) < 1e-6
    assert 728.99 <= hi < 729.0              # 728.01 sí, 729.10 no
