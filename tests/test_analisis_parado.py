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

from modules.analisis import asinfo_parado, queries, views

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


def test_el_anio_de_los_candidatos_sigue_a_la_vista():
    """La columna Estado se sacó el 18/08/2026 ("el estado tampoco importa
    mucho"), pero de qué año son los candidatos SÍ importa: es la diferencia
    entre llamar a alguien que compró en julio y a uno que compró en 2023. Vive
    en el detalle que se abre."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert "f.pista" in html or "anio_pista" in html


# ── La hoja del vendedor ────────────────────────────────────────────────────

def _con_puntos(filas, puntos=None):
    """Un `db.fetch_all` falso que también sabe contestar por los PUNTOS.

    ⚠ El stub de antes devolvía las mismas filas para TODA consulta, así que
    `puntos_por_tela()` recibía filas de clientes y reventaba. Devuelve siempre
    al menos una fila de puntaje para que no se dispare el camino de "tabla
    vacía", que sale a buscar a Asinfo.
    """
    pfilas = [{"subcategoria": sub, "categoria": None, "kg_base": 0,
               "kg_12m": 0, "meses": None, "nivel": 1, "puntos": pk}
              for sub, pk in (puntos or {"—": 1}).items()]

    def fetch(sql, params=None, conn=None):
        if "parado_punto" in " ".join(str(sql).split()):
            return pfilas
        return filas
    return fetch


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
    monkeypatch.setattr(queries.db, "fetch_all", _con_puntos(_filas_falsas()))
    r = queries.por_cliente()
    assert [c["codigo"] for c in r["clientes"]] == ["AAA", "BBB"], (
        "la hoja existe para decidir por quién empezar: si el vendedor corta a "
        "la mitad, que haya cortado por lo chico")


def test_los_improbables_van_al_final_y_aparte(monkeypatch):
    monkeypatch.setattr(queries.db, "fetch_all", _con_puntos(_filas_falsas()))
    r = queries.por_cliente()
    assert [c["codigo"] for c in r["improbables"]] == ["CCC"]
    assert "CCC" not in [c["codigo"] for c in r["clientes"]], (
        "mezclado ensucia una lista que el vendedor tiene que poder creer")


def test_un_cliente_con_una_tela_vieja_y_una_nueva_no_es_improbable(monkeypatch):
    filas = _filas_falsas()
    filas[2]["codigo_cli"] = "AAA"          # la vieja es del cliente grande
    filas[2]["nombre"] = "Cliente Grande"
    monkeypatch.setattr(queries.db, "fetch_all", _con_puntos(filas))
    r = queries.por_cliente()
    assert [c["codigo"] for c in r["improbables"]] == []
    grande = next(c for c in r["clientes"] if c["codigo"] == "AAA")
    assert len(grande["telas"]) == 2


def test_el_orden_alfabetico_y_el_de_provincia_existen(monkeypatch):
    monkeypatch.setattr(queries.db, "fetch_all", _con_puntos(_filas_falsas()))
    assert [c["codigo"] for c in queries.por_cliente(orden="codigo")["clientes"]] \
        == ["AAA", "BBB"]
    assert [c["provincia"] for c in queries.por_cliente(orden="provincia")["clientes"]] \
        == ["AZUAY", "GUAYAS"]


def test_un_orden_inventado_no_rompe_la_pantalla(monkeypatch):
    monkeypatch.setattr(queries.db, "fetch_all", _con_puntos(_filas_falsas()))
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


def test_el_excel_baja_lo_que_se_ve_y_se_arma_en_el_navegador():
    """Dueña 18/08/2026: "que el Excel baje lo filtrado".

    ⭐ Se arma leyendo la MISMA tabla que se está mirando, no consultando de
    nuevo al servidor. Los filtros son de JavaScript: replicarlos allá serían
    dos reglas que un día se despegan y el archivo diría algo distinto de la
    pantalla sin ningún síntoma. Leyendo el DOM eso no puede pasar."""
    html = _html_parado()
    assert "function bajarExcel()" in html
    assert "tr.classList.contains('oculta')" in html, (
        "tiene que saltear justamente las filas que el filtro escondió")
    assert "ufeff" in html, "sin BOM, Excel abre los acentos rotos"

    # la ruta del servidor sigue existiendo y sigue sin leer filtros
    import inspect

    from modules.analisis import views
    assert "request.args" not in inspect.getsource(views.parado_csv)


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
    monkeypatch.setattr(queries.db, "fetch_all", _con_puntos(_filas_falsas()))
    plano = queries.por_cliente_plano()
    pantalla = queries.por_cliente()
    assert {f["codigo"] for f in plano} == (
        {c["codigo"] for c in pantalla["clientes"]}
        | {c["codigo"] for c in pantalla["improbables"]})


def test_el_excel_de_la_hoja_distingue_candidatos_de_improbables(monkeypatch):
    monkeypatch.setattr(queries.db, "fetch_all", _con_puntos(_filas_falsas()))
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
    monkeypatch.setattr(queries.db, "fetch_all", _con_puntos(filas))
    telas = queries.por_cliente()["clientes"][0]["telas"]
    assert [float(t["kg_parado"]) for t in telas] == [900, 50, 10]


def test_las_dos_pantallas_tienen_su_boton_de_excel():
    """Dueña 17/08/2026: "de todos lados deberia poder bajar a excel"."""
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    for archivo, texto in (("parado.html", "Bajar a Excel"),
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
    assert "'FCP'" in s, "la sigla, que es lo que entra en la columna"


def test_los_grupos_chicos_se_unen_al_LEER_y_no_al_guardar():
    """Unirlos en el refresh perdería el dato crudo de Asinfo. Uniéndolos en la
    lectura, el día que uno crezca se separa cambiando una línea."""
    import inspect
    assert "Franela" not in inspect.getsource(queries.actualizar)
    assert "Franela" in inspect.getsource(queries.items)


def test_la_pantalla_de_saldos_ordena_por_puntos(monkeypatch):
    """⭐ Dueña 24/08/2026: "idem para la pantalla de saldos". La pregunta que
    se hace el que abre esta lista es a qué tela conviene ir, y 300 kg de una
    tela de 10 puntos valen más que 3.000 de una de 1. Ordenada por kilos ponía
    arriba justo lo que sale solo."""
    monkeypatch.setattr(
        queries, "puntos_por_tela",
        lambda: {"Microfibra": {"puntos": 10, "nivel_nombre": "Difícil"},
                 "Fleece 102": {"puntos": 1, "nivel_nombre": "Fácil"}})
    filas = queries.con_puntos([
        {"subcategoria": "Fleece 102", "categoria": "Fleece", "stock_kg": 2900,
         "kg_segunda": 0},
        {"subcategoria": "Microfibra", "categoria": "Poliester", "stock_kg": 300,
         "kg_segunda": 0},
    ])
    assert [f["subcategoria"] for f in filas] == ["Microfibra", "Fleece 102"], (
        "300 kg a 10 puntos van ARRIBA de 2.900 kg a 1")
    assert filas[0]["puntos_fila"] == 3000 and filas[1]["puntos_fila"] == 2900
    assert filas[0]["puntos"] == 10 and filas[0]["nivel"] == "Difícil"


def test_una_tela_sin_puntaje_vale_uno_y_no_cero(monkeypatch):
    """⚠ Un kilo vendido nunca puede contar cero. Pasa sólo si la cohorte
    creció después de congelar los puntos."""
    monkeypatch.setattr(queries, "puntos_por_tela", dict)
    filas = queries.con_puntos([
        {"subcategoria": "Tela nueva", "categoria": "Jersey", "stock_kg": 100,
         "kg_segunda": 0}])
    assert filas[0]["puntos"] == 1 and filas[0]["puntos_fila"] == 100


def test_la_pantalla_de_saldos_muestra_lo_que_vale_cada_tela():
    import inspect
    from pathlib import Path
    html = ((Path(__file__).resolve().parent.parent / "modules" / "analisis" /
             "templates" / "analisis" / "parado.html").read_text(encoding="utf-8"))
    assert ">Vale<" in html and ">Puntos<" in html
    # ⚠ "Vale" no puede esconderse en el celular: es la columna que decide.
    import re
    col = re.search(r'<th[^>]*>Vale<', " ".join(html.split()))
    assert col and "opt" not in col.group(0)
    for vista in (views.parado, views.mis_telas, views.parado_csv):
        assert "con_puntos" in inspect.getsource(vista), (
            f"{vista.__name__} tiene que traer los puntos")


def test_la_hoja_del_vendedor_dice_lo_que_vale_cada_tela(monkeypatch):
    """⭐ Dueña 24/08/2026: "es el único papel que se lleva a la calle y es
    justo donde falta". Sin los puntos, el que sale con la hoja en la mano no
    sabe cuál de las telas de ese cliente vale diez veces más que la de al
    lado."""
    filas = [
        {"codigo_cli": "AAA", "nombre": "Cliente", "provincia": "Guayas",
         "vend_pc": "RMY", "subcategoria": "Fleece 102", "kg_cliente": 10,
         "ultima_compra": None, "anio": date.today().year, "colores": 1,
         "kg_parado": 2900, "colores_parados": "BLA"},
        {"codigo_cli": "AAA", "nombre": "Cliente", "provincia": "Guayas",
         "vend_pc": "RMY", "subcategoria": "Microfibra", "kg_cliente": 10,
         "ultima_compra": None, "anio": date.today().year, "colores": 1,
         "kg_parado": 300, "colores_parados": "NEG"},
    ]
    monkeypatch.setattr(
        queries.db, "fetch_all",
        _con_puntos(filas, {"Fleece 102": 1, "Microfibra": 10}))
    c = queries.por_cliente()["clientes"][0]
    assert [t["subcategoria"] for t in c["telas"]] == ["Microfibra", "Fleece 102"], (
        "primero la que más puntos da, no la que más pesa")
    assert c["telas"][0]["puntos"] == 10
    assert c["puntos_potencial"] == 300 * 10 + 2900 * 1

    from pathlib import Path
    html = ((Path(__file__).resolve().parent.parent / "modules" / "analisis" /
             "templates" / "analisis" / "parado_clientes.html")
            .read_text(encoding="utf-8"))
    import re
    col = re.search(r"<th[^>]*>Vale<", " ".join(html.split()))
    assert col, "la hoja tiene que traer la columna Vale"
    # ⚠ Ni en el celular ni en el papel: es la cifra que decide.
    assert "opt" not in col.group(0) and "col" not in col.group(0)


# ── La competencia ──────────────────────────────────────────────────────────

#: Qué tela representa a cada grupo en los datos falsos. Los tests hablan en
#: grupos (Jersey, Fleece) porque así se lee la regla, pero el puntaje vive en
#: la TELA: el helper traduce.
_TELA_DE = {"Jersey": "Jersey 3", "Fleece": "Fleece 102"}


def _competencia_falsa(monkeypatch, vendido=None, override=None, total_pct="100",
                       semanas=None, meses=None, base=None, puntos=None):
    filas = [
        {"categoria": "Jersey", "subcategoria": "Jersey 3", "color": "NEG",
         "stock_kg": 6000, "kg_segunda": 0, "kg_vendidos": 0, "clientes": 1},
        {"categoria": "Fleece", "subcategoria": "Fleece 102", "color": "BLA",
         "stock_kg": 4000, "kg_segunda": 0, "kg_vendidos": 0, "clientes": 1},
    ]
    # ⭐ Por defecto TODA tela vale 1 punto: así los tests que hablan de kilos
    # siguen midiendo lo que decían medir (un punto = un kilo) y los que hablan
    # de puntos pasan un `puntos` distinto y sólo cambia eso.
    puntos = puntos or {}
    pfilas = [{"subcategoria": f["subcategoria"], "categoria": f["categoria"],
               "kg_base": f["stock_kg"] + f["kg_vendidos"], "kg_12m": 1000,
               "meses": 1, "nivel": 1,
               "puntos": puntos.get(f["subcategoria"], 1)} for f in filas]

    def _con_tela(filas_v):
        salida = []
        for r in filas_v or []:
            r = dict(r)
            r.setdefault("subcategoria", _TELA_DE.get(r.get("categoria"), ""))
            salida.append(r)
        return salida

    def fake(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "parado_meta" in s:
            return [{"categoria": k, "pct": v} for k, v in (override or {}).items()]
        if "date_trunc('week'" in s:
            return semanas or []
        if "date_trunc('month'" in s:
            return meses or []
        if "parado_punto" in s:
            return pfilas
        if "parado_venta" in s:
            return _con_tela(vendido)
        if "parado_share" in s:
            return []
        if "parado_base" in s:
            # la meta congelada del día de la largada; vacía = todavía no largó
            return [{"categoria": k, "kg": v, "fijada_el": date(2026, 8, 25)}
                    for k, v in (base or {}).items()]
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


def test_no_hay_meta_por_vendedor(monkeypatch):
    """⭐ Dueña 24/08/2026: "ya que es por puntos, saquemos la meta, ideal es +
    puntos gana".

    ⚠ Y no era sólo simplificar: la meta ya no decidía NADA. Los siete tenían
    la misma (los puntos totales sobre 7), así que ordenar por "% de su meta"
    era dividir a todos por la misma constante — exactamente el mismo orden que
    ordenar por puntos. Era una cuenta de más en la pantalla."""
    c = _competencia_falsa(monkeypatch, puntos={"Jersey 3": 10, "Fleece 102": 1})
    for r in c["ranking"]:
        assert "meta" not in r, "no puede quedar una meta por vendedor"
        assert "pct" not in r, "ni un % contra una meta"
    assert c["puntos_en_juego"] == 64000, (
        "lo que sí queda es cuántos puntos hay en juego, como referencia")


def test_el_ranking_va_por_puntos(monkeypatch):
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Quintero Jose", "categoria": "Jersey", "kg": 100,
         "ultima": None},
        {"vendedor": "Intela", "categoria": "Jersey", "kg": 50, "ultima": None},
    ])
    assert c["ranking"][0]["vendedor"] == "Quintero Jose"
    assert c["ranking"][0]["puesto"] == 1
    assert c["ranking"][0]["pct_lider"] == 100, "el primero es la vara"
    assert c["ranking"][1]["pct_lider"] == 50, "y el resto, contra él"


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


def test_el_vendedor_llega_a_la_competencia_desde_su_portal():
    """El link vive en la barra de abajo de /mi-cartera, que es lo único que el
    vendedor ve. Dice "Competencia" y lleva al tablero: el rótulo y el destino
    son la misma cosa (dueña 25/08/2026: "que el boton diga competencia no
    saldos"). Sin el link, la pantalla abierta no la encuentra nadie."""
    from pathlib import Path
    barra = Path("modules/mi_cartera/templates/mi_cartera/base.html").read_text()
    assert 'href="/analisis/competencia' in barra
    assert ">Competencia</a>" in barra


def test_las_metas_no_cuelgan_del_prefijo_abierto(app):
    """⚠ El allowlist de vendedores matchea por segmento: todo lo que cuelgue de
    `/analisis/competencia/` les queda abierto. La pantalla de metas vive
    afuera para que sean dos cierres y no uno."""
    import scope_vendedor
    rutas = {r.rule for r in app.url_map.iter_rules()}
    assert "/analisis/metas" in rutas
    assert "/analisis/competencia/metas" not in rutas
    # ⚠ Con el prefijo ya abierto, lo que hay que fijar es que las metas NO
    # cuelguen de él: el matcheo es por segmento y las abriría de una.
    assert not any(p.startswith("/analisis/metas")
                   for p in scope_vendedor.PREFIJOS_PERMITIDOS)


def test_al_vendedor_se_le_abrio_la_competencia_y_nada_mas():
    """⭐ Dueña 25/08/2026, el día de la largada: "abrilo para vendedores". La
    pantalla estuvo hecha y frenada desde el 17/08.

    ⚠ Lo que NO puede pasar nunca es que se abra /analisis a secas o
    /analisis/parado: ahí está la cartera de TODOS los vendedores."""
    import scope_vendedor
    abiertos = [p for p in scope_vendedor.PREFIJOS_PERMITIDOS
                if p.startswith("/analisis")]
    assert abiertos == ["/analisis/competencia"], (
        f"la Competencia y nada más de /analisis: {abiertos}")


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
                  "kg": 500, "puntos": 500}])
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
    assert [c["cod"] for c in t[0]["colores_lista"]] == ["LIF", "CAR"], \
        "la pantalla los recibe uno por uno, para poner el nombre al lado"
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

