# Skill addendum — batch 18 (2026-04-29)

**Vocabulario canónico de estados — del dueño.**

Esta es la fuente de verdad sobre los códigos de estado para cheques,
facturas, compras y caja. Cualquier sesión futura que toque estos módulos
se referencia a esta sección. Si el código no coincide, **el código está
mal**. Notas previas que contradigan esto están deprecadas — esta sección
las pisa. Cuando se haga el merge a `SKILL.md`, esta sección reemplaza
cualquier descripción anterior de los stat codes.

---

## Cheques — `scintela.cheque.stat`

| Stat | Significado | Reglas de transición |
|------|-------------|----------------------|
| `Z` | **Cartera** — ingresado, no pasó nada todavía | Estado inicial al crear el cheque. Único origen permitido para `P` (postergar). |
| `B` | **Depositado en banco Pichincha** | Sólo desde `Z`. Estado terminal feliz: una vez depositado en B, **nada puede cambiar** salvo rebote (1, 2 o 3). |
| `V` | **Banco Internacional (legacy)** | NO USAR. Histórico únicamente. Si aparece un V nuevo es un bug. |
| `1` | **Devuelto / rechazado #1** | Sólo desde `B`. Cheque que se depositó y volvió rechazado. |
| `2` | **Devuelto / rechazado #2** (alias de 1) | Sólo desde `B`. Mismo significado que 1, distinto código por compatibilidad histórica. |
| `3` | **Segundo rechazo** | Sólo desde `1` (cuando un cheque ya rechazado se redepositó y volvió a rebotar). |
| `D` | **Daniela** | Cheques en gestión de cobranza con Daniela. Origen Z. No es estado terminal. |
| `P` | **Postergado** — nueva fecha | Sólo desde `Z` (los que están en cartera se pueden postergar). Requiere `fechad` nueva > fecha original. |

### Invariantes críticos

1. Estado inicial al alta = `Z`. Nunca otro.
2. `B` es estado terminal feliz. Una vez allí, sólo puede pasar a 1/2/3 (rebote).
3. **Cheque sólo se puede depositar si previamente estaba en `Z`.** Cualquier otro stat origen al depositar es un bug — la UI tiene que rechazarlo y la query también.
4. Postergar (`P`) requiere stat origen = `Z`. Nunca de B/D/1/2/3/V.
5. La transición `1 → 3` representa "se rechazó, lo redepositamos, volvió a rechazar". El segundo redepósito conceptualmente reabre B otra vez antes de rebotar — pero a efectos del state machine, basta con `1 → 3`.
6. `V` está prohibido para cheques nuevos. Migración: dejar los históricos como están, pero la UI de creación no expone `V` y las transiciones no lo permiten como destino.

### `fecha_recibido` (nuevo, columna a agregar)

- Hoy `scintela.cheque` tiene `fecha` (fecha del cheque, escrita en el papel) y `fechad` (fecha a depositar — para postdatados puede ser > fecha).
- Falta una `fecha_recibido` explícita: cuándo físicamente recibimos el cheque. Puede ser igual que `fechad` o anterior.
- Migración 0013 agrega `scintela.cheque.fecha_recibido date`. Backfill: `COALESCE(fecha_crea::date, fecha)` para filas históricas.
- En el form de alta, el default visible es `fecha_recibido = HOY` (no fecha del cheque).

### Mapeo viejo → nuevo (deprecaciones)

| Antes | Ahora | Notas |
|-------|-------|-------|
| `D` = depositado | `B` = depositado en Pichincha | Ojo: el legacy usaba D para depositado genérico. Ahora D es Daniela. |
| `1` `2` `3` = depositado banco 1/2/3 | `1` `2` `3` = devueltos | Renombrado de "banco N" a "rechazo N" |
| `A` = acreditado | (no aplica) | El stat `A` ahora pertenece a facturas. Cheques no usan A. |
| `R` = rebotado | (deprecado) | Reemplazado por `1/2/3` que son devueltos específicos |

**Migración de datos legacy:** las filas existentes con `stat='D'` (depositado en el sistema viejo) se mapean a `B` (depositado en Pichincha) sólo si se confirma que `no_banco=1` (Pichincha). Las filas con `stat='A'` quedan como están — son acreditaciones legacy y no se re-clasifican.

---

## Facturas — `scintela.factura.stat`

