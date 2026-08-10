"""La TERCERA ruta de depósito — la que el fix del 05/08 no vio.

QUÉ PASÓ. El 05/08 se corrigieron las dos rutas que SACAN un cheque de
cartera (`depositar_lote`, `transicionar_stat`): la fecha del depósito dejó
de escribirse en `fechaing` (día de INGRESO) y pasó a `fechaout` (día de
SALIDA). El test de aquel día —`test_cheque_fecha_salida_cartera`— fijó la
regla como "toda ruta que SACA un cheque de cartera escribe `fechaout`".

Hay una tercera que no saca nada: `crear()`. El cheque NACE afuera, ya
depositado (90/91 → 'B') o ya cobrado en caja (99 → 'C'). Por no sacar nada
de cartera no entraba en la frase del test, y siguió escribiendo sólo
`fechaing`. Cinco días después:

  · 104 cheques por $116.459,12 (banco 90, alex y andres, 05→08/08)
    depositados sin `fechaout`;
  · 13 cobros en efectivo (banco 99, stat 'C') sin NINGUNA de las dos
    fechas — que el detector NO veía, porque busca un movimiento bancario
    'DE' y el efectivo va a CAJA.

LA LECCIÓN: el test escrito con la forma del bug ("la ruta que saca")
protege el caso que ya pasó, no el invariante. El invariante es sobre el
ESTADO, no sobre el camino: **un cheque que no está en cartera tiene fecha
de salida**. Da igual si salió o si nació afuera.
"""
from __future__ import annotations

import inspect
import re
from datetime import date

import pytest

# ── Números REALES medidos en producción el 10/08/2026 ──────────────────
CASOS_QUE_PASARON = [
    # (no_banco, stat que fuerza `crear()`, qué es)
    (90, "B", "depósito directo en Pichincha — los 104 de $116.459,12"),
    (99, "C", "cobro en efectivo — los 13 sin ninguna fecha"),
]


class _DBCaptura:
    """Captura los INSERT de `crear()` sin tocar Postgres (no hay en CI)."""

    def __init__(self):
        self.inserts: list[tuple[str, tuple]] = []

    def execute_returning(self, sql, params=None, conn=None):
        self.inserts.append((" ".join(sql.split()).lower(), params or ()))
        return {"id_cheque": 777, "no_cheque": "", "id_transaccion": 1}

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.banco" in s:
            return {"no_banco": 10, "nombre": "PICHINCHA"}
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        return 1

    def tx(self):
        import contextlib
        return contextlib.nullcontext(object())


def _columnas_y_valores(sql: str, params: tuple) -> dict:
    """Mapea el INSERT de scintela.cheque a {columna: valor}."""
    cols = re.search(r"insert into scintela\.cheque\s*\((.*?)\)\s*values",
                     sql, re.S).group(1)
    nombres = [c.strip() for c in cols.split(",")]
    assert len(nombres) == len(params), (
        f"el INSERT declara {len(nombres)} columnas y pasa {len(params)} "
        f"valores — una columna quedó desalineada"
    )
    return dict(zip(nombres, params, strict=True))


def _crear(monkeypatch, *, no_banco, stat):
    from modules.cheques import queries as q

    cap = _DBCaptura()
    monkeypatch.setattr(q, "db", cap)
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda *a, **k: None)
    monkeypatch.setattr(q, "_banco_real_para_deposito", lambda *a, **k: 10)
    monkeypatch.setattr(q._concepto_cobro, "bootstrap_columna",
                        lambda **k: None)
    import mov_doble
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: None)
    import bank_helpers
    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario",
                        lambda *a, **k: {"id_transaccion": 1})
    monkeypatch.setattr(q, "insertar_caja", lambda *a, **k: {"id_caja": 1},
                        raising=False)

    q.crear(
        fecha=date(2026, 8, 10), codigo_cli="TNZ", no_cheque="",
        importe=5901.45, no_banco=no_banco, stat=stat, usuario="alex",
    )
    sql, params = cap.inserts[0]
    return _columnas_y_valores(sql, params)


@pytest.mark.parametrize("no_banco,stat,caso", CASOS_QUE_PASARON)
def test_el_cheque_que_nace_afuera_lleva_fechaout(monkeypatch, no_banco,
                                                  stat, caso):
    """⭐ LA REGRESIÓN. Entró y salió el mismo día: las dos fechas existen."""
    fila = _crear(monkeypatch, no_banco=no_banco, stat=stat)

    assert "fechaout" in fila, (
        f"el INSERT de crear() ni siquiera nombra fechaout ({caso})"
    )
    assert fila["fechaout"] == date(2026, 8, 10), (
        f"{caso}: el cheque nace fuera de cartera y quedó sin fecha de "
        f"salida — es exactamente lo que dejó 104 cheques colgados"
    )


def test_el_deposito_directo_no_pierde_su_fechaing(monkeypatch):
    """`fechaing` NO se toca: el resumen del día agrupa por día de INGRESO.

    Ese resumen se imprime para contabilidad. Moverlo para arreglar otra
    cosa es cómo se fabricaron los 46 fantasmas del 04/08.
    """
    fila = _crear(monkeypatch, no_banco=90, stat="B")
    assert fila["fechaing"] == date(2026, 8, 10)


