"""Capital — movimientos patrimoniales de los dueños.

scintela.capital: id_capital, fecha, doc(5), concepto(50), importe, invanual,
                  capital, util (utilidad), patri (patrimonio), clave
"""
from datetime import date

import db
from filters import today_ec
from periodo_guard import asegurar_fecha_abierta


def crear(
    *,
    fecha: date,
    doc: str,
    concepto: str,
    importe,
    invanual=None,
    capital=None,
    util=None,
    patri=None,
    clave: str | None = None,
    usuario: str = "web",
) -> dict:
    """Registrar un movimiento de capital.

    `doc` es el código corto (5 ch.) del tipo de asiento (APOR, RET, DIV, AJUS…).
    Los cuatro balances (invanual/capital/util/patri) son snapshots tras el
    movimiento — si no se envían, se arrastra el último conocido + importe en
    capital como aproximación razonable.
    """
    if not doc:
        raise ValueError("Doc requerido.")
    if not concepto:
        raise ValueError("Concepto requerido.")
    if importe is None:
        raise ValueError("Importe requerido.")
    asegurar_fecha_abierta(fecha)

    if capital is None and util is None and patri is None:
        anterior = estado_actual() or {}
        cap_prev = float(anterior.get("capital") or 0)
        capital = cap_prev + float(importe)
        util = anterior.get("util")
        patri = (capital or 0) + float(util or 0)

    return db.execute_returning(
        """
        INSERT INTO scintela.capital
            (fecha, doc, concepto, importe, invanual,
             capital, util, patri, clave, usuario_crea)
        VALUES (%s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s)
        RETURNING id_capital
        """,
        (
            fecha, doc[:5].upper(), concepto[:50], importe,
            invanual, capital, util, patri,
            (clave or None) and clave[:3],
            usuario,
        ),
    ) or {}


CUENTAS_APORTE = ("caja", "pichincha", "internacional")


