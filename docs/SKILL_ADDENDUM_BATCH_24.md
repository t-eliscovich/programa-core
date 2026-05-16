# Skill addendum — batch 24 (2026-05-06)

Pegar al final de `SKILL.md` del skill `programa-core` (la próxima sesión que tenga acceso al folder de skills se encarga, igual que hicieron los addendums previos 9-23).

---

## Session notes — 2026-05-06 (batch 9: anticipos + resultados — UI moderna y bug MP=0)

> Nota: aunque el archivo se llama "batch 24" para no pisar los addendums previos en `docs/`, dentro del SKILL.md va anclado a la cronología de batches (siguiente al 8 — la sesión anterior real fue 2026-04-17 batch 8 procesa_provisiones). En el flujo del paper trail interno del skill esta sesión es la **batch 9 conceptual** después de un mes de pausa.

Sesión corta enfocada en pulir dos pantallas que el gerente mencionó: Anticipos (`/dolares`) — que era una tabla plana sin jerarquía visual — y el panel **Resultados** del balance (`/informes/balance`) — que mostraba MAT.PR. en 0 cuando el mes recién arranca.

### Bug reportado por TMT y arreglado

**Síntoma:** "materia prima está en 0" en el panel Resultados al inicio de mes, aún cuando hay objetivo cargado en `iniciales`.

**Causa:** En `modules/informes/queries.py::informe_balance()`:
```python
proy_mp = kgpro * umx if umx else 0.0
```
`umx = h_ucom / h_kcom` (U$/kg de las compras tipo='H' del mes). Cuando aún no hay compras del mes, `umx == 0` y por tanto `proy_mp == 0`. Misma trampa en `proy_uvent` y `proy_col`.

**Fix:** fallback a las tarifas objetivo de `scintela.iniciales` (`um`, `uk`, `uq`, `pre`). Helper local:
```python
def _eff_rate(live: float, meta: float) -> tuple[float, str]:
    if live and live > 0:  return live, "live"
    if meta and meta > 0:  return meta, "meta"
    return 0.0, "none"
precio_eff, precio_src = _eff_rate(precio, inic_pre)
um_eff,     um_src     = _eff_rate(umx,    inic_um)
uq_eff,     uq_src     = _eff_rate(iqx,    inic_uq)
proy_uvent = kgpro * precio_eff
proy_mp    = kgpro * um_eff
proy_col   = kgpro * uq_eff
```

**Cambio de contrato en `resultados.costos`:** lo que era un tuple `(label, kg, ukg, us, proy)` ahora es un dict `{label, kg, ukg, us, proy, src, ayuda}`. El template ya consume el dict; cualquier consumidor nuevo NO debe iterar como tupla. Único consumidor encontrado en grep: `modules/informes/templates/informes/balance.html:141` (actualizado en este batch).

### Resultados — modernización del panel

`modules/informes/templates/informes/balance.html` — panel izquierdo. Cambios sin tocar la columna ACTIVO/PASIVO:

1. **Barras de progreso act/proy** en las celdas U$ act de Venta, cada COSTO, TOTAL costos y UTILIDAD. Verde ≥80% del objetivo, ámbar 30–80%, slate <30%. La de TOTAL costos cambia a ámbar cuando el costo *supera* la proyección (≥100%) — costos que se van arriba del plan no son alarma roja todavía pero hay que verlos.
2. **Chips `meta` / `sin datos`** al lado del U$/kg cuando la tarifa salió de iniciales en lugar del mes en curso. Ámbar para meta, rojo claro para "ni live ni meta" (= configurador de iniciales lo dejó en blanco).
3. **Tooltips `title` con `c.ayuda`** en cada label de costo: explica qué es cada concepto en una línea. MAT.PR. = "compras tipo H — hilado importado", etc.
4. **Estilos nuevos en `head_extra`**: `.pbar`, `.pbar-ok/warn/empty`, `.src-chip`, `.src-meta`, `.src-none`. Se mantienen las tabular-nums y las clases pre-existentes (`seccion-header`, `fila-dato`).

### Anticipos — rediseño completo

Antes: tabla plana con filtros y dos KPIs en línea. Después: pantalla en tres bandas.

1. **4 KPI hero cards**: Total vivo (verde, suma al balance) / Partidas vivas / Cuentas con saldo / Histórico aplicado. Grid 2 cols mobile, 4 cols desktop.
2. **Cards por cuenta — top 12** ranked por `total_vivo DESC`. Cada card: código de cuenta (chip mono), nombre del cliente, $saldo (verde, énfasis), `desde` (fecha del primer anticipo vivo), barra horizontal proporcional al saldo máximo. Card linkea a `?cta=XX&solo_vivos=1` para drill-down.
3. **Detalle**: la tabla original con filtros (cta/desde/hasta/solo_vivos) en una caja con borde + cabecera "DETALLE — N movimientos". Ahora con pills (`vivo` verde, `aplicado` slate), código de cta como chip clickeable que filtra a esa cuenta, y un botón "Limpiar" cuando hay filtros activos.

### Query nueva

`modules/dolares/queries.py::por_cuenta(solo_vivos=True)`:
- Agrega por `UPPER(TRIM(cta))`, suma total_vivo y total_aplicado, cuenta vivos/aplicados, devuelve `ult_fecha` y `primer_vivo`.
- `HAVING NOT solo_vivos OR n_vivos > 0` filtra cuentas sin saldo vivo cuando `solo_vivos=True`.
- Una sola query, no N+1.

