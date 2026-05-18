---
name: programa-core
description: Working on Programa Core — the Flask + PostgreSQL rewrite of Intela's dBase/Clipper system that lives at /Users/tamaraeliscovich/Documents/Claude/Projects/Programa Core. Use this skill ALWAYS when the user mentions Programa Core, cheques, compras, caja, bancos, facturas, posdat, mov_doble, historial, reversar/anular, conciliación, /informes/balance, /informes/flujo, utilidad, PATR, PATANT, MAT.PR., stock (hilado/tejido/terminado/químicos), or asks to edit/debug/add features under that path. This is DIFFERENT from `formulas_app` (the Ecuador textile Flask app — that has its own skill `intela-formulas-app`). Use this skill whenever the work touches money flow, saldos, reversos, or financial reports in Programa Core — even if the user doesn't name it explicitly.
---

# Programa Core — invariantes y patrones

Programa Core es el rewrite Flask + PostgreSQL del sistema dBase/Clipper de Intela (Ecuador, textil). La data vive en `scintela.*` (PostgreSQL local, base `intela`). El usuario es Tamara — la dueña.

Stack: Flask + Jinja2 + Tailwind, PostgreSQL 17, psycopg2. Pool en `db.py`. Migrations en `migrations/NNNN_*.sql`, runner `scripts/migrate.py`. Server local en `:5000` via `./scripts/dev.sh`.

## Signo de documentos bancarios (la regla más importante)

`bank_helpers.signo_documento` decide el signo del delta:

```python
DOCS_ENTRADA = ("DE", "TR", "XX", "NC", "IN")  # +1 al saldo
# Cualquier otro (CH, ND, DB, ...) = -1 al saldo
```

**Reverso bancario — documento de signo opuesto:**

| Original | Signo | Reverso |
|---|---|---|
| CH (cheque emitido) | − | **NC** |
| DE (depósito) | + | **CH** |
| TR (transferencia recibida) | + | **CH** |
| NC (nota crédito) | + | **CH** |
| ND (nota débito) | − | **NC** |

**Bug histórico real:** `bancos.reversar_cheque_emitido` usaba `ND` para reversar `CH`. Ambos restan. Cada reverso bajaba el saldo en `2×importe`. Fix: usar `NC`.

**En caja** (`caja_helpers.signo_tipo`): `E`+, `S`−, `CB`+. Reverso de E es S, y viceversa.

## Reverso atómico — patrón canónico

```python
with db.tx() as conn:
    # 1) Compensar saldo principal con doc de signo opuesto
    bank_helpers.insert_movimiento_bancario(
        conn, no_banco=..., documento="NC",
        importe=abs(original_importe), ...
    )
    # 2) Deshacer side effects en la misma tx
    ...
    # 3) Registrar mov_doble linkeado
    mov_doble.registrar(
        conn=conn,
        tipo=f"reverso_{tipo_original}",
        id_original=md_original.id_mov_doble,  # ← clave
        ...
    )
```

`id_original` marca el mov_doble del alta como `estado='reversado'` + `id_reverso`. Sin eso, `/historial` lo sigue mostrando "activo".

## Filtrar `stat='Y'` en TODA query que suma desde compra/factura

Bug que infló utilidad +$91K: queries en `informes/queries.py` no filtraban anuladas. Compras `stat='Y'` seguían contando en MAT.PR. → U$/kg ponderado inflado → stock_us inflado → PATR inflado.

```sql
AND COALESCE(stat, '') NOT IN ('X', 'Y')
```

Para queries de SUM contable. Excepción: queries de listado (la dueña quiere ver las anuladas con badge).

## Helpers de saldo NUNCA con `ancla=None`

```python
# Mal — explota:
bank_helpers.recompute_saldos_desde(conn, no_banco=10)
# Bien:
bank_helpers.recompute_saldos_desde(conn, no_banco=10, ancla_id=12345)
```

Defensa contra bug histórico que destruyó el opening de Pichincha de muchos años. Mismo en `caja_helpers`.

## Convención de `posdat.banc`

Documentada en `modules/posdat/__init__.py:12`:

- `banc=0` = abierta, deuda viva sin instrumentar.
- `banc=9` = pagada/cerrada (sale del pasivo).
- `banc=N` (1=Pichincha, 2=Internacional) = posdatado emitido a banco X.

**Para `/informes/flujo` (egresos proyectados):**

```sql
WHERE COALESCE(banc, 0) IN (0, 9)
  AND fechad IS NOT NULL
-- vencidos imputados a hoy:  CASE WHEN fechad < CURRENT_DATE THEN CURRENT_DATE ELSE fechad END
```

**Replica MENU.PRG líneas 683-684**:
```dbase
CASE BANC=9 .OR. BANC=0
   G = G + IMPORTE
```

Y línea 649 del PRG: `&RF DATE()+7 FOR FECHAD<=DATE()+5 AND NB=0 AND PROV=' '` — push de vencidos a today+7 (nosotros usamos today, equivalente).

Categorías:
- `banc=0` → deuda viva (no cheque emitido). Incluir todos.
- `banc=9` → cheques posdatados emitidos en dBase legacy. **Incluir todos** (vencidos + futuros). El saldo bancario actual NO los refleja porque en legacy se descuentan al fechad, y los vencidos pueden no haber clearado.
- `banc=10/32` (modernos PC) → NO contar. Al emitir, `bank_helpers.insert_movimiento_bancario` descuenta saldo hoy. Contarlos sería double-counting.

