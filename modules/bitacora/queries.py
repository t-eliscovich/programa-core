"""Consultas sobre scintela.bitacora_acciones (auditoría)."""
import db


def listar(
    *,
    q: str = "",
    usuario: str | None = None,
    modulo: str | None = None,
    entidad: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    request_id: str | None = None,
    limite: int = 300,
) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # Si viene request_id, ignoramos el resto de filtros y rango de fechas:
    # el caso de uso es "mostrame toda la traza de este request" y un UUID
    # ya es específico de sobra. Lo hacemos pasando el request_id tanto como
    # igualdad exacta como prefijo (permite pegar sólo el tag de 8 chars que
    # logueamos en la consola).
    rid_exact = request_id.strip() if request_id and request_id.strip() else None
    rid_prefix = f"{rid_exact}%" if rid_exact else None
    return db.fetch_all(
        """
        SELECT id_bitacora, ts, usuario, rol, ip, metodo, ruta,
               modulo, accion, entidad, id_entidad, status_http,
               payload, resumen, request_id
        FROM scintela.bitacora_acciones
        WHERE (%(usuario)s IS NULL OR usuario = %(usuario)s)
          AND (%(modulo)s  IS NULL OR modulo  = %(modulo)s)
          AND (%(entidad)s IS NULL OR entidad = %(entidad)s)
          AND (%(desde)s::timestamp IS NULL OR ts >= %(desde)s::timestamp)
          AND (%(hasta)s::timestamp IS NULL OR ts <  %(hasta)s::timestamp + INTERVAL '1 day')
          AND (%(rid)s IS NULL OR request_id LIKE %(rid_prefix)s)
          AND (%(q)s IS NULL
               OR ruta ILIKE %(like)s
               OR COALESCE(resumen,'') ILIKE %(like)s
               OR COALESCE(accion,'')  ILIKE %(like)s
               OR COALESCE(id_entidad,'') ILIKE %(like)s)
        ORDER BY ts DESC, id_bitacora DESC
        LIMIT %(limite)s
        """,
        {
            "q": q or None, "like": like,
            "usuario": usuario or None,
            "modulo": modulo or None,
            "entidad": entidad or None,
            "desde": desde or None, "hasta": hasta or None,
            "rid": rid_exact, "rid_prefix": rid_prefix,
            "limite": limite,
        },
    )


def modulos_distintos() -> list[str]:
    rows = db.fetch_all(
        "SELECT DISTINCT modulo FROM scintela.bitacora_acciones "
        "WHERE modulo IS NOT NULL AND modulo <> '' ORDER BY modulo"
    )
    return [r["modulo"] for r in rows]
