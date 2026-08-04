"""Buscar por PALABRAS sueltas — el reporte de la dueña del 2026-08-04.

Screenshot + mensaje: *"esto no funciona si solo busco condor, podes hacerlo
mejor"*, con la URL `?q=IRENE%25CONDOR` — o sea que para encontrar a
"IRENE CHICAIZA CONDOR" ella tenía que tipear el comodín de SQL a mano,
porque el buscador armaba un solo patrón `%irene condor%` y el apellido del
medio lo rompía.

Estos tests son de dos clases:
  1. unitarios sobre `modules/_lib/busqueda` (normalización, tokens, SQL);
  2. de SEMÁNTICA: simulan el LIKE de Postgres sobre el SQL generado, así el
     test falla si el patrón deja de encontrar a "IRENE CHICAIZA CONDOR"
     aunque el código "parezca" bien.
"""
from __future__ import annotations

import re

import pytest

from modules._lib import busqueda

# ── Simulador del LIKE de Postgres ─────────────────────────────────────────
# El TRANSLATE/UPPER y el patrón que arma `condicion()` se ejecutan en la DB,
# que en los tests no está. Reimplementamos las dos operaciones (son 4 líneas)
# y evaluamos la condición generada — así testeamos el COMPORTAMIENTO, no el
# string de SQL.


def _like(valor: str, patron: str) -> bool:
    rx = re.escape(patron).replace("%", ".*").replace("_", ".")
    # re.escape escapa % y _ en algunas versiones; los volvemos comodines.
    rx = rx.replace(r"\%", ".*").replace(r"\_", ".")
    return re.fullmatch(rx, valor, flags=re.DOTALL) is not None


def _evaluar(sql: str, params: dict, fila: dict) -> bool:
    """Evalúa la condición generada por `condicion()` sobre una fila dict."""
    if not sql:
        return True
    partes = [p.strip() for p in sql.split(" AND ")]
    for parte in partes:
        parte = parte.strip("()")
        ok = False
        for termino in parte.split(" OR "):
            m = re.match(
                r"TRANSLATE\(UPPER\(COALESCE\((?P<col>[^,]+), ''\)\), "
                r"'[^']*', '[^']*'\) LIKE %\((?P<key>\w+)\)s",
                termino.strip(),
            )
            assert m, f"no supe leer el término generado: {termino!r}"
            col = m.group("col").split(".")[-1]
            valor = busqueda.normalizar(fila.get(col) or "")
            if _like(valor, busqueda.normalizar(params[m.group("key")])):
                ok = True
                break
        if not ok:
            return False
    return True


COLUMNAS = ("c.codigo_cli", "c.nombre", "c.ruc")
IRENE = {"codigo_cli": "ICH", "nombre": "IRENE CHICAIZA CONDOR", "ruc": "170123456"}
WILSON = {"codigo_cli": "CON", "nombre": "WILSON CONDOR", "ruc": "180999888"}
PENA = {"codigo_cli": "PEN", "nombre": "JOSÉ PEÑA DÁVILA", "ruc": "190000111"}


# ── 1. Normalización ───────────────────────────────────────────────────────

def test_los_dos_strings_de_translate_miden_igual():
    """Si el 2° es más corto, Postgres BORRA los sobrantes ('PEÑA' → 'PEA')."""
    assert len(busqueda._ACENTOS) == len(busqueda._PLANOS)


def test_normalizar_pliega_acentos_y_enie():
    assert busqueda.normalizar(" josé peña dávila ") == "JOSE PENA DAVILA"
    assert busqueda.normalizar(None) == ""


def test_palabras_parte_y_limita():
    assert busqueda.palabras("  irene   condor ") == ["IRENE", "CONDOR"]
    assert busqueda.palabras("") == []
    assert len(busqueda.palabras(" ".join("abcdefghij"))) == busqueda.MAX_PALABRAS


# ── 2. El bug reportado ────────────────────────────────────────────────────

@pytest.mark.parametrize("q", ["condor", "irene condor", "condor irene",
                               "IRENE CONDOR", "chicaiza condor", "ich",
                               "irene%condor"])
