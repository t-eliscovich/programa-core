"""El anticipo que sale y la mercadería que entra son UN renglón.

TMT 2026-08-10, sobre una ventana con −73.984 en Ant. y +75.026 en Stk.:
*"en el mismo bucket entra mercadería y sale anticipo, ¿no?"*. Sí: el anticipo
de la importación se volvió stock. Salían TRES renglones —el anticipo, la tela
y la revaluación del $/kg— y había que sumarlos a ojo para ver que era una
sola cosa.

⭐ Y la revaluación entra en el MISMO renglón a propósito. El primer intento
mostraba "entró 4.111 abajo del anticipo" y ella lo rechazó con razón: eso no
es un hecho, es la aritmética del promedio ponderado. Los 22.881 kg entraron a
$3,2334 con el stock a $3,0405, así que parte del costo se reparte sobre lo
que ya estaba (los 5.152 de "revaluación"). Partirlo inventaba una pérdida que
no existe. El $/kg queda como nota al pie: es lo que explica el signo.

🚨 El código de la importación NO se busca por la ventana: el anticipo baja
cuando Asinfo muestra la mercadería recibida y la compra la crea el automático
después (medido el 10/08: anticipo 11:26, compra `bap-auto` 11:38). Se busca
por IMPORTE, que coincide al centavo, y sólo se nombra si hay UN candidato.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes import traza as t  # noqa: E402


def _movs():
    """Los tres movimientos reales de la foto 2426 (10/08 06:26 EC)."""
    return [
        {"componente": "antic", "doc_id": "#recibidos", "tipo": "cambio",
         "etiqueta": t.TXT_ANTICIPO_RECIBIDO, "regla": "Anticipos",
         "aporte": -73983.89, "familia": "utilidad"},
        {"componente": "vsto", "doc_id": "#stock", "tipo": "cambio",
         "etiqueta": "entró a hilado y tejido y terminado", "regla": "Stock",
         "aporte": 69873.36, "familia": "utilidad"},
        {"componente": "vsto", "doc_id": "#stock:tarifa", "tipo": "cambio",
         "etiqueta": "cambió el $/kg de 3 etapas",
         "regla": "Revaluación de stock", "aporte": 5152.21,
         "familia": "utilidad"},
    ]


def _resumen(movs, compras=(), d_utilidad=1041.68):
    with patch.object(t.db, "fetch_all", return_value=list(compras)):
        return t.resumir(movs, d_utilidad, {}, hasta="2026-08-10T11:26:40")


def test_los_tres_renglones_son_uno():
    out = _resumen(_movs())
    assert len(out) == 1
    g = out[0]
    assert g["texto"] == "entró la mercadería de los anticipos"
    # Cada componente en SU columna, como la fila de arriba.
    assert g["por_col"]["antic"] == -73983.89
    assert g["por_col"]["vsto"] == round(69873.36 + 5152.21, 2)
    # Y el aporte sigue siendo el Δ de la ventana.
    assert g["aporte"] == round(-73983.89 + 69873.36 + 5152.21, 2)


def test_la_revaluacion_baja_a_la_nota():
    """No desaparece: es lo que explica que la ventana dé positiva."""
    g = _resumen(_movs())[0]
    assert g["nota"] == "cambió el $/kg de 3 etapas"


def test_nombra_la_importacion_por_el_importe():
    g = _resumen(_movs(), compras=[{"codigo_prov": "AC", "concepto": "33"}])[0]
    assert g["texto"] == "entró la mercadería del anticipo AC 33"


def test_con_dos_candidatos_no_inventa_el_nombre():
    g = _resumen(_movs(), compras=[{"codigo_prov": "AC", "concepto": "33"},
                                   {"codigo_prov": "MH", "concepto": "63"}])[0]
    assert g["texto"] == "entró la mercadería de los anticipos"


def test_los_kilos_se_pintan_en_el_renglon_unido():
    """El template pinta las tres columnas de kg sólo cuando `col == 'vsto'`:
    si el renglón unido se quedara con el componente del anticipo, los kilos
    de la mercadería desaparecerían de la pantalla."""
    assert _resumen(_movs())[0]["col"] == "vsto"


def test_sin_mercaderia_el_anticipo_sigue_solo():
    """Un anticipo que baja sin stock que suba NO es una importación que
    llegó: no se toca."""
    out = _resumen([_movs()[0]], d_utilidad=None)
    assert len(out) == 1
    assert out[0].get("texto_unido") is None


def test_el_stock_solo_tampoco_se_toca():
    out = _resumen(_movs()[1:], d_utilidad=None)
    assert len(out) == 2
    assert all(g.get("texto_unido") is None for g in out)


# ── El margen de la venta (TMT 2026-08-10) ──────────────────────────────────
# *"¿se puede? porque Asinfo saca un poco más tarde el stock real que la
# factura"*. Medido: sólo 68 de 137 ventanas con facturación tienen la salida
# de stock en la misma ventana, y por día tampoco (2 de 4). Así que el margen
# NO sale de parear las dos patas: sale de la venta misma, kg × $/kg de
# terminado, que no depende del reloj de Asinfo.


def test_el_margen_sale_de_la_venta_no_del_stock():
    n = t._nota_del_margen({"kg": 3100, "us": 12500, "ukg": 3.0300})
    assert n == "3.100 kg a $ 3,0300 el kilo de terminado → margen 3.107 (25%)"


def test_sin_los_tres_datos_no_hay_nota():
    """Las fotos anteriores a la mig 0185 no guardaron el $/kg de terminado:
    ahí el renglón queda como estaba, sin inventar un margen."""
    assert t._nota_del_margen({"kg": 3100, "us": 12500, "ukg": 0}) == ""
    assert t._nota_del_margen({"kg": 0, "us": 12500, "ukg": 3.03}) == ""
    assert t._nota_del_margen(None) == ""


def test_la_nota_cuelga_del_renglon_de_las_facturas():
    movs = [{"componente": "facturas", "doc_id": "f1", "tipo": "alta",
             "etiqueta": "Factura 181295 · JVL", "regla": "Venta facturada",
             "aporte": 12500.0, "familia": "utilidad"}]
    with patch.object(t.db, "fetch_all", return_value=[]):
        out = t.resumir(movs, 12500.0, {},
                        venta={"kg": 3100, "us": 12500, "ukg": 3.03})
    assert out[0]["nota"].startswith("3.100 kg a $ 3,0300 el kilo")


def test_la_foto_guarda_el_precio_de_cada_etapa():
    """Sin esto el margen no existe: el balance lo tiene, la foto lo tiraba."""
    bal = {"diagnostico": {"componentes": {"utilidad": 1.0}},
           "stock_etapas": {"hilado": {"kg": 10.0, "ukg": 3.0},
                            "tejido": {"kg": 5.0, "ukg": 4.0},
                            "terminado": {"kg": 2.0, "ukg": 5.0}}}
    f = t._fila_desde_balance(bal)
    assert f["tejido_ukg"] == 4.0
    assert f["terminado_ukg"] == 5.0


# ── La CONVERSIÓN: la otra mitad, la que no mueve plata ─────────────────────
#
# 🚨 TMT 2026-08-11, ventana de las 09:52 del AI 16: dos renglones que se
# anulan —"Menos anticipos cuya mercadería ya está en stock" +73.127 y
# "AN AI → CP 10148 (4)" −73.127— y ninguno nombra la importación.
# *"no sé qué es CP y ese número, quiero ver el AI 16"*.

def _movs_conversion():
    """El anticipo sale de `dolares` y el descuento sintético deja de aplicar."""
    return [
        # El descuento que tapaba el anticipo desaparece: +73.127.
        {"componente": "antic", "doc_id": "#recibidos", "tipo": "cambio",
         "etiqueta": t.TXT_ANTICIPO_RECIBIDO, "regla": "Anticipos",
         "aporte": 73127.25, "familia": "utilidad"},
        # Los 4 anticipos se van a la compra: −73.127.
        {"componente": "antic", "doc_id": "d901", "tipo": "baja",
         "etiqueta": "Anticipo AI · 16 SALDO", "regla": "Anticipos",
         "aporte": -73127.25, "familia": "utilidad"},
    ]


_EV_CONVERSION = {
    "d901": {"tipo": "bap_anticipo_a_compra", "grupo": "g1",
             "label": "Anticipo → Compra", "id_mov_doble": 77, "dia": "2026-08-11",
             "concepto": "AI 16 · 4 anticipo(s) → compra #10148",
             "meta": {"codigo_prov": "AI", "numero_compra": 10148,
                      "n_anticipos": 4}},
}


def _resumen_conv(movs, eventos, d_utilidad=0.0):
    with patch.object(t.db, "fetch_all", return_value=[]):
        return t.resumir(movs, d_utilidad, eventos, hasta="2026-08-11T14:52:28")


def test_la_conversion_es_un_solo_renglon_que_dice_AI_16():
    out = _resumen_conv(_movs_conversion(), _EV_CONVERSION)
    assert len(out) == 1, [g["texto"] for g in out]
    g = out[0]
    assert g["texto"] == "AI 16 · 4 anticipos pasaron a la compra"
    assert "CP" not in g["texto"] and "10148" not in g["texto"]
    # No movió plata: aporta cero…
    assert g["aporte"] == 0.0
    # …pero NO se cae por chico: el bruto guarda lo que movió.
    assert g["bruto"] == 73127.25
    assert g["nota"] == "no mueve plata: la mercadería ya había entrado"


def test_la_conversion_sola_tambien_dice_AI_16():
    """Sin la pata del descuento (ventanas donde sólo llega la conversión)."""
    movs = [m for m in _movs_conversion() if m["doc_id"] == "d901"]
    out = _resumen_conv(movs, _EV_CONVERSION)
    assert out[0]["texto"] == "AI 16 · 4 anticipos pasaron a la compra"


def test_el_reverso_dice_que_se_deshizo():
    ev = {"d901": {**_EV_CONVERSION["d901"],
                   "tipo": "bap_anticipo_a_compra_reverso"}}
    movs = [m for m in _movs_conversion() if m["doc_id"] == "d901"]
    out = _resumen_conv(movs, ev)
    assert out[0]["texto"] == "AI 16 · se deshizo el pase de 4 anticipos a la compra"
    # El reverso NO se funde con el descuento: no es la misma formalidad.
    out2 = _resumen_conv(_movs_conversion(), ev)
    assert len(out2) == 2, [g["texto"] for g in out2]


def test_sin_concepto_no_inventa_un_nombre():
    ev = {"d901": {**_EV_CONVERSION["d901"], "concepto": ""}}
    out = _resumen_conv(_movs_conversion(), ev)
    assert out[0]["texto"] == "4 anticipos pasaron a la compra"


def test_si_no_se_anulan_no_se_funden():
    """Dos cosas distintas que caen juntas siguen siendo dos renglones."""
    movs = _movs_conversion()
    movs[0]["aporte"] = 50000.0        # ya no cancela al de la conversión
    out = _resumen_conv(movs, _EV_CONVERSION, d_utilidad=-23127.25)
    assert len(out) == 2, [g["texto"] for g in out]


def test_un_solo_anticipo_no_dice_uno():
    """Con uno solo el número estorba: "el anticipo", no "1 anticipo"."""
    ev = {"d901": {**_EV_CONVERSION["d901"],
                   "meta": {**_EV_CONVERSION["d901"]["meta"], "n_anticipos": 1}}}
    out = _resumen_conv(_movs_conversion(), ev)
    assert out[0]["texto"] == "AI 16 · el anticipo pasó a la compra"


# ── Todo junto en UNA ventana (TMT 2026-08-14) ──────────────────────────────
#
# 🚨 Ventana de las 08:40 (foto 3425), la importación AC 38: el pase a la
# compra −74.769 en Ant. y el hilo que entra +68.548 en Stk. (+22.993 kg) más
# la revaluación +5.832, en tres renglones sueltos.
# *"me gustaría que la entrada de hilo aparezca arriba con el anticipo"*.
#
# Es el mismo hecho del 10/08, pero acá Asinfo mostró la mercadería y el
# automático creó la compra DENTRO de la misma foto de 5 minutos: el descuento
# sintético nunca llegó a aparecer, así que la unión se quedó sin ancla.

_EV_AC38 = {
    "d38": {"tipo": "bap_anticipo_a_compra", "grupo": "g38",
            "label": "Anticipo → Compra", "id_mov_doble": 380,
            "dia": "2026-08-14",
            "concepto": "AC 38 · 4 anticipo(s) → compra #10176",
            "meta": {"codigo_prov": "AC", "numero_compra": 10176,
                     "n_anticipos": 4}},
}


def _movs_ac38():
    """Los cuatro movimientos reales de la foto 3425 (14/08 08:40 EC)."""
    return [
        {"componente": "antic", "doc_id": "d38", "tipo": "baja",
         "etiqueta": "Anticipo AC · 38 SALDO", "regla": "Anticipos",
         "aporte": -74769.12, "familia": "utilidad"},
        {"componente": "vsto", "doc_id": "#stock", "tipo": "cambio",
         "etiqueta": "salió de tejido y terminado · entró a hilado",
         "regla": "Stock", "aporte": 68547.55, "familia": "utilidad"},
        {"componente": "vsto", "doc_id": "#stock:tarifa", "tipo": "cambio",
         "etiqueta": "cambió el $/kg de 3 etapas",
         "regla": "Revaluación de stock", "aporte": 5831.86,
         "familia": "utilidad"},
    ]


def test_el_pase_a_la_compra_tambien_ancla_la_union():
    out = _resumen_conv(_movs_ac38(), _EV_AC38, d_utilidad=-389.71)
    assert len(out) == 1, [g["texto"] for g in out]
    g = out[0]
    assert g["texto"] == "AC 38 · entró la mercadería de 4 anticipos"
    assert g["por_col"]["antic"] == -74769.12
    assert g["por_col"]["vsto"] == round(68547.55 + 5831.86, 2)
    assert g["aporte"] == round(-74769.12 + 68547.55 + 5831.86, 2)
    # Los kilos del hilado se pintan sólo si el renglón es el del stock.
    assert g["col"] == "vsto"
    assert g["nota"] == "cambió el $/kg de 3 etapas"


def test_no_sale_a_buscar_la_compra_por_importe():
    """El concepto del pase ya trae el nombre: `_importacion_del_anticipo`
    haría una consulta de más y podría nombrar otra importación."""
    with patch.object(t, "_importacion_del_anticipo") as buscar:
        _resumen_conv(_movs_ac38(), _EV_AC38, d_utilidad=-389.71)
    buscar.assert_not_called()


def test_con_un_solo_anticipo_no_dice_uno():
    ev = {"d38": {**_EV_AC38["d38"],
                  "meta": {**_EV_AC38["d38"]["meta"], "n_anticipos": 1}}}
    out = _resumen_conv(_movs_ac38(), ev, d_utilidad=-389.71)
    assert out[0]["texto"] == "AC 38 · entró la mercadería del anticipo"


def test_el_pase_sin_mercaderia_sigue_diciendo_que_paso_a_la_compra():
    """Sin stock que suba no hay importación que llegó: el renglón de siempre."""
    movs = [m for m in _movs_ac38() if m["componente"] == "antic"]
    out = _resumen_conv(movs, _EV_AC38, d_utilidad=-74769.12)
    assert out[0]["texto"] == "AC 38 · 4 anticipos pasaron a la compra"


def test_el_descuento_sintetico_le_gana_al_pase():
    """Cuando están los dos, el pase es la OTRA formalidad (la que no mueve
    plata) y lo une `_unir_conversion_del_anticipo`: si el pase anclara la
    unión con el stock, esa ventana quedaría contada dos veces."""
    out = _resumen_conv(_movs_conversion(), _EV_CONVERSION)
    assert len(out) == 1, [g["texto"] for g in out]
    assert out[0]["texto"] == "AI 16 · 4 anticipos pasaron a la compra"
