"""Consultas de la lista de precios (réplica de PRECIOS.DBF)."""
import db

# Columnas de tela en el orden del dBase (PRECIOS.DBF). Cada tupla es
# (columna en la tabla scintela.precios, etiqueta que ve el usuario).
TELAS: list[tuple[str, str]] = [
    ("jersey", "JERSEY"),
    ("pique", "PIQUE"),
    ("toper", "TOPER"),
    ("alemania", "ALEMANIA"),
    ("rib", "RIB"),
    ("cuellos", "CUELLOS"),
    ("lycra", "LYCRA"),
    # TMT 2026-08-04 (dueña: "no es Falso es fleece"): `falso` es el NOMBRE DE
    # COLUMNA que viene de PRECIOS.DBF, no la tela. La tela es FLEECE — así ya
    # estaba etiquetada en Proformas (modules/proformas/queries.py). La columna
    # sigue llamándose `falso` en la tabla; sólo cambia lo que ve el usuario.
    ("falso", "FLEECE"),
    ("kiana", "KIANA"),
    ("medical", "MEDICAL"),
    ("micro", "MICRO"),
    ("james", "JAMES"),
]

# Nombres de columna válidos para el update inline (whitelist — nunca
# interpolar el nombre de columna que llega del request sin validar).
COLUMNAS_TELA: set[str] = {col for col, _ in TELAS}


def matriz() -> list[dict]:
    """Las clases de color con sus 12 precios, ordenadas por clase."""
    cols = ", ".join(col for col, _ in TELAS)
    return db.fetch_all(
        f"""
        SELECT clase, descripcio, {cols}
          FROM scintela.precios
         ORDER BY clase ASC
        """
    ) or []


def actualizar_precio(clase: int, columna: str, valor, usuario: str) -> None:
    """Actualiza una celda (clase, tela). `columna` DEBE estar en COLUMNAS_TELA.

    `valor` puede ser None (borra el precio de esa celda) o un número.
    """
    if columna not in COLUMNAS_TELA:
        raise ValueError(f"columna inválida: {columna!r}")
    db.execute(
        f"""
        UPDATE scintela.precios
           SET {columna} = %(valor)s,
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
         WHERE clase = %(clase)s
        """,
        {"valor": valor, "usuario": usuario, "clase": clase},
    )


def subir_porcentaje(pct: float, usuario: str) -> None:
    """Sube TODOS los precios de la matriz un `pct` % (× (1 + pct/100)).

    Es la forma normal de actualizar la lista: se aplica a las 12 columnas de
    tela en todas las clases de color, redondeando a 2 decimales. Las celdas
    vacías (NULL) quedan como están.
    """
    factor = 1.0 + (float(pct) / 100.0)
    sets = ", ".join(
        f"{col} = ROUND({col} * %(factor)s::numeric, 2)" for col, _ in TELAS
    )
    db.execute(
        f"""
        UPDATE scintela.precios
           SET {sets},
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
        """,
        {"factor": factor, "usuario": usuario},
    )
    # Las telas de precio único suben igual — si no, un aumento general las
    # deja atrás en silencio (las de `ref_col` suben solas: miran la matriz).
    bootstrap_precio_plano()
    db.execute(
        """
        UPDATE scintela.precio_plano
           SET precio = ROUND(precio * %(factor)s::numeric, 4),
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
         WHERE precio IS NOT NULL
        """,
        {"factor": factor, "usuario": usuario},
    )


