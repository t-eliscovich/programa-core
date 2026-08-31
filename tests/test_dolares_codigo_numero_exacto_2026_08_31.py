"""El código de /dolares pide UNA importación, no "todo lo que tenga ese número".

TMT 2026-08-31 (dueña): *"en compras, cuando quiero filtrar el AI 26 me trae
varios que tienen 26 en el concepto"*. El filtro mandaba el número al SQL como
substring (`concepto ILIKE '%26%'`), así que pidiendo el AI 26 salían también
el 23/26, el 33/26, el 44/26 y el 53/26 — donde el 26 es el AÑO de la campaña,
no el número de la importación.

Ahora el número se compara EXACTO contra el que lee `parse_ref_anticipo` (el
mismo parser con el que la pantalla cruza los anticipos con Asinfo), así que
"26", "026", "26/26" y "26 SALDO" entran, y "23/26", "126" y "2026" no.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.dolares import queries as dq  # noqa: E402

_CONCEPTOS = [
    "26",        # el que se pidió
    "026",       # con cero adelante — el mismo
    "26/26",     # número 26 de la campaña 26 — también es el que se pidió
    "26 SALDO",  # con texto atrás
    "23/26",     # el que se colaba: 26 es el AÑO
    "44/26",
    "126",       # el que se colaba: 26 adentro de otro número
    "2026",
    "",          # sin número
]


class _FakeDB:
    def __init__(self, conceptos):
        self.filas = [
            {"id_dolares": i, "fecha": date(2026, 8, 1), "cta": "AI",
             "concepto": c, "importe": 100.0, "st": None, "clave": "AND",
             "usuario_crea": "tmt"}
            for i, c in enumerate(conceptos, start=1)
        ]

    def fetch_all(self, sql, params=None):
        return [dict(f) for f in self.filas]

    def fetch_one(self, sql, params=None):
        return {}


@pytest.fixture
def fake(monkeypatch):
    f = _FakeDB(_CONCEPTOS)
    monkeypatch.setattr(dq, "db", f)
    return f


def test_numero_exacto_deja_solo_la_importacion_pedida(fake):
    filas = dq.lista(cta="AI", concepto_num=26)
    assert [f["concepto"] for f in filas] == ["26 SALDO", "26/26", "026", "26"]


def test_sin_numero_no_filtra_nada(fake):
    """Pedir sólo la cuenta ("AI") sigue trayendo todo lo de la cuenta."""
    assert len(dq.lista(cta="AI")) == len(_CONCEPTOS)


def test_el_saldo_corrido_acumula_solo_las_filas_que_se_ven(fake):
    """La columna "Saldo cta." tiene que cuadrar con lo que hay en pantalla."""
    filas = dq.lista(cta="AI", concepto_num=26)
    assert [f["saldo_acumulado"] for f in filas] == [400.0, 300.0, 200.0, 100.0]


# ── la pantalla ──────────────────────────────────────────────────────────────
def _login(app, fake_db, perms):
    rid = fake_db.add_role("Tester", perms)
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


@pytest.mark.parametrize("codigo,esperado", [
    ("AI 26", 26),
    ("AI26", 26),
    ("26", 26),
    ("AI 23/26", 23),   # el número es el de adelante, el 26 es el año
    ("AI 026", 26),
    ("AI", None),       # sólo cuenta: no filtra por número
])
def test_la_pantalla_manda_el_numero_exacto(app, fake_db, monkeypatch, codigo, esperado):
    cap = {}
    monkeypatch.setattr(dq, "lista", lambda **kw: cap.update(kw) or [])
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/dolares", query_string={"codigo": codigo})
    assert r.status_code == 200
    assert cap["concepto_num"] == esperado
