"""One-shot fix: repair seguridad.* FKs, seed roles, create admin, verify login.

Uso:
    python scripts/fix_everything.py

    # override creds:
    INTELA_ADMIN_USER=otra INTELA_ADMIN_PASSWORD=otra-password python scripts/fix_everything.py

Qué hace (todo dentro de UNA transacción, con rollback si algo falla):
    1. Dropea FKs rotas en seguridad.permiso / seguridad.usuario.
    2. Asegura PK + UNIQUE + secuencia serial en seguridad.rol / usuario / permiso.
    3. Vacía las tres tablas (TRUNCATE RESTART IDENTITY).
    4. Recrea FKs correctamente apuntando a seguridad.rol.
    5. Re-siembra los 6 roles con sus permisos.
    6. Crea usuario admin (username + password via env vars o defaults).
    7. Verifica: cuentas, hash bcrypt, y simula exactamente la query del login.

Exit 0 solo si TODO pasó. Si rollback, la DB queda igual que antes.
"""
import os
import sys

import bcrypt
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from scripts.seed_roles import ROLES  # noqa: E402

# ---------------------------------------------------------------------------
# DDL helpers — se ejecutan sobre la conn compartida, SIN commit automático.
# Usamos cursor.execute(sql) sin segundo arg para que psycopg2 no intente
# substitución de parámetros sobre los `%s` / `%I` del PL/pgSQL format().
# ---------------------------------------------------------------------------

def ddl(conn, sql: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)


def fix_constraints(conn) -> None:
    print("1) Dropeando FKs existentes sobre seguridad.permiso / seguridad.usuario…")
    ddl(conn, """
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT conname, conrelid::regclass AS tbl
                  FROM pg_constraint
                 WHERE contype = 'f'
                   AND conrelid IN ('seguridad.permiso'::regclass,
                                    'seguridad.usuario'::regclass)
            LOOP
                EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.tbl, c.conname);
            END LOOP;
        END$$;
    """)

    print("2) Asegurando PKs en seguridad.rol / usuario / permiso…")
    ddl(conn, """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conrelid = 'seguridad.rol'::regclass AND contype = 'p') THEN
                ALTER TABLE seguridad.rol ADD PRIMARY KEY (id_rol);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conrelid = 'seguridad.usuario'::regclass AND contype = 'p') THEN
                ALTER TABLE seguridad.usuario ADD PRIMARY KEY (id_usuario);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conrelid = 'seguridad.permiso'::regclass AND contype = 'p') THEN
                ALTER TABLE seguridad.permiso ADD PRIMARY KEY (id_permiso);
            END IF;
        END$$;
    """)

    print("3) Asegurando UNIQUE en seguridad.rol(nombre_rol)…")
    ddl(conn, """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conrelid = 'seguridad.rol'::regclass
                              AND contype = 'u'
                              AND pg_get_constraintdef(oid) LIKE '%(nombre_rol)%') THEN
                ALTER TABLE seguridad.rol ADD CONSTRAINT rol_nombre_rol_key UNIQUE (nombre_rol);
            END IF;
        END$$;
    """)

    print("4) Asegurando secuencias serial…")
    ddl(conn, """
        DO $$
        BEGIN
            IF (SELECT pg_get_serial_sequence('seguridad.rol', 'id_rol')) IS NULL THEN
                CREATE SEQUENCE IF NOT EXISTS seguridad.rol_id_rol_seq;
                ALTER TABLE seguridad.rol
                    ALTER COLUMN id_rol SET DEFAULT nextval('seguridad.rol_id_rol_seq');
                ALTER SEQUENCE seguridad.rol_id_rol_seq OWNED BY seguridad.rol.id_rol;
                PERFORM setval('seguridad.rol_id_rol_seq',
                    GREATEST(COALESCE((SELECT MAX(id_rol) FROM seguridad.rol), 0), 1), true);
            END IF;
            IF (SELECT pg_get_serial_sequence('seguridad.usuario', 'id_usuario')) IS NULL THEN
                CREATE SEQUENCE IF NOT EXISTS seguridad.usuario_id_usuario_seq;
                ALTER TABLE seguridad.usuario
                    ALTER COLUMN id_usuario SET DEFAULT nextval('seguridad.usuario_id_usuario_seq');
                ALTER SEQUENCE seguridad.usuario_id_usuario_seq OWNED BY seguridad.usuario.id_usuario;
                PERFORM setval('seguridad.usuario_id_usuario_seq',
                    GREATEST(COALESCE((SELECT MAX(id_usuario) FROM seguridad.usuario), 0), 1), true);
            END IF;
            IF (SELECT pg_get_serial_sequence('seguridad.permiso', 'id_permiso')) IS NULL THEN
                CREATE SEQUENCE IF NOT EXISTS seguridad.permiso_id_permiso_seq;
                ALTER TABLE seguridad.permiso
                    ALTER COLUMN id_permiso SET DEFAULT nextval('seguridad.permiso_id_permiso_seq');
                ALTER SEQUENCE seguridad.permiso_id_permiso_seq OWNED BY seguridad.permiso.id_permiso;
                PERFORM setval('seguridad.permiso_id_permiso_seq',
                    GREATEST(COALESCE((SELECT MAX(id_permiso) FROM seguridad.permiso), 0), 1), true);
            END IF;
        END$$;
    """)

    print("5) Limpiando roles/permisos/usuarios viejos…")
    ddl(conn, "TRUNCATE seguridad.permiso, seguridad.usuario, seguridad.rol RESTART IDENTITY CASCADE;")

    print("6) Recreando FKs apuntando a seguridad.rol…")
    ddl(conn, """
        ALTER TABLE seguridad.permiso
            ADD CONSTRAINT permiso_id_rol_fkey
            FOREIGN KEY (id_rol) REFERENCES seguridad.rol(id_rol) ON DELETE CASCADE;
        ALTER TABLE seguridad.usuario
            ADD CONSTRAINT usuario_id_rol_fkey
            FOREIGN KEY (id_rol) REFERENCES seguridad.rol(id_rol);
    """)


