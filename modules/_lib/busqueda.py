"""Búsqueda por palabras — tolerante al ORDEN, a los ACENTOS y a la ñ.

TMT 2026-08-04 (dueña, sobre el buscador del estado de cuenta):
*"esto no funciona si solo busco condor, podes hacerlo mejor"*.

Todos los buscadores por nombre armaban UN solo patrón `%q%` y lo tiraban
contra `nombre` con ILIKE. Eso hace que:

  · **"irene condor" no encuentre a "IRENE CHICAIZA CONDOR"** — hay un
    apellido en el medio, así que el patrón `%irene condor%` no pega. La
    dueña había aprendido a tipear el comodín A MANO: en el screenshot la
    URL dice `?q=IRENE%25CONDOR`. Ese es el síntoma: si el usuario tiene que
    escribir SQL, el buscador está roto.
  · "peña" no encuentre a "PENA" ni al revés, ni "davila" a "DÁVILA": el
    dBase cargó los nombres con y sin tilde según el día.

Acá vive la lógica compartida (estado de cuenta, /clientes, /proveedores):
partir la consulta en palabras, plegar acentos de los dos lados y exigir que
TODAS las palabras aparezcan, en cualquier orden y en cualquiera de las
columnas buscables.

El `%` que la dueña tipeaba a mano SIGUE andando (viaja adentro del patrón
LIKE), así que sus búsquedas viejas no se rompen.
"""
from __future__ import annotations

import unicodedata

# Los dos strings de TRANSLATE tienen que medir EXACTAMENTE igual: si el
# segundo es más corto, Postgres BORRA los caracteres sobrantes del primero
# (y "PEÑA" pasaría a ser "PEA"). Lo cubre test_busqueda_por_palabras.py.
#
# Van las minúsculas acentuadas TAMBIÉN (mapeadas a la mayúscula plana): el
# `UPPER()` de Postgres sólo pasa 'ñ'→'Ñ' si la base tiene un locale que lo
# contemple, y con collation C se queda en 'ñ'. Plegando los dos casos acá,
# el match no depende del locale del servidor.
_ACENTOS = "ÁÀÄÂÃÅÉÈËÊÍÌÏÎÓÒÖÔÕÚÙÜÛÑÇáàäâãåéèëêíìïîóòöôõúùüûñç"
_PLANOS = "AAAAAAEEEEIIIIOOOOOUUUUNCAAAAAAEEEEIIIIOOOOOUUUUNC"

#: Más de esto no aporta y sólo alarga la query (nadie tipea 7 apellidos).
MAX_PALABRAS = 6


def normalizar(texto: object) -> str:
    """``' Peña  Dávila '`` → ``'PEÑA  DÁVILA'`` sin tildes → ``'PENA  DAVILA'``.

    Tiene que dar lo MISMO que :func:`sql_normalizado` en Postgres — si las
    dos normalizaciones divergen, el patrón no pega con la columna.
    """
    s = unicodedata.normalize("NFD", str(texto or ""))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s.upper().strip()


def sql_normalizado(columna: str) -> str:
    """Expresión SQL que normaliza una columna igual que :func:`normalizar`."""
    return (
        f"TRANSLATE(UPPER(COALESCE({columna}, '')), '{_ACENTOS}', '{_PLANOS}')"
    )


def _escapar(token: str) -> str:
    """Escapa el backslash (escape de LIKE en Postgres).

    `%` y `_` NO se escapan A PROPÓSITO: son el comodín que la dueña ya usa
    a mano ("IRENE%CONDOR") y que tiene que seguir funcionando.
    """
    return token.replace("\\", "\\\\")


def palabras(q: object, maximo: int = MAX_PALABRAS) -> list[str]:
    """``'irene condor'`` → ``['IRENE', 'CONDOR']`` (normalizadas, sin vacías)."""
    return [p for p in normalizar(q).split() if p][:maximo]


