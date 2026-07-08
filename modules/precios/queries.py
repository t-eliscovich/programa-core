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
    ("falso", "FALSO"),
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
