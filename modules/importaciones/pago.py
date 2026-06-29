"""Estado de recepción / deuda / pago de importaciones (scintela.importacion_pago).

Tabla PC-only keyed por (codigo_prov, ref_num) — la identidad de negocio de la
importación (proveedor + número de la Nota Asinfo). NO usamos id_compra porque
el Sync dBase hace TRUNCATE+INSERT de scintela.compra y lo reinicia; esta tabla
no está en el TABLE_MAP del sync, así que sobrevive.

Flujo (migración 0107, TMT 2026-06-29):
  1. RECIBIR: llega el hilo sin pagar → se le asigna costo_estimado (promedio
     histórico, editable) y se genera deuda = costo_estimado - anticipo. Los kg
     entran al stock (recibido_pc=TRUE).
  2. PAGAR: pago real (ej. estimado 30k, real 32k) → se sobrescribe la deuda con
     el monto real (deuda = monto_real - anticipo) y se marca pagada=TRUE.
  Reversible con deshacer_recepcion / deshacer_pago.

Las columnas viejas (contabilizada / monto_pagado, migración 0104) quedan pero
ya no se usan.

Fail-soft: si las columnas/tabla no existen (migración sin correr), las lecturas
devuelven {} y las escrituras lanzan un ValueError claro.
"""
from __future__ import annotations

import logging

import db
from filters import today_ec

_LOG = logging.getLogger("programa_core.importaciones.pago")


def _tabla_existe() -> bool:
    try:
        return bool(db.fetch_one("SELECT to_regclass('scintela.importacion_pago') AS t").get("t"))
    except Exception:  # noqa: BLE001
        return False


