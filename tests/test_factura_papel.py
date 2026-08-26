"""La factura EN PAPEL — la copia que el vendedor le manda al cliente.

Los números de acá NO son inventados: salen de abrir el PDF real de la
001-099-000182675 (26 renglones, 300,25 kg, 2.620,30) y leer cada columna. La
tabla de abajo tiene, por renglón, lo que Asinfo guarda en la base —cantidad,
precio_linea, descuento_linea— y al lado lo que Asinfo IMPRIME. Si la hoja
copiada saca otro número, alguno de estos tests se pone rojo.

Es la única forma de sostener la promesa: *"así no piensan que es distinta"*.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules._lib import code128
from modules.asinfo import factura_papel as fp


@pytest.fixture(autouse=True)
def _limpiar_cache():
    fp.reset_cache()
    yield
    fp.reset_cache()


# Lo que la base guarda y lo que el papel imprime, renglón por renglón:
# (código, descripción, acabado, color, cantidad, precio_linea,
#  descuento_linea, precio impreso, descuento impreso, total impreso)
RENGLONES = [
    ('RICEL', 'RIB CELESTE', 'TUB', 'CELESTE', 3.1, 28.427, 5.202141, 10.55, 5.98, 26.71),
    ('RIBAZ', 'RIB BLANCO-AZU', 'TUB', 'BLANCO-AZU', 14.95, 127.5235, 23.3368005, 9.81, 26.84, 119.81),
    ('RAVIN', 'RIB ACANALADO VINO', 'TUB', 'VINO', 2, 21.02, 3.84666, 12.09, 4.42, 19.75),
    ('RACRU', 'RIB ACANALADO CRUDO', 'TUB', 'CRUDO', 3.15, 28.8855, 5.2860465, 10.55, 6.08, 27.14),
    ('RACAF', 'RIB ACANALADO CAFE', 'TUB', 'CAFE', 1, 10.51, 1.92333, 12.09, 2.21, 9.88),
    ('PUTOP', 'PUÑOS TOPACIO', 'TUB', 'TOPACIO', 1.6, 19.328, 3.537024, 13.89, 4.07, 18.16),
    ('PETOP', 'PIQUE ESPECIAL TOPACIO', 'TUB', 'TOPACIO', 21.55, 200.1995, 36.6365085, 10.68, 42.13, 188.1),
    ('JE35ROY', 'JERSEY 3.5 ROYAL', 'TUB', 'ROYAL ROJO', 21.85, 214.567, 39.265761, 11.29, 45.16, 201.59),
    ('JE35RML', 'JERSEY 3.5 ROJO MARLBORO', 'TUB', ' MARLBORO', 21.75, 213.585, 39.086055, 11.29, 44.95, 200.67),
    ('JE35CAM', 'JERSEY 3.5 CAMOTE', 'TUB', 'CAMOTE', 21.85, 214.567, 39.265761, 11.29, 45.16, 201.59),
    ('JE35CAM', 'JERSEY 3.5 CAMOTE', 'TUB', 'CAMOTE', 21.65, 212.603, 38.906349, 11.29, 44.74, 199.75),
    ('JE30CEL', 'JERSEY 3 CELESTE', 'TUB', 'CELESTE', 21.8, 186.826, 34.189158, 9.86, 39.32, 175.53),
    ('JE30CEL', 'JERSEY 3 CELESTE', 'TUB', 'CELESTE', 20.35, 174.3995, 31.9151085, 9.86, 36.7, 163.86),
    ('FE96CRU', 'FLEECE 96 PERCHADO CRUDO', 'TUB', 'CRUDO', 20.65, 171.6015, 31.4030745, 9.56, 36.11, 161.23),
    ('FE96CRU', 'FLEECE 96 PERCHADO CRUDO', 'TUB', 'CRUDO', 20.85, 173.2635, 31.7072205, 9.56, 36.46, 162.79),
    ('FE10ROY', 'FLEECE 102 ROYAL', 'TUB', 'ROYAL', 21.2, 196.1, 35.8863, 10.64, 41.27, 184.24),
    ('FE10ROY', 'FLEECE 102 ROYAL', 'TUB', 'ROYAL', 21.1, 195.175, 35.717025, 10.64, 41.07, 183.38),
    ('FE10PER', 'FLEECE 102 PERICO', 'TUB', 'PERICO', 21.25, 196.5625, 35.9709375, 10.64, 41.37, 184.68),
    ('C40TOP', 'CUELLOS T40 TOPACIO', 'TUB', 'TOPACIO', 2.1, 25.368, 4.642344, 13.89, 5.34, 23.83),
    ('C38BLA', 'CUELLOS T38 BLANCO', 'TUB', 'BLANCO', 3.1, 33.511, 6.132513, 12.43, 7.05, 31.49),
    ('C36BLA', 'CUELLOS T36 BLANCO', 'TUB', 'BLANCO', 2.9, 31.349, 5.736867, 12.43, 6.6, 29.45),
    ('C34BLA', 'CUELLOS T34 BLANCO', 'TUB', 'BLANCO', 2.8, 30.268, 5.539044, 12.43, 6.37, 28.44),
    ('C32BLA', 'CUELLOS T32 BLANCO', 'TUB', 'BLANCO', 2.2, 23.782, 4.352106, 12.43, 5.0, 22.35),
    ('C30BLA', 'CUELLOS T30 BLANCO', 'TUB', 'BLANCO', 2.55, 27.5655, 5.0444865, 12.43, 5.8, 25.9),
    ('C28BLA', 'CUELLOS T28 BLANCO', 'TUB', 'BLANCO', 1.05, 11.3505, 2.0771415, 12.43, 2.39, 10.66),
    ('C28BLA', 'CUELLOS T28 BLANCO', 'TUB', 'BLANCO', 1.9, 20.539, 3.758637, 12.43, 4.32, 19.3)
]


#: Lo que dice el pie del papel, medido con la regla y con la calculadora.
PAPEL_SUBTOTAL_PRECIO = 2620.28
PAPEL_DESCUENTO = 586.91
PAPEL_SIN_IMPUESTOS = 2278.50
PAPEL_IVA = 341.78
PAPEL_TOTAL = 2620.30
PAPEL_KILOS = 300.25
CLAVE = "2608202601179112576200120010990001826750017716919"


def _filas(renglones=None, **cab):
    """Los renglones tal cual los devuelve Metabase."""
    base = {
        "numero": "001-099-000182675", "fecha": "2026-08-26T00:00:00Z", "doc": 7,
        "autorizacion": CLAVE, "clave": CLAVE, "ambiente": 2, "tipo_emision": 1,
        "fecha_autorizacion": "2026-08-26", "forma_pago": "20",
        "base": 2278.52, "iva_sri": 341.78,
        "cli_razon": "VERA VARGAS RAMON RODOLFO", "cli_comercial": "RRV",
        "cli_ruc": "1308222973001", "cli_direccion": "AV DE LA PRENSA N48-45",
        "cli_ciudad": "QUITO", "cli_email": "eratex2017@gmail.com",
        "cli_telefono": "2222222", "referencia": "PRENSA",
        "emi_matriz": "DUCHICELA N2-150 9 DE AGOSTO CALDERON",
        "emi_sucursal": "DUCHICELA N2-150 9 DE AGOSTO CALDERON",
    }
    base.update(cab)
    salida = []
    for cod, desc, aca, col, cant, bruto, descu, _p, _d, _t in (renglones or RENGLONES):
        salida.append({**base, "codigo": cod, "descripcion": desc,
                       "categoria": "TELAS", "acabado": aca, "color": col,
                       "calidad": "PRI", "cantidad": cant,
                       "precio": bruto / cant, "bruto": bruto,
                       "descuento": descu, "pct1": 5, "pct2": 14})
    return salida


# --- los renglones, uno por uno --------------------------------------------

def test_cada_renglon_dice_lo_mismo_que_el_papel():
    """Precio, descuento y total de los 26 renglones, contra el PDF real."""
    hoja = fp.armar(_filas())
    assert len(hoja["renglones"]) == len(RENGLONES)
    for r, esperado in zip(hoja["renglones"], RENGLONES, strict=False):
        cod, _desc, _aca, _col, _cant, _b, _d, precio, descu, total = esperado
        assert (cod, r["precio"], r["descuento"], r["total"]) == \
               (cod, precio, descu, total)


def test_el_total_del_renglon_resta_dos_numeros_YA_redondeados():
    """La regla que costó tres centavos: redondear y DESPUÉS restar.

    En RACAF la base tiene 10,51 de bruto y 1,92333 de descuento. Redondeando
    la resta da 9,87; redondeando cada uno primero da 9,88, que es lo que
    imprime Asinfo. Con la regla al revés, el pie cerraba en 2.620,29 contra
    los 2.620,28 del papel.
    """
    racaf = next(r for r in fp.armar(_filas())["renglones"] if r["codigo"] == "RACAF")
    assert racaf["total"] == 9.88
    assert round((10.51 - 1.92333) * 1.15, 2) == 9.87   # la cuenta ingenua


def test_el_pie_da_los_cinco_numeros_del_papel():
    t = fp.armar(_filas())["totales"]
    assert t["bruto"] == PAPEL_SUBTOTAL_PRECIO
    assert t["descuento"] == PAPEL_DESCUENTO
    assert t["neto"] == PAPEL_SIN_IMPUESTOS
    assert t["iva"] == PAPEL_IVA
    assert t["total"] == PAPEL_TOTAL


def test_los_kilos_son_los_del_papel():
    assert fp.armar(_filas())["totales"]["kilos"] == PAPEL_KILOS


def test_el_iva_y_el_total_salen_del_SRI_y_no_se_recalculan():
    """Son los que quedaron autorizados: el cliente los tiene en su comprobante.

    Recalcularlos da 2.620,28 —la suma de la columna— y dos centavos de
    diferencia en el TOTAL es exactamente lo que hace dudar de si la hoja es
    la misma factura.
    """
    t = fp.armar(_filas())["totales"]
    assert t["total"] == 2620.30 and t["bruto"] == 2620.28


def test_sin_los_totales_del_SRI_la_hoja_igual_cierra():
    """Una factura de 82.112 no tiene fila en `factura_clienteSRI`."""
    t = fp.armar(_filas(base=None, iva_sri=None))["totales"]
    assert t["total"] == PAPEL_SUBTOTAL_PRECIO
    assert round(t["neto"] + t["iva"], 2) == t["total"]


# --- las trampas del papel --------------------------------------------------

def test_el_flete_no_es_un_kilo():
    """El SERVICIO DE LOGISTICA entra con cantidad = 1, que es una unidad.

    Es la misma trampa que ya se pagó en `dia_despacho` y en `factura_lineas`.
    """
    filas = _filas()
    filas.append({**filas[0], "codigo": "FLETE", "descripcion": "SERVICIO DE LOGISTICA",
                  "categoria": "SERVICIOS", "acabado": "", "color": "", "calidad": "",
                  "cantidad": 1, "precio": 20.0, "bruto": 20.0, "descuento": 0})
    hoja = fp.armar(filas)
    assert hoja["totales"]["kilos"] == PAPEL_KILOS          # los mismos kilos
    assert len(hoja["renglones"]) == len(RENGLONES) + 1      # pero sale impreso
    assert hoja["renglones"][-1]["servicio"] is True
    assert all(a[0] != "SERVICIO DE LOGISTICA" for a in hoja["articulos"])


def test_el_renglon_sin_atributos_no_dice_None():
    """El 2,5% de los renglones no tiene ni color ni acabado cargados."""
    filas = _filas(RENGLONES[:1])
    filas[0].update(acabado=None, color=None, calidad=None)
    r = fp.armar(filas)["renglones"][0]
    assert (r["acabado"], r["color"], r["calidad"]) == ("", "", "")


def test_los_articulos_se_cuentan_por_rollo():
    """Dos rollos de la misma tela son una línea que dice 2."""
    arts = dict(fp.armar(_filas())["articulos"])
    assert arts["JERSEY 3 CELESTE"] == 2
    assert arts["RIB CELESTE"] == 1
    assert sum(arts.values()) == len(RENGLONES)


def test_con_dos_escalones_de_descuento_distintos_no_se_dice_ninguno():
    filas = _filas()
    filas[0]["pct2"] = 7
    assert fp.armar(filas)["totales"]["pct1"] is None


# --- la cabecera ------------------------------------------------------------

def test_el_ruc_del_emisor_sale_de_la_clave_de_acceso():
    """Los 49 dígitos llevan el RUC en las posiciones 11 a 23."""
    assert fp.armar(_filas())["emisor"]["ruc"] == "1791125762001"


def test_sin_clave_el_ruc_es_el_de_siempre():
    hoja = fp.armar(_filas(autorizacion="", clave=""))
    assert hoja["emisor"]["ruc"] == fp.RUC_INTELA
    assert hoja["cabecera"]["clave"] == ""


def test_la_forma_de_pago_dice_su_texto():
    cab = fp.armar(_filas())["cabecera"]
    assert cab["forma_pago"] == "20"
    assert cab["forma_pago_texto"] == "Otros con utilización del sistema financiero"


def test_la_fecha_sale_en_castellano():
    assert fp.armar(_filas())["cabecera"]["fecha"] == "26/08/2026"


def test_ambiente_de_pruebas_no_dice_produccion():
    assert fp.armar(_filas(ambiente=1))["cabecera"]["ambiente"] == "PRUEBAS"


# --- la consulta ------------------------------------------------------------

def test_la_consulta_trae_el_recuadro_del_SRI():
    sql = fp._sql("001-099-000182675")
    assert "factura_clienteSRI" in sql
    assert "sri.autorizacion" in sql and "sri.clave_acceso" in sql
    assert "sri.base_imponible_diferente_cero" in sql and "sri.monto_iva" in sql


def test_la_consulta_ordena_como_el_papel():
    """Código DESCENDENTE, con el id de desempate para que no baile."""
    sql = fp._sql("001-099-000182675")
    assert "ORDER BY pr.codigo DESC, dfc.id_detalle_factura_cliente" in sql


def test_el_color_y_el_acabado_se_buscan_por_atributo_no_por_posicion():
    sql = fp._sql("001-099-000182675")
    for atributo in (fp.ATRIBUTO_COLOR, fp.ATRIBUTO_CALIDAD, fp.ATRIBUTO_ACABADO):
        assert f"dfc.id_atributo_1 = {atributo}" in sql
        assert f"dfc.id_atributo_5 = {atributo}" in sql


@pytest.mark.parametrize("numero", [None, "", "  ", "182675", "001-099-18",
                                    "001-099-000182675'; DROP TABLE x--"])
def test_numero_que_no_es_del_sri_no_pregunta_nada(numero):
    with patch("modules._lib.metabase_client.disponible") as m:
        res = fp.papel(numero)
    assert res["estado"] == "sin-numero"
    m.assert_not_called()


def test_sin_puente_no_es_sin_datos():
    with patch("modules._lib.metabase_client.disponible", return_value=False):
        assert fp.papel("001-099-000182675")["estado"] == "sin-puente"


def test_asinfo_no_contesta_es_error_no_factura_vacia():
    """Un "no hay nada" que en realidad es "no pude preguntar" es la mentira
    que ya costó el balance del 29/07."""
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], False)):
        assert fp.papel("001-099-000182675")["estado"] == "error"


def test_asinfo_contesta_y_no_conoce_la_factura():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], True)):
        assert fp.papel("001-099-000182675")["estado"] == "sin-datos"


def test_la_excepcion_del_puente_no_sube():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=RuntimeError("boom")):
        assert fp.papel("001-099-000182675")["estado"] == "error"


def test_el_exito_se_pregunta_una_sola_vez():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=(_filas(), True)) as m, \
         patch.object(fp, "_de_la_base", return_value=None), \
         patch.object(fp, "_guardar"):
        fp.papel("001-099-000182675")
        fp.papel("001-099-000182675")
    assert m.call_count == 1


def test_el_cache_guarda_solo_el_exito():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], False)), \
         patch.object(fp, "_de_la_base", return_value=None), \
         patch.object(fp, "_guardar") as g:
        fp.papel("001-099-000182675")
    g.assert_not_called()


# --- el código de barras ----------------------------------------------------

def test_el_codigo_de_barras_es_el_de_la_clave():
    ctx_barras = code128.barras(CLAVE)
    assert ctx_barras[0][0] == code128.SILENCIO
    assert code128.ancho_total(CLAVE) == 330      # el mismo ancho del PDF


def test_una_clave_que_no_son_digitos_no_dibuja_nada():
    """Mejor sin código de barras que con uno que lee cualquier cosa."""
    with patch.object(fp, "papel",
                      return_value={"estado": "ok", "cabecera": {"clave": "no-es"}}):
        ctx = fp.hoja("001-099-000182675")
    assert ctx["barras"] == [] and ctx["barras_ancho"] == 0


# --- la hoja, dibujada ------------------------------------------------------

def _render(app, hoja):
    from flask import g, render_template
    with app.test_request_context("/facturas/1/papel"):
        g.user = {"username": "test", "nombre_rol": "Accionista", "rol": 1}
        g.permisos = {"*"}
        return render_template("informes/factura_papel.html", numero=182675, **hoja)


def _hoja_ok(renglones=None):
    with patch.object(fp, "_de_la_base", return_value=None), \
         patch.object(fp, "_guardar"), \
         patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=(_filas(renglones), True)):
        return fp.hoja("001-099-000182675")


def test_la_hoja_dice_lo_que_dice_el_papel(app):
    html = _render(app, _hoja_ok())
    assert "FACTURA: 001-099-000182675" in html
    assert "1791125762001" in html                 # el RUC de Intela
    assert CLAVE in html                           # la clave de acceso
    assert "VERA VARGAS RAMON RODOLFO" in html
    assert "2.620,30" in html                      # VALOR TOTAL
    assert "2.278,50" in html                      # SUBTOTAL SIN IMPUESTOS
    assert "300,25" in html                        # los kilos
    assert "<rect" in html                         # el código de barras


def test_la_hoja_no_inventa_el_recuadro_del_SRI(app):
    with patch.object(fp, "papel", return_value={
            "estado": "ok", "emisor": {"nombre": "INTELA", "ruc": fp.RUC_INTELA,
                                       "matriz": "", "sucursal": "", "especial": "1478"},
            "cabecera": {"numero": "001-099-000182675", "clave": "",
                         "autorizacion": "", "fecha": "26/08/2026",
                         "ambiente": "PRODUCCION", "emision": "NORMAL",
                         "fecha_autorizacion": "", "forma_pago": "",
                         "forma_pago_texto": "", "referencia": ""},
            "cliente": {}, "renglones": [], "articulos": [],
            "totales": {"kilos": 0, "bruto": 0, "descuento": 0, "neto": 0,
                        "iva": 0, "total": 0}}):
        html = _render(app, fp.hoja("001-099-000182675"))
    assert "NÚMERO DE AUTORIZACIÓN" not in html
    assert "<rect" not in html


@pytest.mark.parametrize("estado,dice", [
    ("sin-numero", "no tiene número del SRI"),
    ("sin-datos", "Asinfo no tiene el detalle"),
    ("sin-puente", "no hay conexión con el ERP"),
    ("error", "Volvé a entrar en un rato"),
])
def test_cuando_no_hay_factura_la_hoja_lo_dice(app, estado, dice):
    with patch.object(fp, "papel", return_value={
            "estado": estado, "emisor": {}, "cabecera": {}, "cliente": {},
            "renglones": [], "articulos": [], "totales": {}}):
        html = _render(app, fp.hoja("001-099-000182675"))
    assert dice in html


def test_una_factura_de_192_renglones_no_rompe_la_hoja(app):
    """El 6,5% de las facturas pasa de 26 renglones, que es lo que entra en
    una carilla, y la más larga tiene 192. La tabla FLUYE y repite su
    encabezado en cada hoja en vez de estar posada en coordenadas fijas."""
    largos = [(*RENGLONES[i % len(RENGLONES)][:4], *RENGLONES[i % len(RENGLONES)][4:])
              for i in range(192)]
    html = _render(app, _hoja_ok(largos))
    assert html.count("<tr>") >= 192
    assert "<thead>" in html
    assert "page-break-inside:avoid" in html


# --- una SEGUNDA factura, con otro tramo de descuento ------------------------
#
# La 001-099-000182678 (CACHUPUD CUJI JOSE EFRAIN, 26/08/2026) se bajó de
# Asinfo a propósito: tiene 5% y **8%** donde la otra tiene 5% y 14%. Sirve
# para dos cosas — confirmar que la cuenta del pie no dependía del 14%, y
# poder copiar el cuadro de descuentos, que con un solo papel era adivinar.

#: (código, cantidad, precio_linea, descuento_linea | precio, descuento, total)
RENGLONES_182678 = [
    ("RSBLA", 1.20, 9.648, 1.215648, 9.25, 1.40, 9.70),
    ("JSBLA", 22.10, 168.623, 21.246498, 8.77, 24.43, 169.49),
]
CLAVE_182678 = "2608202601179112576200120010990001826780017717415"


def _filas_182678():
    filas = []
    for cod, cant, bruto, descu, _p, _d, _t in RENGLONES_182678:
        filas.append({
            "numero": "001-099-000182678", "fecha": "2026-08-26T00:00:00Z",
            "doc": 7, "autorizacion": CLAVE_182678, "clave": CLAVE_182678,
            "ambiente": 2, "tipo_emision": 1, "fecha_autorizacion": "2026-08-26",
            "forma_pago": "20", "base": 155.81, "iva_sri": 23.37,
            "guia": "001-099-000170016", "referencia": "riobamba",
            "cli_razon": "CACHUPUD CUJI JOSE EFRAIN", "cli_comercial": "CJE",
            "cli_ruc": "0603985516001", "codigo": cod, "descripcion": cod,
            "categoria": "TELAS", "acabado": "TUB", "color": "BLANCO",
            "calidad": "PRI", "cantidad": cant, "precio": bruto / cant,
            "bruto": bruto, "descuento": descu, "pct1": 5, "pct2": 8,
        })
    return filas


def test_la_segunda_factura_tambien_da_renglon_por_renglon():
    hoja = fp.armar(_filas_182678())
    for r, esperado in zip(hoja["renglones"], RENGLONES_182678, strict=False):
        cod, _c, _b, _d, precio, descu, total = esperado
        assert (cod, r["precio"], r["descuento"], r["total"]) == \
               (cod, precio, descu, total)


def test_el_pie_de_la_segunda_factura():
    t = fp.armar(_filas_182678())["totales"]
    assert (t["bruto"], t["descuento"], t["neto"], t["iva"], t["total"]) == \
           (179.19, 25.83, 155.82, 23.37, 179.18)
    assert t["kilos"] == 23.30


def test_el_cuadro_de_descuentos_copia_la_cuenta_rara_de_asinfo():
    """Abajo a la izquierda Asinfo imprime una resta que NO da:
    3.207,19 − 146,42 − 389,47 son 2.671,30, y la factura es de 2.620,30.
    Donde va el IVA pone 5%. Se copia igual, porque el cliente ya tiene ese
    papel y un número distinto lo hace dudar de si es la misma factura."""
    t675 = fp.armar(_filas())["totales"]
    assert (t675["valor_factura"], t675["desc_contado"], t675["desc_volumen"]) == \
           (3207.19, 146.42, 389.47)
    t678 = fp.armar(_filas_182678())["totales"]
    assert (t678["valor_factura"], t678["desc_contado"], t678["desc_volumen"]) == \
           (205.02, 9.36, 14.23)


def test_el_total_del_cuadro_es_el_total_de_verdad():
    """La resta del cuadro no da, pero el número de abajo sí: la hoja nunca
    miente en el que importa."""
    t = fp.armar(_filas_182678())["totales"]
    assert t["total"] == 179.18
    assert round(t["valor_factura"] - t["desc_contado"] - t["desc_volumen"], 2) \
        != t["total"]


def test_sin_descuento_no_se_dibuja_el_cuadro():
    filas = _filas_182678()
    for f in filas:
        f["pct1"] = f["pct2"] = 0
        f["descuento"] = 0
    t = fp.armar(filas)["totales"]
    assert t["desc_contado"] == 0 and t["desc_volumen"] == 0


def test_la_guia_de_remision_sale_en_la_hoja(app):
    """Una factura la trae y la otra no: la 182675 va con el campo vacío."""
    assert fp.armar(_filas_182678())["cabecera"]["guia"] == "001-099-000170016"
    assert fp.armar(_filas())["cabecera"]["guia"] == ""


def test_la_guia_de_remision_cuelga_del_despacho_no_de_la_factura():
    """`guia_remision` tiene las dos columnas y la de la factura viene SIEMPRE
    en blanco: con el enganche por factura el campo salía vacío siempre.

    La 001-099-000182678 sale con guía 001-099-000170016 (despacho 170290) y la
    001-099-000182675 sin ninguna, igual que los dos papeles de Asinfo.
    """
    sql = fp._sql("001-099-000182678")
    assert "gui.id_despacho_cliente = fc.id_despacho_cliente" in sql
    assert "gui.id_factura_cliente" not in sql
