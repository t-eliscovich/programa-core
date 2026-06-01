# Programa Core — Intela

Reemplazo web del programa dBase/Clipper de 30 años. Flask + PostgreSQL + Jinja
+ Tailwind. UI en español, paleta sky+slate, dark mode incluido.

## Estado actual

Módulos navegables: **login · menú de operaciones · tablero · informes (balance,
cartera, deudas, flujo, ventas, gastos, estado de cuenta) · facturas · cheques
(crear, aplicar, depositar lote, anular, revertir, postergar, endosar) ·
retenciones · compras · provisiones · bancos (emitir cheque, transferir,
conciliar) · caja · capital · clientes · proveedores · proformas · posdat
(pasivos a proveedores) · activos · stock · historial unificado · bitácora**.

Flujos cubiertos end-to-end con reverso atómico: cobranza, gasto, retiro de
socio, aporte de capital, factura+anular, compra+anular, transferencia
bancaria, depósito de cheques en lote, endoso de cheque a proveedor, emisión
de cheque propio.

### Highlights de UX

- **Menú de operaciones** con cards agrupadas por categoría (cobro y pago /
  movimientos del dueño / movimientos internos / ver) con tonos por sección.
- **Action bar superior** con las 10 operaciones más frecuentes accesibles
  desde cualquier pantalla.
- **Sidebar colapsable** (con estado en localStorage) — secciones Operación /
  Análisis / Mantenimiento.
- **Historial unificado** que registra cada operación como `mov_doble`
  (origen → destino) y permite reverso en un click. Batches de cobranza
  agrupan múltiples movs bajo un `batch_id`.
- **Cobranza inteligente**: botón "T" por factura aplica el saldo máximo
  posible con tolerancia para redondeos legacy DBF.
- **Tres fechas en cheques**: cargado (cuando entró), a depositar (programado),
  depositado (cuando pasó por banco) — separadas en columnas y cards.
- **Headers sortable**: click en cualquier header de tabla ordena por esa
  columna; la columna "Acum." se desatena cuando no estás ordenado por fecha.
- **Fallbacks de identificadores**: facturas/cheques/posdat sin número formal
  (legado DBF) muestran `#id_factura` en gris como fallback.

## Requisitos

- Python 3.10+ (probado en 3.14)
- PostgreSQL 13+
- Node sólo si vas a compilar Tailwind (opcional — hay CDN fallback)
- El dump `intela12042026.sql` restaurado en una base local

## Setup — primera vez (local)

```bash
# 1) Venv. Usamos `.venv` (no `venv/`) — launcher.sh y los scripts auxiliares
#    buscan ese nombre primero.
python3 -m venv .venv
source .venv/bin/activate

# 2) Dependencias Python
pip install -r requirements.txt

# 3) Env
cp .env.example .env
# editar .env con tu password local, SECRET_KEY larga y aleatoria, etc.

# 4) Restaurar el dump
createdb intela
psql intela < intela12042026.sql

# 5) Aplicar migraciones de Programa Core sobre el dump.
#    Esto crea el schema `seguridad.*`, arregla las FKs legacy, siembra
#    los 10 roles canónicos y agrega todo lo que el dump no traía
#    (bitacora, periodos, ejecuciones_tareas, etc.)
python scripts/migrate.py

# 6) Crear el primer usuario Dueño.
python scripts/seed_roles.py
# pide usuario + contraseña — es quien va a poder crear el resto

# 7) Arrancar el server
python run.py
# abre http://127.0.0.1:5050
```

### Atajo: `./launcher.sh`

`launcher.sh` hace pasos 1-2-5-6-7 en una sola corrida. Úsalo a partir del
segundo día — asume que ya clonaste, copiaste `.env` y restauraste el dump.

## Cómo correr día a día

```bash
source .venv/bin/activate
python run.py
```

O simplemente `./launcher.sh` — abre el venv, aplica migraciones nuevas si
las hay, y lanza Flask en modo dev con reload.

## Comandos útiles

