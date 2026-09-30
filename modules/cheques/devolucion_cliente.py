"""Devolverle al cliente un cheque DEVUELTO: la deuda pasa a una nota de débito.

TMT 2026-09-30 (pedido de Andrés, decisión de Tamara). Cuando el banco
devuelve un cheque y después se le entrega físicamente al cliente, el cheque
deja de ser nuestro — ya no se puede re-depositar ni cobrar. En el dBase se
pasaba a X y se cargaba a mano un abono NEGATIVO en las facturas que pagaba.

Tamara no quiso reabrir facturas: *"lo que puede pasar es que la factura murió
hace mucho. mejor registremos un valor por cobrar. Debería quedar como una
nota de débito a la cuenta"* — y *"sin vencimiento"*.

Así que el paso hace tres cosas, en una sola transacción:

  1. el cheque pasa a 'X' con la marca "[DEV] devuelto al cliente";
  2. se crea en la cuenta del cliente una NOTA DE DÉBITO (`factura.tipo='ND'`,
     número `ND-<n° de cheque>`, sin vencimiento, sin kilos) por el importe
     del cheque — se cobra como cualquier factura;
  3. las facturas que pagaba el cheque NO se tocan: siguen pagas, y el
     vínculo cheque↔factura queda como historia de quién las pagó.

El banco no se toca: la plata ya salió con la nota de débito del protesto.
La cuenta del cliente tampoco cambia de total: deja de deber un cheque
protestado y pasa a deber una nota de débito por lo mismo.

La vuelta atrás (`deshacer`) anula la nota de débito y devuelve el cheque a
su estado; se niega si la nota ya tiene algún cobro.
"""
from __future__ import annotations

import json

import db
from filters import today_ec
from modules.facturas import tipo_doc
from periodo_guard import asegurar_fecha_abierta

TIPO_MOV = "cheque_devuelto_al_cliente"
MARCA = "[DEV] devuelto al cliente"

#: Desde dónde se puede devolver: sólo los devueltos (el banco ya lo rechazó).
STATS_DEVUELTOS = ("1", "2", "3")

# `numf` es integer: un N° de cheque largo o con letras no entra.
_NUMF_MAX = 2_147_483_647


def _numf_de(no_cheque) -> int:
    txt = str(no_cheque or "").strip()
    if txt.isdigit() and 0 < int(txt) <= _NUMF_MAX:
        return int(txt)
    return 0


def _numero_libre(codigo_cli: str, base: str, conn) -> str:
    """`ND-2161`, o `ND-2161-2` si el cliente ya tiene una con ese número."""
    usados = {
        (r.get("numf_completo") or "").strip().upper()
        for r in (db.fetch_all(
            "SELECT numf_completo FROM scintela.factura "
            " WHERE codigo_cli = %s AND numf_completo LIKE %s",
            (codigo_cli, base + "%"), conn=conn) or [])
    }
    if base.upper() not in usados:
        return base
    n = 2
    while f"{base}-{n}".upper() in usados:
        n += 1
    return f"{base}-{n}"


def vista_previa(id_cheque: int) -> dict:
    """Lo que va a pasar, para la pantalla de confirmación. No escribe nada."""
    ch = db.fetch_one(
        "SELECT id_cheque, no_cheque, stat, codigo_cli, importe, no_banco "
        "  FROM scintela.cheque WHERE id_cheque = %s",
        (id_cheque,),
    )
    if not ch:
        raise ValueError("Ese cheque no existe.")
    aplic = db.fetch_all(
        """
        SELECT x.id_fact, x.importe, f.numf, f.numf_completo, f.stat
          FROM scintela.chequesxfact x
          JOIN scintela.factura f ON f.id_factura = x.id_fact
         WHERE x.id_cheque = %s
         ORDER BY f.fecha, f.id_factura
        """,
        (id_cheque,),
    ) or []
    no = (ch.get("no_cheque") or "").strip() or str(ch["id_cheque"])
    return {
        "cheque": ch,
        "numero_nd": f"ND-{no}",
        "aplicaciones": aplic,
        "error": _por_que_no(ch),
    }


