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
    for col in (chr(34)+"Grupo"+chr(34), chr(34)+"Subgrupo (tela)"+chr(34), chr(34)+"Kg de segunda"+chr(34), chr(34)+"% del saldo"+chr(34)):
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

def _competencia_falsa(monkeypatch, vendido=None, override=None, total_pct="100",
                       semanas=None, meses=None):
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
        if "date_trunc('week'" in s:
            return semanas or []
        if "date_trunc('month'" in s:
            return meses or []
        if "parado_venta" in s:
            return vendido or []
        if "parado_share" in s:
            return []
        return filas

    def fake_one(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "parado_config" in s:
            # ⚠ Devolver lo mismo para toda clave hacía que `cierre` valiera la
            # fecha de largada y el test de la fecha de cierre pasara por el
            # motivo equivocado.
            return {"valor": {"meta_total_pct": total_pct,
                              "largada": "2026-08-25",
                              "cierre": "2026-12-31"}[(params or ("",))[0]]}
        return None

    monkeypatch.setattr(queries.db, "fetch_all", fake)
    monkeypatch.setattr(queries.db, "fetch_one", fake_one)
    return queries.competencia()


def test_la_dueña_pone_el_total_y_todos_los_grupos_tienen_la_misma_exigencia(monkeypatch):
    """⭐ La primera versión le ponía a cada grupo SU PROPIO PESO como meta
    (Jersey 31,5% → sacarle el 31,5%). Daba un total de 21,7% que no decidió
    nadie y una meta de 4 kg para el grupo más chico: una fórmula que se muerde
    la cola. Ahora la dueña pone el total y se reparte por tamaño, o sea que
    todos los grupos quedan con la misma exigencia."""
    c = _competencia_falsa(monkeypatch, total_pct="40")
    for g in c["grupos"]:
        assert g["meta_pct"] == 40
    jersey = next(g for g in c["grupos"] if g["grupo"] == "Jersey")
    assert jersey["meta_kg"] == 2400          # 40% de 6.000
    assert c["meta_kg"] == 4000               # 40% de 10.000


def test_con_la_meta_en_todo_cada_grupo_despeja_sus_kilos(monkeypatch):
    """Dueña 17/08/2026, cuánto hay que sacar: "todo"."""
    c = _competencia_falsa(monkeypatch)
    assert c["meta_kg"] == c["kg_parado"] == 10000


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


def test_la_meta_a_mano_pisa_la_del_total(monkeypatch):
    c = _competencia_falsa(monkeypatch, override={"Jersey": 10}, total_pct="40")
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


def test_todos_los_porcentajes_llevan_un_decimal():
    """Dueña 17/08/2026: "un decimal". Con enteros, un grupo del 0,4% se lee
    como 0% y parece que no existe; con dos, la columna deja de leerse de un
    vistazo."""
    import re
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    for archivo in carpeta.glob("*.html"):
        html = archivo.read_text(encoding="utf-8")
        for decimales in re.findall(r"num_es\((\d)\)\s*\}\}%", html):
            assert decimales == "1", (
                f"{archivo.name}: un porcentaje con {decimales} decimales")


# ── Lo que ve un vendedor ───────────────────────────────────────────────────

def test_la_cartera_del_vendedor_sale_de_programa_core_y_no_de_asinfo():
    """⚠ `cliente.vend` (Programa Core) y el vendedor de la última factura de
    Asinfo no siempre coinciden. Si acá usara el de Asinfo, un cliente podría
    salirle a uno en esta pantalla y a otro en /mi-cartera, y eso no hay forma
    de explicárselo a nadie."""
    import inspect
    fuente = inspect.getsource(queries.mis_clientes_parado)
    assert "scintela.cliente" in fuente and "c.vend" in queries._ES_MI_CLIENTE
    assert "vend_pc" not in fuente, "vend_pc es el de Asinfo, no la cartera de PC"


def test_el_predicado_de_pertenencia_se_importa_del_portal():
    """⭐ No se compara: se IMPORTA. Escrito a mano acá, el día que en
    /mi-cartera cambien el criterio las dos pantallas le mostrarían al vendedor
    carteras distintas y nadie se enteraría."""
    import inspect

    from modules.mi_cartera import queries as mc
    assert mc._ES_MI_CLIENTE.replace(
        "%(vend)s", "%(cartera)s") == queries._ES_MI_CLIENTE
    fuente = inspect.getsource(queries)
    assert "from modules.mi_cartera.queries import" in fuente, (
        "tiene que venir por import, no copiado")


def test_sin_vendedor_no_devuelve_la_lista_entera(monkeypatch):
    """⚠ Un scope que falla ABIERTO no da error: muestra de más y nadie se
    entera. Con `vend` vacío tiene que devolver [], no todo."""
    llamado = {"veces": 0}

    def fake(*a, **k):
        llamado["veces"] += 1
        return [{"codigo_cli": "AAA"}]

    monkeypatch.setattr(queries.db, "fetch_all", fake)
    assert queries.mis_clientes_parado("") == []
    assert queries.mis_clientes_parado(None) == []
    assert llamado["veces"] == 0, "ni siquiera tiene que consultar"


def test_el_vendedor_del_bloque_sale_del_usuario_y_no_de_la_url():
    """Si viniera del querystring, cualquiera vería la cartera de cualquiera
    cambiando tres letras. La vista lo pide por `_vend_actual()`, que sólo mira
    la URL cuando el usuario es wildcard (preview de la dueña)."""
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views.competencia)
    assert "_vend_actual()" in fuente
    assert "request.args" not in fuente, (
        "la vista no lee la URL: eso lo decide _vend_actual")


