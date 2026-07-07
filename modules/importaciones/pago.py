"""Recepción y ANTICIPOS de importaciones — estado PC-only por `im_numero`.

Las importaciones viven en Asinfo; el estado del flujo (recibida / anticipos)
es del programa y se guarda en `scintela.importacion_pago`, keyed por
`im_numero` (el nº IM- es único; la Nota se reusa con los años — AC 40 de 2023
y de 2026 son importaciones distintas — y colisionaban). Migración 0108.
Sobrevive el Sync dBase (no está en el TABLE_MAP). codigo_prov/ref_num se
conservan como referencia.

Modelo v2 (migs 0107 + 0113 — TMT 2026-07-06 dueña, SIMPLIFICADO):
  "Ya no hace falta la división de pagado o no. Dejamos de PREDECIR cuánto
  saldría: los anticipos son casi el 90% y lo RESTANTE se carga en /compras."

  1. RECIBIR: los kg entran al stock (recibido_pc=TRUE, kg_recibidos).
  2. ANTICIPOS: una importación puede tener MUCHOS, cargados en cualquier
     momento (antes un 2º anticipo hacía UPDATE sobre anticipo_aplicado y
     PISABA el 1º — le pasó a la dueña el 06/07). Cada carga es un MOVIMIENTO
     nuevo en `scintela.importacion_pago_mov` y genera AUTOMÁTICAMENTE su
     NOTA DE DÉBITO en Pichincha (la dueña la hacía a mano).
  3. VALOR DEL STOCK de la importación = Σ anticipos pagados (decisión
     explícita de la dueña). `costo_estimado` queda en la tabla SOLO como
     referencia histórica — nada lo usa para valuar.
  4. El RESTANTE (lo que falta sobre los anticipos) se carga por /compras
     como compra normal al proveedor → entra a posdat, que ES el pasivo
     real. Este módulo no predice ni arrastra esa deuda.

  Se eliminó el flujo "Pagar" (set_pago / monto_real como cierre) y la marca
  `pagada` como concepto: las columnas pagada/monto_real/deuda quedan en la
  tabla congeladas como referencia histórica del modelo v1, pero NADA las
  lee. `anticipo_aplicado` pasa a ser CACHE derivado (Σ movimientos) y
  `deuda` se deja en NULL en cada recompute.

  Reversible: cada movimiento tiene ✕ (deshacer_movimiento — borra el
  movimiento Y compensa su ND con una NC, par atómico con mov_doble);
  deshacer_recepcion vuelve la importación a "en tránsito" sin tocar los
  anticipos (son plata real).

Fail-soft: si la tabla/columnas no existen, lecturas → {} y escrituras →
ValueError. Si la mig 0113 no corrió, agregar_movimiento pide la migración.
"""
from __future__ import annotations

import logging

import db
from filters import today_ec

_LOG = logging.getLogger("programa_core.importaciones.pago")

# TMT 2026-07-06 (dueña): la ND automática de anticipos de importación sale
# SIEMPRE de Pichincha ("cuando creo un anticipo hago una nota de débito
# desde Pichincha"). Convención "solo Pichincha" (modules/bancos/views.py).
_BANCO_PICHINCHA = 10


def _tabla_existe() -> bool:
    try:
        return bool(db.fetch_one("SELECT to_regclass('scintela.importacion_pago') AS t").get("t"))
    except Exception:  # noqa: BLE001
        return False


def _tabla_mov_existe() -> bool:
    """¿Corrió la mig 0113 (scintela.importacion_pago_mov)?"""
    try:
        return bool(db.fetch_one("SELECT to_regclass('scintela.importacion_pago_mov') AS t").get("t"))
    except Exception:  # noqa: BLE001
        return False