def test_el_efectivo_sigue_sin_fechaing(monkeypatch):
    """El 99 nunca tuvo `fechaing` y este fix no se la inventa.

    Dársela lo metería en el resumen de cobranza del día — puede que
    corresponda, pero es una decisión de la dueña, no un efecto colateral.
    """
    fila = _crear(monkeypatch, no_banco=99, stat="C")
    assert fila["fechaing"] is None


def test_el_cheque_que_nace_EN_cartera_no_lleva_fechaout(monkeypatch):
    """El candado del candado: un cheque vivo no puede tener fecha de salida."""
    fila = _crear(monkeypatch, no_banco=10, stat="Z")
    assert fila["fechaout"] is None
    assert fila["fechaing"] is None


def test_el_invariante_esta_escrito_sobre_el_ESTADO_no_sobre_la_ruta():
    """La regla no puede volver a decir "la ruta que saca de cartera".

    Si mañana aparece una cuarta forma de que un cheque termine fuera de
    cartera, el gate tiene que cubrirla sin que nadie se acuerde de este día.
    """
    from modules.cheques import queries as q

    src = " ".join(inspect.getsource(q.crear).split())
    assert 'not in STATS_EN_CARTERA' in src, (
        "el gate de fechaout tiene que preguntar por el ESTADO final, no "
        "enumerar los bancos 90/91/99 — enumerar es lo que dejó afuera al 99"
    )
    assert set(q.STATS_EN_CARTERA) >= {"Z", "P", "D"}, (
        "Z/P/D son los estados aplicables a factura: el cheque sigue vivo"
    )
    for depositado in q.STATS_DEPOSITADO:
        assert depositado not in q.STATS_EN_CARTERA, (
            f"stat '{depositado}' no puede estar en las dos listas"
        )


# ── El detector que lo cazó, y lo que él mismo no veía ──────────────────

def _fila(**kw):
    base = dict(id_cheque=102082, no_cheque="", codigo_cli="TNZ",
                importe="5901.45", stat="B", no_banco=90, usuario_crea="alex",
                fecha_deposito="2026-08-08")
    base.update(kw)
    return base


def test_el_detector_cuenta_el_universo_no_la_pagina():
    """⭐ Reportaba 50 cuando eran 104: contaba las filas ya recortadas.

    Un detector que subreporta hace que el arreglo parezca más chico de lo
    que es — y el LIMIT era exactamente igual al número que publicaba, que
    es la forma en que un tope se disfraza de medición.
    """
    from modules.admin_dbase.health_audit_view import _evaluar_fechaout

    alerts, stats = _evaluar_fechaout(
        n_con_mov=104, filas_con_mov=[_fila() for _ in range(20)],
        n_nace_afuera=0, filas_nace_afuera=[],
    )

    assert stats["n_sin_fechaout"] == 104
    assert "104 cheque(s)" in alerts[0]["que"]
    assert len(alerts[0]["filas"]) == 20, "la muestra sigue acotada"


def test_el_efectivo_lo_ve_la_rama_que_no_pregunta_por_el_banco():
    """Los 13 cobros en efectivo no generan un 'DE': la rama (a) es ciega.

    Por eso el invariante se evalúa también sobre el ESTADO, sin pasar por
    `transacciones_bancarias`.
    """
    from modules.admin_dbase.health_audit_view import _evaluar_fechaout

    alerts, stats = _evaluar_fechaout(
        n_con_mov=0, filas_con_mov=[],
        n_nace_afuera=13,
        filas_nace_afuera=[_fila(stat="C", no_banco=99, nacio="2026-08-07")],
    )

    assert stats["n_nacidos_fuera_de_cartera_sin_fechaout"] == 13
    assert len(alerts) == 1
    assert "NACIERON fuera de cartera" in alerts[0]["que"]
    assert "crear()" in alerts[0]["por_que"], (
        "la alerta tiene que mandar a la función correcta: la sesión del "
        "10/08 se fue en buscar la ruta en las de depósito"
    )
    assert alerts[0]["donde_mirar"] == ["banco 99 · stat C"]


def test_sin_nada_colgado_el_detector_se_calla():
    """Un ⚠ diario por algo legítimo entrena a ignorar el panel entero."""
    from modules.admin_dbase.health_audit_view import _evaluar_fechaout

    alerts, stats = _evaluar_fechaout(
        n_con_mov=0, filas_con_mov=[], n_nace_afuera=0, filas_nace_afuera=[])

    assert alerts == []
    assert stats["n_sin_fechaout"] == 0
    assert stats["n_nacidos_fuera_de_cartera_sin_fechaout"] == 0


