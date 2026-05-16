"""Posdat — pasivos abiertos con proveedores (pagos pos-datados).

scintela.posdat representa la deuda viva con proveedores, equivalente a
"cuentas por pagar" en un ERP. Cada fila es un compromiso de pago.

Campos clave:
    num        — correlativo del pago
    fecha      — fecha del asiento
    fechad     — fecha de vencimiento del pago
    prov       — código del proveedor
    importe    — monto a pagar
    banc       — 0 = abierta, 9 = pagada/cerrada, otro número = pagada con banco X
    concepto   — descripción
    compr / no_comp — comprobante de referencia
    tipo       — tipo de comprobante (CM/FC/NV/…)

Regla legacy crítica (INFORMES.PRG):
    TOTP = SUM posdat WHERE banc <> 9
    → deuda viva = cualquier fila con banc != 9
"""

# --- SQL fragments reutilizables --------------------------------------------
# Fuente: SKILL.md sección "Convención de posdat.banc".
#
# POSDAT_DEUDA_VIVA_WHERE — para PASIVO en balance / cuentas por pagar:
#   sólo banc=0 (deuda viva, no instrumentada). banc=9 ya está
#   "comprometido" via cheque emitido, NO double-counta.
#
# POSDAT_EGRESO_FLUJO_WHERE — para /informes/flujo (egresos proyectados):
#   incluye banc=9 (cheques posdatados legacy emitidos en dBase, todavía
#   no clearearon en transacciones_bancarias). Mirror exacto MENU.PRG
#   líneas 683-684: CASE BANC=9 .OR. BANC=0.
POSDAT_DEUDA_VIVA_WHERE = "COALESCE(banc, 0) = 0"
POSDAT_EGRESO_FLUJO_WHERE = "COALESCE(banc, 0) IN (0, 9)"


def posdat_deuda_viva_where(alias: str = "") -> str:
    """Devuelve el WHERE fragment con un alias de tabla opcional.

    Útil cuando hay JOIN y necesitás cualificar la columna:

        WHERE {posdat_deuda_viva_where('pd')}
            AND ...

    Si `alias` es vacío, devuelve la forma sin prefijo. Evita el patrón
    frágil de `POSDAT_DEUDA_VIVA_WHERE.replace('banc', 'pd.banc')` que
    rompería silenciosamente si la constante se ampliara y contuviera
    palabras como "banco" o "bancario".

    Bug #R3 audit 2026-05-14.
    """
    if not alias:
        return POSDAT_DEUDA_VIVA_WHERE
    return f"COALESCE({alias}.banc, 0) = 0"


def posdat_egreso_flujo_where(alias: str = "") -> str:
    """Como arriba pero para el filtro del flujo (banc IN 0,9)."""
    if not alias:
        return POSDAT_EGRESO_FLUJO_WHERE
    return f"COALESCE({alias}.banc, 0) IN (0, 9)"