def aportar(
    *,
    fecha: date,
    importe,
    cuenta: str,
    concepto: str = "",
    doc: str = "APOR",
    socio: str | None = None,
    clave: str | None = None,
    usuario: str = "web",
) -> dict:
    """Aporte de capital del dueño con bank/caja link automático.

    Operación atómica (TMT 2026-05-12 Fase Q):
      1. INSERT en scintela.capital (doc='APOR', importe positivo).
      2. INSERT side-effect según `cuenta`:
         - cuenta='caja'          → caja TIPO='E' (entrada).
         - cuenta='pichincha'     → tx_bancarias DOC='DE' banco Pichincha.
         - cuenta='internacional' → tx_bancarias DOC='DE' banco Internacional.

    El dueño puso plata en X cuenta → la cuenta sube, el capital sube.
    Devuelve dict con id_capital + id_transaccion/id_caja.
    """
    if importe is None or float(importe) <= 0:
        raise ValueError("Importe del aporte debe ser mayor que cero.")
    cuenta_norm = (cuenta or "").lower().strip()
    if cuenta_norm not in CUENTAS_APORTE:
        raise ValueError(
            f"Cuenta inválida: {cuenta!r}. Debe ser una de: {', '.join(CUENTAS_APORTE)}."
        )
    asegurar_fecha_abierta(fecha)

    importe_f = float(importe)
    concepto_full = (concepto or f"APORTE {socio or ''}").strip()[:50]

    with db.tx() as conn:
        # 1. Snapshot del capital actual para mantener el running.
        anterior = db.fetch_one(
            """
            SELECT capital, util, invanual
            FROM scintela.capital
            WHERE capital IS NOT NULL OR util IS NOT NULL
            ORDER BY fecha DESC, id_capital DESC
            LIMIT 1
            """,
            conn=conn,
        ) or {}
        cap_prev = float(anterior.get("capital") or 0)
        util_prev = anterior.get("util")
        capital_nuevo = cap_prev + importe_f
        patri_nuevo = capital_nuevo + float(util_prev or 0)

        # 2. INSERT capital.
        cap_row = db.execute_returning(
            """
            INSERT INTO scintela.capital
                (fecha, doc, concepto, importe, invanual,
                 capital, util, patri, clave, usuario_crea)
            VALUES (%s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
            RETURNING id_capital
            """,
            (
                fecha, doc[:5].upper(), concepto_full,
                importe_f, anterior.get("invanual"),
                capital_nuevo, util_prev, patri_nuevo,
                (clave or socio or usuario[:3])[:3].upper(),
                usuario[:50],
            ),
            conn=conn,
        ) or {}

        # 3. Side-effect: la plata entra a la cuenta indicada.
        side = {"tipo": cuenta_norm}
        if cuenta_norm == "caja":
            import caja_helpers
            res = caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha,
                tipo="E",
                importe=importe_f,
                concepto=f"APORTE {socio or ''} {concepto_full}".strip()[:50],
                usuario=usuario,
            )
            side["id_caja"] = res.get("id_caja")
        else:
            import bank_helpers
            # Resolver no_banco por nombre — Pichincha/Internacional pueden
            # tener IDs distintos según la data del usuario.
            needle = "PICHINC" if cuenta_norm == "pichincha" else "INTER"
            all_b = db.fetch_all(
                "SELECT no_banco, COALESCE(nombre,'') AS nombre "
                "FROM scintela.banco ORDER BY no_banco",
                conn=conn,
            ) or []
            match = next(
                (b for b in all_b if needle in (b.get("nombre") or "").upper()),
                None,
            )
            if not match:
                raise ValueError(
                    f"No encontré banco {cuenta_norm!r} en scintela.banco."
                )
            res = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=int(match["no_banco"]),
                no_cta=None,
                fecha=fecha,
                documento="DE",
                importe=importe_f,
                concepto=f"APORTE {socio or ''} {concepto_full}".strip()[:50],
                usuario=usuario,
            )
            side["no_banco"] = int(match["no_banco"])
            side["id_transaccion"] = res.get("id_transaccion")

        # Historial unificado.
        import mov_doble as _md
        destino_table = "caja" if cuenta_norm == "caja" else "transacciones_bancarias"
        destino_id = side.get("id_caja") or side.get("id_transaccion")
        id_md = _md.registrar(
            conn=conn,
            tipo=f"aporte_capital_a_{cuenta_norm}",
            origen_table="capital",
            origen_id=cap_row.get("id_capital"),
            destino_table=destino_table,
            destino_id=destino_id,
            importe=importe_f,
            fecha=fecha,
            concepto=concepto_full,
            usuario=usuario,
            metadata={"socio": socio, "doc": doc},
        )

    return {
        "id_capital": cap_row.get("id_capital"),
        "importe": importe_f,
        "capital_nuevo": capital_nuevo,
        "patri_nuevo": patri_nuevo,
        "side_effect": side,
        "id_mov_doble": id_md,
    }


