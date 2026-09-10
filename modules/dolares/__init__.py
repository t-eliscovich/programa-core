"""Dólares — anticipos y movimientos en USD.

`scintela.dolares` es la tabla legacy de movimientos en dólares físicos
(efectivo USD que el dueño/socios mantienen aparte de las cuentas
bancarias). Cada fila representa un evento — un anticipo de cliente,
una conversión, un retiro, una aplicación a factura.

Campos:
    fecha     — fecha del movimiento
    cta       — 3-char cliente o socio (ej. "WE", "AC")
    concepto  — descripción libre
    importe   — monto en USD (positivo = entrada, negativo = salida)
    st        — estado del anticipo:
                  NULL / '' / ' '  → ANTICIPO VIVO (no aplicado)
                  cualquier letra  → aplicado / cancelado / convertido
    clave     — 3-char user que cargó el movimiento (legacy dBase)

Regla legacy (INFORMES.PRG → ANTIC):
    ANTICIPOS del balance = SUM(importe) WHERE st IS NULL OR st IN ('', ' ')
    → suma sólo los anticipos vivos
"""