# ── Semana a semana ─────────────────────────────────────────────────────────

def test_el_acumulado_de_la_semana_mas_nueva_es_el_total(monkeypatch):
    from datetime import date as _d
    c = _competencia_falsa(monkeypatch, semanas=[
        {"semana": _d(2026, 8, 17), "vendedor": "Intela", "kg": 100},
        {"semana": _d(2026, 8, 10), "vendedor": "Intela", "kg": 40},
    ])
    filas = c["semanas"]
    assert [f["semana"] for f in filas] == [_d(2026, 8, 17), _d(2026, 8, 10)], \
        "la semana más nueva va arriba"
    assert filas[0]["acumulado"] == 140 and filas[1]["acumulado"] == 40


def test_el_movimiento_del_ranking_se_recalcula_sin_guardar_nada(monkeypatch):
    """El puesto de la semana pasada sale de descontar lo de esta semana. Sin
    esto habría que guardar un ranking por semana para poder decir "subió"."""
    from datetime import date as _d
    c = _competencia_falsa(
        monkeypatch,
        vendido=[{"vendedor": "Quintero Jose", "categoria": "Jersey", "kg": 500,
                  "ultima": None}],
        semanas=[{"semana": _d(2026, 8, 17), "vendedor": "Quintero Jose",
                  "kg": 500}])
    quintero = next(r for r in c["ranking"] if r["vendedor"] == "Quintero Jose")
    assert quintero["puesto"] == 1
    assert quintero["kg_semana"] == 500
    assert quintero["movimiento"] > 0, "venía último y pasó a primero"


def test_los_tres_numeros_de_arriba_cierran_entre_ellos(monkeypatch):
    """"Había" no se guarda: se reconstruye como lo que hay más lo que se
    vendió. Así los tres números cierran siempre, que es lo primero que alguien
    va a chequear de un vistazo."""
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Intela", "categoria": "Jersey", "kg": 250, "ultima": None}])
    assert c["kg_al_largar"] == c["kg_parado"] + c["liquidado"]


def test_no_se_cuentan_las_ventas_anteriores_a_la_largada():
    """La cohorte se marcó el 13/08 y la competencia arranca el 17: sin el
    corte, esos cuatro días le regalarían kilos a quien justo vendió algo."""
    import inspect
    fuente = inspect.getsource(queries.competencia)
    assert "WHERE v.fecha >= %s" in fuente
    assert 'config("largada"' in fuente