⚠ **TMT errores del 2026-05-13 (no repetir)**:
1. Cambié a `banc=0` only → dBase mostraba MIN −2,276, nuestro chart positivo en $954K.
2. Probé `banc=0 OR (banc=9 AND fechad>=hoy)` → todavía faltaban $1.3M de banc=9 vencidos.
3. Fix definitivo: leer **MENU.PRG líneas 683** y mirror exacto = `banc IN (0, 9)`, vencidos imputados a hoy.

**Lección**: cuando hay duda sobre lógica de negocio, abrir `/Users/tamaraeliscovich/Documents/INTELA copy/MENU.PRG` y leer el código dBase original ANTES de cambiar el filtro.

## Amortización diaria prorrateada (activos fijos)

`scintela.activos.amortimes` y `scintela.activos.valor` legacy se recalculan **CADA DÍA** en dBase (MENU.PRG líneas 275-276):

```dbase
COEF = IIF(DAY(DD)>30, 1, DAY(DD)/30)
REPLA ALL AMORTIMES WITH COEF*CUOTA, VALOR WITH INICIAL-AMORTIZAC-AMORTIMES
```

Es decir el día N del mes, AMORTIMES = `N/30 × CUOTA` (truncado a 1.0 si N>30). El valor en libros baja un poquito cada día.

En Programa Core NO guardamos eso en DB. Lo computamos on-the-fly en queries (`informes.activos_totales`, `activos.buscar`, `activos.resumen`):

```sql
WITH coef AS (SELECT LEAST(EXTRACT(DAY FROM CURRENT_DATE)::numeric, 30) / 30.0 AS c)
SELECT ...
  ROUND((coef.c * COALESCE(cuota, 0))::numeric, 2)                AS amortimes,
  GREATEST(inicial - amortizac - coef.c * cuota, 0)               AS valor
FROM scintela.activos
```

La proc mensual `scintela.actualizar_amortizacion()` SÍ consolida AMORTIMES → AMORTIZAC al cambiar de mes (idempotente vía `ult_mes_amortizado`).

⚠ No usar las columnas `valor` o `amortimes` STORED para mostrar al usuario — siempre recalcular con el COEF.

## Provisiones diarias (PASIVOS)

`MENU.PRG` líneas 282-333 aplica provisiones diarias **automáticas** a posdats específicos cada vez que el sistema abre en un día nuevo distinto al guardado en VARMEMO, Y la fecha no es domingo. Total por día hábil: **$31,000**, distribuido en 12 categorías:

| Filtro | Concepto matcher | Monto |
|---|---|---|
| PROV=YY | starts_with "SR" | $2,700 (Imp. Renta + Util.) |
| PROV=YY | starts_with "13" | $1,000 (13° sueldo) |
| PROV=YY | starts_with "14" | $300 (14° sueldo) |
| PROV=YY | starts_with "AB" | $1,300 (Aporte) |
| PROV=YY | starts_with "SS" | $2,400 (Seguro social) |
| PROV=YY | starts_with "A,E,C" | $7,300 |
| PROV=YY | starts_with "SUELDOS" | $6,000 |
| PROV=YY | concepto = "ALQUILER" | $700 |
| PROV=RT | (cualquier concepto) | $8,400 (IVA) |
| (cualquier) | contains "INCOB" | $400 |
| (cualquier) | starts_with "JP" | $200 |
| (cualquier) | contains "INTER" | $300 |

Implementado en `modules/informes/queries.py::correr_provisiones_diarias()`. Idempotente vía `scintela.sistema_meta` clave `provisiones_diarias_ult_fecha`. Auto-ejecuta al cargar `/informes/balance`. Excluye domingos (igual que dBase).

Cada categoría updatea **un solo** posdat (el primer match por `id_posdat`), no todos. dBase usa `LOCA ... IF FOUND` → primer match.

Si no hay match para una categoría (porque no existe ese posdat plantilla en la base), se saltea silenciosamente.

**Para `posdat_totales` (PASIVO en balance):** `banc=0` solamente (deuda viva, lo que falta pagar — los banc=9 ya están "comprometidos" via cheque).

## PLAZ.COBR / PLAZ.DEUDA — fórmula dBase

**Ojo: NO es DSO/DPO clásico.** dBase mide **plazo otorgado promedio ponderado por importe** (vencimiento − fecha de emisión), no días desde emisión.

Implementado en `informes/queries.py::plazos_dbase()`:

```sql
-- PLAZ.COBR ≈ 30 días (dBase legacy: 32.9)
SELECT ROUND(SUM(saldo * (vencimiento - fecha)) / NULLIF(SUM(saldo), 0))::int
  FROM scintela.factura
 WHERE COALESCE(saldo, 0) > 0
   AND COALESCE(stat, '') IN ('Z', 'A', '', ' ')
   AND vencimiento IS NOT NULL AND fecha IS NOT NULL;

-- PLAZ.DEUDA ≈ 117 días (dBase legacy: 96.7)
SELECT ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0))::int
  FROM scintela.posdat
 WHERE COALESCE(banc, 0) = 0
   AND fechad IS NOT NULL AND fecha IS NOT NULL
   AND (fechad - fecha) BETWEEN 0 AND 365;  -- saca refinanciamientos eternos (YY 2009, BP 2022)
```

**No usar** `fecha_evento - hoy` ponderado por inflow/outflow neto (lo que hacía el JS viejo): mide "días restantes promedio en la ventana visible" y depende de la ventana — daba 23/25, completamente desconectado del dBase.