def sumar_monto(monto: float, usuario: str) -> None:
    """Suma un `monto` fijo (USD) a TODOS los precios de la matriz.

    Alternativa al aumento porcentual: agrega el mismo importe (p.ej. 0,10 =
    diez centavos) a las 12 columnas de tela en todas las clases, redondeando a
    2 decimales. Las celdas vacías (NULL) quedan como están (NULL + n = NULL).
    """
    sets = ", ".join(
        f"{col} = ROUND({col} + %(monto)s::numeric, 2)" for col, _ in TELAS
    )
    db.execute(
        f"""
        UPDATE scintela.precios
           SET {sets},
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
        """,
        {"monto": monto, "usuario": usuario},
    )
    bootstrap_precio_plano()
    db.execute(
        """
        UPDATE scintela.precio_plano
           SET precio = ROUND(precio + %(monto)s::numeric, 4),
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
         WHERE precio IS NOT NULL
        """,
        {"monto": monto, "usuario": usuario},
    )


# Los CUATRO tramos de precio que usa la duena: basico (precio de lista, sin
# descuento) y luego descuentos EN CASCADA (sucesivos): 5%, 5%+9% y 5%+14%.
# "5%+9%" = un 5% de descuento y luego un 9% adicional sobre el ya rebajado,
# es decir lista * 0.95 * 0.91. Cada tramo es (etiqueta, factor sobre la lista).
TRAMOS_DESCUENTO: list[tuple[str, float]] = [
    ("Basico", 1.0),
    ("5%", 0.95),
    ("5%+4%", 0.95 * 0.96),
    ("5%+9%", 0.95 * 0.91),
    ("5%+14%", 0.95 * 0.86),
]


# IVA vigente en Ecuador (15%). La lista de PRECIOS.DBF está SIN IVA; la hoja
# que se le entrega al cliente sale con IVA (el Excel de la dueña se titula
# "LISTA DE PRECIOS IVA 15%" y multiplica por 1,15).
IVA_PCT: float = 15.0


# ---------------------------------------------------------------------------
# Telas de PRECIO ÚNICO (scintela.precio_plano) — migración 0159.
#
# La dueña (2026-08-04, foto por chat: "a proforma y precios agregar esto,
# tiene iva incluido") usa aparte de la matriz una tablita de telas que no
# tienen precio por clase de color: uno solo para todas. `precio` se guarda
# SIN IVA, igual que la matriz — el IVA lo agrega la hoja al imprimir.
# `ref_col` = "cobra lo mismo que esta columna de la matriz" (JERSEY 3,5 y
# JERSEY 3 = jersey), para que suban solas cuando sube el jersey.
# ---------------------------------------------------------------------------
_PLANO_SEED: list[tuple[int, str, float | None, str | None, str]] = [
    (1, "JERSEY 3,5", None, "jersey", "Precio de JERSEY"),
    (2, "JERSEY 3", None, "jersey", "Precio de JERSEY"),
    (3, "SCUBA", 9.7826, None, "Todos los colores"),
    (4, "SUPLEX", 8.8783, None, "Todos los colores"),
    (5, "BELTIS", 9.9913, None, "Todos los colores"),
    (6, "NATY", 7.7217, None, "Todos los colores"),
]

_plano_listo = False


