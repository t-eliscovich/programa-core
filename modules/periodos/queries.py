"""Períodos contables — cierre y control de write-block."""
from datetime import date

import db


def listar(limite: int = 36) -> list[dict]:
    return db.fetch_all(
        """
        SELECT id_periodo, anio, mes, fecha_desde, fecha_hasta,
               estado, cerrado_por, fecha_cierre, motivo
        FROM scintela.periodos_contables
        ORDER BY anio DESC, mes DESC
        LIMIT %s
        """,
        (limite,),
    )


def por_fecha(fecha: date) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_periodo, anio, mes, fecha_desde, fecha_hasta, estado, cerrado_por
        FROM scintela.periodos_contables
        WHERE fecha_desde <= %s AND fecha_hasta >= %s
        """,
        (fecha, fecha),
    )


def ultimo_cierre() -> dict | None:
    return db.fetch_one(
        """
        SELECT id_periodo, anio, mes, fecha_hasta, cerrado_por, fecha_cierre
        FROM scintela.periodos_contables
        WHERE estado = 'C'
        ORDER BY fecha_hasta DESC
        LIMIT 1
        """
    )


def fecha_esta_bloqueada(fecha: date) -> tuple[bool, str | None]:
    """True si hay algún período cerrado cuyo fecha_hasta >= fecha."""
    if fecha is None:
        return False, None
    row = db.fetch_one(
        """
        SELECT anio, mes, cerrado_por, fecha_cierre
        FROM scintela.periodos_contables
        WHERE estado = 'C' AND fecha_hasta >= %s
        ORDER BY fecha_hasta DESC LIMIT 1
        """,
        (fecha,),
    )
    if not row:
        return False, None
    return True, (
        f"El período {row['anio']}-{row['mes']:02d} está cerrado "
        f"(por {row.get('cerrado_por')})."
    )


def cerrar(id_periodo: int, *, motivo: str, usuario: str = "web") -> int:
    if not motivo:
        raise ValueError("Motivo requerido para cerrar el período.")
    return db.execute(
        """
        UPDATE scintela.periodos_contables
           SET estado='C', cerrado_por=%s, fecha_cierre=CURRENT_TIMESTAMP,
               motivo=%s
         WHERE id_periodo=%s AND estado='A'
        """,
        (usuario[:40], motivo[:200], id_periodo),
    )


def reabrir(id_periodo: int, *, usuario: str = "web") -> int:
    return db.execute(
        """
        UPDATE scintela.periodos_contables
           SET estado='A', cerrado_por=NULL, fecha_cierre=NULL, motivo=NULL
         WHERE id_periodo=%s
        """,
        (id_periodo,),
    )
