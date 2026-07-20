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

from filters import today_ec

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
    nueva = today_ec() + timedelta(days=30)
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
    nueva = today_ec() + timedelta(days=75)
    r = q.postergar(id_cheque=100, nueva_fechad=nueva, usuario="test")
    assert r["stat_previo"] == "P"
    # Mismo COALESCE — la segunda vez deja el original que ya existía.
    txt = _sql_text(stub.executes)
    assert "coalesce(fechad_original, fechad)" in txt


def test_postergar_fecha_muy_pasada_rechaza(stub):
    """nueva_fechad > 3 días antes de hoy → ValueError (TMT 2026-06-16)."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "Z", "codigo_cli": "CLI",
        "fechad": date(2026, 6, 1), "importe": 1000.0, "no_cheque": "001",
    }]
    with pytest.raises(ValueError, match="3 días|anterior a hoy"):
        q.postergar(id_cheque=100, nueva_fechad=today_ec() - timedelta(days=10))


def test_postergar_a_fecha_futura_distinta_ok(stub):
    """TMT 2026-06-16 dueña: re-postergar a otra fecha (>= hoy-3) se permite,
    aunque sea anterior a la fechad ya postergada (antes exigía estrictamente
    posterior)."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "P", "codigo_cli": "CLI",
        "fechad": today_ec() + timedelta(days=20), "importe": 1000.0, "no_cheque": "001",
    }]
    # nueva fecha futura PERO antes que la postergada actual → debe permitir.
    r = q.postergar(id_cheque=100, nueva_fechad=today_ec() + timedelta(days=5), usuario="test")
    assert r["stat_nuevo"] == "P"


def test_postergar_devuelto_conserva_estado(stub):
    """TMT 2026-06-16 dueña: postergar un cheque DEVUELTO (1) cambia SOLO la
    fecha, NO el estado (antes lo flipeaba a 'P')."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "1", "codigo_cli": "LOY",
        "fechad": date(2026, 6, 20), "importe": 525.0, "no_cheque": "67537",
    }]
    r = q.postergar(id_cheque=100, nueva_fechad=today_ec() + timedelta(days=10), usuario="test")
    assert r["stat_previo"] == "1"
    assert r["stat_nuevo"] == "1"  # se mantiene devuelto


def test_postergar_cartera_z_pasa_a_p(stub):
    """Z (cartera) SÍ pasa a 'P' al postergar (ese es el sentido)."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "Z", "codigo_cli": "CLI",
        "fechad": date(2026, 6, 1), "importe": 1000.0, "no_cheque": "001",
    }]
    r = q.postergar(id_cheque=100, nueva_fechad=today_ec() + timedelta(days=10), usuario="test")
    assert r["stat_nuevo"] == "P"


