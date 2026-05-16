"""Vocabulario central de Programa Core (TMT 2026-05-12).

Único lugar donde se decide cómo se llama cada cosa en la UI. Si en algún
template/queries aparece un nombre que NO viene de acá, es bug y rompe la
consistencia que la dueña pidió: "asi son siempre las mismas variables y
entre todos entendemos lo que hacemos".

Convenciones:
  - Las constantes con _CODE/_KEY son los valores internos (lo que va a DB,
    al form, al concepto). Nunca traducir.
  - Las constantes con _LABEL son textos para humanos. Traducir si hace
    falta UI multi-idioma — pero hoy todo está en español rioplatense.
  - Las funciones helper (`label_banco`, `label_tipo_compra`) se importan
    desde templates/views para evitar hardcodear texto en lugares random.

Reglas decididas con TMT (2026-05-12):
  - Bancos en UI: "Pichincha", "Internacional" (sin abreviar).
  - Identificador interno de cuenta de pago: lowercase ('pichincha',
    'internacional', 'caja'). Tres opciones, sin variantes.
  - Tipos de compra: singular masculino — "Hilado", "Tejido", "Tintura",
    "Químicos", "Otros", "Anticipo", "Servicios". NO usar "Hilados",
    "Productos químicos", etc.
  - Estados de cheque: el código corto (Z/B/E/etc.) es interno; en UI
    siempre el label legible. Nunca mostrar la letra al usuario salvo
    en tooltips diagnósticos.
  - Conceptos legacy (PICH/INTER/RR/PR): se mantienen como tipea la
    contadora — el sistema los parsea, no los traduce.
"""
from __future__ import annotations

# ─────────────────────────── BANCOS ────────────────────────────────
# Nombres canónicos para mostrar en UI.

BANCO_PICHINCHA = "Pichincha"
BANCO_INTERNACIONAL = "Internacional"

# Cuentas de pago — identificador interno (string lowercase) y label.
CUENTA_PAGO_CAJA = "caja"
CUENTA_PAGO_PICHINCHA = "pichincha"
CUENTA_PAGO_INTERNACIONAL = "internacional"

CUENTAS_PAGO_VALIDAS = (
    CUENTA_PAGO_CAJA,
    CUENTA_PAGO_PICHINCHA,
    CUENTA_PAGO_INTERNACIONAL,
)

CUENTAS_PAGO_LABEL = {
    CUENTA_PAGO_CAJA:          "Caja",
    CUENTA_PAGO_PICHINCHA:     f"Banco {BANCO_PICHINCHA}",
    CUENTA_PAGO_INTERNACIONAL: f"Banco {BANCO_INTERNACIONAL}",
}


def label_cuenta_pago(key: str) -> str:
    """'pichincha' → 'Banco Pichincha'. Devuelve la key si no la conoce."""
    return CUENTAS_PAGO_LABEL.get((key or "").lower().strip(), key or "")


# ─────────────────────── TIPOS DE COMPRA ───────────────────────────
# Código de 1 letra en scintela.compra.tipo. Mantener en sincronía con
# modules/compras/queries.py:TIPOS_VALIDOS.

TIPO_COMPRA_HILADO    = "H"
TIPO_COMPRA_TEJIDO    = "K"
TIPO_COMPRA_TINTURA   = "T"
TIPO_COMPRA_QUIMICOS  = "Q"
TIPO_COMPRA_OTROS     = "C"
TIPO_COMPRA_ANTICIPO  = "A"
TIPO_COMPRA_SERVICIOS = "S"

TIPOS_COMPRA_LABEL = {
    TIPO_COMPRA_HILADO:    "Hilado",        # NO "Hilados"
    TIPO_COMPRA_TEJIDO:    "Tejido",        # K en legacy
    TIPO_COMPRA_TINTURA:   "Tintura",
    TIPO_COMPRA_QUIMICOS:  "Químicos",      # NO "Productos químicos"
    TIPO_COMPRA_OTROS:     "Otros",         # NO "Cosas de fábrica" en UI
    TIPO_COMPRA_ANTICIPO:  "Anticipo",
    TIPO_COMPRA_SERVICIOS: "Servicios",
}

# Etiqueta larga (para tooltips o pantallas detalladas).
TIPOS_COMPRA_DESCRIPCION = {
    TIPO_COMPRA_HILADO:    "Hilado — kg de hilado",
    TIPO_COMPRA_TEJIDO:    "Tejido — servicio de tejeduría o producción propia",
    TIPO_COMPRA_TINTURA:   "Tintura — servicio de tintorería",
    TIPO_COMPRA_QUIMICOS:  "Químicos — colorantes y auxiliares",
    TIPO_COMPRA_OTROS:     "Otros — repuestos, aceite, consumibles de fábrica",
    TIPO_COMPRA_ANTICIPO:  "Anticipo — pago a proveedor sin factura todavía",
    TIPO_COMPRA_SERVICIOS: "Servicios — luz, agua, contadora, mantenimiento",
}


def label_tipo_compra(codigo: str) -> str:
    """'H' → 'Hilado'."""
    return TIPOS_COMPRA_LABEL.get((codigo or "").upper().strip(), codigo or "")


# ─────────────────────── ESTADOS DE CHEQUE ─────────────────────────
# Vocabulario canónico ver modules/cheques/queries.py docstring.

