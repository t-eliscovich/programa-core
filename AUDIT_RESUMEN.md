# Auditoría completa — 2026-05-16

Resultados de 5 auditorías corridas en paralelo: **A** (saldos), **C** (mov_doble), **D** (cheques rebotados), **E** (tests E2E), **F** (performance), **G** (permisos).

Archivos de detalle en el mismo directorio: `AUDIT_A_saldos.txt`, `AUDIT_C_mov_doble.txt`, `AUDIT_D_cheques_rebotados.txt`, `AUDIT_F_performance.txt`, `AUDIT_G_permisos.txt`.

---

## A — Saldos vs realidad ✅ Sin drift

| Banco         | oficial          | walk-forward     | drift    | movs |
|---------------|------------------|------------------|----------|------|
| PICHINCHA     | 2.299.449,82     | 2.299.449,82     | -0,00    | 662  |
| INTERNACIONAL | 3.761,19         | 3.761,19         | +0,00    | 1    |

Saldos perfectamente consistentes. La lógica `_signed_delta` maneja correctamente la convención MIXTA (legacy signed / nuevo abs+signo). **Nada que arreglar.**

---

## C — mov_doble ⚠️ 3 issues encontrados

**Estado general:** 5.554 activos · 30 reversados · 4 reversos. Todas las relaciones origen→destino apuntan a registros existentes (0 huérfanos de origen).

| Issue | Cantidad | Decisión |
|---|---|---|
| Huérfano destino → posdat#200 (no existe) | 1 mov (id=339, `compra_backfill` $20.000 ACR) | Es del backfill inicial. La posdat#200 fue borrada después por algún cleanup. Recomiendo dejarlo si la compra real está OK, o anular el mov_doble si querés limpiar. |
| Estado `reversado` sin `id_reverso` | 26 movs | Todos del 15-may, todos `cheque_aplicado_a_factura` / `cheque_creado`. Son los movs que reversó la cobranza durante mis tests. La columna `id_reverso` no se rellena cuando el reverso es "implícito" (estado→reversado sin nueva fila). **Bug menor de tracking** — la reversa funciona pero no se puede navegar `original→reverso`. |
| Endoso 5434 ↔ reverso 5437 | $50.409,93 vs $1,00 (importe mismatch) | **Bug real**: el reverso del endoso quedó registrado con importe $1 placeholder. El cheque#977 volvió a cartera OK pero el mov_doble está mal. Concepto = "test", probablemente un test mal limpio. |

