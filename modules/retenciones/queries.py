"""Consultas de retenciones en la fuente emitidas por clientes.

scintela.retencion: id_retencion, codigo_cli, rete, numf, fecha
Se liga a scintela.factura por (codigo_cli, numf).
"""
from datetime import date

import db
from periodo_guard import asegurar_fecha_abierta


def emitir(
    *,
    codigo_cli: str,
    numf: int,
    rete,
    fecha: date | None = None,
    usuario: str = "web",
) -> dict:
    """Registrar retención en la fuente emitida por el cliente contra una factura.

    Reglas:
      - La factura (codigo_cli, numf) tiene que existir.
      - No debe existir ya una retención para ese (codigo_cli, numf).
      - `rete` no puede superar `factura.importe` (chequeo defensivo).
    """
    if not codigo_cli:
        raise ValueError("codigo_cli requerido.")
    if not numf or numf <= 0:
        raise ValueError("numf de factura requerido.")
    rete_f = float(rete or 0)
    if rete_f <= 0:
        raise ValueError("Valor retenido debe ser mayor que cero.")
    asegurar_fecha_abierta(fecha)

    f = db.fetch_one(
        "SELECT id_factura, importe FROM scintela.factura "
        "WHERE codigo_cli=%s AND numf=%s",
        (codigo_cli, numf),
    )
    if not f:
        raise ValueError(f"Factura {numf} del cliente {codigo_cli!r} no existe.")
    if rete_f > float(f["importe"] or 0) + 0.01:
        raise ValueError(
            f"Retención ({rete_f:.2f}) no puede superar el importe de la factura ({float(f['importe']):.2f})."
        )

    ya = db.fetch_one(
        "SELECT 1 AS x FROM scintela.retencion WHERE codigo_cli=%s AND numf=%s",
        (codigo_cli, numf),
    )
    if ya:
        raise ValueError(
            f"Ya existe una retención para factura {numf} del cliente {codigo_cli}."
        )

    row = db.execute_returning(
        """
        INSERT INTO scintela.retencion (codigo_cli, numf, rete, fecha, usuario_crea)
        VALUES (%s, %s, %s, COALESCE(%s, CURRENT_DATE), %s)
        RETURNING id_retencion
        """,
        (codigo_cli, numf, rete_f, fecha, usuario),
    ) or {}
    return row


def por_id(id_retencion: int) -> dict | None:
    """Lectura puntual — usada por la vista de confirmación."""
    return db.fetch_one(
        """
        SELECT r.id_retencion, r.codigo_cli, r.numf, r.rete, r.fecha,
               COALESCE(c.nombre, '') AS cliente,
               f.importe AS importe_factura,
               f.numf_completo
        FROM scintela.retencion r
        LEFT JOIN scintela.cliente c ON c.codigo_cli = r.codigo_cli
        LEFT JOIN scintela.factura f ON f.codigo_cli = r.codigo_cli AND f.numf = r.numf
        WHERE r.id_retencion = %s
        """,
        (id_retencion,),
    )


def anular(id_retencion: int, usuario: str = "web") -> int:
    """Borrar una retención (sólo con permiso retenciones.anular)."""
    return db.execute(
        "DELETE FROM scintela.retencion WHERE id_retencion = %s",
        (id_retencion,),
    )


def facturas_sin_retencion(codigo_cli: str, limite: int = 100) -> list[dict]:
    """Facturas del cliente que todavía no tienen retención — selector del form."""
    return db.fetch_all(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.importe, f.saldo
        FROM scintela.factura f
        LEFT JOIN scintela.retencion r
               ON r.codigo_cli = f.codigo_cli AND r.numf = f.numf
        WHERE f.codigo_cli = %s
          AND r.id_retencion IS NULL
        ORDER BY f.fecha DESC, f.numf DESC
        LIMIT %s
        """,
        (codigo_cli, limite),
    )


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    return db.fetch_all(
        """
        SELECT r.id_retencion, r.fecha, r.codigo_cli, r.numf, r.rete,
               COALESCE(c.nombre, '')         AS cliente,
               f.id_factura, f.numf_completo, f.importe AS importe_fact,
               CASE WHEN COALESCE(f.importe, 0) > 0
                    THEN ROUND((r.rete / f.importe * 100)::numeric, 2)
                    ELSE NULL END AS pct
        FROM scintela.retencion r
        LEFT JOIN scintela.cliente c ON c.codigo_cli = r.codigo_cli
        LEFT JOIN scintela.factura f ON f.codigo_cli = r.codigo_cli AND f.numf = r.numf
        WHERE (%(q)s IS NULL
               OR UPPER(r.codigo_cli) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(c.nombre,'')) LIKE UPPER(%(like)s)
               OR CAST(r.numf AS TEXT) LIKE %(like)s
               OR UPPER(COALESCE(f.numf_completo,'')) LIKE UPPER(%(like)s))
          AND (%(desde)s::date IS NULL OR r.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR r.fecha <= %(hasta)s::date)
        ORDER BY r.fecha DESC, r.id_retencion DESC
        LIMIT %(limite)s
        """,
        {
            "q": q or None, "like": like,
            "desde": desde or None, "hasta": hasta or None,
            "limite": limite,
        },
    )


def total_por_mes(anio: int | None = None) -> list[dict]:
    return db.fetch_all(
        """
        SELECT date_trunc('month', fecha)::date AS mes,
               COALESCE(SUM(rete), 0) AS total_retenido,
               COUNT(*)  AS n
        FROM scintela.retencion
        WHERE fecha IS NOT NULL
          AND (%s::int IS NULL OR EXTRACT(YEAR FROM fecha)::int = %s::int)
        GROUP BY 1
        ORDER BY 1 DESC
        """,
        (anio, anio),
    )