### Lookup de nombres de clientes

`modules/dolares/views.py::_nombres_clientes(codigos)` — `WHERE UPPER(TRIM(codigo_cli)) = ANY(%s)` con la lista completa de códigos de las cuentas vivas. **Patrón a reusar** cuando una pantalla tiene que enriquecer N códigos con nombres: una sola query con `= ANY(%s)`, dict {cta: nombre}, asignar a las filas en Python. Evita N+1 cuando un join no es trivial (acá se complica porque cta es UPPER+TRIM y el codigo_cli puede tener whitespace).

### Verificación

- `ruff check .` → All checks passed (tras eliminar `inic_uf` que se setteaba pero no se usaba — ya hay `h_uf` más abajo en el bloque STOCK).
- `pytest tests/ --ignore=tests/test_import_dbf.py` → **471 passed, 9 skipped** (los skipped son DB-dependent integration tests).
- Smoke walk: `python tests/test_routes_smoke.py` → **70 GET routes walked, 0 failures**.
- Render directo de `dolares/lista.html` con stub `g.user`/`g.permisos` → 31.993 chars, OK.
- Compilación de Jinja: ambos templates parsean limpios via `app.jinja_env.compile(...)`.

### Invariante nuevo

**Cuando una proyección depende de una tarifa U$/kg que sale de datos del mes en curso, SIEMPRE hay que tener un fallback a la tarifa objetivo de `scintela.iniciales`.** Sin esto, cualquier pantalla de proyección "se cae a 0" los primeros días del mes y al gerente le aparece como bug. El patrón canónico es el helper `_eff_rate(live, meta) → (valor, src)` y exponer `src` en el dict de salida para que el template pueda mostrar el chip "meta".

### Files touched this batch

```
modules/informes/queries.py                              + _eff_rate, fallback inic_um/uk/uq/pre,
                                                           cambio costos a dict {label, kg, ukg, us, proy, src, ayuda},
                                                           tarifas_src en resultados
modules/informes/templates/informes/balance.html         + head_extra: .pbar, .src-chip + variantes,
                                                           barras de progreso en Venta/Costos/TOTAL/Utilidad,
                                                           chips meta/sin-datos en U$/kg de cada costo,
                                                           tooltips ayuda en label,
                                                           iteración como dict en lugar de tuple
modules/dolares/queries.py                               + por_cuenta(solo_vivos)
modules/dolares/views.py                                 + _nombres_clientes (anti-N+1),
                                                           pasa cuentas y nombres al template
modules/dolares/templates/dolares/lista.html             rewrite: KPIs hero, cards por cuenta, pills,
                                                           detalle con cabecera y filtro "Limpiar"
```

### Pendientes que aparecieron y NO se hicieron

- **Eliminar el orphan `modules/informes/templates/informes/anticipos.html`** — no está wireado en ningún view, sólo se referencia a sí mismo. No es urgente pero es ruido. Limpiar próxima sesión que toque `modules/informes/`.
- **Mostrar el panel Resultados como bandera sticky/standalone**: el panel ahora vive dentro del balance grande. Si el render del balance se vuelve pesado, separar permite cachear cada panel por su lado.
- **STOCK por etapa** sigue mostrando datos del último cierre (`historia.hilado/tejido/terminado`) o fallback a iniciales. Para "live stock en kg" hay que reemplazar por compras-tinto-ventas del mes (Fase 2.2 del backlog).

### Bug bonus arreglado este batch — RETIROS.DBF no se sincronizaba

Reportado por TMT mid-sesión: "RETIROS, WHICH ACCOUNTS FOR DIVIDENDOS DID NOT UPLOAD OR IS NOT WORKING. I SEE NOTHING FOR MAY".

**Causa:** `RETIROS.DBF` existía en `/Users/tamaraeliscovich/Documents/INTELA copy/Files/` (44 KB, fresco con datos de mayo 2026) pero **`scripts/import_dbf.py` no tenía mapper ni entry en `TABLE_MAP` para él**. La regla del importer es "si el DBF no está, no se toca la tabla" — pero acá el DBF estaba presente y simplemente lo ignoraba. Postgres `scintela.retiros` quedaba congelado en el dump original (último cierre) y el gerente veía 0 retiros para mayo.

**Fix:**
1. `_map_retiros(r)` nuevo en `scripts/import_dbf.py`. Campos DBF: FECHA(D) NB(N4) RET(N10) DE(C2) CONCEPTO(C15) CLAVE(C1). Mapeo directo a `scintela.retiros`. `de` y `clave` se truncan a 5 chars (varchar(5) en Postgres). `nb` puede ser None u 0.
2. Entry `RETIROS.DBF` en `TABLE_MAP` con criticidad `CRITICO` y descripción "Retiros del dueño (dividendos / RR) — URET en balance".
3. `docs/RUNBOOK_sync_dbf.md` actualizado: nueva fila en la tabla de mapping, removido de "no mapeados".
4. `tests/test_import_dbf.py`: 2 tests nuevos (`test_retiros_mapper_caso_real`, `test_retiros_mapper_acepta_nb_none`) + el existente `test_table_map_tiene_los_dbfs_criticos_para_balance` ahora exige RETIROS.DBF en la lista de críticos.

**Smoke test contra el DBF real:** 1076 registros parseados, 5 entries de mayo 2026 (FE, VI, TA, AM, OP) — los dividendos del gerente ahora están listos para entrar.

