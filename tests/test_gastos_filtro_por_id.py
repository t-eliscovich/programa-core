"""`/gastos?id=<id_xgast>` tiene que mostrar EXACTAMENTE esa fila.

TMT 2026-08-07 (dueña, sobre los links del historial): *"si al clickear hay
que buscar la fila a ojo, el link no está terminado"*. Los movimientos de la
tabla `xgast` linkeaban a `/gastos` a secas — la pantalla entera, 890
movimientos sobre 520 filas distintas.

Y llegar "a mano" no era una opción tampoco:

  - el buscador `q` NO sirve de atajo: mira concepto/prov/doc/nombre y
    `CAST(g.num AS TEXT)`, pero en xgast `num` es la CATEGORÍA V1..V9, no un
    número de documento — tipear el número del gasto no lo encuentra;
  - el listado tiene un `LIMIT 500` fijo ordenado por fecha DESC, así que 36
    de las 526 filas linkeadas quedaban literalmente fuera de la página;
  - el hero suma los ÚLTIMOS 90 DÍAS por default (`resumen()`), o sea que un
    gasto más viejo se veía en la tabla con un "0 gastos" arriba.

Los tres son filtros por DEFAULT: el `WHERE` por id es lo fácil, lo que muerde
es apagarlos. Mismo patrón que /posdat (ver test_posdat_link_del_historial.py).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.gastos import queries as gq  # noqa: E402

# ── 1) la consulta ───────────────────────────────────────────────────────────


class _CapturaSQL:
    """Guarda TODAS las consultas: `buscar()`/`resumen()` pueden hacer varias
    (nombres de proveedor, existencia de columnas…) y quedarse con la última
    captura la equivocada."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def _rec(self, sql, params):
        self.queries.append((" ".join(sql.split()), params or {}))

    def fetch_all(self, sql, params=None):
        self._rec(sql, params)
        return []

    def fetch_one(self, sql, params=None):
        self._rec(sql, params)
        return {"total": 0, "saldo_pendiente": 0, "n": 0,
                "fecha_min": date(2024, 3, 9), "fecha_max": date(2024, 3, 9)}

    def principal(self) -> tuple[str, dict]:
        """La consulta que lista/suma xgast — la que recibe id_xgast."""
        for sql, params in self.queries:
            if isinstance(params, dict) and "id_xgast" in params:
                return sql, params
        raise AssertionError(
            "ninguna consulta recibió el parámetro id_xgast; se hicieron: "
            + " | ".join(q[:70] for q, _ in self.queries))


@pytest.fixture
def cap(monkeypatch):
    c = _CapturaSQL()
    monkeypatch.setattr(gq, "db", c)
    return c


def test_buscar_por_id_filtra_por_esa_fila(cap):
    gq.buscar(id_xgast=8123)
    sql, params = cap.principal()
    assert params["id_xgast"] == 8123
    assert "%(id_xgast)s IS NULL OR g.id_xgast = %(id_xgast)s" in sql


def test_el_filtro_por_id_va_en_el_where_y_no_lo_puede_tapar_el_limit(cap):
    """El `LIMIT 500` es lo que dejaba 36 filas linkeadas inalcanzables. Filtrar
    en el WHERE (antes del tope) las trae; recortar en Python DESPUÉS del LIMIT
    seguiría devolviendo vacío para los gastos más viejos."""
    gq.buscar(id_xgast=8123)
    sql, _ = cap.principal()
    assert sql.index("g.id_xgast = %(id_xgast)s") < sql.index("LIMIT")


def test_pedir_por_id_apaga_el_texto_y_el_rango_de_fechas(cap):
    """Buscar y acotar por fecha son maneras de ENCONTRAR la fila. Si ya se
    sabe cuál es, aplicarlos encima sólo puede esconderla."""
    gq.buscar(q="condor", desde="2026-08-01", hasta="2026-08-07", id_xgast=8123)
    sql, _ = cap.principal()
    assert "%(id_xgast)s IS NOT NULL OR %(q)s IS NULL" in sql
    assert ("%(id_xgast)s IS NOT NULL OR %(desde)s::date IS NULL "
            "OR g.fecha >= %(desde)s::date") in sql
    assert ("%(id_xgast)s IS NOT NULL OR %(hasta)s::date IS NULL "
            "OR g.fecha <= %(hasta)s::date") in sql


def test_sin_id_la_lista_no_cambia(cap):
    """El caso normal sigue igual: id_xgast viaja NULL y no filtra nada."""
    gq.buscar()
    _, params = cap.principal()
    assert params["id_xgast"] is None


def test_un_id_basura_no_llega_al_sql_como_texto(cap):
    """`buscar()` castea a int: nada que no sea un número entra en el WHERE."""
    with pytest.raises((TypeError, ValueError)):
        gq.buscar(id_xgast="8123 OR 1=1")


# ── 2) el hero: los MISMOS filtros que la tabla ──────────────────────────────


def test_el_resumen_recibe_el_mismo_id_que_la_lista(cap):
    """Sin esto, el link a un gasto mostraba el total de TODA la lista arriba
    de una sola fila. Ese bug ya pasó en /posdat (item 18, 2026-05-19)."""
    gq.resumen(id_xgast=8123)
    sql, params = cap.principal()
    assert params["id_xgast"] == 8123
    assert "%(id_xgast)s IS NULL OR id_xgast = %(id_xgast)s" in sql


