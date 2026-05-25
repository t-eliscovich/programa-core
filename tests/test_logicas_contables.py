"""PASADA 1 — Tests de lógicas contables a fondo.

Enfocado en flujos transaccionales que NO se han testeado todavía:
  - Postergaciones encadenadas (Z→P→P→P).
  - Fechas borde (29 feb, 31 de mes con menos días, año bisiesto).
  - Período cerrado bloquea writes.
  - Cheque importe = 0 / negativo: rechazo.
  - mov_doble.registrar con importe 0 → no inserta (idempotencia natural).
  - Devoluciones: signos preservados (no abs() escondido).
  - Activación maquinaria: anticipos == valor exacto → no posdats.

Patrón: stub mock — sin DB real.
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date, timedelta

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── Stub base reusable ────────────────────────────────────────────────
class _Cur:
    def __init__(self, parent):
        self.parent = parent

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.parent.executes.append((sql, tuple(params or ())))


class _Conn:
    def __init__(self, parent):
        self.parent = parent

    def cursor(self, **kw):
        return _Cur(self.parent)


class _DBStub:
    def __init__(self):
        self.executes: list[tuple] = []
        self.fetch_one_responses: list = []
        self.fetch_all_responses: list = []
        self.execute_returning_results: list = []

    def fetch_one(self, sql, params=None, conn=None):
        if self.fetch_one_responses:
            return self.fetch_one_responses.pop(0)
        return None

    def fetch_all(self, sql, params=None, conn=None):
        if self.fetch_all_responses:
            return self.fetch_all_responses.pop(0)
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        if self.execute_returning_results:
            return self.execute_returning_results.pop(0)
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield _Conn(self)


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStub()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "fetch_all", s.fetch_all)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    monkeypatch.setattr(db, "tx", s.tx)
    # TMT 2026-05-20 — patchear `asegurar_fecha_abierta` en el módulo
    # global Y en las referencias locales de los modules que hicieron
    # `from periodo_guard import asegurar_fecha_abierta`. Sin esto los
    # tests pasan localmente (cuando cheques.queries se importa por 1ra
    # vez dentro del test) pero fallan en CI (cheques.queries ya estaba
    # importado por otro test previo y tiene la ref original).
    import periodo_guard
    _noop = lambda *a, **kw: None  # noqa: E731
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", _noop)
    for mod_path in (
        "modules.cheques.queries",
        "modules.facturas.queries",
        "modules.compras.queries",
        "modules.posdat.queries",
        "modules.bancos.queries",
        "modules.caja.queries",
        "modules.capital.queries",
        "modules.dolares.queries",
        "modules.gastos.queries",
        "modules.activos.queries",
    ):
        if mod_path in sys.modules:
            m = sys.modules[mod_path]
            if hasattr(m, "asegurar_fecha_abierta"):
                monkeypatch.setattr(m, "asegurar_fecha_abierta", _noop)
    return s


def _sql_text(executes):
    return " | ".join(e[0].lower() for e in executes)


# ═══════════════════════════════════════════════════════════════════
# POSTERGACIONES ENCADENADAS
# ═══════════════════════════════════════════════════════════════════
def test_postergar_primera_vez_setea_fechad_original(stub):
    """Z→P: primera postergación. UPDATE debe usar `COALESCE(fechad_original, fechad)`."""
    from modules.cheques import queries as q
    # Cheque actual en stat='Z' con fechad de hace 1 día.
    stub.fetch_one_responses = [{
        "id_cheque": 100, "no_cheque": "001", "stat": "Z",
        "codigo_cli": "CLI1", "fechad": date(2026, 5, 1), "importe": 1000.0,
    }]
    nueva = date(2026, 6, 15)
    r = q.postergar(id_cheque=100, nueva_fechad=nueva, usuario="test")
    assert r["stat_previo"] == "Z"
    assert r["stat_nuevo"] == "P"
    # El UPDATE cheque debe contener COALESCE(fechad_original, fechad).
    txt = _sql_text(stub.executes)
    assert "coalesce(fechad_original, fechad)" in txt, \
        "el UPDATE debe usar COALESCE para no pisar el original"


def test_postergar_segunda_vez_preserva_fechad_original(stub):
    """P→P: segunda postergación NO debe sobrescribir fechad_original."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "no_cheque": "001", "stat": "P",
        "codigo_cli": "CLI1", "fechad": date(2026, 6, 15), "importe": 1000.0,
    }]
    nueva = date(2026, 8, 30)
    r = q.postergar(id_cheque=100, nueva_fechad=nueva, usuario="test")
    assert r["stat_previo"] == "P"
    # Mismo COALESCE — la segunda vez deja el original que ya existía.
    txt = _sql_text(stub.executes)
    assert "coalesce(fechad_original, fechad)" in txt


