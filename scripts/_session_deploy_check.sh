#!/usr/bin/env bash
# Wrapper one-shot para la sesión de deploy-check del 2026-04-17.
#
# Qué hace (en orden):
#   1) Muestra python version + conexión Postgres OK.
#   2) Cuenta roles/usuarios ANTES (para poder comparar después).
#   3) Marca 0001-0005 como aplicadas SIN ejecutarlas (el dump ya las trae).
#      Esto evita que 0001 haga TRUNCATE y borre al admin.
#   4) Corre `migrate.py` normal — sólo va a aplicar 0006 y 0007 (las nuevas).
#   5) Muestra --status después.
#   6) Corre `procesa_provisiones_mensual.py` en modo real (es idempotente:
#      si las procs no existen sale con exit 2 y no toca nada).
#   7) Cuenta roles/usuarios DESPUÉS.
#
# Toda la salida va a scripts/_session_deploy_check.log. Seguro para re-correr.
set -u
cd "$(dirname "$0")/.."
LOG="scripts/_session_deploy_check.log"

# Prefer .venv (launcher.sh uses it). Plain venv/ is a stale alternative.
if [ -d .venv ]; then
  PY=.venv/bin/python
elif [ -d venv ]; then
  PY=venv/bin/python
else
  PY=python3
fi

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') — deploy check & apply ==="
  echo
  echo "--- python version ---"
  "$PY" --version
  echo "PY=$PY"
  echo
  echo "--- postgres reachable? ---"
  "$PY" <<'PYEOF'
import os
from dotenv import load_dotenv
load_dotenv()
import psycopg2
try:
    c = psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT","5432"),
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
    )
    cur = c.cursor()
    cur.execute("SELECT version()")
    print(cur.fetchone()[0][:80])
    c.close()
    print("OK")
except Exception as e:
    print("FAIL:", e)
    raise
PYEOF
  echo
  echo "--- roles/usuarios ANTES ---"
  "$PY" <<'PYEOF'
import os
from dotenv import load_dotenv
load_dotenv()
import psycopg2
c = psycopg2.connect(
    host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT","5432"),
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
)
cur = c.cursor()
cur.execute("SELECT COUNT(*) FROM seguridad.rol"); print("roles:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM seguridad.usuario WHERE activo"); print("usuarios activos:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM seguridad.permiso"); print("permisos:", cur.fetchone()[0])
cur.execute("SELECT id_rol, nombre_rol FROM seguridad.rol ORDER BY id_rol"); print("roles actuales:", cur.fetchall())
c.close()
PYEOF
  echo
  echo "--- marcar 0001-0005 como aplicadas (backfill puro del tracker) ---"
  "$PY" <<'PYEOF'
import os, hashlib
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
import psycopg2

ROOT = Path(__file__).resolve().parent if "__file__" in dir() else Path.cwd()
MIG = Path("migrations")

# Backfill solo las que aún NO están en el tracker.
c = psycopg2.connect(
    host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT","5432"),
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
)
cur = c.cursor()

# Asegurar tracker existe (normalmente lo crea migrate.py en _ensure_tracker).
cur.execute("""
CREATE SCHEMA IF NOT EXISTS seguridad;
CREATE TABLE IF NOT EXISTS seguridad.migraciones_aplicadas (
    version     varchar(4)  PRIMARY KEY,
    nombre      varchar(200) NOT NULL,
    aplicada_en timestamp    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    duracion_ms integer,
    checksum    varchar(64)
);
""")

def _csum(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:64]

to_mark = [
    ("0001", "0001_seguridad_fks",   "migrations/0001_seguridad_fks.sql"),
    ("0002", "0002_indexes",         "migrations/0002_indexes.sql"),
    ("0003", "0003_seed_roles",      "migrations/0003_seed_roles.py"),
    ("0004", "0004_bitacora",        "migrations/0004_bitacora.sql"),
    ("0005", "0005_periodos",        "migrations/0005_periodos.sql"),
]
for v, name, path in to_mark:
    cur.execute(
        """INSERT INTO seguridad.migraciones_aplicadas (version, nombre, duracion_ms, checksum)
           VALUES (%s, %s, 0, %s)
           ON CONFLICT (version) DO NOTHING
           RETURNING version""",
        (v, name, _csum(path)),
    )
    marked = cur.fetchone()
    print(f"  {v} {'backfill' if marked else 'ya estaba'}")

c.commit()
c.close()
print("OK")
PYEOF
  echo
  echo "--- migrate.py --status (después del backfill) ---"
  "$PY" scripts/migrate.py --status 2>&1 || true
  echo
  echo "--- migrate.py APPLY (debería correr sólo 0006 + 0007) ---"
  "$PY" scripts/migrate.py 2>&1 || true
  echo
  echo "--- migrate.py --status (después del apply) ---"
  "$PY" scripts/migrate.py --status 2>&1 || true
  echo
  echo "--- roles/usuarios DESPUÉS ---"
  "$PY" <<'PYEOF'
import os
from dotenv import load_dotenv
load_dotenv()
import psycopg2
c = psycopg2.connect(
    host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT","5432"),
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
)
cur = c.cursor()
cur.execute("SELECT COUNT(*) FROM seguridad.rol"); print("roles:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM seguridad.usuario WHERE activo"); print("usuarios activos:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM seguridad.permiso"); print("permisos:", cur.fetchone()[0])
cur.execute("SELECT id_rol, nombre_rol FROM seguridad.rol ORDER BY id_rol"); print("roles actuales:", cur.fetchall())

# Verificar que 0006 realmente agregó la columna
cur.execute("""
    SELECT column_name FROM information_schema.columns
    WHERE table_schema='scintela' AND table_name='bitacora_acciones'
      AND column_name='request_id'
""")
print("bitacora_acciones.request_id existe?:", "SÍ" if cur.fetchone() else "NO")

# Verificar que 0007 realmente creó la tabla
cur.execute("""
    SELECT to_regclass('scintela.ejecuciones_tareas') IS NOT NULL
""")
print("scintela.ejecuciones_tareas existe?:", "SÍ" if cur.fetchone()[0] else "NO")

c.close()
PYEOF
  echo
  echo "--- procesa_provisiones_mensual.py (dry manual) ---"
  "$PY" scripts/procesa_provisiones_mensual.py 2>&1 || echo "[exit code: $?]"
  echo
  echo "--- FIN ==="
} > "$LOG" 2>&1

echo "Listo. Salida en $LOG"