def condicion(
    q: object,
    columnas: tuple[str, ...] | list[str],
    prefijo: str = "bq",
    maximo: int = MAX_PALABRAS,
) -> tuple[str, dict]:
    """Devuelve ``(sql, params)`` que exige TODAS las palabras de ``q``.

    Cada palabra puede caer en CUALQUIERA de las ``columnas`` (OR) y todas
    las palabras tienen que estar (AND) → "irene condor" y "condor irene"
    encuentran lo mismo, y "condor" solo también.

    Con ``q`` vacío devuelve ``("", {})``: el llamador decide si eso
    significa "sin filtro" o "sin resultados".
    """
    toks = palabras(q, maximo)
    if not toks:
        return "", {}
    exprs = [sql_normalizado(c) for c in columnas]
    partes: list[str] = []
    params: dict[str, str] = {}
    for i, t in enumerate(toks):
        clave = f"{prefijo}{i}"
        params[clave] = f"%{_escapar(t)}%"
        partes.append(
            "(" + " OR ".join(f"{e} LIKE %({clave})s" for e in exprs) + ")"
        )
    return " AND ".join(partes), params


def ranking(
    q: object,
    columna_codigo: str,
    prefijo: str = "bqr",
) -> tuple[str, dict]:
    """``(CASE …, params)`` para ordenar los matches por qué tan buenos son.

    0 = el CÓDIGO es exactamente lo tipeado, 1 = el código EMPIEZA así,
    2 = el resto (matchea por nombre / RUC / etc.).

    Queja dueña 2026-07-06: buscar "edu" listaba 40 EDUARDOs por nombre y el
    cliente con CÓDIGO "EDU" (su 3er mayor deudor) no entraba ni en la
    primera página. DENTRO de cada grupo el llamador ordena por SALDO DESC —
    eso lo pidió ella y no se toca (tests/test_clientes_buscar_ranking.py).
    """
    toks = palabras(q)
    if not toks:
        # NULL, no 0: un entero suelto en ORDER BY es una referencia
        # POSICIONAL a la columna N (y `ORDER BY 0` directamente explota).
        return "NULL", {}
    qn = " ".join(toks)
    cod = sql_normalizado(columna_codigo)
    params = {
        f"{prefijo}_eq": qn,
        f"{prefijo}_pref": _escapar(qn) + "%",
    }
    sql = f"""CASE
            WHEN {cod} = %({prefijo}_eq)s             THEN 0
            WHEN {cod} LIKE %({prefijo}_pref)s        THEN 1
            ELSE 2
          END"""
    return sql, params


def parecidos(
    q: object,
    filas: list[dict],
    campos: tuple[str, ...] = ("nombre", "codigo_cli"),
    corte: float = 0.75,
    limite: int = 25,
) -> list[dict]:
    """"¿Quisiste decir…?" — matches aproximados cuando NO hubo ninguno exacto.

    Compara palabra contra palabra con difflib, así "chicaisa"/"chicaiza" o
    "guaranga"/"guaranda" siguen encontrando al cliente. Es un FALLBACK: sólo
    se llama cuando la búsqueda normal volvió vacía, porque es O(n) en Python
    sobre la lista de clientes (~pocos miles, se banca).
    """
    import difflib

    toks = palabras(q)
    if not toks:
        return []
    puntuadas: list[tuple[float, dict]] = []
    for fila in filas:
        vocabulario: list[str] = []
        for campo in campos:
            vocabulario.extend(normalizar(fila.get(campo)).split())
        if not vocabulario:
            continue
        total = 0.0
        for t in toks:
            mejor = max(
                (difflib.SequenceMatcher(None, t, w).ratio() for w in vocabulario),
                default=0.0,
            )
            if mejor < corte:
                total = 0.0
                break
            total += mejor
        if total:
            puntuadas.append((total / len(toks), fila))
    puntuadas.sort(key=lambda par: par[0], reverse=True)
    return [fila for _, fila in puntuadas[:limite]]
