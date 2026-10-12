"""Costo por orden desde formulas (12/10/2026): kg + ajustes JSONB y A44 x0,1,
igual que formulas_app. Si estos fragmentos cambian, el costo de tintorería
deja de coincidir con la pantalla Costos de formulas."""
from modules.tintura import service


def test_sql_kg_linea_suma_ajustes():
    s = service.sql_kg_linea("ol")
    assert s.startswith("(ol.cantidad_kg + ")
    assert "ol.ajustes" in s and "e->>'kg'" in s


def test_sql_kg_linea_alias():
    assert "x.cantidad_kg" in service.sql_kg_linea("x")


def test_sql_factor_a44():
    s = service.sql_factor_a44("p")
    assert "p.num_visible = 44" in s and "THEN 0.1 ELSE 1.0" in s
    assert "q.num_visible" in service.sql_factor_a44("q")


def test_costos_por_orden_usan_ajustes_y_a44():
    import inspect
    src = inspect.getsource(service)
    # Las dos queries de costo por orden usan los dos fragmentos.
    assert src.count("{sql_kg_linea('ol')}") >= 2
    assert src.count("{sql_factor_a44('p')}") >= 2
