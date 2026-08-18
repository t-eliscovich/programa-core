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
        {"stock_kg": 100, "kg_vendidos": 0, "clientes": 3, "kg_segunda": 0},
        {"stock_kg": 50, "kg_vendidos": 20, "clientes": 5, "kg_segunda": 12},
        {"stock_kg": 0, "kg_vendidos": 80, "clientes": 0, "kg_segunda": 0},
    ]
    r = queries.resumen(filas)
    assert r["n_items"] == 3
    assert r["kg"] == 150
    assert r["kg_vendidos"] == 100
    assert r["movidos"] == 2
    assert r["sin_pista"] == 1
    assert r["kg_sin_pista"] == 0
    assert r["kg_segunda"] == 12
    assert r["n_segunda"] == 1


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

def test_ningun_diccionario_que_va_a_un_template_usa_claves_de_dict():
    """⚠ En Jinja `x.items` devuelve el MÉTODO del diccionario, no el dato: la
    pantalla imprime "<built-in method items of dict object at 0x…>" donde va
    la cifra. No da error — renderiza 200 con un texto absurdo.

    Pasó DOS veces: primero en la tarjeta "Parado hoy" y después, con el test
    ya escrito pero mirando sólo `resumen()`, en el resumen por grupo. Por eso
    ahora el test recorre todas las funciones que arman diccionarios para un
    template, y hay que agregarlas acá al crearlas."""
    filas = [{"stock_kg": 10, "kg_vendidos": 0, "clientes": 1, "kg_segunda": 0,
              "categoria": "Jersey", "subcategoria": "Jersey 3"}]
    candidatos = [queries.resumen(filas)] + queries.por_grupo(filas)
    for d in candidatos:
        for clave in d:
            assert not hasattr({}, clave), (
                f"la clave '{clave}' choca con dict.{clave} y Jinja va a "
                f"resolver el método antes que el dato")


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


# ── El % del parado ─────────────────────────────────────────────────────────

def test_el_porcentaje_se_calcula_sobre_las_filas_que_se_muestran(monkeypatch):
    """⭐ El total del % sale de un SUM() OVER () sobre el mismo conjunto de
    filas que se dibuja. Traer el total por separado es cómo dos números del
    mismo cuadro terminan sin sumar 100."""
    visto = {}

    def fake(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split())
        return []

    monkeypatch.setattr(queries.db, "fetch_all", fake)
    queries.items()
    s = visto["sql"]
    assert "OVER ()" in s, "el total tiene que ser el de la misma consulta"
    assert "OVER (PARTITION BY c.subcategoria)" in s, (
        "el % por TELA suma los colores de esa tela")
    assert "NULLIF(SUM(COALESCE(f.stock_kg, 0)) OVER (), 0)" in s, (
        "sin el NULLIF, una lista vacía divide por cero")


def test_la_tabla_no_queda_desalineada_al_agregar_la_columna():
    """La fila de detalle usa colspan: si no acompaña a las columnas de arriba,
    la tabla se desarma y no da ningún error."""
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    # ⚠ Apuntado a la tabla PRINCIPAL por su id: la pantalla tiene otra tabla
    # (el resumen por grupo) y agarrar su encabezado hacía fallar el test por
    # el motivo equivocado.
    principal = html[html.index('<table id="tabla">'):]
    encabezado = re.search(r"<thead><tr>(.*?)</tr></thead>", principal, re.S).group(1)
    columnas = len(re.findall(r"<th", encabezado))
    colspan = int(re.search(r'colspan="(\d+)"', principal).group(1))
    assert colspan == columnas, (
        f"el detalle abarca {colspan} columnas y la tabla tiene {columnas}")


# ── Primera y segunda calidad ───────────────────────────────────────────────

def test_la_calidad_sale_del_lote_y_no_del_producto():
    """⭐ En Asinfo la calidad es el ATRIBUTO 2 del LOTE (PRI/SEG), no una
    propiedad del producto. Por eso un mismo tela × color puede tener kilos de
    las dos y hacen falta DOS columnas: 26 de los ítems parados tienen de las
    dos, y una sola columna obligaría a elegir y perdería la mitad del dato."""
    s = asinfo_parado.SQL_PARADOS
    assert "saldo_producto_lote" in s
    assert "l.id_valor_atributo_2" in s
    assert "v.codigo = 'SEG'" in s
    assert "AS kg_primera" in s and "AS kg_segunda" in s