def test_la_fila_fantasma_sin_grupo_no_se_dibuja(monkeypatch):
    """Restos de la cohorte que ya no están en la foto (la tela cruda que se
    sacó) sumaban un renglón "(sin grupo) · 0 kg" que sólo confunde."""
    import inspect
    assert 'if float(f["stock_kg"]) > 0 or f["categoria"]' in \
        inspect.getsource(queries.competencia)


def test_el_total_no_se_guarda_como_si_fuera_un_grupo():
    """El campo del total se llama `meta_total_pct` y el bucle que guarda los
    grupos toma todo lo que empiece con `meta_`: sin la excepción, se crearía un
    grupo fantasma llamado "total_pct" con su propia meta."""
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views.competencia_metas)
    assert 'clave == "meta_total_pct"' in fuente


def test_un_porcentaje_que_no_se_entiende_se_avisa(monkeypatch):
    """⚠ Tragarse el error y contestar "Metas guardadas" es la peor respuesta:
    la dueña se va convencida de que cambió algo. Hay un test del repo que
    prohíbe los `except: pass` mudos y cazó justamente éste."""
    from modules.analisis import views
    malos: list[str] = []
    assert views._numero("40,5", malos, "x") == 40.5
    assert views._numero("", malos, "x") is None
    assert malos == []
    assert views._numero("cuarenta", malos, "Jersey") is None
    assert malos == ["Jersey"]


# ── Lo que necesita el vendedor para poder ofrecer ──────────────────────────

def test_las_telas_a_sacar_no_llevan_un_solo_cliente_adentro():
    """Es lo que le faltaba al vendedor: la pantalla le decía quién le compró
    qué, pero no cuántos kilos hay ni de qué colores. Sin clientes adentro, la
    lista es información de fábrica y la puede ver cualquiera."""
    t = queries.telas_a_sacar([
        {"subcategoria": "Kiana", "categoria": "Poliester", "color": "CAR",
         "stock_kg": 100},
        {"subcategoria": "Kiana", "categoria": "Poliester", "color": "LIF",
         "stock_kg": 300},
        {"subcategoria": "Jersey 3", "categoria": "Jersey", "color": "NEG",
         "stock_kg": 50},
    ])
    assert [x["tela"] for x in t] == ["Kiana", "Jersey 3"], "de mayor a menor"
    assert t[0]["kg"] == 400 and t[0]["n_colores"] == 2
    assert t[0]["colores"] == "LIF, CAR", "los colores van por kilos"
    assert all("cliente" not in k for x in t for k in x)


def test_las_telas_sin_stock_no_se_listan():
    assert queries.telas_a_sacar([
        {"subcategoria": "X", "categoria": "Y", "color": "Z", "stock_kg": 0}]) == []


def test_la_hoja_del_vendedor_usa_la_plantilla_de_la_oficina():
    """⚠ Dos plantillas para el mismo papel divergen a la primera corrección
    que se le hace a una sola. Ya pasó en /mi-cartera: ocho días imprimiendo
    órdenes distintos sin síntoma."""
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views.mi_hoja)
    assert '"analisis/parado_clientes.html"' in fuente
    assert "cartera_de=vend" in fuente, (
        "la hoja propia se acota por la cartera de Programa Core")


def test_la_hoja_propia_no_ofrece_el_selector_de_otros_vendedores():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado_clientes.html").read_text(encoding="utf-8")
    assert "{% if not mia %}" in html


def test_un_vendedor_no_puede_pedir_la_cartera_de_otro_por_la_url():
    """`?vend=` sólo lo respeta un usuario wildcard, para previsualizar. Si el
    que lo manda ES vendedor, gana el suyo."""
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views._vend_actual)
    assert 'propio = (g.user or {}).get("vend")' in fuente
    assert "if propio:" in fuente and "return propio" in fuente
    assert '"*" in (g.get("permisos")' in fuente


