"""Consultas de proveedores."""
import db


def por_codigo(codigo_prov: str) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_proveedor, codigo_prov, nombre, telefono, ruc,
               representante, tipo, plazo, retbase, retiva, activo,
               direccion, correo, provincia, canton
        FROM scintela.proveedor
        WHERE codigo_prov = %s
        """,
        (codigo_prov.upper().strip(),),
    )


def crear(
    *,
    codigo_prov: str,
    nombre: str,
    ruc: str | None = None,
    telefono: str | None = None,
    representante: str | None = None,
    tipo: str | None = None,
    plazo: int | None = None,
    retbase: float | None = None,
    retiva: float | None = None,
    direccion: str | None = None,
    correo: str | None = None,
    usuario: str = "web",
) -> dict:
    """Alta de proveedor. Código único."""
    codigo_prov = codigo_prov.upper().strip()
    if not codigo_prov:
        raise ValueError("Código requerido.")
    if not nombre:
        raise ValueError("Nombre requerido.")
    if db.fetch_one(
        "SELECT 1 x FROM scintela.proveedor WHERE codigo_prov = %s", (codigo_prov,)
    ):
        raise ValueError(f"Ya existe un proveedor con código {codigo_prov!r}.")
    return db.execute_returning(
        """
        INSERT INTO scintela.proveedor
            (codigo_prov, nombre, telefono, ruc, representante,
             tipo, plazo, retbase, retiva, direccion, correo,
             activo, usuario_crea)
        VALUES (%s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                '1', %s)
        RETURNING id_proveedor, codigo_prov
        """,
        (
            codigo_prov, nombre[:200],
            (telefono or None) and telefono[:30],
            (ruc or None) and ruc[:16],
            (representante or None) and representante[:100],
            (tipo or None) and tipo[:2],
            plazo, retbase, retiva,
            (direccion or None) and direccion[:200],
            (correo or None) and correo[:80],
            usuario,
        ),
    ) or {}


def editar(
    codigo_prov: str,
    *,
    nombre: str | None = None,
    ruc: str | None = None,
    telefono: str | None = None,
    representante: str | None = None,
    tipo: str | None = None,
    plazo: int | None = None,
    retbase: float | None = None,
    retiva: float | None = None,
    direccion: str | None = None,
    correo: str | None = None,
    activo: str | None = None,
    usuario: str = "web",
) -> int:
    """Update parcial del proveedor. Sólo campos no-None."""
    campos = []
    params: list = []
    mapping_str = {
        "nombre": (nombre, 200),
        "ruc": (ruc, 16),
        "telefono": (telefono, 30),
        "representante": (representante, 100),
        "tipo": (tipo, 2),
        "direccion": (direccion, 200),
        "correo": (correo, 80),
        "activo": (activo, 2),
    }
    for col, (val, maxlen) in mapping_str.items():
        if val is not None:
            campos.append(f"{col} = %s")
            params.append(val[:maxlen] if val else None)

    for col, val in (("plazo", plazo), ("retbase", retbase), ("retiva", retiva)):
        if val is not None:
            campos.append(f"{col} = %s")
            params.append(val)

    if not campos:
        return 0

    campos.append("usuario_modifica = %s")
    params.append(usuario)
    params.append(codigo_prov.upper().strip())
    return db.execute(
        f"UPDATE scintela.proveedor SET {', '.join(campos)} WHERE codigo_prov = %s",
        tuple(params),
    )


def set_activo(codigo_prov: str, activo: bool, usuario: str = "web") -> int:
    """Activar/desactivar proveedor ('1' / '0')."""
    val = "1" if activo else "0"
    return db.execute(
        "UPDATE scintela.proveedor SET activo=%s, usuario_modifica=%s WHERE codigo_prov=%s",
        (val, usuario, codigo_prov.upper().strip()),
    )


def buscar(q: str = "", limite: int = 300) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    return db.fetch_all(
        """
        SELECT p.id_proveedor, p.codigo_prov, p.nombre, p.telefono, p.ruc,
               p.representante, p.tipo, p.plazo, p.retbase, p.retiva, p.activo,
               COALESCE(d.saldo_total, 0) AS saldo_total
        FROM scintela.proveedor p
        LEFT JOIN (
            SELECT prov AS codigo_prov, SUM(importe) AS saldo_total
            FROM scintela.posdat
            WHERE COALESCE(banc, 0) <> 9
              AND COALESCE(importe, 0) > 0
              AND (anulada IS NOT TRUE OR anulada IS NULL)
            GROUP BY prov
        ) d ON d.codigo_prov = p.codigo_prov
        WHERE %(q)s IS NULL
           OR UPPER(p.codigo_prov) LIKE UPPER(%(like)s)
           OR UPPER(p.nombre)      LIKE UPPER(%(like)s)
           OR p.ruc LIKE %(like)s
        ORDER BY COALESCE(d.saldo_total, 0) DESC, p.nombre
        LIMIT %(limite)s
        """,
        {"q": q or None, "like": like, "limite": limite},
    )