```bash
# Estado de migraciones (aplicadas/pendientes)
python scripts/migrate.py --status

# Dry-run de lo que aplicaría
python scripts/migrate.py --dry-run

# Re-aplicar una migración puntual (borra su fila del tracker y la corre)
python scripts/migrate.py --force 0003

# Correr el ciclo mensual de contabilidad (procesa_provisiones +
# actualizar_amortizacion). Idempotente: si ya corrió este mes, sale en 0.
python scripts/procesa_provisiones_mensual.py

# Tests y coverage
make test
# Gate de CI local; los tests @db corren sólo si hay dump legacy restaurado:
make ci
```

Ver `docs/TESTING_COVERAGE.md` para el alcance del gate de coverage al 100% y
los comandos de DB/CI.

## Estructura

```
Programa Core/
├── app.py               Flask factory
├── db.py                Pool psycopg2 + helpers (execute, tx, fetch_*)
├── auth.py              Login, sesiones, @requiere_permiso
├── filters.py           num_es, money_es, fecha_es (filtros Jinja)
├── exports.py           csv_response — export compartido entre list views
├── periodo_guard.py     asegurar_fecha_abierta — bloqueo de meses cerrados
├── run.py               Entry point (dev)
├── launcher.sh          Atajo de bootstrap
├── requirements.txt
├── .env.example
├── config/
│   └── roles.py         Fuente única de verdad roles → permisos
├── migrations/          Migration runner (NNNN_*.sql | .py)
│                        Soporta marker `-- migrate:no-transaction` para
│                        statements tipo CREATE INDEX CONCURRENTLY.
├── scripts/
│   ├── migrate.py                       Runner de migrations
│   ├── seed_roles.py                    Primer usuario Dueño (interactivo)
│   ├── procesa_provisiones_mensual.py   Cron mensual
│   ├── check_salud_dia.py               Semáforo verde/amarillo/rojo
│   │                                    sobre caja/gastos/bancos/mov_doble/
│   │                                    cheques/facturas/posdat/provisiones
│   ├── listar_inconsistencias.py        Lista detallada de issues
│   ├── fix_inconsistencias.py           Auto-fix de issues seguros
│   ├── import_dbf.py                    Importador DBF → Postgres (sync legacy)
│   └── lint_permisos_templates.py       CI check anti-`'x.y' in g.permisos`
├── modules/             Cada módulo = blueprint + queries.py + templates/
│   ├── auth/
│   ├── dashboard/
│   ├── informes/
│   ├── facturas/ cheques/ retenciones/ compras/ provisiones/ bancos/ ...
│   └── bitacora/
├── templates/
│   ├── base.html        Sidebar, topbar, theme toggle
│   └── 403.html
└── static/              Tailwind build (o CDN fallback)
```

## Roles y permisos

Fuente única de verdad: `config/roles.py`. La migración `0003_seed_roles.py`
lee ese archivo y upsertea los roles + permisos en cada deploy.

Roles canónicos (10):

- **Dueño** — `*` (todo)
- **Administrador** — informes + bitácora + usuarios + cierre de período
- **Gerente** — lectura amplia + informes + flujo
- **Contabilidad** — facturas, cheques, compras, bancos, retenciones, caja, capital
- **Compras** — proveedores, compras, provisiones, posdat
- **Cobranzas** — clientes, cobranza, cheques aplicar, retenciones emitir
- **Ventas** — clientes, proformas, cupos
- **Bodega** — productos, stock
- **QC** — tintura, costos, precios, fórmulas
- **Lectura** — sólo `*.ver` en módulos asignados

Para agregar un permiso nuevo: editá `config/roles.py`, corré
`python scripts/migrate.py --force 0003`. Listo.

## Fuente de verdad

- **Lógica de negocio**: los `.PRG` en `/Users/tamaraeliscovich/Documents/INTELA copy/`.
  - `INFORMES.PRG` → balance, cartera, deudas, ventas, gastos
  - `RETENCIO.PRG` → cálculos de retención
  - `BANCOS.PRG` → movimientos bancarios
  - `MENU.PRG` → control de acceso viejo
- **Datos**: el dump Postgres (`intela12042026.sql`) + lo que Programa Core
  crea encima vía migraciones.
- **NO** es fuente de verdad: el rewrite PyQt abandonado (`INTELA.rar`).
  Sólo referencia secundaria.