**Para que TMT vea los datos en pantalla:** correr `python scripts/import_dbf.py --only=RETIROS.DBF` (o `make sync-dbf` que toca todos los DBFs presentes). El truncate+insert es transaccional e idempotente — si la corrida vuela, no queda en estado mixto.

**Invariante reforzado (regla operativa):** cuando aparezca un módulo en el sidebar que muestre "no hay datos del mes" o el gerente reporte que faltan datos recientes, **chequear primero `scripts/import_dbf.py::TABLE_MAP`** antes de asumir bug en queries. La pantalla puede estar perfecta y el script de sync puede simplemente no conocer el DBF.

### Bug bonus 2 — TINTO.DBF tampoco se sincronizaba

Al auditar el folder de DBFs después del fix de RETIROS, descubierto que **`TINTO.DBF` (82 registros frescos de mayo 2026) tampoco estaba en `TABLE_MAP`**. Es la causa raíz directa de COL.QUI. = 0 en el screenshot que TMT mandó al inicio de la sesión.

`scintela.tinto` alimenta `tinto_mes_corriente_resultado()` (`modules/informes/queries.py:474-491`), que es la fuente de **COL.QUI.** (= ITIN, suma de importe), **KR** (= SUM kgn donde color<>'LAV', kg terminados), y **KTINT** (= SUM kg donde color<>'LAV') del panel Resultados. Sin sync, los 3 quedaban en 0 aunque el dBase tuviera batches del día.

**Fix:**
1. `_map_tinto(r)` nuevo en `scripts/import_dbf.py`. 21 columnas: FECHA, COD, COLOR, FRANELA…KIANA (12 columnas de kg por tipo de fabric), TIPO, KG, KGN, IMPORTE, STAT, CLAVE.
2. **Trampa con `tipo`**: en Postgres `scintela.tinto.tipo` es `integer`, en el DBF es `C(1)` siempre vacío (`''`). `_int()` ya devuelve None para strings vacíos, así que mapea limpio. Si algún día meten un valor (e.g. '1'), `_int` lo coerciona. Si meten algo no parseable, `_int` devuelve None y la inserción no falla.
3. Entry `TINTO.DBF` → `scintela.tinto` con criticidad `CRITICO`.
4. `docs/RUNBOOK_sync_dbf.md` actualizado.
5. `tests/test_import_dbf.py`: nuevo `test_tinto_mapper_caso_real` + el critic-list test ahora exige `TINTO.DBF`.

**Smoke test contra el DBF real:** 82 records mapeados, 100% del mes de mayo 2026, total **48.794 kg / $32.685** que ahora pueden poblar COL.QUI. + KR. La proyección iniciales para COL.QUI. era $418k — el ratio está bajísimo este mes pero al menos ya no será 0.

**¿Por qué TRUNCATE+INSERT es seguro acá?** `scintela.tinto` se lee únicamente por la query `tinto_mes_corriente_resultado()` que filtra por `fecha >= date_trunc('month', CURRENT_DATE)`. La fuente histórica canónica de tinto vive en `formulas_app` (otra base, otro schema). El DBF que llega al folder TINTO.DBF es ya el window del mes en curso, así que truncar y re-insertar coincide con lo que la app usa. Confirmado por grep en `modules/`: un solo reader.

### Auditoría completa del folder de DBFs (2026-05-06)

Lo que hay vs. lo que se importa, después de batch 9:

| DBF | Tamaño | Registros | Estado |
|---|---|---|---|
| FACTURAS.DBF | 338K | mucho | ✓ mapeado |
| CHEQUES.DBF | 107K | mucho | ✓ mapeado |
| DOLARES.DBF | 103K | mucho | ✓ mapeado |
| RETIROS.DBF | 44K | 1076 | ✓ mapeado (batch 9) |
| INICIALE.DBF | 40K | — | ✓ mapeado |
| HISTORIA.DBF | 38K | — | ✓ mapeado |
| PICHINCH.DBF | 33K | — | ✓ mapeado |
| COMPRAS.DBF | 32K | — | ✓ mapeado |
| FLUJO.DBF | 22K | — | ✓ mapeado |
| CAJA.DBF | 20K | — | ✓ mapeado |
| ENTRADAS.DBF | 15K | 646 | ✗ **intencional** — marcaje horario empleados, alcance de `formulas_app` |
| POSDAT.DBF | 10K | — | ✓ mapeado |
| TINTO.DBF | 7K | 82 | ✓ mapeado (batch 9) |
| XGAST.DBF | 7K | — | ✓ mapeado |
| ACTIVOS.DBF | 4K | — | ✓ mapeado |
| INTER.DBF | 0.5K | — | ✓ mapeado |
| RETEN.DBF | 0.5K | 0 | ✗ **intencional** — sólo header dBase, retenciones se manejan vía `scintela.retencion` desde compras |

Ningún DBF financiero queda fuera. ENTRADAS y RETEN están explícitamente excluidos por scope/data, no por olvido.

### Bug bonus 3 — TEJIDO kg = 0

Reportado por TMT: "tejido sigue diciendo 0".

**Causa:** En `informe_balance()`:
```python
cost_tej_kg = iprov["kg"]   # KK ≈ kg tejido tercerizado del mes
```
Esto venía del PRG legacy donde la celda kg de TEJIDO sólo mostraba kg tercerizado (compras tipo='K'). El U$ sumaba ambos (gastos planta + tercerizado), pero el kg dejaba afuera todo lo INTELA-internal. Cuando no hay tercerización ese mes, kg=0 aunque el U$ tenga $36k de gastos planta — el row queda visualmente roto.

