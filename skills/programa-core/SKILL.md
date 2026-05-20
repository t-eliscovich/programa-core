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

## ⚠ Errores 2026-05-18 — NO repetir

### Error 1 — Suponer que `python scripts/migrate.py` arregla prod

**Lo que pasó:** la dueña reportó `relation "scintela.vendedor" does not exist`
en `/comisiones`. Yo le dije que corriera `python scripts/migrate.py` en su Mac.
Lo corrió, dijo "Todas las migraciones están aplicadas" — pero la URL que ella
ve es la de prod (EC2 → RDS), no su laptop. La 0032 quedó aplicada local pero
NO en RDS. Una hora perdida diagnosticando antes de caer en cuenta.

**Regla:** Programa Core **vive en producción** (EC2 `i-0fcca4d7029f08489` + RDS
`intela-db.c988ucsko537.us-east-2.rds.amazonaws.com`). Cuando la dueña reporta
algo roto en una URL real (no localhost:5000):

1. Antes de pedirle correr migrate.py local, preguntar **¿es local o prod?**
   En general la dueña usa prod.
2. Para aplicar una migración a RDS, hay que mandar SSM Run Command al EC2.
   El patrón está en la skill `intela-aws-deploy`.
3. GitHub Actions tiene `deploy.yml` que **NO corre migraciones automáticamente**
   — sólo copia el código y reinicia el task. Las migraciones son manuales.

Patrón canónico para aplicar migración a RDS (desde CloudShell):

```bash
export AWS_PAGER=""
CMD_ID=$(aws ssm send-command --region us-east-2 \
  --instance-ids i-0fcca4d7029f08489 \
  --document-name AWS-RunPowerShellScript \
  --parameters 'commands=["cd C:\\programa-core; & C:\\Python312\\python.exe scripts\\migrate.py"]' \
  --query Command.CommandId --output text)
sleep 10
aws ssm get-command-invocation --region us-east-2 \
  --instance-id i-0fcca4d7029f08489 --command-id "$CMD_ID" \
  --query '{Status:Status,Out:StandardOutputContent,Err:StandardErrorContent}' --output json
```

### Error 2 — deploy.yml excluía `*.sql` (incluyendo migrations)

