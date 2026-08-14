"""El TIPO de documento de una factura: una sola definición para toda la app.

TMT 2026-08-14 (dueña): *"no anda el filtro"* + *"¿cuánto suman?"*. Abajo de
esos dos síntomas había TRES clasificadores que no se hablaban:

1. `factura.tipo` en la base, escrita en DOS idiomas — el importador viejo
   ponía una letra (F/N/C/D/X) y el nuevo dos (FA/NT/NC/DE), porque la columna
   era `varchar(2)` y guardaba `tipo_asinfo[:2]`. Ese recorte hacía que
   `NC_FINANCIERA` y `NCNT` cayeran las dos en "NC": 235 financieras y 18 NCNT
   mezcladas bajo la misma etiqueta.
2. La letra que muestra la lista, adivinada en el template comparando
   prefijos. Las 263 NCNT viejas (tipo 'X') no le encajaban en ninguna regla y
   caían en el `or negativo` final: se mostraban como **D · Devolución**.
3. El filtro del combo, que no miraba la columna: le preguntaba a Asinfo en el
   momento y adivinaba por (cliente, fecha, kilos). En la pestaña Estado esa
   consulta ni se hacía, así que filtrar daba SIEMPRE cero sobre 35.526
   facturas.

Vocabulario único, elegido por la dueña (*"llamalas NC y NCNT y devolución,
mantengamos lo que trae el Asinfo"*): son los mismos códigos que la pantalla ya
mostraba en el combo. La columna quedó en `varchar(4)` — justo lo que mide
`NCNT` — para que el límite lo cuide la base y nadie vuelva a guardar un nombre
recortado.

    F     Factura
    D     Devolución          — mercadería que vuelve, con número de FACTURA
    N     NTEN (nota de entrega)
    NC    NC financiera       — descuento/ajuste, NO mueve mercadería (kg = 0)
    NCNT  NCNT                — mercadería que vuelve, con numeración PROPIA
                                (`NCNT-01095`)

`NC` y `NCNT` se llaman parecido y son cosas distintas: la financiera es plata
pura (importes de centavos, kg 0); la NCNT es mercadería devuelta. `D` y `NCNT`
devuelven las dos mercadería y se distinguen por la numeración, que es como las
emite Asinfo.

La `X` que usaba el idioma viejo para NCNT se retiró a propósito: se lee como
"eliminada" (que es un `stat`, no un tipo) y ya confundió a la dueña.
"""
from __future__ import annotations

FACTURA = "F"
DEVOLUCION = "D"
NTEN = "N"
NC_FINANCIERA = "NC"
NCNT = "NCNT"

# Orden en que se ofrecen en el combo.
CODIGOS = (FACTURA, DEVOLUCION, NTEN, NC_FINANCIERA, NCNT)

ETIQUETAS = {
    FACTURA: "Factura",
    DEVOLUCION: "Devolución",
    NTEN: "NTEN",
    NC_FINANCIERA: "NC financiera",
    NCNT: "NCNT",
}

# El color con el que la lista pinta cada letra.
COLORES = {
    FACTURA: "text-slate-400",
    DEVOLUCION: "text-amber-600",
    NTEN: "text-sky-600",
    NC_FINANCIERA: "text-rose-600",
    NCNT: "text-rose-600",
}

# Cómo se llama cada uno en Asinfo (lo que devuelve asinfo.service).
DESDE_ASINFO = {
    "FACTURA": FACTURA,
    "DEVOLUCION": DEVOLUCION,
    "NTEN": NTEN,
    "NC_FINANCIERA": NC_FINANCIERA,
    "NCNT": NCNT,
}

# Los dos idiomas viejos → el único nuevo. `NC` es AMBIGUO a propósito y no
# está acá: se resuelve por el signo de los kilos (ver `normalizar`).
DESDE_LEGACY = {
    "F": FACTURA, "FA": FACTURA,
    "D": DEVOLUCION, "DE": DEVOLUCION,
    "N": NTEN, "NT": NTEN,
    "C": NC_FINANCIERA,
    "X": NCNT,
}


def normalizar(tipo, *, kg=None, numf_completo=None) -> str | None:
    """Devuelve el código único, venga el tipo de donde venga.

    `kg` y `numf_completo` sólo se usan para desempatar lo que el idioma viejo
    dejó ambiguo; nunca pisan un tipo que ya se entiende solo.
    """
    t = (tipo or "").strip().upper()

    if t in DESDE_ASINFO:
        return DESDE_ASINFO[t]
    if t in CODIGOS and t != NC_FINANCIERA:
        return t

    # ── El caso ambiguo ──────────────────────────────────────────────────
    # "NC" quiere decir dos cosas distintas según quién la escribió: el
    # código nuevo NC_FINANCIERA, o el recorte de NCNT que hacía el
    # importador. Los separa la mercadería: una nota de crédito financiera
    # es un descuento y no mueve kilos; una NCNT devuelve mercadería.
    if t == "NC":
        if numf_completo and str(numf_completo).strip().upper().startswith("NCNT"):
            return NCNT
        if kg is not None and float(kg) < 0:
            return NCNT
        return NC_FINANCIERA

    if t in DESDE_LEGACY:
        return DESDE_LEGACY[t]

    # ── Sin tipo: lo que se pueda deducir del documento ──────────────────
    if numf_completo:
        n = str(numf_completo).strip().upper()
        if n.startswith("NCNT"):
            return NCNT
        if n.startswith("NTEN"):
            return NTEN

    # Un kg negativo es mercadería que volvió, pero no alcanza para saber si
    # fue por devolución o por NCNT: se deja sin clasificar antes que mentir.
    return None


def etiqueta(codigo) -> str:
    return ETIQUETAS.get((codigo or "").strip().upper(), "")


def color(codigo) -> str:
    return COLORES.get((codigo or "").strip().upper(), "text-slate-400")
