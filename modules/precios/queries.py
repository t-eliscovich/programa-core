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


# Los TRES descuentos de cliente que interesan (los valores dominantes en
# CLIENTES.DBF: 4% -> 1034 clientes, 7% -> 916, 9% -> 651). El dBase
# (INFORMES.PRG :714-715 y :754-755) parte a los clientes en Minorista
# (DESCUENTO <= 7%) y Mayorista (DESCUENTO > 7%): por eso 4% y 7% son
# Minorista y 9% es Mayorista. Precio neto = lista * (1 - d/100).
TRAMOS_DESCUENTO: list[int] = [4, 7, 9]
CORTE_MAYORISTA: int = 7  # <= 7% Minorista, > 7% Mayorista


def tabla_descuentos(filas: list[dict], columna: str) -> list[dict]:
    """Precio de lista y neto a 4/7/9% de descuento, por clase de color, para
    UNA tela (`columna`). Solo lectura -- los descuentos son derivados, no se
    guardan. Replica compacta de "precio de lista - DESCUENTO del cliente".
    """
    if columna not in COLUMNAS_TELA:
        raise ValueError(f"columna invalida: {columna!r}")
    out: list[dict] = []
    for f in filas:
        lista = f.get(columna)
        netos: dict[int, float | None] = {}
        if lista is not None:
            lista = float(lista)
            for d in TRAMOS_DESCUENTO:
                netos[d] = round(lista * (1 - d / 100.0), 2)
        else:
            for d in TRAMOS_DESCUENTO:
                netos[d] = None
        out.append(
            {
                "clase": int(f["clase"]),
                "descripcio": f["descripcio"],
                "lista": lista,
                "netos": netos,
            }
        )
    return out