def retirar(
    *,
    fecha: date,
    importe,
    cuenta: str,
    socio: str,
    concepto: str = "",
    clave: str | None = None,
    usuario: str = "web",
) -> dict:
    """Retiro de un socio con bank/caja link automático.

    Operación atómica (TMT 2026-05-12 Fase Q):
      1. INSERT en scintela.retiros (ret=importe, de=socio, nb=no_banco).
      2. INSERT side-effect: caja TIPO='S' o tx_bancarias DOC='CH'.

    Devuelve dict con id_retiro + id_transaccion/id_caja.

    Nota: a diferencia de `aportar()`, este NO toca `scintela.capital` —
    los retiros viven en tabla aparte (paridad legacy). Para que aparezcan
    en `movimientos_unificados`, ya se hace el UNION ALL en queries.
    """
    # TMT 2026-07-20 (duena): "tenemos que poder cargar aportes que no tengan
    # que ver con OP" — retiro en NEGATIVO = APORTE de capital del socio
    # (paridad dBase: RETIROS con importe negativo). La plata ENTRA a la
    # cuenta (caja E / banco DE) y URET baja -> utilidad quieta. Bloquea solo 0.
    if importe is None or float(importe) == 0:
        raise ValueError("Importe no puede ser cero (negativo = aporte).")
    socio = (socio or "").strip().upper()
    if not socio:
        raise ValueError("Socio requerido para retiro.")
    cuenta_norm = (cuenta or "").lower().strip()
    if cuenta_norm not in CUENTAS_APORTE:
        raise ValueError(
            f"Cuenta inválida: {cuenta!r}. Debe ser una de: {', '.join(CUENTAS_APORTE)}."
        )
    asegurar_fecha_abierta(fecha)

    importe_f = float(importe)
    es_aporte = importe_f < 0
    magnitud = abs(importe_f)
    concepto_full = (
        concepto or (f"APORTE {socio}" if es_aporte else f"RETIRO {socio}")
    ).strip()[:50]

    with db.tx() as conn:
        # Resolver no_banco para columna scintela.retiros.nb (NULL si caja).
        no_banco = None
        if cuenta_norm in ("pichincha", "internacional"):
            needle = "PICHINC" if cuenta_norm == "pichincha" else "INTER"
            all_b = db.fetch_all(
                "SELECT no_banco, COALESCE(nombre,'') AS nombre "
                "FROM scintela.banco ORDER BY no_banco",
                conn=conn,
            ) or []
            match = next(
                (b for b in all_b if needle in (b.get("nombre") or "").upper()),
                None,
            )
            if not match:
                raise ValueError(
                    f"No encontré banco {cuenta_norm!r} en scintela.banco."
                )
            no_banco = int(match["no_banco"])

        # 1. INSERT retiro
        ret_row = db.execute_returning(
            """
            INSERT INTO scintela.retiros
                (fecha, ret, de, nb, concepto, clave, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id_retiro
            """,
            (
                fecha, importe_f, socio[:5], no_banco,
                concepto_full, (clave or socio[:3])[:3],
                # TMT 2026-07-20: marcador pc-capital → el sync dBase lo
                # preserva (antes el TRUNCATE+INSERT lo borraba).
                f"pc-capital:{usuario}"[:50],
            ),
            conn=conn,
        ) or {}

        # 2. Side-effect: retiro = sale plata; APORTE (negativo) = ENTRA.
        side = {"tipo": cuenta_norm}
        if cuenta_norm == "caja":
            import caja_helpers
            res = caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha,
                tipo="E" if es_aporte else "S",
                importe=magnitud,
                concepto=f"RR {socio} {concepto_full}".strip()[:50],
                usuario=usuario,
            )
            side["id_caja"] = res.get("id_caja")
        else:
            import bank_helpers
            res = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=no_banco,
                no_cta=None,
                fecha=fecha,
                documento="DE" if es_aporte else "CH",
                importe=magnitud,
                concepto=f"RR {socio} {concepto_full}".strip()[:50],
                usuario=usuario,
            )
            side["no_banco"] = no_banco
            side["id_transaccion"] = res.get("id_transaccion")

        # Historial unificado.
        import mov_doble as _md
        destino_table = "caja" if cuenta_norm == "caja" else "transacciones_bancarias"
        destino_id = side.get("id_caja") or side.get("id_transaccion")
        id_md = _md.registrar(
            conn=conn,
            tipo=f"retiro_socio_de_{cuenta_norm}",
            origen_table="retiros",
            origen_id=ret_row.get("id_retiro"),
            destino_table=destino_table,
            destino_id=destino_id,
            importe=importe_f,
            fecha=fecha,
            concepto=concepto_full,
            usuario=usuario,
            metadata={"socio": socio},
        )

    return {
        "id_retiro": ret_row.get("id_retiro"),
        "importe": importe_f,
        "socio": socio,
        "side_effect": side,
        "id_mov_doble": id_md,
    }


