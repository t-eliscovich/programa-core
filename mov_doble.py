"""Historial unificado de movimientos dobles.

Cada operación cruzada (caja↔banco, compra↔banco, transferencia banco↔banco,
endoso de cheque, aporte/retiro de capital, transferencia USD, etc.) deja
una fila en `scintela.mov_doble`. Los reversos crean otra fila enlazada al
original y marcan al original como `estado='reversado'`.

Invariante: **registrar() se llama DENTRO del mismo `db.tx()`** que los
inserts de origen y destino. Si el registro falla, rollback de todo —
mejor abortar la operación que tener un par sin auditoría.

Uso típico:

    with db.tx() as conn:
        row_caja  = db.execute_returning(INSERT_CAJA, ..., conn=conn)
        row_banco = bank_helpers.insert_movimiento_bancario(conn, ...)
        mov_doble.registrar(
            conn=conn,
            tipo="caja_a_banco",
            origen_table="caja",
            origen_id=row_caja["id_caja"],
            destino_table="transacciones_bancarias",
            destino_id=row_banco["id_transaccion"],
            importe=importe,
            fecha=fecha,
            concepto=concepto,
            usuario=usuario,
        )

Para reversar:

    with db.tx() as conn:
        # crear los inversos en ambas tablas...
        mov_doble.registrar(
            conn=conn,
            tipo="caja_a_banco",
            origen_table="caja",       origen_id=id_caja_inverso,
            destino_table="transacciones_bancarias", destino_id=id_tx_inverso,
            importe=importe, fecha=fecha,
            concepto=f"REVERSO {concepto_original}",
            usuario=usuario,
            id_original=id_mov_doble_original,  # ← clave
        )
"""
from __future__ import annotations

import json
from datetime import date

import db


def registrar(
    *,
    conn,
    tipo: str,
    origen_table: str,
    origen_id,
    destino_table: str,
    destino_id,
    importe,
    fecha: date,
    concepto: str = "",
    usuario: str = "web",
    metadata: dict | None = None,
    id_original=None,
    batch_id: str | None = None,
) -> int | None:
    """Inserta una fila en scintela.mov_doble.

    Si `id_original` está, esta fila es un reverso — además de crearla,
    marca la fila original con estado='reversado' + id_reverso apuntando
    a la nueva.

    Si `batch_id` (UUID) está, esta fila pertenece a una operación batch:
    múltiples movs del mismo submit comparten el batch_id. El reverso
    atómico de /historial los revierte todos juntos. TMT 2026-05-15.

    Devuelve el id_mov_doble creado, o None si origen/destino vacíos.

    Casos de no-registro (devuelven None sin lanzar):
      - origen_id o destino_id son None/0 (no hay par real).
      - importe es 0 o None.

    Si la tabla `scintela.mov_doble` aún no existe (migración 0023 sin
    aplicar), no levanta — devuelve None y deja al caller seguir. Esto
    evita bloquear todo el sistema si la migración no corrió todavía.
    """
    if not origen_id or not destino_id:
        return None
    importe_f = float(importe or 0)
    if importe_f == 0:
        return None

    estado = "reverso" if id_original else "activo"

    try:
        row = db.execute_returning(
            """
            INSERT INTO scintela.mov_doble
                (fecha_operacion, tipo, origen_table, origen_id,
                 destino_table, destino_id, importe, concepto, usuario,
                 estado, id_original, metadata, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, %s)
            RETURNING id_mov_doble
            """,
            (
                fecha, tipo, origen_table, int(origen_id),
                destino_table, int(destino_id),
                # Preservamos el signo del importe — antes hacíamos abs() y
                # perdíamos info de devoluciones/retiros negativos en /historial.
                # El template ya decide el color y prefijo según tipo. TMT 2026-05-13.
                importe_f, (concepto or "")[:200], (usuario or "")[:50],
                estado, id_original,
                json.dumps(metadata) if metadata else None,
                batch_id,
            ),
            conn=conn,
        ) or {}
        new_id = row.get("id_mov_doble")
        if id_original and new_id:
            db.execute(
                "UPDATE scintela.mov_doble "
                "SET estado='reversado', id_reverso=%s, "
                "    fecha_creacion=fecha_creacion "  # no tocar timestamp
                "WHERE id_mov_doble=%s",
                (new_id, id_original),
                conn=conn,
            )
        return new_id
    except Exception as e:
        # Si la tabla no existe (migración 0023 pendiente), no romper.
        msg = str(e).lower()
        if "mov_doble" in msg and ("does not exist" in msg
                                   or "no existe" in msg
                                   or "relation" in msg):
            return None
        # Si la columna batch_id no existe (migración 0031 pendiente),
        # reintentar el INSERT sin batch_id — degrada graciosamente.
        # TMT 2026-05-15.
        if "batch_id" in msg and ("does not exist" in msg
                                  or "no existe" in msg
                                  or "column" in msg):
            try:
                row = db.execute_returning(
                    """
                    INSERT INTO scintela.mov_doble
                        (fecha_operacion, tipo, origen_table, origen_id,
                         destino_table, destino_id, importe, concepto, usuario,
                         estado, id_original, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s::jsonb)
                    RETURNING id_mov_doble
                    """,
                    (
                        fecha, tipo, origen_table, int(origen_id),
                        destino_table, int(destino_id),
                        importe_f, (concepto or "")[:200], (usuario or "")[:50],
                        estado, id_original,
                        json.dumps(metadata) if metadata else None,
                    ),
                    conn=conn,
                ) or {}
                new_id = row.get("id_mov_doble")
                if id_original and new_id:
                    db.execute(
                        "UPDATE scintela.mov_doble "
                        "SET estado='reversado', id_reverso=%s, "
                        "    fecha_creacion=fecha_creacion "
                        "WHERE id_mov_doble=%s",
                        (new_id, id_original),
                        conn=conn,
                    )
                return new_id
            except Exception:
                return None
        # Cualquier otro error: re-raise. Si la tabla existe pero algo
        # falló (constraint, tipo de dato), queremos abort del flujo.
        raise