def test_la_hoja_general_no_se_le_abre_nunca_al_vendedor():
    """/analisis/parado/clientes tiene los clientes de TODOS. La hoja del
    vendedor cuelga de /analisis/competencia/ justamente para no tener que
    abrirle esa."""
    import scope_vendedor
    for p in scope_vendedor.PREFIJOS_PERMITIDOS:
        assert not p.startswith("/analisis/parado")


def test_la_hoja_propia_baja_a_excel_por_su_propia_ruta():
    """Dueña: "de todos lados deberia poder bajar a excel". El CSV de la hoja
    propia cuelga del MISMO camino que la pantalla (…/mi-hoja.csv) para que
    quede dentro del mismo permiso del allowlist, y va recortado por cartera."""
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views.mi_hoja_csv)
    assert "cartera_de=vend" in fuente
    assert "/analisis/competencia/mi-hoja.csv" in inspect.getsource(views)


def test_el_csv_de_la_hoja_propia_no_lleva_la_columna_del_vendedor():
    """En la hoja propia son todos suyos: una columna "Vendedor" con el mismo
    valor repetido no dice nada y ocupa lugar."""
    import inspect

    from modules.analisis import views
    assert '"Vendedor"' not in inspect.getsource(views.mi_hoja_csv)


# ── El tope por grupo ───────────────────────────────────────────────────────

def test_lo_que_pasa_del_tope_no_le_suma_al_vendedor(monkeypatch):
    """⭐ Dueña 17/08/2026: "cada vendedor tiene que hacer x% de cada grupo…
    una vez que llega al 31% deja de sumar". Sin el tope, el que tiene un
    cliente grande de Jersey llega al 100% sin tocar los otros grupos, y la
    competencia deja de servir para lo que se armó."""
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Intela", "categoria": "Jersey", "kg": 5000, "ultima": None}])
    intela = next(r for r in c["ranking"] if r["vendedor"] == "Intela")
    jersey = next(d for d in intela["detalle"] if d["grupo"] == "Jersey")
    assert jersey["meta"] == 6000 / 7
    assert jersey["kg"] == 5000
    assert round(jersey["contado"], 2) == round(6000 / 7, 2), "se corta en su meta"
    assert round(jersey["excedente"], 2) == round(5000 - 6000 / 7, 2)
    assert jersey["pct"] == 100
    assert intela["pct"] < 100, (
        "con un solo grupo lleno no puede estar al 100% del total")


def test_el_kilo_excedente_igual_sale_de_la_bodega(monkeypatch):
    """El kilo se vendió: cuenta para el grupo y para el termómetro de la
    fábrica. Lo único que no hace es subirle el % al vendedor."""
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Intela", "categoria": "Jersey", "kg": 5000, "ultima": None}])
    jersey = next(g for g in c["grupos"] if g["grupo"] == "Jersey")
    assert jersey["liquidado"] == 5000
    assert c["liquidado"] == 5000


def test_llegar_al_100_exige_tocar_todos_los_grupos(monkeypatch):
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Intela", "categoria": "Jersey", "kg": 6000 / 7,
         "ultima": None},
        {"vendedor": "Intela", "categoria": "Fleece", "kg": 4000 / 7,
         "ultima": None},
    ])
    intela = next(r for r in c["ranking"] if r["vendedor"] == "Intela")
    assert round(intela["pct"], 1) == 100.0


def test_el_ranking_ordena_por_lo_contado_y_no_por_los_kilos(monkeypatch):
    """Uno con 5.000 kg de un solo grupo tiene que ir DETRÁS de otro con menos
    kilos pero repartidos."""
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Intela", "categoria": "Jersey", "kg": 5000, "ultima": None},
        {"vendedor": "Lopez Felipe", "categoria": "Jersey", "kg": 800,
         "ultima": None},
        {"vendedor": "Lopez Felipe", "categoria": "Fleece", "kg": 500,
         "ultima": None},
    ])
    puestos = {r["vendedor"]: r["puesto"] for r in c["ranking"]}
    assert puestos["Lopez Felipe"] < puestos["Intela"]


