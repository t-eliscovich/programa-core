"""Ordenar una columna NO despega las filas de detalle de su fila madre.

TMT 2026-08-20 (dueña: *"no me anda el + en ingreso de hilado"*). En
/importaciones el desglose de una compra o de un anticipo son filas HERMANAS
que viven justo debajo de la fila de la importación, y el "+" las encuentra
caminando con `nextElementSibling`. `sortTable` ordenaba FILA POR FILA, así que
al clickear cualquier título de columna las hijas quedaban colgadas de otra
importación: medido en vivo, 94 de los 115 "+" de la pantalla dejaban de abrir.

Ahora se ordenan BLOQUES (madre + hijas). El test corre el `sortTable` REAL con
node contra un DOM mínimo — el bug es de ORDEN, no de layout, así que un DOM de
juguete alcanza para reproducirlo.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
JS = RAIZ / "static" / "sortable-tables.js"

# DOM de juguete: sólo lo que `sortTable` toca de verdad.
_SCRIPT = """
global.document = {
  readyState: 'complete',
  addEventListener: () => {},
  querySelectorAll: () => [],
  createElement: () => ({ className: '', textContent: '',
                          appendChild() {}, querySelector() { return null; } }),
  createDocumentFragment: () => ({ hijos: [], appendChild(n) { this.hijos.push(n); } }),
};

const m = require(process.argv[1]);

function fila(clases, texto) {
  const set = new Set(clases);
  return {
    texto,
    classList: { contains: (c) => set.has(c) },
    hasAttribute: (a) => set.has(a),
    cells: [{ getAttribute: () => null, textContent: texto,
              classList: { toggle() {} } }],
  };
}

// Una importación con 1 hija, otra con 2, otra sin hijas, y un total fijo.
const filas = [
  fila([], 'CCC'), fila(['acc-detail-row'], ''),
  fila([], 'AAA'), fila(['data-fila-hija'], ''), fila(['data-fila-hija'], ''),
  fila([], 'BBB'),
  fila(['no-sort'], 'TOTAL'),
];

const tb = { _r: filas.slice() };
Object.defineProperty(tb, 'rows', { get: () => tb._r });
tb.appendChild = (frag) => {
  for (const n of frag.hijos) {
    const i = tb._r.indexOf(n);
    if (i >= 0) tb._r.splice(i, 1);
    tb._r.push(n);
  }
};

const th = { dataset: {}, classList: { contains: () => false },
             hasAttribute: () => false, querySelector: () => null,
             appendChild() {}, setAttribute() {}, getAttribute: () => null };
const table = { tHead: { rows: [{ cells: [th] }] }, tBodies: [tb], dataset: {} };

m.sortTable(table, 0, 'asc');

const orden = tb._r.map((r) => (r.texto || '·hija'));
console.log(JSON.stringify({ orden, esFilaHija: [
  m.esFilaHija(fila(['acc-detail-row'], '')),
  m.esFilaHija(fila(['data-fila-hija'], '')),
  m.esFilaHija(fila([], 'AAA')),
]}));
"""


def _correr_node() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node no disponible en este entorno")
    out = subprocess.run(
        [node, "-e", _SCRIPT, str(JS)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(out.stdout)


def test_las_hijas_viajan_con_su_madre_al_ordenar():
    orden = _correr_node()["orden"]
    # AAA se lleva sus DOS hijas, BBB no tiene, CCC se lleva la suya, y el
    # total con class="no-sort" queda al final.
    assert orden == ["AAA", "·hija", "·hija", "BBB", "CCC", "·hija", "TOTAL"]


def test_se_reconoce_una_fila_de_detalle_por_clase_o_por_atributo():
    assert _correr_node()["esFilaHija"] == [True, True, False]
