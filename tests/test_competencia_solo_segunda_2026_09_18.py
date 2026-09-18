"""Después de la largada sólo entra segunda, y la que entra trae su puntaje.

Dueña 18/09/2026, mirando /analisis/entradas: *"de la tela parada no debería
entrar nueva, solo de segunda"*. Y el health `competencia_sin_puntaje` cantó
que Toper 1.80 (41,5 kg de segunda, entrados el 10 y 15/09) valía 1 punto por
kilo "sin que nadie lo haya decidido": el puntaje se congeló el 25/08 y la tela
no existía en la lista ese día.
"""
from datetime import date

from modules.analisis import asinfo_parado, queries
from tests.test_analisis_parado import _DBFalsa


def _refresco(monkeypatch, parados, cohorte, hoy=date(2026, 9, 18),
              venta12=None):
    db = _DBFalsa(cohorte)
    monkeypatch.setattr(queries, "db", db)
    monkeypatch.setattr(queries, "today_ec", lambda: hoy)
    monkeypatch.setattr(asinfo_parado, "parados", lambda: parados)
    monkeypatch.setattr(asinfo_parado, "llamados", lambda: [])
    monkeypatch.setattr(asinfo_parado, "vendido_desde", lambda d: [])
    monkeypatch.setattr(asinfo_parado, "share_por_grupo", lambda: [])
    monkeypatch.setattr(asinfo_parado, "venta_por_tela", lambda: venta12 or {})
    monkeypatch.setattr(asinfo_parado, "formas", lambda: {})
    monkeypatch.setattr(asinfo_parado, "ultima_venta_antes", lambda d: {})
    return db, queries.actualizar()


def _parada(sub, col, kg=100):
    return {"subcategoria": sub, "color": col, "stock_kg": kg,
            "stock_bodega": kg, "motivo": "parado", "nueva": False,
            "pedida": False, "entra": True}


def _segunda(sub, col, kg=20):
    return {"subcategoria": sub, "color": col, "stock_kg": kg,
            "stock_bodega": kg, "motivo": "segunda", "nueva": False,
            "pedida": False, "entra": True}


def _altas(db):
    return [(p[0], p[1], p[4]) for _, p in
            db.sql_con("INSERT INTO scintela.parado_cohorte")]


# ── la cohorte después de la largada ────────────────────────────────────────

def test_despues_de_la_largada_la_parada_nueva_no_entra(monkeypatch):
    """Asturias CAR el 08/09: cumplía la regla ese día, pero es tela que se
    estancó durante la carrera, no un saldo de la largada."""
    db, _ = _refresco(monkeypatch,
                      parados=[_parada("Asturias", "CAR"),
                               _segunda("Toper 1.80", "CAF")],
                      cohorte=[])
    assert _altas(db) == [("Toper 1.80", "CAF", "segunda")], (
        "sólo la segunda puede darse de alta después de la largada")


def test_la_que_ya_estaba_sigue_recibiendo_su_motivo(monkeypatch):
    """La regla es para la que ENTRA. Inter BLA entró el 20/08 y sigue en la
    cohorte: el refresco la sigue tocando como siempre (INSERT que no hace
    nada por el ON CONFLICT, y el UPDATE del motivo)."""
    db, _ = _refresco(
        monkeypatch,
        parados=[_parada("Inter", "BLA", 255)],
        cohorte=[{"subcategoria": "Inter", "color": "BLA",
                  "fecha_marcado": date(2026, 8, 20), "motivo": "parado"}])
    assert _altas(db) == [("Inter", "BLA", "parado")]


def test_el_dia_de_la_largada_la_parada_entra_como_siempre(monkeypatch):
    """La puerta se cierra DESPUÉS de la largada, no el mismo día: el refresco
    del 25/08 es el que armó la cohorte."""
    db, _ = _refresco(monkeypatch,
                      parados=[_parada("Asturias", "CAR")],
                      cohorte=[], hoy=date(2026, 8, 25))
    assert _altas(db) == [("Asturias", "CAR", "parado")]


def test_una_parada_sin_motivo_cuenta_como_parada_y_no_entra(monkeypatch):
    """`motivo` en None es "parado" en todos lados (`_descalifica`,
    `cuenta_el_kilo`); acá también."""
    p = _parada("Asturias", "ELE")
    p["motivo"] = None
    db, _ = _refresco(monkeypatch, parados=[p], cohorte=[])
    assert _altas(db) == []


def test_despues_de_la_largada_la_apagada_no_vuelve_sola(monkeypatch):
    """La que el 25/08 quedó afuera por reciente y hoy cumplió los 90 días es
    tela nueva que se estancó después: no vuelve."""
    db, _ = _refresco(
        monkeypatch,
        parados=[_parada("Pique Nido", "CRO")],
        cohorte=[{"subcategoria": "Pique Nido", "color": "CRO",
                  "fecha_marcado": date(2026, 8, 20), "motivo": "parado",
                  "fuera": True}])
    assert db.sql_con("SET fuera = FALSE") == [], (
        "después de la largada nada se vuelve a encender")


def test_el_dia_de_la_largada_la_apagada_si_vuelve(monkeypatch):
    db, _ = _refresco(
        monkeypatch,
        parados=[_parada("Pique Nido", "CRO")],
        cohorte=[{"subcategoria": "Pique Nido", "color": "CRO",
                  "fecha_marcado": date(2026, 8, 20), "motivo": "parado",
                  "fuera": True}],
        hoy=date(2026, 8, 25))
    assert db.sql_con("SET fuera = FALSE"), "hasta la largada vuelve sola"


# ── el puntaje de la que entra después del congelamiento ────────────────────

