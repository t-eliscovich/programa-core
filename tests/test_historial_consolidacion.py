"""Tests de la lógica nueva de la pantalla Historial.

- consolidar_snapshots_mes_actual: deja las N columnas más recientes del
  mes en curso y borra el resto.
- eliminar_ultima_columna_mes_actual: borra la columna más reciente y
  deja viva la previa (botón "Eliminar última columna").
- _hora_quito: convierte fecha_crea (UTC) a hora de Quito.

Usan mocks de `db` — no necesitan Postgres.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes import queries


def test_consolidar_deja_las_2_mas_recientes(monkeypatch):
    """conservar=2 → el DELETE usa LIMIT 2 y devuelve lo borrado."""
    cap: dict = {}

    def fake_execute(sql, params=None):
        cap["sql"] = sql
        cap["params"] = params
        return 3

    monkeypatch.setattr(queries.db, "execute", fake_execute)

    n = queries.consolidar_snapshots_mes_actual(conservar=2)

    assert n == 3
    assert cap["params"]["k"] == 2
    assert "DELETE FROM scintela.historia" in cap["sql"]
    assert "LIMIT %(k)s" in cap["sql"]


def test_consolidar_nunca_borra_todo(monkeypatch):
    """conservar=0 se clampa a 1 — nunca deja el mes sin ninguna columna."""
    cap: dict = {}
    monkeypatch.setattr(
        queries.db, "execute",
        lambda sql, params=None: cap.update(params=params) or 0,
    )
    queries.consolidar_snapshots_mes_actual(conservar=0)
    assert cap["params"]["k"] == 1


def test_consolidar_solo_toca_el_mes_actual(monkeypatch):
    """Regla de fin de mes: la consolidación SOLO borra columnas del mes
    en curso — nunca de meses ya cerrados.

    Esto garantiza que, al entrar el 1 de junio, la columna del 31 de
    mayo NO se toca: queda viva como resultado mensual de mayo.
    """
    cap: dict = {}
    monkeypatch.setattr(
        queries.db, "execute",
        lambda sql, params=None: cap.update(sql=sql, params=params) or 0,
    )
    queries.consolidar_snapshots_mes_actual(conservar=2)

    # `today_ec()`, no `date.today()`: el server corre en UTC y Ecuador está a
    # −5. Entre las 19:00 y la medianoche de Ecuador, UTC ya está en el día (y a
    # fin de mes, en el MES) siguiente — este test fallaba todas las noches a
    # partir de las 19:00 EC, y el 31 a la noche encima con un mes de
    # diferencia. Es el mismo error que el test dice estar cuidando.
    from filters import today_ec
    hoy = today_ec()
    assert cap["params"]["a"] == hoy.year
    assert cap["params"]["m"] == hoy.month
    assert "EXTRACT(YEAR FROM fecha) = %(a)s" in cap["sql"]
    assert "EXTRACT(MONTH FROM fecha) = %(m)s" in cap["sql"]


def test_eliminar_ultima_borra_la_mas_reciente(monkeypatch):
    """Borra la columna más reciente del mes y la reporta."""
    cap: dict = {}
    monkeypatch.setattr(queries.db, "fetch_one",
                        lambda sql, params=None: {"id_historia": 77})

    def fake_execute(sql, params=None):
        cap["params"] = params
        return 1

    monkeypatch.setattr(queries.db, "execute", fake_execute)

    r = queries.eliminar_ultima_columna_mes_actual()

    assert r["borrado"] is True
    assert r["id_historia"] == 77
    assert cap["params"] == (77,)


def test_eliminar_ultima_sin_columnas_no_rompe(monkeypatch):
    """Si no hay columnas del mes, no borra nada y no crashea."""
    monkeypatch.setattr(queries.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(queries.db, "execute", lambda *a, **k: 0)

    r = queries.eliminar_ultima_columna_mes_actual()

    assert r["borrado"] is False


def test_hora_quito_resta_5_horas():
    """fecha_crea viene en UTC (server/RDS); se muestra en hora de Quito
    (UTC-5, sin horario de verano)."""

# ---------------------------------------------------------------------------
# TMT 2026-07-31 — la FOTO DIARIA no se consolida.
# ---------------------------------------------------------------------------
def test_consolidar_no_borra_la_foto_diaria(monkeypatch):
    """INVARIANTE: las filas `snapshot-diario` quedan FUERA del DELETE.

    El bug: entrar a Historial tomaba una foto propia y después consolidaba a
    2 columnas, así que la foto diaria de AYER se borraba todos los días. Sin
    la fila de ayer, `snapshot_diario_health` nunca podía comparar y las
    guardas "patrimonio saltó > $500k" y "stock saltó > 5%" NUNCA corrían —
    en silencio, sin un solo error.
    """
    cap: dict = {}
    monkeypatch.setattr(
        queries.db, "execute",
        lambda sql, params=None: cap.update(sql=sql, params=params) or 0,
    )
    queries.consolidar_snapshots_mes_actual(conservar=2)

    assert cap["params"]["diario"] == queries.USUARIO_SNAPSHOT_DIARIO
    # La exclusión tiene que estar en el DELETE de afuera, NO sólo en el
    # subselect de los que se conservan (ahí no protegería nada).
    delete_head = cap["sql"].split("id_historia NOT IN")[0]
    assert "COALESCE(usuario_crea, '') <> %(diario)s" in delete_head


def test_consolidar_sigue_borrando_los_manuales(monkeypatch):
    """La exclusión es SÓLO para la foto diaria: los snapshots manuales del
    mes en curso se siguen consolidando a `conservar`."""
    cap: dict = {}
    monkeypatch.setattr(
        queries.db, "execute",
        lambda sql, params=None: cap.update(sql=sql, params=params) or 4,
    )
    n = queries.consolidar_snapshots_mes_actual(conservar=2)

    assert n == 4
    assert "LIMIT %(k)s" in cap["sql"]
    assert cap["params"]["k"] == 2