def test_lo_que_no_esta_marcado_cuenta_como_primera():
    """585.011 lotes no tienen el atributo cargado. Tratarlos como "sin
    calidad" dejaría la mayoría del stock en una tercera categoría que no
    existe para nadie; tratarlos como SEGUNDA sería peor. El CASE los manda a
    primera y la pantalla lo dice."""
    assert "CASE WHEN v.codigo = 'SEG' THEN 0 ELSE lu.saldo END" in \
        asinfo_parado.SQL_PARADOS


def test_los_kilos_por_calidad_suman_el_total_de_la_fila(monkeypatch):
    """Si `saldo_producto` y `saldo_producto_lote` se despegan, primera + segunda
    deja de dar el stock de la fila. Hoy cierran (0,006% en la bodega 53)."""
    filas = [{"stock_kg": 100, "kg_primera": 88, "kg_segunda": 12,
              "kg_vendidos": 0, "clientes": 1}]
    r = queries.resumen(filas)
    assert r["kg_segunda"] == 12
    assert filas[0]["kg_primera"] + filas[0]["kg_segunda"] == filas[0]["stock_kg"]


# ── Grupo / subgrupo y Excel ────────────────────────────────────────────────

def test_el_grupo_de_producto_llega_desde_asinfo():
    """En Asinfo el GRUPO es nombre_categoria_producto y el SUBGRUPO es
    nombre_subcategoria_producto — lo que la pantalla llamaba "tela"."""
    assert "MIN(p.nombre_categoria_producto) AS categoria" in \
        asinfo_parado.SQL_PARADOS
    assert "stk.categoria" in asinfo_parado.SQL_PARADOS


def test_la_pantalla_ofrece_los_dos_desplegables():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert 'id="grupo"' in html and 'id="subgrupo"' in html
    assert "data-grupo=" in html and "data-sub=" in html


def test_elegir_un_grupo_recorta_los_subgrupos():
    """Sin esto se puede pedir "Fleece" + "Jersey 3.5": la tabla queda vacía y
    parece que no hay datos, cuando lo que hay es una combinación imposible."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert "function grupoCambio()" in html
    assert "o.dataset.g === g" in html


def test_el_csv_baja_todo_y_no_lo_que_esta_filtrado():
    """⭐ Los filtros de la pantalla son de JavaScript. Replicarlos del lado del
    servidor sería escribir la misma regla dos veces en dos lenguajes, y el día
    que una cambie el archivo diría algo distinto de la pantalla sin síntoma. El
    archivo trae Grupo y Subgrupo como columnas justamente para filtrar en
    Excel, y el botón dice "Bajar TODO"."""
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views.parado_csv)
    assert "queries.items()" in fuente
    assert "request.args" not in fuente, (
        "si el CSV empieza a leer filtros, hay dos reglas de filtrado que "
        "pueden divergir")

    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert "Bajar TODO a Excel" in html


def test_el_csv_lleva_grupo_y_subgrupo_como_columnas():
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views.parado_csv)
    for col in ('"Grupo"', '"Subgrupo (tela)"', '"Kg de segunda"', '"% del parado"'):
        assert col in fuente, f"falta la columna {col} en el Excel"


# ── El resumen por grupo y el ordenar por columna ───────────────────────────

def test_el_resumen_por_grupo_suma_100(monkeypatch):
    filas = [
        {"categoria": "Jersey", "subcategoria": "Jersey 3", "stock_kg": 60,
         "kg_segunda": 0},
        {"categoria": "Jersey", "subcategoria": "Jersey 3.5", "stock_kg": 20,
         "kg_segunda": 5},
        {"categoria": "Fleece", "subcategoria": "Fleece 102", "stock_kg": 20,
         "kg_segunda": 0},
    ]
    g = queries.por_grupo(filas)
    assert [x["grupo"] for x in g] == ["Jersey", "Fleece"], "de mayor a menor"
    assert round(sum(x["pct"] for x in g), 6) == 100
    assert g[0]["n_items"] == 2 and g[0]["subgrupos"] == 2
    assert g[0]["kg_segunda"] == 5


def test_el_resumen_sale_de_las_mismas_filas_que_la_tabla():
    """Si fuera una consulta aparte, el resumen y la tabla podrían dejar de
    coincidir el día que una cambie de criterio, y no habría síntoma."""
    import inspect
    fuente = inspect.getsource(queries.por_grupo)
    assert "db.fetch" not in fuente, "por_grupo recibe las filas, no las consulta"


def test_sin_grupo_no_desaparece_del_resumen():
    """Una fila sin categoría cargada tiene que sumar igual: si se cayera del
    resumen, los porcentajes seguirían dando 100 y faltarían kilos."""
    g = queries.por_grupo([{"categoria": None, "subcategoria": "X",
                            "stock_kg": 10, "kg_segunda": 0}])
    assert g[0]["grupo"] == "(sin grupo)" and g[0]["kg"] == 10


def test_ordenar_mueve_la_fila_con_su_detalle():
    """⚠ Cada tela son DOS <tr>: la fila y su detalle escondido. Un ordenador
    que mueva filas sueltas deja los detalles pegados a la tela equivocada, y
    como están cerrados no se nota hasta que alguien abre uno."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert "pares.push([tr, tr.nextElementSibling])" in html
    assert "frag.appendChild(fila); frag.appendChild(det);" in html