def buscar_por_origen(
    *,
    origen_table: str,
    origen_id: int,
    conn=None,
) -> dict | None:
    """Devuelve la fila mov_doble activa de un movimiento origen, o None.

    Útil para que reverses busquen el id_original. Si hay varias filas
    para el mismo origen (no debería), devuelve la última activa.
    """
    try:
        return db.fetch_one(
            """
            SELECT id_mov_doble, tipo, destino_table, destino_id, importe,
                   estado, id_reverso
              FROM scintela.mov_doble
             WHERE origen_table = %s AND origen_id = %s
               AND estado = 'activo'
             ORDER BY id_mov_doble DESC
             LIMIT 1
            """,
            (origen_table, int(origen_id)),
            conn=conn,
        )
    except Exception:
        return None


def buscar_por_batch(
    *,
    batch_id: str,
    incluir_reversos: bool = False,
    conn=None,
) -> list[dict]:
    """Devuelve TODAS las filas que comparten el mismo batch_id.

    Usado por el reverso atómico de /historial — cuando la dueña hace
    click en "reversar" sobre cualquier fila de un batch, se reversan
    las N filas hermanas juntas dentro de una sola transacción.

    Por default sólo devuelve las activas (no las que ya son reversos o
    están reversadas) — el reverso atómico no quiere re-reversar lo ya
    reversado.

    Orden: id_mov_doble ASC (orden de creación) — el handler de reverso
    revierte en orden INVERSO de creación para deshacer las dependencias
    en orden correcto.

    TMT 2026-05-15.
    """
    if not batch_id:
        return []
    try:
        where_estado = "" if incluir_reversos else " AND estado = 'activo'"
        rows = db.fetch_all(
            f"""
            SELECT id_mov_doble, fecha_operacion, tipo,
                   origen_table, origen_id, destino_table, destino_id,
                   importe, concepto, usuario, estado, id_reverso,
                   id_original, metadata, batch_id
              FROM scintela.mov_doble
             WHERE batch_id = %s::uuid
             {where_estado}
             ORDER BY id_mov_doble ASC
            """,
            (str(batch_id),),
            conn=conn,
        )
        return list(rows or [])
    except Exception as e:
        # Si la columna batch_id todavía no existe (0031 pendiente), devuelve [].
        msg = str(e).lower()
        if "batch_id" in msg or "column" in msg:
            return []
        raise


def buscar_por_destino(
    *,
    destino_table: str,
    destino_id: int,
    conn=None,
) -> dict | None:
    """Análogo a buscar_por_origen pero busca por el lado destino.

    Útil cuando reversás desde el lado destino (ej. anular una compra
    pagada → encontrar el mov_doble que la enlaza al banco).
    """
    try:
        return db.fetch_one(
            """
            SELECT id_mov_doble, tipo, origen_table, origen_id, importe,
                   estado, id_reverso
              FROM scintela.mov_doble
             WHERE destino_table = %s AND destino_id = %s
               AND estado = 'activo'
             ORDER BY id_mov_doble DESC
             LIMIT 1
            """,
            (destino_table, int(destino_id)),
            conn=conn,
        )
    except Exception:
        return None
