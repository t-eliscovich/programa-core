"""Un error atajado tiene que dejar rastro.

TMT 2026-08-13. Después de una tarde en la que casi todo lo que apareció
tenía la misma forma —algo que no anda y no avisa— conté los lugares donde el
código atrapa una excepción y no hace absolutamente nada: **132 en 54
archivos**, 17 de ellos envolviendo una escritura a la base.

La mayoría son best-effort a propósito (la foto del saldo, un contador de la
pantalla, un snapshot de auditoría) y está bien que no volteen la operación.
Lo que no está bien es que, cuando fallan, no quede NADA con qué enterarse.

Los 85 que estaban en `modules/` ahora dejan un renglón vía
`modules._lib.silencios.avisar`. Estos tests cuidan las dos mitades: que
avisar no pueda romper nada, y que el `except: pass` no vuelva de a poco.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))


# ── avisar: deja rastro y NUNCA rompe ─────────────────────────────────

def test_deja_un_renglon_con_donde_y_que_paso(caplog):
    from modules._lib.silencios import avisar

    with caplog.at_level(logging.WARNING):
        avisar("programa_core.prueba", "guardar_foto", ValueError("no cerró"))

    assert len(caplog.records) == 1
    texto = caplog.records[0].getMessage()
    assert "guardar_foto" in texto, "tiene que decir DÓNDE"
    assert "ValueError" in texto and "no cerró" in texto, "y QUÉ pasó"


def test_el_nivel_debug_es_para_los_angostos(caplog):
    """Los `except (TypeError, ValueError)` de parseo por fila van a debug:
    si fueran warning inundarían el log con un renglón por renglón."""
    from modules._lib.silencios import avisar

    with caplog.at_level(logging.WARNING):
        avisar("programa_core.prueba", "parsear_fila", TypeError("x"),
               nivel="debug")
    assert caplog.records == []

    with caplog.at_level(logging.DEBUG):
        avisar("programa_core.prueba", "parsear_fila", TypeError("x"),
               nivel="debug")
    assert len(caplog.records) == 1


def test_avisar_jamas_puede_romper_lo_que_estaba_atajando():
    """Si avisar levantara, convertiría un error atajado en una pantalla
    rota — justo lo contrario de lo que vino a hacer."""
    from modules._lib import silencios

    class _Explota:
        def __str__(self):
            raise RuntimeError("ni siquiera se puede imprimir")

    silencios.avisar("programa_core.prueba", "x", _Explota())
    silencios.avisar(None, "x", ValueError("y"), nivel="no_existe_este_nivel")


# ── el candado: que `except: pass` no vuelva de a poco ────────────────

_RX_EXC = re.compile(r'^(\s*)except\b[^\n:]*:\s*$')
_RX_PASS = re.compile(r'^(\s*)pass\s*$')


def _silencios_mudos(carpeta: Path) -> list[str]:
    hallazgos = []
    for py in sorted(carpeta.rglob("*.py")):
        if "__pycache__" in str(py):
            continue
        lineas = py.read_text(encoding="utf-8").split("\n")
        for i, ln in enumerate(lineas[:-1]):
            m = _RX_EXC.match(ln)
            p = _RX_PASS.match(lineas[i + 1])
            if m and p and p.group(1) > m.group(1):
                if "pragma: no cover" in ln or "silencio ok" in ln:
                    continue
                rel = os.path.relpath(py, _RAIZ)
                hallazgos.append(f"{rel}:{i + 1}")
    return hallazgos


def test_no_vuelve_el_except_pass_mudo_en_modules():
    """Si agregás uno nuevo, este test te lo dice y te da la línea.

    Si de verdad tiene que quedar mudo (por ejemplo dentro del propio
    `avisar`, que no puede llamarse a sí mismo), poné `# pragma: no cover`
    o el comentario `silencio ok` en la línea del `except` y explicá por qué.
    """
    mudos = _silencios_mudos(_RAIZ / "modules")
    assert not mudos, (
        "estos `except` se tragan el error sin dejar rastro — usá "
        "`from modules._lib.silencios import avisar` y avisá:\n  "
        + "\n  ".join(mudos)
    )


@pytest.mark.parametrize("archivo", ["app.py", "db.py", "bank_helpers.py"])
def test_los_archivos_de_la_raiz_tampoco(archivo):
    ruta = _RAIZ / archivo
    if not ruta.exists():
        pytest.skip(f"{archivo} no existe")
    mudos = _silencios_mudos(ruta.parent) if ruta.is_dir() else []
    lineas = ruta.read_text(encoding="utf-8").split("\n")
    for i, ln in enumerate(lineas[:-1]):
        m = _RX_EXC.match(ln)
        p = _RX_PASS.match(lineas[i + 1])
        if m and p and p.group(1) > m.group(1) and "pragma: no cover" not in ln:
            mudos.append(f"{archivo}:{i + 1}")
    assert not mudos, f"`except: pass` mudo en {archivo}: {mudos}"
