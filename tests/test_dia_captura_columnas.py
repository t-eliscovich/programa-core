"""Las columnas que `dia.capturar()` inserta tienen que existir en la tabla.

🚨 `capturar()` arma el INSERT DINÁMICAMENTE, con las claves del dict `fila`:

    cols = list(fila.keys())
    INSERT INTO scintela.dia_captura ({campos}) VALUES ({marcas})

y las claves salen de `foto.COMPONENTES` cruzado con `foto._CLAVE_BALANCE`. O
sea: agregar un componente al balance y olvidarse de la migración hace que el
INSERT falle. Y `capturar()` NO LEVANTA NUNCA (cuelga del hilo de fondo):
devuelve ok=False, loguea, y la captura del día deja de existir en silencio.

Eso ya pasó el 02-03/09/2026 por el lado de arriba —`patant` entró a
COMPONENTES sin columna y el loop reventó con KeyError, la mañana del 03/09 no
hubo captura y nadie se enteró—. Este test es el freno de raíz: compara las
columnas que el INSERT usaría contra las que crean las migraciones.
"""
import re
from pathlib import Path

from modules.informes import dia as _dia
from modules.informes import traza as _traza
from modules.informes.foto import _CLAVE_BALANCE, COMPONENTES, ETIQUETAS

RAIZ = Path(__file__).resolve().parent.parent
MIGRACIONES = RAIZ / "migrations"


def _columnas_de_la_tabla(tabla: str = "dia_captura") -> set[str]:
    """Las columnas de scintela.<tabla> según las migraciones."""
    cols: set[str] = set()
    for sql in sorted(MIGRACIONES.glob("*.sql")):
        txt = sql.read_text(encoding="utf-8")
        # CREATE TABLE ... <tabla> ( ... );
        for cuerpo in re.findall(
                rf"CREATE TABLE[^;]*?{tabla}\s*\((.*?)\n\);",
                txt, re.S | re.I):
            for linea in cuerpo.splitlines():
                linea = linea.strip()
                if not linea or linea.startswith("--"):
                    continue
                m = re.match(r"([a-z_][a-z0-9_]*)\s", linea)
                if m:
                    cols.add(m.group(1))
        # ALTER TABLE ... dia_captura ADD COLUMN [IF NOT EXISTS] x TIPO
        for cuerpo in re.findall(
                rf"ALTER TABLE\s+scintela\.{tabla}(.*?);", txt, re.S | re.I):
            for m in re.finditer(
                    r"ADD COLUMN\s+(?:IF NOT EXISTS\s+)?([a-z_][a-z0-9_]*)",
                    cuerpo, re.I):
                cols.add(m.group(1))
    return cols


def _columnas_del_insert() -> list[str]:
    """Las claves que `capturar()` le pone al dict `fila`, en su orden."""
    cols = ["fecha_ec", "momento", "id_traza", "utilidad", "patr_neto"]
    cols += [c for c, _s in COMPONENTES if c in _CLAVE_BALANCE]
    for et in ("hilado", "tejido", "terminado"):
        cols += [f"{et}_kg", f"{et}_ukg"]
    return cols


def test_la_tabla_se_parsea():
    """Si el parser no encuentra la tabla, el test de abajo pasa por vacío."""
    tabla = _columnas_de_la_tabla()
    assert "id_captura" in tabla and "fecha_ec" in tabla and "momento" in tabla
    assert "id_traza" in tabla, "mig 0171"
    assert "hilado_ukg" in tabla, "mig 0162"
    assert len(tabla) >= 24


def test_todas_las_columnas_del_insert_existen():
    """El freno. Si esto falla, falta una migración — y sin ella la captura
    del día se apaga EN SILENCIO."""
    tabla = _columnas_de_la_tabla()
    faltan = [c for c in _columnas_del_insert() if c not in tabla]
    assert not faltan, (
        f"capturar() insertaría columnas que no existen en "
        f"scintela.dia_captura: {faltan}. Falta la migración. El INSERT "
        f"fallaría y capturar() lo devolvería como ok=False sin levantar: la "
        f"captura del día dejaría de existir sin que nadie se entere."
    )


def test_los_componentes_derivados_no_entran_al_insert():
    """`patant` es derivado (patr_neto + uret − utilidad): no tiene columna y
    no debe entrar. El filtro es POR EL MAPA, no por nombre, así que el
    próximo derivado queda cubierto sin tocar nada."""
    derivados = [c for c, _s in COMPONENTES if c not in _CLAVE_BALANCE]
    assert "patant" in derivados
    assert not set(derivados) & set(_columnas_del_insert())


def test_el_insert_de_capturar_usa_estas_columnas():
    """Ancla el parser de arriba al código real: si `capturar()` cambia cómo
    arma `fila`, este test avisa en vez de mentir en silencio."""
    import inspect
    src = inspect.getsource(_dia.capturar)
    assert 'cols = list(fila.keys())' in src
    assert "INSERT INTO scintela.dia_captura ({campos})" in src
    assert "if c in _CLAVE_BALANCE:" in src, "el filtro por mapa"
    for et in ("hilado", "tejido", "terminado"):
        assert et in src


# ── El mismo riesgo, del lado de la GRABADORA ──────────────────────────────
# `traza.registrar()` arma su INSERT igual de dinámicamente, con las claves de
# `_fila_desde_balance`. Y la captura del día CUELGA de esa foto: si la traza
# no puede grabar, `capturar()` devuelve ok=False y no hay ancla. O sea que un
# agujero acá apaga las dos cosas.


def _columnas_del_insert_traza() -> list[str]:
    """Las claves que `_fila_desde_balance` devuelve, más las dos que
    `registrar()` agrega a mano."""
    src = (RAIZ / "modules" / "informes" / "traza.py").read_text(encoding="utf-8")
    cuerpo = src.split("def _fila_desde_balance")[1].split("\ndef ")[0]
    return re.findall(r'^\s+"([a-z_][a-z0-9_]*)":', cuerpo, re.M) + ["origen", "momento"]


def test_las_columnas_de_la_traza_existen():
    tabla = _columnas_de_la_tabla("traza_utilidad")
    assert "id_traza" in tabla and "utilidad" in tabla
    assert "terminado_ukg" in tabla, "mig 0186"
    faltan = [c for c in _columnas_del_insert_traza() if c not in tabla]
    assert not faltan, (
        f"traza.registrar() insertaría columnas que no existen en "
        f"scintela.traza_utilidad: {faltan}. Sin foto no hay ancla del día: "
        f"capturar() cuelga de registrar()."
    )


# ── Los tres mapas que tienen que moverse juntos ───────────────────────────


def test_foto_y_traza_declaran_los_mismos_componentes():
    """El docstring de `foto.COMPONENTES` lo pide explícito: *"mismos y mismos
    signos que `traza.COMPONENTES` — si uno cambia, cambian los dos"*. Si se
    separan, la foto y el diff dejan de hablar del mismo balance."""
    assert tuple(COMPONENTES) == tuple(_traza.COMPONENTES)


def test_todo_componente_tiene_etiqueta():
    """`dia.explicar` hace `ETIQUETAS[c]` SIN `.get` (dia.py, armado de
    `out["componentes"]`). Un componente sin etiqueta revienta la pantalla del
    día con KeyError apenas ese componente supere el umbral — y sólo entonces,
    o sea el día menos oportuno."""
    faltan = [c for c, _s in COMPONENTES if c not in ETIQUETAS]
    assert not faltan, f"componentes sin ETIQUETAS: {faltan}"
