"""Tamara 2026-09-03 — ninguna tabla por período se lee "a secas" el día 1.

El 01/09/2026 dos tablas con `periodo TEXT PRIMARY KEY` ('YYYY-MM') rompieron
la proyección de la misma manera: el día 1 la fila del mes nuevo todavía no
existe, el lector devolvía 0 y nadie avisaba.

  * `gastos_proyectado_mes` → ~815.000 de gastos fijos desaparecían.
  * `venta_proyectada_mes`  → la meta volvía al kprog viejo del ERP.

Las dos se arreglaron heredando el último período cargado
(`gastos_proyectado_mes_get`, `venta_proyectada_mes_vigente`, con sus tests en
`test_gastos_proyectados_rollover_mes.py`). Este archivo cubre el PATRÓN, no
el caso: cualquier tabla con esa llave que se lea sin herencia rompe el
próximo día 1. Lee las migraciones y el código, así que una tabla nueva o un
lector nuevo lo hacen fallar hasta que alguien decida cómo arranca de mes.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Cada tabla por período con la función que sabe arrancar el mes sin fila:
#:   - hereda el último período cargado (`_get` / `_vigente`), o
#:   - se CONGELA sola la primera vez que se lee (`gastos_mes_manual`: es la
#:     fila "Gastos mes anterior", que /informes/gastos escribe si falta).
#: Sumar una tabla acá es decidir cómo arranca de mes; no alcanza con crearla.
LECTORES_CON_HERENCIA = {
    "gastos_proyectado_mes": {"gastos_proyectado_mes_get"},
    "venta_proyectada_mes": {"venta_proyectada_mes_get", "venta_proyectada_mes_vigente"},
    "gastos_mes_manual": {"gastos_mes_manual_get"},
}

#: Lectores CRUDOS (devuelven None si no hay fila): sólo puede llamarlos el
#: lector con herencia que los envuelve.
LECTORES_CRUDOS = {
    "venta_proyectada_mes_get": {"venta_proyectada_mes_vigente"},
}


def _tablas_por_periodo() -> set[str]:
    """Las tablas de `migrations/` cuya llave primaria es `periodo TEXT`."""
    tablas: set[str] = set()
    for mig in (ROOT / "migrations").glob("*.sql"):
        src = mig.read_text(encoding="utf-8")
        for m in re.finditer(
            r"CREATE TABLE(?: IF NOT EXISTS)?\s+scintela\.(\w+)\s*\((.*?)\);",
            src, re.S | re.I,
        ):
            if re.search(r"\bperiodo\s+TEXT\s+PRIMARY KEY", m.group(2), re.I):
                tablas.add(m.group(1))
    return tablas


def _funciones_que_leen(tabla: str) -> set[tuple[str, str]]:
    """(archivo, función) de cada SELECT ... FROM scintela.<tabla> en modules/."""
    patron = re.compile(rf"\bFROM\s+scintela\.{tabla}\b", re.I)
    out: set[tuple[str, str]] = set()
    for py in (ROOT / "modules").rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        if not patron.search(src):
            continue
        tree = ast.parse(src)
        for nodo in ast.walk(tree):
            if not isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            cuerpo = ast.get_source_segment(src, nodo) or ""
            # Una SELECT dentro de un INSERT ... ON CONFLICT no es una lectura.
            if patron.search(cuerpo) and re.search(r"\bSELECT\b", cuerpo):
                out.add((py.relative_to(ROOT).as_posix(), nodo.name))
    return out


def _llamadores(funcion: str) -> set[tuple[str, str]]:
    """(archivo, función) desde donde se llama a `funcion` en modules/."""
    out: set[tuple[str, str]] = set()
    for py in (ROOT / "modules").rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        if funcion not in src:
            continue
        for nodo in ast.walk(ast.parse(src)):
            if not isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if nodo.name == funcion:
                continue
            cuerpo = ast.get_source_segment(src, nodo) or ""
            # Llamada directa `f(` o referencia pasada a otro (`_safe(f, ...)`),
            # no la mención en un docstring (`f`).
            if re.search(rf"(?<![\w`]){funcion}\s*[(,)]", cuerpo):
                out.add((py.relative_to(ROOT).as_posix(), nodo.name))
    return out


def test_las_tres_tablas_por_periodo_tienen_lector_con_herencia():
    tablas = _tablas_por_periodo()
    assert tablas == set(LECTORES_CON_HERENCIA), (
        "Hay una tabla con `periodo TEXT PRIMARY KEY` sin decidir cómo arranca "
        "de mes. El día 1 la fila del mes nuevo no existe: si se lee a secas, "
        "devuelve 0 y la pantalla que la usa arranca rota (01/09/2026). Hay que "
        "darle un lector que herede y anotarlo en LECTORES_CON_HERENCIA.\n"
        f"  en migraciones: {sorted(tablas)}\n"
        f"  con lector:     {sorted(LECTORES_CON_HERENCIA)}"
    )


def test_nadie_lee_una_tabla_por_periodo_por_fuera_de_su_lector():
    """Un SELECT nuevo sobre la tabla, en cualquier módulo, es un lector sin
    herencia hasta que se demuestre lo contrario."""
    for tabla, permitidas in LECTORES_CON_HERENCIA.items():
        lectores = _funciones_que_leen(tabla)
        assert lectores, f"nadie lee scintela.{tabla} — ¿se movió el lector?"
        de_mas = {f for f in lectores if f[1] not in permitidas}
        assert not de_mas, (
            f"scintela.{tabla} se lee por fuera de su lector con herencia: "
            f"{sorted(de_mas)}. El día 1 esa lectura devuelve nada."
        )


def test_el_lector_crudo_solo_lo_llama_el_que_hereda():
    """`venta_proyectada_mes_get` devuelve None sin fila; el balance tiene que
    pasar por `_vigente`, que hereda. Un caller nuevo del crudo es el bug del
    01/09 otra vez."""
    for crudo, permitidos in LECTORES_CRUDOS.items():
        llamadores = _llamadores(crudo)
        de_mas = {f for f in llamadores if f[1] not in permitidos}
        assert not de_mas, (
            f"{crudo} (sin herencia) se llama desde {sorted(de_mas)}; "
            f"tiene que ir por {sorted(permitidos)}."
        )


def test_el_balance_y_gastos_pasan_por_los_lectores_con_herencia():
    """Las dos pantallas que se rompieron el 01/09 leen por el camino que hereda."""
    llam_gastos = {f for _, f in _llamadores("gastos_proyectado_mes_get")}
    llam_venta = {f for _, f in _llamadores("venta_proyectada_mes_vigente")}
    assert "informe_balance" in llam_gastos or any("balance" in f for f in llam_gastos), llam_gastos
    assert any("gastos" in f for f in llam_gastos), llam_gastos
    assert any("balance" in f for f in llam_venta), llam_venta


def test_gastos_mes_anterior_se_lee_para_el_mes_cerrado_y_se_congela_si_falta():
    """`gastos_mes_manual` no hereda: es la fila FIJA del mes que cerró. Su
    arranque de mes es que /informes/gastos la escriba la primera vez que
    entra alguien — si falta el `set` después del `get`, el día 1 queda
    recalculándose para siempre."""
    src = (ROOT / "modules" / "informes" / "views.py").read_text(encoding="utf-8")
    i = src.index("queries.gastos_mes_manual_get(periodo_anterior)")
    bloque = src[i:i + 900]
    assert "gastos_mes_manual_set(" in bloque
    assert 'usuario="auto-cierre-mes"' in bloque
    # Nunca se lee para el mes EN CURSO: ese mes todavía no cerró.
    for arch, fn in _llamadores("gastos_mes_manual_get"):
        cuerpo = (ROOT / arch).read_text(encoding="utf-8")
        assert "gastos_mes_manual_get(_periodo_actual_ec" not in cuerpo, (arch, fn)