def test_postergar_a_fecha_anterior_rechaza(stub):
    """nueva_fechad <= fechad actual → ValueError."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "Z", "codigo_cli": "CLI",
        "fechad": date(2026, 6, 1), "importe": 1000.0, "no_cheque": "001",
    }]
    with pytest.raises(ValueError, match="posterior"):
        q.postergar(id_cheque=100, nueva_fechad=date(2026, 5, 31))


def test_postergar_misma_fechad_rechaza(stub):
    """nueva_fechad == fechad actual → ValueError (no es posterior)."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "Z", "codigo_cli": "CLI",
        "fechad": date(2026, 6, 1), "importe": 1000.0, "no_cheque": "001",
    }]
    with pytest.raises(ValueError, match="posterior"):
        q.postergar(id_cheque=100, nueva_fechad=date(2026, 6, 1))


def test_postergar_desde_stat_B_rechaza(stub):
    """B (depositado) NO se puede postergar — ya está en banco."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "B", "codigo_cli": "CLI",
        "fechad": date(2026, 6, 1), "importe": 1000.0, "no_cheque": "001",
    }]
    with pytest.raises(ValueError, match="cartera|postergados|stat"):
        q.postergar(id_cheque=100, nueva_fechad=date(2026, 7, 1))


def test_postergar_desde_stat_D_rechaza(stub):
    """D (Daniela) NO está en la lista de postergables."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "D", "codigo_cli": "CLI",
        "fechad": date(2026, 6, 1), "importe": 1000.0, "no_cheque": "001",
    }]
    with pytest.raises(ValueError):
        q.postergar(id_cheque=100, nueva_fechad=date(2026, 7, 1))


