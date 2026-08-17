"""Tests de la sección Análisis → Lo parado.

Lo que se prueba es lo que se puede romper sin síntoma:

  · que un ítem VENDIDO siga en la lista (el pedido de la dueña);
  · que la cohorte no se pise (kg_al_marcar y fecha son de la primera vez);
  · que Metabase caído NO vacíe la pantalla (fail-closed);
  · que la sección sea invisible para quien no tiene el permiso.
"""

from __future__ import annotations

from datetime import date

import pytest

from modules.analisis import asinfo_parado, queries

# ── El invariante que pidió la dueña ────────────────────────────────────────

def test_un_item_vendido_sigue_en_la_lista(monkeypatch):
    """`items()` sale de la COHORTE, no de la foto: si la foto ya no lo tiene
    (se vendió y dejó de estar parado), la fila tiene que seguir."""
    sql_visto = {}

    def fake_fetch_all(sql, params=None, conn=None):
        sql_visto["sql"] = " ".join(sql.split())
        return []

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)
    queries.items()
    s = sql_visto["sql"]
    assert "FROM scintela.parado_cohorte c" in s, "la lista tiene que salir de la cohorte"
    assert "LEFT JOIN scintela.parado_foto" in s, (
        "si el JOIN fuera INNER, un ítem vendido desaparecería — que es "
        "exactamente lo que la dueña pidió que NO pase"
    )


def test_estado_marca_lo_que_se_movio(monkeypatch):
    """resuelto / empezó a moverse / sigue parado salen del SQL; acá se fija
    que el resumen los cuente bien."""
    filas = [
        {"stock_kg": 100, "kg_vendidos": 0, "clientes": 3},
        {"stock_kg": 50, "kg_vendidos": 20, "clientes": 5},
        {"stock_kg": 0, "kg_vendidos": 80, "clientes": 0},
    ]
    r = queries.resumen(filas)
    assert r["n_items"] == 3
    assert r["kg"] == 150
    assert r["kg_vendidos"] == 100
    assert r["movidos"] == 2
    assert r["sin_pista"] == 1
    assert r["kg_sin_pista"] == 0


# ── Fail-closed contra Metabase ─────────────────────────────────────────────

def test_metabase_caido_no_vacia_la_pantalla(monkeypatch):
    """`fetch_dataset` devuelve [] tanto si no hay filas como si Metabase se
    cayó. Tomar el segundo caso por "no hay parados" borraría la pantalla y
    parecería una buena noticia."""
    monkeypatch.setattr(
        asinfo_parado.metabase_client, "fetch_dataset_estado",
        lambda *a, **k: ([], False))
    with pytest.raises(RuntimeError, match="Metabase no contestó"):
        asinfo_parado.parados()


def test_sin_filas_pero_metabase_ok_devuelve_lista_vacia(monkeypatch):
    monkeypatch.setattr(
        asinfo_parado.metabase_client, "fetch_dataset_estado",
        lambda *a, **k: ([], True))
    assert asinfo_parado.parados() == []


# ── El mapeo de vendedores ──────────────────────────────────────────────────

def test_vendedor_con_espacios_igual_mapea(monkeypatch):
    """⚠ Asinfo devuelve "Ramirez Edgar " con espacio al final. Sin `.strip()`
    el mapeo falla en silencio y TODOS quedan como mostrador — que es un
    estado válido, y por eso no se nota."""
    monkeypatch.setattr(
        asinfo_parado.metabase_client, "fetch_dataset_estado",
        lambda *a, **k: ([{"vendedor": "Ramirez Edgar ", "subcategoria": "X",
                           "codigo": "ABC", "anio": 2026}], True))
    assert asinfo_parado.llamados()[0]["vend_pc"] == "EDG"


def test_vendedor_desconocido_queda_sin_codigo(monkeypatch):
    monkeypatch.setattr(
        asinfo_parado.metabase_client, "fetch_dataset_estado",
        lambda *a, **k: ([{"vendedor": "Cía. Ltda. Intela", "subcategoria": "X",
                           "codigo": "ABC", "anio": 2026}], True))
    assert asinfo_parado.llamados()[0]["vend_pc"] is None


# ── Las definiciones que no se pueden aflojar ───────────────────────────────

def test_el_consumidor_final_no_es_candidato():
    """VPM es el mostrador. Sin excluirlo, telas con 1.700 kg parados mostraban
    un único "candidato" que compró 3 kg y no existe."""
    assert "<> 'VPM'" in asinfo_parado.SQL_LLAMADOS


