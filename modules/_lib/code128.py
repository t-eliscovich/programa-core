"""Código de barras Code 128, el mismo que imprime Asinfo en la factura.

La clave de acceso del SRI son 49 dígitos y abajo del recuadro va su código de
barras. Para que la copia sea la copia, ese código tiene que salir idéntico —
no parecido: un cliente que lo escanea tiene que leer los mismos 49 dígitos.

⭐ Cómo sabemos que es idéntico y no "parecido". Se leyó el PDF de la
001-099-000182675 barra por barra: 169 rectángulos, 310 módulos, y se
compararon UNO A UNO contra lo que devuelve `anchos()`. Dan exactamente lo
mismo. De paso quedó a la vista cómo lo arma Asinfo, que no era obvio:

  · **Code 128 subconjunto C** (dos dígitos por símbolo), nada de A ni B.
  · Los 49 dígitos son IMPARES, así que se les pone un CERO ADELANTE para
    poder aparearlos. El símbolo de arranque es siempre Start C.
  · 27 símbolos = arranque + 25 pares + verificador, más el de parada.

El test `test_code128.py` repite esa comparación contra los anchos reales
sacados del PDF: si alguien toca la tabla, se entera ahí.
"""
from __future__ import annotations

#: Los 107 símbolos del estándar. Cada uno son seis anchos —barra, espacio,
#: barra, espacio, barra, espacio— que suman 11 módulos; el de parada (106)
#: tiene siete y suma 13. El índice ES el valor del símbolo.
PATRONES = (
    "212222", "222122", "222221", "121223", "121322", "131222", "122213",
    "122312", "132212", "221213", "221312", "231212", "112232", "122132",
    "122231", "113222", "123122", "123221", "223211", "221132", "221231",
    "213212", "223112", "312131", "311222", "321122", "321221", "312212",
    "322112", "322211", "212123", "212321", "232121", "111323", "131123",
    "131321", "112313", "132113", "132311", "211313", "231113", "231311",
    "112133", "112331", "132131", "113123", "113321", "133121", "313121",
    "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114",
    "413111", "241112", "134111", "111242", "121142", "121241", "114212",
    "124112", "124211", "411212", "421112", "421211", "212141", "214121",
    "412121", "111143", "111341", "131141", "114113", "114311", "411113",
    "411311", "113141", "114131", "311141", "411131", "211412", "211214",
    "211232", "2331112",
)

ARRANQUE_C = 105
PARADA = 106

#: Espacio en blanco a los lados. Sin él ningún lector engancha el código.
#: Asinfo deja 10 módulos de cada lado y acá se hace lo mismo.
SILENCIO = 10


def valores(digitos: str) -> list[int]:
    """Los símbolos de la clave, verificador y parada incluidos.

    Levanta `ValueError` si viene algo que no sea un dígito: un código de
    barras que dice cualquier cosa es peor que no tener código de barras.
    """
    d = (digitos or "").strip()
    if not d or not d.isdigit():
        raise ValueError("el código de barras se arma sólo con dígitos")
    if len(d) % 2:
        d = "0" + d
    vals = [ARRANQUE_C] + [int(d[i:i + 2]) for i in range(0, len(d), 2)]
    verificador = (vals[0] + sum(i * v for i, v in enumerate(vals[1:], start=1))) % 103
    return [*vals, verificador, PARADA]


def anchos(digitos: str) -> list[int]:
    """Los anchos de barras y espacios, en módulos, empezando por una barra."""
    return [int(c) for v in valores(digitos) for c in PATRONES[v]]


def barras(digitos: str) -> list[tuple[int, int]]:
    """Sólo las BARRAS negras, como (desde, ancho) en módulos.

    Los espacios no se dibujan: el papel ya es blanco. Se devuelve así para
    que la plantilla arme un `<svg>` de dos líneas y nada más.
    """
    salida = []
    x = SILENCIO
    for i, a in enumerate(anchos(digitos)):
        if i % 2 == 0:
            salida.append((x, a))
        x += a
    return salida


def ancho_total(digitos: str) -> int:
    """Cuántos módulos ocupa el código con sus dos silencios."""
    return sum(anchos(digitos)) + 2 * SILENCIO
