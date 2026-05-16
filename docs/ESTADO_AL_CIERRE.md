# Estado al cierre — 2026-04-27 (fin de batch 17)

**Leer ESTO primero cuando se abra una sesión nueva.** Es el índice operativo
por encima de los addendums cronológicos por batch.

## Snapshot

- **388 tests passed, 9 skipped** (los 9 son `@pytest.mark.db` que requieren
  Postgres real).
- **68 GET routes smoke** — 0 failures.
- **`ruff check`** — clean en todo el código nuevo de batch 17.
- **11 migraciones** en `migrations/` — `0001` → `0011` aplicables con
  `scripts/migrate.py`. La 0011 (`compra_stat`) sigue siendo la última.
- **25+ invariantes** documentados a través de los batches — ver sección
  "Invariantes consolidados" abajo.

## Batch 17 — Cierre de paridad con dBase (2026-04-27)

Sesión extra-larga después del audit de paridad dBase ↔ nuevo app.
Cerramos los 4 gaps críticos identificados, mas cleanup adicional.

### Gap 1 — Snapshots historia mensual ✅
- `scripts/snapshot_historia_mensual.py` — calcula KPIs del mes anterior
  (cart, deuda, banco, gasto, retiro, kvent, uvent, kcom, ucom) y los
  guarda en `scintela.historia`. Idempotente — no pisa filas existentes
  sin `--force`.
- Wireado al cron mensual existente (`procesa_provisiones_mensual.py`)
  como tarea `snapshot_historia` (3a tarea, después de procesa_provisiones
  y actualizar_amortizacion). Soporta el caso especial `PYTHON:` en TAREAS.
- Cuando lo corra el cron del día 2, popula la tabla que alimenta el chart
  "Evolución cartera 12 meses" y el balance del Dueño.

### Gap 2 — Depósito en lote de cheques ✅
- `modules/cheques/queries.py::depositar_lote()` — toma N cheques en cartera
  y los deposita en un solo banco en una sola transacción.
- Para cada cheque: UPDATE stat='D' + INSERT transacciones_bancarias
  (documento='DE') + INSERT chequextransaccion (relación N:N).
- Validación previa: si UN cheque no es depositable (stat≠'Z'/'P'),
  aborta TODO el lote.
- UI: `/cheques/depositar-lote` — checkboxes + master, contador vivo,
  selector de banco + fecha + concepto. Botón verde "Depositar lote"
  en `/cheques`.

### Gap 3 — Anticipos en cheques (CONCEPTO=9999 legacy) ✅
- Flag `es_anticipo: bool` en `cheques.queries.crear()`. Cuando True
  inserta el cheque normal + un "espejo" con importe negativo y
  `id_cheque_padre` apuntando al cheque principal.
- UI: checkbox ámbar destacado en `/cheques/nuevo` con explicación.

### Gap 4 — Wizard de cheque emitido (chequera) ✅
- `modules/bancos/queries.py::emitir_cheque()` — única función centralizada
  para emitir cheques propios. En vez de inferir mágicamente como el legacy
  `BANCOS.PRG::CHEQUERA`, pide el TIPO explícito:
  - `proveedor` → cierra posdat (banc=no_banco)
  - `retiro` → INSERT en retiros
  - `caja` → INSERT en caja (entrada)
  - `gasto` → INSERT en xgast (saldo=0 si no postdatado)
  - `otro` → solo movimiento bancario
- Todos comparten: INSERT en transacciones_bancarias con documento='CH',
  importe negativo (egreso del banco).
- UI: `/bancos/emitir-cheque` — wizard visual con 5 cards (👤🏭💵📋📝),
  campos comunes + bloque específico por tipo, lista de posdats abiertas
  filtrable por proveedor.
- Sidebar: "Emitir cheque" gateado en `bancos.conciliar`.

### Gap 5 — Bugs "None" string del dump dBase ✅
- Filtro Jinja nuevo `cleanstr` en `filters.py` — devuelve '' para
  None / "" / "None" / "NULL" / "NaN" (case-insensitive).
- Aplicado en 17 lugares de templates donde campos opcionales del legacy
  podrían venir con string literal "None": detalle cheque (RUC, tel, concepto),
  lista clientes (RUC, tel), lista compras (comprobante, concepto), conciliación
  (no_cheque), caja/capital/retiros/gastos/activos (concepto), estado de
  cuenta (no_cheque), facturas/detalle.

