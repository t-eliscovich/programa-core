#!/usr/bin/env bash
# launcher.sh — one-command local dev start for Programa Core.
# Usage: ./launcher.sh            (starts Postgres + Flask, opens Chrome)
#        ./launcher.sh --no-open  (skip opening the browser)

set -eo pipefail
cd "$(dirname "$0")"

YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${YELLOW}▶${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; }

# ---------- 1. Python env ----------
if [ ! -d ".venv" ]; then
  log "Creando entorno virtual en .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
log "Instalando dependencias (silencioso)"
python -m pip install --quiet --upgrade pip
# Install from requirements.txt so any new dep added to the project is
# picked up automatically on the next launcher run.
if [ -f requirements.txt ]; then
  python -m pip install --quiet -r requirements.txt
else
  python -m pip install --quiet flask psycopg2-binary bcrypt python-dotenv waitress flask-wtf flask-limiter
fi

# ---------- 2. Postgres ----------
if ! command -v psql >/dev/null 2>&1; then
  fail "psql no está instalado. Instalá Postgres: brew install postgresql@16"
  exit 1
fi

# Start Postgres via Homebrew if available and not running
if command -v brew >/dev/null 2>&1; then
  if ! pg_isready -q 2>/dev/null; then
    log "Arrancando Postgres con brew services"
    brew services start postgresql@16 >/dev/null 2>&1 || brew services start postgresql >/dev/null 2>&1 || true
    # wait up to 10s
    for i in {1..10}; do
      pg_isready -q 2>/dev/null && break
      sleep 1
    done
  fi
fi

if ! pg_isready -q 2>/dev/null; then
  fail "Postgres no responde. Arrancalo manualmente y volvé a correr este script."
  exit 1
fi
ok "Postgres está corriendo"

# ---------- 3. .env ----------
if [ ! -f ".env" ]; then
  log "Creando .env por defecto"
  cat > .env <<'ENV'
# Programa Core — local dev
DB_HOST=localhost
DB_PORT=5432
DB_NAME=intela
DB_USER=intela
DB_PASSWORD=intela
SECRET_KEY=change-this-in-production-please

# Primer usuario Dueño — se crea al arrancar si la tabla está vacía.
# Podés cambiarlos antes del primer boot, o editar .env y correr:
#   python scripts/seed_roles.py
INTELA_ADMIN_USER=tamara
INTELA_ADMIN_PASSWORD=intela2026
ENV
  ok ".env creado con usuario por defecto tamara / intela2026 — cambiá la contraseña después del primer login"
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

# ---------- 4. Detect the Postgres superuser ----------
# On Homebrew postgres the default superuser is $USER (not 'postgres').
# On Postgres.app / server installs it's 'postgres'. Try both.
PG_ADMIN=""
for candidate in "postgres" "$USER"; do
  if psql -U "$candidate" -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
    PG_ADMIN="$candidate"; break
  fi
done
if [ -z "${PG_ADMIN}" ]; then
  fail "No pude conectar a Postgres como 'postgres' ni como '$USER'."
  fail "Probá: createuser -s \$USER  (y después volvé a correr este script)"
  exit 1
fi
log "Usando superuser Postgres: ${PG_ADMIN}"

# ---------- 4b. Create role/db if missing ----------
if ! psql -U "${PG_ADMIN}" -d postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" 2>/dev/null | grep -q 1; then
  log "Creando rol ${DB_USER}"
  psql -U "${PG_ADMIN}" -d postgres \
       -c "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}' CREATEDB" >/dev/null \
    || { fail "No se pudo crear el rol ${DB_USER}"; exit 1; }
fi
if ! psql -U "${PG_ADMIN}" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" 2>/dev/null | grep -q 1; then
  log "Creando base ${DB_NAME}"
  createdb -U "${PG_ADMIN}" -O "${DB_USER}" "${DB_NAME}" \
    || { fail "No se pudo crear la base ${DB_NAME}"; exit 1; }
fi

# ---------- 5. Load seed if empty ----------
export PGPASSWORD="${DB_PASSWORD}"
TABLES=$(psql -U "${DB_USER}" -h localhost -d "${DB_NAME}" -tAc \
   "SELECT COUNT(*) FROM pg_tables WHERE schemaname IN ('scintela','seguridad')" 2>/dev/null || echo 0)