| Stat | Significado | Cómo entra |
|------|-------------|------------|
| `Z` | **Emitida**, sin abono todavía | Estado inicial al crear factura. |
| `A` | **Abonada** parcialmente (saldo > 0) | Cuando se aplica un cheque/cobro pero queda saldo. |
| `T` | **Cancelada por el total** | Cuando saldo llega a 0 por cobros. Estado terminal feliz. |
| `X` | **Eliminada por error** (me equivoqué) | Anulación administrativa. NO es la "anulación SRI" — es el botón "me equivoqué cargándola". |

**Cartera de facturas** = `Z + A`. Es decir: facturas con saldo vivo. Excluye `T` (cobrada total) y `X` (eliminada).

### Mapeo viejo → nuevo (deprecaciones)

- Notas previas en SKILL.md decían `stat='Y'` = anulada. Eso se renombra a `X` (eliminación por error). En código, reemplazar `'Y'` por `'X'` en filtros y en la acción anular. Si encontrás un `'Y'` históricamente cargado en producción, dejalo — son históricos pre-rename. Pero todo escritura nueva usa `X`.
- Notas previas decían `stat='A'` es "abierta inicial". Lo nuevo es `Z` para emitida. `A` queda reservado para "abonada parcial" (con cheque aplicado). Renombrar el default en `crear()` de `'A'` a `'Z'`.

### Transiciones automáticas al aplicar cheques

- Factura nueva: `stat = 'Z'`, abono=0, saldo=importe.
- Cheque aplicado, saldo aún > 0: `stat = 'A'`.
- Cheque aplicado, saldo llega a 0: `stat = 'T'`.
- Reverso de cheque (rebote, des-aplicación): el `stat` se recalcula igual: si abono > 0 y saldo > 0 → `A`, si saldo = importe (todo de vuelta) → `Z`, si saldo = 0 → `T`.
- Anulación administrativa: `stat = 'X'`. Sólo si NO hay chequesxfact aplicados y NO hay retención emitida.

---

## Compras vs Producción — `scintela.compra.tipo` + `compra.kg`

La tabla `scintela.compra` mezcla cosas distintas. La regla de discriminación es:

| Tipo | Significado | Tiene `kg`? |
|------|-------------|-------------|
| `K` | **Tejeduría** (compra de servicio) — sin kg = compra; con kg = **producción** | Variable — define el subtipo |
| `H` | **Hilado** | Sí (kg de hilado) |
| `Q` | **Químicos** | No (litros / unidades, no kg de fabric) |
| `C` | **Cosas de fábrica** (aceite, repuestos, consumibles) | No |
| `A` | **Anticipo** (a proveedor, sin factura todavía) — luego se convierte | No |

### Reglas

1. **`K` con kg > 0 = producción.** `K` con kg = 0 (o NULL) = compra de tejeduría.
2. La vista lista de compras se divide en dos pestañas / filtros: **Compras** (tipos H, Q, C, K-sin-kg) y **Producción** (tipo K con kg).
3. Anticipos (tipo `A`) tienen una acción **"Convertir a compra"** que pide el tipo destino (K/H/Q/C) y, si destino es K, los kg.
4. El tipo es validado contra el set `{'K','H','Q','C','A'}`. Cualquier otra cosa se rechaza al alta.

---

## Caja — entradas y salidas explícitas

Hoy `caja.tipo` tiene 'I' (ingreso), 'E' (egreso), 'CB' (cobro con cheque). El módulo no expone esto bien en la UI:

- El listado tiene un solo botón "+ Nuevo" — debe haber dos: **"+ Entrada"** y **"+ Salida"**.
- El form prefija `tipo` según el botón clickeado.
- KPIs en la lista: `Entradas del periodo` y `Salidas del periodo` separadas, no sólo un saldo total.
- Las salidas a caja son egresos típicos (pago menor, propinas, viáticos). Las entradas a caja son cobros chicos en efectivo.

---

## Cartera vs Cheques — separación estricta

Hoy en el sidebar y en `informes.cartera` se mezclan. El gerente quiere claridad:

- **Cartera** = solo facturas con saldo vivo (Z + A). Reporte de "qué nos deben los clientes en facturas".
- **Cheques** = sección propia con sus pestañas por estado (Cartera de cheques = Z, Depositados = B, Devueltos = 1+2+3, Daniela = D, Postergados = P).
- **Cobranzas** (puente) = la combinación de los dos para pronóstico. Ese sí mezcla — pero etiquetado.