### Gap 6 — Cleanup técnico ✅
- **Tests para `compras.anular`** — 6 tests en `tests/test_compras_anular.py`:
  happy path, motivo vacío, motivo solo espacios, compra inexistente,
  ya anulada, sin posdat hermana.
- **Tests para `cheques.depositar_lote`** — 6 tests cubriendo lista vacía,
  banco inexistente, cheque ya depositado, happy path multi-cheque, postdatado.
- **Tests para `bancos.emitir_cheque`** — 10 tests cubriendo cada tipo de
  cheque + validaciones de input.
- **Tests para `cheques.es_anticipo`** — 4 tests del flag de anticipo.
- **Flash con suffix** en `sri/views.py:377` migrado a `humanize()` + sufijo
  manual ("Reintentá con el botón Consultar").
- **Dashboards específicos por rol** en lugar de placeholder genérico:
  - **Cobranzas** — cards a Aging / Calendario / Cheques rebotados +
    quick actions a Cargar cheque / Directorio contactos.
  - **Compras** — cards a Posdat abiertas / Compras / Proveedores +
    Nueva compra / Provisiones.
  - **Bodega** — cards a Compras / Proveedores.
  - **QC** — placeholder con link a `formulas_app` (la tintura está allá).
  - **Lectura** — cards de solo-lectura (Aging / Bancos / Flujo).

## Batch 16 — recap (2026-04-24)
Ver `docs/SKILL_ADDENDUM_BATCH_16.md` para detalle.
- 4 quick wins (compras.anular con 2-step, recientes posdat/retención,
  flash_exc en 25 sites, kbd hint ⌘K)
- Bulk actions cartera + dashboard fluid font
- Quick filters cartera + chart evolución 12 meses + calendario cobranzas
- Módulos gastos / retiros / activos / iniciales / directorio contactos
  / cobros recibidos
- Provisiones bug fixes
- Ruta `/clientes/contactos` con WhatsApp/mailto/tel directos
- Chart Chart.js para saldo 30d
- Bugs "None" en cheques + chart de flujo robusto

## Hotfixes post-batch-17 (2026-04-27 tarde)

Bugs encontrados durante el primer test del usuario, arreglados en la
misma sesión:

1. **`pd.compr does not exist`** en `/posdat` — el SELECT pedía columnas
   que el schema real no tiene (`compr`, `no_comp`, `tipo` eran tablas
   auxiliares en el legacy dBase). Removidas de `posdat.queries.por_id()`,
   `buscar()`, `bancos.queries.posdat_abiertas_de()` y del CSV export.
   Las funciones `crear()` / `editar()` siguen aceptando esos kwargs por
   compatibilidad pero los apendean al `concepto`.
2. **`tuple index out of range`** en `/provisiones` — `db.fetch_one()`
   pasa `params=()` por default cuando no se le pasa argumentos. Un SQL
   con `LIKE '%MENSUAL%'` (sin escapar) hace que psycopg2 interprete cada
   `%` como placeholder y reviente al no encontrar valores. Fix: escapar
   los `%` literales como `%%` en el SQL.

**Invariante nuevo descubierto**: si un SQL tiene `%` literales (LIKE,
TO_CHAR formato, etc) Y se llama a través de `db.fetch_one/all/execute`
sin params explícitos, hay que escapar los `%` como `%%`. Causa: db.py
pasa `params or ()` que activa el modo mogrify de psycopg2.

## Qué hacer para arrancar el app mañana

```bash
cd "~/Documents/Claude/Projects/Programa Core"

# 1. Una vez — instalar deps (psycopg pin ya fijeado correctamente).
pip install -r requirements.txt

# 2. Aplicar TODAS las migraciones pendientes (0006-0010). Sin esto:
#    - El login falla porque `totp_secret` no existe (→ 500).
#    - El cron mensual sale con exit 2.
#    - Los recientes en sidebar se degradan silenciosos.
python scripts/migrate.py

# 3. Levantar
./launcher.sh
```

Login default del `.env`: `tamara / intela2026` (o el que hayas seteado).

## 15 batches — qué hace cada uno

Los addendums están en `docs/SKILL_ADDENDUM_BATCH_*.md`. Los batches 1-8 están
consolidados en el SKILL.md del skill `programa-core`. Los 9-15 están como
addendum porque el skill vive read-only en el sandbox.

