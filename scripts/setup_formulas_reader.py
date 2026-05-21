"""Crea (o rota) el rol read-only `programa_core_reader` en formulas_app.

Diseño igual al `setup_metabase_reader.py` ya documentado en
`intela-aws-deploy`: el rol vive en la DB `postgres` del mismo cluster RDS
que Programa Core, y solo tiene SELECT en las tablas explícitamente listadas
abajo.

Cuándo correrlo:
    - Primera vez en EC2 para crear el rol.
    - Cuando agregás una tabla nueva al `EXPOSED_TABLES` (y necesitás granteársela).
    - Para rotar la password (es idempotente: DROP + CREATE).

Cómo correrlo (RDS es privada — solo desde EC2):

    aws ssm send-command \\
      --region us-east-2 --instance-ids i-0fcca4d7029f08489 \\
      --document-name "AWS-RunPowerShellScript" \\
      --parameters 'commands=["$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable(\\"DATABASE_URL\\",\\"Machine\\"); C:\\\\Python312\\\\python.exe C:\\\\programa-core\\\\scripts\\\\setup_formulas_reader.py"]' \\
      --output text --query 'Command.CommandId'

Imprime al final la connection string lista para pegar como
`FORMULAS_DATABASE_URL` en el env Machine del EC2.
"""

from __future__ import annotations

import os
import secrets
import sys
from urllib.parse import urlparse

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
except ImportError:
    print("ERROR: psycopg2 no instalado en este Python", file=sys.stderr)
    sys.exit(2)


# La DB de formulas_app dentro del cluster RDS. NO es la DB de Programa Core.
FORMULAS_DB_NAME = "postgres"

# Tablas que Programa Core necesita leer de formulas_app. Si la app necesita
# una nueva, agregarla acá Y re-correr este script en EC2.
#
# Por qué tabla-por-tabla y no "ALL TABLES IN SCHEMA":
#   - formulas_app tiene tablas internas (jobs, locks, ajustes manuales firmados)
#     que NO queremos exponer.
#   - El día que formulas_app agrega una tabla sensible nueva, no se filtra
#     silenciosamente.
#   - El rol funciona como contrato leíble: estas son las superficies acopladas.
EXPOSED_TABLES = [
    "ordenes",
    "orden_lineas",
    "orden_piezas",
    "formulas",
    "formula_items",
    "productos",
    "inventario",
    "inventario_ajustes",
    "compras",
]


def parse_database_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "host": p.hostname,
        "port": p.port or 5432,
        "user": p.username,
        "password": p.password,
        "dbname": p.path.lstrip("/") if p.path else "postgres",
    }


def main() -> int:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url or url.startswith("postgresql://localhost"):
        print(
            "REFUSING — DATABASE_URL vacía o apunta a localhost. Este script solo corre en EC2 contra RDS.",
            file=sys.stderr,
        )
        return 1

    admin = parse_database_url(url)
    if not (admin["host"] and admin["user"] and admin["password"]):
        print("REFUSING — DATABASE_URL no contiene host/user/password", file=sys.stderr)
        return 1

    new_password = secrets.token_urlsafe(24)

    # Conectamos con AUTOCOMMIT a la DB de formulas_app (NO a la de Programa Core).
    conn = psycopg2.connect(
        host=admin["host"],
        port=admin["port"],
        user=admin["user"],
        password=admin["password"],
        dbname=FORMULAS_DB_NAME,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    try:
        with conn.cursor() as cur:
            # 1) Drop si existe (idempotente). DROP OWNED limpia grants previos.
            # Dos gotchas:
            #   a) DROP OWNED BY falla con UndefinedObject si el rol no existe,
            #      por eso primero chequeamos.
            #   b) En RDS el admin user NO es full-superuser, así que necesita
            #      ser miembro del rol para droppearle objetos. Le damos
            #      membership temporal con GRANT antes del DROP OWNED.
            cur.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'programa_core_reader'"
            )
            if cur.fetchone() is not None:
                cur.execute(
                    sql.SQL("GRANT programa_core_reader TO {}").format(
                        sql.Identifier(admin["user"])
                    )
                )
                cur.execute("DROP OWNED BY programa_core_reader CASCADE")
                cur.execute("DROP ROLE programa_core_reader")

            # 2) Create con la nueva password.
            cur.execute(
                sql.SQL("CREATE ROLE programa_core_reader WITH LOGIN PASSWORD %s"),
                (new_password,),
            )

            # 3) Connect + USAGE en schema public.
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO programa_core_reader").format(
                    sql.Identifier(FORMULAS_DB_NAME)
                )
            )
            cur.execute("GRANT USAGE ON SCHEMA public TO programa_core_reader")

            # 4) SELECT explícito en las tablas expuestas.
            # Tolerante: si una tabla no existe (ej. esquema desactualizado o
            # tabla todavía no creada), warning y seguir. Mejor que fallar
            # la corrida entera por una tabla faltante.
            granted = []
            missing = []
            for tbl in EXPOSED_TABLES:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = %s",
                    (tbl,),
                )
                if cur.fetchone() is None:
                    missing.append(tbl)
                    continue
                cur.execute(
                    sql.SQL("GRANT SELECT ON public.{} TO programa_core_reader").format(sql.Identifier(tbl))
                )
                granted.append(tbl)

            # 5) DEFAULT PRIVILEGES — para tablas nuevas creadas por el owner
            # que queramos exponer en el futuro (agregándolas a EXPOSED_TABLES
            # arriba, pero el GRANT default es para no tener que volver a tocar
            # roles si la tabla ya existía con dueño correcto).
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT SELECT ON TABLES TO programa_core_reader"
                ).format(sql.Identifier(admin["user"]))
            )

        # 6) Imprimir la connection string lista para pegar.
        url_out = (
            f"postgresql://programa_core_reader:{new_password}"
            f"@{admin['host']}:{admin['port']}/{FORMULAS_DB_NAME}?sslmode=require"
        )
        print()
        print("=" * 78)
        print("ROL programa_core_reader (re)creado. Connection string:")
        print()
        print(f"FORMULAS_DATABASE_URL={url_out}")
        print()
        print(f"Tablas con GRANT SELECT ({len(granted)}): {', '.join(granted)}")
        if missing:
            print(f"Tablas no existentes (skipped): {', '.join(missing)}")
        print()
        print("Pasos siguientes:")
        print("  1) Setear esta var a nivel Machine en el EC2:")
        print("     [Environment]::SetEnvironmentVariable(")
        print('         "FORMULAS_DATABASE_URL", "<la string de arriba>", "Machine")')
        print("  2) Reiniciar el Scheduled Task ProgramaCore para que la lea.")
        print("=" * 78)
        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