Render: server-side via `queries.plazos_dbase()` → `views.flujo_grafico` → template `flujo_grafico.html` con `{{ plazos.cobro }}` / `{{ plazos.deuda }}`. No JS.

## Scripts de diagnóstico read-only

`scripts/diag_flujo.py` corre 10+ queries contra la DB local y guarda en `scripts/_diag/flujo_<ts>.json` y `flujo_latest.json`. Útil cuando hay que entender una métrica nueva sin que Tamara tenga que copiar/pegar. Patrón: agregar más queries acá antes de tocar código de producción.

`scripts/_check_plazos.py` corre `queries.plazos_dbase()` y guarda en `_diag/plazos_check.json`.

## Dispatcher de reverso en `/historial`

`modules/historial/views.py::_REVERSO_DISPATCH` mapea cada `tipo` de mov_doble → endpoint de wizard. Para agregar un tipo nuevo:

1. Crear `<modulo>.confirmar_<accion>` (GET, muestra detalle + motivo opcional).
2. Crear `<modulo>.<accion>` (POST, llama a `queries.<accion>()`).
3. Agregar entrada al dispatcher.
4. Agregar label en `historial.queries.TIPOS_LABEL`.

## Scripts de validación canónicos

### `scripts/check_salud_dia.py` — pasada diaria de invariantes (TMT 2026-05-15)

Read-only, un solo lugar para preguntarle "todo está sano?". Tira reporte
semaforizado en 8 secciones:

1. **CAJA** — egresos S del mes sin clasificar como gasto (huérfanos en
   mov_doble).
2. **GASTOS** — reparto V1..V9 + warning si V9 > 35% (clasificación
   demasiado gruesa).
3. **BANCOS** — saldo running consistente vía walk-forward desde ancla.
   Replica `_signed_delta` de bank_helpers para validar; detecta el primer
   `id_transaccion` donde el running diverge del calculado.
4. **MOV_DOBLE** — reversos sin id_original, reversados sin id_reverso,
   cadenas inconsistentes (alta marcada reversado pero el reverso linkeado
   no es estado='reverso').
5. **CHEQUES** — stat='X' con aplicaciones vivas, stat='E' con compra
   hermana stat='Y'.
6. **FACTURAS** — `saldo != importe - abono` (running invariant).
7. **POSDAT** — banc=9 activas (informativo), anuladas con banc<>9 (raro).
8. **PROVISIONES** — `fecha_modifica` de la posdat SRI vs hoy + check de
   `sistema_meta.provisiones_diarias_ult_fecha`.

```bash
python scripts/check_salud_dia.py
python scripts/check_salud_dia.py --solo bancos,facturas
python scripts/check_salud_dia.py --verbose
```

Exit code 1 si hay ERR. Útil para wrappear en CI / cron si querés.

**Lección clave del walk-forward (sec 3)**: hacer `SUM(_signed_delta)` global
NO es confiable como cross-check — la primera fila legacy ya puede tener un
signo que rompe la cuenta. La técnica que funciona es: anclar en la primera
fila con saldo no-NULL, luego recorrer en orden temporal calculando
`saldo_anterior + _signed_delta(doc, imp)` y comparar contra el saldo
running de cada fila. El primer punto de divergencia ES el bug. Si usás un
sum global como atajo, vas a inventar drifts millonarios que no existen
(probado 2026-05-15: sum global decía $3.2M de drift en Pichincha; walk-
forward dijo OK).

### `scripts/validar_reversos.py` — audit read-only del dispatcher

Corré antes de cerrar el día:
- Tipos OK / bloqueados / huérfanos en el dispatcher.
- Integridad de `mov_doble`.
- **Cross-check de saldos bancarios running**: ancla en primera fila con saldo válido y valida deltas hacia adelante.

### `scripts/snapshot_resultados.py` — antes/después

```bash
python scripts/snapshot_resultados.py --label antes
# ... hacer operación + reverso ...
python scripts/snapshot_resultados.py --label despues
python scripts/snapshot_resultados.py --diff antes despues
```

Si la op + reverso es atómica, el diff debe ser "SIN CAMBIOS". Cualquier delta en saldos/utilidad/stock indica bug.

### `scripts/check_gastos_caja.py` — clasificación V1..V9 detallada

Foco específico en gastos pagados con caja. Tira:
- Caja S sin clasificar.
- Reparto V1..V9 con bar chart.
- Patrones de concepto que caen en >1 num (señal de clasif manual heterogénea).
- Top conceptos en V9 — los que más concentran "varios admin".

```bash
python scripts/check_gastos_caja.py                  # mes actual
python scripts/check_gastos_caja.py --mes 2026-04
python scripts/check_gastos_caja.py --top 30
```

### `scripts/backfill_batch_id.py` — agrupar mov_doble huérfanos

Backfilea `batch_id` en filas que se insertaron antes de la 0031. Cluster
por (usuario, ventana de 5s) — sólo tipos batcheables. Idempotente.

```bash
python scripts/backfill_batch_id.py            # dry-run
python scripts/backfill_batch_id.py --apply
```

### `scripts/migrate.py`

```bash
python scripts/migrate.py --status
python scripts/migrate.py
python scripts/migrate.py --force 0025
```

Si una migration falla con "must be owner", la function la creó `postgres` y la app corre como otro user:
```bash
psql -U postgres intela -c "ALTER FUNCTION scintela.<nombre>() OWNER TO <DB_USER>"
```

