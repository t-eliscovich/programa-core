"""Tests para `scripts/procesa_provisiones_mensual.py`.

Invariantes bajo test (batch 8, 2026-04-17):

1. Happy path: las dos tareas corren en su tx, cada una queda marcada 'O'.
2. Idempotencia: una segunda invocación con slots en 'O' no re-ejecuta — exit 0.
3. Si la primera tarea falla: la segunda se intenta igual (independiente),
   el slot fallido queda en 'E', y el exit code es 1.
4. --force reemplaza un slot previo (O/E/R) y vuelve a correr.
5. Si la procedure no existe, exit code = 2 (distinto de error "algo se rompió").
6. Slot ya existe en 'R' (crashed previous run): no se re-ejecuta sin --force.

Los tests NO tocan Postgres. Monkeypatchean db.execute / db.fetch_one /
db.execute_returning / db.tx / db.init_pool con un recorder en memoria.
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date, datetime

import pytest

# Importar el script como módulo. Lo hago dentro de cada test para que
# el stub de db.init_pool llegue antes de que el script intente conectarse.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _FakeDB:
    """Stub de `db.*` que simula ejecuciones_tareas en memoria.

    El contrato que imita es el REAL (no el que a `_TxSentinel` le habría
    convenido inventarse): `db.tx()` entrega un psycopg2 conn, y el conn
    expone `.cursor()` como context manager devolviendo un cursor con
    `.execute()` y `.fetchone()`. Toda la lógica de "reconocer qué SQL
    estamos viendo" vive en `_FakeCursor.execute` — es ahí donde pegarían
    los bugs si el script volviera a usar una API inventada.

    - execute_returning() reserva slot con INSERT ... ON CONFLICT DO NOTHING
      RETURNING id. Si ya existe, devuelve None (como Postgres).
    - fetch_one() consulta el estado del slot.
    - execute() maneja UPDATEs de estado.
    - tx() entrega un `_FakeConn` que simula cursor real.
    """

    def __init__(self, *, call_handler=None):
        self.slots: dict[tuple[str, str], dict] = {}  # (tarea, periodo) -> dict
        self.next_id = 1
        # Historial de todo lo que pasó por cursor.execute() adentro de tx().
        # Útil en los tests para contar llamadas a procedures.
        self.tx_calls: list[tuple[str, tuple]] = []
        self.call_handler = call_handler or (lambda sql, params: None)
        self.init_pool_called = False

    # --- lo que el script llama directamente -------------------------------

    def init_pool(self):
        self.init_pool_called = True

    def execute_returning(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        params = tuple(params or ())
        if "insert into scintela.ejecuciones_tareas" in s and "on conflict" in s:
            tarea, periodo, host = params
            if (tarea, periodo) in self.slots:
                return None  # CONFLICT — slot ocupado
            slot_id = self.next_id
            self.next_id += 1
            self.slots[(tarea, periodo)] = {
                "id_ejecucion": slot_id,
                "tarea": tarea,
                "periodo": periodo,
                "estado": "R",
                "iniciado_en": datetime.now(),
                "terminado_en": None,
                "mensaje": None,
                "host": host,
            }
            return {"id_ejecucion": slot_id}
        # INSERT del snapshot_historia tarea — devolvemos un id fake
        if "insert into scintela.historia" in s:
            return {"id_historia": 999}
        raise AssertionError(f"execute_returning inesperado: {s[:100]}")

    def fetch_one(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        params = tuple(params or ())
        if "from scintela.ejecuciones_tareas" in s and "where tarea" in s:
            tarea, periodo = params
            slot = self.slots.get((tarea, periodo))
            return dict(slot) if slot else None
        # snapshot_historia tarea agregada en el cron — todas estas queries
        # son del calcular_kpis() y existe_snapshot(). Las dejamos pasar como
        # vacío/None para que el snapshot termine sin tocar Postgres real.
        if "from scintela.historia" in s:
            return None  # "no existe snapshot todavía"
        if "from scintela.factura" in s or "from scintela.posdat" in s \
                or "from scintela.transacciones_bancarias" in s \
                or "from scintela.xgast" in s or "from scintela.retiros" in s \
                or "from scintela.compra" in s:
            # KPIs vacíos para que el snapshot inserte zeros
            return {"v": 0, "kvent": 0, "uvent": 0, "kcom": 0, "ucom": 0}
        raise AssertionError(f"fetch_one inesperado: {s[:100]}")

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        params = tuple(params or ())
        if "update scintela.ejecuciones_tareas" in s and "set estado = 'o'" in s:
            (id_ejec,) = params
            slot = self._find_by_id(id_ejec)
            if slot:
                slot["estado"] = "O"
                slot["terminado_en"] = datetime.now()
                slot["mensaje"] = None
            return 1
        if "update scintela.ejecuciones_tareas" in s and "set estado = 'e'" in s:
            mensaje, id_ejec = params
            slot = self._find_by_id(id_ejec)
            if slot:
                slot["estado"] = "E"
                slot["terminado_en"] = datetime.now()
                slot["mensaje"] = mensaje
            return 1
        raise AssertionError(f"execute inesperado: {s[:100]}")

    # --- tx() — entrega un _FakeConn que imita un psycopg2 conn ------------

    @contextlib.contextmanager
    def tx(self):
        yield _FakeConn(self)

    # --- helpers ------------------------------------------------------------

    def _find_by_id(self, id_ejec):
        for slot in self.slots.values():
            if slot["id_ejecucion"] == id_ejec:
                return slot
        return None


class _FakeConn:
    """Imita el psycopg2 conn que `db.tx()` cede al llamador.

    Sólo expone `.cursor()`. No soporta transacciones anidadas ni commit
    manual — el caller corre bajo `with db.tx()` que ya commitea.
    """

    def __init__(self, fake: _FakeDB):
        self.fake = fake

    def cursor(self, *, cursor_factory=None):
        # psycopg2.extras.RealDictCursor vs default cursor no importa acá:
        # nuestro cursor siempre devuelve dicts (es más barato para los tests
        # y es lo que usa el código real vía RealDictCursor).
        return _FakeCursor(self.fake)


class _FakeCursor:
    """Imita un cursor psycopg2 suficiente para los usos del script."""

    def __init__(self, fake: _FakeDB):
        self.fake = fake
        self._last_row: dict | None = None

    # Context manager protocol — `with conn.cursor() as cur:`
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False  # no swallow

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        params = tuple(params or ())
        self.fake.tx_calls.append((sql, params))
        self._last_row = None

        if "delete from scintela.ejecuciones_tareas" in s:
            tarea, periodo = params
            self.fake.slots.pop((tarea, periodo), None)
            return

        if ("insert into scintela.ejecuciones_tareas" in s
                and "returning" in s
                and "on conflict" not in s):
            tarea, periodo, host = params
            slot_id = self.fake.next_id
            self.fake.next_id += 1
            self.fake.slots[(tarea, periodo)] = {
                "id_ejecucion": slot_id,
                "tarea": tarea,
                "periodo": periodo,
                "estado": "R",
                "iniciado_en": datetime.now(),
                "terminado_en": None,
                "mensaje": None,
                "host": host,
            }
            self._last_row = {"id_ejecucion": slot_id}
            return

        if s.startswith("call scintela.") or s.startswith("select scintela."):
            # Acá es donde decidimos si la procedure "explota" o no.
            # call_handler puede levantar para simular fallas.
            self.fake.call_handler(sql, params)
            return

        raise AssertionError(f"cursor.execute inesperado: {s[:100]}")

    def fetchone(self):
        return self._last_row


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch):
    """Instala un _FakeDB como sustituto de `db` en el namespace del script."""
    import db as db_mod
    fake = _FakeDB()
    monkeypatch.setattr(db_mod, "init_pool", fake.init_pool)
    monkeypatch.setattr(db_mod, "execute_returning", fake.execute_returning)
    monkeypatch.setattr(db_mod, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(db_mod, "execute", fake.execute)
    monkeypatch.setattr(db_mod, "tx", fake.tx)
    return fake


def _importar_script():
    """Importa el script bajo test — fresh cada vez para resetear logging."""
    from scripts import procesa_provisiones_mensual as ppm
    return ppm


# --- tests ----------------------------------------------------------------


def test_happy_path_ambas_tareas_ok(fake_db):
    ppm = _importar_script()
    exit_code, resultados = ppm.correr(periodo="2026-04", fecha=date(2026, 4, 17))

    assert exit_code == 0
    # 3 tareas: procesa_provisiones, actualizar_amortizacion, snapshot_historia
    assert len(resultados) == 3
    assert {r[0] for r in resultados} == {
        "procesa_provisiones", "actualizar_amortizacion", "snapshot_historia",
    }
    assert all(r[1] == "O" for r in resultados)

    # Los dos slots quedaron terminados y con estado='O'.
    slots = [fake_db.slots[(t, "2026-04")] for t, _ in ppm.TAREAS]
    assert all(s["estado"] == "O" for s in slots)
    assert all(s["terminado_en"] is not None for s in slots)
    assert all(s["mensaje"] is None for s in slots)


def test_segunda_corrida_no_re_ejecuta(fake_db):
    """Idempotencia: dos corridas en el mismo periodo no vuelven a llamar la proc."""
    ppm = _importar_script()
    exit1, _ = ppm.correr(periodo="2026-04", fecha=date(2026, 4, 17))
    assert exit1 == 0

    # Capturar cuántas tx.exec hubo de CALL/SELECT.
    calls_1 = [c for c in fake_db.tx_calls if "scintela." in c[0].lower()
               and not c[0].lower().lstrip().startswith("delete")]
    n_calls_1 = len(calls_1)

    exit2, resultados2 = ppm.correr(periodo="2026-04", fecha=date(2026, 4, 17))
    calls_2 = [c for c in fake_db.tx_calls if "scintela." in c[0].lower()
               and not c[0].lower().lstrip().startswith("delete")]

    assert exit2 == 0
    assert len(calls_2) == n_calls_1  # no se agregaron llamadas nuevas
    assert all("ya corrió" in r[2] for r in resultados2)


def test_primera_tarea_falla_segunda_igual_corre(fake_db, monkeypatch):
    """Si procesa_provisiones revienta, actualizar_amortizacion se intenta igual."""
    def handler(sql, params):
        if "procesa_provisiones" in sql:
            raise RuntimeError("boom de provisiones")
        return None
    fake_db.call_handler = handler

    ppm = _importar_script()
    exit_code, resultados = ppm.correr(periodo="2026-04", fecha=date(2026, 4, 17))

    assert exit_code == 1  # hubo un error
    por_tarea = {r[0]: r for r in resultados}
    assert por_tarea["procesa_provisiones"][1] == "E"
    assert "boom de provisiones" in por_tarea["procesa_provisiones"][2]
    # Segunda tarea igual terminó en 'O'
    assert por_tarea["actualizar_amortizacion"][1] == "O"

    # Slots reflejan lo mismo
    assert fake_db.slots[("procesa_provisiones", "2026-04")]["estado"] == "E"
    assert fake_db.slots[("actualizar_amortizacion", "2026-04")]["estado"] == "O"


def test_force_reemplaza_slot_previo_ok(fake_db):
    """--force borra el slot existente y vuelve a correr la proc."""
    ppm = _importar_script()
    ppm.correr(periodo="2026-04", fecha=date(2026, 4, 17))
    n_calls_antes = sum(1 for c in fake_db.tx_calls
                        if c[0].lower().startswith(("call", "select scintela.")))

    exit_code, resultados = ppm.correr(
        periodo="2026-04", fecha=date(2026, 4, 17), force=True,
    )
    n_calls_despues = sum(1 for c in fake_db.tx_calls
                          if c[0].lower().startswith(("call", "select scintela.")))

    assert exit_code == 0
    assert n_calls_despues > n_calls_antes  # sí, volvió a llamar
    assert all(r[1] == "O" for r in resultados)


def test_procedure_no_existe_exit_2(fake_db):
    """Si la proc no existe en el schema, exit code distinto (= 2)."""
    def handler(sql, params):
        raise RuntimeError('procedure scintela.procesa_provisiones(date) does not exist')
    fake_db.call_handler = handler

    ppm = _importar_script()
    exit_code, resultados = ppm.correr(periodo="2026-04", fecha=date(2026, 4, 17))

    assert exit_code == 2  # EXIT_MISSING_PROC
    # No intenta la segunda tarea porque salió del loop al detectar missing.
    assert len(resultados) == 1
    assert resultados[0][0] == "procesa_provisiones"
    assert resultados[0][1] == "E"


def test_slot_colgado_en_R_no_re_ejecuta_sin_force(fake_db):
    """Un run previo que murió (estado='R', terminado_en IS NULL) no se re-ejecuta solo."""
    ppm = _importar_script()
    # Reservar el slot de la primera tarea manualmente, sin terminarlo.
    fake_db.slots[("procesa_provisiones", "2026-04")] = {
        "id_ejecucion": 999,
        "tarea": "procesa_provisiones",
        "periodo": "2026-04",
        "estado": "R",
        "iniciado_en": datetime.now(),
        "terminado_en": None,
        "mensaje": None,
        "host": "crashed-host",
    }
    fake_db.next_id = 1000

    exit_code, resultados = ppm.correr(periodo="2026-04", fecha=date(2026, 4, 17))

    assert exit_code == 1  # hay error (tarea 1 quedó colgada sin force)
    por_tarea = {r[0]: r for r in resultados}
    assert por_tarea["procesa_provisiones"][1] == "R"
    assert "force" in por_tarea["procesa_provisiones"][2].lower()
    # Slot sigue igual (estado='R', no se pisó)
    assert fake_db.slots[("procesa_provisiones", "2026-04")]["estado"] == "R"
    # La segunda tarea sí corrió normal
    assert por_tarea["actualizar_amortizacion"][1] == "O"


def test_parse_args_periodo_default_mes_actual(fake_db):
    """Si no pasás --periodo, usa el mes actual."""
    ppm = _importar_script()
    # Arma argv con fecha explícita y NO periodo — debería derivar '2026-04'.
    exit_code = ppm.main(["--fecha", "2026-04-17"])
    assert exit_code == 0
    assert ("procesa_provisiones", "2026-04") in fake_db.slots


def test_periodo_formato_invalido_levanta(fake_db):
    """--periodo 'abril' revienta con ValueError antes de tocar la DB."""
    ppm = _importar_script()
    with pytest.raises(ValueError):
        ppm.main(["--periodo", "abril"])
    assert not fake_db.slots  # no se creó nada


# --- regresión del bug batch 8 --------------------------------------------
#
# Hasta 2026-04-17 este script llamaba `tx.exec(sql, params)` y
# `tx.query_one(...)` — métodos inventados. Lo que `db.tx()` realmente
# entrega es un psycopg2 conn con `.cursor()`. Los tests de arriba ya usan
# el contrato correcto a través del _FakeConn; estos tests son guardas
# explícitas que revientan si alguien vuelve a la API inventada.


def test_ejecutar_tarea_usa_cursor_no_tx_exec(monkeypatch, fake_db):
    """Si alguien vuelve a usar `tx.exec(...)` el conn no lo tiene y explota."""
    ppm = _importar_script()
    # Capturamos el conn que db.tx() entrega — tiene que tener .cursor().
    conns_vistos: list[object] = []

    original_tx = fake_db.tx

    @contextlib.contextmanager
    def tx_espiada():
        with original_tx() as conn:
            conns_vistos.append(conn)
            yield conn

    import db as db_mod
    monkeypatch.setattr(db_mod, "tx", tx_espiada)

    ppm.correr(periodo="2026-04", fecha=date(2026, 4, 17))

    # Debe haber corrido al menos una tarea — por ende pasó por _ejecutar_tarea
    # y _ejecutar_tarea abrió un cursor del conn.
    assert conns_vistos, "db.tx() nunca se invocó — el script cambió su flujo"
    for conn in conns_vistos:
        assert hasattr(conn, "cursor"), (
            "el objeto que entrega db.tx() debe tener .cursor(); si alguien lo "
            "cambia a `tx.exec(...)` este test explota"
        )
        assert not hasattr(conn, "exec"), (
            "regresión del bug batch 8: el conn NO debe tener .exec()"
        )


def test_reset_slot_fetchone_devuelve_dict(fake_db):
    """`_reset_slot` hace `row['id_ejecucion']` — si el cursor devolviera
    tuplas (default sin RealDictCursor) el cast a int explotaría."""
    ppm = _importar_script()
    # Sembrar un slot para que --force lo resetee.
    fake_db.slots[("procesa_provisiones", "2026-04")] = {
        "id_ejecucion": 1,
        "tarea": "procesa_provisiones",
        "periodo": "2026-04",
        "estado": "E",
        "iniciado_en": datetime.now(),
        "terminado_en": datetime.now(),
        "mensaje": "previo fail",
        "host": "x",
    }
    fake_db.next_id = 2
    # Esto llamaría a _reset_slot internamente. Si row fuera tupla,
    # row["id_ejecucion"] levantaría TypeError.
    exit_code, _ = ppm.correr(
        periodo="2026-04", fecha=date(2026, 4, 17), force=True
    )
    assert exit_code == 0
    # El nuevo slot quedó en 'O'
    assert fake_db.slots[("procesa_provisiones", "2026-04")]["estado"] == "O"
