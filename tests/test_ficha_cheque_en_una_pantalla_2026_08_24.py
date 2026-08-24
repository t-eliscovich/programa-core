"""La ficha del cheque entra en una pantalla.

TMT 2026-08-24 (dueña): *"el cheque debería entrar en una pantalla. demasiada
info toda distendida"*, y sobre el primer intento: *"muy desalineado
todavía"*.

Medido con el navegador sobre la ficha renderizada (cheque 99578, sin aplicar):
**819 px → 278 px**. Lo que se fue:

  · la caja de la observación: 146 px para mostrar un cuadro de texto VACÍO;
  · la caja "Cliente que lo entregó": 99 px con título propio;
  · las dos tablas de ocho columnas que sólo decían "no hay nada": 230 px.

El primer intento las reemplazó por cuatro frases sueltas y quedó peor —de ahí
lo de "desalineado"—. Ahora los cuatro datos van en UNA rejilla de dos
columnas: rótulo a la izquierda, dato a la derecha, todo sobre la misma línea
vertical.
"""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
FICHA = (RAIZ / "modules/cheques/templates/cheques/detalle.html").read_text(
    encoding="utf-8")


def test_los_cuatro_datos_van_en_una_rejilla():
    """Rótulo y dato en columnas fijas: es lo que los alinea."""
    assert "grid-template-columns: 132px minmax(0, 1fr);" in FICHA
    for rotulo in ("Lo entregó", "Observación", "Facturas", "Depósitos"):
        assert f">{rotulo}</dt>" in FICHA, f"falta el renglón {rotulo}"


def test_el_codigo_del_cliente_va_ANTES_que_el_nombre():
    """Dueña: *"poner código de cliente antes que nombre y me gusta más"*.
    Es como se lee al cliente en todas las grillas del programa."""
    dl = FICHA[FICHA.index("<dt"):FICHA.index("</dl>")]
    pos_cod = dl.index("{{ ch.codigo_cli }}")
    pos_nom = dl.index("{{ ch.cliente or '' }}")
    assert pos_cod < pos_nom, "el nombre quedó adelante del código"


def test_la_observacion_no_dibuja_el_cuadro_de_texto_hasta_que_la_tocan():
    assert 'class="ch-nota-form hidden' in FICHA
    assert "ch-nota-abrir" in FICHA
    assert "function" not in FICHA[:FICHA.index("<dl")]  # el JS va al final


def test_las_tablas_solo_salen_cuando_tienen_filas():
    """Vacías eran 230 px para decir dos frases que ya están en la rejilla."""
    assert "{% if aplicaciones %}" in FICHA
    assert "{% if depositos %}" in FICHA
    assert "Cheque aún no aplicado a facturas." not in FICHA
    assert re.search(r"empty_row\(6", FICHA) is None


def test_no_quedaron_las_cajas_viejas():
    assert "Cliente que lo entregó" not in FICHA
    assert "APLICADO A FACTURAS" not in FICHA.upper() or "{% if aplicaciones %}" in FICHA


def test_los_seis_cuadros_de_arriba_siguen_igual():
    """El importe y las tres fechas son lo primero que se mira: no se tocan."""
    for rotulo in ("Importe", "Cargado", "A depositar", "Depositado",
                   "Banco emisor", "Doc. banco"):
        assert rotulo in FICHA