**Lo que pasó:** las migrations nunca llegaban a EC2 porque `deploy.yml`
línea 50 tenía `--exclude='*.sql'` para no shipear dumps grandes como
`intela12042026.sql`. Pero el glob también barría todo `migrations/*.sql`.
Resultado: migrations 0001-0031 estaban en EC2 sólo porque las habían
copiado a mano alguna vez; cualquier migración nueva que agregáramos
quedaba sólo en la Mac. La diagnosis se hizo con
`Get-ChildItem C:\programa-core\migrations\` desde SSM — sin 0032 en
la lista.

**Fix correcto:** excluir el dump por **nombre exacto** (`--exclude='./intela12042026.sql'`),
NO con globs tipo `./*.sql`. GNU tar no anchor los globs automáticamente
— `./*.sql` matchea archivos en subdirs también (probado: `tar --exclude='./*.sql'`
sigue excluyendo `./migrations/0032.sql`). El primer intento de fix fue
con `--exclude='./*.sql'` y siguió igual de roto.

**Regla:** cuando agregás un workflow de deploy con exclusiones por glob,
**siempre verificá qué archivos quedan afuera del tarball**:
```bash
# Local antes de pushear:
tar --exclude=... -czf /tmp/deploy.tar.gz .
tar -tzf /tmp/deploy.tar.gz | grep migrations
tar -tzf /tmp/deploy.tar.gz | grep -c "\.sql$"
```
Si las migrations no están listadas → la exclusión está mal.

### Error 3 — `migrate.py` con chars Unicode `→` rompe en Windows cp1252

**Lo que pasó:** después de fixear el deploy (errores 1 y 2), la 0032
finalmente llegó a EC2. `migrate.py` empezó a aplicarla pero crasheó con
`UnicodeEncodeError: 'charmap' codec can't encode character '→'`.
La consola de Windows usa `cp1252` por default y no encodea el `→` que
había en el print de progreso del runner.

**Fix:** ASCII en todos los prints de migrate.py (`→` → `->`, `⚠` → `[!]`,
etc.). Workaround temporal mientras se deploya: setear
`$env:PYTHONIOENCODING="utf-8"` antes de invocar Python en SSM.

**Regla:** scripts que corren en Windows EC2 → solo ASCII en stdout, o
forzar `PYTHONIOENCODING=utf-8`. Los emojis y flechas Unicode son
trampa silenciosa.

### Error 4 — Inline Python en SSM rompe por quoting + import path

**Lo que pasó:** intenté correr scripts de diagnóstico en EC2 inline con
`commands='["...python -c \"...\\\"sql\\\"...\""]'`. PowerShell se atragantó
con paréntesis literales del SQL (`::int`, `COALESCE(...)`). Después el
fix de usar archivo intermedio funcionó pero el script no encontraba
`db.py` porque `C:\tmp\` no está en PYTHONPATH.

**Patrón canónico para diagnóstico SSM con base64** (ya documentado en
intela-aws-deploy skill, repito acá porque es muy común):

```bash
PY=$(cat <<'PYEOF'
import sys, os
sys.path.insert(0, r'C:\programa-core')
os.chdir(r'C:\programa-core')
from dotenv import load_dotenv
load_dotenv()                # OBLIGATORIO: SSM no inherita las env vars
                             # del web server; sin esto db.py rompe con
                             # KeyError: 'DB_HOST'.
import db
# ... tu lógica
PYEOF
)
B64=$(printf '%s' "$PY" | base64 | tr -d '\n')
CMD="cd C:\\programa-core; \$env:PYTHONIOENCODING='utf-8'; [System.IO.File]::WriteAllBytes('C:\\tmp\\diag.py', [Convert]::FromBase64String('$B64')); & 'C:\\Python312\\python.exe' 'C:\\tmp\\diag.py'; Remove-Item 'C:\\tmp\\diag.py'"
ID=$(aws ssm send-command --region us-east-2 \
  --instance-ids i-0fcca4d7029f08489 \
  --document-name AWS-RunPowerShellScript \
  --parameters "commands=[\"$CMD\"]" \
  --query 'Command.CommandId' --output text)
sleep 12
aws ssm get-command-invocation --region us-east-2 \
  --instance-id i-0fcca4d7029f08489 --command-id "$ID" \
  --query '{Status:Status,Out:StandardOutputContent,Err:StandardErrorContent}' --output json
```

**Regla:** **NUNCA** uses `python -c "..."` inline para queries SQL con
paréntesis. **SIEMPRE** base64 → archivo → ejecutar. Y siempre incluir
`sys.path.insert(0, r'C:\programa-core')` al inicio del script.

### Error 5 — Migración que crea tablas SIN garantizar el owner correcto

**Lo que pasó:** la 0032 inicialmente hacía `CREATE TABLE scintela.vendedor`
sin un `ALTER ... OWNER TO`. Si el runner corre con un user (postgres) pero
la app usa otro (postgres también, en este caso, pero podría ser distinto),
la app ve "relation does not exist" porque PG oculta tablas que el user
no puede ver.

**Regla:** toda migración que crea tablas/funciones debe terminar con un
bloque `DO $$ ALTER ... OWNER TO current_user $$` (idempotente) para que
quede del user correcto. Patrón canónico:

```sql
CREATE TABLE IF NOT EXISTS scintela.<tabla> ( ... );
DO $$
DECLARE u TEXT := current_user;
BEGIN
    IF u <> 'postgres' THEN
        EXECUTE format('ALTER TABLE scintela.<tabla> OWNER TO %I', u);
    END IF;
END $$;
```

Ya implementado al final de `migrations/0032_vendedor.sql` como referencia.
Copiarlo en cualquier migration nueva que cree tablas.

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
gasto, Aporte, Retiro, Emitir cheque, Nuevo posdat, etc.) deben
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

## Módulo comisiones (TMT 2026-05-18)

Replica `MODIFICA.PRG PROCEDURE COMISION` (línea 1770) que listaba
cobranzas del mes filtradas por `cliente.vend`. dBase NO guardaba el
% de comisión; acá lo agregamos en `scintela.vendedor` (migración 0032)
para calcular el monto a pagar automáticamente.

**Modelo:**
```
scintela.vendedor  → (codigo PK, nombre, pct_comision, activo,
                      fecha_crea, fecha_actualiza, usuario_actualiza)
```
Backfill automático en la 0032: un `INSERT` por cada `vend` distinct
no-nulo de `scintela.cliente`. Idempotente vía `ON CONFLICT DO NOTHING`.

**Stat de "cobrado":** dBase usaba `cheque.stat $ "BWVCIK"`. En PC:
`stat IN ('B', 'A')` cubre el caso (B = depositado Pichincha moderno,
A = acreditado legacy). Si aparece algún caso de cheques cobrados con
stat distinto, ampliar el filtro en `comisiones.queries`.

**Cálculo de comisión:**
```sql
comision_mes = SUM(cheque.importe) * (vendedor.pct_comision / 100)
WHERE cheque.stat IN ('B', 'A')
  AND MONTH(cheque.fechad) = mes
  AND YEAR(cheque.fechad)  = anio
  AND cliente.vend         = vendedor.codigo
```

**Ventas mes (bonus PC, no del PRG):** suma de `factura.importe` del
mes para clientes del vendedor, excluyendo `stat IN ('X', 'Y')`.

**Permiso:** todas las rutas usan `informes.ver` (mismo nivel que otros
informes — el dueño ve todo, contabilidad/cobranzas no).

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

## Pedido de la dueña 2026-05-19 — UX cleanup batch 2

Segunda pasada de UI a pedido directo de Tamara (docx "Para Claude 2", 19 items). Patrones que NO se pueden perder:

### Cobranza — `cheques/nuevo.html` (items 1-7)

- Cliente input con `onfocus="this.select()"` + `onclick="this.select()"` — para que al re-focusear, el texto quede seleccionado y la primera tecla lo reemplace. Sin esto la dueña tenía que borrar el cliente actual para reabrir el datalist.
- Facturas se cargan al `input` event del cliente (debounced 300ms), no solo al `blur`. Antes facturas aparecían recién al tabbear a N° cheque porque el `blur` del cliente se disparaba ahí.
- Orden de campos: `Importe → N° cheque → A depositar` (la dueña piensa "cuánto cobro" antes que "qué número de cheque es").
- Columna "Acum. saldo" en el listado de facturas a aplicar — running total de saldos para ver de un vistazo si X facturas suman lo que cobra.
- **NO** auto-distribuir FIFO. Solo pre-asignar cuando hay match EXACTO entre importe y saldo de UNA factura. La dueña decide explícitamente dónde aplica.
- **Botón MAX se conserva** (memoria `feedback_cobranza_max.md`). En el intento inicial lo removí — la dueña corrigió que sí lo usa. Si no se entiende la etiqueta, mejorarla; no eliminar.
- Bloque "Más datos (banco texto libre, endosado a proveedor)" eliminado del form: era redundante con el select de banco emisor y el campo banco_texto no se usaba.

### Nueva compra — `labels.py` (item 8)

`TIPO_COMPRA_OTROS = "C"` ahora se muestra como **"Consumibles"** (no "Otros"). Descripción larga: "Consumibles — repuestos, aceite, otros suministros de fábrica". Mantener este label — la dueña explícitamente dijo "C no es otros". Si surgen más feedback sobre los tipos H/K/T/Q/C, revisar acá primero.

### Bancos detalle — `bancos/movimientos.html` (item 9)

Columnas removidas del listado: `Prov/Ref`, `Usuario`, `Stat`. Eran ruido de auditoría. Si en el futuro se necesitan, mover a un toggle/hover, no de default.

### Emitir cheque — `bancos/emitir_cheque.html` (items 10 + 19)

- Removido header "3. Detalles del destino" — los campos por tipo ya están etiquetados.
- **Wizard de pago desde posdat:** si llega `?id_posdat=N` la view fetchea esa posdat, pasa `posdat_target` al template, y:
  - Muestra un banner emerald arriba con `#N · prov · concepto · $importe` y link "← Volver a posdat".
  - Default `tipo=proveedor`.
  - Pre-filtra `prov` y pre-selecciona el radio button de esa posdat (`bg-emerald-50`).
  - Pre-llena el campo `Importe` con el importe de la posdat.
  - La dueña aún puede editar todo si quiere pagar parcial.

### Cheques lista — `cheques/lista.html` + `queries.py` + `views.py` (items 11-13)

- Conteo "725" en tab "Depositados" ocultado — dato irrelevante para el día a día.
- Columna "Plazo" removida del listado — no aporta acá.
- Renombrado tab `cartera` (solo Z) → **"En cartera Z"** (el bucket interno `cartera` sigue siendo `("Z",)` por compat con balance/historial).
- Agregados 2 buckets/filtros nuevos:
  - `cartera_agg` = `("Z","P","1","2","3","D")` → tab **"CARTERA"** (todo lo en mi poder).
  - `cartera_total` = `("Z","P","1","2","3","D","B")` → tab **"CARTERA TOTAL"** (cartera + depositados pendientes).
  - Los conteos viven en `cheques/views.py` con 2 sub-queries explícitas (no derivados de SUM de buckets para evitar doble-conteo).
- **KPI hero reactivo a filtros**: si el filtro está activo (estado != `todos`, cliente, monto, q, fechas), el hero muestra `total` / `n_total` del subset filtrado. Label cambia a "cheques (filtrado)". Sin filtro: comportamiento clásico (Z+P+1/2/3+D).

### Facturas lista — `facturas/lista.html` (item 12)

KPI hero reactivo análogo a cheques: si hay filtro (cliente, q, fechas, montos, vista≠todas), muestra `total_saldo` del subset filtrado + label "facturas (filtrado)". Sin filtro: `tot_cartera_saldo` + "facturas vivas".

### Resultados / Balance — `informes/balance.html` (items 15a, 15b, 16)

Limpieza grande:
- Removido bloque "Kilos y movimientos — último cierre".
- Removidos `<details>` "Conciliación — totales del balance vs módulos" y "Diagnóstico — qué suma a qué" — la data sigue en `b.conciliacion` y `b.diagnostico` por si queremos exponerla en una vista dedicada (`/informes/utilidad_debug` o similar).
- Removida fila "Plazo cartera (días)" del panel RESULTADOS.
- Removidos bloques aux: "Utilidad = PATR − PATANT" (explicación), "Provisión pendiente del mes", "Utilidad Proyectada". `Utilidad Actual` arriba ya es suficiente.
- Removido panel completo "Detalle bancos" (vivía bajo el Balance) — el dato consolidado ya está en la fila ACTIVO → Bancos.

**Cuadro nuevo MOVIMIENTOS MES (replica INFORMES.PRG L1003-1090):** función nueva `queries.movimientos_mes_dbase()`, expuesta como `b.movimientos_mes` (envuelto en `_try_movimientos_mes()` para fail-safe). Sub-componentes:
- 4 columnas top (HILADO / TEJIDO CRUDO / TERMINADO / COLORANTES) con STOCK INIC / INGRESOS / EGRESOS / STOCK ACT (kg, $/kg, $).
- Sub-tabla **COMPRAS HILADO** con breakdown por PROV (tipo='H' del mes) + total.
- Sub-tabla **PRODUC. TEJIDO** con breakdown por PROV (tipo='K' kg>0 del mes) + total.
- **Pendiente** (TODO): TINTORERIA bajos/fuertes, CS.COLORANTES, CS.PRODUCCION, PROV.EXT — requieren separar tintura por color. Comentado en el template.

### Gastos — `informes/gastos.html` (item 17)

- 3 tarjetas resumen TEJEDURÍA/TINTORERÍA/ADMINISTRACIÓN removidas — duplicaban la fila "Total con amort." de la matriz 3×3 que está justo debajo (viola Regla 3).
- Bloque "Movimientos bancarios del mes" removido — el detalle por banco vive en /bancos, no acá.

### Posdat — `posdat/queries.py` + `views.py` (item 18)

Bug del header "4 partidas" vs 8 filas visibles arreglado. `resumen()` ahora acepta los mismos filtros que `buscar()` (`q`, `solo_abiertas`, `desde`, `hasta`) y NO filtra por `importe > 0`. Resultado: el contador del hero matchea las filas del listado. La vista `posdat.lista` pasa todos los filtros a `resumen()`.

### Fuentes y Usos — `informes/views.py` + `queries.py` + `fuentes_usos.html` (item 14)

- Selector cambió de un solo "mes/año" a **DESDE-HASTA** (dos pares mes/año). Granularidad mensual porque la data viene de `scintela.historia`. Back-compat: si vienen los viejos `?anio=&mes=`, se interpreta como ventana de 1 mes (mes elegido vs anterior).
- **Balancing line** "Aumento/Disminución de líquido (caja + bancos)" — agregada antes de los totales. Resultado: `total_fuentes == total_usos` por construcción (identidad contable). Si el delta global es +, va como USE ("aumento de líquido"); si es −, va como FUENTE ("disminución de líquido"). Replica `INFORMES.PRG::PROCEDURE FUENTES` L1654-1727.
- KPI "Variación de líquido" reemplazado por "Δ líquido del período" (= `delta_banco` real), porque con la balancing line `delta_liquido` ahora es ~0 por construcción.

### Cómo aplicar Regla 3 retroactivamente

Cada vez que la dueña "no entiende" un cuadrito o cuenta, primero preguntarse: **¿está duplicando info que ya aparece en otra parte?** En 5 de los 19 items la respuesta fue sí (cuadritos GTEJ/GTIN/GGF, panel Detalle bancos, fila explicación de utilidad, conteo de Depositados, columna Plazo). Cuando es duplicación → eliminar sin reemplazo. Cuando es nuevo concepto → reemplazar por algo que SÍ entienda (Cuadro MOVIMIENTOS MES estilo dBase).

## Pedido de la dueña 2026-05-19 — Batch 2 follow-up

### Corrección crítica: C = Tintorería (NO Consumibles)

`labels.py`:`TIPO_COMPRA_TINTORERIA = "C"` con label **"Tintorería"**. En el batch original interpreté mal su comentario "C no es otros" y lo cambié a "Consumibles" — la dueña corrigió: **C significa Tintorería**, refleja el LC2 `CC` del dBase. El alias `TIPO_COMPRA_OTROS` apunta al mismo char "C" por compat.

Tipos canónicos (después de la corrección):

| Char | LC2 | Label                 | Notas |
|---|---|---|---|
| H | HH | Hilado                | Materia prima — NO entra al matriz de gastos. |
| K | KK | Tejido                | Producción si kg>0, sino servicio de tejeduría. |
| Q | QQ | Químicos              | Colorantes + auxiliares. |
| C | CC | **Tintorería**        | Servicio de tintura. Corrección crítica 2026-05-19. |
| T | —  | Tintura (legacy)      | Duplicado de C — no usar al alta. |
| A | AA | Anticipo              | A proveedor, luego se "convierte". |
| **I** | **IN** | **Anticipo máquinas** | **Nuevo 2026-05-19** — variante de A para maquinaria. |
| S | —  | Servicios             | Luz, agua, contadora, mantenimiento. |

En la UI del selector (`/compras/nueva`), las opciones muestran el **LC2 (2 chars)** porque es el vocabulario que usa la dueña. Internamente la DB sigue guardando el char único. Mapping en `labels.TIPOS_COMPRA_LC2` + helper `lc2_para_tipo(codigo)`.

### Auto-clasificación de compras en /gastos (cascada dBase completa)

**v1 (2026-05-19 mañana, parcial):** dict simple `TIPOS_COMPRA_A_NUM_GASTO` que sólo miraba tipo. Insuficiente — faltaban SU/EEQ/AGUA/CCSU/GAS/etc.

**v2 (2026-05-19 tarde, definitivo):** cascada SQL `_SQL_COMPRA_NUM_CASE` que replica `INFORMES.PRG` L160-169 mirando tipo + concepto + codigo_prov. Primer match gana, igual que en dBase.

Reglas (orden importa — primer match):

**Set de keywords de SERVICIOS** (repetible — los mismos en los 3 rubros, pedido Tamara 2026-05-19):

```
servicios = CMB OR EEQ OR AGUA OR EMAAP OR (concepto LIKE 'GAS%')
```

El rubro lo decide el `tipo`; la palabra clave decide la sub-categoría:

| Regla | Condición | → num | Rubro · Sub-cat |
|---|---|---|---|
| 1 | `tipo='K'` AND concepto contiene `SU` | 1 | Tej · Sueldos |
| 2 | `tipo='K'` AND **servicios** | 2 | Tej · Servicios |
| 3 | `tipo='K'` resto | 3 | Tej · Otros |
| 4 | `tipo IN (C,Q,T)` AND (`CCSU` OR LC2=`SU`) | 4 | Tin · Sueldos |
| 5 | `tipo IN (C,Q,T)` AND **servicios** | 5 | Tin · Servicios |
| 6 | `tipo IN (C,Q,T)` resto | 6 | Tin · Otros |
| 7 | `tipo='S'` AND (`SU` OR LC2=`SU`) | 7 | Adm · Sueldos |
| 8 | `tipo='S'` AND **servicios** | 8 | Adm · Servicios |
| 9 | `tipo='S'` resto | 9 | Adm · Otros |
| — | `tipo IN (H, A, I)` | NULL | excluido (MP / anticipos) |
| — | `tipo='K' AND kg>0` | NULL | excluido (producción — vive en VK/IPROVK) |

**Divergencia consciente vs dBase:** el dBase original tenía la regla V5 (servicios → Tintorería) catch-all sin chequear tipo. Eso forzaba que AGUA/EEQ en una compra admin terminara en Tin · Servicios. La dueña pidió que sean simétricos por rubro (V2/V5/V8 todos aceptan los mismos keywords). Implementación local: `_SERVICIOS_KEYWORDS_SQL` constante reutilizada en las 3 reglas.

**Implementación:**
- `_SQL_COMPRA_NUM_CASE` constante con el CASE SQL. Usado dentro del SELECT en `gastos_xgast_v1_a_v9_mes()` y `gastos_detalle_categoria(n)`.
- `COMPRA_A_GASTO_REGLAS`: lista de tuplas `(slug, num, label)` para referencia/UI.
- `TIPOS_COMPRA_A_NUM_GASTO`: dict simple legacy, queda para callers externos pero NO usar para clasificar.

**LC2 que aparecen en dBase** (referencia):

| LC2 | Significado | Va a V según rubro |
|---|---|---|
| `KK` | Tejeduría | V1/V2/V3 |
| `CC` | Tintorería | V4/V5/V6 |
| `SU` | Sueldos | V1, V4 o V7 según contexto |
| `HH` | Hilado | (excluido — MP) |
| `QQ` | Químicos | V6 |
| `AA`, `IN` | Anticipos | (excluidos) |
| `GA` (LEFT 3 = `GAS`) | Gasolina | V8 |

**Palabras clave que disparan sub-cat:**

| Palabra | Sub-cat | Va a V |
|---|---|---|
| `SU` en concepto o LC2 | Sueldos | V1/V4/V7 según rubro |
| `EEQ` | Luz (Emp. Eléctrica Quito) | V2/V5/V8 |
| `EMAAP` | Agua potable | V5 |
| `AGUA` | Agua | V5 |
| `CMB` | Combustible | V5 |
| `GAS` al inicio | Gasolina | V8 |
| `CCSU` | Sueldos tintorería | V4 |

**Fórmulas finales** (idem `INFORMES.PRG` L211-217):

```
GTEJ = V1+V2+V3 + DTJ        (gastos Tejeduría + amort máq tej)
GTIN = V4+V5+V6 + DCC        (gastos Tintorería + amort máq tin)
GGF  = V7+V8+V9 + DEPRCAR    (gastos Admin + amort otros)
GSU  = V1+V4+V7              (Personal total)
GEN  = V2+V5+V8              (Servicios total)
GGT  = V3+V6+V9              (Otros total)
TTT  = GTEJ+GTIN+GGF
```

**Por qué cascada SQL y no Python:** la query corre una vez y devuelve los totales sumados. Si lo hacíamos en Python, traíamos N filas de compras al runtime y clasificábamos una por una — más lento y memory-intensive. El CASE vive cerca de la data.

**Reglas de consolidación dBase no implementadas todavía** (`INFORMES.PRG` L172-200):
- NUM=8 + concepto contiene `TRANS` o importe ≤ $10 → consolidar como "TRANSP.VS."
- NUM=9 + `AJU`/`VUELT`/`GS.VS` o importe ≤ $5 → "AJUSTES Y GS.VS."
- NUM=8 + concepto `GASOLINA` → "GASOLINA"
- NUM=6 + importe ≤ $15 → "GS.FAB.VS."

Si la dueña pide totales más limpios (en vez de N filas chicas de transporte), implementar estas consolidaciones en `gastos_detalle_categoria()` post-fetch.

**Lo que NO está clasificado:** compras con tipo `H` (hilado), `A` (anticipo), `I` (anticipo máquinas) — son materia prima / activos diferidos, no gastos operativos.

### Bordes globales más oscuros

`templates/base.html` tiene override CSS de `border-slate-200/100/300` a slate-400/300/500 (solo light mode). Si la dueña pide bordes más oscuros aún o cambios visuales en otro lugar, ahí está el botón central. NO refactorear template por template.

### Pantalla Stock fuera del sidebar

`templates/base.html` quitado `nav_link('stock.lista', 'Stock', ...)`. El route sigue vivo (no se borró el módulo) pero ya no se navega ahí — la dueña ve el stock en Resultados.

### Bancos — action bar con 4 movimientos (2026-05-19)

Réplica de cómo funciona el dBase: la pantalla `/bancos` es el hub de todo lo que se hace con bancos. Sidebar limpio (sacado el link top-level "Emitir cheque").

Action bar visible al entrar a `/bancos` con 5 botones (los 4 pedidos + transferir):

| Botón | Endpoint | Documento | Signo |
|---|---|---|---|
| **Emitir cheque** | `bancos.emitir_cheque` (existente, wizard largo) | CH | − |
| **Depositar** | `bancos.nuevo_movimiento?doc=DE` (nuevo, form simple) | DE | + |
| **Nota de crédito** | `bancos.nuevo_movimiento?doc=NC` (nuevo) | NC | + |
| **Nota de débito** | `bancos.nuevo_movimiento?doc=ND` (nuevo) | ND | − |
| Transferir entre bancos | `bancos.transferir` (existente) | CH+TR | atómico |

**No confundir "Depositar" (DE) con "Depositar lote" de /cheques.**
- `bancos.nuevo_movimiento?doc=DE` = depósito directo, sólo importe + concepto. Para transferencias recibidas, depósitos en efectivo, ingresos sueltos. NO toca tabla `cheque`.
- `cheques.depositar_lote` = wizard que toma cheques `stat='Z'` en cartera y los manda al banco (lote, cambia stat a B, genera 1 DE por cheque).

**Implementación nueva:**

- `queries.crear_movimiento_simple(no_banco, documento, importe, fecha, concepto, prov, usuario)`:
  - Valida doc ∈ {DE, NC, ND}.
  - Llama `bank_helpers.insert_movimiento_bancario(documento=doc, ...)` — signos los aplica el helper.
  - Registra `mov_doble` tipo `deposito` / `nota_credito` / `nota_debito` con `origen_table=destino_table=transacciones_bancarias` y `origen_id=destino_id=id_transaccion`. Self-link porque no hay contraparte (no es factura/posdat).
  - Atómico en `db.tx()`.
- `queries.reversar_movimiento_simple(id_mov_doble, motivo, usuario)`:
  - Lee el mov_doble + la tx original.
  - Inserta documento opuesto: `DE→CH`, `NC→CH`, `ND→NC` (igual que `bancos.reversar_cheque_emitido`).
  - Registra mov_doble del reverso con `id_original=id_mov_doble` (marca el original como reversado).
- Views nuevas: `bancos.nuevo_movimiento` (GET/POST, query param `?doc=`) + `bancos.confirmar_reverso_movimiento_simple` (GET muestra wizard, POST ejecuta).
- Template nuevo: `bancos/nuevo_movimiento.html` (form simple — banco, importe, fecha, concepto, opcional proveedor para NC/ND).
- Dispatcher `historial.views._REVERSO_DISPATCH` actualizado con los 3 tipos nuevos → todos van a `bancos.confirmar_reverso_movimiento_simple`.

**Regla de UX para futuras pantallas:** todo lo que sea movimiento bancario (DE/NC/ND/CH/TR) se inicia desde `/bancos`. Sidebar tiene un único link "Bancos" — las acciones específicas viven adentro. Lo mismo aplica a otras pantallas hub (Cheques, Compras, Facturas). Si alguien pide "agregar X al sidebar" preguntar si en realidad no tendría que vivir como acción dentro de una pantalla hub.

### Cheques — tabs reordenados + edit estado inline (2026-05-19 v2)

Pedido dueña post-walkthrough. La pantalla `/cheques` ahora tiene este orden de tabs (sin "Todos", sin "CARTERA" suelto):

```
Cartera Z · Postergados · Daniela · Devueltos · Cartera total · Depositados · Eliminados
```

- **Cartera Z** = `STATS["cartera"]` = `("Z",)` — solo cheques recién cargados sin movimiento.
- **Cartera total** = `STATS["cartera_total"]` = `("Z","P","1","2","3","D")` — la suma de los 4 buckets visibles arriba (en mi poder). SIN B (depositados). Antes incluía B; la dueña lo corrigió.
- `cartera_agg` legacy removida — `cartera_total` ahora es lo que era `cartera_agg`.
- Default `estado=` ahora es `cartera` (volvió al pre-2026-04-29; antes era `todos` que no existe más).

**Columnas removidas en `/cheques/lista.html`:**
- Nombre del cliente — solo código de cliente (mismo patrón que `/facturas`).
- `Acum.` (saldo acumulado).
- El pill colorido de estado ("En cartera"/"Depositado"/etc).

**Columna Estado reemplazada por:** raw letter (`Z`/`B`/`1`/`P`/etc) en font-mono dentro de un `<details>` clickeable. Click expande un dropdown con las **transiciones legales** del estado actual. No requiere JS — usa `<details>`/`<summary>` nativo.

**Mapping de transiciones legales** (en `modules/cheques/queries.py:TRANSICIONES_LEGALES`):

| Stat actual | Transiciones permitidas |
|---|---|
| **Z** (cartera) | Depositar→B (wizard), Postergar→P (wizard), Daniela→D (POST), Endosar→E (wizard), Anular→X (wizard) |
| **B**, **A**, **V** (depositado) | Marcar rebote→9 (wizard `confirmar_reverso` con motivo obligatorio). NO puede pasar a Z/P/D — ya salió del cliente. |
| **1**, **2** (rebote en gestión) | Postergar→P, Daniela→D, Anular incobrable→X |
| **D** (Daniela) | Postergar→P, Endosar→E |
| **P** (postergado) | Daniela→D, Endosar→E, Re-postergar→P |
| **3**, **R**, **E**, **X**, **T** | (terminal — sin transiciones; en el template aparece la letra sola sin dropdown) |

**Implementación:** cada entrada del dict tiene:
- `stat_destino`: char destino.
- `label`: texto user-facing en el dropdown.
- `kind`: `"POST"` (form submit a `cheques.transicionar` con `stat_destino`) o `"WIZARD"` (link GET a wizard dedicado).
- `endpoint`: nombre Flask del wizard cuando `kind=WIZARD`.

Si `kind=POST`, el template renderiza un mini-form en la opción. Si `kind=WIZARD`, renderiza un `<a>`. El POST llama a `cheques.transicionar` que ya existe y maneja side-effects.

`depositar_lote` es el único wizard sin `id_cheque` en el path (es global — la dueña elige cheques adentro). El template lo trata especial.

**Por qué dropdown nativo y no Alpine/JS:** `<details>` es accesible (teclado funciona), no requiere bundler, y degrada gracioso. La dueña pidió "comodo" — un click revela las opciones, otro click ejecuta.

### Bancos — vista centrada en el banco actual (2026-05-19 v3)

`/bancos` ahora es un hub centrado en UN solo banco a la vez (default = Pichincha). La dueña trabaja casi siempre desde ahí; tener que elegir banco en cada acción era fricción.

**URL: `/bancos?banco=<no_banco>`** o `?banco=PICHINCHA`/`?banco=INTERNACIONAL` (resuelto por nombre — el `no_banco` varía por instalación). Sin param, default = Pichincha por match `'PICHINC' in nombre`.

**Layout:**
- **Hero**: nombre del banco actual + saldo grande prominente.
- **Botón "Cambiar banco"** (`<details>`) con dropdown listando los otros bancos (con saldos). Click switchea `?banco=<no_banco>`.
- **Action bar**: 5 botones cuya URL incluye `?no_banco={banco_actual.no_banco}` — el form destino pre-selecciona el banco automáticamente.
- **Tabla resumen abajo**: todos los bancos, con badge "Actual" en el activo. Cada otro tiene "Usar este →" que cambia el banco activo. La columna N° se removió (la dueña dijo "el número de banco no importa").

**Templates: `bancos/emitir_cheque.html` y `bancos/nuevo_movimiento.html`** aceptan `?no_banco=N` y lo pre-seleccionan en el `<select name="no_banco">`. Si no viene el param, sigue funcionando como antes (dueña elige a mano).

**Resolver banco por NOMBRE (no por no_banco hardcoded):** el view usa `'PICHINC' in nombre.upper()` y `'INTER' in nombre.upper()`. Esto sobrevive a migraciones de DB donde Pichincha pasó de `no_banco=1` a `no_banco=10`. Cualquier referencia hardcoded a `no_banco=1` es bug (revisar `cheques.boleta_deposito` que ya tuvo este problema).

### Servicios NO se infieren por keyword — el chip V2/V5/V8 es la verdad

**Decisión decisiva 2026-05-19 v5 (revertir intento de v4 de ampliar matcher).**

El universo de "servicios" es abierto (EEQ, CNEL, EMAAP, AGUA, GAS, CNT, INTERNET, FIBRA, CLARO, MOVISTAR, TUENTI, TELEFONO, CABLE, DIRECTV, ALARMA, MONITOREO, …) y cualquier intento de enumerarlos en `KEYWORDS_TO_CATEGORIA` se queda corto. La dueña carga proveedores que nunca van a estar en la lista.

**Estrategia correcta:**

1. **xgast.num del chip V1..V9 es la VERDAD canónica.** Cuando la dueña clickea un chip y carga el gasto, el `num` queda fijo. El matcher de `sugerir_categoria`/`_grupo_concepto` NO se mete con esa fila — el num explícito gana siempre.

2. **Templates de chips V2/V5/V8 = vacío.** Antes pre-cargaban `KK EEQ`/`CC EEQ`/`GAS ADM`. La dueña dijo "concepto libre" — escribe "agua", "luz mayo", "internet claro", "EMAAP", "CNT", lo que sea. Los demás chips (V1/V3/V4/V6/V7) sí mantienen template LC2 nemónico (KKSU, KK, CCSU, CC, SU).

3. **Matcher = solo fallback.** Para gastos cargados sin chip (legacy o entrada manual), `KEYWORDS_TO_CATEGORIA` queda mínimo: solo `KKSU→V1`, `CCSU→V4`, `SU→V7` + reglas dBase clásicas que ya existían. NO se enumeran servicios. Si un xgast queda con num=NULL, aparece como "Sin clasificar" — la dueña lo arregla desde el wizard.

4. **Wizard `/informes/gastos/reclasificar`** — pantalla nueva (`gastos.reclasificar` route + template). Lista todos los xgast sin num agrupados por concepto único, con dropdown V1..V9 por concepto. Submit asigna num masivamente a todas las filas con ese concepto. Idempotente (solo afecta filas con num NULL/0). Agrega marca `[RECLASIF V<n> usr=X]` al concepto.

5. **Banner en `/informes/gastos`** — fila amber al pie con `"$ X en N gastos sin clasificar (M conceptos únicos) — no aparecen en V1..V9"` + botón "Reclasificar →". Para que la dueña vea cuánto está fuera del balance categórico y le sea fácil arreglarlo.

6. **Script `scripts/sugerir_reclasificacion.py`** — CLI con dry-run por default. Lista top N conceptos sin num, o aplica un mapping JSON `{concepto: V}`. Idempotente. Útil para backfill batched.

**Helpers nuevos en `gastos.queries`:**
- `xgast_sin_num_resumen()` → `{n, total, n_conceptos_unicos}` para el banner.
- `xgast_sin_num_por_concepto(limite=200)` → lista para el wizard.
- `reclasificar_concepto_bulk(concepto, num, usuario)` → UPDATE masivo idempotente.

### Audit 2026-05-19 v4 — bugs encontrados y fixed

5 bugs detectados durante el audit extenso de gastos + cheques pedido por Tamara:

**BUG #1 (CRÍTICO) — Atomicidad rota caja+xgast.** `caja.views.nuevo` con `xgast_num` commiteaba la caja primero, después llamaba `clasificar_desde_caja()` en otra tx. Si la 2da fallaba, quedaba caja huérfana → saldo caja descendido + sin gasto registrado.

Fix: refactor `gastos.queries.clasificar_desde_caja` → helper `_clasificar_caja_dentro_tx(conn, ...)`. `caja.queries.crear` ahora acepta `xgast_num` y llama al helper DENTRO de su propia tx. Si clasif falla, rollback total. La función pública `clasificar_desde_caja` (post-hoc, después de caja existente) sigue funcionando como wrapper.

**BUG #2 — Doble contabilización si concepto tiene side-effect prefix + chip V.** Si la dueña tipea "PICH 0123" (transferencia banco) y CLICKEA un chip V, antes se contabilizaba como transferencia Y como gasto.

Fix: en `_clasificar_caja_dentro_tx`, verificar si la caja ya tiene mov_doble activo a `transacciones_bancarias`/`retiros`/`dolares`. Si sí, raise error explicando que no se puede clasificar como gasto (sería doble).

**BUG #3 — Templates V1..V9 inconsistentes con matcher `sugerir_categoria`.** Matrix antes del fix: 5/9 chips matcheaban su V. Divergentes: V1 (`KKSU` → V7), V2 (`KK EEQ` → V5), V4 (`CCSU` → V7), V8 (`GAS` → V9), V7 (`SU` → None).

Fix: reorganizado `KEYWORDS_TO_CATEGORIA` con reglas LC2 explícitas ANTES de las genéricas (KKSU/CCSU/KK+servicios/CC+servicios/SU/GAS ADM). Cambiado template V8 a `GAS ADM ` (en vez de `GAS `). Matrix post-fix: **9/9 chips matchean** + 18/18 conceptos extendidos OK.

Aunque en el flow chip el `xgast_num` se guarda explícito (y por eso el balance siempre cierra), la coherencia template↔matcher era importante para edición manual sin chip y para que el concepto guardado en xgast sea autoreferenciado.

**BUG #4 (CRÍTICO — clase $220K invisible) — Cheques tipo=gasto creaban xgast con num=NULL.** `bancos.queries.emitir_cheque(tipo='gasto')` insertaba xgast SIN num. `gastos_xgast_v1_a_v9_mes()` agrupa por num y solo retorna v1..v9, ignorando v0/NULL. Resultado: todos los gastos pagados con cheque eran invisibles en `/informes/gastos`.

Fix: 
- `emitir_cheque` acepta `xgast_num`. Si viene, lo usa. Si no, intenta inferir vía `sugerir_categoria(concepto)`.
- INSERT INTO xgast ahora incluye `num`.
- Template `bancos/emitir_cheque.html` agrega chips V1..V9 dentro del bloque `campos-gasto` (visible solo cuando tipo=gasto). Mismo patrón que /caja/nuevo.

**BUG #5 — Desalineación TRANSICIONES_VALIDAS (backend) vs TRANSICIONES_LEGALES (UI dropdown).** El nuevo dropdown de edit estado en `/cheques` ofrecía transiciones que el backend rechazaba:
- 1 → P (postergar rebotado): UI sí, backend rechazaba (VALIDAS["1"]={"9","X"})
- 1 → D (Daniela cobra rebotado): rechazado
- 2 → P, 2 → D: rechazado
- D → P (Daniela posterga): rechazado
- P → D: rechazado

Fix: ampliada `TRANSICIONES_VALIDAS`:
```python
"P": {"B","C","X","I","D"},        # + D
"D": {"B","C","X","I","P"},        # + P
"1": {"9","X","P","D"},            # + P, D
"2": {"9","X","P","D"},            # + P, D
```

Casos operativos cubiertos:
- 1→D: cheque rebotó, va a Daniela a cobrar
- 1→P: cheque rebotó, lo postergamos
- D→P: Daniela trae el cheque para posdatar
- P→D: el postergado pasa a Daniela

**Reverso atómico caja+xgast** — además de los 5 bugs, agregada lógica de reverso completo: cuando el mov_doble `caja_s_to_xgast` tiene metadata `atomico_caja_xgast=True` (= vino del flow chip atómico), al desclasificar también se reversa la caja S vía `caja.reversar()`. Si la 2da operación falla, queda como warn (el xgast ya está desclasificado correctamente).

### Caja salida — clasificación V1..V9 inline (2026-05-19 v3)

Pedido dueña: el form de `/caja/nuevo` mostraba un dropdown con conceptos históricos (GAS TAXI BLANCA, CH.LES, etc) que ella encontraba ruidoso. Lo reemplazamos por chips V1..V9 que clasifican el gasto en el momento del alta.

**UI:**
- Atajo "Caja chica / día inhábil" (INHB) **removido** (la dueña no lo usa).
- Datalist de conceptos históricos **removida** (ya no hay autocomplete de strings sueltos).
- Chips top-12 de conceptos históricos **removidos**.
- **9 chips V1..V9** debajo del input concepto, visibles SOLO cuando `tipo=S`:
  - V1 Sueldos tejeduría · V2 Servicios tejeduría · V3 Otros tejeduría
  - V4 Sueldos tintorería · V5 Servicios tintorería · V6 Otros tintorería
  - V7 Sueldos admin · V8 Servicios admin · V9 Otros admin
- Click en un chip:
  - Setea hidden `xgast_num` con el num (1-9).
  - Pre-carga el concepto con un template LC2 (`KKSU `, `CC EEQ `, `SU `, `GAS `, etc) si el campo está vacío.
  - Highlight visual de la categoría elegida.
  - Hay un botón "limpiar" para sacar la clasificación.
- Cuando el tipo cambia a `E` el wrap se oculta y `xgast_num` se limpia.

**Backend (`caja.views.nuevo` POST):** después de crear la caja S, si vino `xgast_num` (1-9), llama a `gastos.queries.clasificar_desde_caja(id_caja, num, usuario)` que crea el `xgast` + el `mov_doble` tipo `caja_s_to_xgast` linkeando ambas filas. Mismo dispatcher de reverso que ya existía.

**Flow correcto end-to-end:**
1. Dueña carga `tipo=S, importe=120, concepto="EEQ MAYO"`, click chip V5.
2. Submit → crea `caja id=N` (egreso real) + llama `clasificar_desde_caja(N, 5)` → crea `xgast num=5` + mov_doble. Atómico.
3. En `/informes/gastos` la fila aparece bajo V5 (Tintorería · Servicios).
4. Reverso desde `/historial`: desclasifica el xgast (lo anula) y deja la caja S libre para re-clasificar.

**Por qué chips en vez de campo "num" dropdown:** la dueña pensaba en términos de "es sueldo tej", "es agua de tintorería" — no en V5. El chip dice ambas cosas (V1 + label). Y los templates LC2 prellenados (`KKSU `, `CC EEQ`) le dan un mnemonic al concepto que ella ya conocía del dBase. La clasificación queda explícita (no auto-inferred del concepto) — pedido suyo.

### Flujo de caja — limpieza y gastos forzados full-width (2026-05-19 v3)

`/informes/flujo`:

- **Leyenda HTML removida** (`.legend` div con cuadrados de color). El chart de Chart.js ya tenía la leyenda nativa desactivada (`legend.display=false`); la HTML duplicaba info que los colores del chart ya comunican.
- **Panel "Editar deudas (posdat banc 0/9)" eliminado** del area debajo del chart. Si la dueña quiere editar deudas viva en /posdat. El modal subyacente (`#modal-deudas`) sigue en el DOM por si en el futuro lo accedemos desde otro botón, pero ningún elemento lo abre.
- **Gastos forzados ahora ocupan full-width** debajo del chart (antes era columna lateral 1/3).
- **Edit inline directo**: cada fila tiene 3 inputs editables (fecha, importe, concepto). El cambio se guarda al `change` event de cada input — sin `prompt()` triple. Helper `_gfSaveCampo(id, campo, valor)` valida y persiste en localStorage. Mucho mejor UX que el prompt-spam anterior.
- Storage: sigue siendo `localStorage` con key `flujo_gastos_forzados_v1` (sin migración).

### Provisiones se editan desde /posdat (2026-05-20)

Pedido dueña: "modificar que las provisiones se puedan cambiar tanto el importe como la cuota mensual directamente en posdatados, esto permite eliminar el item Provisiones en el menu principal".

**Cambios:**
- `/posdat/lista.html`: sale columna **Proveedor (nombre)**, entra columna **Cuota mensual** editable inline. La columna "Prov" (código) se mantiene.
- `posdat.queries.buscar()`: LEFT JOIN LATERAL contra `scintela.provisiones` por **match aproximado de concepto** (case-insensitive, trim, longitud ≥ 3, starts-with bidireccional). Trae `id_provisiones`, `cuota_mensual` (= `provisiones.importe`), `provision_periodo`.
- El inline edit POSTea al endpoint existente `/provisiones/_api/<id>/quick-edit` con `{ importe: N }`. NO se creó endpoint nuevo — reuse del que ya servía la pantalla /provisiones.
- **Menú**: link "Provisiones" sale del sidebar (`templates/base.html` sección "Datos base"). La URL `/provisiones` sigue accesible por compatibilidad, pero no es destino visible.
- **CSV export de posdat**: igual que la UI — sale `proveedor`, entra `cuota_mensual`.

**Modelo mental:**
- La columna `Importe` de posdat es el **corrido del día** (suma diaria que va aplicando `correr_provisiones_diarias`).
- La columna `Cuota mensual` es el **valor base** de la provisión (lo que se va a cargar al final del mes).
- El día 1 → importe ≈ 0, el día 30 → importe ≈ cuota_mensual.

**Por qué LATERAL en vez de JOIN simple:** el LATERAL permite ORDER BY LENGTH(concepto) DESC + LIMIT 1 dentro del subquery — así cuando una provisión "ALQUILER" y otra "ALQUILER OFICINA" matchean al mismo posdat "ALQUILER OFICINA NORTE", gana la más larga (más específica). Sin el LATERAL hay que correlar con un GROUP BY que se vuelve caro.

### Cheques — dropdown de estado al alta (2026-05-20)

`/cheques/nuevo` (Nueva Cobranza) ahora pide explícitamente el estado del cheque por bloque. Antes el `stat` se calculaba a partir de `fechad > fecha` (postdatado implícito), pero la dueña quiso elegirlo a mano.

**Dropdown por cheque (col 3 del bloque, después de N°):**
- `Z` cartera (default)
- `P` postdatado
- `D` Daniela (gestión cobranza)
- `B` depositado
- `X` anulado
- `1`/`2` devuelto

**Regla UX:** la fecha "A depositar" (`fechad[]`) está **disabled por default**. Sólo se habilita cuando estado=P, y entonces se vuelve **required**. Para el resto de estados, fechad colapsa a la fecha de recibido (queries.crear lo expande con `fechad or fecha`).

**Backend:** `views.nueva()` lee `stat[]` como lista paralela a `importe[]`/`no_cheque[]`/`fechad[]`. Cada `ch_in` ahora carga `stat`. Se valida que stat='P' venga con fechad explícito; el resto pasa libre. La llamada a `queries.crear()` propaga `stat=ch_in.get("stat") or "Z"`.

**Side-effects existentes que se respetan:**
- `no_banco IN (90, 91, 99)` sigue forzando stat='B' si el usuario eligió Z (no si eligió P/D/X/1/2 explícito).
- `no_banco == 97` sigue forzando `es_anticipo=True`.
- stat='V' sigue prohibido al alta (legacy banco Internacional).

### Tooltip del flujo no duplica saldo (2026-05-20)

`flujo_grafico.html` tenía 2 datasets en el Chart.js (saldo + dots de egresos). El callback `label` se llamaba 1 vez por dataset → tooltip mostraba "Saldo: $X" duplicado.

Fix: `label: ctx => ctx.datasetIndex !== 0 ? null : 'Saldo: $ ' + fmt(row.saldo)`. El dataset de dots devuelve null y Chart.js no agrega esa línea al tooltip.

### Cala Cali — fix tipo (migración 0036)

`scintela.activos`: el terreno "CALA CALI" estaba cargado con `tipo='I'` (intangible) cuando es un terreno físico. Migración `0036_fix_tipo_cala_cali.sql` lo pasa a `tipo='T'`. Filtro defensivo: sólo actualiza filas donde `tipo='I' AND UPPER(concepto) LIKE '%CALA CALI%'`. Idempotente (la 2da corrida ya no encuentra filas con tipo='I' matcheando).

### Cuota diaria también en /posdat (2026-05-20 patch)

Sobre la fila de "cuota mensual" agregada arriba, la dueña pidió: "agrega cuota diaria tambien que se calcule en posdatatos cuando lleno algo".

**Columna nueva "Cuota diaria"** al lado de la mensual:
- SQL: `ROUND((COALESCE(pr.importe, 0) / 30.0)::numeric, 2) AS cuota_diaria` en `posdat.queries.buscar()`.
- Template: input editable bidireccional con la mensual.

**Regla de cálculo: 30 días por mes** (consistente con `provision_pendiente_mes` y MENU.PRG L275). NO usamos calendar days reales — 30 es la convención dBase original.

**Bidireccionalidad en el JS:**
- `input` event en la mensual → recalcula la diaria visualmente (sin POST).
- `input` event en la diaria → recalcula la mensual visualmente.
- `change` event (blur / Enter) en CUALQUIERA → POST único a `/provisiones/_api/<id>/quick-edit` con `{importe: mensual}`. Sólo la mensual viaja al backend; la diaria es derivada.

Por qué no agregar campo `cuota_diaria` separado en `scintela.provisiones`: redundante. La regla mensual / 30 es invariante del modelo, y persistir ambas abre la puerta a inconsistencias (cuota_mensual=300, cuota_diaria=15 → cuál gana?). Una fuente de verdad: `provisiones.importe` (= cuota mensual).

### Importe (total) editable inline en /posdat (2026-05-20)

Pedido dueña: "dejame ingresar le total en posdatados". El importe (total) era read-only hasta ahora.

**Cambios:**
- `lista.html`: la celda "Importe" ahora trae un `<input type="number">` editable.
- `posdat.views.api_editar` (`/posdat/_api/<id>/editar`): aceptaba updates parciales en estructura pero **exigía concepto siempre**. Ahora: si el caller no manda la key `concepto`, conserva el de la fila (chequeo `"concepto" in data.keys()`). Si la manda vacía, sigue error.
- JS: postea sólo `{importe: n}` al endpoint. `dataset.idPosdat` se lee de la `<tr>` (que ahora tiene `data-id-posdat`).

**Regla:** importe debe ser > 0. Para crear ajustes negativos / anticipos hay que ir al form completo (la inline-edit rechaza con alert).

### Script de deploy a GitHub (2026-05-20)

`scripts/push_to_github.sh` — pedido dueña "madame script para subir a github". Defensivo: aborta si no estás en main, muestra el diff y pide confirmación, hace `git pull --rebase` antes del push para no romper si alguien pusheó antes. Imprime recordatorio post-push de correr las migraciones a mano (no se corren solas con git pull).

Uso: `./scripts/push_to_github.sh "mensaje opcional"`.

### Depósito inline de cheques desde /cheques (2026-05-20)

Pedido dueña literal: "Necesitamos agilizar el proceso de los depósitos. Si estoy parado en cheques de clientes. Tener un filtro que dia hoy. Después poder seleccionar, y una vez que pongo depositar lote, se depositan los seleccionados en la pantalla principal. No hace falta una segunda pantalla. (...) Cuando el cheque se deposita, tengo que ver que el saldo subió en el banco".

**Flujo nuevo (sin segunda pantalla):**
1. En `/cheques`, click el chip "Hoy" → setea `desde=hasta=today` y submitea el form (JS local, no requiere endpoint).
2. Aparecen checkboxes a la izquierda de cada fila — sólo en cheques con stat Z (cartera) o P (postergado). El resto NO muestra checkbox (no se puede depositar algo ya depositado / anulado).
3. Al tildar al menos uno, aparece una **barra flotante verde** abajo del centro con: count, total, date picker (default hoy), botón "Depositar lote en Pichincha".
4. Click "Depositar" → confirm si son >1, POST a `/cheques/_api/depositar-lote`.
5. Banner verde arriba del centro: "✓ N cheques depositados en Pichincha · Saldo $X → $Y (+$Z)" + link a la boleta.
6. Auto-reload en 2.5s para refrescar conteos por tab.

**Endpoint nuevo: `POST /cheques/_api/depositar-lote`** (views.api_depositar_lote).
- Acepta JSON `{ids: [int], no_banco?: int, fecha?: 'YYYY-MM-DD'}`. Sin no_banco, fallback a Pichincha por nombre (mismo que el wizard clásico).
- Reusa `queries.depositar_lote()` — toda la lógica transaccional (UPDATE cheque + INSERT transacciones_bancarias + chequextransaccion + mov_doble) está intacta.
- Lee `bank_helpers.saldo_actual(no_banco)` ANTES y DESPUÉS del depósito, devuelve ambos para que la UI muestre el delta.
- Devuelve también `boleta_url` por si la dueña quiere imprimirla.

**Por qué el filtro "Hoy" es client-side:** no requiere endpoint nuevo — el form de filtros ya postea `desde`/`hasta`. Sólo seteamos los inputs y submitt. Más barato que un endpoint dedicado.

**Coexistencia con el wizard viejo:** la pantalla `/cheques/depositar-lote` sigue funcionando (no se eliminó). Quien prefiera el flujo clásico (lo abre desde el botón verde "Depositar lote" del header) sigue teniéndolo. La barra flotante es el camino feliz para 1-N cheques rápidos.

### Bash 3.2 compat en push_to_github.sh (2026-05-20)

macOS viene con bash 3.2 (Apple no actualiza por GPL3). `${VAR,,}` (lowercase expansion) es bash 4+, falla con "bad substitution". Se reemplazó por `printf '%s' "$RESP" | tr '[:upper:]' '[:lower:]'` que es portable. Recordatorio para futuros scripts: en mac NO usar `${VAR,,}`, `${VAR^^}`, `${VAR:N:M}` con offsets negativos. Sí podés usar arrays asociativos en scripts internos siempre que TODOS los users tengan bash 4+ (la EC2 tiene Windows, no aplica). Para .sh que toca corren tanto en mac como en EC2 (vía git bash) → POSIX-friendly.

### Fix urgente /posdat 500 (2026-05-20)

Error ID `61ea4d2e` en prod. El `LEFT JOIN LATERAL` que agregué al SELECT de `posdat.queries.buscar()` fallaba en RDS (la combinación de `%%` escape en psycopg2 + LATERAL + ORDER BY dentro del subselect rompió de un modo que localmente no se replicó).

**Fix:** sacar el JOIN, hacer el match a `scintela.provisiones` en Python:

```python
provisiones_lookup = db.fetch_all("SELECT id_provisiones, concepto, importe, periodo_aplica FROM scintela.provisiones")
# Ordenar por longitud DESC para que el más específico gane
provisiones_lookup.sort(key=lambda p: len(p.get("concepto") or ""), reverse=True)

for r in rows:
    match = _match_provision(r.get("concepto") or "")
    r["cuota_mensual"] = float(match["importe"]) if match else None
    r["cuota_diaria"]  = round(r["cuota_mensual"] / 30.0, 2) if match else None
    r["id_provisiones"] = match.get("id_provisiones") if match else None
```

El lookup es <20 filas, una query extra por request. Trade-off: mucho más simple, más defensivo (try/except en el fetch_all envuelve la tabla missing), y los tests son triviales (matcheo de strings en Python). El LATERAL JOIN dejaba demasiada magia oculta en SQL que no se replicaba bien entre entornos.

**Regla aprendida:** psycopg2 + LATERAL JOIN + `%%` escape literals → frágil. Si el match es chiquito (<100 filas), preferir join en Python.

### Drag-and-drop reorden manual en /activos (2026-05-20)

Pedido dueña: "Dejame drag and drop en activos. porque asi lo ordeno manualmente".

**Migración 0037**: agrega `orden_manual INTEGER` a `scintela.activos` + index `idx_activos_orden_manual`. Idempotente (DO block + IF NOT EXISTS).

**`activos.queries.buscar`**: ORDER BY ahora arranca con `a.orden_manual NULLS LAST,` antes del orden canónico (categoría → fecha DESC). Filas con `orden_manual` NULL caen al final con el orden de siempre.

**Feature flag `_tiene_orden_manual()`**: cache module-level (chequea `information_schema.columns` una vez por worker). Durante el gap deploy→migrate, la query se construye sin las referencias a `orden_manual` y todo sigue funcionando. Restart-task reseta la cache.

**Endpoint `POST /activos/_api/reordenar`** (`activos.api_reordenar`): JSON `{ids: [int]}` con los IDs en el NUEVO orden visible. El handler hace UPDATE de `orden_manual = índice + 1` para cada uno, todo en una sola tx. Permission gate: `activos.crear` (mismo rol que ya puede editar activos).

**Frontend (HTML5 nativo, sin libs externas)**:
- Cada fila draggable tiene `draggable="true"` + handle `⋮⋮` en una columna nueva.
- `dragstart` marca la fila origen con `.dragging` (opacity 0.4).
- `dragover` calcula si el cursor está arriba/abajo del centro de la fila target → muestra una línea sky-500 en el borde top o bottom (`.drop-above` / `.drop-below`).
- `drop` reordena el DOM con `insertBefore` y POSTea `persistirOrden()` con la lista de IDs.
- Banner verde "Orden guardado ✓" arriba del centro durante 2s.
- **Headers de categoría se ocultan** cuando hay AL MENOS UNA fila con `orden_manual` seteado (porque el orden manual cruza categorías y los headers quedarían mal). En "modo canónico" (todo NULL) siguen apareciendo igual que siempre.

**Por qué HTML5 nativo y no Sortable.js**: el caso es chico (<200 filas), no hay sub-listas, no hay groups, no hay nested. Native DnD pesa 0KB extra. Si en el futuro se vuelve complejo (grupos, multi-select, mobile-touch), Sortable.js está al alcance de un CDN.

**Caveat mobile**: HTML5 DnD nativo no anda en mobile táctil. Si la dueña reordena desde tablet/celular, vamos a tener que agregar Sortable.js (que sí cubre touch). Por ahora, este caso es desktop-only.
