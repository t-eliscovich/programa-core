"""Conciliación bancaria + detección automática de cheques rechazados.

Ingesta estado de cuenta en CSV (Pichincha / Internacional) y compara contra
los cheques depositados. Cheques sin match en un plazo > N días aparecen
como "sospecha de rechazo" en una bandeja que la contadora confirma con un
click, disparando `cheques.reversar()` que ya pasa el cliente a STOP.

El parser es tolerante a los quirks típicos de los CSVs de banco: encoding
CP1252/UTF-8, BOM, separador `;` o `,`, fechas DD/MM/YYYY, montos con coma
decimal.
"""