def test_un_kilo_dificil_vale_mas_que_uno_facil(monkeypatch):
    """⭐ Dueña 24/08/2026, después de medir las 98 telas: "si una tela es
    fácil de vender deberíamos dar menos puntos". Dos vendedores que sacan los
    MISMOS kilos no van empatados si uno sacó lo que sale solo y el otro lo que
    no salió en un año."""
    c = _competencia_falsa(
        monkeypatch,
        puntos={"Jersey 3": 10, "Fleece 102": 1},
        vendido=[
            {"vendedor": "Intela", "categoria": "Jersey", "kg": 500,
             "ultima": None},
            {"vendedor": "Lopez Felipe", "categoria": "Fleece", "kg": 500,
             "ultima": None},
        ])
    intela = next(r for r in c["ranking"] if r["vendedor"] == "Intela")
    lopez = next(r for r in c["ranking"] if r["vendedor"] == "Lopez Felipe")
    assert intela["kg"] == lopez["kg"] == 500, "los mismos kilos"
    assert intela["puntos"] == 5000 and lopez["puntos"] == 500
    assert intela["puesto"] < lopez["puesto"], (
        "el que sacó lo difícil tiene que ir adelante")


def test_la_meta_en_puntos_sale_de_las_telas_y_no_del_grupo(monkeypatch):
    """⚠ La bolsa de un grupo NO se puede sacar de sus kilos: adentro de un
    mismo grupo conviven telas de 1 punto y de 10."""
    c = _competencia_falsa(monkeypatch,
                           puntos={"Jersey 3": 10, "Fleece 102": 1})
    jersey = next(g for g in c["grupos"] if g["grupo"] == "Jersey")
    fleece = next(g for g in c["grupos"] if g["grupo"] == "Fleece")
    assert jersey["puntos_base"] == 60000    # 6.000 kg × 10
    assert fleece["puntos_base"] == 4000     # 4.000 kg × 1
    assert c["puntos_en_juego"] == 64000
    assert jersey["kg"] < fleece["kg"] * 2 < jersey["puntos_base"], (
        "un grupo puede tener pocos kilos y muchos puntos")