En el sidebar: "Cartera" es un grupo en Informes con sub-items por bucket (aging, por grupo, etc.). "Cheques" vive en Operaciones — no en Informes. No mezclar.

---

## Activos fijos — orden por categoría

Hoy se ordenan por fecha DESC. El gerente quiere lectura por categoría:

```
1. Terrenos y propiedades   (tipo IN ('TER','TERRENO','PRED','PROP') o concepto contiene 'terreno'/'predio'/'lote')
2. Maquinaria              (tipo IN ('MAQ','MAQUINARIA') o concepto contiene 'máquina'/'telar'/'tinte')
3. Vehículos               (tipo IN ('VEH','VEHICULO','AUTO','CAMION'))
4. Equipo de oficina       (tipo IN ('OFI','OFICINA','COMP','COMPUTO'))
5. Otros                   (todo lo demás)
```

Implementación: una columna derivada `categoria_orden` con un `CASE` y `ORDER BY categoria_orden ASC, fecha DESC, id DESC`. La lista renderiza con headers por categoría (group rows).

---

## Arquitectura de información del sidebar

**Problema (verbatim del dueño):** "El programa tiene que ser limpio y eficiente. No quiero ver clientes y proveedores por todos lados. Deberíamos tener un solo index que diga 'Bases de datos' y ahí se pueda ver clientes, proveedores, etc. El FLUJO es lo más importante, está escondido entre 10000 pestañas."

### Solución (sidebar reordenado, top → bottom)

1. **Tablero** — dashboard.
2. **Flujo** — link directo, top-level (no enterrado en Informes). Es la pantalla más vista del gerente.
3. **Bases de datos** (grupo nuevo, una sola entrada para datos maestros)
   - Clientes
   - Proveedores
   - Productos
   - Bancos
   - Directorio de contactos
4. **Operaciones** (transacciones del día a día — antes "Gestión")
   - Facturas
   - Cheques
   - Retenciones
   - Compras y producción
   - Caja
   - Posdatados
   - Conciliación
   - Emitir cheque
   - Proformas
5. **Cartera** (grupo propio — antes mezclado en Informes)
   - Cartera (saldos por cliente)
   - Aging
   - Por grupo
   - Calendario cobranzas
   - Cobros recibidos
   - Estado de cuenta
6. **Contabilidad**
   - Provisiones
   - Capital
   - Activos fijos
   - Retiros
   - Gastos
7. **Informes** (gerenciales, no operacionales)
   - Resultados (balance)
   - Ventas
   - Deudas
   - Historia
   - Metas
8. **Administración** (Bitácora, Periodos, Usuarios)

### Razones

- **Flujo top-level**: es el primer lugar al que el gerente entra cada mañana. No puede vivir bajo Informes > [click expand] > Flujo.
- **"Bases de datos"**: clientes/proveedores/productos/bancos son datos maestros, no transacciones. Mezclarlos con facturas/cheques (transacciones) confunde el modelo mental. Un solo grupo que diga "acá están los listados de entidades".
- **"Cartera" grupo propio**: es lo que el gerente lee todos los días con los cobranceros. Merece su propio nodo, no enterrado entre 14 sub-items de "Informes".
- **"Operaciones" en lugar de "Gestión"**: nombre más preciso (lo que hay ahí son las transacciones operacionales del día).

---

## Plan de implementación (orden recomendado para no romper nada)

1. **Migración 0013** — `cheque.fecha_recibido`, comentarios `COMMENT ON COLUMN cheque.stat`/`factura.stat` con el vocabulario canónico, y datos seed corregidos si hace falta. Idempotente.
2. **Cheques refactor** — `STATS` dict, transiciones en `crear/depositar/reversar/postergar`, validación de stat origen.
3. **Facturas refactor** — renombrar 'Y' → 'X' en `anular`, default `crear` → 'Z' (no 'A'), `apply_to_factura` flujo Z→A→T.
4. **Compras tipo dropdown + producción split** — validación `{K,H,Q,C,A}`, lista con tabs, acción convertir-anticipo.
5. **Activos orden** — `CASE` por tipo, headers en template.
6. **Caja entradas/salidas** — botones explícitos + KPIs separados.
7. **Sidebar IA** — top-level Flujo, "Bases de datos", "Cartera" grupo propio, "Operaciones" en lugar de "Gestión".
8. **Test pass** — ruff + pytest + smoke. Update tests si se rompen por las transiciones.

---

## Tests de transición que hay que tocar

