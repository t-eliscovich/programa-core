"""Cartera — aging de facturas vivas + stop automático por mora.

Read-only reporting sobre `scintela.factura`/`scintela.cliente` más una
única acción de escritura (`aplicar_stop_automatico`) que marca
`cliente.stop='S'` a todos los clientes con facturas vencidas más de N días.

No crea documentos dated, por lo tanto NO llama `periodo_guard`.
"""