**Fix:**
1. **Live**: `cost_tej_kg = (tin.kr or 0) + (iprov.kg or 0)`. KR es kg que llegan a terminado este mes (pasaron por tejido), aproximación más cercana a "kg tejidos vivos".
2. **Fallback**: `iniciales.tejido` (objetivo kg del mes) si live = 0.
3. **Helper `_eff_rate(live, meta) → (valor, src)` movido a nivel módulo** (`modules/informes/queries.py` line 1062) para que el bloque COSTOS pueda usarlo. Antes era nested function dentro de `informe_balance` definida AFTER el bloque COSTOS, lo que hacía imposible reusarla más arriba en el mismo función.

El src de la fila TEJIDO ahora compone `kg.src` con `us.src`: si el U$ es live ('live'), si no chequea proy + el kg-fallback del campo (`meta` si vino de iniciales.tejido aunque sea), si no 'none'.

**Patrón general aplicable a otras filas COSTOS en el futuro:** cualquier celda que combine "datos del mes" + "objetivo de iniciales" debe pasar por `_eff_rate()` y exponer `src` para que el chip "meta" del template lo señale al gerente.

### Bug bonus 4 — TEJIDO kg DEBÍA SER 66.069 (no 0, no 47.580)

Después del fix anterior TEJIDO seguía mostrando 0 porque el bug real estaba en otro lado. Tras auditar `COMPRAS.DBF` con datos frescos de mayo (TMT subió todos los DBFs), descubierto:

**Las 15 compras `tipo='K'` de mayo tienen TODAS `PROV='KK'`.** Y el filtro legacy `compras_iprovk_mes()` excluye `PROV='KK'` con un `WHERE prov<>'KK'` — específicamente diseñado para sólo mostrar tercerizado externo.

Pero la convención del dBase es más sutil:

| `tipo='K'` row | Significado | ¿Aporta a kg de TEJIDO? | ¿Aporta a U$ de TEJIDO? |
|---|---|---|---|
| `PROV='KK'` AND `KG>0` | kg tejidos por la **planta INTELA** (auto-cargado, "compra a sí mismo" del dBase). El concepto es la fecha como texto: "01/05         4". | **SÍ** ← este era el faltante | NO (importe siempre 0) |
| `PROV='KK'` AND `IMPORTE>0`, KG=0 | gastos varios cargados al rubro tejido (DHL, almuerzos, mascarillas, etc.) | NO | **SÍ** |
| `PROV<>'KK'` AND `KG>0` | tercerizado externo (otra planta) | SÍ | SÍ |

**Datos reales mayo 2026:**
- Interno (KK con KG>0): **66.069 kg / $0** ← lo que TMT esperaba ver
- Tercerizado externo: 0 kg / $0
- KK gastos varios: $4.013

**Fix:**
1. **Nueva query `tejido_mes_componentes()`** descompone los tres componentes en una sola consulta agrupada por `quien` (KK vs OTRO).
2. **`compras_iprovk_mes()` se mantiene** como compat para callers externos pero el panel ya no la usa.
3. **`informe_balance()` cambia el cálculo de TEJIDO**:
   ```python
   cost_tej_kg = tej["kg_interno"] + tej["kg_externo"]  # = 66.069 + 0 = 66.069 ✓
   cost_tej_us = (
       gxg["gtej_sin_dtj"] + amort["dtj"]              # gastos planta + amort
       + tej["us_externo"] + tej["us_kk_gastos"]         # externo + gastos KK
   )
   ```
4. **El detalle del cálculo se expone via `costos[i].detalle`** para que el template pueda mostrarlo en modo debug o tooltip expandible.

**Predicción del panel después del fix:** TEJIDO `kg=66.069`, `U$=$39.537` (= V1+V2+V3 $32.564 + DTJ $2.960 + us_externo $0 + us_kk_gastos $4.013), U$/kg = $0,598.

### Bug bonus 5 — MAT.PR. ahora usa promedio ponderado del stock

Pedido directo de TMT: "podemos buscar el costo promedio? osea el del stock, no el de compras?".

Antes el U$/kg de MAT.PR. era el ratio de las compras del mes — volátil, se va a 0 en meses sin compras y un proveedor caro lo distorsiona.

**Nuevo: `costo_promedio_mp_ponderado(h_kcom, h_ucom, hist, inic)`** implementa el weighted average contable estándar:

```
ukg_promedio = (stock_anterior_us + compras_mes_us)
             / (stock_anterior_kg + compras_mes_kg)

stock_anterior_kg = historia.hilado (kg al último cierre)
tarifa_anterior   = iniciales.um (target, preferido sobre el ratio del mes
                    para evitar circularidad)
stock_anterior_us = stock_anterior_kg × tarifa_anterior
```

**Devuelve `src`** que distingue entre:
- `'stock'` — usamos los dos lados (stock + compras) → número estable y representativo
- `'compras'` — sólo hay compras (cierre anterior sin hilado)
- `'stock_only'` — sólo hay stock (mes sin compras)
- `'none'` — ninguno

**El template muestra el chip `meta` cuando `src ∈ {'compras', 'stock_only'}`** porque en esos casos no estamos viendo un promedio "real" sino sólo un lado de la ecuación.