def bootstrap_precio_plano() -> None:
    """Crea y siembra scintela.precio_plano si no existe.

    El deploy NO corre migraciones (ver 0159 y el precedente de
    modules/cheques/concepto_cobro.py), así que la tabla se crea en caliente
    la primera vez que alguien abre la pantalla. Idempotente y barato: una
    sola vez por proceso.
    """
    global _plano_listo
    if _plano_listo:
        return
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS scintela.precio_plano (
            id            SERIAL PRIMARY KEY,
            orden         INTEGER      NOT NULL DEFAULT 0,
            tela          VARCHAR(40)  NOT NULL UNIQUE,
            precio        NUMERIC(12, 4),
            ref_col       VARCHAR(20),
            nota          VARCHAR(60)  NOT NULL DEFAULT 'Todos los colores',
            actualizado   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
            usuario_edita VARCHAR(50)
        )
        """
    )
    for orden, tela, precio, ref_col, nota in _PLANO_SEED:
        db.execute(
            """
            INSERT INTO scintela.precio_plano (orden, tela, precio, ref_col, nota)
                 VALUES (%(orden)s, %(tela)s, %(precio)s, %(ref_col)s, %(nota)s)
            ON CONFLICT (tela) DO NOTHING
            """,
            {
                "orden": orden,
                "tela": tela,
                "precio": precio,
                "ref_col": ref_col,
                "nota": nota,
            },
        )
    _plano_listo = True


def precio_plano() -> list[dict]:
    """Las telas de precio único, en el orden en que las escribió la dueña."""
    bootstrap_precio_plano()
    return db.fetch_all(
        """
        SELECT id, orden, tela, precio, ref_col, nota
          FROM scintela.precio_plano
         ORDER BY orden ASC, id ASC
        """
    ) or []


def actualizar_precio_plano(id_fila: int, valor, usuario: str) -> None:
    """Actualiza el precio (SIN IVA) de una tela de precio único."""
    db.execute(
        """
        UPDATE scintela.precio_plano
           SET precio = %(valor)s,
               actualizado = CURRENT_TIMESTAMP,
               usuario_edita = %(usuario)s
         WHERE id = %(id)s
           AND ref_col IS NULL
        """,
        {"valor": valor, "usuario": usuario, "id": id_fila},
    )


def resolver_plano(planos: list[dict], filas: list[dict]) -> list[dict]:
    """Resuelve el precio final de cada tela de precio único.

    Las filas con `ref_col` no tienen número propio: cobran lo mismo que esa
    columna de la matriz. Como la matriz SÍ varía por clase de color, se
    muestra el rango (mínimo–máximo) y se deja dicho de dónde sale.
    Devuelve `precio` (float|None) y `rango` ((min, max)|None).
    """
    out: list[dict] = []
    for p in planos:
        precio = float(p["precio"]) if p.get("precio") is not None else None
        rango = None
        if p.get("ref_col"):
            vals = [
                float(f[p["ref_col"]])
                for f in filas
                if f.get(p["ref_col"]) is not None
            ]
            if vals:
                rango = (min(vals), max(vals))
                if rango[0] == rango[1]:
                    precio, rango = rango[0], None
        out.append({**p, "precio": precio, "rango": rango})
    return out


def telas_con_precio(filas: list[dict]) -> list[tuple[str, str]]:
    """Las telas que tienen AL MENOS un precio cargado, en el orden del dBase.

    En la hoja que se le entrega al cliente una columna entera vacía es ruido
    (hoy pasa con MEDICAL). Si mañana le cargan precio, vuelve sola.
    """
    return [
        (col, label)
        for col, label in TELAS
        if any(f.get(col) is not None for f in filas)
    ]


def columnas_hoja(filas: list[dict], planos: list[dict] | None = None) -> list[dict]:
    """TODAS las columnas de la hoja: la matriz + las telas de precio único.

    TMT 2026-08-04, corrección de la dueña sobre el primer intento ("¿para qué
    bloque vas a agregar? sumar a como está, no una foto así el screenshot"):
    las telas de precio único NO van en una tablita aparte — van como
    COLUMNAS más de la misma tabla, que es como están en su Excel (ahí la
    cifra se repite igual en las 5 clases de color).

    Dos tipos de columna:
      * `col`  — columna de la matriz: el precio cambia por clase de color.
      * `fijo` — cifra única: el mismo número en las 5 filas.
    Las de `ref_col` (JERSEY 3,5 / JERSEY 3) son columnas `col`='jersey': se
    dibujan PEGADAS a su tela de referencia y varían igual que ella.
    """
    refs: dict[str, list[dict]] = {}
    fijas: list[dict] = []
    for p in planos or []:
        if p.get("ref_col"):
            refs.setdefault(p["ref_col"], []).append(p)
        elif p.get("precio") is not None:
            fijas.append(p)

    cols: list[dict] = []
    for col, label in TELAS:
        if not any(f.get(col) is not None for f in filas):
            continue  # columna entera vacía: no va al papel (hoy, MEDICAL)
        cols.append({"key": col, "label": label, "col": col, "fijo": None})
        for p in refs.get(col, []):
            cols.append(
                {"key": f"pp{p['id']}", "label": p["tela"], "col": col, "fijo": None}
            )
    for p in fijas:
        cols.append(
            {
                "key": f"pp{p['id']}",
                "label": p["tela"],
                "col": None,
                "fijo": float(p["precio"]),
            }
        )
    return cols


def factor_hoja(tramo_idx: int, con_iva: bool) -> float:
    """El multiplicador de la hoja impresa: tramo de descuento (+ IVA)."""
    if not 0 <= tramo_idx < len(TRAMOS_DESCUENTO):
        raise ValueError(f"tramo inválido: {tramo_idx!r}")
    factor = TRAMOS_DESCUENTO[tramo_idx][1]
    if con_iva:
        factor *= 1.0 + (IVA_PCT / 100.0)
    return factor


def tabla_impresion(
    filas: list[dict],
    tramo_idx: int,
    con_iva: bool,
    columnas: list[dict] | None = None,
) -> list[dict]:
    """La matriz (clases x columnas) lista para imprimir.

    Aplica UN tramo de descuento (índice en TRAMOS_DESCUENTO) y, si
    `con_iva`, el IVA sobre el neto. Devuelve una fila por clase con
    `valores` alineado al orden de `columnas` (ver `columnas_hoja`; por
    default, las 12 de la matriz). None donde no hay precio. Es derivado: no
    se guarda nada.
    """
    cols = (
        columnas
        if columnas is not None
        else [{"key": c, "label": lab, "col": c, "fijo": None} for c, lab in TELAS]
    )
    factor = factor_hoja(tramo_idx, con_iva)
    out: list[dict] = []
    for f in filas:
        valores = []
        for c in cols:
            if c.get("fijo") is not None:
                valores.append(round(float(c["fijo"]) * factor, 2))
                continue
            bruto = f.get(c["col"]) if c.get("col") else None
            valores.append(
                round(float(bruto) * factor, 2) if bruto is not None else None
            )
        out.append(
            {
                "clase": int(f["clase"]),
                "descripcio": f["descripcio"],
                "valores": valores,
            }
        )
    return out


def tabla_descuentos_columna(filas: list[dict], col: dict) -> list[dict]:
    """Igual que `tabla_descuentos` pero para una columna de `columnas_hoja`.

    Sirve tanto para una tela de la matriz (el precio cambia por clase) como
    para una de precio único (`fijo`: la misma cifra en las 5 clases), así el
    selector de la pantalla las ofrece a todas por igual.
    """
    if col.get("fijo") is not None:
        fijo = float(col["fijo"])
        return [
            {
                "clase": int(f["clase"]),
                "descripcio": f["descripcio"],
                "lista": fijo,
                "netos": [round(fijo * factor, 2) for _, factor in TRAMOS_DESCUENTO],
            }
            for f in filas
        ]
    return tabla_descuentos(filas, col["col"])


def tabla_descuentos(filas: list[dict], columna: str) -> list[dict]:
    """Precio de lista (Basico) y neto a los 4 tramos en cascada (5%, 5%+9%,
    5%+14%), por clase de color, para UNA tela (`columna`). Solo lectura -- los
    descuentos son derivados, no se guardan.
    """
    if columna not in COLUMNAS_TELA:
        raise ValueError(f"columna invalida: {columna!r}")
    out: list[dict] = []
    for f in filas:
        lista = f.get(columna)
        netos: list[float | None] = []
        if lista is not None:
            lista = float(lista)
            for _, factor in TRAMOS_DESCUENTO:
                netos.append(round(lista * factor, 2))
        else:
            netos = [None for _ in TRAMOS_DESCUENTO]
        out.append(
            {
                "clase": int(f["clase"]),
                "descripcio": f["descripcio"],
                "lista": lista,
                "netos": netos,
            }
        )
    return out
