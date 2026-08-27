"""Tests de `modules/asinfo/factura_lineas.py` — qué se llevó el cliente.

Sin HTTP: se mockea `metabase_client`. Los números de los tests salen de la
factura 001-099-000182419 del 21/08/2026, leída de Asinfo.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.asinfo import factura_lineas as fl


@pytest.fixture(autouse=True)
def _limpiar_cache():
    """Sin la caché de memoria, y SIN la de la base.

    🐞 Los tests de acá llaman a `que_se_llevo`, que guarda lo que trae en
    `scintela.factura_detalle`. Con una base a mano (la del CI, o la local de
    `vista_local.py`) esas filas quedan escritas: la corrida SIGUIENTE las
    encuentra y `que_se_llevo` contesta desde la base sin preguntarle a nadie,
    así que media docena de tests que cuentan llamadas a Asinfo fallan sin que
    nadie haya tocado el código. Acá la caché de la base se desconecta; el test
    que la quiera mirar la vuelve a enchufar con su propio `patch`, que manda
    sobre éste. TMT 2026-08-26.
    """
    fl.reset_cache()
    with patch.object(fl, "_de_la_base", return_value=None), \
         patch.object(fl, "_guardar"):
        yield
    fl.reset_cache()


def _fila(tela, color, kg, precio, neto, categoria="TELAS", calidad="PRIMERA"):
    bruto = round(kg * precio, 4)
    codigo = color[:3].upper()  # el código del COLOR: FRESA → FRE
    return {
        "tela": tela, "codigo": codigo, "producto": f"{tela} {color}",
        "categoria": categoria,
        "color": color, "calidad": calidad, "cantidad": kg, "precio": precio,
        "bruto": bruto, "descuento": round(bruto - neto, 4), "pct1": 5, "pct2": 14,
    }


# --- el número -------------------------------------------------------------

@pytest.mark.parametrize("numero", [None, "", "  ", "182419", "001-099-18",
                                    "001-099-000182419'; DROP TABLE x--"])
def test_numero_que_no_es_del_sri_no_pregunta_nada(numero):
    """Lo único que se interpola en el SQL es el número: se valida ENTERO.

    El caso con la cola de SQL es el que importa: un validador que recorta a 17
    caracteres antes de mirar lo dejaría pasar sin decir nada.
    """
    with patch("modules._lib.metabase_client.disponible") as m:
        res = fl.que_se_llevo(numero)
    assert res["estado"] == "sin-numero"
    assert res["lineas"] == []
    m.assert_not_called()


def test_numero_bueno_pasa_entero_al_sql():
    sql = fl._sql("001-099-000182419")
    assert "fc.numero = '001-099-000182419'" in sql
    assert "fc.estado <> 0" in sql
    assert "col.codigo" in sql        # el código del COLOR (BLA)
    assert "RIGHT(RTRIM(ISNULL(pr.codigo" in sql   # y su plan B


def test_el_color_y_la_calidad_se_buscan_por_atributo_no_por_posicion():
    """Ver el punto 4 del módulo: el slot lo decide el producto, no Asinfo."""
    sql = fl._sql("001-099-000182419")
    for i in range(1, 6):
        assert f"WHEN dfc.id_atributo_{i} = 3 THEN dfc.id_valor_atributo_{i}" in sql
        assert f"WHEN dfc.id_atributo_{i} = 2 THEN dfc.id_valor_atributo_{i}" in sql


# --- los estados -----------------------------------------------------------

def test_sin_puente_no_es_sin_datos():
    with patch("modules._lib.metabase_client.disponible", return_value=False):
        res = fl.que_se_llevo("001-099-000182419")
    assert res["estado"] == "sin-puente"


def test_asinfo_no_contesta_es_error_no_factura_vacia():
    """`[]` con `ok=False` es "no pude preguntar", no "no llevó nada"."""
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], False)):
        res = fl.que_se_llevo("001-099-000182419")
    assert res["estado"] == "error"


def test_asinfo_contesta_y_no_conoce_la_factura():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], True)):
        res = fl.que_se_llevo("001-099-000182419")
    assert res["estado"] == "sin-datos"


def test_la_excepcion_no_sube():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=RuntimeError("Metabase se cayó")):
        res = fl.que_se_llevo("001-099-000182419")
    assert res["estado"] == "error"


# --- el agrupado -----------------------------------------------------------

def test_cuatro_rollos_de_la_misma_tela_son_una_fila_con_cuatro_rollos():
    filas = [
        _fila("Fleece 96 Perchado", "FRESA", 21.75, 9.25, 164.37),
        _fila("Fleece 96 Perchado", "FRESA", 21.10, 9.25, 159.46),
        _fila("Fleece 96 Perchado", "FRESA", 21.05, 9.25, 159.08),
        _fila("Fleece 96 Perchado", "FRESA", 21.70, 9.25, 163.99),
    ]
    res = fl._agrupar(filas)
    assert len(res["lineas"]) == 1
    fila = res["lineas"][0]
    assert fila["rollos"] == 4
    assert fila["kg"] == 85.60
    assert fila["total"] == 646.90
    assert fila["codigo"] == "FRE"
    assert res["totales"]["rollos"] == 4


def test_el_mismo_color_a_dos_precios_no_se_promedia():
    """Promediarlos escondería el rollo que se vendió mal."""
    filas = [_fila("Jersey 3", "MARINO", 20.0, 9.82, 190.0),
             _fila("Jersey 3", "MARINO", 20.0, 8.00, 150.0)]
    res = fl._agrupar(filas)
    assert len(res["lineas"]) == 2
    assert {x["precio"] for x in res["lineas"]} == {9.82, 8.0}


def test_primera_y_segunda_son_dos_filas():
    filas = [_fila("Rib", "MARINO", 13.8, 10.51, 118.5),
             _fila("Rib", "MARINO", 10.0, 10.51, 86.0, calidad="SEGUNDA")]
    res = fl._agrupar(filas)
    assert sorted(x["calidad"] for x in res["lineas"]) == ["Primera", "Segunda"]


def test_el_servicio_de_logistica_no_es_un_rollo_ni_un_kilo():
    """`cantidad = 1` ahí es UNA unidad. Sumarla daba kilos de más."""
    filas = [
        _fila("Jersey 3", "MARINO", 22.45, 9.82, 180.12),
        {"tela": "SERVICIOS", "producto": "SERVICIO DE LOGISTICA",
         "categoria": "SERVICIOS", "color": "", "calidad": "",
         "cantidad": 1, "precio": 2.6087, "bruto": 2.6087, "descuento": 0,
         "pct1": 5, "pct2": 14},
    ]
    res = fl._agrupar(filas)
    assert res["totales"]["rollos"] == 1
    assert res["totales"]["kg"] == 22.45
    assert res["servicios"] == [
        {"nombre": "SERVICIO DE LOGISTICA", "cantidad": 1.0, "total": 2.61}]


def test_los_totales_son_los_de_la_factura_de_verdad():
    """La 182419: bruto 2.282,71 · descuento 417,73 · IVA 279,74 · 2.144,72.

    Medido contra Asinfo, no calculado a mano.
    """
    crudo = [
        ("Fleece 96 Perchado", "FRESA", 21.75, 9.25, 164.37),
        ("Cuellos T40", "ELECTRICO", 1.45, 12.70, 15.05),
        ("Cuellos T40", "ELECTRICO", 0.90, 12.70, 9.34),
        ("Jersey 3.5", "MENTA", 21.40, 9.20, 160.85),
        ("Rib", "MARINO", 13.80, 10.51, 118.50),
        ("Rib Acanalado", "AZUL NOCHE", 12.20, 10.51, 104.76),
        ("Rib Acanalado", "FRESA", 12.20, 10.51, 104.76),
        ("Jersey 3", "MARINO", 22.45, 9.82, 180.12),
        ("Jersey 3", "MARINO", 22.25, 9.82, 178.51),
        ("Jersey 3", "MARINO", 22.45, 9.82, 180.12),
        ("Fleece 96 Perchado", "FRESA", 21.10, 9.25, 159.46),
        ("Fleece 96 Perchado", "FRESA", 21.05, 9.25, 159.08),
        ("Jersey 3", "MARINO", 20.70, 9.82, 166.07),
        ("Fleece 96 Perchado", "FRESA", 21.70, 9.25, 163.99),
    ]
    res = fl._agrupar([_fila(*c) for c in crudo])
    t = res["totales"]
    assert t["rollos"] == 14
    assert t["kg"] == 235.40
    assert abs(t["bruto"] - 2282.71) < 0.05
    assert abs(t["descuento"] - 417.73) < 0.05
    assert abs(t["iva"] - 279.74) < 0.05
    assert abs(t["total"] - 2144.72) < 0.05
    assert len(res["lineas"]) == 7


def test_la_fila_mas_pesada_va_primero():
    filas = [_fila("Cuellos T40", "ELECTRICO", 1.45, 12.7, 15.05),
             _fila("Jersey 3", "MARINO", 22.45, 9.82, 180.12)]
    res = fl._agrupar(filas)
    assert res["lineas"][0]["tela"] == "Jersey 3"


def test_con_dos_escalones_de_descuento_distintos_no_se_dice_ninguno():
    """Un solo par de porcentajes mentiría sobre las otras filas."""
    a = _fila("Rib", "MARINO", 10, 10.0, 90.0)
    b = _fila("Jersey 3", "MARINO", 10, 10.0, 80.0)
    b["pct2"] = 20
    t = fl._agrupar([a, b])["totales"]
    assert t["pct1"] is None and t["pct2"] is None


# --- el cache --------------------------------------------------------------

def test_el_cache_guarda_solo_el_exito():
    """Cachear un fracaso sostendría diez minutos un "no llevó nada" falso."""
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], False)) as m:
        fl.que_se_llevo("001-099-000182419")
        fl.que_se_llevo("001-099-000182419")
    assert m.call_count == 2


def test_el_exito_se_pregunta_una_sola_vez():
    filas = [_fila("Jersey 3", "MARINO", 22.45, 9.82, 180.12)]
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=(filas, True)) as m:
        uno = fl.que_se_llevo("001-099-000182419")
        dos = fl.que_se_llevo("001-099-000182419")
    assert m.call_count == 1
    assert uno == dos


def test_el_cache_tiene_tope():
    filas = [_fila("Jersey 3", "MARINO", 22.45, 9.82, 180.12)]
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=(filas, True)):
        for i in range(fl._TOPE_CACHE + 5):
            fl.que_se_llevo(f"001-099-{i:09d}")
    assert len(fl._CACHE) <= fl._TOPE_CACHE


# --- la nota de crédito no tiene kilos (punto 7) ---------------------------
#
# TMT 2026-08-26 (dueña): *"kilos está mal"*. La 001-099-000011795 (SPI,
# −377,30) es una NOTA DE CRÉDITO: vive en la misma tabla de Asinfo que las
# facturas, con su propia numeración, y sus cuatro renglones vienen con
# `cantidad = 1` —una unidad, no un kilo— y el importe entero en el precio.
# El bloque decía "4 rollos · 4,00 kg" a 196,84 el kilo.

def _fila_nc(tela, color, importe, doc=17):
    """Un renglón de la 001-099-000011795, tal cual lo devuelve Asinfo."""
    return {
        "tela": tela, "codigo": color[:3].upper(), "producto": f"{tela} {color}",
        "categoria": "TELAS", "color": color, "calidad": "PRIMERA",
        "doc": doc, "cantidad": 1, "precio": importe, "bruto": importe,
        "descuento": 0, "pct1": 0, "pct2": 0,
    }


def _nota_de_credito():
    return [
        _fila_nc("Toper", "ELECTRICO", 196.8374),
        _fila_nc("Toper", "AZUL NOCHE", 98.2197),
        _fila_nc("Rib Acanalado", "ELECTRICO", 6.8176),
        _fila_nc("Jersey 3", "CIELO", 26.2153),
    ]


def test_el_sql_pregunta_que_documento_es():
    """Sin el documento no hay forma de saber si la `cantidad` son kilos."""
    assert "fc.id_documento" in fl._sql("001-099-000011795")


def test_la_nota_de_credito_no_muestra_kilos_ni_rollos():
    res = fl._agrupar(_nota_de_credito())
    assert res["doc"] == "nota-credito"
    assert res["titulo"] == "Nota de crédito"
    assert res["totales"]["kg"] is None
    assert res["totales"]["rollos"] is None
    assert [ln["kg"] for ln in res["lineas"]] == [None, None, None, None]
    assert [ln["rollos"] for ln in res["lineas"]] == [None, None, None, None]


def test_la_plata_de_la_nota_de_credito_es_la_de_la_ficha():
    """Los kilos se van, la plata queda: 328,09 + 15% = 377,30."""
    tot = fl._agrupar(_nota_de_credito())["totales"]
    assert tot["bruto"] == 328.09
    assert tot["iva"] == 49.21
    assert tot["total"] == 377.30


def test_sin_kilos_manda_el_renglon_que_mas_acredita():
    """La fila más pesada va primero; sin kilos, la que más plata devuelve."""
    lineas = fl._agrupar(_nota_de_credito())["lineas"]
    assert [ln["total"] for ln in lineas] == [196.84, 98.22, 26.22, 6.82]


def test_la_devolucion_si_tiene_kilos():
    """La devolución (doc 20) SÍ mueve mercadería: 001-099-000011778, 21,7 kg."""
    f = _fila("Fleece Lycra", "JAS.OSCURO", 21.7, 11.07, 212.23)
    f["doc"] = fl.DOC_DEVOLUCION
    res = fl._agrupar([f])
    assert res["doc"] == "devolucion"
    assert res["titulo"] == "Qué devolvió"
    assert res["totales"]["kg"] == 21.7
    assert res["totales"]["rollos"] == 1


def test_la_factura_sigue_siendo_lo_que_se_llevo():
    f = _fila("Jersey 3", "CELESTE", 21.8, 9.86, 175.53)
    f["doc"] = fl.DOC_FACTURA
    res = fl._agrupar([f])
    assert res["doc"] == "factura"
    assert res["titulo"] == "Detalle"
    assert res["totales"]["kg"] == 21.8


def test_un_documento_que_no_conocemos_se_porta_como_factura():
    """Una caché vieja o un documento nuevo de Asinfo no puede tapar kilos."""
    f = _fila("Jersey 3", "CELESTE", 21.8, 9.86, 175.53)
    res = fl._agrupar([f])          # sin `doc`
    assert res["doc"] == "otro"
    assert res["titulo"] == "Detalle"
    assert res["totales"]["kg"] == 21.8


def test_el_formato_del_cache_subio():
    """Las 23 notas de crédito ya guardadas tienen que reescribirse solas."""
    assert fl.FORMATO >= 5


# --- y lo mismo en las tres pantallas --------------------------------------

def _render(app, plantilla, filas):
    from flask import g, render_template
    with app.test_request_context("/facturas/1/que-se-llevo"):
        g.user = {"username": "test", "nombre_rol": "Accionista", "rol": 1}
        g.permisos = {"*"}
        return render_template(plantilla,
                               det={"estado": "ok", **fl._agrupar(filas)})


@pytest.mark.parametrize("plantilla", [
    "facturas/_que_se_llevo.html",
    "mi_cartera/_que_se_llevo.html",
])
def test_la_pantalla_de_la_nota_de_credito_no_dice_kilos(app, plantilla):
    html = _render(app, plantilla, _nota_de_credito())
    assert "Kilos" not in html
    assert "Rollos" not in html
    assert "377,30" in html
    assert "196,84" in html


@pytest.mark.parametrize("plantilla", [
    "facturas/_que_se_llevo.html",
    "mi_cartera/_que_se_llevo.html",
])
def test_la_pantalla_de_la_factura_sigue_diciendo_kilos(app, plantilla):
    f = _fila("Jersey 3", "CELESTE", 21.8, 9.86, 175.53)
    f["doc"] = fl.DOC_FACTURA
    html = _render(app, plantilla, [f])
    assert "Kilos" in html
    assert "21,80" in html


# --- el título ---------------------------------------------------------------
#
# TMT 2026-08-26 (dueña): *"no me gusta el título. Que se llame detalle"*.

def test_el_bloque_se_llama_detalle():
    """Un solo rótulo para las tres pantallas: oficina, vendedor y cliente."""
    assert fl.TITULO_DEFAULT == "Detalle"


def test_la_nota_de_credito_y_la_devolucion_se_siguen_nombrando():
    """Ahí el título es lo único que dice que ese papel NO es una venta."""
    assert fl.TITULOS["nota-credito"] == "Nota de crédito"
    assert fl.TITULOS["devolucion"] == "Qué devolvió"


# --- lo ya guardado, sin cruzar el puente -----------------------------------
#
# TMT 2026-08-26 (dueña): *"tarda mucho en cargarse"*. La ficha pedía el bloque
# aparte SIEMPRE, aun cuando la respuesta ya estaba en la base.

def test_lo_guardado_se_devuelve_sin_preguntarle_a_asinfo():
    guardado = {"estado": "ok", "formato": fl.FORMATO, "lineas": [],
                "servicios": [], "totales": {}}
    with patch.object(fl, "_de_la_base", return_value=guardado), \
         patch("modules._lib.metabase_client.disponible") as m:
        assert fl.en_cache("001-099-000182419") == guardado
    m.assert_not_called()


def test_lo_que_todavia_no_se_sabe_no_se_va_a_buscar():
    """`en_cache` NUNCA cruza el puente: existe para no hacer esperar a nadie."""
    with patch.object(fl, "_de_la_base", return_value=None), \
         patch("modules._lib.metabase_client.disponible") as m:
        assert fl.en_cache("001-099-000182419") is None
    m.assert_not_called()


def test_lo_guardado_se_lee_de_postgres_una_sola_vez():
    guardado = {"estado": "ok", "formato": fl.FORMATO, "lineas": [],
                "servicios": [], "totales": {}}
    with patch.object(fl, "_de_la_base", return_value=guardado) as m:
        fl.en_cache("001-099-000182419")
        fl.en_cache("001-099-000182419")
    assert m.call_count == 1


@pytest.mark.parametrize("numero", [None, "", "182419", "001-099-18"])
def test_un_numero_que_no_es_del_sri_no_tiene_nada_guardado(numero):
    with patch.object(fl, "_de_la_base") as m:
        assert fl.en_cache(numero) is None
    m.assert_not_called()


# --- el relleno de las facturas viejas --------------------------------------
#
# TMT 2026-08-26 (dueña), mirando una factura de MAYO: los últimos días estaban
# calientes y la historia no.

_UNA = "001-099-000175698"
_OTRA = "001-099-000175699"


def _relleno(monkeypatch, faltantes, filas, ok=True):
    """El relleno con `faltantes` por buscar y `filas` de respuesta."""
    llamadas = {"sql": [], "guardadas": [], "marcadas": []}
    monkeypatch.setattr(fl, "_faltantes", lambda limite, dias: faltantes)
    monkeypatch.setattr(fl, "_guardar",
                        lambda n, r: llamadas["guardadas"].append((n, r)))
    monkeypatch.setattr(fl, "_marcar_sin_datos",
                        lambda n: llamadas["marcadas"].append(n))
    monkeypatch.setattr("modules._lib.metabase_client.disponible", lambda: True)

    def _pregunta(db, sql, **kw):
        llamadas["sql"].append(sql)
        return (filas, ok)

    monkeypatch.setattr("modules._lib.metabase_client.fetch_dataset_estado",
                        _pregunta)
    return llamadas


def test_el_relleno_pregunta_por_todo_el_lote_de_una(monkeypatch):
    """Cruzar el puente cuesta 650 ms fijos: 120 facturas salen lo mismo que 1."""
    fila = _fila("Fleece 102", "PETROLEO", 42.8, 9.25, 330.97)
    fila["numero"] = _UNA
    llamadas = _relleno(monkeypatch, [_UNA, _OTRA], [fila])

    assert fl.precargar_faltantes(cada_secs=0) == 1
    assert len(llamadas["sql"]) == 1
    assert f"fc.numero IN ('{_UNA}', '{_OTRA}')" in llamadas["sql"][0]
    assert [n for n, _ in llamadas["guardadas"]] == [_UNA]
    assert llamadas["guardadas"][0][1]["estado"] == "ok"


def test_la_que_asinfo_no_conoce_queda_marcada(monkeypatch):
    """Sin la marca, el lote siguiente vuelve a mirar las mismas y no avanza."""
    fila = _fila("Fleece 102", "PETROLEO", 42.8, 9.25, 330.97)
    fila["numero"] = _UNA
    llamadas = _relleno(monkeypatch, [_UNA, _OTRA], [fila])

    fl.precargar_faltantes(cada_secs=0)
    assert llamadas["marcadas"] == [_OTRA]


def test_si_el_puente_no_contesta_no_se_marca_nada(monkeypatch):
    """Un "no pude preguntar" marcado como "no hay nada" dura para siempre."""
    llamadas = _relleno(monkeypatch, [_UNA, _OTRA], [], ok=False)

    assert fl.precargar_faltantes(cada_secs=0) == 0
    assert llamadas["marcadas"] == []
    assert llamadas["guardadas"] == []


def test_el_relleno_se_limita_solo(monkeypatch):
    fila = _fila("Fleece 102", "PETROLEO", 42.8, 9.25, 330.97)
    fila["numero"] = _UNA
    llamadas = _relleno(monkeypatch, [_UNA], [fila])

    fl.precargar_faltantes()
    fl.precargar_faltantes()
    assert len(llamadas["sql"]) == 1


def test_sin_puente_el_relleno_ni_pregunta_cuales_faltan(monkeypatch):
    """No tiene sentido revolver Postgres si no se le puede preguntar a Asinfo."""
    llamado = []
    monkeypatch.setattr(fl, "_faltantes",
                        lambda limite, dias: llamado.append(1) or [])
    monkeypatch.setattr("modules._lib.metabase_client.disponible", lambda: False)
    assert fl.precargar_faltantes(cada_secs=0) == 0
    assert llamado == []


def test_las_que_faltan_son_las_viejas_sin_detalle(monkeypatch):
    """Y salen de la más nueva a la más vieja: es la que van a abrir."""
    visto = {}

    def _fetch(sql, params=None, conn=None):
        visto["sql"], visto["params"] = sql, params
        return [{"numero": _UNA}]

    monkeypatch.setattr("db.fetch_all", _fetch)
    assert fl._faltantes(120, 180) == [_UNA]
    assert "LEFT JOIN scintela.factura_detalle" in visto["sql"]
    assert "d.numero IS NULL" in visto["sql"]
    assert "ORDER BY f.fecha DESC" in visto["sql"]
    desde, hasta, limite = visto["params"]
    assert (hasta - desde).days == 177   # 180 días, menos los 3 de la precarga
    assert limite == 120


def test_los_ultimos_tres_dias_son_de_la_precarga(monkeypatch):
    """`precargar` los reescribe cada media hora, que es lo que hace falta
    mientras una factura recién emitida todavía puede recibir un renglón."""
    from datetime import date

    visto = {}
    monkeypatch.setattr("filters.today_ec", lambda: date(2026, 8, 26))
    monkeypatch.setattr("db.fetch_all",
                        lambda sql, params=None, conn=None:
                        visto.setdefault("params", params) and [])
    fl._faltantes(120, 180)
    assert visto["params"][1] == date(2026, 8, 23)


def test_la_marca_de_sin_datos_no_llega_a_ninguna_pantalla():
    """La lee el relleno para saltearla; `_de_la_base` sólo devuelve el éxito."""
    fila = {"datos": {"estado": "sin-datos", "formato": fl.FORMATO}}
    with patch("db.fetch_one", return_value=fila):
        assert fl._de_la_base(_UNA) is None


def test_la_marca_no_pisa_un_detalle_ya_guardado():
    """Si otro la guardó bien mientras tanto, la marca no puede borrarla."""
    visto = {}
    with patch("db.execute", side_effect=lambda sql, params=None: visto.update(
            sql=sql, params=params)):
        fl._marcar_sin_datos(_UNA)
    assert "ON CONFLICT (numero) DO NOTHING" in visto["sql"]
    assert '"estado": "sin-datos"' in visto["params"][1]
