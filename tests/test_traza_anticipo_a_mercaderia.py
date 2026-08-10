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