def test_las_notas_de_credito_no_netean():
    """Sólo documentos de venta (7, 251) y cantidad > 0: "vendió 0 kg en 12
    meses" tiene que significar que no salió, no que salió y volvió."""
    for sql in (asinfo_parado.SQL_PARADOS, asinfo_parado.SQL_LLAMADOS):
        assert "fc.id_documento IN (7, 251)" in sql
        assert "20, 451" not in sql
        assert "dfc.cantidad > 0" in sql


def test_los_llamados_son_por_tela_y_no_por_tela_color():
    """El color no entra en la llamada: quien compra Kiana Forro compra Kiana
    Forro y el color se negocia. Agrupar por tela × color deja listas de uno."""
    assert "GROUP BY pr.nombre_subcategoria_producto, YEAR(fc.fecha), fc.id_empresa" \
        in asinfo_parado.SQL_LLAMADOS


def test_la_fecha_de_venta_se_compara_como_fecha():
    """Metabase devuelve las fechas como texto ISO y Postgres como `date`.
    Comparar str contra date explota; comparar str contra str compara
    alfabéticamente y a veces acierta, que es peor."""
    assert queries._fecha("2026-08-13T00:00:00Z") == date(2026, 8, 13)
    assert queries._fecha(date(2026, 8, 13)) == date(2026, 8, 13)


# ── El candado ──────────────────────────────────────────────────────────────

def test_todas_las_rutas_piden_el_permiso():
    from modules.analisis import views
    for regla in ("inicio", "parado", "parado_actualizar"):
        f = getattr(views, regla)
        assert getattr(f, "_permiso", None) == "analisis.ver", (
            f"{regla} tiene que estar gateada: si no, la sección aparece "
            f"para cualquiera que sepa la URL")


def test_el_menu_no_ofrece_pantallas_que_no_existen(app):
    """Los links de esta app son strings hardcodeados: un link a una ruta que no
    existe NO se ve desde el código, sólo como 404 al clickear. Por eso el test
    no compara contra una lista escrita a mano —que se desactualiza igual— sino
    contra el `url_map` real."""
    from modules.analisis import views

    rutas = {r.rule for r in app.url_map.iter_rules()}
    for m in views.MENU:
        if m["listo"]:
            assert m["url"] in rutas, (
                f"el menú ofrece {m['url']} y esa ruta no existe: es un 404 "
                f"que no se ve leyendo el código")


def test_todos_los_links_internos_de_las_plantillas_existen(app):
    """Ídem para los links escritos a mano DENTRO de las pantallas de la
    sección — el botón «Ver por cliente», el «← Ver por tela», el form de
    actualizar."""
    import re
    from pathlib import Path

    rutas = {r.rule for r in app.url_map.iter_rules()}
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    for archivo in carpeta.glob("*.html"):
        html = archivo.read_text(encoding="utf-8")
        for url in re.findall(r'(?:href|action)="(/analisis[^"?{]*)', html):
            assert url in rutas, f"{archivo.name} linkea a {url}, que no existe"


# ── Lo que se rompió en producción y no puede volver a pasar ────────────────

def test_el_resumen_no_usa_una_clave_que_jinja_confunde():
    """⚠ En Jinja `resumen.items` devuelve el MÉTODO del diccionario, no el
    dato: la tarjeta imprimió "<built-in method items of dict object at 0x…>"
    en producción. No dio error — renderizó 200 con un texto absurdo donde iba
    la cifra. Ninguna clave del resumen puede chocar con un método de dict."""
    r = queries.resumen([])
    for clave in r:
        assert not hasattr({}, clave), (
            f"la clave '{clave}' choca con dict.{clave} y Jinja va a resolver "
            f"el método antes que el dato")


def test_la_plantilla_no_pide_claves_que_el_resumen_no_tiene():
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    pedidas = set(re.findall(r"resumen\.(\w+)", html))
    assert pedidas <= set(queries.resumen([])), (
        f"la plantilla pide {pedidas - set(queries.resumen([]))}, que no existe")


# ── El tercer escalón: improbable ───────────────────────────────────────────