**Marker `-- migrate:no-transaction`** (desde 2026-05-15). Una `.sql` puede declarar en su header que necesita correr SIN transacción — obligatorio para `CREATE INDEX CONCURRENTLY`, `REINDEX CONCURRENTLY`, `VACUUM`, `ALTER TYPE ... ADD VALUE` (pre-12). El runner detecta el marker en las primeras 10 líneas y:

1. Pone `conn.autocommit = True`.
2. Parte el archivo en statements (splitter respeta `--`, `/* */`, strings `'...'`, identifiers `"..."`, dollar-quotes `$tag$...$tag$`).
3. Ejecuta cada statement individualmente (cada uno es su propia tx implícita).
4. Inserta en `seguridad.migraciones_aplicadas` y restaura el autocommit anterior antes de devolver la conn al pool.

Ejemplo canónico: `migrations/0030_indices_concurrently.sql`. Si el runner muere a mitad de un CONCURRENTLY, PG deja el índice `indisvalid=false` — droppealo y re-correr `python scripts/migrate.py` (es idempotente por `IF NOT EXISTS`).

## Modelo de datos

```
scintela.banco                   → Pichincha #10, Internacional #32
scintela.transacciones_bancarias → importe SIGNED legacy + ABS nuevo, saldo running
scintela.caja                    → tipo E/S, importe positivo, saldo running
scintela.cheque                  → stat Z/B/P/D/E/X/1/2/3/A/R
scintela.compra                  → tipo H/K/T/Q/C; stat NULL=activo, Y=anulado; cuenta_pagada B/C/P/E
scintela.factura                 → stat Z/A/T/X/Y; saldo+abono running
scintela.posdat                  → banc 0=viva, 9=pagada, 1/2=emitido a banco
scintela.retiros                 → ret positivo=real, negativo=compensación
scintela.capital                 → snapshot patrimonio (capital+util=patri)
scintela.dolares                 → anticipos USD por cta=codigo_prov
scintela.xgast                   → gastos generales, categoría num 1-9
scintela.mov_doble               → historial unificado; estado activo/reversado/reverso
scintela.historia                → snapshot mensual de cierre
scintela.iniciales               → opening de cada mes (hilado/tejido/terminado)
```

### Stats legacy

**Cheque**: Z=cartera, B=depositado Pichincha, A=legacy acreditado, P=postergado, D=Daniela, E=endosado, 1/2=rebotado, 3/R=terminal, X=eliminado, T=cobrado total.

**Factura**: Z=emitida, A=parcial, T=total, X/Y=anulada.

**Compra**: NULL=activa, Y=anulada. `cuenta_pagada`: NULL=posdat, B=banco, C=caja, P=parcial, E=endoso.

## Convenciones críticas

### Signo importe en `transacciones_bancarias`

**MIXTO:**
- Legacy DBF: SIGNED (-N para egresos).
- bank_helpers nuevo: ABS (+N siempre).

`_signed_delta(documento, importe)` unifica:
- `importe < 0` → ya signed, usar como está.
- `importe >= 0` → aplicar `signo_documento(doc) × importe`.

**Nunca** almacenar negativo al INSERT — pasar `abs(importe)` a `bank_helpers.insert_movimiento_bancario` y dejar que el helper signe.

### `mov_doble.registrar` preserva signo

Antes hacía `abs()` y ocultaba devoluciones. Ahora preserva signo. Distingí visualmente con `tipo='factura_devolucion'`.

### Motivo en reversos

Pasá `motivo_obligatorio=True/False` al template `_confirmar_accion.html`. La validación final es del lado server (`if motivo_obligatorio and not motivo.strip(): flash...`).

**Obligatorio (motivo_obligatorio=True)** — operaciones críticas con side-effect bancario o irreversible:
- `cheques.confirmar_reverso` cuando el cheque rebotó realmente (state=1/2/3).
- `bancos.reversar_cheque_emitido` (compensa saldo con NC).
- `bancos.reversar_transferencia`.
- `caja.confirmar_reverso` con tipo `caja_s_to_*` (afecta side-effect: banco, retiro, USD, compra).
- `cheques.confirmar_reverso_endoso`.
- STOP del sistema / cerrar período.

**Opcional (motivo_obligatorio=False)** — operaciones administrativas que sólo cambian estado, sin movimiento de plata fuerte:
- `facturas.confirmar_anulacion`.
- `gastos.confirmar_anulacion`.
- `compras.confirmar_anulacion`.
- `cheques.transicionar` con stat='P' (postergar) o edit de fechad/importe.
- `posdat.anular` (cuando banc=0, sin instrumentar).
- Cualquier `reverso_factura_anulada` / `reverso_compra_anulada` / `reverso_gasto_anulado` desde `/historial` (administrativo).

Regla mnemónica: **si la acción mueve saldo bancario o de caja, motivo obligatorio**. Si sólo cambia un `stat` o `anulada`, motivo opcional.

### Preview de saldo en confirmaciones

Para acciones que afectan saldo bancario, pasar al template `saldo_preview` (dict o lista de dicts):

```python
return render_template(
    "_confirmar_accion.html",
    titulo="...",
    motivo_obligatorio=True,
    saldo_preview={"label": "Pichincha", "antes": 12345.67, "despues": 9876.54},
    ...
)
```

Renderiza una franja amber con "Saldo banco antes → después" para que la dueña vea el impacto antes de confirmar. Lista: para acciones que tocan dos bancos (transferencias).

## Checks al agregar un flujo nuevo

