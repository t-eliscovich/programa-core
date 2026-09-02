"""Tamara 2026-09-02 — septiembre 2026 apareció SIN precio objetivo (`pre`).

Dos funciones distintas crean la fila de `scintela.iniciales` del mes nuevo:

  * `informes.queries.rollover_y_writeback_iniciales` (cron del día 1 + foto diaria)
  * `iniciales.queries.cerrar_mes_auto`               (auto-cierre desde el balance)

Las dos son idempotentes ("si ya hay fila, no hago nada"), así que **gana la que
corra primero** — y cada una copiaba un juego de columnas distinto.
`cerrar_mes_auto` se olvidaba de `pre`, `dificil`, `pretej`, `pretin` y `preadm`.

Se vio en /informes/iniciales: septiembre con la columna Precio en blanco
mientras julio y agosto tenían 8,55. Ese `pre` es el precio de respaldo con el
que el balance proyecta cuando el mes todavía no tiene ventas — en 0, la fila
Proyección vuelve a salir 0 el día 1 del mes.

Este test compara los dos juegos de columnas leyendo el CÓDIGO. No alcanza con
arreglar una: lo que hay que sostener es que las dos escriban lo mismo.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Columnas de identidad/auditoría: cada creador las arma a su manera.
_IGNORAR = {"mesnum", "mesnom", "yy", "usuario_crea", "id_iniciales"}


def _cols_rollover() -> set[str]:
    """Claves del dict `_row_new` de rollover_y_writeback_iniciales."""
    src = (ROOT / "modules" / "informes" / "queries.py").read_text(encoding="utf-8")
    src = src[src.index("def rollover_y_writeback_iniciales"):]
    i = src.index("_row_new = {")
    bloque = src[i:src.index("}", i)]
    return set(re.findall(r'"([a-z_]+)":', bloque)) - _IGNORAR


def _cols_cerrar_mes_auto() -> set[str]:
    """Columnas del INSERT INTO scintela.iniciales de cerrar_mes_auto."""
    src = (ROOT / "modules" / "iniciales" / "queries.py").read_text(encoding="utf-8")
    # Anclado DENTRO de cerrar_mes_auto: el archivo tiene otros INSERT a la
    # misma tabla (el alta manual de /iniciales), y agarrar el primero medía
    # la función equivocada.
    src = src[src.index("def cerrar_mes_auto"):]
    i = src.index("INSERT INTO scintela.iniciales")
    bloque = src[i:src.index("VALUES", i)]
    bloque = bloque[bloque.index("("):]
    return set(re.findall(r"[a-z_]+", bloque)) - _IGNORAR


def test_los_dos_creadores_copian_las_mismas_columnas():
    roll, auto = _cols_rollover(), _cols_cerrar_mes_auto()
    assert roll == auto, (
        "Los dos creadores de la fila de iniciales del mes nuevo dejaron de "
        "coincidir. Gana el que corra primero, así que el mes nuevo arranca con "
        "columnas distintas según quién lo creó.\n"
        f"  sólo en rollover_y_writeback_iniciales: {sorted(roll - auto)}\n"
        f"  sólo en cerrar_mes_auto:                {sorted(auto - roll)}"
    )


def test_el_precio_objetivo_viaja_en_los_dos():
    """`pre` es el que más duele: sin él la Proyección sale 0 el día 1."""
    assert "pre" in _cols_rollover()
    assert "pre" in _cols_cerrar_mes_auto()


def test_el_presupuesto_por_area_viaja_en_los_dos():
    for col in ("pretej", "pretin", "preadm", "pretot"):
        assert col in _cols_rollover(), col
        assert col in _cols_cerrar_mes_auto(), col