def reversar_aporte(
    *,
    id_capital: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversa un aporte de capital previamente registrado.

    Toma el id_capital, busca su mov_doble linkeado (`aporte_capital_a_*`),
    y compensa atómicamente:
      - INSERT en scintela.capital con `importe = -original` (rollback del
        snapshot de patrimonio).
      - Side effect inverso:
        - cuenta='caja' (E+) → INSERT caja tipo='S' (-).
        - cuenta='pichincha'/'internacional' (DE+) → INSERT documento='CH' (-).
      - Marca mov_doble original como 'reversado', INSERT mov_doble reverso
        linkeado con `id_original`.

    Regla de signos: cada operación de alta tenía signo +, su reverso es −.
    TMT 2026-05-13.
    """
    import bank_helpers
    import caja_helpers
    import mov_doble as _md

    motivo = (motivo or "").strip()
    fecha_rev = today_ec()
    asegurar_fecha_abierta(fecha_rev)

    cap_orig = db.fetch_one(
        """
        SELECT id_capital, fecha, doc, concepto, importe, capital, util, patri
          FROM scintela.capital
         WHERE id_capital = %s
        """,
        (id_capital,),
    )
    if not cap_orig:
        raise ValueError(f"Aporte de capital id={id_capital} no existe.")
    importe_f = float(cap_orig.get("importe") or 0)
    if importe_f <= 0:
        raise ValueError(
            f"Aporte id={id_capital} no tiene importe positivo "
            f"(importe={importe_f}) — quizá ya fue reversado."
        )

    # Buscar el mov_doble original — tipo 'aporte_capital_a_*'.
    md_orig = db.fetch_one(
        """
        SELECT id_mov_doble, tipo, destino_table, destino_id, importe
          FROM scintela.mov_doble
         WHERE origen_table = 'capital' AND origen_id = %s
           AND tipo LIKE 'aporte_capital_%%'
           AND estado = 'activo'
         ORDER BY id_mov_doble DESC LIMIT 1
        """,
        (id_capital,),
    )
    if not md_orig:
        raise ValueError(
            f"No encuentro mov_doble activo de aporte para id_capital={id_capital}. "
            "Quizás ya fue reversado o es un aporte legacy sin historial."
        )
    cuenta = (md_orig.get("tipo") or "").replace("aporte_capital_a_", "")

    with db.tx() as conn:
        # 1) Snapshot patrimonio actual + restar el aporte original.
        anterior = db.fetch_one(
            """
            SELECT capital, util, invanual FROM scintela.capital
             WHERE capital IS NOT NULL OR util IS NOT NULL
             ORDER BY fecha DESC, id_capital DESC LIMIT 1
            """,
            conn=conn,
        ) or {}
        cap_prev = float(anterior.get("capital") or 0)
        util_prev = anterior.get("util")
        capital_nuevo = cap_prev - importe_f
        patri_nuevo = capital_nuevo + float(util_prev or 0)
        cap_rev = db.execute_returning(
            """
            INSERT INTO scintela.capital
                (fecha, doc, concepto, importe, invanual,
                 capital, util, patri, clave, usuario_crea)
            VALUES (%s, 'REV', %s, %s, %s,
                    %s, %s, %s, %s, %s)
            RETURNING id_capital
            """,
            (
                fecha_rev,
                (f"REVERSO aporte id={id_capital}"
                 + (f" — {motivo}" if motivo else ""))[:50],
                -importe_f,                              # NEGATIVO compensa
                anterior.get("invanual"),
                capital_nuevo, util_prev, patri_nuevo,
                (usuario[:3] or "REV").upper(),
                usuario[:50],
            ),
            conn=conn,
        ) or {}

        # 2) Side-effect inverso según cuenta.
        side_rev_id = None
        if cuenta == "caja":
            res = caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha_rev,
                tipo="S",   # ANTES era 'E' → ahora compensación 'S'
                importe=importe_f,
                concepto=(f"REVERSO aporte cap#{id_capital}"
                          + (f" — {motivo}" if motivo else ""))[:50],
                clave="REV",
                usuario=usuario,
            )
            side_rev_id = res.get("id_caja")
        elif cuenta in ("pichincha", "internacional"):
            # Buscar no_banco
            needle = "PICHINC" if cuenta == "pichincha" else "INTER"
            all_b = db.fetch_all(
                "SELECT no_banco, COALESCE(nombre,'') AS nombre "
                "FROM scintela.banco ORDER BY no_banco",
                conn=conn,
            ) or []
            match = next(
                (b for b in all_b if needle in (b.get("nombre") or "").upper()),
                None,
            )
            if not match:
                raise ValueError(f"No encontré banco {cuenta!r}.")
            res = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=int(match["no_banco"]),
                no_cta=None,
                fecha=fecha_rev,
                documento="CH",   # ANTES era 'DE' → ahora compensación 'CH'
                importe=importe_f,
                concepto=(f"REVERSO aporte cap#{id_capital}"
                          + (f" — {motivo}" if motivo else ""))[:50],
                usuario=usuario,
            )
            side_rev_id = res.get("id_transaccion")

        # 3) Registrar reverso en mov_doble linkeado al original.
        destino_table = "caja" if cuenta == "caja" else "transacciones_bancarias"
        _md.registrar(
            conn=conn,
            tipo=f"reverso_aporte_capital_{cuenta}",
            origen_table="capital",
            origen_id=cap_rev.get("id_capital"),
            destino_table=destino_table,
            destino_id=side_rev_id,
            importe=importe_f,
            fecha=fecha_rev,
            concepto=(f"REVERSO aporte id={id_capital}"
                      + (f" — {motivo}" if motivo else ""))[:200],
            usuario=usuario,
            metadata={"id_capital_original": id_capital,
                      "id_capital_compensacion": cap_rev.get("id_capital"),
                      "cuenta": cuenta,
                      "motivo": motivo or ""},
            id_original=md_orig["id_mov_doble"],
        )

    return {
        "id_capital_original":     id_capital,
        "id_capital_compensacion": cap_rev.get("id_capital"),
        "cuenta":                  cuenta,
        "side_effect_reversado":   side_rev_id,
        "importe":                 importe_f,
    }


def reversar_retiro(
    *,
    id_retiro: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversa un retiro de socio previamente registrado.

    Compensación:
      - INSERT en scintela.retiros con `ret = -importe_original` (compensación
        contable, mantiene audit trail).
      - Side effect inverso:
        - cuenta='caja' (S−) → INSERT caja tipo='E' (+).
        - cuenta='pichincha'/'internacional' (CH−) → documento='NC' (+).
      - Marca mov_doble original como 'reversado', INSERT reverso linkeado.

    TMT 2026-05-13.
    """
    import bank_helpers
    import caja_helpers
    import mov_doble as _md

    motivo = (motivo or "").strip()
    fecha_rev = today_ec()
    asegurar_fecha_abierta(fecha_rev)

    ret_orig = db.fetch_one(
        """
        SELECT id_retiro, fecha, ret, de, nb, concepto, clave,
               COALESCE(usuario_crea, '') AS usuario_crea
          FROM scintela.retiros
         WHERE id_retiro = %s
        """,
        (id_retiro,),
    )
    if not ret_orig:
        raise ValueError(f"Retiro id={id_retiro} no existe.")
    _conc_u = (ret_orig.get("concepto") or "").upper()
    if (ret_orig.get("clave") or "").strip().upper() == "REV" or \
            _conc_u.startswith(("REVERSO", "ANULACION")):
        raise ValueError(
            f"Retiro id={id_retiro} es una compensación/reverso — no se reversa."
        )
    importe_f = float(ret_orig.get("ret") or 0)
    # TMT 2026-07-20: negativo = APORTE (tambien reversable). Solo 0 no tiene
    # nada que reversar; las filas de compensacion no tienen mov_doble
    # retiro_socio_* activo, asi que el lookup de abajo las rechaza solo.
    if importe_f == 0:
        raise ValueError(f"Retiro id={id_retiro} tiene importe 0 — nada que reversar.")
    magnitud = abs(importe_f)
    socio = (ret_orig.get("de") or "").strip()

    md_orig = db.fetch_one(
        """
        SELECT id_mov_doble, tipo, destino_table, destino_id, importe
          FROM scintela.mov_doble
         WHERE origen_table = 'retiros' AND origen_id = %s
           AND tipo LIKE 'retiro_socio_%%'
           AND estado = 'activo'
         ORDER BY id_mov_doble DESC LIMIT 1
        """,
        (id_retiro,),
    )
    # TMT 2026-07-20 (dueña: cancelar "RR DEP AMAZONAS"): un retiro/aporte que
    # vino del dBase NO tiene mov_doble ni pata de banco/caja en el programa
    # (esa pata vive en el dBase) → se anula con una fila COMPENSATORIA sola
    # (ret = -ret, concepto ANULACION), auditada en mov_doble. El sync no la
    # pisa (marcador pc-capital + guard en import_dbf).
    if not md_orig:
        if (ret_orig.get("usuario_crea") or "") != "dbf-import":
            raise ValueError(
                f"Retiro id={id_retiro} no tiene rastro activo en el programa "
                "(¿ya fue reversado?)."
            )
        _pref = f"ANULACION retiro dBase id={id_retiro}"
        _ya = db.fetch_one(
            "SELECT 1 AS ok FROM scintela.retiros "
            " WHERE concepto = %s OR concepto LIKE %s",
            (_pref, _pref + " %"),
        )
        if _ya:
            raise ValueError(f"Retiro id={id_retiro} ya tiene una ANULACION.")
        with db.tx() as conn:
            ret_rev = db.execute_returning(
                """
                INSERT INTO scintela.retiros
                    (fecha, ret, de, nb, concepto, clave, usuario_crea)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id_retiro
                """,
                (
                    ret_orig.get("fecha") or fecha_rev, -importe_f,
                    (ret_orig.get("de") or "")[:5] or None, None,
                    (f"ANULACION retiro dBase id={id_retiro}"
                     + (f" — {motivo}" if motivo else ""))[:50],
                    "REV", f"pc-capital:{usuario}"[:50],
                ),
                conn=conn,
            ) or {}
            _md.registrar(
                conn=conn,
                tipo="reverso_retiro_dbase",
                origen_table="retiros",
                origen_id=ret_rev.get("id_retiro"),
                destino_table="retiros",
                destino_id=id_retiro,
                importe=importe_f,
                fecha=fecha_rev,
                concepto=(f"ANULACION retiro dBase id={id_retiro} "
                          f"$ {importe_f:,.2f}"
                          + (f" — {motivo}" if motivo else ""))[:200],
                usuario=usuario,
                metadata={"id_retiro_original": id_retiro,
                          "id_retiro_compensacion": ret_rev.get("id_retiro"),
                          "origen": "dbase", "motivo": motivo or ""},
            )
        return {
            "id_retiro_original": id_retiro,
            "id_retiro_compensacion": ret_rev.get("id_retiro"),
            "socio": (ret_orig.get("de") or "").strip(),
            "cuenta": "dbase (sin pata en el programa)",
            "importe": importe_f,
        }
    cuenta = (md_orig.get("tipo") or "").replace("retiro_socio_de_", "")

    with db.tx() as conn:
        # 1) INSERT retiro NEGATIVO (compensación contable).
        ret_rev = db.execute_returning(
            """
            INSERT INTO scintela.retiros
                (fecha, ret, de, nb, concepto, clave, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id_retiro
            """,
            (
                fecha_rev, -importe_f, socio[:5], None,  # nb NULL en compensación
                (f"REVERSO retiro id={id_retiro}"
                 + (f" — {motivo}" if motivo else ""))[:50],
                "REV", usuario[:50],
            ),
            conn=conn,
        ) or {}

        # 2) Side-effect inverso.
        side_rev_id = None
        if cuenta == "caja":
            res = caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha_rev,
                # Retiro (salio plata) -> vuelve a ENTRAR; aporte (entro) -> SALE.
                tipo="E" if importe_f > 0 else "S",
                importe=magnitud,
                concepto=(f"REVERSO retiro id={id_retiro}"
                          + (f" — {motivo}" if motivo else ""))[:50],
                clave="REV",
                usuario=usuario,
            )
            side_rev_id = res.get("id_caja")
        elif cuenta in ("pichincha", "internacional"):
            no_banco = ret_orig.get("nb")
            if not no_banco:
                # Fallback resolución por nombre
                needle = "PICHINC" if cuenta == "pichincha" else "INTER"
                all_b = db.fetch_all(
                    "SELECT no_banco, COALESCE(nombre,'') AS nombre "
                    "FROM scintela.banco ORDER BY no_banco",
                    conn=conn,
                ) or []
                match = next(
                    (b for b in all_b if needle in (b.get("nombre") or "").upper()),
                    None,
                )
                if not match:
                    raise ValueError(f"No encontré banco {cuenta!r}.")
                no_banco = int(match["no_banco"])
            res = bank_helpers.insert_movimiento_bancario(
                conn,
                no_banco=int(no_banco),
                no_cta=None,
                fecha=fecha_rev,
                # NC compensa el CH del retiro; ND compensa el DE del aporte.
                documento="NC" if importe_f > 0 else "ND",
                importe=magnitud,
                concepto=(f"REVERSO retiro id={id_retiro}"
                          + (f" — {motivo}" if motivo else ""))[:50],
                usuario=usuario,
            )
            side_rev_id = res.get("id_transaccion")

        # 3) Registrar reverso en mov_doble.
        destino_table = "caja" if cuenta == "caja" else "transacciones_bancarias"
        _md.registrar(
            conn=conn,
            tipo=f"reverso_retiro_socio_{cuenta}",
            origen_table="retiros",
            origen_id=ret_rev.get("id_retiro"),
            destino_table=destino_table,
            destino_id=side_rev_id,
            importe=importe_f,
            fecha=fecha_rev,
            concepto=(f"REVERSO retiro socio {socio} id={id_retiro}"
                      + (f" — {motivo}" if motivo else ""))[:200],
            usuario=usuario,
            metadata={"id_retiro_original":     id_retiro,
                      "id_retiro_compensacion": ret_rev.get("id_retiro"),
                      "socio": socio,
                      "cuenta": cuenta,
                      "motivo": motivo or ""},
            id_original=md_orig["id_mov_doble"],
        )

    return {
        "id_retiro_original":     id_retiro,
        "id_retiro_compensacion": ret_rev.get("id_retiro"),
        "socio":                  socio,
        "cuenta":                 cuenta,
        "side_effect_reversado":  side_rev_id,
        "importe":                importe_f,
    }