class _DBPuntos(_DBFalsa):
    """Puntaje ya congelado, y una tela de la cohorte sin fila."""

    def __init__(self, faltan, filas_items, congelado=True):
        super().__init__(cohorte=[])
        self.faltan = faltan
        self.filas_items = filas_items
        self.congelado = congelado

    def fetch_one(self, sql, params=None, conn=None):
        if "MIN(fijado_el)" in sql:
            return {"f": date(2026, 8, 25) if self.congelado else None}
        return {}

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT DISTINCT c.subcategoria"):
            return [{"subcategoria": t} for t in self.faltan]
        if "FROM scintela.parado_cohorte c" in s and "parado_foto" in s:
            return self.filas_items
        return super().fetch_all(sql, params, conn)


def _fila_item(sub, stock, vendido, cat="Toper"):
    return {"subcategoria": sub, "color": "X", "stock_kg": stock,
            "kg_vendidos": vendido, "categoria": cat}


def test_la_tela_que_entra_despues_recibe_su_puntaje_congelado(monkeypatch):
    """Toper 1.80: 41,5 kg parados contra 5.059 kg vendidos en 12 meses son
    0,1 meses → fácil → 1 punto. El número es el mismo que daba el default,
    pero ahora sale de la regla y queda escrito con `fijado_el`."""
    db = _DBPuntos(faltan=["Toper 1.80"],
                   filas_items=[_fila_item("Toper 1.80", 0, 21.45),
                                _fila_item("Toper 1.80", 0, 20.05, cat=None),
                                # la que ya tiene puntaje no se toca
                                _fila_item("Toper", 500, 100)])
    monkeypatch.setattr(queries, "db", db)
    monkeypatch.setattr(queries, "today_ec", lambda: date(2026, 9, 18))
    monkeypatch.setattr(asinfo_parado, "venta_por_tela",
                        lambda: {"Toper 1.80": {"kg": 5058.85, "seg": 62.45}})
    assert queries._completar_puntos("CONN") == ["Toper 1.80"]
    (sql, p), = db.sql_con("INSERT INTO scintela.parado_punto")
    assert "ON CONFLICT (subcategoria) DO NOTHING" in sql
    sub, cat, kg_base, kg_12m, seg, meses, nivel, puntos, fijado = p
    assert (sub, cat, kg_base, kg_12m, seg) == (
        "Toper 1.80", "Toper", 41.5, 5058.85, 62.45)
    assert (nivel, puntos, fijado) == (1, 1, date(2026, 9, 18))
    assert round(meses, 2) == 0.1


def test_una_tela_que_no_se_vende_entra_como_dificil(monkeypatch):
    """Sin venta en 12 meses no hay con qué dividir: es el peor caso, 10."""
    db = _DBPuntos(faltan=["Rib Spun"],
                   filas_items=[_fila_item("Rib Spun", 300, 0, "Rib")])
    monkeypatch.setattr(queries, "db", db)
    monkeypatch.setattr(queries, "today_ec", lambda: date(2026, 9, 18))
    monkeypatch.setattr(asinfo_parado, "venta_por_tela",
                        lambda: {"Toper": {"kg": 1000, "seg": 0}})
    assert queries._completar_puntos() == ["Rib Spun"]
    (_, p), = db.sql_con("INSERT INTO scintela.parado_punto")
    assert (p[5], p[6], p[7]) == (None, 3, 10)


def test_sin_venta_de_asinfo_no_escribe_nada(monkeypatch):
    """Fail-closed, igual que `_fijar_puntos`: con Metabase caído la tela
    daría "difícil" y valdría 10 por un corte de red. Se queda sin fila y el
    health la sigue cantando."""
    db = _DBPuntos(faltan=["Toper 1.80"],
                   filas_items=[_fila_item("Toper 1.80", 0, 41.5)])
    monkeypatch.setattr(queries, "db", db)
    monkeypatch.setattr(asinfo_parado, "venta_por_tela", lambda: {})
    assert queries._completar_puntos() == []
    assert db.sql_con("INSERT INTO scintela.parado_punto") == []


def test_no_toca_nada_si_no_falta_ninguna(monkeypatch):
    db = _DBPuntos(faltan=[], filas_items=[])
    monkeypatch.setattr(queries, "db", db)
    monkeypatch.setattr(asinfo_parado, "venta_por_tela",
                        lambda: (_ for _ in ()).throw(AssertionError(
                            "no tiene que ir a Asinfo si no falta nada")))
    assert queries._completar_puntos() == []


def test_antes_del_congelamiento_no_completa(monkeypatch):
    """Sin `fijado_el` el puntaje todavía es una previsualización que
    `_fijar_puntos` reescribe entero: acá no hay nada que completar."""
    db = _DBPuntos(faltan=["Toper 1.80"], filas_items=[], congelado=False)
    monkeypatch.setattr(queries, "db", db)
    assert queries._completar_puntos() == []


def test_el_refresco_completa_los_puntos_despues_de_congelar(monkeypatch):
    """`actualizar()` llama a las dos, en ese orden, dentro de la misma
    transacción."""
    import inspect
    fuente = inspect.getsource(queries.actualizar)
    assert fuente.index("_fijar_puntos(conn)") < fuente.index(
        "_completar_puntos(conn)")


def test_la_migracion_apaga_solo_la_parada_de_despues_de_la_largada():
    from pathlib import Path
    sql = Path(__file__).resolve().parents[1].joinpath(
        "migrations", "0251_despues_de_la_largada_solo_entra_segunda.sql"
    ).read_text()
    assert "SET fuera = TRUE" in sql and "DELETE" not in sql, (
        "se apaga, no se borra: la cohorte es inmutable")
    assert "motivo = 'parado'" in sql, "la segunda no se toca"
    assert "clave = 'largada'" in sql, "la fecha sale de parado_config"
