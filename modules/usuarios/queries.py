"""Consultas de usuarios (seguridad.usuario + seguridad.rol)."""

import bcrypt

import db

# TMT 2026-05-21 dueña: set canónico de claves de operador de movimientos.
# El form de /usuarios usa un dropdown cerrado a estas 4 opciones.
# `None` / cadena vacía son válidos (clave opcional).
CLAVES_CANONICAS = ("FED", "TAM", "ALX", "ADR")


def _normalizar_clave(clave: str | None) -> str | None:
    """Devuelve clave en mayúsculas si está en el set canónico, sino None.

    Acepta '' o None → None (clave opcional). Cualquier valor fuera del
    set se rechaza retornando None (el caller decide si flashear warning).
    """
    if not clave:
        return None
    c = clave.strip().upper()
    if c not in CLAVES_CANONICAS:
        return None
    return c


def listar() -> list[dict]:
    return db.fetch_all(
        """
        SELECT u.id_usuario, u.username, u.email, u.activo, u.id_rol,
               r.nombre_rol, u.clave
        FROM seguridad.usuario u
        LEFT JOIN seguridad.rol r USING (id_rol)
        ORDER BY u.activo DESC, u.username
        """
    )


def por_id(id_usuario: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT u.id_usuario, u.username, u.email, u.activo, u.id_rol,
               r.nombre_rol, u.clave
        FROM seguridad.usuario u
        LEFT JOIN seguridad.rol r USING (id_rol)
        WHERE u.id_usuario = %s
        """,
        (id_usuario,),
    )


def roles_disponibles() -> list[dict]:
    return db.fetch_all("SELECT id_rol, nombre_rol FROM seguridad.rol ORDER BY nombre_rol")


def crear(
    *,
    username: str,
    password: str,
    id_rol: int,
    clave: str | None = None,
) -> dict:
    username = (username or "").strip().lower()
    if not username:
        raise ValueError("Username requerido.")
    if len(password or "") < 6:
        raise ValueError("Password debe tener al menos 6 caracteres.")
    if not id_rol:
        raise ValueError("Rol requerido.")
    if db.fetch_one("SELECT 1 FROM seguridad.usuario WHERE lower(username) = %s", (username,)):
        raise ValueError(f"Ya existe un usuario {username!r}.")

    ph = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    return (
        db.execute_returning(
            """
        INSERT INTO seguridad.usuario
            (username, password_hash, id_rol, activo, clave)
        VALUES (%s, %s, %s, TRUE, %s)
        RETURNING id_usuario, username
        """,
            (username[:40], ph, id_rol, _normalizar_clave(clave)),
        )
        or {}
    )


def editar(
    id_usuario: int,
    *,
    id_rol: int | None = None,
    clave: str | None = None,
    activo: bool | None = None,
    password: str | None = None,
    email: str | None = None,
) -> int:
    campos = []
    params: list = []
    if id_rol is not None:
        campos.append("id_rol = %s")
        params.append(id_rol)
    if clave is not None:
        # TMT 2026-05-21 dueña: solo aceptamos claves canónicas (FED/TAM/ALX/ADR).
        campos.append("clave = %s")
        params.append(_normalizar_clave(clave))
    if activo is not None:
        campos.append("activo = %s")
        params.append(bool(activo))
    if email is not None:
        # NULL si viene string vacío. Normalizar a lowercase.
        campos.append("email = %s")
        params.append((email or "").strip().lower() or None)
    if password:
        if len(password) < 6:
            raise ValueError("Password debe tener al menos 6 caracteres.")
        ph = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        campos.append("password_hash = %s")
        params.append(ph)
    if not campos:
        return 0
    params.append(id_usuario)
    return db.execute(
        f"UPDATE seguridad.usuario SET {', '.join(campos)} WHERE id_usuario = %s",
        tuple(params),
    )


def set_activo(id_usuario: int, activo: bool) -> int:
    return db.execute(
        "UPDATE seguridad.usuario SET activo = %s WHERE id_usuario = %s",
        (bool(activo), id_usuario),
    )
