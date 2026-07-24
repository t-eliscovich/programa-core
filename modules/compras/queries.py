"""Consultas de compras (facturas de proveedor).

La tabla scintela.compra NO tiene columna de saldo. La deuda viva se lee de
scintela.posdat (coincide con INFORMES.PRG: 'TOTP = SUM posdat WHERE banc <> 9').
Este módulo muestra el histórico de compras; para deudas vivas, ir a /proveedores
o al informe de deudas.

Vocabulario canónico de tipos (TMT 2026-05-19 — corregido por Tamara):

    K (KK) = tejeduría — sin kg = compra de servicio; con kg = PRODUCCIÓN
    H (HH) = hilado (kg de hilado)
    Q (QQ) = químicos (colorantes + auxiliares)
    C (CC) = tintorería (servicio de tintura + insumos) — NO es "Otros".
              Era mi mala interpretación previa; la dueña lo corrigió.
    A (AA) = anticipo (a proveedor sin factura todavía; luego se "convierte")
    I (IN) = anticipo para máquinas (variante de A para maquinaria)

LC2 (left-concepto-2): los códigos de 2 letras (KK, CC, etc.) son el mapeo
LC2 del dBase. Internamente el schema usa el char único (K, C, ...). El
mapping LC2 ↔ tipo vive en `labels.TIPOS_COMPRA_LC2`. El selector en
/compras/nueva muestra los LC2 al usuario pero submite el char único.

La discriminación compras vs producción es:
    PRODUCCIÓN  ⟺ tipo = 'K' AND kg > 0
    COMPRAS     ⟺ todo lo demás (H, Q, C, K-sin-kg, A, I)
"""

from datetime import date, timedelta

import db
from filters import today_ec
from periodo_guard import asegurar_fecha_abierta

# Set de tipos válidos para validación al alta. Cualquier otro valor → ValueError.
# TMT 2026-05-19 — agregado 'I' (IN = Anticipo máquinas, pedido Tamara).
# Importante: C ahora significa TINTORERÍA (no "Consumibles"/"Otros").
TIPOS_VALIDOS = ("K", "H", "Q", "C", "A", "I")

# Etiquetas legibles para la UI — fuente única en labels.py para mantener
# consistencia con todo el resto del programa (TMT 2026-05-12).
import labels as _L

TIPOS_LABEL = {k: _L.TIPOS_COMPRA_LABEL[k] for k in TIPOS_VALIDOS}


def es_produccion(tipo: str | None, kg) -> bool:
    """True si la fila es producción (K + kg > 0)."""
    return (tipo or "").upper().strip() == "K" and bool(kg) and float(kg or 0) > 0.01


def proximo_numero() -> int:
    """Siguiente número de compra (MAX+1)."""
    row = db.fetch_one("SELECT COALESCE(MAX(numero), 0) + 1 AS siguiente FROM scintela.compra")
    return int(row["siguiente"]) if row else 1


# Cuentas válidas para pago al instante de una compra.
CUENTAS_PAGO = ("caja", "pichincha", "internacional")