**Detalle expuesto** en `costos[mat_pr].detalle` con todos los componentes (stock_anterior_kg, tarifa_anterior, compras_kg/us/ukg, kg_disponible, us_disponible, ukg_ponderado) para que el gerente pueda auditar el cálculo desde la pantalla.

**Lección general (= invariante nuevo):** los promedios de costo en un sistema de manufactura **siempre** deben ser ponderados sobre todo el stock disponible, no sobre el flujo del mes. El flujo dice "qué se compró este mes a qué precio"; el ponderado dice "cuánto vale el material que tenés ahora". Para decisiones (¿conviene esta venta? ¿qué margen tiene este producto?), el segundo es el correcto.

### Bug bonus 6 — TOTF (cartera de facturas) en $5.21M cuando dBase mostraba $4.92M

Reportado por TMT 2026-05-06 con número exacto: "el numero esta cerca de 4.916.203 nosotros tenemos 5.2 millones, fijate bugs o que no restamos/sumamos."

**Causa:** la query `totf()` filtraba `WHERE saldo > 0`, excluyendo **664 facturas con saldo NEGATIVO** ($-293.923,87 total). Saldo negativo = `abono > importe` = el cliente sobrepagó. En la convención dBase legacy `SUM ALL SALDO TO TOTF FOR STAT $ "ZA"` (PRG línea 27), TOTF es la cartera **NETA** — los sobrepagos restan automáticamente porque suman negativo.

**Fix:** removido el filtro `saldo > 0` de `totf()`. Ahora replica exactamente la fórmula PRG: $5.210.126,64 - $293.923,87 = **$4.916.202,77** ✓ matchea con TMT.

Mismo cambio en la query de diagnóstico/conciliación. Se agregó un sub-detalle que cuenta los sobrepagos por separado: `n_sobrepagos`, `saldo_sobrepagos` — para que el gerente vea cuánto neteo hay y de cuántas facturas viene.

**Verificación contra DBF mayo:** `SUM(saldo) Z+A sin filtro signo = $4.916.202,77` exacto. Los top 10 sobrepagos vienen de cliente EDU (varias filas con abono > importe ≈ $26k, 16k, 15k...) y MWT (facturas con importe negativo, posiblemente notas de crédito mal clasificadas).

**Invariante nuevo:** **nunca filtrar `saldo > 0` en consultas de cartera/balance.** El dBase netea automáticamente; replicar ese comportamiento. Cualquier filtro de positivos sólo aplica en pantallas operacionales tipo "lista de deudores" donde explícitamente querés ver quién debe.

### Bug bonus 7 — Dividendos del mes mostraba 63 días en vez del mes en curso

Reportado por TMT: "Dividendos no suma bien mayo, deberia ser 85369".

**Causa:** `uret_mes_corriente()` filtraba `WHERE fecha >= CURRENT_DATE - INTERVAL '63 days'`, sumando retiros de marzo + abril + mayo (~$250k). La fórmula PRG real es `SUM ALL RET TO URET FOR &MA .AND. DD-FECHA<63` donde `&MA` es el macro "mes/año actual" (= `MONTH(FECHA)=MES .AND. YEAR(FECHA)=YEAR(DATE())`). El `DD-FECHA<63` es una guarda defensiva, no el filtro principal.

**Fix:** cambiado el filtro a `fecha >= date_trunc('month', CURRENT_DATE) AND fecha < date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'`. Resultado mayo 2026: $85.368,88 ✓ (suma exacta de los retiros del mes: FE + VI + TA + AM + OP).

Actualizados los textos de UI ("Retiros últimos 63d" → "Retiros del mes en curso") y tooltip "Dividendos" en el balance ("retiros de socios del mes en curso (= dividendos)").

**Invariante nuevo:** **`&MA` en cualquier `.PRG` legacy = filtro mes/año actual.** No es un macro vacío. Cuando portemos otra fórmula que use `&MA`, hay que aplicar el filtro de mes/año.

### Bug bonus 8 — STOCK MP+PROD del panel izq vs der no coincidían

Reportado por TMT: "STOCK MP+PROD. deberia usar el mismo valor que el de la izquierda, no con diferencia: Total 2.497.414 3,281 8.194.507".

**Causa:** dos cálculos paralelos para el mismo concepto:
- Panel izquierdo (Resultados, sección STOCK): `total_us = sum(kg_etapa × U$/kg_iniciales)` por etapa (hilado/tejido/terminado)
- Panel derecho (ACTIVO/PASIVO, fila STOCK MP+PROD.): `vsto = historia.ustock` (canónico)

Los dos cálculos pueden diferir porque el primero usa rates de iniciales (objetivo) y el segundo el snapshot del último cierre (real). El template ya tenía un disclaimer "Total ≠ STOCK MP+PROD. en ACTIVO. Diff X — esta vista usa rates de iniciales, ACTIVO usa el snapshot del último cierre".

**Fix:** en `informe_balance()`:
```python
val_estimado_etapas = val_hilado + val_tejido + val_terminado
stock_total_us = vsto if vsto else val_estimado_etapas   # ← canónico
stock_ukg_prom = _safe_div(stock_total_us, stock_total_kg)
```

VSTO es la fuente canónica para patrimonio (el dBase escribe `historia.ustock = VSTO` en cada cierre, PRG línea 1346). El desglose por etapa se conserva como informativo. Si la suma del desglose difiere del VSTO en >$1, el template muestra una nota "el desglose por etapa es informativo".