def test_las_columnas_numericas_ordenan_por_el_numero_y_no_por_el_texto():
    """"1.779" y "580" como texto ordenan al revés: "1" < "5"."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert 'parseFloat(td.dataset.v ?? \'0\')' in html
    import re
    principal = html[html.index('<table id="tabla">'):]
    encabezado = re.search(r'<thead><tr>\n(.*?)</tr></thead>', principal, re.S).group(1)
    numericas = re.findall(r'data-i="(\d+)" data-num="1"', encabezado)
    assert numericas, "alguna columna tiene que estar marcada como numérica"
    # toda columna marcada numérica tiene que tener data-v en sus celdas
    assert html.count('data-v="') >= len(numericas), (
        "una columna numérica sin data-v ordena por el texto formateado")


# ── Excel desde la hoja del vendedor ────────────────────────────────────────

def test_el_excel_de_la_hoja_sale_de_la_misma_funcion_que_la_pantalla(monkeypatch):
    """Si fueran dos caminos, el archivo y el papel podrían decir cosas
    distintas del mismo día."""
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: _filas_falsas())
    plano = queries.por_cliente_plano()
    pantalla = queries.por_cliente()
    assert {f["codigo"] for f in plano} == (
        {c["codigo"] for c in pantalla["clientes"]}
        | {c["codigo"] for c in pantalla["improbables"]})


def test_el_excel_de_la_hoja_distingue_candidatos_de_improbables(monkeypatch):
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: _filas_falsas())
    plano = queries.por_cliente_plano()
    tipos = {f["codigo"]: f["tipo"] for f in plano}
    assert tipos["AAA"] == "candidato" and tipos["CCC"] == "improbable", (
        "aplanado a Excel, un improbable mezclado con los buenos es "
        "indistinguible — y el orden de las filas no lo dice")


def test_el_excel_de_la_hoja_respeta_el_vendedor_elegido(monkeypatch):
    """A diferencia del CSV de «Lo parado», acá el filtro NO es de JavaScript:
    viaja en la URL y lo aplica la misma función que dibuja la pantalla, así que
    puede y debe viajar al archivo."""
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views.parado_clientes_csv)
    assert 'request.args.get("vend")' in fuente
    assert "queries.por_cliente_plano(vend, orden)" in fuente


def test_las_telas_de_cada_cliente_salen_de_mayor_a_menor(monkeypatch):
    filas = _filas_falsas()
    for f in filas:
        f["codigo_cli"], f["nombre"] = "AAA", "Uno"
    filas[0]["kg_parado"], filas[1]["kg_parado"], filas[2]["kg_parado"] = 10, 900, 50
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: filas)
    telas = queries.por_cliente()["clientes"][0]["telas"]
    assert [float(t["kg_parado"]) for t in telas] == [900, 50, 10]


def test_las_dos_pantallas_tienen_su_boton_de_excel():
    """Dueña 17/08/2026: "de todos lados deberia poder bajar a excel"."""
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    for archivo, texto in (("parado.html", "Bajar TODO a Excel"),
                           ("parado_clientes.html", "Bajar a Excel")):
        html = (carpeta / archivo).read_text(encoding="utf-8")
        assert texto in html, f"{archivo} no ofrece bajar a Excel"


def test_la_tela_cruda_no_entra_aunque_este_en_la_bodega_de_terminado():
    """Dueña 17/08/2026: "tela cruda no debería estar.. es producto terminado".
    Hay 2 productos de categoría TELA CRUDA con saldo en la bodega 53 (37 kg):
    estar ahí no los convierte en producto terminado, y el filtro por bodega
    solo no los saca."""
    for sql in (asinfo_parado.SQL_PARADOS, asinfo_parado.SQL_LLAMADOS):
        assert "'TELA CRUDA'" in sql
    assert "'TELA CRUDA'" in asinfo_parado.CATS


def test_los_tres_grupos_chicos_van_juntos(monkeypatch):
    """Dueña 17/08/2026: "los ultimos y mas chicos unilos todos en una
    categoria franela cuellos y punos". Entre los tres son 361 kg — menos del
    1% — y ocupaban tres renglones del resumen para decir casi nada."""
    visto = {}
    monkeypatch.setattr(queries.db, "fetch_all",
                        lambda sql, *a, **k: visto.setdefault("sql", " ".join(sql.split())) and [])
    queries.items()
    s = visto["sql"]
    assert "'Franela', 'Cuellos', 'Puños'" in s
    assert "'Franela, cuellos y puños'" in s


def test_los_grupos_chicos_se_unen_al_LEER_y_no_al_guardar():
    """Unirlos en el refresh perdería el dato crudo de Asinfo. Uniéndolos en la
    lectura, el día que uno crezca se separa cambiando una línea."""
    import inspect
    assert "Franela" not in inspect.getsource(queries.actualizar)
    assert "Franela" in inspect.getsource(queries.items)


# ── La competencia ──────────────────────────────────────────────────────────

def _competencia_falsa(monkeypatch, vendido=None, override=None):
    filas = [
        {"categoria": "Jersey", "subcategoria": "Jersey 3", "color": "NEG",
         "stock_kg": 6000, "kg_segunda": 0, "kg_vendidos": 0, "clientes": 1},
        {"categoria": "Fleece", "subcategoria": "Fleece 102", "color": "BLA",
         "stock_kg": 4000, "kg_segunda": 0, "kg_vendidos": 0, "clientes": 1},
    ]

    def fake(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "parado_meta" in s:
            return [{"categoria": k, "pct": v} for k, v in (override or {}).items()]
        if "parado_venta" in s:
            return vendido or []
        if "parado_share" in s:
            return []
        return filas

    monkeypatch.setattr(queries.db, "fetch_all", fake)
    return queries.competencia()


def test_la_meta_de_un_grupo_es_su_peso_en_el_parado(monkeypatch):
    """Dueña: "meta segun cantidad de ese grupo % en el total del stock".
    Jersey es 6.000 de 10.000 = 60% → su meta es sacar el 60% de sus 6.000."""
    c = _competencia_falsa(monkeypatch)
    jersey = next(g for g in c["grupos"] if g["grupo"] == "Jersey")
    assert jersey["meta_pct"] == 60
    assert jersey["meta_kg"] == 3600
    fleece = next(g for g in c["grupos"] if g["grupo"] == "Fleece")
    assert fleece["meta_pct"] == 40 and fleece["meta_kg"] == 1600


def test_la_meta_se_reparte_en_partes_iguales(monkeypatch):
    """⭐ Dueña 17/08/2026: "nono igual para todos. todos deberian tener lugares
    fuertes y debiles asumo". Repartir por lo que cada uno ya vende premiaría
    quedarse en lo de siempre — lo contrario de lo que busca la competencia."""
    c = _competencia_falsa(monkeypatch)
    metas = {r["vendedor"]: r["meta"] for r in c["ranking"]}
    assert len(set(round(m, 6) for m in metas.values())) == 1, (
        "todos tienen que tener la misma meta")
    assert round(sum(metas.values()), 2) == round(c["meta_kg"], 2)


def test_el_ranking_va_por_porcentaje_y_no_por_kilos(monkeypatch):
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Quintero Jose", "categoria": "Jersey", "kg": 100,
         "ultima": None},
        {"vendedor": "Intela", "categoria": "Jersey", "kg": 50, "ultima": None},
    ])
    assert c["ranking"][0]["vendedor"] == "Quintero Jose"
    assert c["ranking"][0]["puesto"] == 1


def test_intela_compite_como_uno_mas(monkeypatch):
    """Decisión de la dueña con el dato a la vista: Intela es el 51,3% de las
    ventas de estas telas. "Compite como uno mas porque hay una vendedora
    dedicada"."""
    c = _competencia_falsa(monkeypatch)
    assert "Intela" in [r["vendedor"] for r in c["ranking"]]
    assert len(c["ranking"]) == 7