def test_el_COUNT_del_detector_sale_de_su_propia_consulta():
    """⭐ El bug del `LIMIT` disfrazado de medición, cerrado desde el código.

    Los tres tests de arriba le pasan el universo a `_evaluar_fechaout` a
    mano, así que ninguno se entera si el CALL SITE vuelve a contar la lista
    ya recortada. Este mira ahí: los dos totales tienen que salir de un
    `COUNT` propio y nunca de un `len(...)` sobre la muestra.
    """
    from modules.admin_dbase import health_audit_view

    src = " ".join(inspect.getsource(
        health_audit_view.deposito_sin_fechaout).split())

    for arg in ("n_con_mov", "n_nace_afuera"):
        assert not re.search(rf"{arg}\s*=\s*len\s*\(", src), (
            f"`{arg}` volvió a contar la lista recortada por el LIMIT — eso "
            f"es lo que hacía publicar 50 cuando eran 104"
        )
    assert src.lower().count("select count(") >= 2, (
        "cada rama necesita su propio COUNT sobre el universo entero"
    )


# ── La migración 0185: el backfill de las 117 ───────────────────────────

def _migracion_0185():
    import importlib.util
    from pathlib import Path

    ruta = (Path(__file__).resolve().parent.parent / "migrations"
            / "0185_backfill_fechaout_nacidos_afuera.py")
    spec = importlib.util.spec_from_file_location("mig0185", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _CursorFake:
    def __init__(self, filas):
        self.filas = filas
        self.updates: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if s.lstrip().startswith("select"):
            self.select_params = params
        else:
            self.updates.append((s, params or ()))

    def fetchall(self):
        return self.filas

    def close(self):
        pass


class _ConnFake:
    def __init__(self, filas):
        self.cur = _CursorFake(filas)

    def cursor(self):
        return self.cur


def test_la_migracion_escribe_la_fecha_de_salida_de_cada_fila():
    mod = _migracion_0185()
    conn = _ConnFake([
        (102082, "B", 90, 5901.45, date(2026, 8, 8)),
        (102500, "C", 99, 1200.00, date(2026, 8, 7)),
    ])
    mod.run(conn)

    assert len(conn.cur.updates) == 2
    ids = [u[1][-1] for u in conn.cur.updates]
    assert ids == [102082, 102500]
    assert [u[1][0] for u in conn.cur.updates] == [date(2026, 8, 8),
                                                   date(2026, 8, 7)]


def test_la_migracion_es_idempotente_y_no_toca_fechaing():
    """`fechaout IS NULL` en el WHERE = correrla dos veces no repisa nada."""
    mod = _migracion_0185()
    conn = _ConnFake([(102082, "B", 90, 5901.45, date(2026, 8, 8))])
    mod.run(conn)

    sql = conn.cur.updates[0][0]
    assert "and fechaout is null" in sql, "sin esto deja de ser idempotente"
    assert "fechaing" not in sql, (
        "el resumen de cobranza del día agrupa por día de INGRESO y se "
        "imprime para contabilidad — esta migración no lo toca"
    )


def test_la_fila_sin_fecha_se_saltea_pero_no_voltea_el_deploy():
    """Media limpieza, sí; deploy caído por una fila rara, no.

    El script original abortaba entero. Una migración que aborta deja la app
    sin reiniciar, así que acá la fila sin fecha se saltea — y no queda
    escondida: el vigía la sigue marcando en HIGH.
    """
    mod = _migracion_0185()
    conn = _ConnFake([
        (102082, "B", 90, 5901.45, date(2026, 8, 8)),
        (999999, "B", 90, 10.00, None),
    ])
    mod.run(conn)

    assert len(conn.cur.updates) == 1
    assert conn.cur.updates[0][1][-1] == 102082


def test_la_migracion_pregunta_por_el_ESTADO_no_por_el_camino():
    """No filtra por `usuario_modifica`: eso pregunta por el camino.

    Así se MIDIÓ (para probar que nadie más las había tocado), pero como
    criterio dejaría afuera una fila que alguien editó en el medio — y esa
    fila sigue estando fuera de cartera sin fecha de salida.
    """
    mod = _migracion_0185()
    assert "usuario_modifica" not in mod.SELECT_OBJETIVO.lower().split(
        "set")[0], "el SELECT no puede filtrar por quién la tocó"
    assert set(mod.EN_CARTERA) == {"Z", "P", "D", "1", "2", "3"}


def test_el_vigia_tampoco_pregunta_por_quien_toco_la_fila():
    """El vigía mira el ESTADO, igual que el fix y que la mig 0185.

    La rama (b) filtraba `usuario_modifica IS NULL`. Con eso, editarle el
    concepto a un cheque roto lo volvía invisible aunque siguiera fuera de
    cartera y sin fecha de salida: una puerta de atrás que se abre sola con el
    uso normal del sistema.
    """
    from modules.admin_dbase import health_audit_view

    src = " ".join(inspect.getsource(
        health_audit_view.deposito_sin_fechaout).split())
    sin_comentarios = re.sub(r"#[^\n]*", "", inspect.getsource(
        health_audit_view.deposito_sin_fechaout))

    assert "usuario_modifica IS NULL" not in sin_comentarios, (
        "eso pregunta por el CAMINO (quién tocó la fila), no por el estado"
    )
    assert "usuario_crea" in src, (
        "el vigía sigue diciendo de qué usuario vienen — eso es informar, no "
        "filtrar"
    )