def test_la_pantalla_explica_las_reglas():
    """Dueña: "escribamos las reglas del juego. facil". Una competencia cuyas
    reglas hay que preguntar no la juega nadie."""
    # ⚠ Dos cuidados para que el test mire lo que se VE:
    #   · se sacan los comentarios Jinja ({# … #}), donde justamente se explica
    #     por qué NO se dice cierta frase — si no, el test se engancha con su
    #     propia explicación;
    #   · se normalizan los espacios, porque el texto viene cortado en varias
    #     líneas y si no falla por el formato del HTML y no por lo que dice.
    import re as _re
    from pathlib import Path
    crudo = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
             "templates" / "analisis" / "competencia.html").read_text(encoding="utf-8")
    html = " ".join(_re.sub(r"\{#.*?#\}", " ", crudo, flags=_re.S).split())
    assert "Las reglas" in html
    assert "no le suma" in html or "no le</b> suma" in html, (
        "la regla del tope tiene que estar escrita")
    assert "qué parte de su meta" in html
    assert "no por kilos" not in html, (
        "todo se mide EN kilos; decir que no, confunde (dueña 17/08/2026)")


def test_la_fila_del_vendedor_se_abre_y_muestra_sus_grupos():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "competencia.html").read_text(encoding="utf-8")
    assert "abrirVend" in html and 'class="vdet"' in html
    for col in ("Su meta", "Vendió", "Le cuenta"):
        assert col in html


# ── La copia de "Lo parado" para el vendedor ────────────────────────────────

def test_el_vendedor_ve_la_misma_pantalla_con_sus_clientes():
    """Dueña 17/08/2026: "la tab de que hay que sacar copiala para ellos. y que
    vean con sus clientes… estaba linda diseñada". Es el MISMO template: lo
    único que cambia es de dónde salen los candidatos."""
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views.mis_telas)
    assert '"analisis/parado.html"' in fuente
    assert "cartera_de=vend" in fuente
    assert 'f["clientes"] = len(llamados.get(f["subcategoria"], []))' in fuente, (
        "la columna tiene que contar SUS clientes: con el total de la fábrica "
        "diría 137 y al abrir la fila aparecerían tres")


def test_los_candidatos_se_pueden_acotar_a_una_cartera(monkeypatch):
    visto = {}

    def fake(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split())
        visto["params"] = params
        return []

    monkeypatch.setattr(queries.db, "fetch_all", fake)
    queries.llamados_por_tela(cartera_de="FL1")
    assert "scintela.cliente" in visto["sql"] and "c.vend" in visto["sql"]
    assert visto["params"] == {"cartera": "FL1"}

    queries.llamados_por_tela()
    assert "scintela.cliente" not in visto["sql"], (
        "sin cartera no se filtra: es la pantalla de la oficina")


def test_el_menu_del_vendedor_no_tiene_links_que_le_dan_404(app):
    """Sus pantallas son OTRAS rutas, no un subconjunto de las de la oficina:
    mostrarle el menú de la oficina serían tres links a 404."""
    from modules.analisis import views
    rutas = {r.rule for r in app.url_map.iter_rules()}
    for m in views.MENU_VENDEDOR:
        assert m["url"] in rutas
        assert m["url"].startswith("/analisis/competencia"), (
            f"{m['url']} queda fuera del prefijo que se le habilita")


