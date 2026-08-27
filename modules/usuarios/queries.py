"""Consultas de usuarios (seguridad.usuario + seguridad.rol)."""

import re

import bcrypt

import db

# TMT 2026-05-21 dueña: set canónico de claves de operador de movimientos
# (sugerencias en el datalist del form). TMT 2026-07-08: ya NO es cerrado —
# se pueden crear claves nuevas (ver _normalizar_clave).
CLAVES_CANONICAS = ("FED", "TAM", "ALX", "ADR")

_CLAVE_RE = re.compile(r"^[A-Z0-9]{1,4}$")


def _normalizar_clave(clave: str | None) -> str | None:
    """Normaliza la clave corta de operador a MAYÚSCULAS.

    TMT 2026-07-08 (dueña "me tiene que dejar crear clave acá"): antes sólo
    aceptaba el set canónico {FED,TAM,ALX,ADR}. Ahora acepta CUALQUIER clave
    corta nueva (1-4 letras/números), normalizada a mayúsculas para evitar
    drift (tel/TEL, and/ADR). '' o None → None (opcional). Lo que no matchea
    el patrón se rechaza (None).
    """
    if not clave:
        return None
    c = clave.strip().upper()
    if not _CLAVE_RE.match(c):
        return None
    return c


def _col_vend() -> str:
    """`u.vend` si la migración 0153 ya corrió; si no, NULL.

    El deploy no corre migraciones, así que la pantalla de usuarios tiene que
    abrir igual en una base que todavía no la tiene.
    """
    from auth import _columna_vend_existe

    return "u.vend" if _columna_vend_existe() else "NULL::varchar AS vend"


def _columna_vend_disponible() -> bool:
    from auth import _columna_vend_existe

    return _columna_vend_existe()


def _normalizar_vend(vend: str | None) -> str | None:
    """Código de vendedor a MAYÚSCULAS, 3 chars. '' / None → None."""
    if not vend:
        return None
    v = vend.strip().upper()[:3]
    return v or None


def vendedores_disponibles() -> list[dict]:
    """Vendedores ACTIVOS del catálogo, para el desplegable de /usuarios.

    Si la tabla no existe (base vieja) devuelve [] en vez de romper la
    pantalla de administración de usuarios.
    """
    try:
        return db.fetch_all(
            """
            SELECT codigo, COALESCE(NULLIF(TRIM(nombre), ''), codigo) AS nombre
              FROM scintela.vendedor
             WHERE COALESCE(activo, TRUE) = TRUE
             ORDER BY codigo
            """
        ) or []
    except Exception:  # noqa: BLE001
        return []


def listar_vendedores() -> list[dict]:
    """Usuarios ACTIVOS que son vendedores (tienen `vend`), para
    /usuarios/vendedores — la pantalla desde la que INT usa "Ver como".

    El nombre sale del catálogo `scintela.vendedor`; si el código no está
    ahí, se muestra el código pelado. Orden alfabético por CÓDIGO, como la
    lista de clientes del portal y la hoja impresa.
    """
    if not _columna_vend_disponible():
        return []
    return db.fetch_all(
        """
        SELECT u.id_usuario, u.username, u.vend,
               COALESCE(NULLIF(TRIM(v.nombre), ''), u.vend) AS vendedor
          FROM seguridad.usuario u
          LEFT JOIN scintela.vendedor v ON v.codigo = u.vend
         WHERE u.activo = TRUE AND COALESCE(TRIM(u.vend), '') <> ''
         ORDER BY u.vend, u.username
        """
    ) or []


def listar() -> list[dict]:
    return db.fetch_all(
        f"""
        SELECT u.id_usuario, u.username, u.email, u.activo, u.id_rol,
               r.nombre_rol, u.clave, {_col_vend()}
        FROM seguridad.usuario u
        LEFT JOIN seguridad.rol r USING (id_rol)
        ORDER BY u.activo DESC, u.username
        """
    )


def por_id(id_usuario: int) -> dict | None:
    return db.fetch_one(
        f"""
        SELECT u.id_usuario, u.username, u.email, u.activo, u.id_rol,
               r.nombre_rol, u.clave, {_col_vend()}
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
    vend: str | None = None,
) -> dict:
    username = (username or "").strip().lower()
    if not username:
        raise ValueError("Username requerido.")
    # TMT 2026-06-03 audit fix: unificar contra auth._valida_password_nueva
    # (10 chars + 1 letra + 1 dígito). Antes este check pedía 6 chars sueltos
    # → usuarios creados quedaban con password que NO podían cambiar después
    # porque el cambiar_password aplica la política completa.
    from auth import _valida_password_nueva as _vpn
    _err = _vpn(password or "")
    if _err:
        raise ValueError(_err)
    if not id_rol:
        raise ValueError("Rol requerido.")
    if db.fetch_one("SELECT 1 FROM seguridad.usuario WHERE lower(username) = %s", (username,)):
        raise ValueError(f"Ya existe un usuario {username!r}.")

    ph = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    vend_norm = _normalizar_vend(vend)
    if vend_norm and not _columna_vend_disponible():
        raise ValueError(
            "Falta correr la migración 0153 antes de asignar un vendedor. "
            "Corrémela en /admin/migraciones y volvé a intentar."
        )
    cols = "(username, password_hash, id_rol, activo, clave"
    vals = "(%s, %s, %s, TRUE, %s"
    params: list = [username[:40], ph, id_rol, _normalizar_clave(clave)]
    if _columna_vend_disponible():
        cols += ", vend"
        vals += ", %s"
        params.append(vend_norm)
    return (
        db.execute_returning(
            f"""
        INSERT INTO seguridad.usuario
            {cols})
        VALUES {vals})
        RETURNING id_usuario, username
        """,
            tuple(params),
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
    vend: str | None = None,
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
    if vend is not None:
        vend_norm = _normalizar_vend(vend)
        if not _columna_vend_disponible():
            if vend_norm:
                raise ValueError(
                    "Falta correr la migración 0153 antes de asignar un "
                    "vendedor. Corrémela en /admin/migraciones."
                )
        else:
            campos.append("vend = %s")
            params.append(vend_norm)
    if password:
        # TMT 2026-06-03 audit fix: mismo policy que crear() / cambiar_password.
        from auth import _valida_password_nueva as _vpn
        _err = _vpn(password)
        if _err:
            raise ValueError(_err)
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