def crear(
    *,
    fecha: date,
    codigo_prov: str,
    importe,
    kg=None,
    tipo: str | None = None,
    comprobante: str | None = None,
    numero: int | None = None,
    concepto: str | None = None,
    fechad: date | None = None,
    no_banco: int | None = None,
    clave: str | None = None,
    pagada: bool = False,
    cuenta: str | None = None,
    pago_parcial=None,
    es_anticipo_dolares: bool = False,
    usuario: str = "web",
) -> dict:
    """Alta de compra (factura de proveedor).

    Reglas heredadas + extensiones del addendum batch 22 §D:

      - Si no es pagada contado (`pagada=False`, lo habitual), se crea una
        fila en `scintela.posdat` con `banc=0` (abierta). Esa fila es la
        deuda viva con el proveedor — los informes leen de posdat, no de
        compra.
      - Si `pagada=True`, NO crea posdat. Si además se especifica `cuenta`:
          * cuenta='caja'         → INSERT caja TIPO='S' con saldo running.
          * cuenta='pichincha'    → INSERT tx_bancarias DOC='CH' banco=1.
          * cuenta='internacional'→ INSERT tx_bancarias DOC='CH' banco=2.
        El `id_transaccion`/`id_caja` resultante queda en `compra.id_transaccion`
        para enlace.
      - Si `es_anticipo_dolares=True` y proveedor.tipo ∈ ('HIL','QUI') →
        INSERT en `scintela.dolares` (paridad ALTAS.PRG L229-233).
      - `fechad` (vencimiento) por defecto = fecha + proveedor.plazo días
        (fallback 30).
      - `numero` = MAX+1 si no se pasa.

    Pago parcial (TMT 2026-05-12 — Fase P movimientos dobles):
      - Si `pago_parcial > 0` AND `pago_parcial < importe`, se considera
        compra con pago parcial. `cuenta` es obligatorio (de dónde sale el
        pago parcial). Comportamiento atómico:
          * INSERT side-effect banco/caja por `pago_parcial` (no por `importe`).
          * INSERT posdat con `importe - pago_parcial` (saldo pendiente).
          * compra.cuenta_pagada = 'P' (partial).
        `pagada` se ignora en este caso (es siempre False conceptualmente —
        queda algo por pagar).
      - Si `pago_parcial >= importe`, equivale a pago total (pagada=True).
      - Si `pago_parcial = 0` o None, comportamiento clásico.

    Todo en una sola transacción.
    """
    asegurar_fecha_abierta(fecha)

    # Validación de tipo
    if tipo:
        tipo_norm = tipo.upper().strip()
        if tipo_norm not in TIPOS_VALIDOS:
            raise ValueError(
                f"Tipo inválido: {tipo!r}. Valores permitidos: "
                f"{', '.join(TIPOS_VALIDOS)} (ver docs/SKILL_ADDENDUM_BATCH_18.md)."
            )
        tipo = tipo_norm

    # TMT 2026-06-03 audit fix: si numero=None se computa adentro de la tx
    # con advisory lock. Antes proximo_numero() se llamaba ACÁ AFUERA, lo
    # que permitía dos crear() concurrentes calcular el mismo MAX+1 y
    # crear compras duplicadas. La lock vive en el bloque tx más abajo.
    # numero==None deja la asignación para dentro de la tx.

    if fechad is None:
        row = db.fetch_one(
            "SELECT plazo FROM scintela.proveedor WHERE codigo_prov = %s",
            (codigo_prov,),
        )
        dias = int(row["plazo"]) if row and row.get("plazo") else 30
        fechad = fecha + timedelta(days=dias)

    # Validación de cuenta de pago (si se pasó pagada=True o pago_parcial>0)
    cuenta_norm = (cuenta or "").lower().strip() or None
    if pagada and cuenta_norm and cuenta_norm not in CUENTAS_PAGO:
        raise ValueError(f"Cuenta de pago inválida: {cuenta!r}. Debe ser una de: {', '.join(CUENTAS_PAGO)}.")

    # --- Resolver modo de pago (TMT 2026-05-12 Fase P) ---
    importe_f = float(importe or 0)
    pago_parcial_f = float(pago_parcial or 0) if pago_parcial else 0.0
    if pago_parcial_f < 0:
        raise ValueError("El pago parcial no puede ser negativo.")
    # TMT 2026-06-16 (OP/over-price): el chequeo "excede" sólo aplica si hay
    # un pago parcial REAL (>0). Antes se disparaba con cualquier importe
    # negativo (0 > -14535) y bloqueaba el alta del pasivo negativo a "OP".
    if pago_parcial_f > 0.01 and pago_parcial_f > importe_f + 0.01:
        raise ValueError(
            f"El pago parcial ({pago_parcial_f:.2f}) excede el importe de la compra ({importe_f:.2f})."
        )

    # Normalizar: si pago_parcial == importe, es pago total clásico.
    if pago_parcial_f >= importe_f - 0.01 and pago_parcial_f > 0:
        pago_parcial_f = 0.0
        pagada = True
        # cuenta queda como vino — el caller la pasó.

    es_pago_parcial = pago_parcial_f > 0.01

    if es_pago_parcial:
        if not cuenta_norm:
            raise ValueError("Pago parcial requiere especificar `cuenta` (caja, pichincha o internacional).")
        if cuenta_norm not in CUENTAS_PAGO:
            raise ValueError(
                f"Cuenta de pago inválida: {cuenta!r}. Debe ser una de: {', '.join(CUENTAS_PAGO)}."
            )
        # En modo parcial, pagada=False conceptualmente — queda saldo en posdat.
        pagada = False

    importe_pago_inmediato = importe_f if pagada else (pago_parcial_f if es_pago_parcial else 0.0)
    saldo_posdat = importe_f - importe_pago_inmediato

    with db.tx() as conn:
        # TMT 2026-06-03 audit fix: advisory lock para serializar asignación
        # de numero. Misma clave que facturas usa para numf (4243 para compras
        # para no colisionar). Si numero ya vino del caller, igualmente lockeamos
        # para que no se choquen dos asignaciones automáticas concurrentes.
        # try/except defensivo: tests con cursor mock no tienen rowcount.
        try:
            db.execute(
                "SELECT pg_advisory_xact_lock(4243)",
                (), conn=conn,
            )
        except (AttributeError, TypeError):
            pass
        if numero is None:
            np_row = db.fetch_one(
                "SELECT COALESCE(MAX(numero), 0) + 1 AS siguiente FROM scintela.compra",
                (), conn=conn,
            )
            numero = int(np_row["siguiente"]) if np_row else 1

        prov_row = db.fetch_one(
            "SELECT id_proveedor, tipo AS tipo_prov FROM scintela.proveedor WHERE codigo_prov = %s",
            (codigo_prov,),
            conn=conn,
        )
        if not prov_row:
            raise ValueError(f"El proveedor {codigo_prov!r} no existe.")

        # Si hay pago inmediato (total o parcial) por banco → resolver no_banco.
        no_banco_pago = no_banco
        cuenta_pagada_letra = None
        if importe_pago_inmediato > 0 and cuenta_norm:
            if cuenta_norm == "pichincha":
                no_banco_pago = 1
                cuenta_pagada_letra = "B"
            elif cuenta_norm == "internacional":
                no_banco_pago = 2
                cuenta_pagada_letra = "B"
            elif cuenta_norm == "caja":
                cuenta_pagada_letra = "C"
            # Marca 'P' para pago parcial (saldo en posdat).
            if es_pago_parcial:
                cuenta_pagada_letra = "P"

        compra = (
            db.execute_returning(
                """
            INSERT INTO scintela.compra
                (fecha, id_proveedor, codigo_prov, tipo, comprobante,
                 kg, importe, numero, fecha_ing, fechad, concepto,
                 clave, no_banco, usuario_crea, cuenta_pagada)
            VALUES (%s, %s, %s, %s, %s,
                    %s, %s, %s, CURRENT_DATE, %s, %s,
                    %s, %s, %s, %s)
            RETURNING id_compra, numero
            """,
                (
                    fecha,
                    prov_row["id_proveedor"],
                    codigo_prov.upper().strip(),
                    (tipo or None),
                    (comprobante or None),
                    kg,
                    importe_f,
                    numero,
                    fechad,
                    (concepto or None),
                    (clave or None) and clave[:3],
                    no_banco_pago,
                    usuario,
                    cuenta_pagada_letra,
                ),
                conn=conn,
            )
            or {}
        )

        # Side-effect bancario o de caja por el importe inmediato (total o parcial).
        id_transaccion = None
        if importe_pago_inmediato > 0 and cuenta_norm:
            sufijo_concepto = " (parcial)" if es_pago_parcial else ""
            concepto_pago = (
                (concepto or comprobante or f"Compra #{numero} {codigo_prov}") + sufijo_concepto
            )[:50]
            if cuenta_norm == "caja":
                import caja_helpers

                res = caja_helpers.insert_movimiento_caja(
                    conn,
                    fecha=fecha,
                    tipo="S",
                    importe=importe_pago_inmediato,
                    concepto=concepto_pago,
                    usuario=usuario,
                )
                id_transaccion = res["id_caja"]
            else:
                import bank_helpers

                res = bank_helpers.insert_movimiento_bancario(
                    conn,
                    no_banco=no_banco_pago,
                    no_cta=None,
                    fecha=fecha,
                    documento="CH",  # cheque emitido / pago por banco
                    importe=importe_pago_inmediato,
                    concepto=concepto_pago,
                    prov=codigo_prov.upper().strip(),
                    numreferencia=compra.get("numero"),
                    usuario=usuario,
                )
                id_transaccion = res["id_transaccion"]
            # Linkar la compra al movimiento (audit + reverse trail)
            if id_transaccion is not None:
                db.execute(
                    "UPDATE scintela.compra SET id_transaccion=%s WHERE id_compra=%s",
                    (id_transaccion, compra["id_compra"]),
                    conn=conn,
                )
                # Historial unificado (TMT 2026-05-12 Fase I).
                import mov_doble as _md

                destino_table = "caja" if cuenta_norm == "caja" else "transacciones_bancarias"
                tipo_md = "compra_pago_parcial" if es_pago_parcial else f"compra_pagada_{cuenta_norm}"
                _md.registrar(
                    conn=conn,
                    tipo=tipo_md,
                    origen_table="compra",
                    origen_id=compra.get("id_compra"),
                    destino_table=destino_table,
                    destino_id=id_transaccion,
                    importe=importe_pago_inmediato,
                    fecha=fecha,
                    concepto=(concepto or comprobante or f"Compra #{numero} {codigo_prov}")[:200],
                    usuario=usuario,
                    metadata={
                        "codigo_prov": codigo_prov,
                        "numero_compra": numero,
                        "es_parcial": es_pago_parcial,
                        "importe_total_compra": importe_f,
                    },
                )

        # Anticipo USD si proveedor es HIL/QUI y flag activo
        if es_anticipo_dolares and (prov_row.get("tipo_prov") or "").upper() in ("HIL", "QUI"):
            dol_row = (
                db.execute_returning(
                    """
                INSERT INTO scintela.dolares
                    (fecha, cta, importe, concepto, usuario_crea)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id_dolares
                """,
                    (
                        fecha,
                        codigo_prov.upper().strip()[:5],
                        importe_f,
                        (concepto or f"Anticipo {codigo_prov}")[:50],
                        usuario,
                    ),
                    conn=conn,
                )
                or {}
            )
            # Historial unificado: compra→dolares para que el anticipo
            # USD aparezca linkeado en /historial. TMT 2026-05-13.
            if dol_row.get("id_dolares"):
                import mov_doble as _md

                _md.registrar(
                    conn=conn,
                    tipo="compra_anticipo_dolares",
                    origen_table="compra",
                    origen_id=compra.get("id_compra"),
                    destino_table="dolares",
                    destino_id=dol_row["id_dolares"],
                    importe=importe_f,
                    fecha=fecha,
                    concepto=(f"Anticipo USD a {codigo_prov} (compra #{numero})")[:200],
                    usuario=usuario,
                    metadata={
                        "codigo_prov": codigo_prov,
                        "numero_compra": numero,
                        "tipo_prov": prov_row.get("tipo_prov"),
                    },
                )
        # Contrapartida en posdat:
        #   - pagada total → nada (no hay deuda → saldo_posdat ≈ 0).
        #   - no pagada    → posdat por importe completo (deuda original).
        #   - pago parcial → posdat por saldo_posdat (importe - parcial).
        # TMT 2026-06-16 (OP/over-price): usamos abs() para que una compra
        # NEGATIVA (sobreprecio "OP") también genere su posdat banc=0 con
        # importe negativo. Así cuenta como pasivo negativo en posdat_totales
        # (TOTP = SUM(importe) banc=0), imitando COMPRAS.DBF BANC#9 del dBase.
        if abs(saldo_posdat) > 0.01:
            concepto_posdat = concepto or comprobante or None
            if es_pago_parcial:
                concepto_posdat = (f"saldo {codigo_prov} #{numero} (pagué {importe_pago_inmediato:.0f})")[:50]
            posdat_row = (
                db.execute_returning(
                    """
                INSERT INTO scintela.posdat
                    (fecha, fechad, prov, num, importe, concepto, banc, clave, usuario_crea)
                VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s)
                RETURNING id_posdat
                """,
                    (
                        fecha,
                        fechad,
                        codigo_prov.upper().strip(),
                        numero,
                        saldo_posdat,
                        concepto_posdat,
                        (clave or None) and clave[:3],
                        usuario,
                    ),
                    conn=conn,
                )
                or {}
            )
            # Historial unificado: registramos compra→posdat para que la
            # compra a crédito aparezca en /historial. Si la compra ya tuvo
            # pago inmediato (parcial), ya hay un mov_doble compra→banco;
            # este segundo registra el saldo que quedó como deuda.
            # TMT 2026-05-13: la dueña pidió "todo aparece en historial".
            if posdat_row.get("id_posdat"):
                import mov_doble as _md

                tipo_md = "compra_saldo_a_posdat" if es_pago_parcial else "compra_a_posdat"
                _md.registrar(
                    conn=conn,
                    tipo=tipo_md,
                    origen_table="compra",
                    origen_id=compra.get("id_compra"),
                    destino_table="posdat",
                    destino_id=posdat_row["id_posdat"],
                    importe=saldo_posdat,
                    fecha=fecha,
                    concepto=(concepto_posdat or f"Compra #{numero} {codigo_prov}")[:200],
                    usuario=usuario,
                    metadata={
                        "codigo_prov": codigo_prov,
                        "numero_compra": numero,
                        "es_parcial": es_pago_parcial,
                        "importe_total_compra": importe_f,
                        "fechad": fechad.isoformat() if fechad else None,
                    },
                )

    if id_transaccion is not None:
        compra["id_transaccion"] = id_transaccion
    if es_pago_parcial:
        compra["pago_parcial"] = importe_pago_inmediato
        compra["saldo_posdat"] = saldo_posdat
    return compra