def test_un_vendedor_que_ya_no_esta_suma_al_grupo_pero_no_al_ranking(monkeypatch):
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Bedon Hector", "categoria": "Jersey", "kg": 80, "ultima": None}])
    assert c["kg_fuera_del_ranking"] == 80
    assert all(r["kg"] == 0 for r in c["ranking"])
    jersey = next(g for g in c["grupos"] if g["grupo"] == "Jersey")
    assert jersey["liquidado"] == 80, (
        "el kilo salió de la bodega igual: tiene que contar para el grupo")


def test_la_meta_a_mano_pisa_la_automatica(monkeypatch):
    c = _competencia_falsa(monkeypatch, override={"Jersey": 10})
    jersey = next(g for g in c["grupos"] if g["grupo"] == "Jersey")
    assert jersey["meta_pct"] == 10 and jersey["meta_es_manual"]
    fleece = next(g for g in c["grupos"] if g["grupo"] == "Fleece")
    assert fleece["meta_pct"] == 40 and not fleece["meta_es_manual"]


def test_la_competencia_sale_de_las_mismas_filas_que_lo_parado():
    """Si saliera de otra consulta, el termómetro de acá y el total de allá
    podrían no coincidir el mismo día."""
    import inspect
    assert "items()" in inspect.getsource(queries.competencia)


