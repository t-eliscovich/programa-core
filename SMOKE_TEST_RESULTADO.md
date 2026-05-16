# Smoke Test UI + Flujo Cobranza — 2026-05-16

## Pantallas verificadas visualmente

| Pantalla | Estado | Notas |
|---|---|---|
| `/` (Menú de operaciones) | ✅ | 6 cards Cobro y pago en 2 filas de 3, secciones Movimientos/Ver bien separadas con divisores. Action bar arriba con todas las operaciones. |
| `/cheques` | ✅ | 3 columnas de fecha (Cargado/A depositar/Depositado), KPI tabs (En cartera/Depositados/Devueltos/Daniela/Postergados/Eliminados) con monto cada uno, headers sortable, N° con fallback `#40`, `#55` en cheques sin no_cheque. |
| `/cheques/depositar-lote` | ✅ | Destino = PICHINCHA chip, fecha hoy 05/16/2026, columnas Cargado/A depositar/Código/Cliente, N° con fallback. 84 de 996 visibles. |
| `/cheques/40` (detalle) | ✅ | 5 cards: Importe/Cargado/A depositar/Depositado/Banco emisor. Estado "Depositado (Pichincha)". Botones Reversar, Cambiar estado, Editar, Imprimir. **Mini bug:** browser tab title dice "Cheque None" (browser tab, no h1) — `<title>` no usa el fallback `#id`. |
| `/cheques/nuevo` | ✅ | Cobranza active en chip top, cliente input funcional, banco emisor auto-pre-load, 6 facturas abiertas de BED visibles con botón T y badge "ajuste/crédito" en la fila amarilla. |
| `/posdat` | ✅ | **Botón "+ Nuevo pasivo" visible** (fix G funciona). Total $2.132.818 con puntos de miles. N° con fallback `#7, #1, #111, #2`. Editar/Pagar botones en cada fila. |
| `/facturas` | ✅ | KPIs $5.283.061 cartera, $6.129.102 facturado, $612.835 canceladas. Primera fila `#1` (sin numf, con fallback). Tabs Todas/Cartera/Canceladas/Eliminadas con montos. |
| `/historial` | ✅ | KPIs 6617 movs/6585 activos/29 reversados. DESTINO muestra "Pichincha" (nombre real). Operación compuesta (batch) en violeta con "reversar todo el batch". Tipo sin subtitle extra. |
| `/stock` | ✅ | **Todos los puntos de miles correctos**: VALOR TOTAL US$ 8.046.022, Hilado 2.764.214 kg US$ 7.292.027, Tejido 957.448 kg US$ 491.242, Químicos US$ 262.753, Total con químicos 8.046.022. |

## Flujo cobranza — TEST FUNCIONAL ✅

Pasos ejecutados sobre `/cheques/nuevo`:

1. Cliente: `BED` → banco emisor pre-cargado automáticamente con "90 · DEP.PICH." (porque BED tiene cheques previos en ese banco). ✅
2. Aparecen las **6 facturas abiertas de BED**: 2.204,67 / 5.292,33 / 1.502,50 / -296,15 (crédito en amarillo) / 2.908,57 / 392,84. ✅
3. Importe del cheque: `2204.67` → tipiado en el campo. ✅
4. Click en botón **T** de la primera fila ($2.204,67).
5. **Resultado:**
   - Checkbox de esa factura: **marcado automáticamente** ✅
   - A APLICAR: **2204.67** ✅
   - Badge "⮕ MATCH" aparece al lado del N° (importe del cheque == saldo de la factura) ✅
   - Header summary: Total $2.204,67 · Aplicado $2.204,67 · Restante $0,00 · **Plazo: 87 días** ✅
   - ACUM. en todas las filas: $2.204,67 ✅

El flow completo de cobranza funciona como se espera. Falta solo guardar para confirmar el commit, pero por ser test no submit-eo (no contaminar la base con el cheque de prueba).

## Bug menor encontrado y arreglado durante el smoke test

**`/cheques/nuevo` — N° de factura mostraba "—"** cuando `numf=0` (facturas de BED, VPM etc. sin número formal).

**Fix aplicado:** `modules/cheques/templates/cheques/nuevo.html:407` — ahora muestra `numf_completo` > `numf` > `#{id_factura}` en gris (mismo patrón que `/facturas/lista.html`).

## Conclusión

Todas las pantallas que toqué en esta sesión se ven correctamente y el flujo crítico de cobranza (T button + match detection + plazo calculation) está operativo. Único pendiente menor: el `<title>` de la página de detalle de cheque para cheques sin `no_cheque` (dice "None" en el tab del browser).
