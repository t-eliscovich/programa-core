"""Ajuste al CIERRE ANTERIOR (PATANT) — plata de otro ejercicio.

TMT 2026-09-03 (dueña): pasó a compra un anticipo de MH del **2024**
($ 21.253) y la utilidad de septiembre bajó eso. *"Ya sé que no se ató a
hilo, pero hagamos una excepción porque era muy viejo: quiero que entre a
plata que ya está en el hilo."*

Por qué bajó: el balance descuenta los anticipos cuya mercadería YA está en
bodega (los kilos ya cuentan en el stock). Ese descuento sale del cruce con
Asinfo por cuenta + número de importación, y un anticipo del 2024 con
concepto "ANTICIPO" nunca cruzó: fue un activo fantasma dos años. Al pasarlo
a compra el fantasma desapareció de golpe — pero esa plata se gastó en 2024,
no este mes.

Qué hace: baja `historia.patrimonio` del último cierre (el PATANT) en el
importe. `utilidad = patr − patant`, así que la utilidad del mes vuelve a
donde estaba y el año no lo cuenta en ningún mes: queda como plata de un
ejercicio ya cerrado. Todo el resto (traza, explicación del día, balance)
lee el mismo PATANT, así que el Δ aparece con nombre en la columna
"Cierre anterior" y no como un `#ajuste` sin dueño. Queda una `mov_doble`
`ajuste_cierre_anterior` con antes/después para el historial.

Signo: importe POSITIVO = "esto no es gasto de este mes" (PATANT baja, la
utilidad del mes sube). Negativo hace lo inverso.
"""
from __future__ import annotations

import db
from filters import today_ec


def cierre_anterior() -> dict | None:
    from modules.informes import queries as _q
    return _q.historia_ultimo_mes()


def aplicar(*, importe: float, motivo: str, usuario: str = "web",
            id_mov_doble_origen: int | None = None) -> dict:
    """Aplica el ajuste. Devuelve {id_historia, fecha, antes, despues, importe}."""
    imp = float(importe or 0)
    if imp == 0:
        raise ValueError("El importe no puede ser cero.")
    motivo = (motivo or "").strip()
    if not motivo:
        raise ValueError("Escribí el motivo: es lo que va a leer quien mire el cierre.")
    with db.tx() as conn:
        hist = cierre_anterior()
        if not hist:
            raise ValueError("No hay un cierre anterior en scintela.historia.")
        row = db.fetch_one(
            "SELECT id_historia, fecha, patrimonio FROM scintela.historia "
            " WHERE id_historia = %s FOR UPDATE",
            (int(hist["id_historia"]),), conn=conn,
        )
        antes = float(row.get("patrimonio") or 0)
        despues = antes - imp
        db.execute(
            "UPDATE scintela.historia SET patrimonio = %s WHERE id_historia = %s",
            (despues, int(row["id_historia"])), conn=conn,
        )
        import mov_doble as _md
        _md.registrar(
            conn=conn,
            tipo="ajuste_cierre_anterior",
            origen_table="mov_doble" if id_mov_doble_origen else "historia",
            origen_id=int(id_mov_doble_origen or row["id_historia"]),
            destino_table="historia",
            destino_id=int(row["id_historia"]),
            importe=imp,
            fecha=today_ec(),
            concepto=f"Ajuste al cierre anterior · {motivo}"[:200],
            usuario=usuario,
            metadata={
                "id_historia": int(row["id_historia"]),
                "fecha_cierre": str(row.get("fecha")),
                "patrimonio_antes": antes,
                "patrimonio_despues": despues,
                "motivo": motivo,
                "id_mov_doble_origen": id_mov_doble_origen,
            },
        )
    return {"id_historia": int(row["id_historia"]), "fecha": row.get("fecha"),
            "antes": antes, "despues": despues, "importe": imp}


def listar(limite: int = 50) -> list[dict]:
    return db.fetch_all(
        """
        SELECT id_mov_doble, fecha_operacion, importe, concepto, usuario, metadata
          FROM scintela.mov_doble
         WHERE tipo = 'ajuste_cierre_anterior'
         ORDER BY id_mov_doble DESC
         LIMIT %s
        """,
        (int(limite),),
    ) or []
