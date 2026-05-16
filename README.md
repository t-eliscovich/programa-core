# Programa Core — Intela

Reemplazo web del programa dBase/Clipper de 30 años. Flask + PostgreSQL + Jinja
+ Tailwind + HTMX. UI en español.

## Estado actual

Módulos navegables: **login · tablero · informes (balance, cartera, deudas,
flujo, ventas, gastos) · facturas · cheques (crear, aplicar, anular, revertir)
· retenciones · compras · provisiones · bancos · caja · capital · clientes ·
proveedores · proformas · posdat · activos · bitácora**.

Lo que todavía corre en dBase: algunas pantallas de consulta rápida y ciertos
flujos que no pasaron nunca por el programa viejo al día.

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
# abre http://127.0.0.1:5000
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

# Tests
pytest -q
```

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
│   ├── 0001_seguridad_fks.sql
│   ├── 0002_indexes.sql
│   ├── 0003_seed_roles.py
│   ├── 0004_bitacora.sql
│   ├── 0005_periodos.sql
│   ├── 0006_bitacora_request_id.sql
│   └── 0007_ejecuciones_tareas.sql
├── scripts/
│   ├── migrate.py       Runner de migrations
│   ├── seed_roles.py    Primer usuario Dueño (interactivo)
│   └── procesa_provisiones_mensual.py   Cron mensual
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