if [ "${TABLES:-0}" -lt 5 ] && [ -f "intela12042026.sql" ]; then
  log "Cargando seed intela12042026.sql (puede tardar 1-2 minutos)"
  # Cargar como el superuser para evitar problemas de ownership
  if file intela12042026.sql | grep -qi "PostgreSQL custom database dump"; then
    pg_restore -U "${PG_ADMIN}" -d "${DB_NAME}" --no-owner --no-privileges -j 2 intela12042026.sql
  else
    psql -U "${PG_ADMIN}" -d "${DB_NAME}" -q -f intela12042026.sql
  fi
  # Dar ownership al rol de app
  psql -U "${PG_ADMIN}" -d "${DB_NAME}" -c \
    "ALTER SCHEMA scintela OWNER TO ${DB_USER}; ALTER SCHEMA seguridad OWNER TO ${DB_USER};
     DO \$\$ DECLARE r record; BEGIN
       FOR r IN SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('scintela','seguridad') LOOP
         EXECUTE format('ALTER TABLE %I.%I OWNER TO ${DB_USER}', r.schemaname, r.tablename);
       END LOOP;
       FOR r IN SELECT schemaname, sequencename FROM pg_sequences WHERE schemaname IN ('scintela','seguridad') LOOP
         EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO ${DB_USER}', r.schemaname, r.sequencename);
       END LOOP;
     END \$\$;" >/dev/null 2>&1 || true
  ok "Seed cargado"
fi

# Verificación: si las tablas clave no existen, abortar antes de arrancar Flask
NEED_TABLES=$(psql -U "${DB_USER}" -h localhost -d "${DB_NAME}" -tAc \
   "SELECT COUNT(*) FROM pg_tables WHERE schemaname='scintela' AND tablename IN ('factura','cheque','banco')" 2>/dev/null || echo 0)
if [ "${NEED_TABLES:-0}" -lt 3 ]; then
  fail "La base ${DB_NAME} no tiene las tablas esperadas (factura, cheque, banco)."
  fail "Revisá el dump o borrá la BD y volvé a correr: dropdb -U ${PG_ADMIN} ${DB_NAME}"
  exit 1
fi

# ---------- 6. Seed roles + demo user if missing ----------
if [ -f scripts/seed_roles.py ]; then
  log "Sincronizando roles/permisos"
  python scripts/seed_roles.py 2>/dev/null || true
fi

# ---------- 6b. Performance: apply indexes (idempotent) + build Tailwind once ----------
if [ -f scripts/add_indexes.sql ]; then
  log "Aplicando índices (idempotente)"
  PGPASSWORD="${DB_PASSWORD}" psql -U "${DB_USER}" -h localhost -d "${DB_NAME}" \
      -q -f scripts/add_indexes.sql 2>&1 | grep -vE "(NOTICE|already exists)" || true
fi

if [ -f scripts/build-tailwind.sh ] && [ ! -f static/tailwind.css ]; then
  log "Compilando Tailwind (una vez)"
  bash scripts/build-tailwind.sh >/dev/null 2>&1 || true
fi

# ---------- 7. Start Flask in background ----------
PORT=${PORT:-5050}
if lsof -i :"${PORT}" >/dev/null 2>&1; then
  log "Puerto ${PORT} ya tiene algo; matándolo"
  lsof -ti :"${PORT}" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

log "Arrancando Flask en http://127.0.0.1:${PORT}"
FLASK_APP=run.py FLASK_ENV=development PORT="${PORT}" \
  nohup python -c "from run import app; app.run(host='127.0.0.1', port=${PORT}, debug=False, use_reloader=False)" \
  > /tmp/programa-core.log 2>&1 &
FLASK_PID=$!
echo "${FLASK_PID}" > /tmp/programa-core.pid

# wait until server responds
for i in {1..20}; do
  if curl -sf "http://127.0.0.1:${PORT}/" -o /dev/null 2>/dev/null \
     || curl -sf "http://127.0.0.1:${PORT}/auth/login" -o /dev/null 2>/dev/null; then
    ok "Flask responde (PID ${FLASK_PID})"
    break
  fi
  sleep 0.5
done

# ---------- 8. Open Chrome ----------
if [ "${1:-}" != "--no-open" ]; then
  if command -v open >/dev/null 2>&1; then
    open -a "Google Chrome" "http://127.0.0.1:${PORT}/" 2>/dev/null || open "http://127.0.0.1:${PORT}/"
  fi
fi

cat <<EOF

───────────────────────────────────────────
 Programa Core corriendo en http://127.0.0.1:${PORT}/
 Logs:   tail -f /tmp/programa-core.log
 Detener: kill \$(cat /tmp/programa-core.pid)
───────────────────────────────────────────
EOF