| Batch | Tema | Ver |
|-------|------|-----|
| 1-5 | Scaffolding inicial + módulos CRUD (facturas, cheques, compras, retenciones, caja, capital, provisiones, posdat, usuarios, bitácora, periodos, cartera) | SKILL.md |
| 6 | X-Request-Id + correlación con bitácora | SKILL.md |
| 7 | Cleanup chico (ruff, scripts archive, num_es) | SKILL.md |
| 8 | Tarea mensual `procesa_provisiones` idempotente + tracker `ejecuciones_tareas` | SKILL.md |
| 9 | Cleanup bugs post-deploy + live stock kg + session timeout por rol + 2FA + password policy | `docs/SKILL_ADDENDUM_BATCH_9.md` |
| 10 | SRI MVP: XML factura + clave acceso + persistencia (sin firma ni envío) | `docs/SKILL_ADDENDUM_BATCH_10.md` |
| 11 | Costos OT (fake data), cheque rechazado por conciliación, IP allowlist, Docker+CI, integration tests, firma SOAP SRI, runbook rotación | `docs/SKILL_ADDENDUM_BATCH_11.md` |
| 12 | `/healthz` liveness+readiness + notas de crédito electrónicas SRI + runbook docker local | `docs/SKILL_ADDENDUM_BATCH_12.md` |
| Hotfix | 3 bugs de primer arranque (psycopg pin, CSRF missing, 500 post-login por migración 0008 pendiente) | `docs/HOTFIX_2026-04-17.md` |
| 13 | CSV upload inline en facturas/cheques/compras (plantilla + parser + reporte por fila) | `docs/SKILL_ADDENDUM_BATCH_13.md` |
| 14 | UX quick wins: fecha DD/MM/YYYY, autocomplete clientes/proveedores, autofocus, Ctrl+K búsqueda global, sidebar mobile | — |
| 15 | UX tier 2: errores en español, dashboard modo Dueño compacto, undo 2-step, recientes por usuario | `docs/SKILL_ADDENDUM_BATCH_15.md` |
| 16 | 4 quick wins: `compras.anular` con 2-step (migración 0011 + stat/observacion en compra); tipos `posdat`/`retencion` en recientes; `flash_exc()` helper aplicado a 25 sites; kbd hint ⌘K/Ctrl+K con platform detection + pulse one-shot para onboarding | sección "Batch 16" abajo |

## Árbol del proyecto

```
/Users/tamaraeliscovich/Documents/Claude/Projects/Programa Core/
├── app.py                    — Flask factory + middleware (timing, request_id, ip_allowlist)
├── auth.py                   — login, 2FA, password policy, session timeout, bitácora
├── db.py                     — single data layer (fetch_one, execute_returning, tx)
├── filters.py                — Jinja filters (num_es, fecha_es, money_es, kg_es, humanizar)
├── parsers.py                — parse_date/monto/int/bool compartido por views
├── periodo_guard.py          — asegurar_fecha_abierta()
├── extensions.py             — csrf + limiter
├── ip_allowlist.py           — middleware por rol
├── exports.py                — csv_response con BOM + per-column formatter
├── csv_upload.py             — helper compartido para uploads
├── error_messages.py         — humanize(exc) para mensajes contables
│
├── modules/
│   ├── auth/                 — login.html, cambiar_password.html
│   ├── bancos/               — listado + saldos por banco
│   ├── bitacora/             — visor de auditoría (/bitacora)
│   ├── caja/                 — ingresos/egresos cash
│   ├── capital/              — movimientos de capital
│   ├── cartera/              — aging 0-30/31-60/61-90/90+ + stop-automatico
│   ├── cheques/              — alta, detalle, aplicar a factura, reversar, CSV upload
│   ├── clientes/             — CRUD
│   ├── compras/              — alta, listado, anular, CSV upload
│   ├── conciliacion/         — upload CSV bancario → bandeja sospecha rechazo
│   ├── costos_ot/            — puente formulas_app con 3 adapters (fake/metabase/postgres)
│   ├── dashboard/            — tablero Dueño/Gerente/Contabilidad/Ventas + vista simple/completa
│   ├── facturas/             — alta, detalle, anular (2-step), CSV upload
│   ├── healthz/              — liveness + readiness
│   ├── informes/             — balance, flujo gráfico, cartera, estado de cuenta, upload flujo
│   ├── periodos/             — cierre de mes contable
│   ├── posdat/               — CRUD deuda con proveedores
│   ├── proformas/            — cotizaciones
│   ├── proveedores/          — CRUD
│   ├── provisiones/          — recurring accruals
│   ├── recientes/            — últimos N registros por usuario
│   ├── retenciones/          — emitir + anular
│   ├── sri/                  — facturación electrónica (XML, firma, envío, NC, queries, views)
│   ├── two_fa/               — TOTP opt-in + QR
│   └── usuarios/             — admin de usuarios
│
├── config/
│   ├── emisor.py             — datos SRI del emisor (Intela)
│   └── roles.py              — mapa canónico 10 roles × permisos
│
├── migrations/               — 0001 a 0010 (aplicables con scripts/migrate.py)
├── scripts/                  — migrate, seed_roles, procesa_provisiones_mensual, dashboard_profile
├── static/                   — tailwind.css + app.js (fecha autoformat + Ctrl+K + sidebar mobile)
├── templates/                — base.html + _ui.html + _inputs.html + _confirmar_accion.html + _csv_upload.html + 404/500.html
├── tests/                    — pytest suite (362 tests) + smoke test
│
├── docs/
│   ├── SKILL_ADDENDUM_BATCH_9.md / 10 / 11 / 12 / 13 / 15   — notas por batch
│   ├── HOTFIX_2026-04-17.md                                  — 3 bugs del primer deploy
│   ├── RUNBOOK_rotar_credencial.md                           — procedimiento para 1n7el4Pyth0n
│   ├── RUNBOOK_docker_local.md                               — docker compose paso a paso
│   └── ESTADO_AL_CIERRE.md                                   — ESTE archivo
│
├── Dockerfile + docker-compose.yml + Makefile + .github/workflows/ci.yml
├── launcher.sh + run.py + requirements.txt
└── preview/                  — HTMLs estáticos con fake data para diseño
```

