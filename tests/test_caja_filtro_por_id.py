"""El link del historial a un movimiento de caja tiene que caer EN ESA FILA.

TMT 2026-08-07 (dueña): *"si al clickear hay que buscar la fila a ojo, el link
no está terminado"*.

El historial linkeaba a `/caja#id-<id_caja>`. Ese ancla NO EXISTE en ningún
template de la app — la única aparición de la cadena `#id-` en todo el repo era
el código que la generaba —, así que el link era literalmente `/caja`: la lista
completa, ordenada por fecha DESC y paginada de a 500, con 603 movimientos del
historial apuntando a 467 filas distintas.

Lo que esconde la fila NO es el WHERE (eso es lo fácil), son:

1. Los filtros que ya estaban puestos (concepto/tipo, desde, hasta): el link a
   un movimiento de marzo con la pantalla filtrada por este mes muestra vacío.
2. La PAGINACIÓN: 52 de las 466 filas linkeadas caen más allá de la página 1, y
   el paginador reenvía TODOS los args, así que un `pagina=3` viejo en la URL
   se come la única fila.
3. Los agregados: el hero cuenta "· N movimientos" del universo entero. Sin
   propagarle el id, decía 603 arriba de UNA fila (el mismo bug que ya mordió
   en /posdat — item 18, 2026-05-19).

Y lo que NO se toca: el SALDO. El KPI del hero es la plata que hay en la caja,
no el subtotal de las filas visibles.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.caja import queries as cq  # noqa: E402

# ── infra ────────────────────────────────────────────────────────────────────

class _CapturaSQL:
    """Guarda TODAS las consultas: `movimientos()`/`resumen()` pueden hacer
    varias y quedarse con la última captura la equivocada."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def _rec(self, sql, params):
        self.queries.append((" ".join(sql.split()), params or {}))

    def fetch_all(self, sql, params=None):
        self._rec(sql, params)
        return []

    def fetch_one(self, sql, params=None):
        self._rec(sql, params)
        return {}

    def principal(self) -> tuple[str, dict]:
        """La consulta que lista/suma caja — la que recibe `id_caja`."""
        for sql, params in self.queries:
            if isinstance(params, dict) and "id_caja" in params:
                return sql, params
        raise AssertionError(
            "ninguna consulta recibió el parámetro id_caja; se hicieron: "
            + " | ".join(q[:70] for q, _ in self.queries))


@pytest.fixture
def cap(monkeypatch):
    c = _CapturaSQL()
    monkeypatch.setattr(cq, "db", c)
    return c


# ── 1) la lista ──────────────────────────────────────────────────────────────

def test_movimientos_por_id_filtra_por_esa_fila(cap):
    cq.movimientos(id_caja=1234)
    sql, params = cap.principal()
    assert params["id_caja"] == 1234
    assert "%(id_caja)s IS NULL OR c.id_caja = %(id_caja)s" in sql


def test_pedir_por_id_apaga_las_fechas_y_el_texto(cap):
    """La pantalla se queda con los filtros de la búsqueda anterior. Si el link
    del historial no los apaga, un movimiento de marzo abierto con `desde` en
    agosto muestra una lista vacía y parece que apunta a la nada."""
    cq.movimientos(desde="2026-08-01", hasta="2026-08-31", q="PICH", id_caja=1234)
    sql, _ = cap.principal()
    assert "%(id_caja)s IS NOT NULL OR %(desde)s::date IS NULL" in sql
    assert "%(id_caja)s IS NOT NULL OR %(hasta)s::date IS NULL" in sql
    assert "%(id_caja)s IS NOT NULL OR %(q)s IS NULL" in sql


def test_pedir_por_id_ignora_el_offset_de_la_paginacion(cap):
    """La caja pagina de a 500 con ORDER BY fecha DESC. Con el offset puesto,
    la fila vieja sigue estando en la página N y el link no muestra nada."""
    cq.movimientos(limite=501, offset=1000, id_caja=1234)
    _, params = cap.principal()
    assert params["offset"] == 0


def test_sin_id_la_lista_no_cambia(cap):
    """El caso normal sigue igual: `id_caja` viaja NULL, filtra y pagina."""
    cq.movimientos(desde="2026-08-01", q="PICH", offset=500)
    _, params = cap.principal()
    assert params["id_caja"] is None
    assert params["offset"] == 500       # la paginación de siempre, intacta


# ── 2) los agregados ─────────────────────────────────────────────────────────

def test_el_contador_del_hero_recibe_el_mismo_id_que_la_lista(cap):
    """"· N movimientos" tiene que coincidir con las filas visibles."""
    cq.resumen(id_caja=1234)
    sql, params = cap.principal()
    assert params["id_caja"] == 1234
    assert "%(id_caja)s IS NULL OR f.id_caja = %(id_caja)s" in sql