def movimientos(
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
) -> list[dict]:
    return db.fetch_all(
        """
        SELECT id_capital, fecha, doc, concepto, importe,
               invanual, capital, util, patri, clave
        FROM scintela.capital
        WHERE (%(desde)s::date IS NULL OR fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR fecha <= %(hasta)s::date)
        ORDER BY fecha DESC, id_capital DESC
        LIMIT %(limite)s
        """,
        {"desde": desde or None, "hasta": hasta or None, "limite": limite},
    )


def movimientos_unificados(
    filtro: str = "todos",
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
) -> list[dict]:
    """Timeline unificado de movimientos del dueño: capital + retiros.

    Combina:
      - scintela.capital  → tipo 'aporte' (importe del asiento)
      - scintela.retiros  → tipo 'retiro' (con persona y banco origen)

    `filtro`: 'todos' | 'aportes' | 'retiros'.
    Devuelve filas con shape común para que el template las renderee uniforme.
    """
    filtro = (filtro or "todos").lower().strip()
    if filtro not in ("todos", "aportes", "retiros"):
        filtro = "todos"

    rows: list[dict] = []
    if filtro in ("todos", "aportes"):
        cap = db.fetch_all(
            """
            SELECT 'aporte'         AS tipo,
                   id_capital       AS id,
                   fecha,
                   doc,
                   concepto,
                   importe,
                   NULL::varchar    AS persona,
                   NULL::int        AS no_banco,
                   NULL::text       AS banco,
                   capital, util, patri,
                   clave
            FROM scintela.capital
            WHERE (%(desde)s::date IS NULL OR fecha >= %(desde)s::date)
              AND (%(hasta)s::date IS NULL OR fecha <= %(hasta)s::date)
            """,
            {"desde": desde or None, "hasta": hasta or None},
        ) or []
        rows.extend(cap)

    if filtro in ("todos", "retiros"):
        ret = db.fetch_all(
            """
            SELECT 'retiro'           AS tipo,
                   r.id_retiro        AS id,
                   r.fecha,
                   NULL::varchar      AS doc,
                   COALESCE(r.concepto, '') AS concepto,
                   r.ret              AS importe,
                   r.de               AS persona,
                   r.nb               AS no_banco,
                   COALESCE(b.nombre, '') AS banco,
                   NULL::numeric      AS capital,
                   NULL::numeric      AS util,
                   NULL::numeric      AS patri,
                   r.clave
            FROM scintela.retiros r
            LEFT JOIN scintela.banco b ON b.no_banco = r.nb
            WHERE (%(desde)s::date IS NULL OR r.fecha >= %(desde)s::date)
              AND (%(hasta)s::date IS NULL OR r.fecha <= %(hasta)s::date)
            """,
            {"desde": desde or None, "hasta": hasta or None},
        ) or []
        rows.extend(ret)

    # Orden cronológico real (TMT 2026-05-12): scintela.capital y .retiros
    # tienen secuencias de id independientes — comparar id_capital vs id_retiro
    # da resultado arbitrario cuando hay empate de fecha. Usamos
    # mov_doble.fecha_creacion (TIMESTAMPTZ) como tie-breaker para los movs
    # nuevos. Los viejos (sin mov_doble) caen a fecha+id local.
    ids_aportes = [int(r["id"]) for r in rows if r.get("tipo") == "aporte" and r.get("id")]
    ids_retiros = [int(r["id"]) for r in rows if r.get("tipo") == "retiro" and r.get("id")]
    md_ts: dict[tuple[str, int], object] = {}
    try:
        if ids_aportes:
            for md in (db.fetch_all(
                "SELECT origen_table, origen_id, fecha_creacion FROM scintela.mov_doble "
                "WHERE origen_table='capital' AND origen_id = ANY(%s)",
                (ids_aportes,),
            ) or []):
                md_ts[(md["origen_table"], md["origen_id"])] = md["fecha_creacion"]
        if ids_retiros:
            for md in (db.fetch_all(
                "SELECT origen_table, origen_id, fecha_creacion FROM scintela.mov_doble "
                "WHERE origen_table='retiros' AND origen_id = ANY(%s)",
                (ids_retiros,),
            ) or []):
                md_ts[(md["origen_table"], md["origen_id"])] = md["fecha_creacion"]
    except Exception:
        # mov_doble puede no existir aún (migración 0023 pendiente) — fallback
        # silencioso al orden por fecha + id local.
        md_ts = {}

    def _sort_key(r):
        tipo = r.get("tipo") or ""
        origen = "capital" if tipo == "aporte" else "retiros"
        rid = int(r["id"]) if r.get("id") else 0
        ts = md_ts.get((origen, rid))
        ts_num = ts.timestamp() if ts else 0
        # Bug TMT 2026-05-12: el fallback string "" rompía sort cuando otras
        # filas tenían date object. Usamos date.min para mantener el tipo.
        return (r.get("fecha") or date.min, ts_num, rid)

    # Saldo acumulado running: aportes suman, retiros restan.
    # Refleja el balance neto patrimonial puesto por el dueño hasta la fecha.
    rows.sort(key=_sort_key)
    acumulado = 0.0
    for r in rows:
        importe = float(r.get("importe") or 0)
        if (r.get("tipo") or "") == "aporte":
            acumulado += importe
        elif (r.get("tipo") or "") == "retiro":
            acumulado -= importe
        r["saldo_acumulado"] = acumulado

    # Orden descendente para mostrar (más reciente primero).
    rows.reverse()
    return rows[:limite]