def _por_que_no(ch: dict) -> str | None:
    stat = (ch.get("stat") or "").strip().upper()
    if stat not in STATS_DEVUELTOS:
        return ("Sólo se le devuelve al cliente un cheque DEVUELTO por el banco "
                f"(estado 1, 2 o 3). Este está en '{stat or '—'}'.")
    if float(ch.get("importe") or 0) <= 0:
        return "El cheque no tiene importe: no hay nada que pasar a la cuenta."
    if int(ch.get("no_banco") or 0) in (97, 98):
        return "Es un anticipo o saldo a favor, no un cheque del cliente."
    if not (ch.get("codigo_cli") or "").strip():
        return "El cheque no tiene cliente."
    return None


def devolver(id_cheque: int, *, usuario: str = "web", motivo: str = "") -> dict:
    """Pasa el cheque devuelto a X y crea la nota de débito. Todo o nada."""
    import mov_doble as _md

    fecha = today_ec()
    asegurar_fecha_abierta(fecha)
    motivo = (motivo or "").strip()

    with db.tx() as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat, codigo_cli, importe, no_banco, "
            "       fechaout "
            "  FROM scintela.cheque WHERE id_cheque = %s FOR UPDATE",
            (id_cheque,), conn=conn,
        )
        if not ch:
            raise ValueError("Ese cheque no existe.")
        error = _por_que_no(ch)
        if error:
            raise ValueError(error)

        cli = ch["codigo_cli"].strip().upper()
        importe = round(float(ch["importe"]), 2)
        no = (ch.get("no_cheque") or "").strip() or str(id_cheque)
        numero = _numero_libre(cli, f"ND-{no}", conn)
        stat_prev = (ch.get("stat") or "").strip().upper()

        # 1. La nota de débito: una "factura" sin kilos y sin vencimiento.
        nd = db.execute_returning(
            """
            INSERT INTO scintela.factura
                (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat,
                 tipo, vencimiento, numf_completo, usuario_crea)
            VALUES (%s, %s, %s, 0, %s, 0, %s, 'Z', %s, NULL, %s, %s)
            RETURNING id_factura
            """,
            (_numf_de(no), fecha, cli, importe, importe,
             tipo_doc.NOTA_DEBITO, numero, usuario),
            conn=conn,
        )
        id_nd = int(nd["id_factura"])

        # 2. El cheque deja de ser nuestro.
        db.execute(
            "UPDATE scintela.cheque "
            "   SET stat='X', fechaout=%s, "
            "       observacion = RIGHT("
            "           COALESCE(observacion || ' | ', '') || %s, 200), "
            "       usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            " WHERE id_cheque=%s",
            (fecha, f"{MARCA} → {numero}" + (f": {motivo[:40]}" if motivo else ""),
             usuario, id_cheque),
            conn=conn,
        )

        # 3. La huella, con lo necesario para deshacerlo exacto.
        id_mov = _md.registrar(
            conn=conn,
            tipo=TIPO_MOV,
            origen_table="cheque",
            origen_id=id_cheque,
            destino_table="factura",
            destino_id=id_nd,
            importe=importe,
            fecha=fecha,
            concepto=(f"Cheque {no} devuelto al cliente {cli} → {numero}"
                      + (f" — {motivo}" if motivo else ""))[:200],
            usuario=usuario,
            metadata={
                "codigo_cli": cli,
                "id_cheque": id_cheque,
                "no_cheque": no,
                "stat_previo": stat_prev,
                "fechaout_previo": (ch["fechaout"].isoformat()
                                    if ch.get("fechaout") else None),
                "id_factura_nd": id_nd,
                "numero_nd": numero,
                "motivo": motivo or None,
            },
        )
    return {"id_cheque": id_cheque, "no_cheque": no, "codigo_cli": cli,
            "importe": importe, "id_factura_nd": id_nd, "numero_nd": numero,
            "id_mov_doble": id_mov}


def _meta(mv: dict) -> dict:
    meta = mv.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    return meta


