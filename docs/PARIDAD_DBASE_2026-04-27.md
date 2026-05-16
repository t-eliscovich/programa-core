# Auditoría de Paridad dBase ↔ Programa Core — 2026-04-27

## Resumen ejecutivo

- **Flujos auditados**: 18 flujos principales
- **Paridad completa**: 14 (78%)
- **Gaps parciales**: 4 (22%)
- **No implementados**: 0
- **Severidad crítica**: 3 gaps (chequera, snapshots historia, depósito lote)

## Gaps priorizados

| # | Flujo | Tipo de gap | Severidad |
|---|---|---|---|
| 1 | Movimientos bancarios cascada (chequera BANCOS.PRG) | Side-effects faltantes | **CRÍTICA** |
| 2 | Snapshots flujo/ahistoria | Tablas de auditoría no usadas | Alta |
| 3 | Depósito en lote de cheques | Automatización faltante | Alta |
| 4 | Recalculo de saldos bancarios | Fix de datos no automático | Media |
| 5 | Anticipos en cheques (CONCEPTO=9999) | Lógica especial faltante | Media |
| 6 | cliente.fecha_ult_compra | Columna inexistente en schema | Media |
| 7 | Movimiento banco→caja en rebote | Side-effect parcial | Baja |
| 8 | Conciliación automática vs manual | Cambio de paradigma | Baja |

## Flujos auditados — detalle

### ✅ Paridad completa (14)

1. **Crear factura nueva** (ALTAS.PRG → `facturas/queries.py::crear`)
2. **Aplicar cheque a factura** (REPLA → `cheques/queries.py::aplicar_a_factura`)
3. **Reversar cheque rebotado** (MODIFICA.PRG → `cheques/queries.py::reversar`)
4. **Crear compra** (ALTAS.PRG → `compras/queries.py::crear`)
5. **Anular factura** (MODIFICA.PRG → `facturas/queries.py::anular`)
6. **Anular compra** (MODIFICA.PRG → `compras/queries.py::anular`)
7. **Emitir retención** (RETENCIO.PRG → `retenciones/queries.py::emitir`)
8. **Anular retención** (RETENCIO.PRG → `retenciones/queries.py::anular`)
9. **Crear posdat manual** (`posdat/queries.py::crear`)
10. **Marcar pagada posdat** (`posdat/queries.py::marcar_pagada`)
11. **Anular posdat** (`posdat/queries.py::anular`)
12. **Editar provisiones** (`provisiones/queries.py::editar`)
13. **Eliminar provisiones** (`provisiones/queries.py::eliminar`)
14. **Crear retiro** (`retiros/queries.py` — implícito)

### ⚠️ Gaps parciales (4)

#### Gap 1 — Crear cheque de cobranza (parity 80%)
- **dBase**: ALTAS.PRG líneas 138-189
- **Nuevo**: `modules/cheques/queries.py::crear`
- **Faltante**: lógica especial de "anticipo" cuando `CONCEPTO=9999` (debería insertar cheque espejo con importe negativo, stat='Z')

#### Gap 2 — Movimientos bancarios (chequera) — el más grande
- **dBase**: BANCOS.PRG líneas 80-310 (PROCEDURE CHEQUERA)
- **Nuevo**: NO existe función centralizada. Cada módulo inserta por separado.
- **Cascada faltante**:
  ```
  CHEQUE (prov, concepto) →
    ├─ PROV='IN' & 'HB' en CONCEPTO  → INSERT retiros (FE/LC) + UPDATE capital + INSERT caja
    ├─ PROV in (TOT/HIL/TIN)         → INSERT compra automática
    ├─ DOC in (ND/DE) & prov='RR'    → INSERT retiros con doc='RR'
    ├─ CONCEPTO='CAJA'               → INSERT caja
    ├─ LEFT(CONCEPTO,4)='INOP'       → INSERT posdat futuro con importe=-IMP
    └─ FECHAD > HOY                  → INSERT posdat + UPDATE STAT='P'
  ```
- **Impacto**: si la contadora carga un cheque de chequera en el nuevo app, falta hacer manualmente todos esos pasos.

#### Gap 3 — Depósito en lote de cheques
- **dBase**: BANCOS.PRG líneas 21-50 (DEPOSITOS)
- **Nuevo**: Solo lecturas en `modules/conciliacion/`. No hay endpoint que dado un grupo de cheques marque cheque.stat='D' + INSERT en transacciones_bancarias en una transacción.
- **Workaround actual**: hacer el depósito manualmente cheque por cheque.

#### Gap 4 — Snapshots de historia/ahistoria
- **dBase**: cierre mensual escribe en HISTORIA.DBF (kg, $, gastos, etc).
- **Nuevo**: la tabla existe pero no se popula desde el app (era una procedure dBase). Puede haber un script en `scripts/` que aún no audité.
- **Impacto**: el chart "Evolución cartera 12 meses" depende de esta tabla; si no se popula, queda con datos viejos.

## Cosas que el agent no pudo resolver

1. **FLUJO / AHISTORIA**: ¿son snapshots manuales o batch nocturno?
2. **XFACTURAS, FAC20-23**: tablas de archivo histórico, el nuevo no las menciona.
3. **Columnas no mapeadas**: `factura.pase`, `cliente.pase`, `cliente.cupo`/`fecha_cupo`, `compra.observacion`.
4. **CLEARINIG (BANCOS.PRG:470)**: operación sin documentación.

## Recomendación de prioridades para cerrar paridad

**Antes de ir a producción real**:
- Gap 2 (chequera) — sin esto la contadora tiene que duplicar trabajo manual
- Gap 3 (depósito lote) — frecuencia diaria, fricción alta
- Gap 4 (snapshots) — sin esto el dashboard del Dueño se queda sin gráfico de evolución

**Fase 2 (nice to have)**:
- Gap 1 (anticipos) — caso especial poco común
- Gap 5-8 — mejoras incrementales

## Conclusión

**78% de paridad** es un buen estado para un proyecto de re-write de un sistema legacy de 30 años. Los gaps restantes son concentrados (no diseminados): la mayoría está en BANCOS.PRG::CHEQUERA, que es históricamente el módulo más espagueti del legacy.

La estrategia correcta es **NO replicar la cascada espagueti del dBase**, sino documentar los flujos de negocio y rediseñarlos con UX moderna en el nuevo app. Por ejemplo, "cargar cheque de chequera" puede ser un wizard de 3 pasos en vez de la cascada implícita por concepto/proveedor del legacy.