def editar(
    id_compra: int,
    *,
    concepto: str | None = None,
    comprobante: str | None = None,
    fechad: date | None = None,
    observacion: str | None = None,
    importe=None,
    tipo: str | None = None,
    usuario: str = "web",
) -> dict:
    """Edición *blanda* de una compra existente.

    Reglas (paridad addendum batch 22 §D):
      - Compras pagadas al instante (id_transaccion ≠ NULL): bloqueado
        cambio de importe/fechad. Para corregir hay que anular y reemitir.
      - Si tiene posdat hermana abierta (banc=0): los cambios de importe/
        fechad se propagan automáticamente al posdat.
      - `concepto`, `comprobante`, `observacion`: edición libre.
      - `tipo`: edición libre (TMT 2026-07-17, dueña: reclasificar NC/QI de
        Q a C — es clasificación, no toca importe ni posdat).
      - `asegurar_fecha_abierta(compra.fecha)` — el período original.
    """
    compra = db.fetch_one(
        "SELECT id_compra, fecha, codigo_prov, numero, importe, fechad, "
        "       tipo, concepto, comprobante, stat, id_transaccion "
        "FROM scintela.compra WHERE id_compra = %s",
        (id_compra,),
    )
    if not compra:
        raise ValueError("Compra inexistente.")
    if (compra.get("stat") or "").upper() == "Y":
        raise ValueError("Compra anulada — no se puede editar.")

    asegurar_fecha_abierta(compra["fecha"])

    pagada = compra.get("id_transaccion") is not None

    # #24 (TMT 2026-05-14): detectar si la compra tiene un pago parcial
    # ACTIVO (mov_doble tipo='compra_pago_parcial' estado='activo'). En
    # ese caso la posdat hermana tiene importe = importe_compra − parcial,
    # NO importe_compra. Editar importe sin restar el parcial le metería
    # un saldo incorrecto al proveedor. Decisión: BLOQUEAR el cambio de
    # importe con mensaje claro (más simple que propagar el delta).
    md_parcial = db.fetch_one(
        """
        SELECT id_mov_doble, importe FROM scintela.mov_doble
         WHERE origen_table = 'compra'
           AND origen_id    = %s
           AND tipo         = 'compra_pago_parcial'
           AND estado       = 'activo'
         ORDER BY id_mov_doble DESC LIMIT 1
        """,
        (id_compra,),
    )
    parcial_pagado = float((md_parcial or {}).get("importe") or 0)
    tiene_pago_parcial_activo = parcial_pagado > 0.01

    # Validar locks por estado
    nuevo_importe = compra["importe"]
    nuevo_fechad = compra["fechad"]
    if importe is not None and float(importe) != float(compra["importe"] or 0):
        if pagada:
            raise ValueError(
                "Compra ya pagada — el importe no se puede editar. Para corregir, anular y reemitir."
            )
        if tiene_pago_parcial_activo:
            raise ValueError(
                f"Esta compra tiene un pago parcial activo de "
                f"${parcial_pagado:.2f}. Editar el importe rompería la "
                f"posdat hermana (que guarda el saldo, no el total). "
                f"Para corregir: anular la compra y reemitir."
            )
        nuevo_importe = float(importe)
    if fechad is not None and fechad != compra["fechad"]:
        if pagada:
            raise ValueError("Compra ya pagada — la fecha de vencimiento no se puede editar.")
        nuevo_fechad = fechad

    nuevo_tipo = None
    if tipo is not None:
        tipo_norm = tipo.upper().strip()
        if tipo_norm not in TIPOS_VALIDOS:
            raise ValueError(f"Tipo inválido: {tipo!r}. Debe ser uno de: {', '.join(TIPOS_VALIDOS)}.")
        if tipo_norm != (compra.get("tipo") or "").upper().strip():
            nuevo_tipo = tipo_norm

    obs_marca = f"[E] {observacion[:120]}" if observacion else None

    with db.tx() as conn:
        sql_set = ["importe=%s", "fechad=%s", "usuario_modifica=%s"]
        params: list = [nuevo_importe, nuevo_fechad, usuario]
        if concepto is not None:
            sql_set.append("concepto=%s")
            params.append(concepto or None)
        if comprobante is not None:
            sql_set.append("comprobante=%s")
            params.append(comprobante or None)
        if nuevo_tipo is not None:
            sql_set.append("tipo=%s")
            params.append(nuevo_tipo)
        if obs_marca:
            sql_set.append("observacion = COALESCE(observacion||' | ','')||%s")
            params.append(obs_marca)
        params.append(id_compra)

        db.execute(
            f"UPDATE scintela.compra SET {', '.join(sql_set)} WHERE id_compra=%s",
            tuple(params),
            conn=conn,
        )

        # Propagar a posdat hermana si existe (banc=0).
        # Si hay pago parcial activo (no llegamos acá si cambia importe —
        # raise arriba), el campo `nuevo_importe` == importe_anterior, así
        # que el UPDATE de la posdat no cambia el saldo. Sólo se propaga
        # fechad (que sí puede cambiar). Mantenemos el SET completo por
        # simplicidad — si nuevo_importe == importe_anterior, la fila
        # queda idem.
        if not pagada:
            db.execute(
                "UPDATE scintela.posdat "
                "SET importe=%s, fechad=%s, usuario_modifica=%s "
                "WHERE prov=%s AND num=%s AND banc=0 "
                "  AND (anulada IS NOT TRUE OR anulada IS NULL)",
                (
                    # Si hay parcial activo, mantenemos el saldo de posdat
                    # = importe_compra_actual − parcial. Como no
                    # permitimos cambiar el importe en ese caso, esto
                    # equivale a no tocar el importe de la posdat.
                    (nuevo_importe - parcial_pagado) if tiene_pago_parcial_activo else nuevo_importe,
                    nuevo_fechad,
                    usuario,
                    compra["codigo_prov"],
                    compra["numero"],
                ),
                conn=conn,
            )

    return {
        "id_compra": id_compra,
        "importe_previo": float(compra["importe"] or 0),
        "importe_nuevo": float(nuevo_importe or 0),
        "fechad_previo": compra["fechad"],
        "fechad_nuevo": nuevo_fechad,
        "pagada": pagada,
    }


