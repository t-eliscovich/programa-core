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
    """Las 5 clases de color con sus 12 precios, ordenadas por clase."""
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


# --- Descuentos y porcentajes (como los muestra el dBase) --------------------
#
# El dBase, además de la matriz base (precio de LISTA USD/kg por clase de color
# x tela), maneja dos cosas que la lista "pelada" de PC no mostraba:
#
#   1) DESCUENTO por cliente (CLIENTES.DBF, campo DESCUENTO, valores 0..14 %):
#      el precio que paga cada cliente = precio de lista * (1 - desc/100).
#      El dBase parte la clientela en MINORISTA (DESCUENTO <= 7) y MAYORISTA
#      (DESCUENTO > 7) — INFORMES.PRG líneas 714-715 y 753-754
#      (MAYO = IIF(B->DESCUENTO>7,...) / MENO = IIF(B->DESCUENTO<=7,...)).
#
#   2) El PORCENTAJE de recargo de cada clase de color sobre el BLANCO (clase 1),
#      que es la "escalera" de precios embebida en la matriz.
#
# Los descuentos son DERIVADOS (se calculan al vuelo desde la matriz base), así
# que no hacen falta columnas nuevas ni migración: si cambia un precio de lista,
# los precios con descuento se recalculan solos.

# Corte mayorista/minorista del dBase (DESCUENTO > 7 = mayorista).
CORTE_MAYORISTA = 7

# Tramos de descuento estándar (los que realmente usa CLIENTES.DBF). El 0 % =
# precio de lista sin descuento. Se muestran en dos grupos: MINORISTA (<=7) y
# MAYORISTA (>7), igual que el dBase.
TRAMOS_DESCUENTO: list[int] = [0, 4, 5, 6, 7, 9, 10, 12, 14]


def _num(v):
    """Float seguro (los precios vienen como Decimal/None desde la DB)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def porcentajes_sobre_blanco(filas: list[dict]) -> dict[int, dict[str, float]]:
    """Para cada clase (>1) y tela, el % de recargo sobre el BLANCO (clase 1).

    Devuelve {clase: {columna_tela: pct}}. Si falta el precio base o el de la
    clase, esa celda queda fuera del dict.
    """
    base = None
    for f in filas:
        if int(f["clase"]) == 1:
            base = f
            break
    out: dict[int, dict[str, float]] = {}
    if not base:
        return out
    for f in filas:
        cl = int(f["clase"])
        if cl == 1:
            continue
        celdas: dict[str, float] = {}
        for col, _ in TELAS:
            b = _num(base.get(col))
            v = _num(f.get(col))
            if b and v is not None and b != 0:
                celdas[col] = (v - b) / b * 100.0
        out[cl] = celdas
    return out


def tabla_descuentos(filas: list[dict], columna: str) -> list[dict]:
    """Precio de lista y precio con cada tramo de descuento, por clase de color,
    para UNA tela (`columna`). Réplica de "precio de lista - DESCUENTO cliente".

    Cada fila: {clase, descripcio, lista, netos: {tramo: precio_neto}}.
    """
    if columna not in COLUMNAS_TELA:
        columna = TELAS[0][0]
    out: list[dict] = []
    for f in filas:
        lista = _num(f.get(columna))
        netos = {}
        if lista is not None:
            for d in TRAMOS_DESCUENTO:
                netos[d] = round(lista * (1 - d / 100.0), 2)
        out.append(
            {
                "clase": int(f["clase"]),
                "descripcio": f["descripcio"],
                "lista": lista,
                "netos": netos,
            }
        )
    return out