def conteos_unificados(
    desde: str | None = None,
    hasta: str | None = None,
) -> dict:
    """Conteos para los tabs de la pantalla unificada."""
    cap = db.fetch_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
        FROM scintela.capital
        WHERE (%(desde)s::date IS NULL OR fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR fecha <= %(hasta)s::date)
        """,
        {"desde": desde or None, "hasta": hasta or None},
    ) or {}
    ret = db.fetch_one(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(ret), 0) AS total
        FROM scintela.retiros
        WHERE (%(desde)s::date IS NULL OR fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR fecha <= %(hasta)s::date)
        """,
        {"desde": desde or None, "hasta": hasta or None},
    ) or {}
    return {
        "aportes": {"n": int(cap.get("n") or 0), "total": float(cap.get("total") or 0)},
        "retiros": {"n": int(ret.get("n") or 0), "total": float(ret.get("total") or 0)},
        "todos":   {
            "n": int(cap.get("n") or 0) + int(ret.get("n") or 0),
            "total": float(cap.get("total") or 0) + float(ret.get("total") or 0),
        },
    }


def estado_actual() -> dict | None:
    """Última línea → valores vigentes de capital / utilidad / patrimonio."""
    return db.fetch_one(
        """
        SELECT fecha, capital, util, patri, invanual
        FROM scintela.capital
        WHERE capital IS NOT NULL OR util IS NOT NULL OR patri IS NOT NULL
        ORDER BY fecha DESC, id_capital DESC
        LIMIT 1
        """,
    )
