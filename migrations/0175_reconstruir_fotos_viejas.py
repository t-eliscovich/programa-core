"""RETIRADA — la reemplaza la 0178.

Esta migración reconstruía el detalle de las fotos viejas leyendo `fecha_crea`
de facturas, cheques, caja y posdat. Nunca se corrió en producción, y mejor:
tiene dos problemas que la hacen mentir.

1. 🚨 **`POSDAT`, `DOLARES`, `ACTIVOS` y `RETIROS` se truncaban enteras en cada
   sync del dBase** (`scripts/import_dbf.py`: esas cuatro no tienen
   `delete_where`). Su `fecha_crea` es la hora del SYNC, no la del hecho. Si
   una corrida de sync cayó dentro del rango de fotos, el JOIN por ventana mete
   la tabla POSDAT COMPLETA dentro de una sola foto como "deuda nueva
   cargada" — un millón de dólares inventados en una ventana de cinco minutos.

2. Sólo veía ALTAS. Lo aplicado —una cobranza, una devolución, un cheque
   depositado— no dejaba fecha, así que quedaba afuera justo lo que más se
   quería ver.

La 0178 hace lo mismo pero desde `scintela.mov_doble`, que tiene
`fecha_creacion` como `timestamptz` real, es nativa de Programa Core (ningún
truncate del dBase la toca) y registra los movimientos, no sólo las altas.
"""

from __future__ import annotations


def run(conn) -> None:
    return