def por_id(id_compra: int) -> dict | None:
    """Ficha de una compra por ID — alimenta la vista de confirmación/detalle."""
    return db.fetch_one(
        """
        SELECT c.id_compra, c.fecha, c.fechad, c.codigo_prov, c.tipo,
               c.comprobante, c.numero, c.kg, c.importe, c.concepto,
               c.clave, c.no_banco, c.stat, c.observacion,
               c.fecha_crea, c.usuario_crea,
               COALESCE(p.nombre, '') AS proveedor,
               COALESCE(b.nombre, '') AS banco
        FROM scintela.compra c
        LEFT JOIN scintela.proveedor p ON p.codigo_prov = c.codigo_prov
        LEFT JOIN scintela.banco b     ON b.no_banco    = c.no_banco
        WHERE c.id_compra = %s
        """,
        (id_compra,),
    )


def anular(id_compra: int, *, motivo: str = "", usuario: str = "web") -> int:
    """Marca la compra como anulada (stat='Y') Y reverte sus side-effects.

    Reglas (paridad con facturas.anular pero con compensación atómica):
        - Debe existir.
        - No puede estar ya anulada.
        - Motivo opcional (queda en observacion).
        - **La posdat hermana** (prov + num) se BORRA — la obligación de pago
          desaparece con la compra.
        - **Si era pagada con id_transaccion bancaria**: inserta una ND
          compensatoria en transacciones_bancarias vía bank_helpers.
        - **Si era pagada con cuenta_pagada='C' (caja)**: inserta una E
          compensatoria en caja vía caja_helpers.
        - **Si tenía anticipo USD asociado** (compra_anticipo_dolares): inserta
          una fila en dolares con importe negativo del original.
        - **Si era endoso (cuenta_pagada='E')**: se delega — el reverso del
          endoso vive en `cheques.reversar_endoso`, no acá. Levanta ValueError
          si se intenta anular directo desde compras (la dueña tiene que
          reversar el endoso desde el cheque).
        - Marca mov_doble original como 'reversado' + INSERT mov_doble reverso.
        - Todo atómico.

    TMT 2026-05-13.
    """
    motivo = (motivo or "").strip()  # opcional

    compra = db.fetch_one(
        """
        SELECT id_compra, codigo_prov, numero, stat, importe, fecha,
               cuenta_pagada, id_transaccion, no_banco
          FROM scintela.compra
         WHERE id_compra = %s
        """,
        (id_compra,),
    )
    if not compra:
        raise ValueError("Compra inexistente.")
    if (compra.get("stat") or "").upper() == "Y":
        raise ValueError("La compra ya está anulada.")
    if (compra.get("cuenta_pagada") or "").upper() == "E":
        raise ValueError(
            "Esta compra fue pagada por endoso de cheque — anularla desde "
            "/compras dejaría el cheque en limbo. Reversá el endoso desde "
            "/cheques (botón 'Reversar endoso')."
        )

    importe_compra = float(compra.get("importe") or 0)
    cuenta = (compra.get("cuenta_pagada") or "").upper()
    obs_marca = f"[ANUL] {motivo[:150]}" if motivo else "[ANUL]"
    fecha_rev = today_ec()

    with db.tx() as conn:
        # 1) Marca la compra como anulada
        db.execute(
            """
            UPDATE scintela.compra
               SET stat = 'Y',
                   observacion = COALESCE(observacion||' | ','')||%s,
                   usuario_modifica = %s
             WHERE id_compra = %s
            """,
            (obs_marca, usuario, id_compra),
            conn=conn,
        )

        # 2) Anula la posdat hermana (si existe) — saldo o deuda íntegra.
        # #10 (TMT 2026-05-14): primero chequear que la posdat NO esté
        # pagada con cheque emitido (banc<>0). Si está pagada y la
        # borramos, dejamos la partida bancaria colgada sin contrapartida.
        # Cambiado de DELETE a soft-delete (migración 0027) para preservar
        # audit trail; los filtros de listado/balance ya ignoran anuladas.
        if compra.get("numero") is not None and compra.get("codigo_prov"):
            posdat_hermana = db.fetch_one(
                """
                SELECT id_posdat, banc FROM scintela.posdat
                 WHERE prov = %s AND num = %s
                   AND (anulada IS NOT TRUE OR anulada IS NULL)
                """,
                (compra["codigo_prov"], compra["numero"]),
                conn=conn,
            )
            if posdat_hermana and (posdat_hermana.get("banc") or 0) != 0:
                raise ValueError(
                    f"No se puede anular: la posdat hermana ya fue pagada "
                    f"con cheque (banc={posdat_hermana['banc']}). Reversá "
                    f"el cheque emitido primero desde /bancos o /cheques."
                )
            db.execute(
                """
                UPDATE scintela.posdat
                   SET anulada = TRUE,
                       motivo_anulacion = %s,
                       fecha_anulacion = CURRENT_TIMESTAMP,
                       usuario_modifica = %s
                 WHERE prov = %s AND num = %s
                   AND (anulada IS NOT TRUE OR anulada IS NULL)
                """,
                (
                    (
                        f"compra anulada #{compra.get('numero')} — {motivo}"[:200]
                        if motivo
                        else f"compra anulada #{compra.get('numero')}"
                    )[:200],
                    usuario[:50],
                    compra["codigo_prov"],
                    compra["numero"],
                ),
                conn=conn,
            )

        # 3) Reverte side-effects según cuenta_pagada
        side_effect_resumen: list[str] = []

        # 3a) Pagada por caja (cuenta_pagada='C'). En pagos parciales también
        # puede tener cuenta='P' con id_transaccion apuntando a caja: detectamos
        # por id_transaccion + ausencia de no_banco.
        if cuenta in ("C", "P") and compra.get("id_transaccion") and not compra.get("no_banco"):
            # Buscar el importe real del lado caja (puede ser parcial).
            row_caja = db.fetch_one(
                "SELECT importe FROM scintela.caja WHERE id_caja = %s",
                (compra["id_transaccion"],),
                conn=conn,
            )
            if row_caja:
                imp_caja = float(row_caja.get("importe") or 0)
                import caja_helpers

                caja_helpers.insert_movimiento_caja(
                    conn,
                    fecha=fecha_rev,
                    tipo="E",
                    importe=imp_caja,
                    concepto=(f"REVERSO compra #{compra.get('numero') or id_compra}")[:80],
                    clave="REV",
                    usuario=usuario,
                )
                side_effect_resumen.append(f"caja +${imp_caja:.2f}")

        # 3b) Pagada por banco (cuenta_pagada='B') o parcial banco.
        if cuenta in ("B", "P") and compra.get("id_transaccion") and compra.get("no_banco"):
            # Buscar el movimiento original para saber el importe exacto.
            row_tx = db.fetch_one(
                """
                SELECT importe, no_banco FROM scintela.transacciones_bancarias
                 WHERE id_transaccion = %s
                """,
                (compra["id_transaccion"],),
                conn=conn,
            )
            if row_tx:
                imp_tx = abs(float(row_tx.get("importe") or 0))
                import bank_helpers

                bank_helpers.insert_movimiento_bancario(
                    conn,
                    no_banco=int(row_tx["no_banco"]),
                    no_cta=None,
                    fecha=fecha_rev,
                    documento="NC",  # nota de crédito = ingreso al banco (cancela el CH)
                    importe=imp_tx,
                    concepto=(f"REVERSO compra #{compra.get('numero') or id_compra}")[:50],
                    prov=compra.get("codigo_prov"),
                    numreferencia=compra.get("numero"),
                    usuario=usuario,
                    stat="A",
                )
                side_effect_resumen.append(f"banco +${imp_tx:.2f}")

        # 3c) Anticipo USD — la compra tenía un mov_doble de
        # tipo='compra_anticipo_dolares' que apuntaba a una fila en dolares.
        md_usd = db.fetch_one(
            """
            SELECT destino_id, importe FROM scintela.mov_doble
             WHERE origen_table = 'compra' AND origen_id = %s
               AND tipo = 'compra_anticipo_dolares'
               AND estado = 'activo'
             LIMIT 1
            """,
            (id_compra,),
            conn=conn,
        )
        if md_usd and md_usd.get("destino_id"):
            row_usd = db.fetch_one(
                "SELECT cta, importe FROM scintela.dolares WHERE id_dolares = %s",
                (md_usd["destino_id"],),
                conn=conn,
            )
            if row_usd:
                imp_orig = float(row_usd.get("importe") or 0)
                # Defensa de signo: si el original era positivo (caso típico —
                # anticipo entrante), reverso = negativo. Si el original ya era
                # negativo (caso raro), igual queremos compensación NEGATIVA
                # del valor absoluto, no doblar el negativo. TMT 2026-05-13.
                imp_reverso = -abs(imp_orig)
                db.execute(
                    """
                    INSERT INTO scintela.dolares
                        (fecha, cta, importe, concepto, usuario_crea)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        fecha_rev,
                        row_usd.get("cta") or "",
                        imp_reverso,
                        f"REVERSO anticipo compra #{compra.get('numero') or id_compra}"[:50],
                        usuario[:50],
                    ),
                    conn=conn,
                )
                side_effect_resumen.append(f"USD -${abs(float(row_usd.get('importe') or 0)):.2f}")

        # 4) Marca el mov_doble original como reversado + INSERT reverso.
        # R2 (TMT 2026-05-14): NO suprimir excepciones — antes había
        # try/except: pass silencioso que ocultaba bugs reales del
        # historial. Si mov_doble.registrar falla, abortamos toda la
        # anulación: prefer rollback que dejar mov_doble desincronizado.
        import mov_doble as _md

        md_orig_rows = (
            db.fetch_all(
                """
            SELECT id_mov_doble, tipo, importe FROM scintela.mov_doble
             WHERE origen_table = 'compra'
               AND origen_id    = %s
               AND estado       = 'activo'
             ORDER BY id_mov_doble DESC
            """,
                (id_compra,),
                conn=conn,
            )
            or []
        )
        for md_orig in md_orig_rows:
            _md.registrar(
                conn=conn,
                tipo="reverso_compra_anulada",
                origen_table="compra",
                origen_id=id_compra,
                destino_table="compra",
                destino_id=id_compra,
                importe=float(md_orig.get("importe") or importe_compra),
                fecha=fecha_rev,
                concepto=(
                    f"ANULACION compra #{compra.get('numero') or id_compra}"
                    + (f" — {motivo}" if motivo else "")
                    + (f" [{', '.join(side_effect_resumen)}]" if side_effect_resumen else "")
                )[:200],
                usuario=usuario,
                metadata={
                    "motivo": motivo or "",
                    "id_compra": id_compra,
                    "numero_compra": compra.get("numero"),
                    "codigo_prov": compra.get("codigo_prov"),
                    "side_effects_reversados": side_effect_resumen,
                    "tipo_orig": md_orig.get("tipo"),
                },
                id_original=md_orig["id_mov_doble"],
            )

    return 1


def convertir_anticipo(
    id_compra: int,
    *,
    nuevo_tipo: str,
    kg=None,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Convierte una fila tipo='A' (anticipo) a una compra real (K/H/Q/C).

    Se usa cuando llega la factura del proveedor y el anticipo deja de ser
    "pago sin factura": pasa a ser una compra concreta. Si el destino es
    'K' y se pasan kg > 0, queda como producción.

    Reglas:
        - El tipo origen debe ser 'A' (anticipo). Cualquier otro origen
          levanta ValueError.
        - El tipo destino debe estar en TIPOS_VALIDOS y no ser 'A'.
        - Audit: anota en observacion el motivo + cambio de tipo.
    """
    nuevo_tipo = (nuevo_tipo or "").upper().strip()
    if nuevo_tipo not in TIPOS_VALIDOS:
        raise ValueError(f"Tipo destino inválido: {nuevo_tipo!r}.")
    if nuevo_tipo == "A":
        raise ValueError("El tipo destino no puede ser 'A' — eso no es convertir nada.")

    compra = db.fetch_one(
        "SELECT id_compra, tipo, kg FROM scintela.compra WHERE id_compra = %s",
        (id_compra,),
    )
    if not compra:
        raise ValueError("Compra inexistente.")
    tipo_actual = (compra.get("tipo") or "").upper().strip()
    if tipo_actual != "A":
        raise ValueError(f"Sólo se pueden convertir anticipos. Tipo actual: {tipo_actual!r}.")

    motivo = (motivo or "").strip()
    nuevo_kg = kg if kg is not None else compra.get("kg")
    marca = (
        f"[CONV {tipo_actual}→{nuevo_tipo}"
        + (f" kg={nuevo_kg}" if nuevo_kg else "")
        + (f" — {motivo[:80]}" if motivo else "")
        + "]"
    )
    db.execute(
        "UPDATE scintela.compra "
        "SET tipo=%s, kg=%s, "
        "    observacion = COALESCE(observacion || ' | ', '') || %s, "
        "    usuario_modifica=%s "
        "WHERE id_compra=%s",
        (nuevo_tipo, nuevo_kg, marca, usuario, id_compra),
    )
    return {
        "id_compra": id_compra,
        "tipo_previo": tipo_actual,
        "tipo_nuevo": nuevo_tipo,
        "kg": nuevo_kg,
        "es_produccion": es_produccion(nuevo_tipo, nuevo_kg),
    }


def total_buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    incluir_anuladas: bool = False,
    vista: str = "todas",
    kg_filter: str | None = None,
    tipo: str | None = None,
    numero: int | None = None,
) -> dict:
    """SUM(importe) + COUNT(*) sobre TODO el universo del filtro (sin LIMIT).

    TMT 2026-05-20 PASADA 6 Federico #15 — antes el total se calculaba
    sumando `filas` que ya venían limitadas a 500. Cuando había >500
    compras en el filtro, el total no se movía con cambios del filtro.
    Ahora va una query separada sin LIMIT.
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    row = db.fetch_one(
        """
        SELECT COUNT(*)                    AS n,
               COALESCE(SUM(c.importe), 0) AS total,
               COALESCE(SUM(c.kg), 0)      AS total_kg
        FROM scintela.compra c
        LEFT JOIN scintela.proveedor p ON p.codigo_prov = c.codigo_prov
        WHERE (%(incluir_anuladas)s OR COALESCE(c.stat, '') <> 'Y')
          AND COALESCE(c.stat, '') <> 'Y'
          AND (%(q)s IS NULL
               OR UPPER(TRIM(c.codigo_prov)) = UPPER(TRIM(%(q)s)))
          -- Filtro por NÚMERO (dígitos del campo flexible) — dueña 2026-07-11.
          -- TMT 2026-07-23 (dueña): "AC 15" no andaba porque el 15 vive en el
          -- CONCEPTO (ej "15" / "15 SALDO"), no en c.numero (que suele ser el
          -- comprobante). Ahora matchea contra la parte numérica del concepto O
          -- contra c.numero — igual que el cruce de /importaciones.
          AND (%(numero)s::int IS NULL
               OR c.numero = %(numero)s::int
               OR NULLIF(substring(TRIM(COALESCE(c.concepto, '')) FROM '[0-9]+'), '')::int
                    = %(numero)s::int)
          AND (%(desde)s::date IS NULL OR c.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR c.fecha <= %(hasta)s::date)
          AND (%(tipo)s IS NULL OR UPPER(TRIM(COALESCE(c.tipo, ''))) = %(tipo)s)
          AND (
                %(kg_filter)s IS NULL
             OR (%(kg_filter)s = 'gt0' AND ABS(COALESCE(c.kg, 0)) > 0.01)
             OR (%(kg_filter)s = 'eq0' AND ABS(COALESCE(c.kg, 0)) <= 0.01)
          )
          AND (
                %(vista)s = 'todas'
             OR (%(vista)s = 'produccion'
                 AND UPPER(COALESCE(c.tipo, '')) = 'K'
                 AND ABS(COALESCE(c.kg, 0)) > 0.01)
             OR (%(vista)s = 'anticipos'
                 AND UPPER(COALESCE(c.tipo, '')) = 'A')
             OR (%(vista)s = 'compras'
                 -- TMT 2026-07-24 (dueña): "quiero ver la maquila (Ponce/Reyes)
                 -- en Compras". Antes Compras escondía a TODO proveedor con
                 -- producción (NOT EXISTS por prov), y de rebote ocultaba la
                 -- maquila tercerizada — que es una compra REAL a terceros
                 -- (genera costo y cuenta por pagar). Ahora se esconde SOLO la
                 -- autoproducción INTELA (prov='KK': los ~202k kg/mes de
                 -- "compra a sí mismo" que inundarían la lista). Los
                 -- tercerizados (AP/RY, prov<>'KK') SÍ se listan con su
                 -- desglose. Cambio de DISPLAY: no toca la utilidad ni el panel
                 -- COSTOS (esos leen scintela.compra tipo K directo, sin este
                 -- filtro — ver tejido_mes_componentes()).
                 AND UPPER(TRIM(COALESCE(c.codigo_prov, ''))) <> 'KK')
          )
        """,
        {
            "q": q or None,
            "like": like,
            "desde": desde or None,
            "hasta": hasta or None,
            "incluir_anuladas": bool(incluir_anuladas),
            "vista": (vista or "todas").lower(),
            "kg_filter": kg_filter,
            "tipo": ((tipo or "").upper().strip() or None),
            "numero": numero,
        },
    )
    return {
        "n": int(row["n"] or 0) if row else 0,
        "total": float(row["total"] or 0) if row else 0.0,
        "total_kg": float(row["total_kg"] or 0) if row else 0.0,
    }


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
    incluir_anuladas: bool = False,
    vista: str = "todas",
    kg_filter: str | None = None,
    tipo: str | None = None,
    numero: int | None = None,
) -> list[dict]:
    """Histórico de compras filtrable por proveedor/concepto/comprobante + fecha.

    Por default esconde las anuladas (stat='Y'). Pasar `incluir_anuladas=True`
    para verlas en el histórico con un badge.

    `vista` (2026-04-29): filtra por categoría de la nueva taxonomía:
        'todas'      → todo (default)
        'compras'    → tipos H, Q, C, K-sin-kg
        'produccion' → tipo K con kg > 0
        'anticipos'  → tipo A

    TMT 2026-05-18 — `kg_filter`:
        'gt0' → solo filas con kg > 0 (producción diaria)
        'eq0' → solo filas con kg = 0 (compras sin kg)
        None  → sin restricción
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    vista = (vista or "todas").lower().strip()
    rows = (
        db.fetch_all(
            """
        SELECT c.id_compra, c.fecha, c.fechad, c.codigo_prov, c.tipo,
               c.comprobante, c.numero, c.kg, c.importe, c.concepto,
               c.clave, c.no_banco, c.stat, c.observacion, c.cuenta_pagada,
               c.usuario_crea,
               COALESCE(p.nombre, '') AS proveedor,
               p.plazo                AS plazo,
               (c.fecha + (COALESCE(p.plazo, 0) * INTERVAL '1 day'))::date
                                      AS fecha_vencimiento,
               COALESCE(b.nombre, '') AS banco,
               (UPPER(COALESCE(c.tipo, '')) = 'K'
                AND COALESCE(c.kg, 0) > 0.01)               AS es_produccion,
               -- PAGADA (dueña 2026-07-23, "no va a haber sync, armá la lógica"):
               -- fuente de verdad = el posdatado (deuda viva). Una compra de PC
               -- se vincula a su posdat por scintela.mov_doble (compra→posdat,
               -- estado='activo'). Está PENDIENTE mientras ese posdat siga abierto
               -- (banc=0); pasa a PAGADA cuando el posdat se salda (banc<>0). Las
               -- históricas del dBase NO tienen ese vínculo → fallback al BANC
               -- importado (compra.no_banco 9/banco=pagada, 0/vacío=pendiente) o
               -- a cuenta_pagada (compras de PC pagadas contado/banco).
               CASE
                 WHEN EXISTS (
                        SELECT 1 FROM scintela.mov_doble md
                        JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND COALESCE(pd.banc, 0) = 0
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                 ) THEN FALSE
                 WHEN EXISTS (
                        SELECT 1 FROM scintela.mov_doble md
                        JOIN scintela.posdat pd ON pd.id_posdat = md.destino_id
                        WHERE md.origen_table = 'compra' AND md.origen_id = c.id_compra
                          AND md.destino_table = 'posdat' AND md.estado = 'activo'
                          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                 ) THEN TRUE
                 ELSE ((c.no_banco IS NOT NULL AND c.no_banco <> 0)
                       OR (c.cuenta_pagada IS NOT NULL AND btrim(c.cuenta_pagada) <> ''))
               END                                          AS pagada
        FROM scintela.compra c
        LEFT JOIN scintela.proveedor p ON p.codigo_prov = c.codigo_prov
        LEFT JOIN scintela.banco b     ON b.no_banco    = c.no_banco
        WHERE (%(incluir_anuladas)s OR COALESCE(c.stat, '') <> 'Y')
          AND (%(q)s IS NULL
               OR UPPER(TRIM(c.codigo_prov)) = UPPER(TRIM(%(q)s)))
          -- Filtro por NÚMERO (dígitos del campo flexible) — dueña 2026-07-11.
          -- TMT 2026-07-23 (dueña): "AC 15" no andaba porque el 15 vive en el
          -- CONCEPTO (ej "15" / "15 SALDO"), no en c.numero (que suele ser el
          -- comprobante). Ahora matchea contra la parte numérica del concepto O
          -- contra c.numero — igual que el cruce de /importaciones.
          AND (%(numero)s::int IS NULL
               OR c.numero = %(numero)s::int
               OR NULLIF(substring(TRIM(COALESCE(c.concepto, '')) FROM '[0-9]+'), '')::int
                    = %(numero)s::int)
          AND (%(desde)s::date IS NULL OR c.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR c.fecha <= %(hasta)s::date)
          -- Filtro por TIPO (H/K/Q/C/A/I) — dueña 2026-07-09
          AND (%(tipo)s IS NULL OR UPPER(TRIM(COALESCE(c.tipo, ''))) = %(tipo)s)
          -- TMT 2026-05-18 — filtro KG (>0 producción, =0 compras sin kg)
          AND (
                %(kg_filter)s IS NULL
             OR (%(kg_filter)s = 'gt0' AND ABS(COALESCE(c.kg, 0)) > 0.01)
             OR (%(kg_filter)s = 'eq0' AND ABS(COALESCE(c.kg, 0)) <= 0.01)
          )
          -- Filtro por vista (compras / producción / anticipos)
          AND (
                %(vista)s = 'todas'
             OR (%(vista)s = 'produccion'
                 AND UPPER(COALESCE(c.tipo, '')) = 'K'
                 AND ABS(COALESCE(c.kg, 0)) > 0.01)
             OR (%(vista)s = 'anticipos'
                 AND UPPER(COALESCE(c.tipo, '')) = 'A')
             OR (%(vista)s = 'compras'
                 -- TMT 2026-07-24 (dueña): "quiero ver la maquila (Ponce/Reyes)
                 -- en Compras". Antes Compras escondía a TODO proveedor con
                 -- producción (NOT EXISTS por prov), y de rebote ocultaba la
                 -- maquila tercerizada — que es una compra REAL a terceros
                 -- (genera costo y cuenta por pagar). Ahora se esconde SOLO la
                 -- autoproducción INTELA (prov='KK': los ~202k kg/mes de
                 -- "compra a sí mismo" que inundarían la lista). Los
                 -- tercerizados (AP/RY, prov<>'KK') SÍ se listan con su
                 -- desglose. Cambio de DISPLAY: no toca la utilidad ni el panel
                 -- COSTOS (esos leen scintela.compra tipo K directo, sin este
                 -- filtro — ver tejido_mes_componentes()).
                 AND UPPER(TRIM(COALESCE(c.codigo_prov, ''))) <> 'KK')
          )
        ORDER BY c.fecha DESC, c.id_compra DESC
        LIMIT %(limite)s
        """,
            {
                "q": q or None,
                "like": like,
                "desde": desde or None,
                "hasta": hasta or None,
                "limite": limite,
                "incluir_anuladas": bool(incluir_anuladas),
                "vista": vista,
                "kg_filter": (kg_filter or None),
                "tipo": ((tipo or "").upper().strip() or None),
                "numero": numero,
            },
        )
        or []
    )

    # Saldo acumulado running TMT 2026-05-13: total comprado corrido por fecha.
    # Ordenar ASC para acumular, marcar cada fila, revertir a DESC para mostrar.
    rows_asc = sorted(rows, key=lambda r: (r.get("fecha") or date.min, r.get("id_compra") or 0))
    acum = 0.0
    for r in rows_asc:
        acum += float(r.get("importe") or 0)
        r["saldo_acumulado"] = acum
    return list(reversed(rows_asc))


