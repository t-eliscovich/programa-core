"""La flechita de una columna de fecha ordena también por la HORA.

Dueña 11/09/2026 (en Novedades, después de arreglar el ORDER BY del server):
"¿hay otro orden mal?". Sí: `parseDate` de static/sortable-tables.js leía
sólo "dd/mm/yyyy" y tiraba lo que seguía, así que "31/08/2026 19:00" y
"31/08/2026 08:46" empataban y dentro del mismo día las filas quedaban en
cualquier orden al clickear el header. Ahora la hora (y los segundos, si
vienen) entran en el valor, en los dos formatos (dd/mm/yyyy y yyyy-mm-dd).

Corre el parser REAL con node, como test_sortable_tables_vacios.py.
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
const vals = ['31/08/2026 19:00', '31/08/2026 08:46', '31/08/2026',
              '2026-08-31 17:47', '2026-08-31T10:04:30', '11/09/2026 00:01',
              'hola'];
const parsed = vals.map((v) => m.parseDate(v));
const orden = vals.slice(0, 6)
  .map((v) => [v, m.parseDate(v)])
  .sort((a, b) => b[1] - a[1])
  .map((p) => p[0]);
console.log(JSON.stringify({
  parsed: parsed.map((p) => (Number.isNaN(p) ? null : p)),
  orden,
  tipos: vals.map((v) => (Number.isNaN(m.parseDate(v)) ? 'no' : 'date')),
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


def test_la_hora_desempata_dentro_del_mismo_dia():
    r = _correr_node()
    p = r["parsed"]
    # 19:00 > 17:47 > 10:04 > 08:46 > 00:00 (sin hora), todos del 31/08.
    assert p[0] > p[3] > p[4] > p[1] > p[2]
    # y el 11/09 a las 00:01 sigue después de cualquier hora del 31/08.
    assert p[5] > p[0]
    assert r["orden"] == ['11/09/2026 00:01', '31/08/2026 19:00',
                          '2026-08-31 17:47', '2026-08-31T10:04:30',
                          '31/08/2026 08:46', '31/08/2026']


def test_sin_hora_sigue_siendo_fecha_y_el_texto_no():
    r = _correr_node()
    assert r["tipos"] == ['date'] * 6 + ['no']
    # sin hora = medianoche, un múltiplo exacto de minuto
    assert r["parsed"][2] % 60000 == 0