## Cron mensual (opcional local, obligatorio en producción)

`scripts/procesa_provisiones_mensual.py` corre `scintela.procesa_provisiones`
y `scintela.actualizar_amortizacion`. Trackea cada ejecución en
`scintela.ejecuciones_tareas` con UNIQUE(tarea, periodo), así que es seguro
agendar sin miedo a doble-disparo.

- Local (macOS / Linux): `cron` → primer día del mes.
- Windows Server: ver la skill `intela-aws-deploy` para el Scheduled Task.

Exit codes: `0` = todo OK · `1` = hubo algún error · `2` = la procedure no
existe todavía (migración no corrió). Eso le permite al scheduler alertar
con claridad.

## Seguridad

- Contraseñas: bcrypt.
- CSRF: Flask-WTF en todos los POST.
- Rate-limit: Flask-Limiter en login + rutas sensibles.
- `.env` no se commitea. `SECRET_KEY` rotable sin downtime.
- Sesiones: cookie firmada, expiran a las 12h.
- Auditoría: toda acción escribe en `scintela.bitacora_acciones` con
  `request_id` correlacionable al log HTTP.
- Períodos cerrados: `periodo_guard.asegurar_fecha_abierta()` bloquea
  writes en meses cerrados desde la DB (no desde la UI — la UI puede
  mentir).

## Producción

Misma plantilla que `formulas_app`: Waitress en Windows Server EC2, RDS
PostgreSQL en us-east-2. Ver skill `intela-aws-deploy` para deploy /
rollback / logs.

## Contribuir

- Todo cambio de schema va por `migrations/NNNN_*.sql|.py`, nunca ad-hoc
  en producción.
- Tests: `pytest -q` pasa limpio antes de cada commit. Los tests no
  necesitan Postgres — mockean `db.*`.
- Commits: presente imperativo corto, una línea resume y el cuerpo
  explica el porqué si no es obvio.

## Reglas no-obvias del código

- **Permisos en templates**: usar siempre `tiene_permiso('x.y')` —
  nunca `'x.y' in g.permisos`. El primero honra el wildcard `*` del
  rol Dueño; el segundo no, y deja al Dueño sin botones. El check de
  CI `scripts/lint_permisos_templates.py` lo enforza.

- **Saldos bancarios — convención mixta**: la columna `importe` en
  `transacciones_bancarias` puede venir signed (legacy DBF: negativo
  = egreso) o abs (creado por `bank_helpers`). Para sumar correctamente
  usar `_signed_delta(documento, importe)` de `bank_helpers.py`, que
  detecta el caso automáticamente. Sumas naive `signo * importe` rompen.

- **Walk-forward, no SUM**: para recalcular saldos bancarios siempre
  partir de un ancla conocida y aplicar deltas en orden. Un `SUM`
  sobre toda la tabla puede dar drifts grandes ($800k+ en Pichincha)
  porque la convención mixta arriba mencionada.

- **Reverso atómico**: cada operación que cruza tablas (cobranza,
  depósito, transferencia, anulación, etc.) escribe una fila en
  `mov_doble` con `(origen_table, origen_id, destino_table, destino_id,
  importe, tipo, estado)`. Reverso = update estado='reversado' +
  insertar fila inversa. El dispatcher está en
  `modules/historial/views.py:_REVERSO_DISPATCH`.

- **Batch_id**: cuando una cobranza aplica un cheque a múltiples
  facturas (o un depósito mueve N cheques al banco), todas las filas
  comparten un `batch_id` UUID y el reverso del batch reversa todo
  junto. La UI los muestra agrupados en `/historial`.

- **Stat canónicos cheques**: Z=cartera · P=postdatado · B=depositado
  nuevo · A=depositado legacy · 1,2,3=rebotes (1=primer, 3=tercero) ·
  R=rebotado terminal · D=Daniela (gestión) · E=endosado · X=eliminado.

- **NUMF=0 en facturas legacy**: ~480 facturas DBF importadas vienen
  con `numf=0` (Daniela las registra como cuenta corriente sin
  número formal). Los templates usan fallback `#id_factura` en gris
  cuando `numf=0`. No es bug, es dato origen.