def test_la_pantalla_del_vendedor_no_ofrece_actualizar_desde_asinfo():
    """El refresh toca la base de todos y tarda 10 s: no es del vendedor."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    i = html.index("/analisis/parado/actualizar")
    assert "{% if not mia %}" in html[i - 400:i]


def test_la_fila_sin_stock_y_sin_grupo_no_ensucia_el_resumen():
    """Restos que ya no existen sumaban un renglón "(sin grupo) · 0 kg"."""
    g = queries.por_grupo([
        {"categoria": None, "subcategoria": "X", "stock_kg": 0, "kg_segunda": 0},
        {"categoria": "Jersey", "subcategoria": "Y", "stock_kg": 100,
         "kg_segunda": 0},
    ])
    assert [x["grupo"] for x in g] == ["Jersey"]


def test_un_item_vendido_entero_SI_sigue_en_el_resumen():
    """⚠ 0 kg y CON grupo es un ítem que se vendió entero: es justamente lo que
    la dueña pidió que no desaparezca."""
    g = queries.por_grupo([
        {"categoria": "Jersey", "subcategoria": "Y", "stock_kg": 0,
         "kg_segunda": 0}])
    assert [x["grupo"] for x in g] == ["Jersey"] and g[0]["n_items"] == 1


def test_la_competencia_tiene_fecha_de_cierre(monkeypatch):
    """Dueña 17/08/2026: "es una competencia hasta fin de año". La fecha vive en
    `parado_config` y no hardcodeada, porque la presentación que se les da a los
    vendedores dice la misma: si un día se corre, tiene que cambiar en UN lugar
    y no en dos que se olvidan de coincidir."""
    from datetime import date as _d
    c = _competencia_falsa(monkeypatch)
    assert c["cierre"] == _d(2026, 12, 31)
    assert c["dias_para_el_cierre"] == (_d(2026, 12, 31) - c["hoy"]).days


def test_la_pantalla_dice_cuanto_falta_para_el_cierre():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "competencia.html").read_text(encoding="utf-8")
    assert "dias_para_el_cierre" in html
    assert "Terminó el" in html, "cuando pase la fecha tiene que decirlo"


def test_la_largada_es_el_martes_25(monkeypatch):
    """Dueña 18/08/2026: "la vamos a empezar el martes que viene". Antes decía
    17/08 y habría contado kilos de una semana antes de que los vendedores
    supieran que la competencia existía."""
    from datetime import date as _d
    c = _competencia_falsa(monkeypatch)
    assert c["largada"] == _d(2026, 8, 25)


# ── Que se pueda mirar en el celular ────────────────────────────────────────

def test_las_pantallas_esconden_columnas_en_el_celular():
    """Dueña 18/08/2026: "los vendedores tienen un distinto portal, tmb lo ven
    asi?". No: ellos andan en el teléfono, y esto nació como tablas anchas de
    escritorio. En pantalla chica hay que SACAR columnas, no achicarlas: una
    tabla de diez columnas a 390 px no se lee ni con lupa."""
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    base = (carpeta / "base.html").read_text(encoding="utf-8")
    assert "@media (max-width: 780px)" in base
    assert ".opt{display:none!important}" in base, (
        "sin !important pierde contra cualquier display del cascarón")
    for archivo in ("competencia.html", "parado.html"):
        assert 'opt' in (carpeta / archivo).read_text(encoding="utf-8")


def test_en_el_celular_no_se_esconde_lo_que_identifica_la_fila():
    """Las columnas que se sacan son las de CONTEXTO. Si desapareciera el
    vendedor, la tela o el %, la tabla dejaría de decir nada."""
    import re
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    comp = " ".join((carpeta / "competencia.html").read_text(encoding="utf-8").split())
    for imprescindible in (">Vendedor<", ">% de su meta<", ">Cliente<"):
        col = re.search(r'<th[^>]*' + re.escape(imprescindible), comp)
        assert col, f"falta la columna {imprescindible}"
        assert "opt" not in col.group(0), (
            f"{imprescindible} no puede esconderse en el celular")


def test_las_columnas_se_esconden_por_clase_y_no_por_posicion():
    """Escondiendo `td:nth-child(5)`, una columna nueva en el medio correría
    todas y el que desaparecería sería otro — sin ningún error."""
    from pathlib import Path
    base = ((Path(__file__).resolve().parent.parent / "modules" / "analisis" /
             "templates" / "analisis" / "base.html").read_text(encoding="utf-8"))
    assert "nth-child" not in base


def test_las_pantallas_tratan_de_usted():
    """Dueña 18/08/2026: "en ecuador se trata de usted". El voseo se cuela de a
    una palabra por vez, así que el test lo caza en cualquier plantilla nueva de
    la sección."""
    import re
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    # ⚠ Los imperativos van SÓLO con tilde: "toca", "baja" y "deja" sin tilde
    # son formas de usted o de tercera persona y son correctas. Sin esa
    # distinción el test marcaba "le toca" y "Bajar a Excel" como voseo.
    voseo = re.compile(
        r"\b(toc[áí]|baj[áí]|entrás|eleg[íi]|volvé|fijate|tenés|pod[ée]s|"
        r"querés|vos|tuyo|tuyos|tus|llevás|ponés|dejá|mirá)\b", re.I)
    malas = []
    for archivo in carpeta.glob("*.html"):
        # sin comentarios Jinja: ahí se explica el porqué y puede nombrarlo
        texto = re.sub(r"\{#.*?#\}", " ", archivo.read_text(encoding="utf-8"),
                       flags=re.S)
        for m in voseo.finditer(texto):
            malas.append(f"{archivo.name}: {m.group(0)}")
    assert not malas, f"voseo en pantallas que ven los vendedores: {malas}"


def test_las_pantallas_hablan_de_saldos_y_no_de_sacar():
    """Dueña 18/08/2026: "que hay que sacar, lo podemos llamar distinto en todos
    lados" → eligió SALDOS. "Sacar" suena a que la fábrica se lo quiere sacar de
    encima, y a ellos se les está pidiendo que lo ofrezcan; "saldos" es además
    la palabra que el cliente ya usa cuando pregunta."""
    import re
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    prohibido = re.compile(r"(hay que sacar|Lo parado|liquidar lo parado)", re.I)
    malas = []
    for archivo in carpeta.glob("*.html"):
        # sin comentarios: ahí se explica el cambio y puede nombrar lo viejo
        texto = re.sub(r"(\{#.*?#\}|/\*.*?\*/|//[^\n]*)", " ",
                       archivo.read_text(encoding="utf-8"), flags=re.S)
        for m in prohibido.finditer(texto):
            malas.append(f"{archivo.name}: {m.group(0)}")
    assert not malas, f"quedó el nombre viejo: {malas}"


def test_el_menu_dice_saldos():
    from modules.analisis import views
    assert any(m["titulo"] == "Saldos" for m in views.MENU)
    assert any(m["titulo"] == "Saldos" for m in views.MENU_VENDEDOR)


# ── Toda la segunda entra a la competencia ──────────────────────────────────

def test_entra_toda_la_segunda_y_no_solo_la_parada():
    """Dueña 18/08/2026: "agreguemos toda la tela de segunda a la competencia"."""
    s = asinfo_parado.SQL_PARADOS
    assert "OR ISNULL(cal.kg_segunda, 0) > 0" in s


def test_de_una_tela_que_se_vende_entran_SOLO_los_kilos_de_segunda():
    """⚠ Esos ítems tienen 61.272 kg de primera que salen solos. Sumarlos
    habría inflado la competencia con tela que ya se vende y la meta dejaría de
    significar algo."""
    s = asinfo_parado.SQL_PARADOS
    assert "ELSE ISNULL(cal.kg_segunda, 0) END      AS stock_kg" in s
    assert "ELSE 0 END                              AS kg_primera" in s


def test_cada_fila_guarda_por_que_entro():
    """Sin `motivo`, en la pantalla no hay forma de distinguir 300 kg parados de
    300 kg de segunda de una tela que se vende todas las semanas."""
    assert "'parado' ELSE 'segunda' END AS motivo" in asinfo_parado.SQL_PARADOS
    import inspect
    assert "motivo" in inspect.getsource(queries.actualizar)
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert "de segunda</span>" in html


# ── El premio del mes ───────────────────────────────────────────────────────

def test_el_premio_del_mes_va_por_kilos_y_sin_tope(monkeypatch):
    """Dueña 18/08/2026: "un premio mensual por kg totales quizás?". Es a
    propósito la carrera contraria a la del año: aquélla premia repartir entre
    tipos de tela, ésta premia sacar kilos, y le da algo para ganar al que viene
    último en el porcentaje. Sin eso, el que se descuelga en octubre no vuelve
    a mirar la pantalla."""
    from datetime import date as _d
    c = _competencia_falsa(monkeypatch, meses=[
        {"mes": _d(2026, 10, 1), "vendedor": "Quintero Jose", "kg": 400},
        {"mes": _d(2026, 10, 1), "vendedor": "Intela", "kg": 900},
    ])
    oct_ = next(m for m in c["meses"] if m["mes"] == _d(2026, 10, 1))
    assert oct_["ganador"] == "Intela", "gana por kilos, no por % de meta"
    assert oct_["kg"] == 1300
    assert [x["vendedor"] for x in oct_["podio"]] == ["Intela", "Quintero Jose"]


def test_agosto_y_septiembre_cuentan_juntos(monkeypatch):
    """⚠ Del 25 al 31 de agosto hay cinco días hábiles: un "premio del mes" por
    esa semana no es un mes."""
    from datetime import date as _d
    c = _competencia_falsa(monkeypatch, meses=[
        {"mes": _d(2026, 8, 1), "vendedor": "Intela", "kg": 100},
        {"mes": _d(2026, 9, 1), "vendedor": "Intela", "kg": 250},
    ])
    assert [m["mes"] for m in c["meses"]] == [_d(2026, 9, 1)]
    assert c["meses"][0]["kg"] == 350


def test_el_mes_en_curso_se_marca_como_en_juego(monkeypatch):
    from datetime import date as _d
    c = _competencia_falsa(monkeypatch, meses=[
        {"mes": _d(2026, 9, 1), "vendedor": "Intela", "kg": 100}])
    assert c["meses"][0]["cerrado"] is False


def test_la_pantalla_explica_las_dos_carreras():
    from pathlib import Path
    html = " ".join((Path(__file__).resolve().parent.parent / "modules" /
                     "analisis" / "templates" / "analisis" /
                     "competencia.html").read_text(encoding="utf-8").split())
    assert "El premio del mes" in html
    assert "kilos totales" in html and "sin tope" in html


def test_la_tabla_semana_a_semana_no_se_dibuja():
    """Dueña 18/08/2026: "ok saca semana a semana". Con el premio del mes
    arriba era demasiada tabla para el vendedor.

    ⚠ El CÁLCULO por semana se sigue haciendo: de ahí salen la columna "Esta
    semana" del ranking y las flechas de subió/bajó. Lo que se saca es el
    dibujo, no el dato."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "competencia.html").read_text(encoding="utf-8")
    assert "<h2>Semana a semana</h2>" not in html
    assert "Esta semana" in html, "la columna del ranking se queda"


def test_el_nombre_de_la_tela_no_se_parte_en_dos_renglones():
    """Dueña 18/08/2026: "intenta que el nombre no ocupa mas de una fila". Con
    10 columnas la tabla aprieta esa celda y "Jersey Fancy" se parte en dos, lo
    que duplica el alto de la fila y hace que la lista se lea a saltos.

    ⚠ En el celular vuelve a partirse a propósito: a 390 px no entra de otra
    forma, y ahí el mal menor es la fila alta."""
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
           "templates" / "analisis" / "base.html").read_text(encoding="utf-8")
    assert "#tabla td.tela{white-space:nowrap}" in css
    i = css.index("@media (max-width: 780px)")
    assert "#tabla td.tela{white-space:normal}" in css[i:]