def test_los_candidatos_no_se_cortan_en_dos_anios():
    """Dueña 17/08/2026: "ponemelos como improbable pero la última fecha aunque
    sea de 2024". Un cliente de 2023 no es un buen candidato, pero es más que
    un renglón vacío."""
    assert "YEAR(GETDATE()) - 1" not in asinfo_parado.SQL_LLAMADOS, (
        "el corte de dos años dejaba 7 telas sin una sola pista teniendo el "
        "dato a mano")
    assert "MAX(anio) AS anio FROM compras" in asinfo_parado.SQL_LLAMADOS, (
        "el año que manda es el ÚLTIMO con compras, sea cual sea")


def test_improbable_se_marca_distinto_de_solo_el_anio_pasado():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert "improbable · {{ f.anio_pista }}" in html
    assert "ahora_anio - 1" in html, (
        "sin el -1, el año pasado y 2023 se pintan igual y la palabra "
        "'improbable' deja de querer decir algo")


# ── La hoja del vendedor ────────────────────────────────────────────────────

def _filas_falsas():
    return [
        {"codigo_cli": "AAA", "nombre": "Cliente Grande", "provincia": "GUAYAS",
         "vend_pc": "FL1", "subcategoria": "Kiana", "kg_cliente": 100,
         "ultima_compra": None, "anio": 2026, "kg_parado": 900,
         "colores_parados": "CAR"},
        {"codigo_cli": "BBB", "nombre": "Cliente Chico", "provincia": "AZUAY",
         "vend_pc": "FL1", "subcategoria": "Microfibra", "kg_cliente": 10,
         "ultima_compra": None, "anio": 2026, "kg_parado": 100,
         "colores_parados": "FRE"},
        {"codigo_cli": "CCC", "nombre": "Cliente Viejo", "provincia": "AZUAY",
         "vend_pc": "FL1", "subcategoria": "Jersey Fancy", "kg_cliente": 50,
         "ultima_compra": None, "anio": 2023, "kg_parado": 500,
         "colores_parados": "FNB"},
    ]


def test_la_hoja_ordena_por_oportunidad_por_defecto(monkeypatch):
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: _filas_falsas())
    r = queries.por_cliente()
    assert [c["codigo"] for c in r["clientes"]] == ["AAA", "BBB"], (
        "la hoja existe para decidir por quién empezar: si el vendedor corta a "
        "la mitad, que haya cortado por lo chico")


def test_los_improbables_van_al_final_y_aparte(monkeypatch):
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: _filas_falsas())
    r = queries.por_cliente()
    assert [c["codigo"] for c in r["improbables"]] == ["CCC"]
    assert "CCC" not in [c["codigo"] for c in r["clientes"]], (
        "mezclado ensucia una lista que el vendedor tiene que poder creer")


def test_un_cliente_con_una_tela_vieja_y_una_nueva_no_es_improbable(monkeypatch):
    filas = _filas_falsas()
    filas[2]["codigo_cli"] = "AAA"          # la vieja es del cliente grande
    filas[2]["nombre"] = "Cliente Grande"
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: filas)
    r = queries.por_cliente()
    assert [c["codigo"] for c in r["improbables"]] == []
    grande = next(c for c in r["clientes"] if c["codigo"] == "AAA")
    assert len(grande["telas"]) == 2


def test_el_orden_alfabetico_y_el_de_provincia_existen(monkeypatch):
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: _filas_falsas())
    assert [c["codigo"] for c in queries.por_cliente(orden="codigo")["clientes"]] \
        == ["AAA", "BBB"]
    assert [c["provincia"] for c in queries.por_cliente(orden="provincia")["clientes"]] \
        == ["AZUAY", "GUAYAS"]


def test_un_orden_inventado_no_rompe_la_pantalla(monkeypatch):
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: _filas_falsas())
    r = queries.por_cliente(orden="loquesea")
    assert [c["codigo"] for c in r["clientes"]] == ["AAA", "BBB"]


def test_la_hoja_avisa_que_los_kg_no_se_suman():
    """Una misma tela parada aparece en la lista de todos los que la compran:
    sumar la columna entre clientes da mucho más que el stock real."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado_clientes.html").read_text(encoding="utf-8")
    assert "no se suman entre clientes" in html


def test_la_hoja_se_imprime_con_la_misma_plantilla():
    """⚠ En /mi-cartera la oficina y el portal imprimieron órdenes distintos
    ocho días sin síntoma, por tener dos caminos para el mismo papel."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado_clientes.html").read_text(encoding="utf-8")
    assert "@media print" in html and "window.print()" in html
