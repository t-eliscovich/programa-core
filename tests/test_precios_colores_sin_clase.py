"""La clase de precio de un color, deducida del precio al que Asinfo lo vende.

CONTEXTO (2026-08-12). Alex: *"hay tonos nuevos por ejemplo un cocoa no existe
en el listado"* → *"puedes adicionarlos desde ASINFO?"*.

El listado de colores de la Factura Proforma sale de `scintela.tinto_costos`
filtrado por `clase BETWEEN 1 AND 5`: un color sin clase no tiene de qué fila
de la matriz sacar el precio, así que no se puede cotizar y no aparece. La
clase vivía sólo en COSTOS.DBF, retirado el 05/08.

Lo que reemplaza al DBF es el PRECIO: en Asinfo cada producto es tela + color,
y el precio al que se vende cae exacto en una fila de la matriz de esa tela.
Esa fila es la clase. Medido contra los 318 colores que ya tenían clase: 205
aciertos sobre 207 comparables (99,0%).

Lo que se prueba acá es lo que puede COBRAR MAL si se rompe:

1. Un precio que en su tela identifica UNA sola clase, la asigna.
2. Un precio AMBIGUO (ALEMANIA/KIANA/MICRO/JAMES cobran lo mismo en las cinco)
   NO asigna nada — antes que sugerir una clase inventada, la fila queda para
   decidir a mano. Es la diferencia entre cotizar bien y cotizar 0,72 US/kg
   abajo.
3. El color que gana es el MODAL, no el último ni el promedio.
4. Un color que NO se vende no entra en la tabla (350 de los 374 sin clase son
   fórmulas de tintura que nunca se cotizaron).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.precios import queries

# Matriz de juguete con la forma de la real: JERSEY distingue las cinco clases,
# ALEMANIA cobra lo mismo en todas (que es lo que pasa de verdad).
def _fila(clase, descripcio, jersey, rib):
    """Fila completa: el template de /precios lee TODAS las columnas de TELAS."""
    f = {col: None for col, _ in queries.TELAS}
    f.update(clase=clase, descripcio=descripcio, jersey=jersey, rib=rib, alemania=7.26)
    return f


MATRIZ = [
    _fila(1, "BLANCO", 7.94, 8.53),
    _fila(2, "BAJOS", 8.57, 9.17),
    _fila(3, "MEDIOS", 9.20, 9.84),
    _fila(4, "JASPEADOS", 9.28, 9.57),
    _fila(5, "FUERTES", 9.82, 10.51),
]


def _tabla():
    return queries._precio_a_clase_por_tela(MATRIZ)


# ---------------------------------------------------------------------------
# 1 y 2 — qué precio dice la clase y qué precio no dice nada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("precio,clase", [
    (7.94, 1), (8.57, 2), (9.20, 3), (9.28, 4), (9.82, 5),
])
def test_cada_precio_de_jersey_identifica_su_clase(precio, clase):
    assert _tabla()[("Jersey", precio)] == clase


def test_el_precio_de_alemania_no_identifica_ninguna_clase():
    """Cobra 7,26 en las cinco: el precio no puede decir cuál es.

    Si esto dejara de valer, el sugeridor le pondría al color la clase que
    apareciera primero y la proforma cotizaría cualquier cosa.
    """
    tabla = _tabla()
    assert ("Alemania", 7.26) not in tabla
    assert not any(t == "Alemania" for t, _ in tabla)


def test_una_tela_sin_precio_cargado_no_rompe():
    matriz = [{"clase": 1, "jersey": None}, {"clase": 5, "jersey": 9.82}]  # noqa: E501
    assert queries._precio_a_clase_por_tela(matriz) == {("Jersey", 9.82): 5}


# ---------------------------------------------------------------------------
# 3 y 4 — la sugerencia
# ---------------------------------------------------------------------------


def _corre(rows, sin_clase):
    with patch.object(queries, "matriz", return_value=MATRIZ), \
            patch.object(queries, "_codigos_sin_clase", return_value=sin_clase), \
            patch("modules._lib.metabase_client.fetch_dataset", return_value=rows):
        queries._COLORES_CACHE.clear()
        out = queries.colores_sin_clase()
    queries._COLORES_CACHE.clear()
    return out


def test_gana_la_clase_con_mas_productos_no_la_ultima():
    """COCOA: 5 productos a MEDIOS y 2 a FUERTES → MEDIOS.

    El caso real. Con la categoría de formulas_app COCOA figura como
    "Color Bajo" y cotizaría 0,72 US/kg por debajo de lo que factura Asinfo.
    """
    rows = [
        {"cod": "COA", "categoria": "Jersey", "precio": 9.20, "n": 5, "lineas": 5},
        {"cod": "COA", "categoria": "Jersey", "precio": 9.82, "n": 2, "lineas": 2},
    ]
    out = _corre(rows, {"COA": "COCOA"})
    assert len(out) == 1
    assert out[0]["sugerida"] == 3
    assert out[0]["sugerida_desc"] == "MEDIOS"


def test_el_color_que_solo_se_vende_en_tela_de_precio_plano_queda_sin_sugerencia():
    """Se vende, pero el precio no alcanza: la fila aparece igual, sin sugerir.

    Antes que inventar una clase, que la elija una persona.
    """
    rows = [{"cod": "ACP", "categoria": "Alemania", "precio": 7.26, "n": 9, "lineas": 45}]
    out = _corre(rows, {"ACP": "ACERO PES"})
    assert len(out) == 1, "el color se vende: tiene que aparecer para decidirlo"
    assert out[0]["sugerida"] is None
    assert "no distingue la clase" in out[0]["evidencia"]


def test_el_color_que_no_se_vende_no_entra():
    """350 de los 374 sin clase nunca se facturaron: no hay nada que decidir."""
    rows = [{"cod": "OTRO", "categoria": "Jersey", "precio": 9.82, "n": 3, "lineas": 3}]
    out = _corre(rows, {"NADIE": "COLOR QUE NO SE VENDE"})
    assert out == []


def test_un_color_que_ya_tiene_clase_no_aparece():
    """La tabla es de los que FALTAN; si aparecieran los 692 no se podría usar."""
    rows = [{"cod": "BLA", "categoria": "Jersey", "precio": 7.94, "n": 30, "lineas": 900}]
    assert _corre(rows, {"COA": "COCOA"}) == []


def test_los_sin_sugerencia_van_al_final():
    """Son los que piden una decisión; arriba va lo que se confirma de un clic."""
    rows = [
        {"cod": "ACP", "categoria": "Alemania", "precio": 7.26, "n": 9, "lineas": 45},
        {"cod": "COA", "categoria": "Jersey", "precio": 9.20, "n": 5, "lineas": 1389},
    ]
    out = _corre(rows, {"ACP": "ACERO PES", "COA": "COCOA"})
    assert [c["cod"] for c in out] == ["COA", "ACP"]


def test_si_asinfo_no_contesta_devuelve_vacio_y_no_explota():
    """Fail-soft: /precios no se puede caer porque el puente tosió."""
    assert _corre([], {"COA": "COCOA"}) == []


def test_sin_colores_sin_clase_ni_pregunta_a_asinfo():
    with patch.object(queries, "matriz", return_value=MATRIZ), \
            patch.object(queries, "_codigos_sin_clase", return_value={}), \
            patch("modules._lib.metabase_client.fetch_dataset") as fake:
        queries._COLORES_CACHE.clear()
        assert queries.colores_sin_clase() == []
    fake.assert_not_called()
    queries._COLORES_CACHE.clear()


# ---------------------------------------------------------------------------
# La frase que lee la dueña
# ---------------------------------------------------------------------------


def test_la_evidencia_entra_en_un_renglon():
    """Dueña 2026-08-12: *"cada fila es muy grande"*."""
    frase = queries._frase_evidencia({3: 5, 5: 2}, 1389)
    assert frase == "5 a MEDIOS, 2 a FUERTES · 1.389 fact."
    assert len(frase) < 60


def test_los_miles_van_con_punto_no_con_coma():
    assert "1.389" in queries._frase_evidencia({3: 5}, 1389)
    assert "1,389" not in queries._frase_evidencia({3: 5}, 1389)


def test_una_sola_factura_no_dice_1_facturas():
    assert queries._frase_evidencia({}, 1).endswith("1 fact.")


# ---------------------------------------------------------------------------
# Las dos rutas
# ---------------------------------------------------------------------------


@pytest.fixture
def _matriz_fake(monkeypatch):
    monkeypatch.setattr(queries, "matriz", lambda: MATRIZ)


@pytest.fixture
def duenia(app, fake_db, _matriz_fake):
    """Accionista: rol wildcard, tiene precios.editar."""
    rid = fake_db.add_role("Accionista", ["*"])
    uid = fake_db.add_user("tamara", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    return c


@pytest.fixture
def vendedor(app, fake_db, _matriz_fake):
    """Sin precios.editar: ve la lista, no puede tocar la clase."""
    rid = fake_db.add_role("Vendedor", [])
    uid = fake_db.add_user("vendedor", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    return c


def test_el_bloque_nace_cerrado(duenia):
    """Dueña 2026-08-12: *"que aparezca cerrado"*.

    Es un `<details>` sin `open`; si alguien se lo agrega, /precios pasa a
    pagar la consulta a Asinfo en cada carga, para todos.
    """
    html = duenia.get("/precios").get_data(as_text=True)
    assert 'id="colores-asinfo"' in html
    marca = html[html.index('<details id="colores-asinfo"'):][:400]
    assert " open" not in marca


def test_precios_no_le_pregunta_a_asinfo_al_cargar(duenia):
    """La consulta sale recién al abrir el bloque, no al entrar a la pantalla."""
    with patch("modules._lib.metabase_client.fetch_dataset") as fake:
        duenia.get("/precios")
    fake.assert_not_called()


def test_la_ruta_devuelve_los_colores_y_si_puede_editar(duenia):
    rows = [{"cod": "COA", "categoria": "Jersey", "precio": 9.20, "n": 5, "lineas": 1389}]
    with patch.object(queries, "_codigos_sin_clase", return_value={"COA": "COCOA"}), \
            patch("modules._lib.metabase_client.fetch_dataset", return_value=rows):
        queries._COLORES_CACHE.clear()
        data = duenia.get("/precios/colores-asinfo").get_json()
    queries._COLORES_CACHE.clear()
    assert data["ok"] is True
    assert data["puede_editar"] is True
    assert data["colores"][0]["cod"] == "COA"
    assert data["colores"][0]["sugerida"] == 3


def test_el_vendedor_ve_la_lista_pero_no_los_botones(vendedor):
    with patch.object(queries, "_codigos_sin_clase", return_value={"COA": "COCOA"}), \
            patch("modules._lib.metabase_client.fetch_dataset", return_value=[]):
        queries._COLORES_CACHE.clear()
        data = vendedor.get("/precios/colores-asinfo").get_json()
    queries._COLORES_CACHE.clear()
    assert data["puede_editar"] is False


def test_si_la_consulta_explota_la_ruta_contesta_igual(duenia):
    """Fail-soft con el motivo, no un 500 en la cara del usuario."""
    with patch.object(queries, "colores_sin_clase", side_effect=RuntimeError("boom")):
        data = duenia.get("/precios/colores-asinfo").get_json()
    assert data["ok"] is False
    assert data["colores"] == []


def test_asignar_la_clase_la_graba(duenia, fake_db):
    with patch.object(queries, "asignar_clase_color") as fake:
        resp = duenia.post("/precios/color-clase", data={"cod": "coa", "clase": "3"})
    assert resp.status_code == 302
    cod, clase, _usuario = fake.call_args[0]
    assert (cod, clase) == ("COA", 3)


def test_el_vendedor_no_puede_asignar_la_clase(vendedor):
    """El permiso es `precios.editar`, el mismo que gobierna la matriz.

    Devuelve 404, no 403 — decisión de UX de la dueña (2026-05-22): que quien
    no tiene el permiso no vea que la sección existe.
    """
    with patch("db.execute") as fake:
        resp = vendedor.post("/precios/color-clase", data={"cod": "COA", "clase": "3"})
    assert resp.status_code == 404
    assert [c for c in fake.call_args_list if "tinto_costos" in str(c.args[0])] == []


@pytest.mark.parametrize("clase", ["0", "6", "", "abc"])
def test_una_clase_invalida_no_se_graba(duenia, clase):
    """Sin esto, un POST a mano dejaría el color con una clase que no existe y
    la proforma volvería a no poder cotizarlo, ahora sin aparecer en la tabla.
    """
    with patch("db.execute") as fake:
        duenia.post("/precios/color-clase", data={"cod": "COA", "clase": clase})
    # `db.execute` lo llama también la bitácora: mirar SÓLO el UPDATE del catálogo.
    updates = [c for c in fake.call_args_list if "tinto_costos" in str(c.args[0])]
    assert updates == []


def test_asignar_clase_color_escribe_y_limpia_la_cache(fake_db):
    queries._COLORES_CACHE["v1"] = (9e9, ["basura"])
    with patch("db.execute") as fake:
        queries.asignar_clase_color(" coa ", 3, "tamara")
    sql, params = fake.call_args[0]
    assert "UPDATE scintela.tinto_costos" in sql
    assert params == (3, "tamara", "COA")
    assert "v1" not in queries._COLORES_CACHE, "si queda cacheado, el color sigue apareciendo como pendiente"


def test_asignar_clase_color_rechaza_lo_invalido():
    for cod, clase in [("", 3), ("COA", 0), ("COA", 6), ("COA", None)]:
        with pytest.raises(ValueError):
            queries.asignar_clase_color(cod, clase, "tamara")


# ---------------------------------------------------------------------------
# Los bordes que no se ven pero muerden
# ---------------------------------------------------------------------------


def test_el_nombre_pierde_la_categoria_que_le_pega_el_sync_de_formulas():
    """En el catálogo el color viene "COCOA · Color Bajo".

    Esa categoría es justo la que NO sirve para la clase (formulas no
    distingue MEDIOS). Mostrarla al lado de la clase que sugiere el precio
    sería mandar dos respuestas distintas a la misma pregunta.
    """
    filas = [
        {"cod": " ccr ", "color": "COCOA RIB · Color Bajo"},
        {"cod": "COA", "color": "COCOA"},
        {"cod": "XXX", "color": ""},
        {"cod": "", "color": "sin codigo"},
    ]
    with patch("db.fetch_all", return_value=filas):
        out = queries._codigos_sin_clase()
    assert out == {"CCR": "COCOA RIB", "COA": "COCOA", "XXX": "XXX"}


def test_la_segunda_lectura_sale_de_la_cache_sin_tocar_asinfo():
    """/precios lo abre cualquiera: la consulta pesada va una vez cada 5 min."""
    rows = [{"cod": "COA", "categoria": "Jersey", "precio": 9.20, "n": 5, "lineas": 9}]
    with patch.object(queries, "matriz", return_value=MATRIZ), \
            patch.object(queries, "_codigos_sin_clase", return_value={"COA": "COCOA"}), \
            patch("modules._lib.metabase_client.fetch_dataset", return_value=rows) as fake:
        queries._COLORES_CACHE.clear()
        primera = queries.colores_sin_clase()
        segunda = queries.colores_sin_clase()
    queries._COLORES_CACHE.clear()
    assert primera == segunda
    assert fake.call_count == 1


def test_sin_matriz_no_se_pregunta_nada():
    """Sin precios cargados no hay con qué comparar; preguntar sería al pedo."""
    with patch.object(queries, "matriz", return_value=[]), \
            patch.object(queries, "_codigos_sin_clase", return_value={"COA": "COCOA"}), \
            patch("modules._lib.metabase_client.fetch_dataset") as fake:
        queries._COLORES_CACHE.clear()
        assert queries.colores_sin_clase() == []
    fake.assert_not_called()
    queries._COLORES_CACHE.clear()


def test_una_fila_con_basura_se_saltea_sin_tirar_abajo_la_tabla():
    """Metabase devuelve TEXT en columnas que uno esperaría numéricas."""
    rows = [
        {"cod": "COA", "categoria": "Jersey", "precio": "no es un numero", "n": 1, "lineas": 1},
        {"cod": "COA", "categoria": "Jersey", "precio": 9.20, "n": 5, "lineas": 1389},
    ]
    out = _corre(rows, {"COA": "COCOA"})
    assert len(out) == 1
    assert out[0]["sugerida"] == 3
    assert out[0]["lineas"] == 1389


def test_el_color_con_precio_de_lista_pero_sin_ventas_igual_aparece():
    """Producto nuevo con precio puesto y todavía sin facturar: ya se cotiza."""
    rows = [{"cod": "COA", "categoria": "Jersey", "precio": 9.20, "n": 2, "lineas": 0}]
    out = _corre(rows, {"COA": "COCOA"})
    assert len(out) == 1
    assert out[0]["lineas"] == 0
    assert out[0]["evidencia"] == "2 a MEDIOS"


def test_el_color_sin_ventas_y_con_precio_que_no_dice_nada_no_entra():
    """Producto en tela de precio plano y nunca facturado: no hay señal de que
    se venda ni pista de la clase. Sumarlo sería ruido en una tabla que existe
    justamente para tener pocas filas.
    """
    rows = [{"cod": "ACP", "categoria": "Alemania", "precio": 7.26, "n": 4, "lineas": 0}]
    assert _corre(rows, {"ACP": "ACERO PES"}) == []
