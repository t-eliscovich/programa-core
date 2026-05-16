# Skill addendum — batch 17 (2026-04-27)

Sesión extra-larga (autopilot) post-audit dBase. **Cierre de paridad con
el legacy.**

## Snapshot al cierre

- **388 tests passed** (subió de 362 → +26 tests nuevos)
- **69 GET routes smoke** — 0 failures (subió de 68 → +1, `cartera.grupos`)
- **`ruff check .`** — TODO el proyecto clean (primera vez en muchas sesiones)
- **12 migraciones** — `0012_cliente_activo.sql` agregada (soft-delete clientes)
- 12+ archivos nuevos: snapshot script, depósito en lote template, wizard
  emitir cheque template, cartera grupos template, 4 tests files,
  migration 0012, addendum batch 17, paridad audit doc.

## Lo que se cerró

### Audit de paridad dBase ↔ nuevo (28% gaps → ~5% gaps)

**De 18 flujos del audit, los 4 críticos quedaron implementados:**

1. **Snapshots historia mensual** (`scripts/snapshot_historia_mensual.py`)
   - Calcula KPIs del mes anterior y los persiste en `scintela.historia`
   - Wireado al cron mensual existente como tarea `snapshot_historia`
   - Usa `INSERT ... ON CONFLICT DO NOTHING` semantics — no pisa filas
     existentes sin `--force`
   - Field set computado: `cart, deuda, banco, gasto, retiro, kvent, uvent,
     kcom, ucom`. Otros campos quedan NULL (los completa el cierre manual
     o el legacy si hubo).

2. **Depósito en lote de cheques** (`/cheques/depositar-lote`)
   - `cheques.queries.depositar_lote(ids_cheques, no_banco, ...)`
   - Para cada cheque: UPDATE stat='D' + INSERT transacciones_bancarias
     (documento='DE') + INSERT chequextransaccion
   - Validación previa: aborta el lote completo si UN cheque no es depositable
   - UI: checkboxes + master + contador vivo; botón "Depositar lote" en `/cheques`

3. **Anticipos en cheques (CONCEPTO=9999 legacy)**
   - Flag `es_anticipo: bool` en `cheques.queries.crear()`
   - Cuando True: cheque normal + cheque "espejo" con importe negativo,
     `id_cheque_padre` apunta al original
   - UI: checkbox en `/cheques/nuevo` con explicación contable

4. **Wizard de cheque emitido (chequera)** (`/bancos/emitir-cheque`)
   - **Decisión arquitectural**: en vez de inferir mágicamente como el
     legacy `BANCOS.PRG::CHEQUERA` (split por proveedor + concepto), pedimos
     el TIPO explícito.
   - Tipos: `proveedor` / `retiro` / `caja` / `gasto` / `otro`
   - Cada tipo tiene side-effect específico, todo en una tx:
     - `proveedor` + id_posdat → cierra posdat (banc=no_banco)
     - `retiro` → INSERT en retiros con doc='CH'
     - `caja` → INSERT en caja (entrada)
     - `gasto` → INSERT en xgast (saldo=0 si no postdatado)
     - `otro` → solo movimiento bancario
   - Common: INSERT en transacciones_bancarias documento='CH', importe
     negativo (egreso del banco)
   - UI: wizard con 5 cards visuales + bloque específico por tipo

### Quality of life

5. **Filtro `cleanstr`** (filters.py)
   - Devuelve `''` para None/""/"None"/"NULL"/"NaN" (case-insensitive)
   - Aplicado en 17 lugares de templates donde el dump dBase→Postgres
     puede haber dejado string literal "None"
   - Lugares cubiertos: detalle cheque, lista clientes, lista compras,
     conciliación (no_cheque), caja/capital/retiros/gastos/activos
     (concepto), estado de cuenta, facturas/detalle

6. **Retiros — modelo correcto**
   - Antes confundíamos `de` con "socio". Es un código de 2 letras tipo
     "FE", "LC", "RR" (categoría/destino legacy)
   - Quitamos KPI "Personas" (no aporta con un solo dueño)
   - Chip strip "Por código" solo si hay >1 valor distinto
   - Columna "De" → "Cód." con tooltip explicando

7. **Dashboard inicial — fix definitivo**
   - Saludo + fecha en el header (`Buenos días, tamara · Lunes 27 de abril`)
   - Vista "Simple" llena con 3 chips de la semana (cobrar/pagar/rebotados)
   - Bancos con saldo negativo → fila roja con badge "SOBREGIRO"
   - Top 10 deudores con barra de proporción del total
   - "Neto del mes" → "Cartera nueva del mes" (subtítulo más claro)
   - Trend "flat" → guion `—` en vez de "0%" engañoso
   - **Fix font cards gigantes**: Tailwind no recompila → usar inline
     `style="font-size: 1.5rem; line-height: 1.2;"` en lugar de classes
     no compiladas (md:text-3xl, xl:text-4xl no estaban en el CSS)

8. **Charts del dashboard**
   - Chart "Saldo bancario 30d" migrado de SVG crudo a Chart.js
   - Init pattern robusto: `window._dashCharts.push(fn)` + Chart.js
     `<script ... onload="...">` dispara todos los inits
   - try/catch por chart, console.warn si menos de 2 puntos
   - onerror del CDN logea "No pude cargar Chart.js"

### Dashboards específicos por rol

