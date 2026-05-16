"""Proformas (cotizaciones a clientes).

scintela.proforma_cabecera: id_proforma, id_cliente, fecha_emision, subtotal,
   porcentaje_descuento_volumen, monto_descuento_volumen, subtotal_con_descuento,
   aplica_descuento_contado, monto_descuento_contado, total_final, observaciones

scintela.proforma_detalle: id_detalle, id_proforma, id_subcategoria_producto,
   nombre_producto, color, cantidad_kilos, precio_unitario, precio_total
"""
import db


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 300,
) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    return db.fetch_all(
        """
        SELECT h.id_proforma, h.fecha_emision, h.id_cliente,
               COALESCE(c.codigo_cli, '') AS codigo_cli,
               COALESCE(c.nombre, '')     AS cliente,
               h.subtotal, h.monto_descuento_volumen, h.subtotal_con_descuento,
               h.aplica_descuento_contado, h.monto_descuento_contado, h.total_final,
               h.observaciones
        FROM scintela.proforma_cabecera h
        LEFT JOIN scintela.cliente c ON c.id_cliente = h.id_cliente
        WHERE (%(q)s IS NULL
               OR UPPER(COALESCE(c.nombre,'')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(c.codigo_cli,'')) LIKE UPPER(%(like)s)
               OR CAST(h.id_proforma AS TEXT) LIKE %(like)s)
          AND (%(desde)s::date IS NULL OR h.fecha_emision >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR h.fecha_emision <= %(hasta)s::date)
        ORDER BY h.fecha_emision DESC, h.id_proforma DESC
        LIMIT %(limite)s
        """,
        {
            "q": q or None, "like": like,
            "desde": desde or None, "hasta": hasta or None,
            "limite": limite,
        },
    )


def detalle(id_proforma: int) -> dict | None:
    cabecera = db.fetch_one(
        """
        SELECT h.*, c.codigo_cli, c.nombre AS cliente, c.ruc, c.telefono
        FROM scintela.proforma_cabecera h
        LEFT JOIN scintela.cliente c ON c.id_cliente = h.id_cliente
        WHERE h.id_proforma = %s
        """,
        (id_proforma,),
    )
    if not cabecera:
        return None
    items = db.fetch_all(
        """
        SELECT id_detalle, nombre_producto, color,
               cantidad_kilos, precio_unitario, precio_total
        FROM scintela.proforma_detalle
        WHERE id_proforma = %s
        ORDER BY id_detalle
        """,
        (id_proforma,),
    )
    return {"cabecera": cabecera, "items": items}
