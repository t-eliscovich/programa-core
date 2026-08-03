"""El comparador de static/sortable-tables.js manda los vacíos al final EN LAS
DOS DIRECCIONES.

TMT 2026-08-03 (dueña: "tiene que estar ordenado como depositado descendiente
y no lo veo bien"). `compareValues` ya devolvía los vacíos al final, pero
`sortTable` negaba TODO el resultado para 'desc' — incluido ese desempate — así
que en descendente los '—' subían al tope y tapaban las filas con dato. Afecta
a todas las tablas sortables de la app, no sólo la columna Depositado.

El test corre el comparador REAL con node (el archivo se exporta con
`module.exports` cuando lo carga node; en el browser `module` no existe).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
JS = RAIZ / "static" / "sortable-tables.js"

_SCRIPT = """
const m = require(process.argv[1]);
const vals = [NaN, 3, NaN, 1, 2];
const norm = (xs) => xs.map((v) => (Number.isNaN(v) ? null : v));
console.log(JSON.stringify({
  desc: norm(vals.slice().sort(m.comparadorFilas('date', 'desc'))),
  asc:  norm(vals.slice().sort(m.comparadorFilas('date', 'asc'))),
  texto_desc: vals
    .map((v) => (Number.isNaN(v) ? '' : String(v)))
    .sort(m.comparadorFilas('text', 'desc')),
}));
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


def test_vacios_al_final_en_desc_y_en_asc():
    r = _correr_node()
    # Descendente: 3, 2, 1 y los vacíos ABAJO (antes salían arriba).
    assert r["desc"] == [3, 2, 1, None, None]
    # Ascendente: sigue igual que siempre.
    assert r["asc"] == [1, 2, 3, None, None]


def test_vacios_al_final_tambien_en_columnas_de_texto():
    r = _correr_node()
    assert r["texto_desc"][-2:] == ["", ""]