## Invariantes consolidados (32 reglas)

Cada batch agregó invariantes. Los que tenés que respetar para no romper nada:

1. Todo `crear()` que escribe a `scintela.*` con columna fecha llama `asegurar_fecha_abierta(fecha)` antes del INSERT.
2. `execute_returning()` para `INSERT ... RETURNING`, NUNCA `fetch_one()` — no commitea.
3. Writes multi-statement usan `db.tx()` context manager — una sola commit al final.
4. Cada write-route escribe a `scintela.bitacora_acciones` via el `after_request` hook.
5. Audit failures son best-effort (try/except pass). Nunca rompen requests reales.
6. Contabilidad tiene `*.anular`, no `*.editar` en docs emitidos (regla Ecuador).
7. `scintela.provisiones` usa `fecha_actualiza`/`usuario_actualiza` (único caso — el resto usa `fecha_modifica`).
8. `posdat.banc=9` significa cerrada/pagada. Todos los rollups de pasivos filtran `banc<>9`.
9. `factura.abono` es DERIVED desde chequesxfact. Aplicar/un-aplicar un cheque actualiza ambos en la misma tx.
10. Tareas programadas at-most-once-per-period usan `scintela.ejecuciones_tareas` con `UNIQUE(tarea, periodo)`.
11. `X-Request-Id` se genera/acepta en `before_request`, se emite en `after_request` (sólo UUID canónico).
12. Ruta nueva → agregar en `templates/base.html` sidebar con permiso correcto e ícono.
13. `db.tx()` yield-ea una conn cruda: `with db.tx() as conn: with conn.cursor(cursor_factory=RealDictCursor) as cur:`.
14. Migración destructiva (TRUNCATE/DROP) requiere guard condicional que demuestre estado pre-fix.
15. `last_activity` se reinicia en paths reales, no en keepalive (`/static/`, `/healthz`, `/favicon`).
16. 2FA es opt-in. Usuarios en "setup mode" (secret sin confirmado_en) loguean normal.
17. QR de 2FA se regenera en cada GET de `/2fa/setup`, inline como data-URL.
18. Password policy: largo + variedad mínima (NIST SP 800-63B), no complexity obligatoria.
19. Backend-agnostic adapters (como `costos_ot`): fachada + factory + fake.
20. Resilient HTMX fragments nunca rompen la página host al fallar el backend.
21. Default-allow para middlewares de access control (IP allowlist) — sin env var = passthrough.
22. `requests` para clientes SOAP, no zeep — envelope a mano.
23. Reintento sólo en transient errors (5xx/Timeout/ConnectionError), nunca 4xx.
24. Tests `@pytest.mark.db` skip-ean cuando no hay DB, NO error.
25. Liveness ≠ readiness — separados para evitar restart cascades.
26. NC SRI sólo contra facturas autorizadas. Una factura → N notas de crédito.
27. Rutas `/firmar` y `/enviar` SRI son polimórficas — sirven factura y NC sin cambios.
28. `password_debe_cambiar` y `totp_*` defensivos — el login sobrevive sin migración 0008 (degrada a False).
29. Acciones destructivas van por 2-step: ruta `confirmar_*` + partial `_confirmar_accion.html` + motivo obligatorio.
30. `recientes.registrar()` es best-effort. Falla silenciosa si la migración 0010 no está.
31. Errores nunca se muestran raw. Todo pasa por `humanize()` / filter `| humanizar`.
32. Dashboard tiene modo `simple` (4 big KPIs) y `completa` — persiste en session. Sólo afecta Dueño.