# ── El candado, con una ruta nueva abierta ──────────────────────────────────

def test_la_competencia_esta_abierta_a_todos():
    """Dueña: "aca tienen acceso todos, vendedores sobre todo incluidos"."""
    from modules.analisis import views
    assert getattr(views.competencia, "_permiso", None) is None, (
        "la competencia no lleva gate de permiso a propósito")


def test_las_metas_siguen_cerradas():
    from modules.analisis import views
    assert getattr(views.competencia_metas, "_permiso", None) == "analisis.ver"


def test_las_metas_no_cuelgan_del_prefijo_abierto(app):
    """⚠ El allowlist de vendedores matchea por segmento: todo lo que cuelgue de
    `/analisis/competencia/` les queda abierto. La pantalla de metas vive
    afuera para que sean dos cierres y no uno."""
    import scope_vendedor
    rutas = {r.rule for r in app.url_map.iter_rules()}
    assert "/analisis/metas" in rutas
    assert "/analisis/competencia/metas" not in rutas
    assert not any(p.startswith("/analisis") for p in scope_vendedor.PREFIJOS_PERMITIDOS)


def test_al_vendedor_todavia_no_se_le_habilito_la_competencia():
    """La pantalla está hecha para ellos, pero la dueña la quiere ver primero:
    "todavia igual no se las habilites". Cuando lo diga, se agrega
    "/analisis/competencia" a PREFIJOS_PERMITIDOS y este test se da vuelta.

    ⚠ Lo que NO puede pasar nunca es que se abra /analisis a secas o
    /analisis/parado: ahí está la cartera de TODOS los vendedores."""
    import scope_vendedor
    abiertos = [p for p in scope_vendedor.PREFIJOS_PERMITIDOS
                if p.startswith("/analisis")]
    assert abiertos == [], f"todavía no había que habilitar nada: {abiertos}"


def test_si_algun_dia_se_abre_que_sea_solo_la_competencia():
    """El guard del guard: el día que se habilite, que no se cuele /analisis
    entero de un copy-paste."""
    import scope_vendedor
    for p in scope_vendedor.PREFIJOS_PERMITIDOS:
        assert p not in ("/analisis", "/analisis/parado"), (
            f"{p} le abre al vendedor la cartera de los otros cinco")


def test_ningun_porcentaje_sale_con_quince_decimales():
    """⚠ En Jinja `x if y else 0 | round(1)` aplica el filtro SÓLO al 0: el
    termómetro mostraba "17.284974824441832%". Además con punto decimal, que en
    esta app es separador de miles."""
    import re
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    for archivo in carpeta.glob("*.html"):
        html = archivo.read_text(encoding="utf-8")
        for expr in re.findall(r"\{\{([^}]*%)", html):
            if "if" in expr and "|" in expr:
                antes_del_filtro = expr.split("|")[0]
                assert antes_del_filtro.strip().startswith("("), (
                    f"{archivo.name}: '{expr.strip()}' — el filtro se aplica "
                    f"sólo a la última rama del if; faltan paréntesis")