def conteos_por_vista(
    desde: str | None = None,
    hasta: str | None = None,
    incluir_anuladas: bool = False,
) -> dict:
    """Conteos para las pestañas (todas / compras / produccion / anticipos).

    Computa SOBRE EL UNIVERSO COMPLETO (con los filtros de fecha y anuladas
    aplicados, pero SIN el filtro de vista), porque los tabs muestran cuántos
    hay en cada bucket en total — independiente del que esté activo. Si los
    contás desde el `filas` del listado actual te queda 0 para los buckets
    no-activos (bug del 2026-04-29).
    """
    rows = (
        db.fetch_all(
            """
        SELECT
          CASE
            WHEN UPPER(COALESCE(c.tipo, '')) = 'K'
                 AND ABS(COALESCE(c.kg, 0)) > 0.01      THEN 'produccion'
            WHEN UPPER(COALESCE(c.tipo, '')) = 'A'      THEN 'compras'
            ELSE 'compras'
          END                                        AS bucket,
          COUNT(*)                                   AS n,
          COALESCE(SUM(c.importe), 0)                AS total
        FROM scintela.compra c
        WHERE (%(incluir_anuladas)s OR COALESCE(c.stat, '') <> 'Y')
          AND (%(desde)s::date IS NULL OR c.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR c.fecha <= %(hasta)s::date)
        GROUP BY 1
        """,
            {
                "desde": desde or None,
                "hasta": hasta or None,
                "incluir_anuladas": bool(incluir_anuladas),
            },
        )
        or []
    )
    # Devuelve {"compras": {"n": N, "total": $}, ...} + "todas" agregado.
    out: dict = {r["bucket"]: {"n": int(r["n"] or 0), "total": float(r["total"] or 0)} for r in rows}
    out["todas"] = {
        "n": sum(b["n"] for b in out.values()),
        "total": sum(b["total"] for b in out.values()),
    }
    # Asegurar que las claves siempre existen aunque no haya filas.
    for key in ("compras", "produccion", "anticipos"):
        out.setdefault(key, {"n": 0, "total": 0.0})
    return out