def test_ya_no_hay_tope_por_grupo(monkeypatch):
    """⭐ El tope existía para obligar a tocar los ocho grupos. Los puntos hacen
    ese trabajo sin tener que explicar un tope: adentro de un grupo hay telas
    que salen solas y telas que no salió una en un año, y el tope las igualaba
    a todas. Dueña 24/08/2026."""
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Intela", "categoria": "Jersey", "kg": 5000, "ultima": None}])
    intela = next(r for r in c["ranking"] if r["vendedor"] == "Intela")
    jersey = next(d for d in intela["detalle"] if d["grupo"] == "Jersey")
    assert jersey["puntos"] == 5000, "el kilo cuenta entero, no cortado"
    assert "contado" not in intela, "no queda rastro del tope"
    assert "meta" not in jersey, "ni de la meta por grupo"


def test_el_kilo_igual_sale_de_la_bodega(monkeypatch):
    """El termómetro de la fábrica sigue siendo de KILOS: es la bodega, no el
    marcador."""
    c = _competencia_falsa(monkeypatch, vendido=[
        {"vendedor": "Intela", "categoria": "Jersey", "kg": 5000, "ultima": None}])
    jersey = next(g for g in c["grupos"] if g["grupo"] == "Jersey")
    assert jersey["liquidado"] == 5000
    assert c["liquidado"] == 5000


def test_los_puntos_de_un_vendedor_son_la_suma_de_sus_grupos(monkeypatch):
    c = _competencia_falsa(
        monkeypatch,
        puntos={"Jersey 3": 10, "Fleece 102": 1},
        vendido=[
            {"vendedor": "Intela", "categoria": "Jersey", "kg": 100,
             "ultima": None},
            {"vendedor": "Intela", "categoria": "Fleece", "kg": 200,
             "ultima": None},
        ])
    intela = next(r for r in c["ranking"] if r["vendedor"] == "Intela")
    assert intela["kg"] == 300
    assert intela["puntos"] == 1200          # 100×10 + 200×1
    assert sum(d["puntos"] for d in intela["detalle"]) == intela["puntos"]


