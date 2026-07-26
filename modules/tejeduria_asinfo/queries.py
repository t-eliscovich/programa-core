"""Tarifas $/kg de tejeduría tercerizada — CRUD sobre scintela.tejeduria_tarifa.

POR QUÉ (TMT 2026-07-26, pedido dueña): las compras tipo K de tejeduría faltan
en PC a propósito (los kg salen de Asinfo). Para que la carga sea automática el
sistema necesita saber cuánto cobra cada tejedor por kg. Esas tarifas viven en
una tabla editable desde /produccion-tejeduria-asinfo, no hardcodeadas.

`patron` = substring del PRODUCTO de la OF de Asinfo. Ejemplo real: Ponce cobra
1,0350 los artículos con "HUF" y 0,5750 el resto. `patron IS NULL` = tarifa por
defecto del proveedor. El patrón MÁS ESPECÍFICO (el más largo que matchea) gana.

Todo fail-soft: si la tabla todavía no existe (migración 0133 sin aplicar), los
lectores devuelven vacío y la pantalla sigue andando con la carga manual.
"""
from decimal import Decimal

import db


def listar_tarifas() -> list[dict]:
    """Todas las tarifas, default del proveedor primero y después los patrones."""
    try:
        rows = db.fetch_all(
            """
            SELECT id_tarifa, cod_prov, patron, tarifa, nota,
                   usuario_modifica, fecha_modifica
              FROM scintela.tejeduria_tarifa
             ORDER BY cod_prov,
                      CASE WHEN patron IS NULL THEN 0 ELSE 1 END,
                      patron
            """,
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft (migración sin aplicar)
        return []
    return [
        {
            "id_tarifa": int(r["id_tarifa"]),
            "cod_prov": (r["cod_prov"] or "").upper().strip(),
            "patron": (r["patron"] or "") or None,
            "tarifa": float(r["tarifa"] or 0),
            "nota": r.get("nota") or "",
            "usuario_modifica": r.get("usuario_modifica") or "",
            "fecha_modifica": r.get("fecha_modifica"),
        }
        for r in rows
    ]


def resolver(tarifas: list[dict], cod_prov: str, producto: str | None) -> float | None:
    """Tarifa $/kg que le corresponde a (proveedor, producto), o None.

    Función PURA — recibe la lista ya leída para no pegarle a la DB por fila.
    El patrón más específico gana: entre varios que matchean, el más largo. Si
    ninguno matchea, la fila `patron IS NULL` del proveedor (default). Si el
    proveedor no tiene ninguna fila → None (y la pantalla NO sugiere importe:
    nunca inventamos un precio).
    """
    cod = (cod_prov or "").upper().strip()
    if not cod:
        return None
    prod = (producto or "").upper()
    mejor: tuple[int, float] | None = None
    default: float | None = None
    for t in tarifas:
        if t["cod_prov"] != cod:
            continue
        pat = t["patron"]
        if not pat:
            default = t["tarifa"]
            continue
        if pat.upper() in prod:
            largo = len(pat)
            if mejor is None or largo > mejor[0]:
                mejor = (largo, t["tarifa"])
    if mejor is not None:
        return mejor[1]
    return default


def guardar_tarifas(filas: list[dict], usuario: str = "web") -> dict:
    """UPSERT de tarifas. `filas` = [{cod_prov, patron, tarifa, nota}].

    - `cod_prov` vacío → se ignora la fila (es la fila en blanco del formulario).
    - `tarifa` None → se ignora (no borra; para borrar está `borrar_tarifa`).
    - Clave de conflicto = (cod_prov, COALESCE(patron,'')), igual que el índice
      único de la migración 0133.
    """
    guardadas = 0
    with db.tx() as conn:
        for f in filas:
            cod = (f.get("cod_prov") or "").upper().strip()[:5]
            tarifa = f.get("tarifa")
            if not cod or tarifa is None:
                continue
            if float(tarifa) < 0:
                raise ValueError(f"La tarifa de {cod} no puede ser negativa.")
            patron = (f.get("patron") or "").strip()[:60] or None
            nota = (f.get("nota") or "").strip()[:120] or None
            db.execute(
                """
                INSERT INTO scintela.tejeduria_tarifa
                       (cod_prov, patron, tarifa, nota, usuario_modifica, fecha_modifica)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (cod_prov, COALESCE(patron, ''))
                DO UPDATE SET tarifa = EXCLUDED.tarifa,
                              nota   = EXCLUDED.nota,
                              usuario_modifica = EXCLUDED.usuario_modifica,
                              fecha_modifica   = now()
                """,
                (cod, patron, Decimal(str(tarifa)), nota, usuario),
                conn=conn,
            )
            guardadas += 1
    return {"guardadas": guardadas}


def borrar_tarifa(id_tarifa: int) -> int:
    """Borra una fila de tarifa. Devuelve 1 si borró, 0 si no existía."""
    with db.tx() as conn:
        db.execute(
            "DELETE FROM scintela.tejeduria_tarifa WHERE id_tarifa = %s",
            (int(id_tarifa),),
            conn=conn,
        )
    return 1