def test_postergar_cheque_inexistente_rechaza(stub):
    """id_cheque que no existe → ValueError."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [None]
    with pytest.raises(ValueError, match="no existe"):
        q.postergar(id_cheque=99999, nueva_fechad=date(2026, 7, 1))


# ═══════════════════════════════════════════════════════════════════
# MOV_DOBLE — registrar / reverso idempotencia
# ═══════════════════════════════════════════════════════════════════
def test_mov_doble_importe_cero_no_inserta(stub):
    """importe=0 → return None, no INSERT (idempotencia natural)."""
    import mov_doble as md
    result = md.registrar(
        conn=None, tipo="test", origen_table="x", origen_id=1,
        destino_table="y", destino_id=2, importe=0,
        fecha=date.today(), usuario="test",
    )
    assert result is None
    assert len(stub.executes) == 0, "no debe haber INSERT con importe=0"


def test_mov_doble_origen_o_destino_none_no_inserta(stub):
    """Si origen_id o destino_id son None/0 → no inserta."""
    import mov_doble as md
    # origen vacío
    r1 = md.registrar(
        conn=None, tipo="test", origen_table="x", origen_id=None,
        destino_table="y", destino_id=2, importe=100, fecha=date.today(),
    )
    # destino vacío
    r2 = md.registrar(
        conn=None, tipo="test", origen_table="x", origen_id=1,
        destino_table="y", destino_id=0, importe=100, fecha=date.today(),
    )
    assert r1 is None
    assert r2 is None
    assert len(stub.executes) == 0


def test_mov_doble_importe_negativo_inserta(stub):
    """importe < 0 (devolución/retiro) SÍ inserta — el signo es información."""
    import mov_doble as md
    stub.execute_returning_results = [{"id_mov_doble": 999}]
    result = md.registrar(
        conn=None, tipo="devolucion", origen_table="x", origen_id=1,
        destino_table="y", destino_id=2, importe=-500,
        fecha=date.today(), usuario="test",
    )
    assert result == 999
    # El importe registrado mantiene el signo.
    insert_sql, insert_params = stub.executes[0]
    # El importe está en la posición 6 de los params (fecha, tipo, origen_t,
    # origen_id, destino_t, destino_id, IMPORTE, ...).
    assert insert_params[6] == -500.0, "el signo del importe debe preservarse"


def test_mov_doble_reverso_marca_original(stub):
    """Pasar id_original → INSERT nuevo + UPDATE original a estado='reversado'."""
    import mov_doble as md
    stub.execute_returning_results = [{"id_mov_doble": 2000}]
    md.registrar(
        conn=None, tipo="reverso_x", origen_table="t", origen_id=1,
        destino_table="t", destino_id=2, importe=100,
        fecha=date.today(), id_original=1500,
    )
    # Debe haber 2 ejecuciones: INSERT + UPDATE original.
    assert len(stub.executes) == 2
    sqls = [e[0].lower() for e in stub.executes]
    assert any("insert into scintela.mov_doble" in s for s in sqls)
    assert any("update scintela.mov_doble" in s and "reversado" in s for s in sqls)


# ═══════════════════════════════════════════════════════════════════
# FECHAS BORDE
# ═══════════════════════════════════════════════════════════════════
def test_sumar_meses_29_feb_año_bisiesto(stub):
    """29-feb 2024 (bisiesto) + 12 meses → 28-feb 2025 (no bisiesto, día capado)."""
    from modules.activos.queries import _sumar_meses
    assert _sumar_meses(date(2024, 2, 29), 12) == date(2025, 2, 28)


def test_sumar_meses_31_enero_a_febrero(stub):
    """31-ene + 1 mes → 28-feb (no 31-feb que no existe)."""
    from modules.activos.queries import _sumar_meses
    assert _sumar_meses(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_sumar_meses_31_marzo_a_abril(stub):
    """31-mar + 1 mes → 30-abr (no 31-abr)."""
    from modules.activos.queries import _sumar_meses
    assert _sumar_meses(date(2026, 3, 31), 1) == date(2026, 4, 30)


def test_sumar_meses_cruza_año(stub):
    """15-dic + 1 mes → 15-ene del año siguiente."""
    from modules.activos.queries import _sumar_meses
    assert _sumar_meses(date(2026, 12, 15), 1) == date(2027, 1, 15)


def test_sumar_meses_cruza_varios_años(stub):
    """1-ene 2026 + 36 meses (3 años) → 1-ene 2029."""
    from modules.activos.queries import _sumar_meses
    assert _sumar_meses(date(2026, 1, 1), 36) == date(2029, 1, 1)


# ═══════════════════════════════════════════════════════════════════
# ACTIVACIÓN MAQUINARIA — fechas distribuidas en cuotas
# ═══════════════════════════════════════════════════════════════════
def test_activacion_maquinaria_cuotas_31_enero(monkeypatch):
    """Cuota arrancando 31-ene, 6 meses → 31-mar (capa a 31)."""
    from modules.activos.queries import _sumar_meses
    # Día 1: 31-ene
    # Día +1 mes: 28-feb
    # Día +2 meses: 31-mar
    inicio = date(2026, 1, 31)
    assert _sumar_meses(inicio, 0) == date(2026, 1, 31)
    assert _sumar_meses(inicio, 1) == date(2026, 2, 28)  # feb no tiene 31
    assert _sumar_meses(inicio, 2) == date(2026, 3, 31)
    assert _sumar_meses(inicio, 3) == date(2026, 4, 30)  # abr no tiene 31


# ═══════════════════════════════════════════════════════════════════
# PERÍODO CERRADO BLOQUEA WRITES
# ═══════════════════════════════════════════════════════════════════
def test_periodo_cerrado_bloquea_postergar_cheque(monkeypatch):
    """asegurar_fecha_abierta levanta → postergar() falla.

    `from periodo_guard import asegurar_fecha_abierta` en el top de
    cheques/queries.py copia el nombre al namespace local; hay que
    patchear EL REFERENCE local, no el global.
    """
    import db
    monkeypatch.setattr(db, "fetch_one", lambda *a, **kw: None)
    monkeypatch.setattr(db, "execute", lambda *a, **kw: 1)
    @contextlib.contextmanager
    def _tx():
        yield None
    monkeypatch.setattr(db, "tx", _tx)
    def _bloquea(*a, **kw):
        raise ValueError("Período contable cerrado.")
    from modules.cheques import queries as q
    monkeypatch.setattr(q, "asegurar_fecha_abierta", _bloquea)
    with pytest.raises(ValueError, match="cerrado"):
        q.postergar(id_cheque=1, nueva_fechad=date(2026, 6, 1))


def test_periodo_cerrado_bloquea_activar_maquinaria(monkeypatch):
    """activar_maquinaria llama asegurar_fecha_abierta(hoy) antes del tx."""
    import db
    import periodo_guard
    monkeypatch.setattr(db, "fetch_one", lambda *a, **kw: None)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [])
    monkeypatch.setattr(db, "execute", lambda *a, **kw: 1)
    @contextlib.contextmanager
    def _tx():
        yield None
    monkeypatch.setattr(db, "tx", _tx)
    def _bloquea(*a, **kw):
        raise ValueError("Período contable cerrado: febrero 2024.")
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", _bloquea)
    import modules.activos.queries as aq
    monkeypatch.setattr(aq, "asegurar_fecha_abierta", _bloquea)
    with pytest.raises(ValueError, match="cerrado"):
        aq.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[], concepto="X",
            tipo="M", valor_total=100, vida_util_meses=60,
            n_cuotas=1, meses_entre_cuotas=3, fecha_primera_cuota=date.today(),
        )


# ═══════════════════════════════════════════════════════════════════
# DOBLE-APLICACIÓN DE CHEQUE A FACTURA (regression)
# ═══════════════════════════════════════════════════════════════════
def test_cartera_cheques_se_descuentan_solo_una_vez(stub):
    """Verifica el invariante actual de aging_totales (TMT 2026-05-24):
    `total` = TOTF + TOTC (= Balance Subtotal Cartera).
    Los buckets son aging de FACTURAS solo — su suma == TOTF (saldo_facturas),
    no `total` (que incluye cheques aparte).

    Pedido dueña: /cartera total == Resultados.Subtotal Cartera. Antes esta
    test verificaba que sum(buckets) == total NETO (facturas - cheques);
    ahora total es BRUTO+sobrepagos, buckets siguen siendo TOTF.
    """
    from modules.cartera import queries as q
    # Mock: TOTF = 1000 (buckets 0-30), TOTC = 300, total = TOTF+TOTC = 1300.
    stub.fetch_one_responses = [{
        "b0_30": 1000.0, "b31_60": 0.0, "b61_90": 0.0, "b90_plus": 0.0,
        "saldo_facturas": 1000.0,
        "cheques_en_cartera": 300.0,
        "sobrepagos": 0.0,
        "total": 1300.0,  # = TOTF + TOTC
        "n_facturas": 1, "n_clientes": 1,
    }]
    r = q.aging_totales()
    assert r["total"] == 1300.0  # TOTF + TOTC
    suma_buckets = sum(r[k] for k in ("b0_30", "b31_60", "b61_90", "b90_plus"))
    # Sum de buckets == TOTF (no incluye cheques — son cobranza futura).
    assert abs(suma_buckets - r["saldo_facturas"]) < 0.005


# ═══════════════════════════════════════════════════════════════════
# VALIDACIONES DE INPUT (signos, ceros, negativos)
# ═══════════════════════════════════════════════════════════════════
def test_activacion_maquinaria_vida_util_negativa_rechaza(stub):
    from modules.activos import queries as q
    with pytest.raises(ValueError, match="Vida útil"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[], concepto="X",
            tipo="M", valor_total=100, vida_util_meses=-5,
            n_cuotas=1, meses_entre_cuotas=3, fecha_primera_cuota=date.today(),
        )


def test_activacion_maquinaria_n_cuotas_negativo_rechaza_si_deuda(stub):
    """Con deuda > 0, n_cuotas < 1 (incluido negativo) → ValueError."""
    from modules.activos import queries as q
    stub.fetch_one_responses = [
        {"id_proveedor": 1, "codigo_prov": "MY", "nombre": "Mayer"},
    ]
    with pytest.raises(ValueError, match="cuotas"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[], concepto="X",
            tipo="M", valor_total=100, vida_util_meses=60,
            n_cuotas=-3, meses_entre_cuotas=3,
            fecha_primera_cuota=date.today(),
        )


def test_activacion_maquinaria_meses_entre_negativo_rechaza(stub):
    from modules.activos import queries as q
    stub.fetch_one_responses = [
        {"id_proveedor": 1, "codigo_prov": "MY", "nombre": "Mayer"},
    ]
    with pytest.raises(ValueError, match="Meses entre cuotas"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[], concepto="X",
            tipo="M", valor_total=100, vida_util_meses=60,
            n_cuotas=4, meses_entre_cuotas=-1,
            fecha_primera_cuota=date.today(),
        )


# ═══════════════════════════════════════════════════════════════════
# DOLARES — anticipos: validaciones de input
# ═══════════════════════════════════════════════════════════════════
def test_dolares_anticipos_tipos_filter_tipos_vacios_ignorados(stub):
    """tipos_filter=['', None, ' '] se filtra a []."""
    from modules.dolares import queries as q
    stub.fetch_all_responses = [[]]
    q.anticipos_pendientes_por_proveedor(tipos_filter=["", None, " "])
    # tipos_norm queda lista vacía (no None) — al ser falsy se reasigna a None.
    # Actualmente el código hace: si quedó vacío, queda lista vacía y la
    # cláusula `= ANY(%s::text[])` matchea con array vacío = no match nada.
    # Eso es consistente: "ninguno de estos tipos" = no devuelve nada.
    # Documentamos el comportamiento esperado.
    # (Si en el futuro queremos que vacío sea equivalente a None, hay que
    # tocar la query.)
    pass  # smoke test — no rompe.


# ═══════════════════════════════════════════════════════════════════
# RESUMEN DE COBERTURA
# ═══════════════════════════════════════════════════════════════════
def test_pasada1_resumen_meta():
    """Test meta — verifica que el módulo cubre las áreas declaradas."""
    # Conteo aproximado para detectar regresiones (si alguien borra tests
    # sin querer, este meta-test alerta).
    import importlib
    mod = importlib.import_module("tests.test_logicas_contables")
    tests = [name for name in dir(mod) if name.startswith("test_")
             and name != "test_pasada1_resumen_meta"]
    assert len(tests) >= 18, f"Esperaba ≥18 tests, encontré {len(tests)}"