def seed_roles(conn) -> None:
    print("\n7) Cargando roles + permisos…")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for nombre, permisos in ROLES:
            cur.execute(
                "INSERT INTO seguridad.rol (nombre_rol) VALUES (%s) RETURNING id_rol",
                (nombre,),
            )
            id_rol = cur.fetchone()["id_rol"]
            for p in permisos:
                cur.execute(
                    "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s, %s)",
                    (id_rol, p),
                )
            print(f"   ✓ {nombre!r} (id_rol={id_rol}) con {len(permisos)} permisos")


def crear_admin(conn, username: str, password: str) -> int:
    print(f"\n8) Creando admin {username!r}…")
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Dueño'")
        row = cur.fetchone()
        if not row:
            raise RuntimeError("No se creó el rol 'Dueño' — abortando.")
        id_rol = row["id_rol"]

        cur.execute(
            """
            INSERT INTO seguridad.usuario (username, password_hash, id_rol, activo)
            VALUES (%s, %s, %s, TRUE)
            RETURNING id_usuario
            """,
            (username, hashed, id_rol),
        )
        id_usuario = cur.fetchone()["id_usuario"]
        print(f"   ✓ usuario id={id_usuario} con id_rol={id_rol}")
        return id_usuario


# ---------------------------------------------------------------------------
# Verificación — corre DESPUÉS del commit, con un SELECT nuevo para confirmar
# que los datos estén efectivamente persistidos y que el login funcione.
# ---------------------------------------------------------------------------

def verify(username: str, password: str) -> None:
    print("\n9) Verificando estado final (post-commit)…")
    fails: list[str] = []

    roles = db.fetch_all("SELECT id_rol, nombre_rol FROM seguridad.rol ORDER BY id_rol")
    print(f"   roles: {len(roles)}")
    if len(roles) != len(ROLES):
        fails.append(f"esperaba {len(ROLES)} roles, hay {len(roles)}")

    permisos = db.fetch_one("SELECT count(*) AS n FROM seguridad.permiso")
    print(f"   permisos: {permisos['n']}")
    if permisos["n"] == 0:
        fails.append("tabla permiso vacía")

    # Simula EXACTAMENTE la query que hace auth.login()
    u = db.fetch_one(
        """
        SELECT id_usuario, username, password_hash, activo
          FROM seguridad.usuario
         WHERE lower(username) = %s
        """,
        (username.lower(),),
    )
    if not u:
        fails.append(f"usuario {username!r} no se encuentra por el SELECT de login")
    elif not u["activo"]:
        fails.append(f"usuario {username!r} está inactivo")
    else:
        hashed = u["password_hash"]
        if isinstance(hashed, str):
            hashed = hashed.encode("utf-8")
        if bcrypt.checkpw(password.encode("utf-8"), hashed):
            print(f"   ✓ bcrypt.checkpw({username!r}, …) → True")
        else:
            fails.append("bcrypt.checkpw falló — el hash no matchea la password")

    # Simula load_logged_in_user → debe traer permisos del rol
    if u and u["activo"]:
        full = db.fetch_one(
            """
            SELECT u.id_usuario, u.id_rol, r.nombre_rol
              FROM seguridad.usuario u
              JOIN seguridad.rol r USING (id_rol)
             WHERE u.id_usuario = %s AND u.activo = TRUE
            """,
            (u["id_usuario"],),
        )
        if not full:
            fails.append("JOIN usuario↔rol falla — FK/role rota")
        else:
            perms = db.fetch_all(
                "SELECT nombre_opcion FROM seguridad.permiso WHERE id_rol = %s",
                (full["id_rol"],),
            )
            nombres = {p["nombre_opcion"] for p in perms}
            print(f"   permisos del rol {full['nombre_rol']!r}: {len(nombres)}")
            if full["nombre_rol"] == "Dueño" and "*" not in nombres:
                fails.append("Dueño no tiene wildcard '*'")

    if fails:
        print("\n✗ FALLAS DE VERIFICACIÓN:")
        for f in fails:
            print(f"   - {f}")
        sys.exit(1)

    print("\n✓ Todo OK. Entrá a http://127.0.0.1:5050/")
    print(f"   usuario:    {username}")
    print(f"   contraseña: {password}")


def main() -> None:
    print("=== Programa Core — fix completo (transaccional) ===\n")
    username = (os.environ.get("INTELA_ADMIN_USER") or "tamara").strip().lower()
    password = os.environ.get("INTELA_ADMIN_PASSWORD") or "intela2026"

    with db.get_conn() as conn:
        try:
            fix_constraints(conn)
            seed_roles(conn)
            crear_admin(conn, username, password)
            conn.commit()
            print("\n✓ commit exitoso.")
        except Exception as e:
            conn.rollback()
            print(f"\n✗ rollback. La DB quedó intacta. Error:\n   {type(e).__name__}: {e}")
            sys.exit(2)

    verify(username, password)


if __name__ == "__main__":
    main()