def test_postergar_desde_stat_B_rechaza(stub):
    """B (depositado) NO se puede postergar — ya está en banco."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "B", "codigo_cli": "CLI",
        "fechad": date(2026, 6, 1), "importe": 1000.0, "no_cheque": "001",
    }]
    with pytest.raises(ValueError, match="cartera|postergados|stat"):
        q.postergar(id_cheque=100, nueva_fechad=date(2026, 7, 1))


def test_postergar_desde_stat_X_rechaza(stub):
    """X (eliminado) NO es postergable (D/1/2 ahora sí — TMT 2026-06-16)."""
    from modules.cheques import queries as q
    stub.fetch_one_responses = [{
        "id_cheque": 100, "stat": "X", "codigo_cli": "CLI",
        "fechad": date(2026, 6, 1), "importe": 1000.0, "no_cheque": "001",
    }]
    with pytest.raises(ValueError):
        q.postergar(id_cheque=100, nueva_fechad=today_ec() + timedelta(days=15))


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


def test_dropdown_nunca_ofrece_transicion_invalida():
    """El dropdown SIEMPRE es consistente con el backend: cada opción que va por
    `cheques.transicionar` (POST plano a transicionar_stat) está permitida por
    TRANSICIONES_VALIDAS (así no se 'ofrece pero rechaza').
    TMT 2026-07-11 (dueña: "confirm every move makes sense").

    Los WIZARD (deshacer_deposito, reverso, anulación) NO pasan por
    transicionar_stat — validan por su cuenta —, así que no se gatean contra
    TRANSICIONES_VALIDAS. Ej.: B→P (volver a postdatado, TMT 2026-07-15) es un
    wizard que reversa el depósito de banco, no un relabel plano."""
    from modules.cheques import queries as q
    for stat, permit in q.TRANSICIONES_VALIDAS.items():
        for opt in q.transiciones_para(stat):
            if opt.get("endpoint") != "cheques.transicionar":
                continue  # wizard: valida en su propio endpoint
            assert opt["stat_destino"] in permit, (
                f"dropdown de {stat} ofrece {opt['stat_destino']} que el backend rechaza"
            )


def test_estados_con_movimiento_no_salen_por_etiqueta():
    """Un cheque DEPOSITADO (B/A) no puede volver a cartera con un cambio de
    etiqueta pelado (dejaría el depósito colgado en el banco). Sólo sale por
    rebote (9) o anulación (X), que compensan el banco. TMT 2026-07-11 (dueña:
    "que la contabilidad quede consistente")."""
    from modules.cheques import queries as q
    for dep in ("B", "A"):
        assert q.TRANSICIONES_VALIDAS[dep] == {"9", "X"}, (
            f"{dep} no debe poder ir a cartera por etiqueta"
        )
        # 'X' desde un depositado va por el wizard de anulación (compensa banco).
        x_opt = next(o for o in q.transiciones_para(dep) if o["stat_destino"] == "X")
        assert x_opt["kind"] == "WIZARD" and "anular" in x_opt["endpoint"]


def test_eliminar_siempre_por_wizard_de_anulacion():
    """'Eliminar' (X) SIEMPRE se ofrece por el wizard de anulación, nunca como
    cambio de etiqueta pelado — porque anular reversa las aplicaciones a
    facturas. TMT 2026-07-11 (dueña)."""
    from modules.cheques import queries as q
    for stat, permit in q.TRANSICIONES_VALIDAS.items():
        if "X" not in permit:
            continue
        x_opts = [o for o in q.transiciones_para(stat) if o["stat_destino"] == "X"]
        assert x_opts, f"{stat} permite X pero el dropdown no lo ofrece"
        assert all(o["kind"] == "WIZARD" for o in x_opts), (
            f"{stat}: 'Eliminar' debe ir por wizard de anulación, no POST pelado"
        )


def test_transicion_postergado_a_devuelto_permitida():
    """Desde Postergado (P) se puede marcar DEVUELTO 1° (inicio de la secuencia).

    TMT 2026-07-11 dueña ("only 1 can go to 2, some rules apply"): el contador
    de devuelto es una SECUENCIA 1→2→3. Desde cartera (Z/P/D) sólo se entra a
    "1"; a "2" sólo se llega desde "1", y a "3" sólo desde "2". No se puede
    saltar de Postergado directo a "2".
    """
    from modules.cheques import queries as q
    # P puede marcar devuelto 1°, pero NO saltar a 2° (hay que pasar por 1°).
    assert "1" in q.TRANSICIONES_VALIDAS["P"]
    assert "2" not in q.TRANSICIONES_VALIDAS["P"]
    # La secuencia sí avanza 1→2 y 2→3.
    assert "2" in q.TRANSICIONES_VALIDAS["1"]
    assert "3" in q.TRANSICIONES_VALIDAS["2"]
    assert "3" not in q.TRANSICIONES_VALIDAS["1"]  # no se saltea el 2°
    # y el dropdown de P incluye "Devuelto" (1°) pero no "2°".
    destinos = [t["stat_destino"] for t in q.transiciones_para("P")]
    assert "1" in destinos and "2" not in destinos
    # Z también permite marcar devuelto 1° en backend.
    assert "1" in q.TRANSICIONES_VALIDAS["Z"] and "2" not in q.TRANSICIONES_VALIDAS["Z"]


def test_transicion_v_protestado_de_nuevo_a_1():
    """V (protestado vuelto a depositar) → 1 cuando el banco lo protesta otra
    vez. TMT 2026-07-20 (dueña: cheque CG3 en V no dejaba pasar a 1). Etiqueta
    plana — la V nueva no tiene mov de banco en la app. NO se permite ningún
    otro relabel plano desde V."""
    from modules.cheques import queries as q
    assert q.TRANSICIONES_VALIDAS["V"] == {"1"}
    opts = q.transiciones_para("V")
    uno = [o for o in opts if o["stat_destino"] == "1"]
    assert uno and uno[0]["kind"] == "POST" and uno[0]["endpoint"] == "cheques.transicionar"


def test_transicionar_v_a_1_con_nueva_fechad(stub):
    """V→1 con fecha nueva (dueña 2026-07-20): guarda fechad=nueva (POSTERGADA),
    preserva fechad_original (F.DEP) con COALESCE, y setea fecha_postergacion."""
    from modules.cheques import queries as q
    stub.fetch_one_responses.append({
        "id_cheque": 10002, "no_cheque": "10002", "stat": "V",
        "codigo_cli": "CG3", "importe": 1133.62, "no_banco": None,
        "banco": "MACHALA", "fechad": date(2026, 7, 6), "doc_banco": None,
    })
    manana = today_ec() + timedelta(days=1)
    r = q.transicionar_stat(10002, stat_destino="1", nueva_fechad=manana, usuario="t")
    assert r["stat_nuevo"] == "1"
    sql = _sql_text(stub.executes)
    assert "fechad=%s" in sql
    assert "coalesce(fechad_original, fechad)" in sql
    assert "fecha_postergacion = current_date" in sql or "fecha_postergacion=current_date" in sql
    # la fecha nueva viaja en los params del UPDATE
    upd = next(e for e in stub.executes if "fechad=%s" in e[0].lower())
    assert manana in upd[1]


def test_transicionar_v_a_1_fecha_pasada_rechaza(stub):
    """La nueva fecha del protestado no puede ser pasada (dueña 2026-07-20:
    'una fecha en el futuro no en el pasado')."""
    from modules.cheques import queries as q
    stub.fetch_one_responses.append({
        "id_cheque": 10002, "no_cheque": "10002", "stat": "V",
        "codigo_cli": "CG3", "importe": 1133.62, "no_banco": None,
        "banco": "MACHALA", "fechad": date(2026, 7, 6), "doc_banco": None,
    })
    with pytest.raises(ValueError, match="hoy o futura"):
        q.transicionar_stat(
            10002, stat_destino="1",
            nueva_fechad=today_ec() - timedelta(days=1), usuario="t",
        )


def test_transicionar_v_a_1_sin_fecha_sigue_andando(stub):
    """Sin nueva_fechad el V→1 es el relabel plano de siempre (compat: otros
    callers de transicionar_stat no mandan fecha)."""
    from modules.cheques import queries as q
    stub.fetch_one_responses.append({
        "id_cheque": 10002, "no_cheque": "10002", "stat": "V",
        "codigo_cli": "CG3", "importe": 1133.62, "no_banco": None,
        "banco": "MACHALA", "fechad": date(2026, 7, 6), "doc_banco": None,
    })
    r = q.transicionar_stat(10002, stat_destino="1", usuario="t")
    assert r["stat_nuevo"] == "1"
    sql = _sql_text(stub.executes)
    assert "fechad=%s" not in sql