1. ¿Query filtra `stat='Y'` cuando suma para reportes?
2. ¿Reverso usa documento de signo opuesto?
3. ¿INSERT en `transacciones_bancarias` pasa por `bank_helpers`?
4. ¿`db.tx()` es realmente atómica (todo dentro del `with`)?
5. ¿SQL sin `%` literal sin escapar (incluso en comentarios)?
6. ¿`recompute_saldos_desde` con ancla?
7. ¿Conceptos truncados a 50 chars no pierden info crítica?

## Acciones canónicas + reversos

| Acción | Endpoint | Reverso |
|---|---|---|
| Emitir cheque | `bancos.emitir_cheque` | `bancos.reversar_cheque_emitido` |
| Transferir banco↔banco | `bancos.transferir` | `bancos.reversar_transferencia` |
| Caja movimiento | `caja.crear` | `caja.confirmar_reverso` |
| Endosar cheque | `cheques.endosar` | `cheques.confirmar_reverso_endoso` |
| Aplicar cheque a factura | `cheques.aplicar_a_factura` | `cheques.confirmar_desaplicar` (granular) |
| Rebote/anulación cheque | `cheques.transicionar` | `cheques.confirmar_reverso` |
| Compra | `compras.nueva` | `compras.confirmar_anulacion` |
| Factura | `facturas.nueva` | `facturas.confirmar_anulacion` |
| Gasto | `gastos.nuevo` | `gastos.confirmar_anulacion` |
| Aporte capital | `capital.aportar` | `capital.reversar_aporte` |
| Retiro socio | `capital.retirar` | `capital.reversar_retiro` |
| Pago posdat | `bancos.emitir_cheque tipo=proveedor` | `bancos.reversar_cheque_emitido` (reabre posdat) |
| Clasificar caja S como gasto V1..V9 | `gastos.clasificar_desde_caja` | `gastos.confirmar_desclasificar` (anula xgast, deja caja S libre para re-clasificar) |

Todos en `historial.views._REVERSO_DISPATCH`. Botón "↺ reversar" en cada fila activa de `/historial`.

## Conciliación bancaria

`/conciliacion/` (link "Conciliar con banco" en sidebar). Sube CSV del banco real → cruza con cheques+depósitos → muestra sospechosos. POST "confirmar rebote" llama a `cheques.reversar` con motivo editable.

## Backfill / limpieza si los números no cuadran

1. `python scripts/validar_reversos.py` — ¿drift en saldos?
2. `python scripts/snapshot_resultados.py --label diag`
3. Mirar sección 5 (inconsistencias de negocio).
4. Si faltan mov_doble: `python scripts/backfill_historial_crud.py` (dry-run primero).

## Lo que NO hacer

- **No** `recompute_saldos_desde` sin ancla.
- **No** raw INSERT en `transacciones_bancarias` o `caja` — usar helpers.
- **No** `try/except: pass` silencioso en `mov_doble.registrar`.
- **No** reusar ND para reversar CH — usar NC.
- **No** filtrar `stat='Y'` en queries de listado.
- **No** sumar posdat banc=9 como egresos futuros — ya están en transacciones_bancarias (double-counting de $2.38M históricamente).
- **No** medir plazos con `fecha_evento - hoy` sobre la ventana del chart — sólo da números que dependen de qué tan grande sea la ventana. Usar `plazos_dbase()` (plazo otorgado ponderado).

## Re-audit 2026-05-15 — 17 bugs cerrados (post-port dBase)

Después del port de los 12 items dBase, una pasada de paranoia tipo R-audit cazó **6 CRITICAL + 7 HIGH + 4 MEDIUM**. Todos fixed. Smoke tests `scripts/smoke_test_dbase_port.py` 16/16 OK (120 asserts). Algunos invariantes que NO se pueden volver a romper:

### `cheques.reemplazar()` — bloque atómico crítico

- `FOR UPDATE` en el SELECT del cheque viejo y en CADA factura del loop (serializa contra reemplazos/aplicaciones concurrentes).
- **NO zeroar `importe` del cheque viejo** — preserva face value para auditoría. La marca de reemplazo va sólo en `stat='X'` + observación.
- **NO** hacer `DELETE FROM posdat WHERE prov=codigo_cli` (el clásico bug: `prov` es código de proveedor, NO de cliente — la cláusula era incorrecta y peligrosa). Usar `UPDATE anulada=TRUE` (soft-delete migración 0027) con filtro sólo por `num=id_cheque_viejo`.
- **NO** pasar `id_original=md_alta_viejo` al `mov_doble.registrar('cheque_reemplazo', ...)` — eso marcaba el alta original como `estado='reversado'`, confundiendo "alta deshecha" con "primer reemplazo aplicado". `cheque_reemplazo` es su propio evento; `id_original=None`.
- Si la suma de aplicaciones del viejo > importe nuevo: REHUSAR explícito con mensaje (no decidir auto cómo re-escalar).
- Loop con `OrderedDict` por `id_fact` — evita N+1 cuando un cheque tiene múltiples aplicaciones a la misma factura.

### `dolares.convertir_a_compra()` — BAP

- `pg_advisory_xact_lock(hashtext('bap_seq_compra'))` al inicio de la tx — serializa generación de comprobante `BAP{N+1}`.
- Seq se calcula con `MAX(NULLIF(regexp_replace(comprobante, '^BAP', ''), '')::int) + 1`. **NO** usar `COUNT(*) WHERE comprobante LIKE 'BAP%'` (no es monótono: si borrás un BAP el próximo colisiona).
- `SELECT ... FROM dolares WHERE id IN (...)` necesita `ORDER BY id ASC FOR UPDATE` para serializar contra otra conversión de los mismos ids.

