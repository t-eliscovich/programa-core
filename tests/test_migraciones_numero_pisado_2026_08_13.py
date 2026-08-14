"""Un número de migración pisado tiene que cortar el deploy, no saltearse.

TMT 2026-08-13. El migrador lleva la cuenta en `seguridad.migraciones_aplicadas`
por NÚMERO. Si dos migraciones eligen el mismo, la segunda queda dada por
aplicada sin haber corrido jamás, y no avisa. Encontré dos así, las dos vivas
en producción desde junio:

  · `0062_conciliacion_no_cierre` — el número lo tenía
    `0062_fix_yy_baseline_zona_ec`. El índice único de las sesiones de
    conciliación se quedó en `(no_banco, usuario)`: dos usuarios podían tener
    cada uno su conciliación abierta del mismo banco.
  · `0064_histos_codigo` — lo tenía `0064_reset_importes_yy_snapshot_original`.
    Sin la columna `codigo`, "Restaurar borradas" de la papelera reventaba.

Las dos se renumeraron (0189 y 0190). Esto es el candado para que no vuelva.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def test_no_hay_dos_archivos_con_el_mismo_numero():
    """Lo que sí se puede chequear sin base: el repo consigo mismo."""
    vistos: dict[str, str] = {}
    repetidos = []
    for p in sorted((_REPO_ROOT / "migrations").iterdir()):
        m = re.match(r"^(\d{4})_.+\.(sql|py)$", p.name)
        if not m:
            continue
        num = m.group(1)
        if num in vistos:
            repetidos.append((num, vistos[num], p.name))
        vistos[num] = p.name
    assert not repetidos, f"números repetidos en migrations/: {repetidos}"


def test_detecta_el_numero_pisado_y_no_lo_saltea():
    from scripts import migrate

    aplicadas = {
        "0062": "0062_fix_yy_baseline_zona_ec",
        "0064": "0064_reset_importes_yy_snapshot_original",
        "0100": "0100_backfill_dep_pich_20260611",
    }
    archivos = [
        ("0062", "0062_conciliacion_no_cierre", Path("x")),
        ("0064", "0064_histos_codigo", Path("x")),
        ("0100", "0100_backfill_dep_pich_20260611", Path("x")),
        ("0189", "0189_conciliacion_no_cierre", Path("x")),
    ]
    choques = migrate.colisiones_de_numero(aplicadas, archivos)

    nums = sorted(v for v, _a, _r in choques)
    assert nums == ["0062", "0064"], (
        "tiene que marcar los dos números pisados y sólo esos: el 0100 coincide "
        "en nombre y el 0189 todavía no está aplicado"
    )


def test_el_que_todavia_no_corrio_no_es_una_colision():
    from scripts import migrate

    assert migrate.colisiones_de_numero({}, [("0191", "0191_lo_que_sea", Path("x"))]) == []
