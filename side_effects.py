"""Side effects automáticos para movimientos concept-driven.

Dado un dict producido por `concepto_parser.parse_concepto()`, inserta la
fila secundaria en la tabla destino usando los helpers existentes
(bank_helpers, caja_helpers). Todo dentro de la misma transacción del
caller — usa el `conn` que viene de `db.tx()`.

Diseñado para que el caller (caja.crear, bancos.emitir_cheque, etc.)
NO tenga que saber qué tabla tocar — sólo `parse → dispatch`.

Convención de signos:
  - Si el movimiento ORIGEN es un egreso (caja sale, banco egresa),
    el side effect ES un ingreso al destino (caja → banco / proveedor / etc.).
  - Si el origen es un ingreso (raro pero posible), el side effect es egreso.

Por ahora cubrimos los tipos más comunes:
  - transfer_banco    → bank_helpers.insert_movimiento_bancario(documento='DE')
  - retiro_socio      → INSERT en scintela.retiros + bank_helpers (si origen banco)
  - dolares           → INSERT en scintela.dolares
  - compra_proveedor  → INSERT en scintela.compra (TIPO='C' por default)
  - caja_inhb         → INSERT en scintela.caja con marca HB
  - none              → no-op
"""

from __future__ import annotations

from datetime import date

import db


def aplicar_side_effect(
    *,
    parsed: dict,
    importe: float,
    fecha: date,
    origen: str,            # 'caja_egreso' | 'caja_ingreso' | 'banco_egreso' | 'banco_ingreso'
    usuario: str,
    conn,
    inverso: bool = False,
    id_destino_original: int | None = None,   # para reverso de compra_proveedor: anular la fila original en vez de insertar negativa
) -> dict | None:
    """Aplica el side effect descrito por `parsed`. Devuelve dict con info
    de lo creado, o None si no hubo side effect.

    `inverso=True` invierte el side effect (para reversar movimientos):
      - transfer_banco: en vez de DE crea CH (o viceversa)
      - retiro_socio: importe negativo en scintela.retiros
      - dolares: signo opuesto
      - compra_proveedor: importe negativo (no-anular, traza compensatoria)

    Si algo falla, levanta excepción y el caller (en su db.tx) debería
    rollback automáticamente.
    """
    tipo = parsed.get("tipo")
    if not tipo or tipo == "none":
        return None

    # Importes siempre positivos en abs — el SIGNO lo da el origen + destino.
    importe_abs = abs(float(importe or 0))
    if importe_abs <= 0:
        return None

    # Direccionalidad: si caja egresa, la plata entra a destino → DE (ingreso).
    # Si caja ingresa (raro), la plata sale del destino → CH (egreso).
    # En modo inverso, todo se da vuelta.
    origen_es_egreso = origen.endswith("_egreso")
    if inverso:
        origen_es_egreso = not origen_es_egreso

    if tipo == "transfer_banco":
        return _aplicar_transfer_banco(
            parsed=parsed, importe=importe_abs, fecha=fecha,
            es_ingreso_al_banco=origen_es_egreso,
            usuario=usuario, conn=conn, inverso=inverso,
        )

    if tipo == "retiro_socio":
        return _aplicar_retiro_socio(
            parsed=parsed, importe=importe_abs, fecha=fecha,
            usuario=usuario, conn=conn, inverso=inverso,
        )

    if tipo == "dolares":
        return _aplicar_dolares(
            parsed=parsed, importe=importe_abs, fecha=fecha,
            es_ingreso=origen_es_egreso,
            usuario=usuario, conn=conn, inverso=inverso,
        )

    if tipo == "compra_proveedor":
        return _aplicar_compra(
            parsed=parsed, importe=importe_abs, fecha=fecha,
            usuario=usuario, conn=conn, inverso=inverso,
            id_destino_original=id_destino_original,
        )

    if tipo == "caja_inhb":
        # Variante de caja — el legacy tenía sub-flujos (capital/retiros)
        # según subcódigo; por ahora sólo lo trackeamos en concepto.
        return {"tipo": "caja_inhb", "nota": "no implementado (legacy)"}

    if tipo == "gasto":
        # TMT 2026-05-15: keyword del concepto matcheó V1..V9 (PINTURA→V6,
        # LUZ→V5, SUELDO→V7, etc.). Crear xgast directamente — cero clicks.
        return _aplicar_gasto(
            parsed=parsed, importe=importe_abs, fecha=fecha,
            usuario=usuario, conn=conn, inverso=inverso,
        )

    return None