**Invariante nuevo (corregido por TMT 2026-05-06):** **El TOTAL del panel STOCK del lado izquierdo (= kg actuales × tarifa de iniciales) es la fuente canónica.** El snapshot `historia.ustock` puede estar desactualizado y representa el cierre del mes anterior; lo que el gerente quiere ver es el valor del stock con los kg DE HOY × tarifas DEL MES ACTUAL. El panel ACTIVO/PASIVO y el cálculo del patrimonio (TOTL = SUBT + VSTO + ...) ahora usan ese mismo total.

Implementación: en `informe_balance()`, después de computar `stock_total_us = val_hilado + val_tejido + val_terminado`, **se sobrescribe `vsto` con ese valor** y se RECOMPUTAN `totl`, `patr` y `utilidad` para que el math check (`TOTL = SUBT + VSTO + VQX + UMAQ + UACT + URET + ANTIC`) siga cuadrando. Sin el recálculo, los tests del balance fallan con "TOTL esperado X vs calculado Y".

Originalmente había hecho la inversa (panel izquierdo copiando vsto). TMT corrigió: "stock hiciste la inversa, era al revez copiar el otro valor".

### Bug bonus 9 — Costos U$/kg desalineados con dBase

Reportado por TMT 2026-05-06: "costos us/kg estan todos distintos al dbase. fijate como lo calcula dbase y hagamos lo mismo".

**Causa:** las 5 fórmulas de U$/kg en el panel COSTOS estaban implementadas con ratios genéricos `cost_us / cost_kg`, no con las fórmulas exactas del PRG INFORMES.PRG líneas 399-403. Tres divergencias importantes:

1. **MAT.PR. ukg**: usábamos un weighted-average simple `(stock_us + compras_us) / (stock_kg + compras_kg)`. El legacy usa la fórmula FIFO `UMX = (VM + (HI - KM) × UM0) / HI` con `HI = HI0 + KM - KH` (PRG línea 337). Esa diferencia importa cuando hay rotación grande de stock.
2. **COL.QUI. ukg**: dividíamos por KTINT (kg tinturados), pero el legacy divide por **KR** (kg que llegan a terminado). KR ≤ KTINT así que el U$/kg legacy es más alto.
3. **TEJIDO us**: incluíamos V1+V2+V3 (gastos planta tejeduría) en el total, pero el legacy `VK = SUM(IMPORTE WHERE TIPO='K') + DTJ` los **excluye** del panel COSTOS — V1+V2+V3 sólo aparecen en el reporte detallado de GASTOS. Esto reduce TEJIDO U$ pero alinea con el dBase.
4. **GASTOS us**: el legacy usa `GS = G1 + G2 + CA + DEPRCAR` (calculado desde flujo bancario con filtros complejos). Acá aproximamos con `V7+V8+V9 + DEPRCAR` (= xgast categorizado como rubro 7-9). Documentado como aproximación, podría refinarse en otra sesión.

**Fix:** reescritura del bloque COSTOS en `informe_balance()` para mapear 1-a-1 con las líneas 399-403 del PRG. Cada fila tiene un `detalle` dict que expone los componentes (KH, HI, UM0, UMX, KTINT, KR, ITIN, GTIN, GS, KV, etc.) para auditar manualmente desde la pantalla.

**Reorganización del orden:** los iniciales/proyecciones (`kgpro`, `pretej`, `pretin`, `preadm`, `inic_um`, `inic_uk`, `inic_uq`, `inic_pre`, `precio_eff`, `um_eff`, `uq_eff`) estaban DESPUÉS del bloque COSTOS, pero el bloque COSTOS los necesita para construir las proyecciones. Movidos AL PRINCIPIO del flujo, antes del bloque COSTOS.

**Nota sobre `iqx`:** el legacy usa `iqx = ITIN/KR` (ratio del mes) — no `historia.utin/historia.ktin` (ratio histórico). Refrescamos `iqx` y `uq_eff` después de calcular ITIN/KR con tin del mes, para que la proyección COL.QUI. (`KGPRO × ITIN/KR`) coincida con PRG.

**Mapping completo de las 5 filas COSTOS al PRG:**

| Fila | PRG | kg | ukg | us | proy |
|---|---|---|---|---|---|
| MAT.PR. | línea 399 | KM | UMX | VM | KGPRO×UMX |
| TEJIDO | línea 400 | KK | VK/KK | VK | XPRETEJ |
| COL.QUI. | línea 401 | KTINT | **ITIN/KR** | ITIN | KGPRO×ITIN/KR |
| GS.PROC. | línea 402 | KR | GTIN/KR | GTIN | XPRETIN |
| GASTOS | línea 403 | — | GS/KV | GS | XPREADM |

**Invariante nuevo:** **toda fórmula de costos en `informe_balance()` debe tener una referencia explícita a la línea del PRG INFORMES.PRG** en el comentario o en el ayuda. Cuando una fórmula divergía (como ukg de COL.QUI. dividiendo por KTINT en vez de KR), era casi imposible detectarlo sin volver al PRG. Con la referencia inline, el revisor puede chequear directo.

### Bug bonus 10 — Flujo de caja gráfico siempre en negro

Reportado por TMT 2026-05-06: "flujo de caja se ve siempre en negro, nunca vemos el grafico. hay que arreglar esto si o si".

