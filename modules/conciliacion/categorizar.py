"""Categorización heurística de movimientos bancarios.

TMT 2026-05-23 — pedido Tamara: que cualquiera pueda hacer la conciliación
sin conocer códigos internos. Asignamos a cada mov una categoría visible
("Cobro por transferencia", "Pago a proveedor", "Comisión bancaria", etc.)
basándonos en regex sobre el concepto + Tipo C/D.

Si la heurística devuelve `OTRO`, el caller puede llamar a `ai_categorizar`
para que Claude Haiku lo intente. Esa función NO se llama desde acá para
mantener este módulo libre de I/O.

Categorías canónicas (grupo en MAYÚS, label legible en sentence case):

  ENTRADA:
    ENTRADA_COBRO_TRANSFERENCIA   "Cobro por transferencia"
    ENTRADA_COBRO_CHEQUE          "Depósito de cheque"
    ENTRADA_DEPOSITO_EFECTIVO     "Depósito en efectivo"
    ENTRADA_NOTA_CREDITO          "Nota de crédito banco"
    ENTRADA_OTRO                  "Entrada (otro)"

  SALIDA:
    SALIDA_PAGO_PROVEEDOR         "Pago a proveedor"
    SALIDA_PAGO_NOMINA            "Pago de nómina / vacaciones"
    SALIDA_PAGO_SERVICIO          "Pago de servicio (DHL, luz, etc.)"
    SALIDA_PAGO_ANTICIPO          "Pago de anticipo"
    SALIDA_CHEQUE_EMITIDO         "Cheque emitido"
    SALIDA_TRANSFERENCIA          "Transferencia enviada"
    SALIDA_OTRO                   "Salida (otro)"

  COMISION:
    COMISION_BANCARIA             "Comisión bancaria"
    IMPUESTO                      "Impuesto / retención"

  Sin clasificar:
    OTRO                          "Sin categorizar"

El "grupo" arriba (ENTRADA/SALIDA/COMISION) es lo que usa la UI para
agrupar las tablas (pedido Tamara).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ─── Constantes ───────────────────────────────────────────────────────────

# Mapeo categoría → (grupo, label legible, abreviatura compacta).
# La abreviatura se usa como chip en la UI (1-3 chars). Tooltip muestra label.
GRUPO_LABEL: dict[str, tuple[str, str, str]] = {
    # Entradas
    "ENTRADA_COBRO_TRANSFERENCIA": ("ENTRADA", "Cobro por transferencia", "TR"),
    "ENTRADA_COBRO_CHEQUE":        ("ENTRADA", "Depósito de cheque",       "CH"),
    "ENTRADA_DEPOSITO_EFECTIVO":   ("ENTRADA", "Depósito en efectivo",     "DEP"),
    "ENTRADA_NOTA_CREDITO":        ("ENTRADA", "Nota de crédito banco",    "NC"),
    "ENTRADA_OTRO":                ("ENTRADA", "Entrada (otro)",            "?↓"),
    # Salidas
    "SALIDA_PAGO_PROVEEDOR":       ("SALIDA",  "Pago a proveedor",          "P"),
    "SALIDA_PAGO_NOMINA":          ("SALIDA",  "Pago de nómina/vacaciones", "N"),
    "SALIDA_PAGO_SERVICIO":        ("SALIDA",  "Pago de servicio",          "S"),
    "SALIDA_PAGO_ANTICIPO":        ("SALIDA",  "Pago de anticipo",          "A"),
    "SALIDA_CHEQUE_EMITIDO":       ("SALIDA",  "Cheque emitido",            "CH"),
    "SALIDA_TRANSFERENCIA":        ("SALIDA",  "Transferencia enviada",     "TR"),
    "SALIDA_OTRO":                 ("SALIDA",  "Salida (otro)",             "?↑"),
    # Comisiones / impuestos (van aparte)
    "COMISION_BANCARIA":           ("COMISION", "Comisión bancaria",        "COM"),
    "IMPUESTO":                    ("COMISION", "Impuesto / retención",     "IMP"),
    # Sin clasificar
    "OTRO":                        ("OTRO",    "Sin categorizar",           "?"),
}


@dataclass
class Categoria:
    codigo: str       # ej: "SALIDA_PAGO_PROVEEDOR"
    grupo: str        # ENTRADA | SALIDA | COMISION | OTRO
    label: str        # "Pago a proveedor"
    abrev: str        # "P" | "TR" | "CH" | etc. — chip compacto en UI
    confianza: float  # 0..1 — 1.0 si regex acertó certero; 0.5 si fallback por Tipo
    fuente: str       # "regex" | "ai" | "tipo-fallback"


def _label(codigo: str) -> tuple[str, str, str]:
    """Devuelve (grupo, label, abrev) para un codigo."""
    return GRUPO_LABEL.get(codigo, ("OTRO", "Sin categorizar", "?"))


# ─── Reglas (orden importa: la primera que matchea gana) ─────────────────

# Cada regla es (regex, tipo_filtro_opcional, categoria).
# tipo_filtro: "C" | "D" | None (sin restricción).
_REGLAS: list[tuple[re.Pattern, str | None, str]] = [
    # COMISIONES / IMPUESTOS (alta prioridad — son chicos)
    (re.compile(r"\b(comision|comisi[oó]n|cargo\s+por|servicio\s+banco)\b", re.I), None, "COMISION_BANCARIA"),
    (re.compile(r"\b(iva|retenci[oó]n|impuesto|isr|sri|^rr\s|^rr-)", re.I), None, "IMPUESTO"),

    # ENTRADAS
    (re.compile(r"transferencia\s+(directa|interbancaria|interna)?\s*(de|recibida)", re.I), "C", "ENTRADA_COBRO_TRANSFERENCIA"),
    (re.compile(r"^\s*tr\b|^\s*trf\b|\btransferencia\b", re.I), "C", "ENTRADA_COBRO_TRANSFERENCIA"),
    (re.compile(r"dep[oó]sito\s+(efectivo|efectivizado|en\s+efectivo|caja)", re.I), "C", "ENTRADA_DEPOSITO_EFECTIVO"),
    (re.compile(r"\b\d*\s*ch\.?\s*[A-Z]{2,5}", re.I), "C", "ENTRADA_COBRO_CHEQUE"),
    (re.compile(r"dep[oó]sito|\bdep\b", re.I), "C", "ENTRADA_COBRO_CHEQUE"),
    (re.compile(r"nota\s+(de\s+)?cr[eé]dito|\bnc\b", re.I), "C", "ENTRADA_NOTA_CREDITO"),

    # SALIDAS específicas (orden importa)
    (re.compile(r"pag[\.\-]?\s*anticipo|p[\.\-]ant|\banticipo\b|^ac\s|^ac-", re.I), "D", "SALIDA_PAGO_ANTICIPO"),
    (re.compile(r"pag[\.\-]?\s*(vaca|nomina|sueldo|decimo|d[eé]cimo|util|rrhh|empleado)", re.I), "D", "SALIDA_PAGO_NOMINA"),
    # Servicios (con catálogo de proveedores conocidos Ecuador)
    (re.compile(r"pag[\.\-]?\s*(dhl|luz|agua|tel[eé]fono|internet|servicio|transporte|emaap|cnel|epmaps|cnt|claro|movistar|tuenti|directv|netlife|punto\s*net|megadatos|conecel|otecel|repsol|emelec|empresa\s+el[eé]ctrica)", re.I), "D", "SALIDA_PAGO_SERVICIO"),
    (re.compile(r"\b(megadatos|emaap|emaapq|cnel|epmaps|cnt|directv|netlife|punto\s*net)\b", re.I), "D", "SALIDA_PAGO_SERVICIO"),
    (re.compile(r"pag[\.\-]?\s*cash|\bcash\b", re.I), "D", "SALIDA_PAGO_PROVEEDOR"),
    (re.compile(r"\bpago\s+(a|de)\b|^pag-|intela\s+c[\.\-]pag", re.I), "D", "SALIDA_PAGO_PROVEEDOR"),
    (re.compile(r"transferencia\s+enviada|tr\s+a\s+\w+|^tr-|^trf-", re.I), "D", "SALIDA_TRANSFERENCIA"),
    (re.compile(r"\bcheque\b|\bch\b\s|^ch\.|\bch\s+prot|protesto", re.I), "D", "SALIDA_CHEQUE_EMITIDO"),
    # Códigos de tipo de tx interna del banco (genéricos — tipo pago a proveedor)
    (re.compile(r"^(gs|cc|in|op)\b|^(gs|cc|in|op)-", re.I), "D", "SALIDA_PAGO_PROVEEDOR"),
]


def categorizar(concepto: str, tipo: str) -> Categoria:
    """Heurística regex → Categoria.

    Args:
        concepto: texto del concepto del banco (en MAYÚS o no, da igual).
        tipo: 'C' (crédito = entrada) o 'D' (débito = salida).

    Returns:
        Categoria. Si nada matchea, devuelve un fallback razonable según
        tipo: ENTRADA_OTRO / SALIDA_OTRO / OTRO si tipo desconocido.
    """
    concepto = (concepto or "").strip()
    tipo = (tipo or "").strip().upper()

    for rx, tipo_filtro, codigo in _REGLAS:
        if tipo_filtro and tipo_filtro != tipo:
            continue
        if rx.search(concepto):
            grupo, label, abrev = _label(codigo)
            return Categoria(codigo=codigo, grupo=grupo, label=label, abrev=abrev, confianza=1.0, fuente="regex")

    # Fallback por tipo — asumir lo más común:
    #   Entrada sin patrón = cobro por transferencia (TR), la entrada más común.
    #   Salida sin patrón = pago a proveedor (P), la salida más común.
    # Confianza baja para que la AI lo pueda mejorar si está habilitada.
    if tipo == "C":
        grupo, label, abrev = _label("ENTRADA_COBRO_TRANSFERENCIA")
        return Categoria("ENTRADA_COBRO_TRANSFERENCIA", grupo, label, abrev, confianza=0.4, fuente="tipo-fallback")
    if tipo == "D":
        grupo, label, abrev = _label("SALIDA_PAGO_PROVEEDOR")
        return Categoria("SALIDA_PAGO_PROVEEDOR", grupo, label, abrev, confianza=0.4, fuente="tipo-fallback")
    grupo, label, abrev = _label("OTRO")
    return Categoria("OTRO", grupo, label, abrev, confianza=0.0, fuente="tipo-fallback")


def necesita_ai(cat: Categoria) -> bool:
    """¿Vale la pena pasarlo por AI?

    Solo cuando la regex devolvió 'OTRO' explícito. NO por confianza baja:
    el fallback por tipo (P para salida, TR para entrada) es un default
    razonable que NO queremos que la cache pisotee con un 'OTRO' viejo.
    """
    return cat.codigo in ("ENTRADA_OTRO", "SALIDA_OTRO", "OTRO")