`tests/test_cheques_reversar.py` — el "rebote real" que dispara stop automático estaba definido como `stat_previo IN ('D','A')`. Ahora el rebote real es `stat_previo = 'B'` (que se va a 1/2) o `stat_previo = '1'` (que se va a 3). `D` deja de ser "depositado" y pasa a ser "Daniela" (no rebote). `A` ya no aplica a cheques — `A` ahora es factura abonada.

Ajuste: en `cheques.queries.reversar()`, la condición `es_rebote_real = stat_prev in ('B', '1')`. El test correspondiente cambia de `('D','A')` a `('B','1')`.

---

## Verificación final (cierre del batch)

- `ruff check .` en los módulos tocados (cheques, facturas, compras, caja, activos, informes, templates) — **clean**.
- `pytest -q` — **390 passed, 9 skipped, 0 failed**. (Los 9 skipped son tests de integración que requieren Postgres en CI, no aplica al sandbox.)
- `tests/test_routes_smoke.py` — **69 GET routes walked, 0 failures**.
- Tests de transición de cheques (`test_cheques_reversar.py`) reescritos a la nueva máquina de estados — **11/11 pass**.
- Tests de depósito en lote (`test_cheques_depositar_lote.py`) actualizados a stat='B' — **6/6 pass**.

## Archivos tocados en este batch

```
migrations/0013_estados_canonicos.sql                                NUEVO
modules/cheques/queries.py                                           refactor
modules/cheques/views.py                                             + fecha_recibido en form
modules/cheques/templates/cheques/detalle.html                       badges nuevo vocabulario
modules/cheques/templates/cheques/depositar_lote.html                stat='B' en docstring
modules/cheques/templates/cheques/nuevo.html                         + campo fecha_recibido
modules/facturas/queries.py                                          refactor (Z/A/T/X + STATS_VIVOS)
modules/facturas/templates/facturas/detalle.html                     badges nuevo vocabulario
modules/compras/queries.py                                           + TIPOS_VALIDOS, convertir_anticipo, vista filter
modules/compras/views.py                                             + convertir_anticipo route, vista param
modules/compras/templates/compras/lista.html                         + tabs (todas/compras/produccion/anticipos)
modules/compras/templates/compras/nueva.html                         tipo dropdown K/H/Q/C/A
modules/caja/views.py                                                ?tipo=I|E prefija el form
modules/caja/templates/caja/lista.html                               + botones "+ Entrada" y "+ Salida"
modules/activos/queries.py                                           + categoria_orden, ordenamiento canónico
modules/activos/templates/activos/lista.html                         + headers de sección por categoría
modules/informes/queries.py                                          totc() restringido a Z/D/P (no rebotados)
templates/base.html                                                  arquitectura del sidebar reordenada
tests/test_cheques_reversar.py                                       reescrito a nueva máquina B/1/2/3/D/X
tests/test_cheques_depositar_lote.py                                 actualizado stat 'D' → 'B'
docs/SKILL_ADDENDUM_BATCH_18.md                                      ESTE DOCUMENTO
```

## Pendiente (deploy)

Antes del próximo ship a producción:

1. Aplicar la migración 0013 en RDS — agrega `cheque.fecha_recibido`, remapea legacy `stat='D'` → `'B'`, y añade los `COMMENT ON COLUMN` canónicos.
   ```
   python scripts/migrate.py
   ```
   O manual: `psql $DATABASE_URL -f migrations/0013_estados_canonicos.sql`.

2. Backfill manual opcional: si hay filas históricas con `stat='A'` que deban remapearse a `B` (acreditadas legacy), evaluarlo caso por caso. La migración 0013 NO lo hace automáticamente porque la semántica histórica de `A` (acreditado vs depositado feliz) puede ser ambigua.

3. Verificar que los reportes que dependen de `totc()` (cheques en cartera) reflejen el nuevo número — el filtro pasó de incluir `1/2/3/B` legacy a sólo `Z/D/P`. Es probable que el total baje porque ahora los rebotados y depositados quedan fuera de "cartera de cheques".

## Lo que NO se cambia

- `xfactura` sigue siendo histórico read-only.
- `bitacora_acciones` sigue capturando todo write.
- Los permisos roles existentes no cambian — `cheques.crear/anular/aplicar` siguen como están.
- La integración SRI sigue siendo proyecto aparte.
- `posdat.banc=9` sigue significando cerrada.
- `seguridad.*` y `migraciones_aplicadas` no se tocan.