def test_pedir_por_id_apaga_la_ventana_de_90_dias_del_hero(cap):
    """`resumen()` arranca en HOY−90 días cuando no le pasan fechas. Un gasto
    más viejo que eso quedaba fuera del BETWEEN: la tabla mostraba la fila y
    el KPI de arriba decía "0 gastos"."""
    gq.resumen(id_xgast=8123)
    sql, _ = cap.principal()
    assert ("%(id_xgast)s IS NOT NULL OR fecha BETWEEN %(desde)s::date "
            "AND %(hasta)s::date") in sql


def test_con_id_el_periodo_del_hero_es_el_del_gasto(cap):
    """El KPI "Periodo" describe lo que se está viendo. Mostrar HOY−90 días
    arriba de un gasto de 2024 es una afirmación falsa (y sin cartel que la
    aclare, que es justo lo que la dueña no quiere)."""
    r = gq.resumen(id_xgast=8123)
    assert r["desde"] == "2024-03-09"
    assert r["hasta"] == "2024-03-09"


def test_sin_id_el_periodo_sigue_siendo_la_ventana_pedida(cap):
    r = gq.resumen(desde="2026-07-01", hasta="2026-07-31")
    assert (r["desde"], r["hasta"]) == ("2026-07-01", "2026-07-31")


# ── 3) la vista ─────────────────────────────────────────────────────────────


@pytest.fixture
def app_logueada(app):
    """La app real con una sesión de Accionista (wildcard de permisos).

    Se registra DESPUÉS de create_app() a propósito: `app.py` importa
    `load_logged_in_user` en el import, así que pisarlo no sirve una vez que el
    módulo ya está cargado."""
    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    return app


@pytest.fixture
def contexto(monkeypatch):
    """Captura el contexto con el que la vista renderiza el template — así se
    puede mirar la matriz V1..V9, que se calcula en la vista y hoy no se
    imprime en /gastos (vive en la solapa "Reporte mensual")."""
    from modules.gastos import views as gv

    visto: dict = {}

    def fake_render(_tpl, **ctx):
        visto.update(ctx)
        return "ok"

    monkeypatch.setattr(gv, "render_template", fake_render)
    return visto


def test_la_vista_propaga_el_id_a_la_lista_y_al_resumen(
        app_logueada, monkeypatch, contexto):
    from modules.gastos import views as gv

    vistos = {}

    def fake_buscar(*a, **kw):
        vistos["buscar"] = kw.get("id_xgast")
        return []

    def fake_resumen(*a, **kw):
        vistos["resumen"] = kw.get("id_xgast")
        return {}

    monkeypatch.setattr(gv.queries, "buscar", fake_buscar)
    monkeypatch.setattr(gv.queries, "resumen", fake_resumen)

    r = app_logueada.test_client().get("/gastos?id=8123")
    assert r.status_code == 200
    assert vistos["buscar"] == 8123
    assert vistos["resumen"] == 8123
    assert contexto["id_xgast"] == 8123


def test_los_totales_y_la_matriz_se_arman_sobre_la_fila_pedida(
        app_logueada, monkeypatch, contexto):
    """Los TOTALES VISIBLES y la matriz 3×3 salen de `filas`, que ya viene
    filtrada. La categoría (`num`, V1..V9) decide en qué celda vive la fila:
    num=5 → servicios/tintorería. Si mañana la matriz saliera de su propia
    consulta, el id tendría que llegar TAMBIÉN ahí."""
    from modules.gastos import views as gv

    monkeypatch.setattr(gv.queries, "buscar", lambda *a, **kw: [
        {"id_xgast": 8123, "num": 5, "importe": 1234.5, "saldo": 0},
    ])
    monkeypatch.setattr(gv.queries, "resumen", lambda *a, **kw: {})

    app_logueada.test_client().get("/gastos?id=8123")
    assert contexto["total_importe"] == pytest.approx(1234.5)
    assert contexto["matriz"]["servicios"]["tin"] == pytest.approx(1234.5)
    assert contexto["suma_v_total"] == pytest.approx(1234.5)
    # y NINGUNA otra celda quedó cargada con el resto de la lista
    otras = sum(
        v for fila, cols in contexto["matriz"].items() for col, v in cols.items()
        if (fila, col) != ("servicios", "tin")
    )
    assert otras == 0


def test_un_id_que_no_es_un_numero_se_ignora(app_logueada, monkeypatch, contexto):
    """?id=borrar-todo no puede tirar un 500 ni colarse en el SQL."""
    from modules.gastos import views as gv

    vistos = {}

    def fake_buscar(*a, **kw):
        vistos["id"] = kw.get("id_xgast")
        return []

    monkeypatch.setattr(gv.queries, "buscar", fake_buscar)
    monkeypatch.setattr(gv.queries, "resumen", lambda *a, **kw: {})

    r = app_logueada.test_client().get("/gastos?id=borrar-todo")
    assert r.status_code == 200
    assert vistos["id"] is None


def test_la_pantalla_filtrada_no_agrega_ningun_cartel(app_logueada, monkeypatch):
    """DECISIÓN DE LA DUEÑA (2026-08-07): *"sin el cartel"*.

    Este test está dado vuelta a propósito, no borrado: si mañana alguien mete
    un aviso «mostrando un solo gasto · ver todos», que falle acá y lea la
    decisión antes de re-discutirla. La pantalla filtrada se ve igual que
    siempre, con una fila."""
    from modules.gastos import views as gv

    monkeypatch.setattr(gv.queries, "buscar", lambda *a, **kw: [])
    monkeypatch.setattr(gv.queries, "resumen", lambda *a, **kw: {})

    html = app_logueada.test_client().get("/gastos?id=8123").get_data(as_text=True)
    for cartel in ("un solo gasto", "Mostrando un solo", "ver todos"):
        assert cartel not in html
    assert html.count("<table") >= 1      # la grilla de siempre, sin adornos
