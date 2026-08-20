"""Los tipos de proveedor, en UN solo lugar (Tamara 2026-08-20).

El tipo no es un rótulo: decide en qué caja cae el proveedor. Los tipo 'H'
entran como MATERIA PRIMA en el flujo de fondos (separada de gastos,
modules/informes/queries.py), 'U' se trata como maquinaria y 'Q' como químicos
en dólares. Cargar un proveedor con la letra equivocada lo manda a la caja
equivocada.

Antes había TRES listas distintas conviviendo: la pantalla de alta ofrecía
B/S/M/I (tres letras que no tenía ningún proveedor de la base), el filtro de
/proveedores ofrecía U/H/Q/B/Y/K y el lapicito de la columna Tipo U/H/Q/B/Y
(sin K). Ahora las tres leen de acá.

Las letras viejas que ya están cargadas (por ejemplo 'M', 37 proveedores) NO se
tocan: la pantalla las muestra como están y las deja elegidas, así editarle el
teléfono a un proveedor no le cambia la clasificación.
"""

# Código de una letra -> cómo se lee en pantalla.
TIPOS: list[tuple[str, str]] = [
    ("U", "Máquina"),
    ("H", "Hilado"),
    ("Q", "Químicos"),
    ("B", "Bancos"),
    ("Y", "Otro"),
    ("K", "Tejido"),
]

CODIGOS: list[str] = [codigo for codigo, _ in TIPOS]

# "U · Máquina", para los desplegables que muestran código y nombre juntos.
OPCIONES: list[tuple[str, str]] = [
    (codigo, f"{codigo} · {nombre}") for codigo, nombre in TIPOS
]

# "U=Máquina · H=Hilado · …", para los title= de las columnas.
AYUDA: str = " · ".join(f"{codigo}={nombre}" for codigo, nombre in TIPOS)


def es_valido(tipo: str | None) -> bool:
    """True si `tipo` es uno de los que ofrece la pantalla (vacío también)."""
    limpio = (tipo or "").strip().upper()
    return not limpio or limpio in CODIGOS