def test_el_saldo_del_hero_NO_se_filtra_por_id(cap):
    """El KPI del hero es la plata que HAY en la caja, no el subtotal de lo
    que se ve. Si `ingresos`/`egresos`/`opening` se filtraran, abrir un
    movimiento de $150 diría "Caja: $ 150" — mentira, y encima dispararía en
    falso el warning de drift (saldo_ultimo vs opening + ΣE − ΣS)."""
    cq.resumen(id_caja=1234)
    sql, _ = cap.principal()
    # El único agregado que menciona el id es el contador; las sumas de plata
    # se calculan sobre el FROM global, sin condición.
    cuerpo = sql.split("AS n_movimientos")[0]
    for columna in ("AS ingresos", "AS egresos", "AS saldo_derivado",
                    "AS saldo_ultimo", "AS opening"):
        assert columna in cuerpo, f"{columna} se movió después del contador"
    # Ojo: `id_caja` aparece como COLUMNA en los ORDER BY de los subselects
    # de saldo_ultimo/opening. Lo que no puede aparecer es el PLACEHOLDER.
    antes_del_contador = sql.split("(SELECT COUNT(*)")[0]
    assert "%(id_caja)s" not in antes_del_contador
    assert sql.count("%(id_caja)s") == 2      # las dos del contador y nada más


# ── 3) la pantalla ───────────────────────────────────────────────────────────

@pytest.fixture
def app_logueada(app):
    """La app real con sesión de Accionista (wildcard de permisos).

    Se registra DESPUÉS de create_app() a propósito: `app.py` importa
    `load_logged_in_user` en el import, así que pisarlo no sirve una vez que
    el módulo ya está cargado (pasa en la suite completa, no corriendo el
    archivo solo).
    """
    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    return app


@pytest.fixture
def vista(monkeypatch):
    """Neutraliza la base detrás de /caja y devuelve lo que vio la query."""
    from modules.caja import views as cv
    from modules.gastos import queries as gq

    visto: dict = {}

    def fake_movimientos(desde=None, hasta=None, q="", limite=500, offset=0,
                         id_caja=None):
        visto["id_caja"] = id_caja
        visto["offset"] = offset
        return []

    def fake_resumen(id_caja=None):
        visto["resumen_id"] = id_caja
        return {}

    monkeypatch.setattr(cv.queries, "movimientos", fake_movimientos)
    monkeypatch.setattr(cv.queries, "resumen", fake_resumen)
    monkeypatch.setattr(cv.queries, "saldo_actual", lambda: 0.0)
    monkeypatch.setattr(cv.queries, "recomputar_saldos", lambda: {})
    monkeypatch.setattr(gq, "caja_egresos_sin_clasificar", lambda **kw: [])
    return visto


def test_la_url_del_historial_deja_una_sola_fila(app_logueada, vista):
    r = app_logueada.test_client().get("/caja?id=1234")
    assert r.status_code == 200
    assert vista["id_caja"] == 1234


def test_un_pagina_viejo_en_la_url_no_se_come_la_fila(app_logueada, vista):
    """El paginador reenvía TODOS los args de la URL. Si el `pagina=3` de la
    búsqueda anterior sobrevive, el offset (2×500) saltea la única fila."""
    r = app_logueada.test_client().get("/caja?id=1234&pagina=3")
    assert r.status_code == 200
    assert vista["offset"] == 0


def test_el_contador_del_hero_llega_filtrado_a_la_pantalla(app_logueada, vista):
    app_logueada.test_client().get("/caja?id=1234")
    assert vista["resumen_id"] == 1234


def test_un_id_que_no_es_un_numero_se_ignora(app_logueada, vista):
    """?id=borrar-todo no puede tirar un 500 ni colarse en el SQL."""
    r = app_logueada.test_client().get("/caja?id=borrar-todo")
    assert r.status_code == 200
    assert vista["id_caja"] is None


def test_la_pantalla_filtrada_no_agrega_ningun_cartel(app_logueada, vista):
    """DECISIÓN DE LA DUEÑA (2026-08-07, sobre el mismo cambio en /posdat):
    *"sin el cartel pero el resto perfecto"*.

    Este test está DADO VUELTA a propósito, no borrado: si mañana alguien
    vuelve a meter el aviso «mostrando una sola fila», que falle acá y lea
    esta decisión antes de re-discutirla."""
    html = app_logueada.test_client().get("/caja?id=1234").get_data(as_text=True)
    for cartel in ("Mostrando un solo", "Mostrando una sola", "ver todos"):
        assert cartel not in html
    assert html.count("<table") >= 1     # la grilla de siempre, sin adornos


def test_el_banner_de_sin_clasificar_no_manda_a_botones_que_no_estan(
        app_logueada, monkeypatch):
    """El banner dice "N egresos de este mes sin clasificar — buscá los botones
    Clasificar en la lista abajo". Con UNA fila esos botones no están: el
    contador se calcula sobre lo visible y con 0 el banner no se renderiza."""
    from modules.caja import views as cv
    from modules.gastos import queries as gq

    monkeypatch.setattr(cv.queries, "movimientos", lambda *a, **kw: [])
    monkeypatch.setattr(cv.queries, "resumen", lambda **kw: {})
    monkeypatch.setattr(cv.queries, "saldo_actual", lambda: 0.0)
    monkeypatch.setattr(cv.queries, "recomputar_saldos", lambda: {})
    # 3 egresos del mes sin clasificar; NINGUNO es la fila pedida.
    monkeypatch.setattr(gq, "caja_egresos_sin_clasificar",
                        lambda **kw: [{"id_caja": i} for i in (11, 22, 33)])
    cli = app_logueada.test_client()

    sin_filtro = cli.get("/caja").get_data(as_text=True)
    assert "3 egreso(s)" in sin_filtro          # la pantalla de siempre

    filtrado = cli.get("/caja?id=1234").get_data(as_text=True)
    assert "egreso(s)" not in filtrado
