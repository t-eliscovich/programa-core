"""Ningún signo de porcentaje suelto en un SQL que se manda con parámetros.

TMT 2026-08-03 — INCIDENTE. El estado de cuenta de **todos** los clientes tiró
404 en producción durante unos minutos. La causa no estaba en el SQL: estaba
en un COMENTARIO dentro del SQL, que decía justamente "no pongas el signo de
porcentaje acá porque psycopg2 lo toma como placeholder"… y lo tenía escrito.

psycopg2 interpola la cadena ENTERA cuando la query lleva parámetros
(`cur.execute(sql, args)`), sin saber qué es comentario y qué no. Un `%`
seguido de cualquier cosa que no sea `s`, `(` u otro `%` levanta
`ValueError: unsupported format character`. La suite no lo cazó porque los
tests de esta zona inspeccionan el source y **no hay Postgres en CI**: el SQL
nunca se ejecuta. Este test es el sustituto — es puro texto, no necesita base.

Regla: dentro de un literal SQL con `%s`, el signo va **duplicado** (`%%`),
tanto en un `LIKE` como en un comentario. O no va.
"""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

# Todo el código que arma SQL. Se camina el repo entero a propósito: el bug
# puede aparecer en cualquier módulo, no sólo en el que lo estrenó.
# `scripts/_archive` queda afuera: son one-offs ya corridos, con SQL de
# PL/pgSQL (`format('… %s …')`) que no pasa por psycopg2 con parámetros.
ARCHIVOS = sorted(
    p for p in RAIZ.rglob("*.py")
    if not any(
        x in p.parts
        for x in ("tests", ".git", "node_modules", "migrations", "_archive")
    )
)


def _sospechosos(src: str) -> list[tuple[int, str]]:
    """Devuelve (línea, contexto) por cada `%` suelto en un SQL parametrizado."""
    hallazgos: list[tuple[int, str]] = []
    for m in re.finditer(r'"""(.*?)"""', src, re.S):
        cuerpo = m.group(1)
        # Sin placeholders no hay interpolación: el `%` es inofensivo.
        if "%s" not in cuerpo and "%(" not in cuerpo:
            continue
        for mm in re.finditer(r"%", cuerpo):
            i = mm.start()
            siguiente = cuerpo[i + 1:i + 2]
            anterior = cuerpo[i - 1:i] if i else ""
            if siguiente in ("s", "%", "(") or anterior == "%":
                continue
            linea = src[:m.start()].count("\n") + cuerpo[:i].count("\n") + 1
            hallazgos.append((linea, cuerpo[max(0, i - 70):i + 20].replace("\n", " ")))
    return hallazgos


def test_ningun_porcentaje_suelto_en_sql_parametrizado():
    errores = []
    for archivo in ARCHIVOS:
        try:
            src = archivo.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for linea, ctx in _sospechosos(src):
            errores.append(f"{archivo.relative_to(RAIZ)}:{linea} … {ctx}")
    assert not errores, (
        "signo de porcentaje sin duplicar dentro de un SQL que lleva "
        "parámetros — psycopg2 va a tirar 'unsupported format character' en "
        "PRODUCCIÓN (en CI no, porque no hay Postgres):\n  "
        + "\n  ".join(errores)
    )


def test_el_detector_encuentra_el_caso_que_rompio_produccion():
    """Sin esto, el test de arriba podría estar pasando por no mirar nada."""
    veneno = '''
    db.fetch_one(
        """
        SELECT a
          -- Sin `%` en la clase de caracteres: psycopg2 lo toma como placeholder
          FROM t WHERE b = %s
        """,
        (x,),
    )
    '''
    assert _sospechosos(veneno), "el detector no ve el bug real del 03/08"

    # Y no se queja de lo que SÍ está bien escrito.
    sano = '''
        """
        SELECT a FROM t WHERE nombre LIKE %%algo%% AND b = %s
        """
    '''
    assert not _sospechosos(sano)