### `iniciales.cerrar_mes_auto()` — cierre mensual de stock

- Endpoint sólo POST (antes aceptaba GET — prefetchers podían dispararlo).
- Empezar la tx con `pg_advisory_xact_lock(hashtext('cerrar_mes_auto'))` ANTES del `SELECT ... FOR UPDATE` en `sistema_meta`. El `FOR UPDATE` sobre fila inexistente (primer-ever-run) no bloquea, así que dos workers pueden duplicar `iniciales`.

### `cartera.tomar_snapshot()` — snapshot atómico

- SELECT de filas + INSERTs ON CONFLICT DEBEN estar en la **MISMA** `db.tx()`. El SELECT inicial NO debe correr en autocommit (era el bug: cobros entrantes contaminaban el snapshot).
- `pg_advisory_xact_lock(hashtext('cartera_snapshot'))` para serializar entre runs concurrentes.

### `cartera.comparar_contra_snapshot()` — controlc

- **NO** caer silenciosamente a comparar "hoy vs hoy" cuando sólo existe el snapshot del día actual. Error explícito: "no hay snapshots ANTERIORES a hoy".
- Caso contrario el usuario veía `diferencia=0` en todas las filas y pensaba que la cartera estaba estable cuando en realidad no había contra qué comparar.

### Provisiones diarias (`correr_provisiones_diarias`) — INFORMES.PRG L282-333

La lista canónica vive en `modules/informes/queries.py:PROVISIONES_DIARIAS`. Total $31,600/día. Si el balance dBase ↔ Programa Core no calza por exactamente N × $X/día, el bug es esta lista. Los valores correctos verificados con dueña 2026-05-15:

| Concepto | $/día | Notas |
|---|---|---|
| **SR (SRI)** | **3300** | Era 2700 antes — bug stale del PRG vieja. |
| 13 (aguinaldo) | 1000 | matcher `concepto_starts_with` |
| 14 (sueldo) | 300 | matcher `concepto_starts_with` |
| AB (Andrés Bucheli) | 1300 | |
| SS (IESS) | 2400 | |
| **A|E|C** (agua/energía/combust) | **7300** | matcher `concepto_starts_with_any` — pattern original "A,E,C" del PRG nunca matchea con `LIKE 'A,E,C%'`. Equivalente dBase: `LEFT(concepto,1) $ 'AEC'`. Sin este fix se pierden $7,300/día = **~$220k/mes invisibles**. |
| SUELDOS | 6000 | |
| ALQUILER | 700 | matcher `concepto_eq` |
| RT | 8400 | matcher `prov_eq`, sin filtro YY |
| INCOB | 400 | matcher `concepto_contains` |
| JP (jubilación patronal) | 200 | |
| INTER (intereses) | 300 | matcher `concepto_contains` |

`forzar=True` rechaza si `ult_fecha >= hoy` — antes dos llamadas seguidas con `forzar=True` re-aplicaban el mismo día.

### Migration 0030 — índices CONCURRENTLY

Hay 3 índices que la 0029 creó SIN `CONCURRENTLY` (ACCESS EXCLUSIVE LOCK durante la creación, mata reads/writes en `transacciones_bancarias`, `chequesxfact`, `cheque`). La 0030 los dropea + recrea CONCURRENTLY.

Inicialmente la 0030 había que correrla a mano con `psql -X -f` porque el runner envolvía cada `.sql` en `BEGIN/COMMIT` y CONCURRENTLY tira `ActiveSqlTransaction`. El **fix definitivo** fue agregar el marker `-- migrate:no-transaction` al runner (ver sección de `scripts/migrate.py` más arriba). Con eso `python scripts/migrate.py` aplica la 0030 sin tocar nada a mano. Cualquier migración futura con CONCURRENTLY/VACUUM/REINDEX debe abrir con esa línea en el header.

### Validación de inputs en endpoints nuevos

- `cobranzas.matriz_3_semanas`, `posdat.api_nuevo`, `cartera.controlc` → si `?fecha=` o `?hasta=` raw no-empty NO parsea, devolver error explícito (no caer silenciosamente a `today()`).
- `cartera.cartera_por_cliente_y_color` → `max(1, min(int(meses_atras or 3), 24))` con `try/except` (antes ValueError o OverflowError con inputs hostiles).
- `iniciales.cerrar_mes_auto` → POST-only.
- `cheques.boleta_deposito` → resolver Pichincha **dinámico** por nombre, NO `no_banco=1` hardcoded (en data 2026 Pichincha es `no_banco=10`).

### Logging en `recientes.registrar`

`try/except Exception: pass` se reemplazó con `logging.exception(...)` sin re-raise en los call sites (`cheques/views.py:944`, `posdat/views.py:145`). Si "Recientes" rompe, los stacks aparecen en log; la UX del detalle no se rompe.

## Convergencia Programa Core ↔ dBase (estado 2026-05-15)

Después de SR=3300 + UPDATE catch-up de $1,800 sobre `posdat id=155 (SRI PROVISION)`, los números calzan:

| Concepto | dBase | Programa Core | Diff |
|---|---|---|---|
| PASIVOS | $1,997,063 | $1,997,063 | $0 ✓ |
| TOTAL ACTIVO | $22,008,901 | $22,008,902 | $1 (redondeo) ✓ |
| PATRIM. NETO | $20,011,838 | $20,011,839 | $1 ✓ |
| UT.ACT | -$18,663 | -$18,662 | $1 ✓ |
| GASTOS (línea costo del balance) | $628,055 | $264,998 | -$363K — fuente data, ver abajo |

