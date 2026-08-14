"""El buscador de la conciliación tiene que entender nuestros importes.

TMT 2026-08-14. La dueña escribió **1000** en el filtro y le aparecieron
100,00 y 10,00 — y no le apareció ninguno de 1.000,00.

`norm()` estaba escrita para formato americano: borraba la COMA y dejaba el
PUNTO. Pero los importes se muestran como acá (1.234,56), así que borraba el
separador DECIMAL y conservaba el de MILES:

      100,00  ->  10000   contiene "1000"  → aparecía, y no es mil
       10,00  ->   1000   contiene "1000"  → aparecía, y no es mil
    1.000,00  ->  1.00000 NO lo contiene   → no aparecía, y sí era mil

El test corre la función REAL sacada del template con node, así no se puede
arreglar el test sin arreglar la pantalla.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

_TPL = (_RAIZ / "modules" / "conciliacion" / "templates" / "conciliacion"
        / "_banco_v2_tab_manual.html")


def _norm_js() -> str:
    m = re.search(r"function norm\(s\)\{.*?\n  \}", _TPL.read_text(encoding="utf-8"), re.S)
    assert m, "no encontré norm() en el template — ¿la renombraron?"
    return m.group(0)


def _norm(valores: list[str]) -> dict[str, str]:
    if not shutil.which("node"):
        pytest.skip("sin node")
    js = _norm_js() + (
        "\nconst v = " + json.dumps(valores) + ";"
        "\nconst o = {}; for (const c of v) o[c] = norm(c);"
        "\nconsole.log(JSON.stringify(o));"
    )
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


IMPORTES = ["1.000,00", "100,00", "10,00", "21.000,00", "1.500,00"]


def test_buscar_1000_no_trae_cien_ni_diez():
    """El caso exacto que reportó la dueña."""
    d = _norm(IMPORTES)
    aparecen = [k for k, v in d.items() if "1000" in v]
    assert "100,00" not in aparecen, "100,00 no es mil"
    assert "10,00" not in aparecen, "10,00 no es mil"


def test_buscar_1000_si_trae_los_de_mil():
    d = _norm(IMPORTES)
    aparecen = [k for k, v in d.items() if "1000" in v]
    assert "1.000,00" in aparecen, "el de mil tiene que aparecer"


def test_da_igual_como_lo_escriba():
    """1000, 1.000 y 1000,00 tienen que buscar lo mismo."""
    d = _norm(["1000", "1.000", "1000,00", "1.000,00"])
    assert d["1000"] == d["1.000"] == "1000"
    assert d["1000,00"] == d["1.000,00"] == "1000.00"


def test_el_numero_de_documento_no_se_toca():
    """Los documentos del banco son números largos sin separadores: si la
    normalización los tocara, no se podría buscar por documento."""
    d = _norm(["5107346", "60968769", "31790629"])
    assert d["5107346"] == "5107346"
    assert d["60968769"] == "60968769"


def test_los_centavos_siguen_distinguiendo():
    d = _norm(["1.288,05", "1.288,50"])
    assert d["1.288,05"] != d["1.288,50"], "05 y 50 no son lo mismo"