def deuda_proveedores() -> dict:
    """Suma de obligaciones abiertas con proveedores (= TOTP del balance).

    Modelo dBase legacy: las obligaciones de pago vienen de `scintela.posdat`.

    #46 (TMT 2026-05-14): el filtro correcto es `banc=0` (POSDAT_DEUDA_VIVA_WHERE),
    no `banc<>9`. Antes se incluían banc=10/32 (cheques emitidos modernos)
    que YA bajaron el saldo bancario — contarlos como deuda viva sería
    double-counting (mismo bug que el flujo del 2026-05-13, ver SKILL.md).
    Sólo banc=0 = deuda sin instrumentar. Excluye anuladas (migración 0027).

    Devuelve:
        total: importe total adeudado (≈ TOTP en INFORMES.PRG).
        n_partidas: cantidad de obligaciones abiertas.
        n_proveedores: cuántos proveedores distintos.
        vence_pronto: importe que vence en los próximos 7 días.
        vencido: importe ya vencido (fechad < hoy).
    """
    # POSDAT_DEUDA_VIVA_WHERE inlineado para no importarlo en runtime
    # (queries puras, sin side imports). Coincide con modules/posdat/__init__.py.
    row = db.fetch_one(
        """
        SELECT
            COALESCE(SUM(p.importe), 0)                                    AS total,
            COUNT(*)                                                       AS n_partidas,
            COUNT(DISTINCT p.prov)                                         AS n_proveedores,
            COALESCE(SUM(CASE WHEN p.fechad < CURRENT_DATE
                              THEN p.importe ELSE 0 END), 0)               AS vencido,
            COALESCE(SUM(CASE WHEN p.fechad >= CURRENT_DATE
                              AND p.fechad < CURRENT_DATE + INTERVAL '7 days'
                              THEN p.importe ELSE 0 END), 0)               AS vence_pronto
        FROM scintela.posdat p
        WHERE COALESCE(p.banc, 0) = 0
          AND (p.anulada IS NOT TRUE OR p.anulada IS NULL)
        """
    )
    if not row:
        return {"total": 0.0, "n_partidas": 0, "n_proveedores": 0, "vencido": 0.0, "vence_pronto": 0.0}
    return {
        "total": float(row["total"] or 0),
        "n_partidas": int(row["n_partidas"] or 0),
        "n_proveedores": int(row["n_proveedores"] or 0),
        "vencido": float(row["vencido"] or 0),
        "vence_pronto": float(row["vence_pronto"] or 0),
    }


def por_proveedor(codigo_prov: str, limite: int = 300) -> list[dict]:
    return db.fetch_all(
        """
        SELECT c.id_compra, c.fecha, c.fechad, c.tipo, c.comprobante,
               c.numero, c.kg, c.importe, c.concepto, c.no_banco,
               COALESCE(b.nombre, '') AS banco
        FROM scintela.compra c
        LEFT JOIN scintela.banco b ON b.no_banco = c.no_banco
        WHERE c.codigo_prov = %s
        ORDER BY c.fecha DESC
        LIMIT %s
        """,
        (codigo_prov, limite),
    )


def total_por_mes(anio: int | None = None) -> list[dict]:
    """Totales mensuales de compras (para ver el ritmo de compra)."""
    return db.fetch_all(
        """
        SELECT date_trunc('month', fecha)::date AS mes,
               SUM(importe) AS total,
               SUM(kg)      AS kg_total,
               COUNT(*)     AS n_compras
        FROM scintela.compra
        WHERE (%s::int IS NULL OR EXTRACT(YEAR FROM fecha)::int = %s::int)
        GROUP BY 1
        ORDER BY 1 DESC
        """,
        (anio, anio),
    )
