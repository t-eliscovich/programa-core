"""Crear o resetear un usuario admin (rol Dueño) con contraseña conocida.

Uso:
    python scripts/reset_admin.py                 # usa env vars o default tamara/intela2026
    INTELA_ADMIN_USER=x INTELA_ADMIN_PASSWORD=y python scripts/reset_admin.py

Diagnóstico:
    python scripts/reset_admin.py --debug
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt  # noqa: E402

import db  # noqa: E402


def diagnose():
    print("=== DIAGNÓSTICO seguridad.* ===\n")

    print("-- roles --")
    for r in db.fetch_all("SELECT id_rol, nombre_rol FROM seguridad.rol ORDER BY id_rol"):
        print(f"  id_rol={r['id_rol']:3d}  nombre={r['nombre_rol']!r}")

    print("\n-- permisos por rol --")
    for r in db.fetch_all("""
        SELECT p.id_rol, r.nombre_rol, count(*) AS n
          FROM seguridad.permiso p
          LEFT JOIN seguridad.rol r ON r.id_rol = p.id_rol
         GROUP BY p.id_rol, r.nombre_rol
         ORDER BY p.id_rol
    """):
        print(f"  id_rol={r['id_rol']:3d}  {r['nombre_rol']!r:20s}  permisos={r['n']}")

    print("\n-- usuarios --")
    for r in db.fetch_all("""
        SELECT u.id_usuario, u.username, u.id_rol, r.nombre_rol, u.activo
          FROM seguridad.usuario u
          LEFT JOIN seguridad.rol r ON r.id_rol = u.id_rol
         ORDER BY u.id_usuario
    """):
        print(f"  id_usuario={r['id_usuario']:3d}  username={r['username']!r:20s}  "
              f"rol={r['nombre_rol']!r:15s}  activo={r['activo']}")

    print("\n-- FK de seguridad.permiso.id_rol --")
    for r in db.fetch_all("""
        SELECT conname, pg_get_constraintdef(oid) AS def
          FROM pg_constraint
         WHERE conrelid = 'seguridad.permiso'::regclass
    """):
        print(f"  {r['conname']}: {r['def']}")


def main():
    if "--debug" in sys.argv:
        diagnose()
        return

    username = (os.environ.get("INTELA_ADMIN_USER") or "tamara").strip().lower()
    password = os.environ.get("INTELA_ADMIN_PASSWORD") or "intela2026"

    # Buscar el rol Dueño que YA tenga permisos cargados (por si hay duplicados).
    rol = db.fetch_one("""
        SELECT r.id_rol
          FROM seguridad.rol r
          JOIN seguridad.permiso p ON p.id_rol = r.id_rol
         WHERE r.nombre_rol = 'Dueño'
         GROUP BY r.id_rol
         ORDER BY count(p.id_permiso) DESC
         LIMIT 1
    """)
    if not rol:
        # Fallback: cualquier Dueño, aunque no tenga permisos
        rol = db.fetch_one("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Dueño' LIMIT 1")
    if not rol:
        print("ERROR: no existe rol 'Dueño'. Corré primero: python scripts/seed_roles.py")
        sys.exit(1)

    id_rol = rol["id_rol"]
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    existente = db.fetch_one(
        "SELECT id_usuario FROM seguridad.usuario WHERE username = %s",
        (username,),
    )
    if existente:
        db.execute(
            """
            UPDATE seguridad.usuario
               SET password_hash = %s,
                   id_rol        = %s,
                   activo        = TRUE
             WHERE id_usuario    = %s
            """,
            (hashed, id_rol, existente["id_usuario"]),
        )
        print(f"✓ Usuario {username!r} actualizado (id_rol={id_rol}, password reseteado, activo).")
    else:
        db.execute(
            """
            INSERT INTO seguridad.usuario (username, password_hash, id_rol, activo)
            VALUES (%s, %s, %s, TRUE)
            """,
            (username, hashed, id_rol),
        )
        print(f"✓ Usuario {username!r} creado (id_rol={id_rol}).")

    print("\nEntrá a http://127.0.0.1:5050/ con:")
    print(f"  usuario:    {username}")
    print(f"  contraseña: {password}")


if __name__ == "__main__":
    main()
