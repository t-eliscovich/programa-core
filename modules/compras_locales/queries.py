"""Tarifas $/kg de las compras LOCALES de hilo — CRUD sobre scintela.hilo_local_tarifa.

POR QUÉ (TMT 2026-07-30, pedido dueña): las compras locales de hilo (HY
Hiltexpoy, EP El Peral) llegan a Asinfo con kg y producto, pero **la plata de
Asinfo no se usa** (*"asinfo nunca nos importa en plata"*). El importe sale de
un tarifario editable, igual que en tejeduría, con los $/kg promediados del
COMPRAS.DBF del FoxPro.

`patron` = substring del CÓDIGO DE PRODUCTO de la recepción de Asinfo
(ej. '22/1-65:35CAR-10%-HY'). `patron IS NULL` = tarifa por defecto del
proveedor. El patrón MÁS ESPECÍFICO (el más largo que matchea) gana.

⚠ La tarifa se guarda **SIN IVA**. El motor multiplica por 1,15 al crear la
compra, porque el dBase graba COMPRAS.IMPORTE con IVA (ver migración 0143).

Todo fail-soft: si la tabla todavía no existe (migración 0143 sin aplicar), los
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
              FROM scintela.hilo_local_tarifa
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
    """Tarifa $/kg (SIN IVA) de (proveedor, producto), o None.

    Función PURA — recibe la lista ya leída para no pegarle a la DB por fila.
    El patrón más específico gana: entre varios que matchean, el más largo. Si
    ninguno matchea, la fila `patron IS NULL` del proveedor (default). Si el
    proveedor no tiene ninguna fila → None, y la carga automática SALTEA esa
    factura: nunca inventamos un precio.
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
      único de la migración 0143.
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
                INSERT INTO scintela.hilo_local_tarifa
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
            "DELETE FROM scintela.hilo_local_tarifa WHERE id_tarifa = %s",
            (int(id_tarifa),),
            conn=conn,
        )
    return 1


def proveedores_por_ruc() -> dict[str, str]:
    """{RUC → codigo_prov} de scintela.proveedor.

    El mapeo Asinfo↔PC de los proveedores LOCALES sale del RUC, que las dos
    puntas ya tienen (Asinfo lo guarda en `empresa.codigo`): HY = Hiltexpoy =
    1791436210001, EP = El Peral = 1890153654001. **Sin tabla de alias tipeada
    a mano** — misma lección que los aliases de cliente (30/07): si hay un dato
    real que identifica, se usa ese.

    Los proveedores de importación (AC, AI, MH, KX…) tienen el RUC vacío y en
    Asinfo el código es la sigla, así que no entran acá y no molestan.
    """
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(TRIM(codigo_prov)) AS cod, TRIM(COALESCE(ruc, '')) AS ruc
              FROM scintela.proveedor
             WHERE COALESCE(TRIM(ruc), '') <> ''
            """,
        ) or []
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    out: dict[str, str] = {}
    for r in rows:
        ruc = (r.get("ruc") or "").strip()
        cod = (r.get("cod") or "").strip()
        if ruc and cod:
            out[ruc] = cod
    return out