**Causa:** bug clásico de Chart.js 4.x con `responsive: true; maintainAspectRatio: false`. En esta combinación, Chart.js calcula la altura del canvas mirando el **contenedor padre**, NO el CSS del propio canvas. Como el `<canvas>` estaba directamente dentro de `.panel` (sin altura explícita), el padre dependía de la altura de los hijos — pero el canvas era el hijo más grande y **aún no se había dibujado** cuando Chart.js midió. Resultado: parent.contentHeight ≈ pequeño → canvas se renderiza a 0px → área negra del wrapper queda visible sin chart encima.

**Fix:** envolver el canvas en un div con altura fija que NO dependa del canvas:

```html
<div class="chart-wrap">
  <canvas id="chart"></canvas>
</div>
```

```css
.flujo-wrap .chart-wrap {
  position: relative;
  width: 100%;
  height: 480px;
}
.flujo-wrap .chart-wrap canvas { width: 100% !important; height: 100% !important; }
```

Removido el `height: 480px !important` del canvas (Chart.js lo respetaba a medias). Ahora Chart.js mide el `.chart-wrap` (= 480px fijo) y dimensiona correctamente.

**Defensas adicionales agregadas:**
1. **Check `if (typeof Chart === 'undefined')`** al inicio del script — si el CDN de cdnjs falla (sin internet, adblock, network issue), muestra un mensaje rojo en español explicando el problema en vez de un canvas en blanco.
2. **`try/catch` alrededor de `buildChart()`** — si Chart.js está pero algo de los datos rompe, se loguea a consola Y se renderiza un mensaje "Error al construir el gráfico" con el detalle, en vez de fallar silente.
3. **Listener de los botones de agrupación** también con try/catch — si re-renderear con q=14 fallara, no rompe el resto de la página.

**Invariante nuevo:** **cualquier `<canvas>` Chart.js con `responsive:true` debe ir dentro de un wrapper con altura fija explícita.** No usar `height` directamente en el canvas — Chart.js lo sobrescribe. Si en algún momento volvemos a agregar otro chart (dashboard, ventas, etc.), aplicar el mismo patrón. Documentado en este file y en el código.

### Bug bonus 11 — Costos U$/kg, segunda iteración

Después de portar las fórmulas exactas del PRG (bug bonus 9), TMT comparó contra el dBase live y aún había diferencias:

| Fila | Ours | dBase | Diff |
|---|---|---|---|
| MAT.PR. | 2.701 | **2.926** | -0.225 |
| TEJIDO | 0.560 | OK | — |
| COL.QUI. | 0.682 | **0.696** | -0.014 |
| GS.PROC. | 1.719 | **1.741** | -0.022 |
| GASTOS | 1.163 | OK | — |

**Tres trampas dBase encontradas tras audit del DBF + PRG:**

#### Trampa A — `KH = KK / (1 - DESK/100)` (no `KH = KK`)

PRG línea 267: `KH = KK / (1 - DESK/100)` con `DESK = 0.5` (PRG línea 6). Esto contempla la merma de yarn loss en tejeduría: los kg de hilado consumidos son ~0.5% más que los kg de tejido producidos. Yo usaba `KH = KK = tej.kg_total` directo, sub-estimando la salida. Resultado: HI (stock final hilado) salía más grande de lo real, y UMX = (VM + (HI-KM)·UM0)/HI tenía un denominador inflado → UMX más bajo de lo correcto.

Fix: en `informe_balance()` ahora `KH = KK / (1 - DESK_PCT/100)` con `DESK_PCT = 0.5`.

#### Trampa B — ITIN no filtra `kg > 0`

PRG línea 252: `&SAI ITIN` (= `SUM ALL IMPORTE TO ITIN`, sin FOR). No filtra. Yo filtraba `WHERE kg > 0` en la query, lo que excluía 1 fila de mayo (color "LAVADO MAQ", kg=0, importe=$225). Diff: ITIN ours = $32.460, dBase = $32.685.

Fix: removido el filtro `kg > 0` de ITIN. Ahora suma todas las filas de TINTO del mes.

#### Trampa C — `COLOR='LAV'` en dBase con `SET EXACT OFF` hace prefix match

El PRG filtra `FOR .NOT. COLOR='LAV'` en KTINT (línea 254) y KR (línea 256). En dBase con `SET EXACT OFF` (default), la igualdad `COLOR='LAV'` es **prefix match**: matchea cualquier valor que empiece con "LAV". El registro de mayo con `COLOR='LAVADO MAQ'` SÍ es excluido por el filtro dBase, pero nuestro SQL `UPPER(TRIM(color)) <> 'LAV'` (exact match) lo dejaba pasar. Diff: 600 kgn de KR de más.

Fix: cambiado a `UPPER(TRIM(color)) NOT LIKE 'LAV%'` para replicar el prefix match.

**Resultado tras los tres fixes (verificado contra DBF mayo):**
- COL.QUI. ukg = $32.685 / 46.980 = **0.696** ✓
- GS.PROC. ukg = $81.775 / 46.980 = **1.741** ✓
- MAT.PR. ukg = depende de HI0 (= historia.hilado) y UM0 reales en la DB; con KH corregido el número va a estar más cerca de 2.926

**Invariante nuevo (super-importante):** **cuando porteamos un filtro string del PRG legacy a SQL, asumir que es prefix match** salvo que veamos `SET EXACT ON` explícito en el PRG. Convertir a `LIKE 'X%'` en SQL. La igualdad exact-match es el comportamiento moderno; dBase legacy usa prefix por defecto.