def test_el_ranking_ordena_por_puntos_y_no_por_kilos(monkeypatch):
    """Uno con 5.000 kg de tela fácil tiene que ir DETRÁS de otro con menos
    kilos pero de la difícil."""
    c = _competencia_falsa(
        monkeypatch,
        puntos={"Jersey 3": 1, "Fleece 102": 10},
        vendido=[
            {"vendedor": "Intela", "categoria": "Jersey", "kg": 5000,
             "ultima": None},
            {"vendedor": "Lopez Felipe", "categoria": "Fleece", "kg": 800,
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
    assert "No todos los kilos valen igual" in html, (
        "la regla del puntaje tiene que estar escrita")
    assert "meses de venta hay" in html, (
        "y con qué se mide difícil, o el puntaje parece arbitrario")
    assert "el que más puntos hace" in html
    assert "No hay meta" in html, (
        "que no haya meta es una regla, no una omisión (dueña 24/08/2026)")
    assert "no por kilos" not in html, (
        "todo se mide EN kilos; decir que no, confunde (dueña 17/08/2026)")


def test_la_fila_del_vendedor_se_abre_y_muestra_sus_grupos():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "competencia.html").read_text(encoding="utf-8")
    assert "abrirVend" in html and 'class="vdet"' in html
    for col in ("Vendió", "Puntos"):
        assert col in html
    assert "Su meta" not in html, "la meta se fue de la pantalla"


def test_la_lista_de_telas_muestra_lo_que_vale_cada_una():
    """⚠ `telas_a_sacar` recibe los puntos por parámetro: si los buscara sola,
    sería una segunda lectura que puede contradecir a la del tablero. La
    pantalla tiene que pasárselos."""
    import inspect

    from modules.analisis import views
    fuente = inspect.getsource(views.competencia)
    assert "puntos_por_tela()" in fuente, (
        "la pantalla tiene que pasarle los puntos a la lista de telas")
    t = queries.telas_a_sacar(
        [{"subcategoria": "Kiana", "categoria": "Poliester", "color": "CAR",
          "stock_kg": 100},
         {"subcategoria": "Jersey 3", "categoria": "Jersey", "color": "NEG",
          "stock_kg": 300}],
        {"Kiana": {"puntos": 10, "nivel_nombre": "Difícil"},
         "Jersey 3": {"puntos": 1, "nivel_nombre": "Fácil"}})
    assert [x["tela"] for x in t] == ["Kiana", "Jersey 3"], (
        "ordena por puntos: 100 kg × 10 vale más que 300 × 1")
    assert t[0]["puntos"] == 10 and t[0]["puntos_total"] == 1000
    assert t[1]["puntos_total"] == 300


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
    for imprescindible in (">Vendedor<", ">Puntos<", ">Cliente<"):
        col = re.search(r'<th[^>]*' + re.escape(imprescindible), comp)
        assert col, f"falta la columna {imprescindible}"
        assert "opt" not in col.group(0), (
            f"{imprescindible} no puede esconderse en el celular")


def test_las_columnas_se_esconden_por_clase_y_no_por_posicion():
    """Escondiendo `td:nth-child(5)`, una columna nueva en el medio correría
    todas y el que desaparecería sería otro — sin ningún error.

    ⚠ `nth-child` SÍ se usa para el rayado de filas, que no depende de qué
    columna es: por eso el test mira sólo las reglas que esconden algo."""
    import re
    from pathlib import Path
    base = ((Path(__file__).resolve().parent.parent / "modules" / "analisis" /
             "templates" / "analisis" / "base.html").read_text(encoding="utf-8"))
    for regla in re.findall(r"([^{}]+)\{([^}]*display\s*:\s*none[^}]*)\}", base):
        assert "nth-child" not in regla[0], (
            f"esconde por posición: {regla[0].strip()}")


def test_en_el_celular_la_calidad_viaja_pegada_a_la_tela():
    """Con la columna Categoría la tabla mide 563 px y el teléfono 390: los
    kilos se cortaban afuera. Abajo de 560 la columna se esconde y la píldora
    va al lado del nombre. Y la copia NO puede ensuciar ni el orden ni el
    Excel — la columna Tela decía "Jersey Fancy PRI"."""
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    base = (carpeta / "base.html").read_text(encoding="utf-8")
    parado = (carpeta / "parado.html").read_text(encoding="utf-8")

    assert ".qm{display:none}" in base, "la copia se ve también en la pantalla grande"
    assert "#tabla .cal{display:none!important}" in base, (
        "la columna Categoría no se esconde en el celular")
    # ⚠ `block`, no `inline`: al lado del nombre la columna medía 244 px y la
    # tabla se salía igual de la pantalla. Debajo, vale lo que el nombre.
    assert ".qm{display:block" in base, "en el celular la píldora no aparece"
    # en el teléfono chico se achica la letra, NO se parte el nombre
    assert "@media (max-width: 380px)" in base
    assert "#tabla{font-size:11px}" in base

    assert 'class="ord cal" data-i="3"' in parado, (
        "el encabezado de Categoría no se esconde con su columna")
    assert '<span class="qm">{{ calidad }}</span></td>' in parado
    # una sola vez se decide PRI/SEG: dos copias del if se despegan
    assert parado.count('<span class="q seg">SEG</span>') == 2, (
        "las píldoras se arman en más de un lugar")
    # el orden y el Excel leen el texto sin la copia
    assert "c.querySelectorAll('.qm').forEach(e => e.remove())" in parado
    assert "return texto(td).trim().toLowerCase();" in parado
    assert "limpia(texto(td).replace" in parado
    assert "limpia(td.textContent" not in parado, "el Excel se lleva la píldora"


def test_la_meta_congelada_no_se_mueve_cuando_se_mueve_el_stock(monkeypatch):
    """Dueña 18/08/2026, después de ver el número moverse solo: el mismo día,
    sin una sola venta, «había al arrancar» pasó de 52.407 a 51.654 kg —753 kg
    de ajustes de bodega—. Con la meta congelada, el stock de hoy puede hacer
    lo que quiera: la meta y el % de cada uno salen de los kilos del día de la
    largada."""
    congelada = {"Jersey": 6000, "Fleece": 4000}
    c = _competencia_falsa(monkeypatch, base=congelada)
    assert c["kg_al_largar"] == 10000
    assert c["meta_kg"] == 10000            # 100% de lo congelado
    assert c["meta_fijada_el"] == date(2026, 8, 25)

    # ahora la bodega dice otra cosa —un ajuste, no una venta— y la meta NO se
    # entera: sigue valiendo lo mismo
    c2 = _competencia_falsa(monkeypatch, base={"Jersey": 6000, "Fleece": 3000})
    assert c2["meta_kg"] == 9000            # sólo cambia si cambia la BASE
    assert c["meta_kg"] != c2["meta_kg"]
    # …y sin base, la pantalla es una previa que se calcula con lo de hoy
    previa = _competencia_falsa(monkeypatch)
    assert previa["meta_fijada_el"] is None
    assert previa["kg_al_largar"] == 10000


def test_un_grupo_despejado_entero_conserva_su_meta(monkeypatch):
    """Si un grupo se vende del todo deja de estar en la foto. Sin esto se
    caía de la tabla y la meta total se achicaba justo cuando alguien había
    hecho bien el trabajo — el que lo despejó perdía el puntaje."""
    c = _competencia_falsa(monkeypatch,
                           base={"Jersey": 6000, "Fleece": 4000, "Lycra": 2000})
    grupos = {g["grupo"]: g for g in c["grupos"]}
    assert "Lycra" in grupos, "el grupo despejado se cayó de la tabla"
    assert grupos["Lycra"]["meta_kg"] == 2000
    assert grupos["Lycra"]["kg"] == 0
    assert c["meta_kg"] == 12000


def test_la_base_se_fija_una_sola_vez_y_recien_desde_la_largada(monkeypatch):
    """Se escribe en el primer refresco del día de la largada o después. Si se
    escribiera antes, congelaría kilos de una semana en la que nadie estaba
    compitiendo; si se reescribiera, la meta volvería a moverse —que es
    justamente lo que se vino a arreglar."""
    escritos = []

    def fake_execute(sql, params=None, conn=None):
        if "parado_base" in " ".join(sql.split()):
            escritos.append(params)

    monkeypatch.setattr(queries.db, "execute", fake_execute)
    monkeypatch.setattr(queries, "config", lambda k, d=None: "2026-08-25")
    monkeypatch.setattr(queries, "items", lambda: [
        {"categoria": "Jersey", "stock_kg": 100, "kg_vendidos": 40},
        {"categoria": "Jersey", "stock_kg": 10, "kg_vendidos": 0},
        {"categoria": None, "stock_kg": 5, "kg_vendidos": 0}])

    # antes de la largada no escribe nada
    monkeypatch.setattr(queries, "today_ec", lambda: date(2026, 8, 24))
    monkeypatch.setattr(queries.db, "fetch_all", lambda *a, **k: [])
    assert queries._fijar_base() is None
    assert escritos == []

    # el día de la largada sí, y con stock + lo ya vendido (110 + 40)
    monkeypatch.setattr(queries, "today_ec", lambda: date(2026, 8, 25))
    base = queries._fijar_base()
    assert base == {"Jersey": 150}, base
    assert escritos == [("Jersey", 150.0, date(2026, 8, 25))]

    # ya fijada: no la vuelve a tocar
    escritos.clear()
    monkeypatch.setattr(queries.db, "fetch_all",
                        lambda *a, **k: [{"categoria": "Jersey"}])
    assert queries._fijar_base() is None
    assert escritos == []


def test_el_desplegable_va_por_codigo_y_el_nombre_no_se_pierde():
    """Dueña 19/08/2026: "cliente podemos poner solo codigo, no hace falta el
    nombre?". Con el nombre, una fila del desplegable ocupaba cuatro renglones
    en el celular y la tabla no entraba en 390 px.

    Lo que NO puede pasar es que el nombre desaparezca del todo: sigue en el
    `title` del código y en `data-buscar`, así que buscar "ARELLANO" sigue
    encontrando la fila aunque en pantalla diga WFA."""
    from pathlib import Path
    parado = ((Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis" / "parado.html")
              .read_text(encoding="utf-8"))
    detalle = parado[parado.index("{% if cand %}"):]
    assert "<th>Cliente</th>" not in detalle, "el desplegable volvió a traer el nombre"
    assert "{{ c.nombre }}" not in detalle.replace('title="{{ c.nombre }}"', ""), (
        "el nombre se imprime como columna")
    assert 'title="{{ c.nombre }}"' in detalle, "el nombre se perdió del todo"
    # el buscador lo sigue teniendo: es lo que hace que se pueda buscar por nombre
    assert "map(attribute='nombre')" in parado

    # la hoja del vendedor SÍ lleva el nombre grande: ahí la unidad es el cliente
    hoja = ((Path(__file__).resolve().parent.parent / "modules" / "analisis" /
             "templates" / "analisis" / "parado_clientes.html")
            .read_text(encoding="utf-8"))
    assert "{{ c.nombre }}" in hoja


def test_en_la_app_del_vendedor_no_se_repite_su_propio_codigo():
    """Dueña 19/08/2026: "el vendedor ya sabe quién es". En `/mi-cartera` de
    saldos sólo ve SUS clientes, así que la columna Vend. decía FL1 en todas
    las filas — una columna entera para no decir nada. En la oficina se queda:
    ahí se mezclan los seis y el mostrador."""
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    parado = (carpeta / "parado.html").read_text(encoding="utf-8")
    i = parado.index("<th>Provincia</th>")
    assert "{% if not mia %}<th>Vend.</th>{% endif %}" in parado[i:i + 120], (
        "la columna Vend. no está condicionada al modo oficina")
    assert "{% if not mia %}<td class=\"g vnd\">" in parado, (
        "el encabezado se esconde pero la celda no: la fila se corre")

    hoja = (carpeta / "parado_clientes.html").read_text(encoding="utf-8")
    j = hoja.index('<p class="meta">')
    assert "{% if not mia %}" in hoja[j:j + 160], (
        "la hoja propia sigue repitiendo el código del vendedor")


def test_la_pantalla_no_explica_la_formula_vieja_de_metas():
    """La bajada de «Por grupo» quedó describiendo la PRIMERA fórmula —«la meta
    de cada grupo es su propio peso en el parado»—, la que la dueña cazó con
    "¿por qué 8k kilos?". Se cambió el cálculo y el texto se quedó: contradecía
    a la columna Meta de la misma tabla, que dice 100% en todas las filas."""
    from pathlib import Path
    html = ((Path(__file__).resolve().parent.parent / "modules" / "analisis" /
             "templates" / "analisis" / "competencia.html")
            .read_text(encoding="utf-8"))
    import re
    texto = re.sub(r"\{#.*?#\}", " ", html, flags=re.S)
    assert "su propio peso en el parado" not in texto
    assert "Dónde están los puntos" in texto


def test_la_fecha_del_refresco_se_muestra_en_hora_de_ecuador():
    """`actualizado` se guarda con NOW() y el servidor corre en UTC. Sin el
    filtro, a las 19:41 del 18 en la fábrica la pantalla decía "datos al
    19/08/2026 00:42": mañana. Lo vio la dueña el mismo día que se estrenó."""
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    for nombre in ("parado.html", "parado_clientes.html"):
        html = (carpeta / nombre).read_text(encoding="utf-8")
        assert "estado.actualizado.strftime" not in html, (
            f"{nombre} muestra la hora del servidor, no la de Ecuador")
        assert "estado.actualizado | hora_ec" in html


def test_la_hoja_en_el_celular_deja_solo_la_tela_y_la_fecha():
    """Dueña 19/08/2026, mirando una ficha en el teléfono: "acá solo el cliente
    y último día que compró, más no hace falta" — y "y chiquito". En la mano,
    la hoja sirve para saber a quién llamar y hace cuánto no compra; los
    colores y las dos cifras de kilos son para armar la recorrida en el
    escritorio o en el papel, y ahí siguen estando."""
    import re
    from pathlib import Path
    hoja = ((Path(__file__).resolve().parent.parent / "modules" / "analisis" /
             "templates" / "analisis" / "parado_clientes.html")
            .read_text(encoding="utf-8"))
    movil = re.search(r"@media screen and \(max-width:\s*\d+px\)\s*\{(.+?)\n\}",
                      hoja, re.S)
    assert movil, "la hoja no tiene bloque de celular"
    css = movil.group(1)
    assert ".cli .col,.cli .hay,.cli .compro{display:none}" in css, (
        "en el celular siguen los colores y los kilos")
    assert ".cli h3 .kg{display:none}" in css, "los kilos siguen en el encabezado"
    # las celdas se agarran por clase, no por posición
    for clase in ("col", "hay", "compro", "ult"):
        assert f'{clase}"' in hoja, f"la celda {clase} no tiene clase"
    # en papel NO cambia nada: el bloque es `screen`
    assert "@media screen and (max-width" in hoja
    imp = hoja[hoja.index("@media print{"):]
    for clase in (".col", ".hay", ".compro"):
        assert f"{clase}{{display:none" not in imp, (
            f"el papel también se está comiendo {clase}")


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
        r"querés|vos|tuyo|tuyos|tus|llevás|ponés|dejá|mirá|andá|hacé|sacá|"
        r"apretá|revisá|escribí|abrí|and[áa]te)\b", re.I)
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
    # el motivo se ve por la columna "De 2ª": si tiene kilos ahí, entró por
    # segunda. La píldora que lo decía se fue con la columna Estado.
    assert "f.kg_segunda" in html


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
    # ⭐ En el celular NO vuelve a partirse: entra porque la columna Estado ya
    # no existe.
    assert "#tabla td.tela{white-space:normal}" not in css[i:]
    from pathlib import Path as _P
    html = (_P(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert ">Estado</th>" not in html


def test_una_fila_sin_nada_se_va_pero_la_liquidada_se_queda():
    """Dueña 18/08/2026: "más de 0, si no hay nada para qué están?".

    ⚠ La condición lleva las DOS cosas: 0 kg Y 0 vendidos. Una fila con 0 kg
    que SÍ vendió algo es un ítem liquidado entero, y ésa se queda — es la que
    muestra que la competencia funcionó. Confundirlas borraría la buena
    noticia."""
    from pathlib import Path
    sql = (Path(__file__).resolve().parent.parent / "migrations" /
           "0203_saldos_limpieza.sql").read_text(encoding="utf-8")
    assert "stock_kg <= 0 AND kg_vendidos <= 0" in sql
    assert sql.count("kg_vendidos <= 0") == 2, "las dos tablas con la misma regla"


def test_los_kilos_chicos_no_se_muestran_como_cero():
    """Un ítem de 0,4 kg mostrado como "0" parece un error de la pantalla — fue
    literalmente la pregunta de la dueña."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    assert "f.stock_kg | num_es(1) if f.stock_kg < 1" in html


def test_la_foto_no_pierde_el_grupo_al_liquidarse_un_item():
    """Un ítem vendido entero ya no viene de Asinfo, así que su grupo quedaba
    en NULL y la fila aparecía con "—" justo en las filas "resuelto", que son
    las que uno mira para ver si esto funciona."""
    import inspect
    fuente = inspect.getsource(queries.actualizar)
    assert "grupo_previo" in fuente
    assert '(p or {}).get("categoria") or grupo_previo.get(k)' in fuente


# ── Las mejoras de la tabla ─────────────────────────────────────────────────

def _html_parado():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")


def test_los_indices_de_orden_coinciden_con_las_columnas():
    """⚠ Cada `th` ordena por su `data-i`. Al sacar una columna del medio, si
    los índices no se renumeran, tocar "Clientes" ordena por otra cosa — y no
    da ningún error: sólo ordena mal."""
    import re
    html = _html_parado()
    enc = re.search(r"<thead><tr>\n(.*?)</tr></thead>", html, re.S).group(1)
    idx = [int(x) for x in re.findall(r'data-i="(\d+)"', enc)]
    assert idx == list(range(len(idx))), f"índices salteados o repetidos: {idx}"
    assert len(idx) == int(re.search(r'colspan="(\d+)"', html).group(1))


def test_lo_vendido_no_va_apilado_en_la_celda_de_los_kilos():
    """Primero fue una columna vacía ("—" en las 711 filas); después la puse
    debajo del stock como "−43 vendidos" y tampoco se entendía: dos números de
    kilos apilados sin decir qué era cada uno. Dueña 18/08/2026: "que quiere
    decir x vendidos? deja de ponerme cosas raras".

    Se cuenta igual y se muestra al ABRIR la fila, con la frase entera."""
    html = _html_parado()
    assert ">Vendidos</th>" not in html
    assert "vendidos</div>" not in html
    assert "Desde que arrancó la competencia se vendieron" in html


def test_el_cero_de_clientes_se_marca():
    """Es la señal más accionable de la tabla —"no hay a quién llamar"— y era
    un cero gris como cualquier otro."""
    assert "'sincli' if not f.clientes" in _html_parado()


def test_el_encabezado_queda_fijo_al_scrollear():
    """⚠ Un sticky muere si un ancestro fija la altura: es el mismo error que
    tuvo el appbar de /mi-cartera durante meses, sin dar ningún síntoma."""
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
           "templates" / "analisis" / "base.html").read_text(encoding="utf-8")
    assert "#tabla thead th{position:sticky;top:var(--alto-barra)" in css
    assert "background:#fff" in css, "sin fondo sólido las filas se ven por atrás"
    # sin comentarios: ahí se explica justamente cuál es el error a evitar
    import re as _re
    reglas = _re.sub(r"/\*.*?\*/", " ", css, flags=_re.S)
    assert "height:100%" not in reglas, "un ancestro con altura fija mataría el sticky"


def test_las_filas_alternadas_van_de_a_cuatro():
    """Cada tela son DOS filas (la visible y su detalle), así que el ciclo del
    rayado es de 4 y no de 2: con nth-child(2n) se pintarían los detalles."""
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
           "templates" / "analisis" / "base.html").read_text(encoding="utf-8")
    assert "#tabla tbody tr:nth-child(4n+1) td" in css


def test_abrir_una_fila_cierra_la_anterior():
    html = _html_parado()
    assert "#tabla tr.det.abierta" in html and "o.classList.remove('abierta')" in html


def test_el_encabezado_se_pega_debajo_de_la_barra_y_no_atras():
    """🐛 La barra de arriba también es sticky (top:0, z-index 5) y mide 52 px:
    con el encabezado en top:0 quedaba ESCONDIDO detrás de ella. Se veía sólo
    scrolleando en la pantalla real — el sticky "funcionaba", pero abajo de
    otra cosa."""
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
           "templates" / "analisis" / "base.html").read_text(encoding="utf-8")
    assert "#tabla thead th{position:sticky;top:var(--alto-barra)" in css
    assert "--alto-barra:52px" in css
    i = css.index("@media (max-width: 780px)")
    assert "--alto-barra:84px" in css[i:], (
        "en el celular la barra se parte en dos líneas y tapa el encabezado")


def test_se_puede_filtrar_por_calidad():
    """Dueña 18/08/2026: "porque hay cosas que se vendieron recien?". Porque 305
    de las 307 filas con venta reciente entraron por sus kilos de SEGUNDA: la
    tela se vende bien, lo que no sale es la segunda. Mezcladas la lista se
    contradice sola —dice "hace 12 meses que no se vende" al lado de una venta
    de la semana pasada—, así que se pueden separar."""
    html = _html_parado()
    assert 'id="calidad"' in html
    assert "data-cal=" in html
    assert "tr.dataset.cal.split(' ').includes(cal)" in html, (
        "una fila puede ser PRI y SEG a la vez: el filtro tiene que mirar la "
        "lista, no comparar el string entero")
    assert "Sólo SEG" in html


def test_la_calidad_es_una_columna_y_no_una_pildora_suelta():
    """Dueña 18/08/2026: "no asi. no es eficiente. tiene que haber columna PRI
    SEG y que se pueda filtrar". Una píldora con kilos al costado no se ordena
    ni se filtra; una columna sí, y se lee en vertical."""
    todo = " ".join(_html_parado().split())
    # sólo la tabla principal: el resumen por grupo SÍ tiene una columna con
    # los kilos de segunda de cada grupo, y ahí está bien
    html = todo[todo.index('<table id="tabla">'):]
    assert ">Categoría</th>" in html
    assert ">De 2ª</th>" not in html
    assert 'class="q pri">PRI' in html and 'class="q seg">SEG' in html
    # ⚠ Se chequea la IDEA, no la frase: los textos se acortaron el 20/08/2026
    # (dueña: "todo muy wordy") y un test pegado a la redacción obliga a elegir
    # entre el test y la copia. Lo que no puede faltar es que la bajada explique
    # por qué hay filas con una venta reciente: son los kilos SEG.
    assert "paradas" in todo and "SEG</b>" in todo, (
        "la bajada explica por qué hay filas con venta reciente")


def test_los_filtros_viven_en_la_direccion_y_se_recuerdan():
    """Dueña 18/08/2026: filtrar, mirar una fila, volver y tener que rearmar
    todo. En la URL además se pueden mandar por WhatsApp."""
    html = _html_parado()
    assert "history.replaceState" in html
    assert "localStorage.setItem" in html
    assert "new URLSearchParams(location.search)" in html


def test_la_direccion_le_gana_a_lo_guardado():
    """Si alguien te mandó un link es porque quiere que veas ESO, no lo que vos
    tenías filtrado ayer."""
    import re
    html = " ".join(_html_parado().split())
    i = html.index("function ponerFiltros()")
    cuerpo = html[i:i + 700]
    assert re.search(r"if \(\[\.\.\.url\.keys\(\)\].*?\) \{.*?url\.get", cuerpo), (
        "primero la URL; el localStorage sólo si la URL no trae nada")


def test_el_grupo_se_aplica_antes_que_el_subgrupo():
    """⚠ `grupoCambio()` borra el subgrupo que no pertenece al grupo elegido.
    Si se restauran en orden alfabético, el subgrupo se pone y el grupo lo
    borra — el link compartido abre a medias y nadie sabe por qué."""
    html = " ".join(_html_parado().split())
    assert "if (c === 'grupo') { e.value = v[c]; grupoCambio(); }" in html


def test_todo_se_cuenta_desde_la_largada_y_no_desde_que_entro_cada_fila():
    """Dueña 18/08/2026: "hace todo desde 25/08".

    Antes cada fila medía desde su propia `fecha_marcado` (13/08), así que la
    pantalla de Saldos y la de Competencia daban dos números distintos para "lo
    vendido" y el primero que los comparara no iba a saber cuál creer.
    `fecha_marcado` sigue guardada —es cuándo entró cada tela— pero ya no manda
    sobre la cuenta."""
    import inspect
    fuente = inspect.getsource(queries.actualizar)
    assert 'config("largada"' in fuente
    assert "_fecha(v[\"fecha\"]) >= desde_f" in fuente
    assert "MIN(fecha_marcado)" not in fuente, (
        "ya no se arranca desde la fila más vieja de la cohorte")


def test_la_pantalla_dice_desde_cuando_cuenta():
    """Un "vendido" sin fecha al lado invita justamente a la comparación que
    generó la confusión."""
    html = " ".join(_html_parado().split())
    # ⚠ La IDEA, no la frase: el 24/08/2026 el resumen pasó de cinco tarjetas a
    # un renglón y el rótulo quedó partido ("Vendido … · desde el 25/08"). Lo
    # que no puede faltar es la fecha al lado del número.
    assert "Vendido" in html and "desde el 25/08" in html


# ── Intela como una cartera más ─────────────────────────────────────────────

def test_se_puede_mirar_intela_por_separado(monkeypatch):
    """Dueña 18/08/2026: "me haces uno para intela? osea mostrador y todo lo
    que no sea vendedores". Es el 51,3% de las ventas de estas telas y no se
    podía aislar.

    ⚠ Se resuelve con `vend_pc IS NULL` y no inventándole un código: Intela no
    está en `scintela.vendedor` y no debería estarlo, porque no es una
    persona."""
    visto = {}

    def fake(sql, params=None, conn=None):
        # ⚠ Se guarda SÓLO la primera consulta —la de la hoja—: `por_cliente`
        # después pide los puntos, y si se pisara `visto` el test terminaría
        # mirando otra query y pasando o fallando por el motivo equivocado.
        if "sql" not in visto:
            visto["sql"] = " ".join(sql.split())
            visto["params"] = params
        # Una fila de puntaje para que no salga a buscar a Asinfo.
        if "parado_punto" in " ".join(sql.split()):
            return [{"subcategoria": "—", "categoria": None, "kg_base": 0,
                     "kg_12m": 0, "meses": None, "nivel": 1, "puntos": 1}]
        return []

    monkeypatch.setattr(queries.db, "fetch_all", fake)
    monkeypatch.setattr(queries.db, "fetch_one", lambda *a, **k: None)
    queries.por_cliente("INTELA")
    assert "%(vend)s = 'INTELA' AND l.vend_pc IS NULL" in visto["sql"]
    assert visto["params"]["vend"] == "INTELA"


def test_intela_esta_en_los_dos_desplegables():
    from pathlib import Path
    carpeta = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
               "templates" / "analisis")
    for archivo in ("parado.html", "parado_clientes.html"):
        html = (carpeta / archivo).read_text(encoding="utf-8")
        assert "Intela (mostrador)" in html, f"falta en {archivo}"


def test_una_tela_cuyos_clientes_son_del_mostrador_aparece_con_INTELA():
    """El filtro de la pantalla de saldos arma `data-vend` salteando los nulos:
    sin agregar INTELA a mano, una tela que sólo compra el mostrador quedaba
    fuera de todos los filtros por vendedor."""
    html = " ".join(_html_parado().split())
    assert "rejectattr('vend_pc')" in html and "['INTELA']" in html


def _html_hoja():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado_clientes.html").read_text(encoding="utf-8")


def test_los_dos_kilos_de_la_hoja_dicen_de_quien_son():
    """Dueña 18/08/2026: "no se que quiere decir le vendimos / kg en saldo".
    Eran dos cifras de kilos una al lado de la otra sin decir de quién era cada
    una: una es lo que HAY en bodega y la otra lo que ese cliente COMPRÓ."""
    html = _html_hoja()
    assert "Hay para ofrecerle" in html and "Él ya compró" in html
    assert "Kg en saldo</th>" not in html and "Le vendimos</th>" not in html
    assert 'class="anio">en {{ t.anio }}' in html, (
        "sin el año, 'ya compró' no dice si fue en julio o en 2023")


def test_las_fichas_encolumnan_todas_igual():
    """⚠ Sin `table-layout:fixed` cada ficha arma sus columnas según el largo
    de SU contenido: un cliente con "Fleece 96 Sin Perchar" y otro con "Naty"
    quedaban con las cifras en lugares distintos y la hoja se lee saltando."""
    html = _html_hoja()
    assert "table-layout:fixed" in html
    assert "<colgroup>" in html
    import re
    anchos = [int(x) for x in re.findall(r"\.cli col\.c\d\{width:(\d+)%\}", html)]
    assert sum(anchos) == 100, f"los anchos suman {sum(anchos)}%"


def test_una_sola_palabra_para_la_categoria():
    """Dueña 18/08/2026: "otra columna que diga categoria y pones PRI SEG, no
    repetir con segunda". Convivían "calidad", "segunda", "de 2ª" y "SEG" para
    la misma cosa: cuatro nombres obligan a traducir mentalmente en cada
    columna."""
    import re
    html = _html_parado()
    visible = re.sub(r"(\{#.*?#\}|<!--.*?-->|//[^\n]*)", " ", html, flags=re.S)
    for palabra in ("De 2ª", "de segunda", "segunda calidad", "Primera y segunda"):
        assert palabra not in visible, f"quedó «{palabra}» conviviendo con SEG"
    assert ">Categoría</th>" in visible


def test_la_hoja_sale_alfabetica_por_codigo():
    """Dueña 18/08/2026: "cuando es 'a quien ofrecerle que' ordena
    alfabeticamente".

    ⭐ Por CÓDIGO y no por nombre, aunque la ficha muestre el nombre grande: es
    el mismo orden con el que salen la hoja de estado de cuenta de la oficina y
    la de /mi-cartera, y el vendedor tiene el papel y el celular delante a la
    vez. Dos alfabéticos distintos para el mismo cliente son peor que uno."""
    import inspect

    from modules.analisis import views
    for vista in (views.parado_clientes, views.mi_hoja, views.mi_hoja_csv,
                  views.parado_clientes_csv):
        fuente = inspect.getsource(vista)
        assert 'request.args.get("orden") or "codigo"' in fuente, (
            f"{vista.__name__} sigue arrancando por oportunidad")


def test_ordenar_por_oportunidad_sigue_disponible():
    """Se cambia el DEFAULT, no se saca la opción: para decidir por quién
    empezar sigue siendo la mejor."""
    assert "oportunidad" in queries.ORDENES


def test_el_titulo_de_saldos_no_promete_lo_mismo_que_la_otra_pantalla():
    """Dueña 20/08/2026: "¿por qué Saldos se llama 'y a quién llamar', si ya hay
    otra página que dice 'A quién ofrecerle qué'?".

    Las dos muestran los mismos clientes; lo que cambia es el EJE (por tela vs.
    por cliente). Con los dos títulos prometiendo clientes, el que entra no sabe
    cuál abrir."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "parado.html").read_text(encoding="utf-8")
    titulo = html.split("<h1>")[1].split("</h1>")[0]
    assert titulo == "Saldos, tela por tela"
    assert "llamar" not in titulo and "ofrec" not in titulo


def test_el_premio_del_mes_lista_a_los_siete_ordenados_por_kilos(monkeypatch):
    """Dueña 20/08/2026: "acá me gustaría tener a todos ordenados por kg".

    El podio de tres dejaba afuera a la mitad de la tabla —y en el teléfono se
    veía UN nombre, porque las columnas de la derecha se esconden—. Van los
    siete, también los que todavía no vendieron: un cero en la lista dice más
    que no figurar."""
    from datetime import date as _d
    c = _competencia_falsa(monkeypatch, meses=[
        {"mes": _d(2026, 10, 1), "vendedor": "Quintero Jose", "kg": 400},
        {"mes": _d(2026, 10, 1), "vendedor": "Intela", "kg": 900},
    ])
    r = next(m for m in c["meses"] if m["mes"] == _d(2026, 10, 1))["ranking"]
    assert len(r) == len(queries.COMPETIDORES) == 7
    assert [x["vendedor"] for x in r[:2]] == ["Intela", "Quintero Jose"]
    assert [x["kg"] for x in r[:2]] == [900, 400]
    assert [x["puesto"] for x in r] == [1, 2, 3, 4, 5, 6, 7]
    assert all(x["kg"] == 0 for x in r[2:]), "los que no vendieron van con cero"
    # el orden de los empatados en cero lo fija el nombre, no el diccionario
    assert [x["vendedor"] for x in r[2:]] == sorted(x["vendedor"] for x in r[2:])


def test_la_pantalla_dibuja_la_lista_entera_del_mes():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "modules" / "analisis" /
            "templates" / "analisis" / "competencia.html").read_text(encoding="utf-8")
    assert "m.ranking" in html, "sin esto la pantalla sigue mostrando sólo el podio"
    assert "Kg del mes" in html


# ──────────────────────────────────────────────────────────────────────────
# Qué kilo puntúa · migración 0212
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("motivo", "calidad", "cuenta"),
    [
        # La tela × color entera está parada: todos sus kilos entraron a la
        # lista, así que todos puntúan.
        ("parado", "PRI", True),
        ("parado", "SEG", True),
        # ⭐ Entró SÓLO por sus kilos de segunda: la primera de esa misma tela
        # se vende sola y no puede dar puntos. Dueña 24/08/2026: "solo tiene
        # que ponerse la de segunda en la competencia, la de primera no cuenta".
        ("segunda", "SEG", True),
        ("segunda", "PRI", False),
        # Sin calidad en la línea de factura, un ítem de segunda no regala el
        # beneficio de la duda.
        ("segunda", None, False),
        # Cohorte vieja, sin motivo guardado: cuenta todo, como venía.
        (None, "PRI", True),
    ],
)
def test_solo_puntua_el_kilo_de_la_clase_por_la_que_entro(motivo, calidad, cuenta):
    assert queries.cuenta_el_kilo(motivo, calidad) is cuenta


def test_las_devoluciones_restan_en_lo_que_puntua():
    """⭐ Dueña 24/08/2026: "dale contemos devoluciones". La nota de crédito es
    el documento 20 o 451 y hay que RESTARLA: sin esto un vendedor facturaba un
    saldo, sumaba los puntos, el cliente devolvía la tela y los puntos quedaban.

    ⚠ Se imputa al día de la factura MADRE, no al suyo: una devolución de
    octubre de una venta de agosto tiene que caer en agosto."""
    sql = " ".join(asinfo_parado._sql_vendido("2026-08-25").split())
    assert "fc.id_documento IN (7, 251, 20, 451)" in sql
    assert "THEN -dfc.cantidad ELSE dfc.cantidad END" in sql
    assert "COALESCE(pad.fecha, fc.fecha)" in sql
    assert "id_factura_cliente_padre" in sql


def test_la_dificultad_de_la_tela_tambien_es_neta():
    """Con la devolución adentro la tela parece más fácil de colocar de lo que
    es, y se le da MENOS puntaje del que corresponde."""
    sql = " ".join(asinfo_parado.SQL_VENTA_TELA.split())
    assert "fc.id_documento IN (7, 251, 20, 451)" in sql
    assert "THEN -dfc.cantidad ELSE dfc.cantidad END" in sql
    # y trae la SEG aparte, para poder ponerle puntaje propio
    assert "kg_seg_12m" in sql


def test_lo_vendido_trae_la_calidad_de_la_linea_y_no_del_lote():
    """En las líneas de factura `dfc.id_lote` viene en NULL: la calidad está en
    el atributo 2 de la LÍNEA (3 = PRI, 4 = SEG). Verificado contra
    `valor_atributo` el 24/08/2026."""
    sql = " ".join(asinfo_parado._sql_vendido("2026-08-25").split())
    assert "COALESCE(dfc.id_valor_atributo_2, mad.va2) = 4 THEN 'SEG'" in sql
    assert "dfc.id_lote" not in sql


def test_la_devolucion_hereda_la_calidad_y_el_vendedor_de_la_factura_madre():
    """⚠ En las líneas de nota de crédito el atributo de calidad viene en NULL
    (17 de 17, verificado el 24/08/2026) y `v_ventas` no tiene la NC.

    Sin heredar los dos de la madre: la devolución de un kilo SEG se etiquetaba
    PRI y en un ítem de segunda quedaba sin contar —la venta sumaba los puntos y
    la devolución no los restaba—, y el kilo negativo se le restaba a 'Intela',
    que también compite, en vez de al vendedor que lo facturó.

    ⚠ TOP 1 y no un JOIN: la madre puede repetir el mismo producto en dos
    renglones y un JOIN duplicaría los kilos de la devolución.
    """
    sql = " ".join(asinfo_parado._sql_vendido("2026-08-25").split())
    assert "OUTER APPLY (SELECT TOP 1 m.id_valor_atributo_2" in sql
    assert ("ON vx.id_factura_cliente = COALESCE(fc.id_factura_cliente_padre, "
            "fc.id_factura_cliente)") in sql


def test_la_fecha_de_la_madre_es_solo_para_la_nota_de_credito():
    """Con un COALESCE pelado, una factura común con padre cargado se mudaría a
    la fecha del padre y podría salirse de la ventana de la competencia."""
    sql = " ".join(asinfo_parado._sql_vendido("2026-08-25").split())
    assert ("CASE WHEN fc.id_documento IN (20, 451) THEN "
            "COALESCE(pad.fecha, fc.fecha) ELSE fc.fecha END") in sql


def test_el_motivo_de_la_cohorte_se_escribe_una_sola_vez():
    """El INSERT de la cohorte es ON CONFLICT DO NOTHING: sin un UPDATE, los 734
    ítems anteriores a la migración 0212 se quedaban con el motivo en NULL —o
    sea contando toda la primera. Y con `motivo IS NULL` en el WHERE, una vez
    escrito no se vuelve a mover: la regla de un ítem no puede cambiar en la
    mitad de la carrera."""
    import inspect as _i
    fuente = " ".join(_i.getsource(queries.actualizar).split())
    assert "UPDATE scintela.parado_cohorte SET motivo = %s" in fuente
    assert "AND motivo IS NULL" in fuente


def test_las_pantallas_respetan_la_bandera_cuenta():
    """La regla vive en UN lugar (el refresh la escribe en `parado_venta.cuenta`)
    y las pantallas la respetan. Con tres WHERE distintos, tarde o temprano uno
    queda sin actualizar y el ranking y el total dejan de coincidir."""
    import inspect as _i
    fuente = _i.getsource(queries)
    n = fuente.count("FROM scintela.parado_venta v")
    assert n == fuente.count("v.cuenta"), (
        f"{n} lecturas de parado_venta y sólo "
        f"{fuente.count('v.cuenta')} filtran por la bandera")


def test_once_noventa_y_ocho_meses_es_doce():
    """⭐ Dueña 24/08/2026: "11.98 es igual que 12". El corte entre 4 y 10
    puntos está en 12 meses parados, y Jersey Forro Spun daba 11,98: sus
    2.448 kg —el ítem más grande de la lista— valían 4 en vez de 10 por una
    diferencia del 0,2%, menos que el error de medición de la bodega."""
    nivel, meses = queries._nivel(2448.3, 2452.35)
    assert round(meses, 2) == 11.98
    assert nivel == 3 and queries.PUNTOS[nivel] == 10


def test_el_redondeo_no_mueve_a_ninguna_otra_tela():
    """El redondeo es de UN decimal a propósito: arregla el borde sin correr la
    línea. Las cuatro telas que están cerca del otro corte (1 mes) se quedan
    donde estaban."""
    for kg_base, kg_12m in [(659.6, 7673.95),    # Jersey Lycra 3.3 — 1,03
                            (191.25, 2023.4),    # Franela — 1,13
                            (21.25, 223.1),      # Stefi — 1,14
                            (204.0, 1974.0)]:    # WAFFER — 1,24
        nivel, _ = queries._nivel(kg_base, kg_12m)
        assert nivel == 2, (kg_base, kg_12m)
    # y una que sí es fácil de verdad sigue siendo fácil
    assert queries._nivel(100, 12000)[0] == 1


def test_hay_un_solo_numero_de_puntos_en_juego(monkeypatch):
    """⭐ Dueña 25/08/2026: "en juego es distinto que la presentacion, puntos".

    La Competencia mostraba la bolsa CONGELADA (`kg_base × puntos`, los kilos
    del día en que se fijó el puntaje) y Saldos la suma de la foto de HOY, que
    se mueve con la bodega. Dos números con el mismo nombre, y el de Saldos
    nunca vuelve a coincidir con uno impreso.
    """
    monkeypatch.setattr(queries, "puntos_por_tela", lambda: {
        "Jersey 3": {"kg_base": 100, "puntos": 10},
        "Fleece 102": {"kg_base": 50, "puntos": 4},
    })
    assert queries.bolsa_congelada() == 1200

    # y la pantalla la usa, en vez de sumar la foto de hoy
    import inspect as _i
    fuente = _i.getsource(views.parado) + _i.getsource(views.mis_telas)
    assert fuente.count("bolsa=queries.bolsa_congelada()") == 2