## Backlog abierto al cierre de batch 15

### Bloqueado por entorno (no AWS / no .p12)
- **Fase 1.1.2** — Firma electrónica real: contratar .p12 con BanEcuador / Security Data / Uanataca. Cuando llegue: setear `SRI_P12_PATH` + `SRI_P12_PASSWORD` y agregar a requirements: `signxml==3.2.2`, `cryptography>=42.0`, `lxml>=5.0`. Las rutas `/sri/.../firmar` están wireadas.
- **Fase 1.2** — Rotar `1n7el4Pyth0n`: ejecutar `docs/RUNBOOK_rotar_credencial.md` con acceso AWS.
- **Fase 3.1** — IP allowlist real: configurar `ROLE_IP_ALLOWLIST_*` cuando se sepa la IP fija de fábrica.

### Ejecutable cuando haya datos reales
- Benchmark `scripts/dashboard_profile.py` contra RDS → decidir parallel vs sequential.
- `COSTOS_OT_ADAPTER=metabase|postgres` cuando formulas_app migre/se conecte.

### Próximos items chicos (~15-30 min cada uno)
- Aplicar `_confirmar_accion.html` partial a `retenciones.anular`, `compras.anular`, `provisiones.eliminar` (quedan 3 — patrón ya implementado en facturas/cheques).
- Agregar tipos a `recientes` (posdat, retención).
- Aplicar `humanize` a flash messages también, no sólo templates con `exc`.
- Kbd hint más visible en el header para onboarding del Ctrl+K.

### Items de mediano plazo
- SRI tipos nuevos: notas de débito (`06`), guías de remisión (`06` también pero XSD distinto), retenciones electrónicas.
- ATS (Anexo Transaccional Simplificado) mensual al SRI.
- Puente bidireccional `formulas_app` → Programa Core (trigger al cerrar OT).
- Bulk actions: seleccionar N facturas vencidas y mandar email de cobranza.

## Comandos operativos

```bash
# Test + lint rápido antes de commit
python -m pytest -q                             # 362 tests esperados
python -m ruff check .                          # archivos nuevos deben estar clean
python tests/test_routes_smoke.py               # 57 routes

# Deploy
python scripts/migrate.py --status              # qué falta aplicar
python scripts/migrate.py                        # aplica todo lo pendiente
python scripts/procesa_provisiones_mensual.py    # one-shot manual del cron mensual

# Debugging
python scripts/migrate.py --dry-run              # previsualiza
python scripts/dashboard_profile.py              # benchmark parallel vs sequential
DASHBOARD_MODE=sequential ./launcher.sh          # correr en modo secuencial

# Docker (alternativa a launcher.sh)
docker compose up -d
docker compose exec app python scripts/migrate.py
docker compose exec app python scripts/seed_roles.py
curl http://localhost:5050/healthz
```

## Credenciales y URLs

- **Dev local**: `http://localhost:5050/login` — `tamara / intela2026` (default del `.env` generado por `launcher.sh`).
- **DB local**: Postgres en `localhost:5432`, user `postgres` o `intela`, pwd `postgres` o `intela` (ver `.env`).
- **Prod (futuro)**: RDS en us-east-2 + EC2 Windows. Ver skill `intela-aws-deploy`.
- **SRI certificación**: `celcer.sri.gob.ec`. Producción: `cel.sri.gob.ec`. Env var `SRI_AMBIENTE=1` o `2`.

## Batch 16 (2026-04-24) — 4 quick wins

