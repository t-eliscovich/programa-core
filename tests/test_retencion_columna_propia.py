"""La RETENCIÓN es una columna propia, no una parte escondida del abono.

TMT 2026-08-07 (dueña): *"ayer hicimos código para que retenciones se sume a
abono. necesitaríamos dividir esto en dos columnas: retenciones y abono, no
todo junto (entonces no se debería sumar más)"*.

Qué se protege acá:

1. La ARITMÉTICA: saldo = importe − abono − retención, en UNA sola función
   (`facturas.queries.saldo_de`) que usan los diez lugares que recalculan un
   saldo. Si alguien vuelve a `importe − abono` en cualquiera de ellos, la
   retención desaparece del saldo y el cliente queda debiendo plata que ya
   retuvo.
2. La FORMA de las dos tablas donde ella lo va a leer. Agregar una columna a
   mano es exactamente donde este repo ya se lastimó dos veces (los colspan
   del pie del estado de cuenta, 2026-07-06 y 2026-08-04): los números estaban
   bien y la alineación no. Acá se cuentan las celdas.
3. Los ANCHOS de impresión: en el motor de PDF el ancho de la tabla ES la suma
   de sus columnas. Si suman más de 100%, la hoja que se le manda al cliente
   se va del papel.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TPL_FACTURAS = ROOT / "modules/facturas/templates/facturas/lista.html"
TPL_EC = ROOT / "modules/informes/templates/informes/_estado_cuenta_impreso.html"


# ─────────────────────────── 1. la aritmética ──────────────────────────────

@pytest.mark.parametrize(
    "importe, abono, retencion, esperado",
    [
        (100.0, 0.0, 0.0, 100.0),      # factura recién emitida
        (100.0, 27.01, 0.0, 72.99),    # sólo abono
        (100.0, 0.0, 27.01, 72.99),    # sólo retención — pesa igual que el abono
        (100.0, 51.98, 27.01, 21.01),  # los dos conviven (el caso de la dueña)
        (100.0, 90.0, 10.0, 0.0),      # cancelada entre pago y retención
        (-812.40, 0.0, 0.0, -812.40),  # nota de crédito
        (100.0, None, None, 100.0),    # columnas NULL de filas viejas
    ],
)
def test_saldo_de(importe, abono, retencion, esperado):
    from modules.facturas.queries import saldo_de
    assert saldo_de(importe, abono, retencion) == esperado


def test_ningun_recalculo_de_saldo_quedo_con_la_formula_vieja():
    """Ningún módulo vuelve a hacer `importe − abono` a mano.

    Este es el test que caza el olvido: la fórmula cambió, y basta con que UNO
    de los diez lugares que la recalculan siga restando sólo el abono para que
    la retención se pierda al aplicar un cheque, al reversarlo o al totalizar.
    """
    sospechosos = []
    for mod in ("modules/cheques/queries.py", "modules/facturas/queries.py",
                "modules/informes/queries.py", "modules/retenciones/queries.py"):
        for n, linea in enumerate((ROOT / mod).read_text(encoding="utf-8").splitlines(), 1):
            # Sólo ASIGNACIONES reales (la línea arranca con `algo =`), para
            # no cazar docstrings que cuentan la historia de la fórmula.
            if re.match(r"\s*\w[\w\[\]\.\"']*\s*=\s*"
                        r"(round\()?\s*(float\()?[\w\[\]\"'.()]*importe"
                        r"[\w\[\]\"'.()]*\s*-\s*(float\()?[\w\[\]\"'.()]*abono",
                        linea):
                sospechosos.append(f"{mod}:{n}: {linea.strip()}")
    assert not sospechosos, (
        "estas líneas calculan el saldo sin restar la retención — usar "
        "facturas.queries.saldo_de():\n  " + "\n  ".join(sospechosos)
    )


# ─────────────────────────── 2. la forma de las tablas ─────────────────────

def _celdas(fragmento: str, tag: str) -> int:
    return len(re.findall(rf"<{tag}[\s>]", fragmento))


def test_facturas_lista_columnas_cuadran():
    """colgroup, encabezado, fila y fila-vacía tienen que decir lo mismo."""
    h = TPL_FACTURAS.read_text(encoding="utf-8")
    colgroup = h[h.index("<colgroup>"):h.index("</colgroup>")]
    n_cols = _celdas(colgroup, "col")
    thead = h[h.index("<thead"):h.index("</thead>")]
    n_th = _celdas(thead, "th")
    vacia = int(re.search(r"empty_row\((\d+),", h).group(1))
    assert n_cols == n_th == vacia, (
        f"colgroup={n_cols}, th={n_th}, empty_row={vacia} — si no coinciden, "
        "la tabla queda corrida una columna a la izquierda"
    )
    assert "Retención" in thead


def test_facturas_lista_anchos_suman_100_y_la_tabla_es_fija():
    """Con `table-fixed` + anchos al 100% la tabla mide exactamente lo que su
    contenedor: agregar una columna no puede empujarla fuera de la pantalla.
    Sin `table-fixed` el navegador la agranda según el contenido y aparece el
    scroll horizontal — que es lo que la dueña pidió chequear al sumar la
    columna de retención (07/08)."""
    h = TPL_FACTURAS.read_text(encoding="utf-8")
    colgroup = h[h.index("<colgroup>"):h.index("</colgroup>")]
    anchos = [int(x) for x in re.findall(r"width:\s*(\d+)%", colgroup)]
    assert sum(anchos) == 100, f"los anchos suman {sum(anchos)}%: {anchos}"
    tabla = h[h.rindex("<table", 0, h.index("<colgroup>")):h.index("<colgroup>")]
    assert "table-fixed" in tabla and "w-full" in tabla, tabla


def test_estado_cuenta_tiene_columna_retencion_y_las_celdas_cuadran():
    """Encabezado, fila de factura y pie: la misma cantidad de celdas.

    El pie usa colspan=2 para el rótulo "Totales", así que se cuenta el
    colspan, no las celdas: es justo lo que se rompió el 04/08 en el papel.
    """
    h = TPL_EC.read_text(encoding="utf-8")
    thead = h[h.index("<thead"):h.index("</thead>")]
    n_th = _celdas(thead, "th")
    assert "Retención" in thead, "falta la columna Retención en el encabezado"

    # Primera fila de facturas (la del bucle sobre data.facturas).
    i = h.index("{% for f in data.facturas %}")
    fila = h[i:h.index("</tr>", i)]
    assert _celdas(fila, "td") == n_th, (
        f"la fila tiene {_celdas(fila, 'td')} celdas y el encabezado {n_th}"
    )
    assert "ec-c-ret" in fila

    tfoot = h[h.index("<tfoot"):h.index("</tfoot>")]
    pie = tfoot[tfoot.index("<tr"):tfoot.index("</tr>")]
    ancho_pie = _celdas(pie, "td") + sum(
        int(c) - 1 for c in re.findall(r'colspan="(\d+)"', pie)
    )
    assert ancho_pie == n_th, (
        f"el pie ocupa {ancho_pie} columnas y la tabla tiene {n_th} — los "
        "totales van a caer bajo la columna equivocada"
    )


def test_estado_cuenta_anchos_de_impresion_suman_100():
    """En el PDF el ancho de la tabla ES la suma de sus columnas."""
    h = TPL_EC.read_text(encoding="utf-8")
    anchos = [int(x) for x in re.findall(
        r"\.ec-c-\w+\s*\{\s*width:\s*(\d+)%", h)]
    assert anchos, "no encontré los anchos ec-c-* de impresión"
    assert sum(anchos) == 100, f"suman {sum(anchos)}%: {anchos}"