def deshacer(id_mov_doble: int, *, usuario: str = "web") -> dict:
    """Anula la nota de débito y devuelve el cheque al estado que tenía."""
    import mov_doble as _md

    mv = db.fetch_one(
        "SELECT id_mov_doble, tipo, origen_id, destino_id, estado, metadata, "
        "       importe "
        "  FROM scintela.mov_doble WHERE id_mov_doble = %s",
        (id_mov_doble,),
    )
    if not mv or (mv.get("tipo") or "") != TIPO_MOV:
        raise ValueError("Ese movimiento no es una devolución de cheque al cliente.")
    if (mv.get("estado") or "") == "reversado":
        raise ValueError("Esa devolución ya se deshizo.")
    meta = _meta(mv)
    stat_prev = (meta.get("stat_previo") or "").strip().upper()
    if stat_prev not in STATS_DEVUELTOS:
        raise ValueError("No sé a qué estado volvía el cheque: revisalo a mano.")

    fecha = today_ec()
    asegurar_fecha_abierta(fecha)

    with db.tx() as conn:
        ch = db.fetch_one(
            "SELECT id_cheque, no_cheque, stat FROM scintela.cheque "
            " WHERE id_cheque = %s FOR UPDATE",
            (int(mv["origen_id"]),), conn=conn,
        )
        nd = db.fetch_one(
            "SELECT id_factura, numf_completo, stat, abono, "
            "       COALESCE(retencion, 0) AS retencion "
            "  FROM scintela.factura WHERE id_factura = %s FOR UPDATE",
            (int(mv["destino_id"]),), conn=conn,
        )
        if not ch or not nd:
            raise ValueError("El cheque o la nota de débito ya no existen.")
        if (ch.get("stat") or "").strip().upper() != "X":
            raise ValueError(
                f"El cheque está en '{ch.get('stat')}', no en X: alguien lo "
                "movió después. Revisalo antes de deshacer.")
        cobros = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.chequesxfact WHERE id_fact = %s",
            (nd["id_factura"],), conn=conn,
        ) or {}
        if (float(nd.get("abono") or 0) != 0 or float(nd.get("retencion") or 0) != 0
                or int(cobros.get("n") or 0)):
            raise ValueError(
                f"La {nd['numf_completo']} ya tiene cobros aplicados. Sacalos "
                "primero; si no, esa plata quedaría pagando una nota anulada.")

        db.execute(
            "UPDATE scintela.factura SET stat='X', usuario_modifica=%s, "
            "       fecha_modifica=CURRENT_TIMESTAMP WHERE id_factura=%s",
            (usuario, nd["id_factura"]), conn=conn,
        )
        fechaout = meta.get("fechaout_previo")
        db.execute(
            "UPDATE scintela.cheque "
            "   SET stat=%s, fechaout=%s, "
            "       observacion = RIGHT("
            "           COALESCE(observacion || ' | ', '') || %s, 200), "
            "       usuario_modifica=%s, fecha_modifica=CURRENT_TIMESTAMP "
            " WHERE id_cheque=%s",
            (stat_prev, fechaout, "[R] devolución al cliente deshecha",
             usuario, ch["id_cheque"]),
            conn=conn,
        )
        _md.registrar(
            conn=conn,
            tipo="reverso_" + TIPO_MOV,
            origen_table="cheque",
            origen_id=ch["id_cheque"],
            destino_table="factura",
            destino_id=nd["id_factura"],
            importe=float(mv.get("importe") or 0),
            fecha=fecha,
            concepto=(f"Deshecha la devolución del cheque {ch.get('no_cheque')}"
                      f" — {nd['numf_completo']} anulada")[:200],
            usuario=usuario,
            metadata={"id_mov_original": id_mov_doble, "stat_restaurado": stat_prev},
            id_original=id_mov_doble,
        )
    return {"id_cheque": ch["id_cheque"], "no_cheque": ch.get("no_cheque"),
            "stat_restaurado": stat_prev, "numero_nd": nd["numf_completo"]}