def estados_por_im(ims: set[str]) -> dict[str, dict]:
    """{im_numero: {recibido_pc, kg_recibidos, fecha_recepcion_pc,
    anticipo_aplicado, costo_estimado}} para las importaciones pedidas.
    anticipo_aplicado es el CACHE derivado (Σ movimientos, mig 0113) = valor
    del stock de la importación. costo_estimado es solo referencia histórica.
    Fail-soft: {} si no hay ims, faltan columnas o la DB falla.
    """
    ims = sorted({(i or "").strip() for i in ims if i})
    if not ims:
        return {}
    try:
        rows = db.fetch_all(
            """
            SELECT im_numero,
                   recibido_pc, fecha_recepcion_pc, kg_recibidos,
                   costo_estimado, anticipo_aplicado
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
        }
    return out


def movimientos_por_im(ims: set[str]) -> dict[str, list[dict]]:
    """{im_numero: [movimientos]} — anticipos (mig 0113), orden cronológico.
    Cada movimiento: {id_mov, tipo, fecha, monto, nota, id_transaccion}.
    Fail-soft: {} si la tabla no existe o la DB falla.
    """
    ims = sorted({(i or "").strip() for i in ims if i})
    if not ims:
        return {}
    try:
        rows = db.fetch_all(
            """
            SELECT id_mov, im_numero, tipo,
                   TO_CHAR(fecha, 'YYYY-MM-DD') AS fecha,
                   monto, nota, id_transaccion
              FROM scintela.importacion_pago_mov
             WHERE im_numero = ANY(%s)
             ORDER BY fecha, id_mov
            """,
            (ims,),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("movimientos_por_im falló: %s", e)
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        im = (str(r.get("im_numero") or "")).strip()
        if not im:
            continue
        out.setdefault(im, []).append({
            "id_mov": r.get("id_mov"),
            "tipo": (r.get("tipo") or "").strip(),
            "fecha": r.get("fecha"),
            "monto": float(r.get("monto") or 0),
            "nota": (r.get("nota") or "").strip(),
            "id_transaccion": r.get("id_transaccion"),
        })
    return out


def _upsert(im_numero: str, prov: str, num, *, campos: list[str], valores: list,
            usuario: str, conn=None) -> None:
    """UPSERT por im_numero. Crea la fila (con codigo_prov/ref_num de referencia)
    si no existe. Acepta conn= para participar de una tx del caller."""
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
        (im_numero,), conn=conn,
    )
    if existe:
        set_sql = ", ".join(campos + ["usuario_modifica = %s", "fecha_modifica = CURRENT_TIMESTAMP"])
        db.execute(
            f"UPDATE scintela.importacion_pago SET {set_sql} WHERE id_importacion_pago = %s",
            tuple(valores) + (usuario[:50], existe["id_importacion_pago"]),
            conn=conn,
        )
    else:
        cols = ["im_numero", "codigo_prov", "ref_num"] + [c.split("=")[0].strip() for c in campos] + ["usuario_crea"]
        ph = ", ".join(["%s"] * len(cols))
        db.execute(
            f"INSERT INTO scintela.importacion_pago ({', '.join(cols)}) VALUES ({ph})",
            (im_numero, prov, ref_num) + tuple(valores) + (usuario[:50],),
            conn=conn,
        )


def _suma_movs(movimientos: list[dict]) -> float:
    """Σ montos de TODOS los movimientos = valor del stock de la importación
    (TMT 2026-07-06 dueña: "el stock vale lo que se pagó de anticipos").

    Suma anticipos Y eventuales 'pago' legacy (el CHECK de la tabla admite
    ambos por si acaso; la UI v2 solo carga anticipos). Pura (sin DB) —
    replica la semántica del backfill de la mig 0113: el valor efectivo de
    cada importación queda IGUAL al de antes del deploy.
    """
    return round(sum(float(m.get("monto") or 0) for m in movimientos), 2)


def _recompute_cache(conn, im_numero: str, usuario: str) -> dict | None:
    """Recalcula el CACHE `anticipo_aplicado` (= Σ movimientos = valor del
    stock) de la fila de importacion_pago. `deuda` se deja en NULL: dejó de
    existir como concepto (el pasivo real vive en posdat vía /compras).
    pagada/monto_real no se tocan (referencia histórica congelada del v1).
    Corre DENTRO de la tx del caller. Devuelve el derivado, o None sin fila."""
    im_numero = (im_numero or "").strip()
    row = db.fetch_one(
        """
        SELECT id_importacion_pago
          FROM scintela.importacion_pago
         WHERE im_numero = %s
        """,
        (im_numero,), conn=conn,
    )
    if not row:
        return None
    movs = db.fetch_all(
        "SELECT tipo, monto FROM scintela.importacion_pago_mov WHERE im_numero = %s",
        (im_numero,), conn=conn,
    )
    total = _suma_movs(movs)
    db.execute(
        """
        UPDATE scintela.importacion_pago
           SET anticipo_aplicado = %s, deuda = NULL,
               usuario_modifica = %s, fecha_modifica = CURRENT_TIMESTAMP
         WHERE id_importacion_pago = %s
        """,
        (total, usuario[:50], row["id_importacion_pago"]),
        conn=conn,
    )
    return {"anticipo_aplicado": total}


def _asegurar_fila(conn, im_numero: str, prov: str, usuario: str) -> None:
    """Garantiza la fila base en importacion_pago (los anticipos suelen
    cargarse ANTES de recibir la importación)."""
    existe = db.fetch_one(
        "SELECT id_importacion_pago FROM scintela.importacion_pago WHERE im_numero = %s",
        (im_numero,), conn=conn,
    )
    if not existe:
        db.execute_returning(
            """
            INSERT INTO scintela.importacion_pago
                (im_numero, codigo_prov, ref_num, usuario_crea)
            VALUES (%s, %s, %s, %s)
            RETURNING id_importacion_pago
            """,
            (im_numero, (prov or "").strip().upper(), 0, usuario[:50]),
            conn=conn,
        )


def agregar_movimiento(
    im_numero: str,
    tipo: str,
    monto,
    *,
    fecha=None,
    nota: str = "",
    prov: str = "",
    usuario: str = "web",
) -> dict:
    """Registra un anticipo como MOVIMIENTO nuevo (nada se pisa).

    TMT 2026-07-06 (dueña): cada anticipo genera AUTOMÁTICAMENTE su NOTA DE
    DÉBITO en Pichincha (antes la hacía a mano — la pantalla avisa para que
    no la dupliquen). Todo en UNA transacción:
      movimiento + ND (bank_helpers, saldo running ok) + mov_doble
      ('importacion_anticipo') + recompute del cache (anticipo_aplicado =
      Σ movimientos = valor del stock).

    La UI v2 solo carga 'anticipo'; 'pago' sigue aceptado por si acaso (el
    CHECK de la tabla admite ambos) — mismo circuito, otro rótulo.

    NO usamos bancos.queries.crear_movimiento_simple a propósito: su router
    dBase (_routear_mov_simple) puede disparar side effects por el concepto
    (compra directa / anticipo USD / retiro) y además abre su propia tx.

    Devuelve {id_mov, id_transaccion, saldo_nuevo, anticipo_aplicado}.
    """
    import bank_helpers
    import mov_doble as _md
    from periodo_guard import asegurar_fecha_abierta

    im_numero = (im_numero or "").strip()
    if not im_numero:
        raise ValueError("Importación inválida (falta el número IM-).")
    tipo = (tipo or "").strip().lower()
    if tipo not in ("anticipo", "pago"):
        raise ValueError("Tipo de movimiento inválido (anticipo o pago).")
    monto_f = round(float(monto or 0), 2)
    if monto_f <= 0:
        raise ValueError("El monto debe ser mayor a 0.")
    if not _tabla_mov_existe():
        raise ValueError(
            "Falta correr la migración 0113 (scintela.importacion_pago_mov). "
            "Corré /admin/migraciones."
        )
    fecha = fecha or today_ec()
    prov = (prov or "").strip().upper()
    nota = (nota or "").strip()[:120]
    asegurar_fecha_abierta(fecha)

    pref = "ANT" if tipo == "anticipo" else "PAGO"
    tipo_md = "importacion_anticipo" if tipo == "anticipo" else "importacion_pago_parcial"

    with db.tx() as conn:
        _asegurar_fila(conn, im_numero, prov, usuario)
        row = db.execute_returning(
            """
            INSERT INTO scintela.importacion_pago_mov
                (im_numero, tipo, fecha, monto, nota, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id_mov
            """,
            (im_numero, tipo, fecha, monto_f, nota or None, usuario[:50]),
            conn=conn,
        ) or {}
        id_mov = row.get("id_mov")

        # ND automática en Pichincha — concepto "ANT IMP <im> <prov>".
        # insert_movimiento_bancario mantiene el saldo running (y recomputa
        # solo si la fecha es pasada).
        concepto_nd = f"{pref} IMP {im_numero} {prov}".strip()[:50]
        mov_b = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=_BANCO_PICHINCHA,
            no_cta=None,
            fecha=fecha,
            documento="ND",
            importe=monto_f,
            concepto=concepto_nd,
            prov=(prov or None),
            usuario=usuario,
        )
        id_tx = mov_b.get("id_transaccion")
        saldo_nuevo = mov_b.get("saldo_nuevo")
        db.execute(
            "UPDATE scintela.importacion_pago_mov SET id_transaccion = %s WHERE id_mov = %s",
            (id_tx, id_mov), conn=conn,
        )

        _md.registrar(
            conn=conn,
            tipo=tipo_md,
            origen_table="importacion_pago_mov",
            origen_id=id_mov,
            destino_table="transacciones_bancarias",
            destino_id=id_tx,
            importe=monto_f,
            fecha=fecha,
            concepto=(f"{pref} IMP {im_numero} {prov}" + (f" — {nota}" if nota else ""))[:200],
            usuario=usuario,
            metadata={
                "im_numero": im_numero,
                "tipo": tipo,
                "prov": prov,
                "no_banco": _BANCO_PICHINCHA,
                "id_transaccion": id_tx,
            },
        )

        derivado = _recompute_cache(conn, im_numero, usuario) or {}

    return {
        "id_mov": id_mov,
        "id_transaccion": id_tx,
        "saldo_nuevo": saldo_nuevo,
        **derivado,
    }


def deshacer_movimiento(id_mov, *, usuario: str = "web") -> dict:
    """✕ de un movimiento: borra el movimiento Y deshace su ND — par atómico.

    Patrón de reverso bancario del repo (reversar_movimiento_simple /
    insertar_compensacion): la ND(−) NO se borra, se COMPENSA con una NC(+)
    de hoy (paper trail completo). El movimiento sí se borra (es PC-only) y
    el cache se recalcula. mov_doble: fila reverso enlazada al original
    (estado='reversado'). Todo en UNA transacción.

    Movimientos sin ND linkeada (backfill mig 0113: esas ND las hizo la
    dueña a mano en su momento) solo borran el movimiento — su ND se
    resuelve en el banco.
    """
    import bank_helpers
    import mov_doble as _md
    from periodo_guard import asegurar_fecha_abierta

    if not id_mov:
        raise ValueError("Movimiento inválido.")
    mov = db.fetch_one(
        """
        SELECT id_mov, im_numero, tipo, fecha, monto, nota, id_transaccion
          FROM scintela.importacion_pago_mov
         WHERE id_mov = %s
        """,
        (int(id_mov),),
    )
    if not mov:
        raise ValueError(f"El movimiento #{id_mov} no existe (¿ya se deshizo?).")

    im_numero = (str(mov.get("im_numero") or "")).strip()
    tipo = (mov.get("tipo") or "").strip()
    monto_f = abs(float(mov.get("monto") or 0))
    id_tx = mov.get("id_transaccion")
    pref = "ANT" if tipo == "anticipo" else "PAGO"
    tipo_md = "importacion_anticipo" if tipo == "anticipo" else "importacion_pago_parcial"

    fecha_rev = today_ec()
    if id_tx:
        asegurar_fecha_abierta(fecha_rev)

    with db.tx() as conn:
        id_tx_rev = None
        if id_tx:
            tx_orig = db.fetch_one(
                """
                SELECT id_transaccion, no_banco, no_cta, documento
                  FROM scintela.transacciones_bancarias
                 WHERE id_transaccion = %s
                """,
                (id_tx,), conn=conn,
            )
            if tx_orig:
                # ND(−) se compensa con NC(+) — mismo mapa que
                # reversar_movimiento_simple ({"ND": "NC"}).
                mov_rev = bank_helpers.insert_movimiento_bancario(
                    conn,
                    no_banco=int(tx_orig["no_banco"]),
                    no_cta=tx_orig.get("no_cta"),
                    fecha=fecha_rev,
                    documento="NC",
                    importe=monto_f,
                    concepto=f"REVERSO {pref} IMP {im_numero}"[:50],
                    usuario=usuario,
                )
                id_tx_rev = mov_rev.get("id_transaccion")

        # mov_doble del reverso, enlazado al original (queda 'reversado').
        md_orig = db.fetch_one(
            """
            SELECT id_mov_doble FROM scintela.mov_doble
             WHERE origen_table = 'importacion_pago_mov'
               AND origen_id = %s AND estado = 'activo'
             ORDER BY id_mov_doble DESC LIMIT 1
            """,
            (int(id_mov),), conn=conn,
        ) or {}
        _md.registrar(
            conn=conn,
            tipo=f"reverso_{tipo_md}",
            origen_table="importacion_pago_mov",
            origen_id=int(id_mov),
            destino_table=("transacciones_bancarias" if id_tx_rev else "importacion_pago_mov"),
            destino_id=(id_tx_rev or int(id_mov)),
            importe=monto_f,
            fecha=fecha_rev,
            concepto=f"REVERSO {pref} IMP {im_numero}"[:200],
            usuario=usuario,
            id_original=md_orig.get("id_mov_doble"),
            metadata={
                "im_numero": im_numero,
                "tipo": tipo,
                "id_transaccion_orig": id_tx,
                "id_transaccion_reverso": id_tx_rev,
            },
        )

        db.execute(
            "DELETE FROM scintela.importacion_pago_mov WHERE id_mov = %s",
            (int(id_mov),), conn=conn,
        )
        derivado = _recompute_cache(conn, im_numero, usuario) or {}

    return {
        "im_numero": im_numero,
        "tipo": tipo,
        "monto": monto_f,
        "id_transaccion_reverso": id_tx_rev,
        **derivado,
    }


def set_recepcion(im_numero: str, prov: str, num, *, kg, costo_estimado=None, usuario: str = "web") -> None:
    """Recibe la importación: los kg entran al stock (recibido_pc=TRUE).

    Modelo v2 (TMT 2026-07-06): recibir ya NO genera deuda ni toca anticipos —
    el valor del stock de la importación es Σ anticipos (movimientos) y el
    restante se carga por /compras. `costo_estimado` se acepta opcional SOLO
    como referencia histórica (nada lo usa para valuar).
    """
    kg_f = round(float(kg or 0), 2)
    if kg_f <= 0:
        raise ValueError("Los kilos recibidos deben ser mayores a 0.")
    campos = ["recibido_pc = %s", "fecha_recepcion_pc = %s", "kg_recibidos = %s"]
    valores: list = [True, today_ec(), kg_f]
    if costo_estimado is not None:
        costo = round(float(costo_estimado or 0), 2)
        if costo < 0:
            raise ValueError("El costo de referencia no puede ser negativo.")
        campos.append("costo_estimado = %s")
        valores.append(costo)
    _upsert(im_numero, prov, num, campos=campos, valores=valores, usuario=usuario)


def deshacer_recepcion(im_numero: str, prov: str = "", num=None, *, usuario: str = "web") -> None:
    """Revierte la recepción: vuelve a 'en tránsito' (saca los kg del stock).
    Los MOVIMIENTOS (anticipos + sus ND) NO se tocan — son plata real; se
    deshacen uno a uno con su ✕ si corresponde."""
    _upsert(
        im_numero, prov, num,
        campos=["recibido_pc = %s", "fecha_recepcion_pc = %s", "kg_recibidos = %s"],
        valores=[False, None, None],
        usuario=usuario,
    )
