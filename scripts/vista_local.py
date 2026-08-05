#!/usr/bin/env python3
"""Renderizar cualquier ruta de Programa Core LOCALMENTE, sin deploy.

    python3 scripts/vista_local.py /informes/balance -o /tmp/balance.html

Levanta un Postgres embebido (binarios zonky, sin root ni Docker), restaura la
baseline de tests + migraciones la PRIMERA vez, y renderiza la ruta con el
test client de Flask logueado como Accionista. Una corrida ≈ segundos, contra
~5 min de deploy + verificación por Chrome.

Datos: por defecto usa tests/fixtures/legacy_minimal_dump.sql (poca data, ideal
para layout/CSS/templates). Para data real, poner un dump SQL en $DUMP_LOCAL.

Setup del Postgres embebido (una vez por sandbox):
    A=$(uname -m); case $A in x86_64) Z=amd64;; aarch64) Z=arm64v8;; esac
    curl -sL -o /tmp/pg.jar "https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-linux-$Z/16.4.0/embedded-postgres-binaries-linux-$Z-16.4.0.jar"
    cd /tmp && unzip -o -q pg.jar -d pgbin && cd pgbin && tar xJf postgres-linux-*.txz

OJO sandbox: los PROCESOS mueren entre llamadas de bash (el disco no) — este
script arranca el postgres al empezar. Si ya hay uno corriendo, lo reusa.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
import time
from pathlib import Path

if not hasattr(_dt, "UTC"):  # py3.10
    _dt.UTC = _dt.timezone.utc

ROOT = Path(__file__).resolve().parent.parent
PG_BIN = Path(os.environ.get("PG_BIN", "/tmp/pgbin/bin"))
PGDATA = Path(os.environ.get("PGDATA_LOCAL", "/tmp/pgdata"))
SOCKET_DIR = "/tmp"
PORT = os.environ.get("PG_PORT_LOCAL", "5433")
DBNAME = "programa_core"
FIXTURE = ROOT / "tests" / "fixtures" / "legacy_minimal_dump.sql"


def _conn(dbname: str):
    import psycopg2

    return psycopg2.connect(host=SOCKET_DIR, port=PORT, user="app", dbname=dbname)


def asegurar_postgres() -> None:
    try:
        _conn("postgres").close()
        return  # ya hay uno corriendo
    except Exception:
        pass
    if not (PG_BIN / "postgres").exists():
        sys.exit(f"No está el Postgres embebido en {PG_BIN} — ver docstring (setup una vez).")
    if not PGDATA.exists():
        subprocess.run(
            [PG_BIN / "initdb", "-D", PGDATA, "-U", "app", "--auth=trust", "-E", "UTF8"],
            check=True, capture_output=True,
        )
    subprocess.Popen(
        [PG_BIN / "postgres", "-D", PGDATA, "-p", PORT, "-k", SOCKET_DIR],
        stdout=open("/tmp/pg.log", "ab"), stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(50):
        try:
            _conn("postgres").close()
            return
        except Exception:
            time.sleep(0.2)
    sys.exit("El postgres embebido no levantó — ver /tmp/pg.log")


def asegurar_db() -> None:
    con = _conn("postgres")
    con.autocommit = True
    cur = con.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DBNAME,))
    if not cur.fetchone():
        cur.execute(f"CREATE DATABASE {DBNAME} OWNER app")
    con.close()

    con = _conn(DBNAME)
    con.autocommit = True
    cur = con.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='scintela'"
    )
    vacia = cur.fetchone()[0] == 0
    if vacia:
        dump = Path(os.environ.get("DUMP_LOCAL", FIXTURE))
        print(f"[vista_local] restaurando {dump.name} …")
        cur.execute(dump.read_text())
        con.close()
        print("[vista_local] migraciones …")
        import runpy

        os.environ.update(_env())
        sys.path.insert(0, str(ROOT))
        argv, sys.argv = sys.argv, ["migrate.py"]
        try:
            runpy.run_path(str(ROOT / "scripts" / "migrate.py"), run_name="__main__")
        except SystemExit as e:
            if e.code not in (0, None):
                sys.exit(f"migrate.py falló ({e.code})")
        finally:
            sys.argv = argv
    else:
        con.close()


def _env() -> dict:
    env = dict(os.environ)
    env.update(
        DB_HOST=SOCKET_DIR, DB_PORT=str(PORT), DB_NAME=DBNAME,
        DB_USER="app", DB_PASSWORD="local", SECRET_KEY="dev-local",
        FLASK_ENV="development",
    )
    return env


def render(ruta: str, salida: Path) -> int:
    os.environ.update(_env())
    sys.path.insert(0, str(ROOT))
    from flask import g

    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_local():  # corre DESPUÉS de load_logged_in_user → pisa
        g.user = {
            "id_usuario": 0, "username": "local", "id_rol": 0,
            "nombre_rol": "Accionista", "activo": True, "vend": None,
        }
        g.permisos = {"*"}

    client = app.test_client()
    resp = client.get(ruta, follow_redirects=True)
    salida.write_bytes(resp.data)
    titulo = ""
    if b"<title>" in resp.data:
        titulo = resp.data.split(b"<title>")[1].split(b"</title>")[0].decode("utf8", "ignore").strip()
    print(f"[vista_local] {ruta} → {resp.status_code} · {len(resp.data):,} bytes · «{titulo}» → {salida}")
    return 0 if resp.status_code == 200 else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("ruta", help="p.ej. /informes/balance")
    p.add_argument("-o", "--out", default="/tmp/vista.html", type=Path)
    a = p.parse_args()
    asegurar_postgres()
    asegurar_db()
    return render(a.ruta, a.out)


if __name__ == "__main__":
    raise SystemExit(main())
