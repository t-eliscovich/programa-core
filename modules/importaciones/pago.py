"""Estado de pago / contabilización de importaciones (scintela.importacion_pago).

Tabla PC-only (migración 0104) keyed por (codigo_prov, ref_num) — la identidad
de negocio de la importación (proveedor + número de la Nota Asinfo). NO usamos
id_compra porque el Sync dBase hace TRUNCATE+INSERT de scintela.compra y lo
reinicia; esta tabla no está en el TABLE_MAP del sync, así que sobrevive.

Semántica (ver migración 0104):
  - contabilizada=FALSE → "pendiente": kilos se pueden mostrar/restar aparte.
  - contabilizada=TRUE  → la dueña marcó "ok, está el total" → libera todo.
  - monto_pagado        → informativo (permite parcial); no libera kilos solo.

Fail-soft: si la tabla aún no existe (migración 0104 sin correr), las lecturas
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
    """{(PROV, num): {contabilizada, monto_pagado}} para las refs pedidas.

    Fail-soft: {} si no hay refs, la tabla no existe o la DB falla.
    """
    if not refs:
        return {}
    provs = sorted({(p or "").upper() for p, _ in refs})
    numeros = sorted({int(n) for _, n in refs})
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(codigo_prov) AS codigo_prov, ref_num,
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
        out[key] = {
            "contabilizada": bool(r.get("contabilizada")),
            "monto_pagado": float(r.get("monto_pagado") or 0),
        }
    return out


def _upsert(prov: str, num: int, *, campos: list[str], valores: list, usuario: str) -> None:
    """UPSERT por (UPPER(prov), num). Crea la fila si no existe."""
    if not _tabla_existe():
        raise ValueError(
            "Falta correr la migración 0104 (scintela.importacion_pago). "
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


def set_contabilizada(prov: str, num: int, contabilizada: bool, *, usuario: str = "web") -> None:
    """Marca/desmarca una importación como contabilizada (libera/retiene kilos)."""
    fecha = today_ec() if contabilizada else None
    _upsert(
        prov, num,
        campos=["contabilizada = %s", "fecha_contabilizada = %s"],
        valores=[bool(contabilizada), fecha],
        usuario=usuario,
    )


def set_monto_pagado(prov: str, num: int, monto, *, usuario: str = "web") -> None:
    """Guarda el monto pagado (informativo, permite parcial)."""
    monto = round(float(monto or 0), 2)
    if monto < 0:
        raise ValueError("El monto pagado no puede ser negativo.")
    _upsert(
        prov, num,
        campos=["monto_pagado = %s"],
        valores=[monto],
        usuario=usuario,
    )