def _aplicar_gasto(
    *, parsed, importe, fecha, usuario, conn, inverso=False,
) -> dict:
    """Crea fila scintela.xgast con num=V1..V9 detectado por keyword."""
    num = int(parsed.get("num") or 9)
    concepto = (parsed.get("resto") or parsed.get("concepto_original") or "")[:100]
    if inverso:
        # En reverso, anulamos la fila creada originalmente. La identificamos
        # buscando por fecha+importe+num+concepto (no tenemos id directo
        # del original sin pasarlo explícito).
        return {"tipo": "gasto", "nota": "reverso de gasto no implementado"}
    row = db.execute_returning(
        """
        INSERT INTO scintela.xgast
            (fecha, doc, prov, concepto, num, fechad, importe, saldo, stat,
             usuario_crea)
        VALUES (%s, 'OTR', NULL, %s, %s, %s, %s, 0, 'A', %s)
        RETURNING id_xgast
        """,
        (fecha, concepto, num, fecha, importe, usuario),
        conn=conn,
    ) or {}
    return {
        "tipo": "gasto",
        "id_xgast": row.get("id_xgast"),
        "num": num,
    }


def _aplicar_transfer_banco(
    *, parsed, importe, fecha, es_ingreso_al_banco, usuario, conn, inverso=False,
) -> dict:
    """Inserta el movimiento en el banco destino. Reusa bank_helpers."""
    import bank_helpers

    no_banco = parsed.get("no_banco")
    if not no_banco:
        # Resolver por nombre (mismo patrón que el resto del app).
        nombre = parsed.get("banco_nombre") or ""
        all_b = db.fetch_all(
            "SELECT no_banco, COALESCE(nombre, '') AS nombre "
            "FROM scintela.banco ORDER BY no_banco",
            conn=conn,
        ) or []
        needle = nombre.replace("INTERNACIONAL", "INTER").upper()[:6]
        m = next(
            (b for b in all_b
             if needle in (b.get("nombre") or "").upper()),
            None,
        )
        if not m:
            raise ValueError(
                f"No encontré banco '{parsed.get('banco_nombre')}' "
                f"en scintela.banco. Revisá los nombres."
            )
        no_banco = int(m["no_banco"])

    documento = "DE" if es_ingreso_al_banco else "CH"
    prefijo = "REV " if inverso else "DE CAJA "
    concepto = (prefijo + (parsed.get("resto") or "")).strip()[:50]
    mov = bank_helpers.insert_movimiento_bancario(
        conn,
        no_banco=no_banco,
        no_cta=None,
        fecha=fecha,
        documento=documento,
        importe=importe,
        concepto=concepto,
        usuario=usuario,
    )
    return {
        "tipo": "transfer_banco",
        "no_banco": no_banco,
        "id_transaccion": mov.get("id_transaccion"),
        "saldo_nuevo": mov.get("saldo_nuevo"),
        "inverso": inverso,
    }