**Otro invariante:** **nunca filtrar `kg > 0` o `cantidad > 0` defensivamente** sin chequear que el PRG lo haga. El dBase legacy a menudo suma "todo" para totales operativos, y un filtro nuestro defensivo puede ocultar registros legítimos (ej: lavados sin kg pero con importe).

### Bug bonus 12 — UTILIDAD = delta(patrimonio) + dividendos

Reportado por TMT 2026-05-06: "utilidad esta mal. deberia ser delta patrimonio con principio de mes, mas dividendos".

**Causa:** la fórmula PRG `UTILIDAD = PATR - PATANT` (ver session notes batch 17 del addendum existente) sólo refleja el delta del patrimonio entre el cierre anterior y hoy, **ignorando los dividendos retirados durante el mes**. Si los dueños retiraron $85k este mes, ese efectivo salió del patrimonio sin ser pérdida — fue ingreso económico real que ellos cosecharon.

La fórmula contable correcta para "utilidad económica del mes" es:

    UTILIDAD = (PATR - PATANT) + URET

donde URET = retiros del mes (dividendos). Esto contempla que los dividendos son distribución de utilidad, no pérdida.

**Fix:** dos lugares en `informe_balance()`:
- Cómputo inicial: `utilidad = (patr - patant) + _uret`
- Recálculo después del override de VSTO: `utilidad = (patr - patant) + _uret`

Ambos usan `_uret = uret_mes_corriente()` que ahora es correctamente "retiros del mes en curso" (ver bug bonus 7).

**Invariante nuevo:** **utilidad económica ≠ delta patrimonio** cuando hay distribuciones a dueños. Siempre sumar dividendos/retiros al delta para tener la utilidad real del período.

### Bug bonus 13 — Chart.js no carga (CDN bloqueado)

Después de aplicar las defensas del bug 10, TMT vio el mensaje "Chart.js no se pudo cargar" — confirmando que el CDN cdnjs.cloudflare.com está efectivamente bloqueado en su entorno (firewall fábrica / adblock / etc.).

**Fix:** **bundlear Chart.js localmente** en `static/vendor/chart.umd.min.js` (205KB) y servirlo desde el propio servidor Flask:

```html
<script src="{{ url_for('static', filename='vendor/chart.umd.min.js') }}"
        onerror="...fallback al CDN..."></script>
```

Aplicado en dos templates:
- `modules/informes/templates/informes/flujo_grafico.html` — el que TMT reportó.
- `modules/dashboard/templates/dashboard.html` — usa Chart.js para evolución de cartera/deuda y saldo bancario 30d. Mismo problema potencial.

El CDN queda como fallback dinámico (si por algún motivo el static no carga, intenta CDN). Pero el path principal es local — sin internet la app sigue funcionando completa.

**Detalle del fallback dinámico:**
```js
onerror="var s=document.createElement('script');s.onload=...;s.src='cdnjs...';document.head.appendChild(s);"
```

Crea un nuevo `<script>` apuntando al CDN si el local falla, mantiene los `_dashCharts` callbacks intactos.

**Origen del bundle:** `https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js` (mismo Chart.js 4.4.3 que el CDN de cdnjs). Hash MD5: `c9c7a14398a513bdb97934c8468143b9`. Para regenerar:

```bash
curl -L "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js" \
     -o static/vendor/chart.umd.min.js
```

**Invariante nuevo:** **toda librería JS de terceros que el app necesite debe vivir en `static/vendor/`**. CDNs son convenientes pero hacen al app dependiente de internet, adblock, y polizas IT del cliente. Programa Core está en una fábrica con conexión variable y firewall corporativo — bundle local es la opción safe-by-default.

### Verificación contra DBFs reales mayo 2026

Audit ejecutado contra los DBFs en `/Users/tamaraeliscovich/Documents/INTELA copy/Files/`:

```
COMPRAS.DBF mayo (15 filas tipo='K'):
  Todas PROV='KK'.
  KK con KG>0:    66.068,56 kg / $0       (production interna INTELA) ← era el bug
  KK con IMPORTE>0, KG=0:  0 kg / $4.013,26  (gastos varios cargados a tej)
  PROV<>KK con KG>0:        0 kg / $0       (sin tercerización externa)

XGAST.DBF mayo (97 filas):
  V1+V2+V3 = $32.563,59
  V4+V5+V6 = $76.689,80
  V7+V8+V9 = $60.266,38

ACTIVOS.DBF (todo histórico):
  TIPO='K' (tejido):  21 filas / amort.mes total $2.960
  TIPO='C' (color):    2 filas / amort.mes total $790
  TIPO='M' (maquinaria): 27 filas
  TIPO='I' (inmuebles): 10 filas
```

Cuadra con la pantalla actual de TMT: TEJIDO U$ ahora debería ser ~$39.537 (era $36.969 — la diferencia es justo `us_kk_gastos = $4.013` que el filtro legacy excluía + ajustes en amort).

### Files touched (este sub-batch)

```
modules/informes/queries.py  + tejido_mes_componentes() — descompone tipo='K' en 3 partes
                              + costo_promedio_mp_ponderado() — weighted avg stock+compras
                              + actualizado informe_balance() para usar las dos
                              + costos[i].detalle expone componentes para debug
                              - removido `iprov` (no usado, replazado por tej)

(Test cases nuevos quedan pendientes — la próxima sesión debería agregar
 tests aislados con monkeypatched fetch_all para tejido_mes_componentes
 y costo_promedio_mp_ponderado.)
```
