"""Estado de recepción / deuda / pago de importaciones (scintela.importacion_pago).

Tabla PC-only. **Identidad = `im_numero`** (el número de importación de Asinfo,
ej. "IM-0000593", único). NO usamos (codigo_prov, ref_num) porque el número de
la Nota se reusa con los años (AC 40 de 2023 y de 2026 son importaciones
distintas) y colisionaban. Migración 0108. Sobrevive el Sync dBase (no está en
el TABLE_MAP). codigo_prov/ref_num se conservan como referencia.

Flujo (mig 0107):
  1. RECIBIR: costo_estimado (promedio por tipo de hilado) → deuda = costo −
     anticipo; kg al stock; recibido_pc=TRUE.
  2. PAGAR: el monto real sobrescribe la deuda (= monto_real − anticipo);
     pagada=TRUE. El stock vale el costo total (no se le resta el anticipo).
  Reversible: deshacer_recepcion / deshacer_pago.

Fail-soft: si la tabla/columnas no existen, lecturas → {} y escrituras → ValueError.
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


def estados_por_im(ims: set[str]) -> dict[str, dict]:
    """{im_numero: {recibido_pc, kg_recibidos, costo_estimado, anticipo_aplicado,
    deuda, pagada, monto_real, fecha_recepcion_pc, fecha_pago}} para las
    importaciones pedidas. Fail-soft: {} si no hay ims, faltan columnas o la DB falla.
    """
    ims = sorted({(i or "").strip() for i in ims if i})
    if not ims:
        return {}
    try:
        rows = db.fetch_all(
            """
            SELECT im_numero,
                   recibido_pc, fecha_recepcion_pc, kg_recibidos,
                   costo_estimado, anticipo_aplicado, deuda,
                   pagada, fecha_pago, monto_real
              FROM scintela.importacion_pago
             WHERE im_numero = ANY(%s)
            """,
            (ims,),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("estados_por_im falló: %s", e)
        return {}
    out: dict[str, dict] = {}

    def _f(v):
        return float(v) if v is not None else None

    for r in rows:
        im = (str(r.get("im_numero") or "")).strip()
        if not im:
            continue
        out[im] = {
            "recibido_pc": bool(r.get("recibido_pc")),
            "fecha_recepcion_pc": (str(r["fecha_recepcion_pc"]) if r.get("fecha_recepcion_pc") else None),
            "kg_recibidos": _f(r.get("kg_recibidos")),
            "costo_estimado": _f(r.get("costo_estimado")),
            "anticipo_aplicado": float(r.get("anticipo_aplicado") or 0),
            "deuda": _f(r.get("deuda")),
            "pagada": bool(r.get("pagada")),
            "fecha_pago": (str(r["fecha_pago"]) if r.get("fecha_pago") else None),
            "monto_real": _f(r.get("monto_real")),
        }
    return out


def _upsert(im_numero: str, prov: str, num, *, campos: list[str], valores: list, usuario: str) -> None:
    """UPSERT por im_numero. Crea la fila (con codigo_prov/ref_num de referencia)
    si no existe."""
    if not _tabla_existe():
        raise ValueError(
            "Falta correr la migración 0104/0107/0108 (scintela.importacion_pago). "
            "Corré /admin/migraciones."
        )
    im_numero = (im_numero or "").strip()
    if not im_numero:
        raise ValueError("Importación inválida (falta el número IM-).")
    prov = (prov or "").strip().upper()
    ref_num = int(num) if num is not None else 0
    existe = db.fetch_one(
        "SELECT id_importacion_pago FROM scintela.importacion_pago WHERE im_numero = %s",
        (im_numero,),
    )
    if existe:
        set_sql = ", ".join(campos + ["usuario_modifica = %s", "fecha_modifica = CURRENT_TIMESTAMP"])
        db.execute(
            f"UPDATE scintela.importacion_pago SET {set_sql} WHERE id_importacion_pago = %s",
            tuple(valores) + (usuario[:50], existe["id_importacion_pago"]),
        )
    else:
        cols = ["im_numero", "codigo_prov", "ref_num"] + [c.split("=")[0].strip() for c in campos] + ["usuario_crea"]
        ph = ", ".join(["%s"] * len(cols))
        db.execute(
            f"INSERT INTO scintela.importacion_pago ({', '.join(cols)}) VALUES ({ph})",
            (im_numero, prov, ref_num) + tuple(valores) + (usuario[:50],),
        )


def _deuda(costo, anticipo) -> float:
    """Deuda neta = costo total − anticipo aplicado (nunca negativa).
    El anticipo es el pago parcial; el costo total es el valor del stock."""
    return max(0.0, round(float(costo or 0) - float(anticipo or 0), 2))


def set_recepcion(im_numero: str, prov: str, num, *, kg, costo_estimado, anticipo=0, usuario: str = "web") -> None:
    """Recibe la importación con costo estimado → genera la deuda. Kg al stock."""
    kg_f = round(float(kg or 0), 2)
    costo = round(float(costo_estimado or 0), 2)
    if costo < 0:
        raise ValueError("El costo estimado no puede ser negativo.")
    if kg_f <= 0:
        raise ValueError("Los kilos recibidos deben ser mayores a 0.")
    _upsert(
        im_numero, prov, num,
        campos=[
            "recibido_pc = %s", "fecha_recepcion_pc = %s", "kg_recibidos = %s",
            "costo_estimado = %s", "anticipo_aplicado = %s", "deuda = %s",
        ],
        valores=[True, today_ec(), kg_f, costo, round(float(anticipo or 0), 2),
                 _deuda(costo, anticipo)],
        usuario=usuario,
    )


def set_pago(im_numero: str, prov: str, num, *, monto_real, anticipo=0, usuario: str = "web") -> None:
    """Paga la importación: el monto real total sobrescribe la deuda y la marca pagada."""
    monto = round(float(monto_real or 0), 2)
    if monto < 0:
        raise ValueError("El monto real no puede ser negativo.")
    _upsert(
        im_numero, prov, num,
        campos=[
            "pagada = %s", "fecha_pago = %s", "monto_real = %s",
            "anticipo_aplicado = %s", "deuda = %s",
        ],
        valores=[True, today_ec(), monto, round(float(anticipo or 0), 2),
                 _deuda(monto, anticipo)],
        usuario=usuario,
    )


def deshacer_recepcion(im_numero: str, prov: str = "", num=None, *, usuario: str = "web") -> None:
    """Revierte la recepción (y el pago si lo hubiera): vuelve a 'en tránsito'."""
    _upsert(
        im_numero, prov, num,
        campos=[
            "recibido_pc = %s", "fecha_recepcion_pc = %s", "kg_recibidos = %s",
            "costo_estimado = %s", "deuda = %s",
            "pagada = %s", "fecha_pago = %s", "monto_real = %s",
        ],
        valores=[False, None, None, None, None, False, None, None],
        usuario=usuario,
    )


def deshacer_pago(im_numero: str, prov: str = "", num=None, *, anticipo=0, usuario: str = "web") -> None:
    """Revierte solo el pago: vuelve a 'recibida, pendiente de pago'.
    La deuda se recalcula desde el costo_estimado guardado (menos anticipo)."""
    row = db.fetch_one(
        "SELECT costo_estimado FROM scintela.importacion_pago WHERE im_numero = %s",
        ((im_numero or "").strip(),),
    ) or {}
    costo = row.get("costo_estimado")
    _upsert(
        im_numero, prov, num,
        campos=["pagada = %s", "fecha_pago = %s", "monto_real = %s", "deuda = %s"],
        valores=[False, None, None, (_deuda(costo, anticipo) if costo is not None else None)],
        usuario=usuario,
    )
