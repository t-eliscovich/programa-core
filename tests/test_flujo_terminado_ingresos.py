"""MOVIMIENTOS DEL MES (inicial Asinfo) — la celda TERM. KG de Ingresos vuelve.

Dueña 2026-08-04, mirando /informes/flujo-produccion mientras investigaba por
qué se movió la utilidad: *"¿podés volver a agregar ingresos terminado en esta
tabla?"*. La celda se había vaciado el 2026-07-31 (commit c4acf0d4, pedido de
Federico) y sin ella la fila de terminado no dice de dónde salió el stock.

Es un test de TEMPLATE y no de vista a propósito: lo que se rompió —y lo que
se puede volver a romper— es que alguien deje el `<td>` en blanco. Renderizar
toda la pantalla no probaría nada más y arrastra medio informe de fixtures.

El chequeo es POSICIONAL: no alcanza con que `ingresos_kg` de terminado
aparezca en el archivo, tiene que estar en la 7ª celda (TERM. KG) de la fila
Ingresos. Si alguien la mueve de columna, esto falla.
"""
from __future__ import annotations

import re
from pathlib import Path

TPL = (Path(__file__).resolve().parents[1]
       / "modules/informes/templates/informes/flujo_produccion.html")

#: Orden de las columnas de la tabla, según su <thead>.
COL_TERM_KG = 6  # 0 = etiqueta, 1 HILADO KG, 2 $/KG, 3 $, 4 vacía, 5 CRUDO KG


def _fila(nombre: str) -> list[str]:
    """Las celdas <td> de la fila cuyo <td class="lbl"> es `nombre`."""
    html = TPL.read_text(encoding="utf-8")
    filas = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    for f in filas:
        if re.search(rf'class="lbl"[^>]*>\s*{nombre}\s*<', f):
            return re.findall(r"<td[^>]*>(.*?)</td>", f, re.S)
    raise AssertionError(f"no encontré la fila «{nombre}» en {TPL.name}")


def test_ingresos_terminado_no_esta_vacia():
    celda = _fila("Ingresos")[COL_TERM_KG].strip()
    assert celda, "la celda TERM. KG de Ingresos quedó en blanco"
    assert "ate.get('ingresos_kg')" in celda, (
        "TERM. KG de Ingresos tiene que mostrar el ingreso de terminado")


def test_ingresos_terminado_no_muestra_el_pct_de_merma():
    """Dueña 2026-08-04: *"acordate de sacar merma"*. Vuelven los kg, no el
    porcentaje de merma de tintura que antes los acompañaba."""
    celda = _fila("Ingresos")[COL_TERM_KG]
    assert "ingresos_pct" not in celda


def test_la_celda_sigue_siendo_la_columna_de_terminado():
    """Guarda del índice: si el <thead> cambia de orden, el test de arriba
    estaría mirando otra columna sin enterarse."""
    html = TPL.read_text(encoding="utf-8")
    thead = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
    ths = [re.sub(r"<[^>]+>", "", t).strip()
           for t in re.findall(r"<th[^>]*>.*?</th>", thead, re.S)]
    assert ths[COL_TERM_KG] == "TERM. KG"
