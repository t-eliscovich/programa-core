"""Menú de Inventario y de Tintorería con pestañas (Tamara 2026-10-01).

*"Llamamos a todo Inventario y distintas tabs ... hay demasiados en la
izquierda. Producción tintorería queda con lo que está y un tab para el stock
de químicos."*"""
from __future__ import annotations

import re
from pathlib import Path

INVENTARIO = ("stock_asinfo.consumo_hilado", "importaciones.lista",
              "tejeduria_asinfo.tab", "terminado_asinfo.tab",
              "inventario_rotativo.lista")


def _seccion_stock(app) -> str:
    base = (Path(app.root_path) / "templates" / "base.html").read_text(encoding="utf-8")
    i = base.index('data-key="stock"')
    return base[i:base.index("</details>", i)]


def _links(seccion: str) -> list[str]:
    sin_comentarios = re.sub(r"\{#.*?#\}", "", seccion, flags=re.S)
    return re.findall(r"nav_link\('([^']+)'", sin_comentarios)


def test_el_menu_tiene_una_entrada_por_seccion(app):
    links = _links(_seccion_stock(app))
    assert links == ["informes.flujo_produccion", "pedidos.lista",
                     "stock_asinfo.fabricacion_tc",
                     "comparativa_tintoreria.comparativa_tintoreria"]


def test_inventario_queda_prendido_en_todas_sus_pestanas(app):
    sec = _seccion_stock(app)
    for ep in INVENTARIO:
        assert f"'{ep}'" in sec          # en el `tambien=` de Inventario
    assert "tambien=('stock_asinfo.quimicos',)" in sec


def test_cada_pantalla_muestra_sus_pestanas(app):
    root = Path(app.root_path) / "modules"
    casos = {
        "stock_asinfo/templates/stock_asinfo/fabricacion.html": "nav_inventario('stock_asinfo.fabricacion_tc')",
        "stock_asinfo/templates/stock_asinfo/consumo_hilado.html": "nav_inventario('stock_asinfo.consumo_hilado')",
        "importaciones/templates/importaciones/lista.html": "nav_inventario('importaciones.lista')",
        "tejeduria_asinfo/templates/tejeduria_asinfo/tab.html": "nav_inventario('tejeduria_asinfo.tab')",
        "terminado_asinfo/templates/terminado_asinfo/tab.html": "nav_inventario('terminado_asinfo.tab')",
        "inventario_rotativo/templates/inventario_rotativo/lista.html": "nav_inventario('inventario_rotativo.lista')",
        "stock_asinfo/templates/stock_asinfo/quimicos.html": "nav_tintoreria('stock_asinfo.quimicos')",
        "comparativa_tintoreria/templates/comparativa_tintoreria/index.html":
            "nav_tintoreria('comparativa_tintoreria.comparativa_tintoreria')",
    }
    for tpl, llamada in casos.items():
        assert llamada in (root / tpl).read_text(encoding="utf-8"), tpl


def test_las_pestanas_se_ven_en_la_pantalla(app, fake_db):
    from unittest.mock import patch

    from modules.terminado_asinfo import service as term
    rid = fake_db.add_role("S", ["stock.ver"])
    uid = fake_db.add_user("s", b"x", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    with patch.object(term, "resumen", return_value={"disponible": False}):
        h = c.get("/produccion-terminado-asinfo").get_data(as_text=True)
    for lbl in ("Inventario", "Consumo de hilado", "Ingreso de hilado", "Terminado", "Rotación"):
        assert lbl in h
    # sin tejeduria.ver no aparece la pestaña (sería un 404)
    assert 'href="/produccion-tejeduria-asinfo"' not in h


def test_fuentes_y_usos_es_pestana_de_resultados(app):
    root = Path(app.root_path)
    base = (root / "templates" / "base.html").read_text(encoding="utf-8")
    sin_comentarios = re.sub(r"\{#.*?#\}", "", base, flags=re.S)
    assert "nav_link('informes.fuentes_y_usos'" not in sin_comentarios
    ui = (root / "templates" / "_ui.html").read_text(encoding="utf-8")
    i = ui.index("macro nav_resultados")
    assert "('fuentes_y_usos', 'Fuentes y usos')" in ui[i:ui.index("endmacro", i)]
    tpl = (root / "modules" / "informes" / "templates" / "informes" / "fuentes_usos.html").read_text(encoding="utf-8")
    assert "nav_resultados('fuentes_y_usos')" in tpl


def test_flujo_produccion_no_lleva_las_pestanas_de_resultados():
    """Tamara 01/10/2026: "¿por qué aparece ahí arriba Resultados?" — el
    Flujo de producción está en Producción y stocks, no en Resultados."""
    from pathlib import Path
    tpl = Path("modules/informes/templates/informes/flujo_produccion.html").read_text()
    assert "nav_resultados" not in tpl


def test_ordenes_de_tintura_siempre_visible_en_pedidos():
    from pathlib import Path
    tpl = Path("modules/pedidos/templates/pedidos/lista.html").read_text()
    i = tpl.index("url_for('pedidos.ordenes')")
    assert "{% if disponible and categorias %}" not in tpl[tpl.rindex('<div class="acciones', 0, i):i]