def test_irene_chicaiza_condor_aparece_como_sea_que_la_busques(q):
    """EL test del reporte: apellido en el medio, orden libre, mayúsculas."""
    sql, params = busqueda.condicion(q, COLUMNAS)
    assert _evaluar(sql, params, IRENE), f"'{q}' no encontró a IRENE CHICAIZA CONDOR"


def test_el_comodin_que_la_duena_tipeaba_a_mano_sigue_andando():
    """Su búsqueda vieja `IRENE%CONDOR` no se puede romper."""
    sql, params = busqueda.condicion("IRENE%CONDOR", COLUMNAS)
    assert _evaluar(sql, params, IRENE)


def test_todas_las_palabras_tienen_que_estar():
    """"irene condor" NO puede traer a WILSON CONDOR (sería peor que antes)."""
    sql, params = busqueda.condicion("irene condor", COLUMNAS)
    assert not _evaluar(sql, params, WILSON)


def test_acentos_y_enie_matchean_en_los_dos_sentidos():
    for q in ("peña", "pena", "davila", "dávila", "peña davila"):
        sql, params = busqueda.condicion(q, COLUMNAS)
        assert _evaluar(sql, params, PENA), q


def test_busca_tambien_por_ruc_y_por_codigo():
    for q, fila in (("170123456", IRENE), ("ICH", IRENE), ("con", WILSON)):
        sql, params = busqueda.condicion(q, COLUMNAS)
        assert _evaluar(sql, params, fila), q


def test_sin_termino_no_filtra():
    sql, params = busqueda.condicion("   ", COLUMNAS)
    assert (sql, params) == ("", {})


def test_una_palabra_por_parametro_y_reusado_en_cada_columna():
    sql, params = busqueda.condicion("irene condor", COLUMNAS)
    assert len(params) == 2, "un parámetro por PALABRA (reusado en cada columna)"
    assert sql.count(" AND ") == 1 and sql.count(" OR ") == 2 * 2


def test_el_backslash_se_escapa():
    """`\\` es el escape de LIKE en Postgres: sin escapar, revienta la query."""
    _, params = busqueda.condicion("a\\", COLUMNAS)
    assert list(params.values()) == ["%A\\\\%"]


# ── 3. Ranking ─────────────────────────────────────────────────────────────

def test_ranking_pone_el_codigo_exacto_primero():
    """Queja dueña 2026-07-06: buscar "edu" listaba EDUARDOs y el cliente EDU
    no aparecía ni en la primera página."""
    sql, params = busqueda.ranking("edu", "c.codigo_cli")
    assert params["bqr_eq"] == "EDU"
    assert params["bqr_pref"] == "EDU%"
    # el primer WHEN mira el CÓDIGO, no el nombre
    primero = sql.split("WHEN")[1]
    assert "codigo_cli" in primero and "THEN 0" in primero


def test_ranking_sin_termino_no_devuelve_ninguna_constante():
    """Sin término devuelve VACÍO — el llamador ARMA el ORDER BY.

    Postgres rechaza los términos constantes: `ORDER BY 0` es "position 0 is
    not in select list" (entero suelto = referencia POSICIONAL) y
    `ORDER BY NULL` es "non-integer constant in ORDER BY". Las dos se
    deployaron el 2026-08-04 y las dos rompieron una pantalla.
    """
    sql, params = busqueda.ranking("", "c.codigo_cli")
    assert (sql, params) == ("", {})


# ── 4. "¿Quisiste decir?" ──────────────────────────────────────────────────

def test_parecidos_aguanta_el_typo():
    padron = [IRENE, WILSON, PENA]
    assert busqueda.parecidos("chicaisa", padron)[0]["codigo_cli"] == "ICH"
    assert busqueda.parecidos("condorr", padron)


def test_parecidos_no_devuelve_cualquier_cosa():
    assert busqueda.parecidos("zzzzzzzz", [IRENE, WILSON, PENA]) == []
    assert busqueda.parecidos("", [IRENE]) == []


def test_parecidos_ignora_filas_sin_texto():
    assert busqueda.parecidos("irene", [{"nombre": "", "codigo_cli": ""}]) == []