**Sobre GASTOS**: la dueña confirmó que dBase muestra $628,055 (correcto) y PC $264,998 (sub-contado). La diff de $363K viene de que la fórmula `GASTOS = V7+V8+V9 + DEPRCAR` (`modules/informes/queries.py:2577-2599`) sólo lee `xgast` y la amortización de carros. dBase usa `GS = G1+G2+CA+DEPRCAR` que también incluye gastos pagados directos por **caja** y por **banco** que nunca pasaron por `xgast`. Pendiente de implementar: agregar `caja egresos mes` ($125K disponibles en data actual) + `compras tipo C/Q mes` (a confirmar contra qué tipo). NO sumar `xgast V7V8V9 + caja egresos` sin lógica anti-duplicación — un gasto pagado por caja puede haberse cargado además en xgast (ej. factura de luz registrada en xgast + pago a caja). Antes de tocar la fórmula: revisar muestra real para ver si hay overlap.

## Catch-up manual de provisiones SR (post-fix 2026-05-15)

Si en el futuro se cambia un valor de `PROVISIONES_DIARIAS` y querés "compensar" los días que se aplicaron con la lista vieja sin esperar a que converja:

```sql
-- Ejemplo: SR pasó de 2700 a 3300, se aplicó $600 de menos por N días.
UPDATE scintela.posdat
   SET importe = importe + (N * 600),
       usuario_modifica = 'catch_up_sr_3300',
       fecha_modifica = CURRENT_TIMESTAMP
 WHERE id_posdat = 155;  -- posdat de SRI PROVISION, prov='YY', concepto LIKE 'SR%'
```

Identificar la posdat correcta con:
```sql
SELECT id_posdat, prov, concepto, importe
FROM scintela.posdat
WHERE COALESCE(banc,0) <> 9
  AND (anulada IS NOT TRUE OR anulada IS NULL)
  AND UPPER(TRIM(COALESCE(prov,''))) = 'YY'
  AND UPPER(COALESCE(concepto,'')) LIKE 'SR%'
ORDER BY id_posdat LIMIT 3;
```

Más limpio: dejar que las corridas diarias converjan solas (1-3 días).

## Smoke tests post-port

`scripts/smoke_test_dbase_port.py` cubre los 12 items del port + 4 regression guards del re-audit (R5–R8 — A,E,C matcher, boleta no_banco dinámico, reemplazar no zeroa importe, BAP advisory lock). Antes de cualquier deploy: 16/16 OK obligatorio. Setup usa SELECT-then-INSERT (no `ON CONFLICT (codigo_cli)`) porque `scintela.cliente.codigo_cli` y `scintela.proveedor.codigo_prov` no tienen UNIQUE constraint en la data legacy.

## Reglas de UX canónicas — la dueña es muy exigente

Estas tres reglas son **no-negociables** y aplican tanto a pantallas
existentes (corregir lo que tenemos) como a cualquier feature nuevo
(antes de pedir merge). Si una pantalla las viola, es bug.

### Regla 1 — Vocabulario: "Historial", no "Historial de movimientos dobles"
La dueña no entiende "dobles" y la confunde. En títulos, breadcrumbs,
sidebars, headers, URLs visibles y cualquier copy user-facing, decir
**Historial** a secas. "mov_doble" sí puede vivir en código/comentarios
(es el nombre real de la tabla `scintela.mov_doble`), pero NUNCA en
texto que vea Tamara.

### Regla 2 — No scrollear para ingresar datos
Las pantallas de alta (Nueva compra, Nueva factura, Cobranza, Nuevo
gasto, Aporte, Retiro, Emitir cheque propio, Nuevo posdat, etc.) deben
caber **enteras en un viewport de laptop** (~720px de alto útiles
después del header + sidebar + breadcrumb). Si no entran:
- Sacar campos opcionales a un `<details>` colapsado abajo.
- Reducir altura de inputs (`py-1` en vez de `py-2`).
- Layout en 2 columnas en vez de 1.
- Sacar copy explicativo (la regla 3 también lo manda).
El "diario" del sidebar tiene 3 tabs (Cobranza / Ventas / Compras),
cada uno con su pantalla — ese patrón está bien porque cada tab es un
form chico y dedicado.

### Regla 3 — Cero información redundante
Si un dato ya está visible en otro lugar de la misma pantalla (hero,
KPI, badge, tooltip), **NO repetirlo** en otro componente. Casos
canónicos a evitar:
- Total en el hero + card "Total" + footer "Total" → quedarse con uno.
- KPI grid arriba + strip horizontal abajo mostrando lo mismo desglosado.
- Explicaciones largas debajo de cuadros ("Cómo se calcula", "Nota:")
  cuando la fórmula ya es visible en headers de columnas.
- Mismo botón en sidebar + dropdown "Más" + acción primaria.
Aplicación retroactiva: cualquier pantalla con KPI hero + cards
duplicados, o con párrafos explicativos largos, se simplifica.

**Cómo aplicar al revisar una pantalla nueva:**
1. ¿El título mantiene vocabulario humano? (Regla 1)
2. ¿Si es un alta, entra sin scroll en laptop? (Regla 2)
3. ¿Cada dato visible aparece UNA sola vez? (Regla 3)

Si las 3 dan sí, mandar PR. Si alguna da no, refactorear antes.

## Pedido de la dueña 2026-05-18 — UX cleanup batch