def estados_por_refs(refs: set[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """{(PROV, num): {recibido_pc, kg_recibidos, costo_estimado, anticipo_aplicado,
    deuda, pagada, monto_real, fecha_recepcion_pc, fecha_pago,
    contabilizada, monto_pagado}} para las refs pedidas.

    Fail-soft: {} si no hay refs, la tabla/columnas no existen o la DB falla.
    """
    if not refs:
        return {}
    provs = sorted({(p or "").upper() for p, _ in refs})
    numeros = sorted({int(n) for _, n in refs})
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(codigo_prov) AS codigo_prov, ref_num,
                   recibido_pc, fecha_recepcion_pc, kg_recibidos,
                   costo_estimado, anticipo_aplicado, deuda,
                   pagada, fecha_pago, monto_real,
                   contabilizada, monto_pagado
              FROM scintela.importacion_pago
             WHERE UPPER(codigo_prov) = ANY(%s)
               AND ref_num = ANY(%s)
            """,
            (provs, numeros),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("estados_por_refs falló: %s", e)
        return {}
    out: dict[tuple[str, int], dict] = {}
    for r in rows:
        if r.get("codigo_prov") is None or r.get("ref_num") is None:
            continue
        try:
            key = (str(r["codigo_prov"]).strip().upper(), int(r["ref_num"]))
        except (TypeError, ValueError):
            continue
        def _f(v):
            return float(v) if v is not None else None
        out[key] = {
            "recibido_pc": bool(r.get("recibido_pc")),
            "fecha_recepcion_pc": (str(r["fecha_recepcion_pc"]) if r.get("fecha_recepcion_pc") else None),
            "kg_recibidos": _f(r.get("kg_recibidos")),
            "costo_estimado": _f(r.get("costo_estimado")),
            "anticipo_aplicado": float(r.get("anticipo_aplicado") or 0),
            "deuda": _f(r.get("deuda")),
            "pagada": bool(r.get("pagada")),
            "fecha_pago": (str(r["fecha_pago"]) if r.get("fecha_pago") else None),
            "monto_real": _f(r.get("monto_real")),
            # legado (0104) — ya no se usa en el flujo, se conserva por compat.
            "contabilizada": bool(r.get("contabilizada")),
            "monto_pagado": float(r.get("monto_pagado") or 0),
        }
    return out


def _upsert(prov: str, num: int, *, campos: list[str], valores: list, usuario: str) -> None:
    """UPSERT por (UPPER(prov), num). Crea la fila si no existe."""
    if not _tabla_existe():
        raise ValueError(
            "Falta correr la migración 0104/0107 (scintela.importacion_pago). "
            "Corré /admin/migraciones."
        )
    prov = (prov or "").strip().upper()
    if not prov or num is None:
        raise ValueError("Importación inválida (faltan proveedor o número).")
    num = int(num)
    existe = db.fetch_one(
        "SELECT id_importacion_pago FROM scintela.importacion_pago "
        "WHERE UPPER(codigo_prov) = %s AND ref_num = %s",
        (prov, num),
    )
    if existe:
        set_sql = ", ".join(campos + ["usuario_modifica = %s", "fecha_modifica = CURRENT_TIMESTAMP"])
        db.execute(
            f"UPDATE scintela.importacion_pago SET {set_sql} "
            "WHERE id_importacion_pago = %s",
            tuple(valores) + (usuario[:50], existe["id_importacion_pago"]),
        )
    else:
        cols = ["codigo_prov", "ref_num"] + [c.split("=")[0].strip() for c in campos] + ["usuario_crea"]
        ph = ", ".join(["%s"] * len(cols))
        db.execute(
            f"INSERT INTO scintela.importacion_pago ({', '.join(cols)}) VALUES ({ph})",
            (prov, num) + tuple(valores) + (usuario[:50],),
        )


def _deuda(costo, anticipo) -> float:
    """Deuda neta = costo bruto - anticipo aplicado (nunca negativa)."""
    return max(0.0, round(float(costo or 0) - float(anticipo or 0), 2))


def set_recepcion(prov: str, num: int, *, kg, costo_estimado, anticipo=0, usuario: str = "web") -> None:
    """Recibe la importación con costo estimado → genera la deuda. Kg entran al stock."""
    kg_f = round(float(kg or 0), 2)
    costo = round(float(costo_estimado or 0), 2)
    if costo < 0:
        raise ValueError("El costo estimado no puede ser negativo.")
    if kg_f <= 0:
        raise ValueError("Los kilos recibidos deben ser mayores a 0.")
    _upsert(
        prov, num,
        campos=[
            "recibido_pc = %s", "fecha_recepcion_pc = %s", "kg_recibidos = %s",
            "costo_estimado = %s", "anticipo_aplicado = %s", "deuda = %s",
        ],
        valores=[True, today_ec(), kg_f, costo, round(float(anticipo or 0), 2),
                 _deuda(costo, anticipo)],
        usuario=usuario,
    )


def set_pago(prov: str, num: int, *, monto_real, anticipo=0, usuario: str = "web") -> None:
    """Paga la importación: sobrescribe la deuda con el monto real y la marca pagada."""
    monto = round(float(monto_real or 0), 2)
    if monto < 0:
        raise ValueError("El monto real no puede ser negativo.")
    _upsert(
        prov, num,
        campos=[
            "pagada = %s", "fecha_pago = %s", "monto_real = %s",
            "anticipo_aplicado = %s", "deuda = %s",
        ],
        valores=[True, today_ec(), monto, round(float(anticipo or 0), 2),
                 _deuda(monto, anticipo)],
        usuario=usuario,
    )


def deshacer_recepcion(prov: str, num: int, *, usuario: str = "web") -> None:
    """Revierte la recepción (y el pago si lo hubiera): vuelve todo a 'en tránsito'."""
    _upsert(
        prov, num,
        campos=[
            "recibido_pc = %s", "fecha_recepcion_pc = %s", "kg_recibidos = %s",
            "costo_estimado = %s", "deuda = %s",
            "pagada = %s", "fecha_pago = %s", "monto_real = %s",
        ],
        valores=[False, None, None, None, None, False, None, None],
        usuario=usuario,
    )


def deshacer_pago(prov: str, num: int, *, anticipo=0, usuario: str = "web") -> None:
    """Revierte solo el pago: la importación vuelve a 'recibida, pendiente de pago'.

    La deuda se recalcula desde el costo_estimado guardado (menos anticipo).
    """
    row = db.fetch_one(
        "SELECT costo_estimado FROM scintela.importacion_pago "
        "WHERE UPPER(codigo_prov) = %s AND ref_num = %s",
        ((prov or "").strip().upper(), int(num)),
    ) or {}
    costo = row.get("costo_estimado")
    _upsert(
        prov, num,
        campos=["pagada = %s", "fecha_pago = %s", "monto_real = %s", "deuda = %s"],
        valores=[False, None, None, (_deuda(costo, anticipo) if costo is not None else None)],
        usuario=usuario,
    )