9. Reemplazamos el placeholder genérico de roles "otros" por dashboards
   contextuales:
   - **Cobranzas**: cards a Aging / Calendario / Rebotados + quick actions
     (Cargar cheque, Directorio)
   - **Compras**: cards a Posdat / Compras / Proveedores + Nueva compra,
     Provisiones
   - **Bodega**: Compras / Proveedores
   - **QC**: link a `formulas_app` (la tintura está allá)
   - **Lectura**: Aging / Bancos / Flujo (solo-lectura)

### Cartera por grupo (NUEVO al final de batch)

10. `/cartera/grupos` — usa `scintela.grupo_cliente` (codigo_padre/hijo)
    para sumar saldos de clientes que comparten casa matriz. Reemplaza la
    PROCEDURE GRUPOS de `INFORMES.PRG` legacy. Tabla con barra de proporción
    + sidebar link.

### Difuntos / soft-delete clientes (extra)

11. **Migración 0012** agrega `cliente.activo boolean DEFAULT TRUE` con
    índice parcial.
12. `clientes.queries.set_activo()` + ruta `POST /clientes/<codigo>/activar`
    para toggle.
13. `buscar()` ahora acepta `incluir_inactivos` — UI con toggle "ver/ocultar
    inactivos" en `/clientes`.
14. Sección "Estado del cliente" en el form de edición con botón
    "Marcar como INACTIVO (difunto)" / "Reactivar".
15. Reemplaza `MODIFICA.PRG > DIFUNTOS` legacy. Las facturas históricas y
    saldos vivos del cliente se preservan — sólo se esconde de
    autocompletes y lista por default.

### Tests agregados

26 tests nuevos:
- `test_compras_anular.py` — 6 tests
- `test_cheques_depositar_lote.py` — 6 tests
- `test_cheques_anticipo.py` — 4 tests
- `test_bancos_emitir_cheque.py` — 10 tests

Patrón: cada uno usa stub `_DBStub` propio + monkeypatch de
`asegurar_fecha_abierta` tanto en `periodo_guard` como en el módulo `queries`
(porque hace `from periodo_guard import ...` al cargar).

### Cleanup técnico

- 1 flash con suffix migrado a humanize (sri/views.py:377)
- Ruff issues pre-existentes en exports.py / parsers.py / filters.py /
  procesa_provisiones_mensual.py / tests/test_exports.py — todos limpios.
- `Union[A, B]` → `A | B`, `isinstance(x, (A, B))` → `isinstance(x, A | B)`
  donde correspondía.

## Bugs reportados durante la sesión + resueltos

1. ✅ **Cards gigantes del dashboard cortadas** — clamp() no funcionaba con
   classes Tailwind no compiladas; fix con `style="font-size: 1.5rem"` puro
2. ✅ **"None" en columna N° cheques** — fix con filtro `cleanstr`
3. ✅ **Provisiones bug** — varchar overflow + falta botón "+ Nueva" + falta
   columna acciones; todos arreglados
4. ✅ **Retiros "Por socio"** — modelo confundido; corregido a "Por código"
5. ✅ **Gráfico de flujo no aparecía** — Chart.js timing fragility; fix con
   `window._dashCharts` array + onload pattern

## Migraciones pendientes de aplicar en el server

Ninguna nueva en batch 17. Las que ya estaban:
- 0006-0010 ya aplicadas
- 0011 (compras stat) — pendiente si todavía no se corrió en prod

## Flujos del dBase aún NO replicados (deuda explícita)

Los menos frecuentes/críticos:

- **`INFORMES.PRG > FUENTES`** — informe de fuentes y usos de fondos
  (compara dos meses de `historia`). Implementarlo cuando historia esté
  bien populada con datos de varios meses.
- **`INFORMES.PRG > PROG`** — informe de programación. Probablemente
  redundante con `/iniciales/comparativo` que ya tenemos.
- **`BANCOS.PRG > BOLETA*`** — generar boletas de depósito imprimibles.
  Significaría templates HTML + print CSS específicos por banco.
- **`MODIFICA.PRG > DIFUNTOS`** — soft-delete de clientes inactivos.
- **`CHEQUES propios — chequera`** (la tabla de cheques que NOSOTROS
  emitimos) — está parcialmente cubierto por el wizard `/bancos/emitir-cheque`
  pero no hay listado de cheques propios separado de cheques recibidos.

## Comandos útiles

```bash
# Run completo
python -m pytest tests/ -q --ignore=tests/test_integration_migrations.py --ignore=tests/test_integration_flows.py
python tests/test_routes_smoke.py
ruff check .

# Aplicar el snapshot manualmente
python scripts/snapshot_historia_mensual.py             # mes anterior
python scripts/snapshot_historia_mensual.py --force     # sobreescribe
python scripts/snapshot_historia_mensual.py --dry-run   # ver KPIs sin guardar
python scripts/snapshot_historia_mensual.py --periodo 2026-03  # mes específico
```

## Para el próximo

Si todo lo anterior funciona, los siguientes pasos naturales:

1. Probar el wizard `/bancos/emitir-cheque` con un user real
2. Probar el depósito en lote `/cheques/depositar-lote`
3. Verificar que el snapshot mensual corre OK el día 2
4. Implementar **Difuntos** (soft-delete clientes) — chico, ~1h
5. **Boletas de depósito imprimibles** — proyecto de medio día (HTML + print CSS por banco)
6. **Notas de débito SRI + retenciones electrónicas** — replica el pattern
   de NC ya implementado, pendiente del .p12 de firma real