def _aplicar_retiro_socio(
    *, parsed, importe, fecha, usuario, conn, inverso=False,
) -> dict:
    """Inserta una fila en scintela.retiros para el socio indicado."""
    socio = (parsed.get("socio") or "")[:5]
    base_concepto = (parsed.get("concepto_original") or "")[:80]
    concepto = ("REV " + base_concepto) if inverso else base_concepto
    importe_signed = -importe if inverso else importe
    row = db.execute_returning(
        """
        INSERT INTO scintela.retiros
            (fecha, ret, de, concepto, clave, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id_retiro
        """,
        (fecha, importe_signed, socio, concepto, socio[:3], usuario[:50]),
        conn=conn,
    ) or {}
    return {
        "tipo": "retiro_socio",
        "socio": socio,
        "id_retiro": row.get("id_retiro"),
        "inverso": inverso,
    }


def _aplicar_dolares(
    *, parsed, importe, fecha, es_ingreso, usuario, conn, inverso=False,
) -> dict:
    """Inserta una fila en scintela.dolares."""
    cuenta = (parsed.get("cuenta") or "")[:5]
    base = (parsed.get("resto") or "")[:55]
    concepto = (("REV " + base) if inverso else base)[:60]
    # es_ingreso ya fue invertido por aplicar_side_effect si inverso=True.
    importe_signed = importe if es_ingreso else -importe
    row = db.execute_returning(
        """
        INSERT INTO scintela.dolares
            (fecha, cta, importe, concepto, clave, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id_dolares
        """,
        (fecha, cuenta, importe_signed, concepto, cuenta[:3], usuario[:50]),
        conn=conn,
    ) or {}
    return {
        "tipo": "dolares",
        "cuenta": cuenta,
        "id_dolares": row.get("id_dolares"),
        "inverso": inverso,
    }


def _aplicar_compra(
    *, parsed, importe, fecha, usuario, conn, inverso=False,
    id_destino_original: int | None = None,
) -> dict:
    """Inserta una fila en scintela.compra (TIPO='C' = otros por default).

    El dBase legacy decide el TIPO según la categoría del proveedor
    (H=hilados, K=tejido, T=tintura, Q=químicos). Sin tener esa tabla
    de categorías en Programa Core, usamos 'C' por defecto. El usuario
    puede editarlo después en /compras/<id>/editar.

    En modo inverso:
      - Si recibe `id_destino_original`, anula la compra original
        (stat='Y') en vez de insertar una negativa. UX más limpia —
        antes quedaba el par activa+compensatoria visualmente confuso.
      - Si no recibe id (fallback), inserta una compra con importe
        negativo (comportamiento legacy).
    TMT 2026-05-13.
    """
    prov = (parsed.get("prov") or "")[:3]
    base = (parsed.get("resto") or "")[:95]
    if inverso and id_destino_original:
        # Anular la compra original
        db.execute(
            """
            UPDATE scintela.compra
               SET stat = 'Y',
                   observacion = COALESCE(observacion||' | ','') ||
                                 ('[REVERSO caja ' || CURRENT_DATE::text || ' — ' || %s || ']'),
                   usuario_modifica = %s,
                   fecha_modifica = CURRENT_TIMESTAMP
             WHERE id_compra = %s
            """,
            (base[:80], usuario[:50], id_destino_original),
            conn=conn,
        )
        return {
            "tipo": "compra_proveedor",
            "prov": prov,
            "id_compra": id_destino_original,
            "inverso": True,
            "modo": "anulado",
        }
    # Comportamiento legacy: inserta una compensación negativa.
    concepto = ("REV " + base) if inverso else base
    importe_signed = -importe if inverso else importe
    row = db.execute_returning(
        """
        INSERT INTO scintela.compra
            (fecha, codigo_prov, tipo, importe, concepto, no_banco,
             clave, usuario_crea)
        VALUES (%s, %s, 'C', %s, %s, 9, %s, %s)
        RETURNING id_compra
        """,
        (fecha, prov, importe_signed, concepto[:100], prov[:3], usuario[:50]),
        conn=conn,
    ) or {}
    return {
        "tipo": "compra_proveedor",
        "prov": prov,
        "id_compra": row.get("id_compra"),
        "inverso": inverso,
        "modo": "compensacion_negativa",
    }