### 1. `compras.anular` con 2-step — nueva feature end-to-end
**Deuda aclarada:** cuando se cerró batch 15, el backlog decía "Aplicar
_confirmar_accion.html a retenciones.anular, compras.anular,
provisiones.eliminar". Al empezar batch 16 descubrimos que `retenciones`
y `provisiones` YA tenían el 2-step aplicado (se debió hacer en un
micro-batch no documentado); solo `compras.anular` faltaba — y no era un
wrapper sobre lógica existente, era una feature entera porque
`scintela.compra` no tenía siquiera columna `stat`.

**Migración 0011** (aditiva + idempotente):

```sql
ALTER TABLE scintela.compra
    ADD COLUMN IF NOT EXISTS stat        char(1),
    ADD COLUMN IF NOT EXISTS observacion varchar(200);
CREATE INDEX IF NOT EXISTS idx_compra_stat
    ON scintela.compra (stat)
    WHERE stat IS NOT NULL;
```

Paridad exacta con `scintela.factura`: `stat='Y'` = anulada; NULL/vacío =
vigente. Index parcial porque las anuladas son minoría.

**Queries nuevas** en `modules/compras/queries.py`:
- `por_id(id_compra) -> dict | None` — ficha completa para el confirm view.
- `anular(id_compra, *, motivo, usuario) -> int` — marca stat='Y' +
  apendea `[ANUL] motivo` a observacion, Y BORRA la posdat hermana
  (prov+num match) en la misma `db.tx()`. Espejo de `posdat.anular`
  porque la obligación de pago desaparece con la compra.
- `buscar(...)` ahora acepta `incluir_anuladas: bool = False`. Default
  esconde las anuladas. El `SELECT` devuelve `stat` y `observacion` para
  el badge en lista.

**Views nuevas** en `modules/compras/views.py`:
- `GET /compras/<id>/confirmar-anulacion` — render de `_confirmar_accion.html`
  con detalle de la compra + campo motivo.
- `POST /compras/<id>/anular` — ejecuta `queries.anular()`, flash OK, redirect a lista.

**Lista** — badge rojo "ANUL" con tooltip del motivo, fila opaca + line-through
en Kg/Importe, toggle "ver anuladas / ocultar anuladas" en el header.

**Invariante nuevo:** cuando una compra se anula, se borra su posdat hermano
por `(prov, num)` en la misma tx. El rollup de pasivos (`TOTP`) cae
automáticamente sin recálculo.

### 2. Tipos `posdat` y `retencion` en recientes
`modules/recientes/queries.py::_TIPOS_VALIDOS` extendido a 6 tipos (los 4 originales
+ `posdat` + `retencion`). Registro:
- **posdat** → en la rama GET de `/posdat/<id>/editar` (es el único detail view).
- **retencion** → en `/retenciones/<id>/confirmar-anulacion` (idem,
  único detail view — por permiso, solo gente con `retenciones.anular` va a poder
  registrar retenciones en sus recientes).

**Template `base.html`** — nuevos `elif r.tipo == 'posdat'` / `'retencion'` con
dots naranja y púrpura. `url_for` apunta a las mismas rutas detail.

### 3. `flash_exc()` — humanize() en todos los flash de excepción
**Helper nuevo** en `error_messages.py`:

```python
def flash_exc(prefix: str, exc: Exception, category: str = "error") -> None:
    from flask import flash as _flash
    _flash(f"{prefix.rstrip(':').rstrip()}: {humanize(exc)}", category)
```

Import lazy de flask para no romper uso desde scripts/tests de queries.

**Aplicado en 25 sites** vía batch replace (regex + verificación manual)
en 13 archivos (periodos, compras, conciliacion, cartera, posdat, provisiones,
sri, usuarios, cheques, proveedores, facturas, clientes, retenciones).

Resultado: cuando falla un UPDATE por FK violation, el usuario ve
*"No pude anular: Referencia inválida: el registro al que apuntás no
existe."* en vez de *"No pude anular: (psycopg2.errors.ForeignKeyViolation)
update or delete on table "factura" violates foreign key constraint..."*.

**Gotcha:** el script de batch-replace puso el import `from error_messages
import flash_exc` DENTRO del multi-line `from flask import (` de varios
archivos, rompiendo la sintaxis. Fix: script de reparación ad-hoc que
busca el patrón roto `from flask import (\n\nfrom error_messages import
flash_exc\n` y reubica el import DESPUÉS del `)`. Aprendizaje: para
inyectar imports al tope de un archivo, matchear hasta el `)` de cierre
del import multi-line, no la primer línea.

