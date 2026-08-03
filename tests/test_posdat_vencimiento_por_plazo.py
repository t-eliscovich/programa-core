"""El vencimiento de una posdat sin fecha de venc. sale del PLAZO del proveedor.

Dueña 2026-08-03, sobre dos líneas OP cargadas ese día (#918 `AI 31/32/34/42`
y #919 `AC 37/40/48/73`) que salían con **Venc. = 03/08/2026**, el mismo día de
la carga: *"el proveedor OP no tiene asignado un amount de tiempo para
vencimiento? La fecha de vencimiento seguro no es hoy"*. Sí lo tiene:
`proveedor.plazo = 99` para OP (y la línea anterior, `AC 36-38-57 [CM]`,
cargada el 15/07 con venc 22/10, es 15/07 + 99 EXACTO).

El bug estaba en que `posdat.queries.crear` hacía `fechad = fecha` cuando el
alta no traía vencimiento — a diferencia de `compras.queries.crear`, que ya
usaba `fecha + proveedor.plazo`. Resultado: la deuda nacía vencida el mismo día
y el flujo de fondos (que ordena por `fechad`) la imputaba a hoy.

También cubre la columna FECHA de /posdat: tiene que mostrar la fecha de CARGA,
no la del último retiro (ver `posdat/lista.html`).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.posdat import queries as pq  # noqa: E402


class _FakeDB:
    """Sustituye `db` dentro de modules.posdat.queries para el alta."""

    def __init__(self, plazo):
        self.plazo = plazo
        self.insert_params = None

    # --- lecturas que hace crear() ---
    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "COALESCE(MAX(num), 0) + 1" in s:
            return {"n": 7}
        if "SELECT plazo FROM scintela.proveedor" in s:
            return {"plazo": self.plazo}
        if "FROM scintela.proveedor WHERE codigo_prov" in s:
            return {"x": 1}
        return None

    def execute_returning(self, sql, params=None, conn=None):
        self.insert_params = params
        return {"id_posdat": 1}

    def execute(self, *a, **k):  # pragma: no cover - defensivo
        return None


@pytest.fixture()
def fake_db(monkeypatch):
    def _mk(plazo):
        f = _FakeDB(plazo)
        monkeypatch.setattr(pq, "db", f)
        monkeypatch.setattr(pq, "asegurar_fecha_abierta", lambda *_a, **_k: None)
        monkeypatch.setattr(pq, "registrar_alta", lambda *_a, **_k: None, raising=False)
        return f

    return _mk


def _fechad_de(fake):
    """El INSERT es (num, fecha, fechad, prov, ...) — fechad es el 3er param."""
    assert fake.insert_params is not None, "no se ejecutó el INSERT"
    return fake.insert_params[2]


def test_op_sin_vencimiento_usa_los_99_dias_de_plazo(fake_db):
    f = fake_db(99)
    pq.crear(
        fecha=date(2026, 8, 3),
        fechad=None,
        prov="OP",
        importe=-37323.84,
        concepto="AI 31/32/34/42",
    )
    # 03/08/2026 + 99 días = 10/11/2026 (no "hoy").
    assert _fechad_de(f) == date(2026, 11, 10)


def test_reproduce_la_linea_vieja_que_si_estaba_bien(fake_db):
    """`AC 36-38-57 [CM]`: cargada 15/07/2026, venc real en la base 22/10/2026.
    Es exactamente fecha + 99 → confirma que 99 es el plazo que se venía usando
    a mano."""
    f = fake_db(99)
    pq.crear(
        fecha=date(2026, 7, 15),
        fechad=None,
        prov="OP",
        importe=-37723.47,
        concepto="AC 36-38-57 [CM]",
    )
    assert _fechad_de(f) == date(2026, 10, 22)


def test_vencimiento_explicito_gana_sobre_el_plazo(fake_db):
    f = fake_db(99)
    pq.crear(
        fecha=date(2026, 8, 3),
        fechad=date(2026, 9, 30),
        prov="OP",
        importe=-100.0,
        concepto="con venc a mano",
    )
    assert _fechad_de(f) == date(2026, 9, 30)


def test_proveedor_sin_plazo_mantiene_el_comportamiento_viejo(fake_db):
    """Sin plazo cargado (None o 0) seguimos con fechad = fecha."""
    for plazo in (None, 0):
        f = fake_db(plazo)
        pq.crear(
            fecha=date(2026, 8, 3),
            fechad=None,
            prov="AC",
            importe=-100.0,
            concepto="sin plazo",
        )
        assert _fechad_de(f) == date(2026, 8, 3)


def test_yy_no_consulta_proveedor_y_queda_en_la_fecha(fake_db):
    """YY es un proveedor virtual (no tiene ficha) — no puede romper."""
    f = fake_db(99)
    pq.crear(
        fecha=date(2026, 8, 3),
        fechad=None,
        prov="YY",
        importe=0,
        concepto="",
    )
    assert _fechad_de(f) == date(2026, 8, 3)


def test_columna_fecha_muestra_la_carga_no_el_retiro():
    """Regresión de la confusión que disparó todo: la fila OP #918 TENÍA
    fecha 03/08 y la pantalla mostraba '—' porque priorizaba el retiro."""
    tpl = (ROOT / "modules/posdat/templates/posdat/lista.html").read_text()
    celda = tpl.split("<th", 1)[0] if False else tpl
    i = celda.index("{%- if f.fecha -%}")
    j = celda.index("</td>", i)
    bloque = celda[i:j]
    # La fecha propia se evalúa PRIMERO; el retiro es sólo fallback.
    assert bloque.index("f.fecha | fecha_es") < bloque.index("op_fecha_hecha")