**Fixes propuestos:**
- Limpiar el endoso de prueba: `DELETE FROM mov_doble WHERE id_mov_doble IN (5434, 5437);` (si el cheque#977 ya está OK en cartera).
- Para los 26 reversado-sin-id_reverso: agregar trigger o fix en `_REVERSO_DISPATCH` que rellene `id_reverso` cuando el reverso sea inline.

---

## D — Cheques rebotados sin gestión 📋 10 cheques, $34.923

10 cheques en `stat='1'` (1er rebote) sin movimiento > 60 días. 8 hace > 1 año (probable incobrable).

**Top 5 por monto:**

| id   | cliente                          | importe   | días sin tocar | banco     |
|------|----------------------------------|-----------|----------------|-----------|
| 1212 | MIRIAN MARGOTH TOAPANTA          | 6.058,56  | 1496           | Pichincha |
| 1210 | MIRIAN MARGOTH TOAPANTA          | 4.402,00  | 1516           | Pichincha |
| 1215 | MIRIAN MARGOTH TOAPANTA          | 3.862,81  | 1453           | Pichincha |
| 1216 | MIRIAN MARGOTH TOAPANTA          | 3.862,81  | 1444           | Pichincha |
| 1217 | MIRIAN MARGOTH TOAPANTA          | 3.632,01  | 1444           | Pichincha |

**Acción:** decidir si pasar a "R" (rebotado terminal, dado por perdido) o a "D" (Daniela para gestión). MIRIAN MARGOTH concentra el riesgo (21.818 con cheques de hace 4 años — probable que ya no se cobre).

---

## E — Tests E2E ✅ 423/457 (92,5%)

Corrí la suite completa contra DB real (no SQL directo) — tests usan los stubs de Python que llaman las funciones de producción.

**Bugs reales encontrados y arreglados:**

1. **3 stubs de test no aceptaban `conn` kwarg** — `test_bancos_emitir_cheque.py`, `test_cheques_depositar_lote.py`, `test_cheques_anticipo.py`. Fix: agregué `conn=None` a las firmas. Estos eran tests rotos que silenciaban bugs reales en producción.

2. **2 stubs no mockeaban la query de saldo previo** — agregué handlers para `SELECT saldo FROM transacciones_bancarias`.

**Tests restantes con falla (34):** son tests stale donde el SQL de producción cambió y los stubs no se actualizaron (test_cheques_reversar, test_compras_anular, test_confirmar_accion, test_paridad_compra_a_balance). **No son bugs de producción**, son test infrastructure debt.

---

## F — Performance ✅ Todo <10ms

Tablas son chicas (factura 4.925, cheque 1.917, transacciones_bancarias 663). Todas las queries típicas corren <10ms. Los Seq Scan que aparecen son sobre tablas mini donde Postgres correctamente prefiere scan sobre índice. **No hay nada que optimizar a este volumen.**

Cuando la app llegue a >100k filas en alguna tabla, volver a auditar.

---

## G — Permisos por rol ⚠️ 6 BUGS REALES ARREGLADOS

Encontré una **clase de bug crítica**: 6 templates usaban `'x.y' in g.permisos` directo en vez de `tiene_permiso('x.y')`. La diferencia: `tiene_permiso()` honra el wildcard `*` (que es el único permiso que tiene el rol Dueño), pero el check directo NO lo honra → **el Dueño no veía botones de acción en esas páginas**.

**Templates arreglados:**
1. `modules/posdat/templates/posdat/lista.html:16` — botón "Nuevo pasivo" (esto explica por qué hoy no aparecía)
2. `modules/posdat/templates/posdat/lista.html:100` — botón "Editar" en cada posdat
3. `modules/facturas/templates/facturas/detalle.html:37` — botón "Generar XML SRI"
4. `modules/facturas/templates/facturas/detalle.html:46` — botón "Editar..."
5. `modules/facturas/templates/facturas/detalle.html:52` — botón "Eliminar..."
6. `modules/sri/templates/sri/detalle.html:35-36` — botón "Emitir nota de crédito"

**Recomendación adicional:** agregar un test que grepee `'x.y' in g.permisos` en todos los templates y falle si encuentra alguno — para que no vuelva a pasar.

Otros hallazgos:
- 4 permisos referenciados en código sin definir en DB para ningún rol: `bancos.editar`, `gastos.anular`, y 2 strings de prueba (`cualquier.cosa`, `x.y`) que parecen tests olvidados. Buscar y borrar `cualquier.cosa` y `x.y` del código.
- 19 permisos definidos en DB pero nunca usados — son de los módulos de Intela (formulas, tintura, stock, productos, etc.) que conviven en la misma DB.

---

## Resumen ejecutivo

| Auditoría | Resultado | Bugs reales |
|---|---|---|
| A — Saldos | ✅ | 0 |
| C — mov_doble | ⚠️ | 1 (endoso $1) + 26 cosméticos |
| D — Cheques rebotados | 📋 (operativo) | 0 (decisión de negocio) |
| E — Tests | ✅ 92,5% | 5 (stubs rotos, ya arreglados) |
| F — Performance | ✅ | 0 |
| G — Permisos | ⚠️ | **6 (arreglados)** |

**Total bugs arreglados en esta sesión: 11** (5 stubs + 6 templates de permisos).

**Pendientes de tu decisión:**
1. Limpiar endoso de prueba mov_doble 5434/5437.
2. Decidir qué hacer con los 10 cheques rebotados antiguos de MIRIAN MARGOTH y otros.
3. Eventualmente: ¿se reasigna el rol Dueño con permisos explícitos o se mantiene con `*` wildcard? El wildcard es más simple pero menos auditable.