### 4. Kbd hint más visible en el header
**Antes:** botón chico "Buscar [Ctrl+K]" en el header, 180px de ancho,
poco llamativo.

**Después:**
- Botón ancho (min-width 280px) con placeholder descriptivo
  *"Buscar cliente, factura, cheque…"*.
- Kbd pill con borde + monoespaciada; platform detection en JS: muestra
  `⌘K` en Mac/iOS, `Ctrl+K` en Windows/Linux (via `navigator.platform`
  regex).
- **Pulse one-shot** para usuarios nuevos: `@keyframes gs-breathe` CSS
  de 2.2s × 3 ciclos en box-shadow azul. Se activa solo si
  `localStorage.__gs_seen !== '1'`. Al primer Ctrl+K el flag se setea y
  el pulse nunca más se muestra. Respeta `prefers-reduced-motion: reduce`.

**Rule:** usar `localStorage` para preferencias de onboarding a nivel
browser. No es Claude artifact — es un Flask app real, localStorage está
permitido. Para preferencias por-usuario-DB hay que ir a
`seguridad.usuario_preferencias` (no existe todavía, postergado).

### Verificación batch 16

```
ruff check modules/ error_messages.py    → All checks passed!
pytest -q (excluyendo @pytest.mark.db)   → 362 passed in 2.33s
tests/test_routes_smoke.py               → 57 GET routes walked, 0 failures
node --check static/app.js                → OK
```

### Deploy pendiente para batch 16
- Aplicar `migrations/0011_compra_stat.sql` en RDS antes del ship.
  Aditiva, idempotente, segura de correr en caliente.
- Regenerar tailwind CSS si el build lo requiere (no tocamos classes nuevas
  en el pulse — `@keyframes` vive en `<style>` inline, el ring es rgba
  puro).

### Backlog post-batch-16
Sin cambios estructurales — se tachan de "Próximos items chicos" los 4
items que se cerraron hoy. Lo que sigue abierto:

**Bloqueado por entorno (no AWS / no .p12):**
- Firma SRI real con .p12 (BanEcuador/Security Data/Uanataca).
- Rotar `1n7el4Pyth0n`.
- IP allowlist real.

**Medio plazo:**
- Bulk actions en cartera aging.
- SRI notas de débito + retenciones electrónicas (misma shape que NC).
- ATS mensual.
- Puente bidireccional `formulas_app` → Core.
- `seguridad.usuario_preferencias` para onboarding/ajustes por usuario
  (hoy usamos localStorage browser-side).

**XS nuevo:**
- Test unitarios para `queries.anular` de compras (happy + guard de
  ya-anulada + período cerrado). Patrón igual a `test_cheques_reversar.py`.
- Migrar los `flash(f"No pude X: {e}", ...)` que quedaron con suffix
  después de `{e}` (solo 1 en sri/views.py:377) a un helper
  `flash_exc_suffix()` o similar.

## Decisión de dónde seguir

- **Sesión corta**: aplicar 2-step a los 3 módulos restantes (retenciones/compras/provisiones), agregar tipos a recientes, kbd hints.
- **Sesión media**: Bulk actions en cartera aging, o `humanize()` en flash messages globalmente.
- **Sesión larga**: Notas de débito y retenciones electrónicas SRI (mismo pattern que NC).
- **Proyecto aparte**: ATS mensual + firma real .p12 cuando llegue.

## Stack tecnológico al cierre

- **Runtime**: Python 3.11 (Docker) / 3.10 compatible (sandbox dev).
- **Framework**: Flask 3.0 + Jinja + Tailwind (compilado) + HTMX + vanilla JS.
- **DB**: PostgreSQL 16 (dev: local; prod: RDS us-east-2). Schemas: `scintela` + `seguridad`.
- **Auth**: bcrypt + Flask session + Flask-WTF CSRF + Flask-Limiter (login).
- **2FA**: pyotp + qrcode (opt-in).
- **SRI**: `requests` + envelopes SOAP manuales + (pending) signxml.
- **Deploy**: Waitress + Docker / ECS (futuro) o Windows Scheduled Task (actual EC2).
- **Tests**: pytest + ruff. pytest-postgresql + psycopg[binary] para `@pytest.mark.db`.
