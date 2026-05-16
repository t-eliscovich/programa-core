# Tests E2E reales — 2026-05-16 (COMPLETO)

14 flujos testeados end-to-end contra Chrome + DB real. **5 bugs encontrados, 2 ya arreglados en código.**

## Resumen final

| # | Flujo | Funcional | Bug |
|---|---|---|---|
| 1 | Cobranza cheque→factura | ✅ | A (tracking) |
| 2 | Gasto pagado | ⚠️ | **B (caja no baja)** |
| 3 | Aporte capital | ✅ | — |
| 4 | Retiro socio | ✅ | — |
| 5 | Caja entrada | ✅ | — |
| 6 | Transferencia bancos | ❌ | **C (confirm pierde data)** |
| 7 | Depósito cheques lote | ✅ | — |
| 8 | Posdat manual | ✅ | — |
| 9 | Cheque emitido propio | ✅ | — |
| 10 | Factura nueva | ❌ | **D (FIXED — restart)** |
| 11 | Anular factura | ❌ | **E (FIXED — restart)** |
| 12 | Compra a proveedor | ✅ | — |
| 13 | Anular compra | ✅ | — |
| 14 | Endoso cheque a proveedor | ✅ | — |

**9/14 funcionan limpio. 1 con bug menor (A). 4 con bugs reales (B, C, D, E).**

---

## BUGS — los 5

### A — Tracking de reverso (tracking, no funcional)
Los `mov_doble` originales quedan `estado='activo'` después del reverso. El sistema funciona OK pero rompe trazabilidad histórico→reverso. Mismo bug del Audit C (26 movs así).

### B — Gasto pagado NO descuenta caja (CRÍTICO)
`/gastos/nuevo` con pagado=1 → xgast creado, mov_doble `gasto_simple` activo, **pero caja no baja**. Al reversar, caja **recibe** los $5 → asimétrico, genera plata de la nada.

### C — Transferencia confirm pierde datos (CRÍTICO)
Pantalla de confirmación muestra montos OK, al confirmar vuelve al form vacío con "Elegí banco origen", etc. Flujo de transferencia bancaria roto desde la UI.

### D — Factura crash `pago='C'` *(FIXED)*
3545 clientes legacy DBF tienen `pago='C'` (no numérico) y el código hacía `int(row['pago'])` que crasheaba con `invalid literal for int() with base 10: 'C'`. **Fix aplicado**: parsea como entero solo si es numérico, fallback 30 días.

### E — Factura anular columna `observacion` inexistente *(FIXED)*
`facturas/queries.py:413` UPDATE `observacion = ...` pero la tabla `scintela.factura` no tiene esa columna. **Fix aplicado**: removido el SET observacion del UPDATE. El motivo igual queda en bitácora vía `registrar_bitacora()`.

---

## Cosas que funcionan PERFECTO

1. **Cobranza con MATCH y plazo** — botón Max + factura ticked automático + plazo días calculado.
2. **Reverso de cheque** — factura vuelve a Z saldo original, cheque queda X. Solo Bug A de tracking.
3. **Aporte/Retiro con side-effect Pichincha** — flash explícita "Side effect: pichincha", Pichincha sube/baja correcto.
4. **Caja simple** — entrada/salida con botón Reversar inline.
5. **Depósito cheques en lote** — Pichincha sumó $3.457,29 exacto, generó boleta PDF con todos los cheques del día.
6. **Posdat manual** — total subió $10, partidas 149→150.
7. **Cheque emitido propio (chequera)** — Pichincha bajó $1, side-effect "ninguno" (tipo="otro").
8. **Compra + Anular compra** — total compras 2.028.688 → 2.028.673 (-$15) tras anular, posdat asociada se borra.
9. **Endoso cheque** — cheque #1399 MARIA YUGCHA $4.555,02 endosado a QC, auto-creó compra #100000 por mismo importe.

---

## Para verificar bugs fix D y E

Necesitás **reiniciar waitress** para que los cambios en `queries.py` tomen efecto. Lo que necesitás correr:

```bash
pkill -f waitress && ./launcher.sh
```

Después reabrí en Chrome y vamos a re-correr:
1. `/facturas/nueva` con cliente BIP → debería pasar (Bug D)
2. `/facturas/<id>/confirmar-anulacion` con factura limpia → debería pasar (Bug E)

Si verifico esos, me queda arreglar Bug A, B, C que son cambios más complejos.

---

## Datos de prueba en la DB

Resumen de cambios reales que dejaron los tests:
- **Cheque TEST001** id=2001 (stat=X reversado) ✓
- **Cheque #1399 MYU** endosado a QC (stat=E ahora) — ⚠️ no reverso este, deberías validar manualmente
- **Compra #100000** auto-creada del endoso, prov=QC, $4.555,02
- **Pichincha**: +$3.456,29 vs baseline (+$100 aporte -$100 retiro +$3.457,29 depósito -$1 cheque emitido)
- **Caja**: +$55 vs baseline (entrada $50 test + reverso gasto $5)
- **Posdat**: +1 partida $10 QC abierta (TEST E2E posdat)
- **xgast #306** anulado (gasto $5 test)
- **2 cheques** pasaron a depositados: #441 IEH $1.157,29 + #554 EEU $2.300,00
- **~15 mov_doble nuevos** del 16/05

Cuando confirmes, hago un script de cleanup que reverse todo via UI (sin DELETE directo).

---

## Próximos pasos

1. **Restart waitress** y verificar fix D + E
2. **Fix Bug A** — agregar `id_original` + UPDATE estado='reversado' en cada reverso del `_REVERSO_DISPATCH`
3. **Fix Bug B** — en `/gastos/nuevo` con `pagado=1`, crear mov_doble `caja_s_to_xgast` que descuente caja
4. **Fix Bug C** — debug del flujo confirm de transferencia, asegurar hidden inputs se preserven
5. **Cleanup** de los datos de prueba