ESTADO_CHEQUE_CARTERA      = "Z"
ESTADO_CHEQUE_DEPOSITADO   = "B"
ESTADO_CHEQUE_POSTERGADO   = "P"
ESTADO_CHEQUE_DANIELA      = "D"
ESTADO_CHEQUE_ENDOSADO     = "E"
ESTADO_CHEQUE_DEVUELTO_1   = "1"
ESTADO_CHEQUE_DEVUELTO_2   = "2"
ESTADO_CHEQUE_REBOTE_FINAL = "3"
ESTADO_CHEQUE_ANULADO      = "X"
# Legacy — no se generan más, pero se preservan en datos viejos:
ESTADO_CHEQUE_LEGACY_INTER = "V"  # depositado Internacional viejo
ESTADO_CHEQUE_LEGACY_ACRED = "A"  # acreditado viejo
ESTADO_CHEQUE_LEGACY_REB   = "R"  # rebotado genérico viejo

ESTADOS_CHEQUE_LABEL = {
    ESTADO_CHEQUE_CARTERA:      "En cartera",
    ESTADO_CHEQUE_DEPOSITADO:   "Depositado",
    ESTADO_CHEQUE_POSTERGADO:   "Postergado",
    ESTADO_CHEQUE_DANIELA:      "En gestión Daniela",
    ESTADO_CHEQUE_ENDOSADO:     "Endosado",
    ESTADO_CHEQUE_DEVUELTO_1:   "Devuelto",
    ESTADO_CHEQUE_DEVUELTO_2:   "Devuelto",
    ESTADO_CHEQUE_REBOTE_FINAL: "Segundo rechazo",
    ESTADO_CHEQUE_ANULADO:      "Anulado",
    ESTADO_CHEQUE_LEGACY_INTER: "Depositado (legacy Internacional)",
    ESTADO_CHEQUE_LEGACY_ACRED: "Acreditado (legacy)",
    ESTADO_CHEQUE_LEGACY_REB:   "Rebotado (legacy)",
}


def label_estado_cheque(stat: str) -> str:
    return ESTADOS_CHEQUE_LABEL.get((stat or "").upper().strip(), stat or "")


# ─────────────────────── TIPOS DE CAJA ─────────────────────────────
# scintela.caja.tipo

TIPO_CAJA_ENTRADA = "E"
TIPO_CAJA_SALIDA  = "S"
TIPO_CAJA_CB      = "CB"  # contra banco — caja sale, banco recibe

TIPOS_CAJA_LABEL = {
    TIPO_CAJA_ENTRADA: "Entrada",
    TIPO_CAJA_SALIDA:  "Salida",
    TIPO_CAJA_CB:      "Contra banco",
}


def label_tipo_caja(tipo: str) -> str:
    return TIPOS_CAJA_LABEL.get((tipo or "").upper().strip(), tipo or "")


# ─────────────────────── CONCEPTOS LEGACY ──────────────────────────
# Los códigos que la contadora tipea en el concepto (PR/RR/PICH/INTER/IN/INHB)
# son convención del programa dBase original. NO se traducen — el sistema
# los reconoce y dispara side effects automáticos. Acá listamos los labels
# explicativos para las pantallas de ayuda.

CONCEPTO_LEGACY_LABEL = {
    "PICH":  f"Depósito a banco {BANCO_PICHINCHA}",
    "INTER": f"Depósito a banco {BANCO_INTERNACIONAL}",
    "PR":    "Pago a proveedor",
    "RR":    "Retiro de socio",
    "IN":    "Movimiento de cuenta dólares",
    "INHB":  "Caja chica / día inhábil",
}


# ─────────────────────── DOCUMENTOS BANCARIOS ──────────────────────
# scintela.transacciones_bancarias.documento

DOC_BANCO_CHEQUE_EMITIDO  = "CH"
DOC_BANCO_DEPOSITO        = "DE"
DOC_BANCO_TRANSFERENCIA   = "TR"
DOC_BANCO_NOTA_DEBITO     = "ND"
DOC_BANCO_NOTA_CREDITO    = "NC"
DOC_BANCO_ACREDITACION    = "AC"

DOCS_BANCO_LABEL = {
    DOC_BANCO_CHEQUE_EMITIDO: "Cheque emitido",
    DOC_BANCO_DEPOSITO:       "Depósito",
    DOC_BANCO_TRANSFERENCIA:  "Transferencia recibida",
    DOC_BANCO_NOTA_DEBITO:    "Nota de débito",
    DOC_BANCO_NOTA_CREDITO:   "Nota de crédito",
    DOC_BANCO_ACREDITACION:   "Acreditación",
}


def label_doc_banco(doc: str) -> str:
    return DOCS_BANCO_LABEL.get((doc or "").upper().strip(), doc or "")


def es_doc_egreso(doc: str) -> bool:
    """True si el documento resta del saldo bancario."""
    return (doc or "").upper().strip() in (DOC_BANCO_CHEQUE_EMITIDO,
                                            DOC_BANCO_NOTA_DEBITO, "DB")


def es_doc_ingreso(doc: str) -> bool:
    """True si suma al saldo bancario."""
    return (doc or "").upper().strip() in (DOC_BANCO_DEPOSITO,
                                            DOC_BANCO_TRANSFERENCIA,
                                            DOC_BANCO_ACREDITACION,
                                            DOC_BANCO_NOTA_CREDITO)


# ─────────────────────── ESTADOS DE FACTURA ────────────────────────
# scintela.factura.stat

ESTADO_FACTURA_EMITIDA   = "Z"
ESTADO_FACTURA_ABONADA   = "A"
ESTADO_FACTURA_TOTAL     = "T"  # cancelada totalmente
ESTADO_FACTURA_ANULADA   = "Y"

ESTADOS_FACTURA_LABEL = {
    ESTADO_FACTURA_EMITIDA: "Emitida",
    ESTADO_FACTURA_ABONADA: "Abonada parcial",
    ESTADO_FACTURA_TOTAL:   "Cancelada",
    ESTADO_FACTURA_ANULADA: "Anulada",
}


def label_estado_factura(stat: str) -> str:
    return ESTADOS_FACTURA_LABEL.get((stat or "").upper().strip(), stat or "")