Pasada grande de UI a pedido directo de Tamara (docx "Para Claude"). Lo
que importa que NO se pierda:

### Botón "Volver" como patrón canónico
La dueña odia quedar "atrapada" en una pantalla filtrada. Cualquier vista
con filtros (prov, q, fecha, etc.) debe mostrar un botón "← Volver" a la
lista completa cuando el filtro está activo. Implementado en:
- `posdat/lista.html` — `prov`/`q`/`desde`/`hasta` activos → "← Volver a todos"
- `compras/lista.html` — idem
- `informes/estado_cuenta.html` — "← Volver a Cartera"
- `deudas.html` → cada proveedor es link a `/posdat?prov=XX` (que tiene Volver)

Patrón a copiar:
```jinja
{% if prov or q or desde or hasta %}
  <a href="{{ url_for('blueprint.lista') }}"
     class="inline-flex items-center gap-1.5 text-sm px-3 py-2 rounded-lg border border-slate-300 text-slate-700 hover:bg-slate-100">
    ← Volver
  </a>
{% endif %}
```

### KPI hero filtrado vs global
Para `/posdat?prov=XX`, el hero ahora muestra el total del **proveedor
filtrado**, no la deuda global. Patrón: la query de resumen acepta el
mismo filtro que la query de listado:
```python
def resumen(prov: str | None = None) -> dict:
    return db.fetch_one(
        "... WHERE (%(prov)s IS NULL OR UPPER(prov) = UPPER(%(prov)s))",
        {"prov": prov or None},
    )
```

### Cartera: bug histórico `buckets > total` (fix 2026-05-18)
`aging_totales()` y `aging_buckets()` sumaban `b0_30 + b31_60 + b61_90 +
b90_plus = saldo_facturas`, pero `total = saldo_facturas −
cheques_en_cartera`. Resultado: el bucket 0-30 podía dar > total
("$3.5M > $3.4M"). Fix: asignar `cheques_en_cartera` contra los buckets
desde el más joven (los cheques posdatados cancelan facturas recientes).
Con esto `sum(buckets) == total` por construcción.

### Cartera por cliente — vista 5-columnas (`/informes/cartera`)
Pedido literal de la dueña: vista compacta `CLIENTE | CHEQUES | FACTURAS
| TOTAL | % DEL TOTAL`, ordenada por % desc (mayores deudores primero).
Implementado en `informes.queries.cartera_por_cliente()` + template
`informes/cartera.html`. `%` se calcula post-fetch para no complicar la
query.

### Mismatch facturas $5.3M vs cartera $3.4M
La dueña los ve como "distintos" pero son válidos por diseño:
- Facturas/lista hero → `SUM(factura.saldo)` bruto
- /cartera (aging) → `SUM(saldo) − cheques_en_cartera` (paridad dBase)
La diferencia siempre = cheques en cartera (Z/1/2/3/P/D/A). Si Tamara
vuelve a preguntar, mostrar la fórmula explícita.

### Gastos forzados en /informes/flujo
- localStorage key `flujo_gastos_forzados_v1`. Cada item:
  `{id, fecha (YYYY-MM-DD), importe, concepto}`.
- Edit + delete ya disponibles en el panel — `prompt()` triple (importe,
  concepto, fecha) para mantener UX simple sin un modal completo.
- El panel "Egresos del flujo" (lista larga de posdat) se sacó del area
  bajo el chart; quedó sólo el botón "Editar deudas (posdat banc 0/9)"
  que abre el modal.

### Provisiones inline edit
`/provisiones` ahora permite editar el importe inline (sin pasar por el
form completo). Endpoint nuevo: `provisiones.actualizar_importe` POST
sólo recibe `importe` y llama `queries.editar(id, importe=...)` que ya
soporta partial updates. La dueña dijo "esto cambia con cierta
frecuencia" — opening el form completo es overkill.

### Features faltantes vs PRG viejo (en BACKLOG)
4 items quedaron documentados en `BACKLOG.md` (sección "Pedido dueña
2026-05-18 — Features faltantes vs PRG viejo"): Comisión de vendedores,
Cobros semana actual + 3 anteriores (parcial — ya existe matriz_3_semanas),
Historia con cuadros mensuales (parcial), y Cuadro de Fuentes y Usos.
Cada uno necesita decisión de negocio antes de implementar.

## Setup local

Postgres en Homebrew@17 (base `intela`):
```bash
brew services start postgresql@17
pg_isready  # accepting connections
```

Si "lock file already exists":
```bash
brew services stop postgresql@17
rm -f /opt/homebrew/var/postgresql@17/postmaster.pid
rm -f /tmp/.s.PGSQL.5432 /tmp/.s.PGSQL.5432.lock
brew services start postgresql@17
```

Hay Postgres.app también pero apunta a cluster vacío — no usar.

### Tunnel a la DB de prod (RDS)

Cuando hay que correr `scripts/migrate.py` o `--status` contra RDS desde la laptop, la dueña abre un **ngrok TCP** sobre el puerto 5432:

```bash
ngrok tcp 5432
```

Ngrok devuelve un host:port tipo `0.tcp.ngrok.io:NNNNN` — usar ESE en el `.env` (o `DB_HOST` / `DB_PORT` env vars) para que `db.py` apunte ahí. **No usar cloudflared** — la dueña ya tiene ngrok configurado y es lo que usa habitualmente.

Igual que cualquier tunnel ad-hoc, fine para corridas cortas (migrate, status, diagnóstico). Para reads/writes sostenidos, ir por la app en EC2.
