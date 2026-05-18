"""Consultas de posdat (deuda viva con proveedores)."""
from datetime import date

import db
from periodo_guard import asegurar_fecha_abierta

# Filtro reutilizable de "no anuladas": toda query de listado/balance/saldo
# tiene que excluir las filas soft-deleted (migración 0027). NULL = legacy
# pre-migración, equivalente a FALSE.
POSDAT_NO_ANULADA_WHERE = "(anulada IS NOT TRUE OR anulada IS NULL)"


def por_id(id_posdat: int) -> dict | None:
    # NOTA: NO filtramos `anulada` acá — `por_id` se usa también desde la
    # vista de confirmar_anulacion y desde scripts de auditoría que pueden
    # querer ver una fila anulada. Los listados/balances sí filtran.
    return db.fetch_one(
        """
        SELECT pd.id_posdat, pd.num, pd.fecha, pd.fechad, pd.prov, pd.importe,
               pd.banc, pd.concepto, pd.clave,
               pd.anulada, pd.motivo_anulacion, pd.fecha_anulacion,
               COALESCE(p.nombre, '') AS proveedor
        FROM scintela.posdat pd
        LEFT JOIN scintela.proveedor p ON p.codigo_prov = pd.prov
        WHERE pd.id_posdat = %s
        """,
        (id_posdat,),
    )


def proximo_num() -> int:
    row = db.fetch_one("SELECT COALESCE(MAX(num), 0) + 1 AS n FROM scintela.posdat")
    return int(row["n"]) if row else 1


def crear(
    *,
    fecha: date,
    fechad: date | None,
    prov: str,
    importe,
    concepto: str,
    tipo: str | None = None,       # backward-compat: ignorado (no existe en schema)
    compr: str | None = None,      # idem
    no_comp: str | None = None,    # idem
    num: int | None = None,
    banc: int = 0,
    clave: str | None = None,
    usuario: str = "web",
) -> dict:
    """Alta manual de pasivo (banc=0 abierto).

    NOTA: el schema real de scintela.posdat NO tiene compr/no_comp/tipo
    (eran tablas auxiliares en el legacy dBase). Los args se aceptan por
    compatibilidad pero se ignoran (se concatenan al concepto si vienen).
    """
    asegurar_fecha_abierta(fecha)
    prov = (prov or "").upper().strip()
    if not prov:
        raise ValueError("Proveedor requerido.")
    if importe is None or float(importe) <= 0:
        raise ValueError("Importe debe ser mayor que cero.")
    if not concepto:
        raise ValueError("Concepto requerido.")
    if not db.fetch_one(
        "SELECT 1 AS x FROM scintela.proveedor WHERE codigo_prov = %s", (prov,)
    ):
        raise ValueError(f"Proveedor {prov!r} no existe.")
    if num is None:
        num = proximo_num()
    if fechad is None:
        fechad = fecha

    # Backward compat: si vinieron compr/no_comp/tipo, los apendeamos al concepto.
    extras = " ".join(
        x for x in [
            f"[{tipo}]" if tipo else None,
            compr or None,
            no_comp or None,
        ] if x
    )
    concepto_full = (f"{concepto} {extras}".strip() if extras else concepto)[:100]

    return db.execute_returning(
        """
        INSERT INTO scintela.posdat
            (num, fecha, fechad, prov, importe, banc, concepto, clave, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id_posdat, num
        """,
        (
            num, fecha, fechad, prov, importe, banc,
            concepto_full,
            (clave or None) and clave[:3],
            usuario,
        ),
    ) or {}


def editar(
    id_posdat: int,
    *,
    fechad: date | None = None,
    importe=None,
    concepto: str | None = None,
    compr: str | None = None,      # backward-compat: ignorado
    no_comp: str | None = None,    # idem
    tipo: str | None = None,       # idem
    usuario: str = "web",
) -> int:
    """Edición limitada: sólo campos que NO afectan la contrapartida.

    No se permite cambiar banc, prov o num por aquí — para eso existen
    marcar_pagada / reabrir y la anulación explícita.

    NOTA: compr/no_comp/tipo no existen en el schema real. Si vienen, se
    apendean al concepto (backward compat).

    Audit (TMT 2026-05-14, #23): si el `importe` cambia, registra un
    mov_doble tipo='posdat_edit_importe' con el delta + appenda
    `[ED imp_prev:X nuevo:Y]` al concepto. Sin esto, la edición del
    importe queda sin huella en /historial.
    """
    # Leer el estado actual ANTES de armar el UPDATE para poder auditar
    # los cambios de importe y construir el concepto con la marca.
    actual = db.fetch_one(
        "SELECT id_posdat, importe, fecha, concepto, prov, num "
        "FROM scintela.posdat WHERE id_posdat = %s",
        (id_posdat,),
    )
    if not actual:
        raise ValueError(f"Posdat id={id_posdat} no existe.")

    importe_prev = float(actual.get("importe") or 0)
    importe_nuevo = float(importe) if importe is not None else importe_prev
    importe_cambio = (importe is not None
                      and abs(importe_nuevo - importe_prev) > 0.01)

    campos = []
    params: list = []

    # Mergear extras al concepto si vinieron
    extras_parts = [
        f"[{tipo}]" if tipo else None,
        compr or None,
        no_comp or None,
    ]
    # Si cambia el importe, agregamos marca de auditoría al concepto
    # (visible en lista/csv) — sin desplazar la lógica del mov_doble.
    if importe_cambio:
        extras_parts.append(
            f"[ED imp_prev:{importe_prev:.2f} nuevo:{importe_nuevo:.2f}]"
        )
    extras = " ".join(x for x in extras_parts if x)

    if concepto is not None or extras:
        concepto_full = ((concepto or "") + (" " + extras if extras else "")).strip()
        if concepto_full:
            campos.append("concepto = %s")
            params.append(concepto_full[:100])

    if fechad is not None:
        campos.append("fechad = %s")
        params.append(fechad)
    if importe is not None:
        campos.append("importe = %s")
        params.append(importe_nuevo)
    if not campos:
        return 0
    campos.append("usuario_modifica = %s")
    params.append(usuario[:50])
    params.append(id_posdat)

    with db.tx() as conn:
        rc = db.execute(
            f"UPDATE scintela.posdat SET {', '.join(campos)} WHERE id_posdat = %s",
            tuple(params),
            conn=conn,
        )
        # Audit del cambio de importe (TMT 2026-05-14, #23).
        if importe_cambio:
            try:
                import mov_doble as _md
                _md.registrar(
                    conn=conn,
                    tipo="posdat_edit_importe",
                    origen_table="posdat",
                    origen_id=id_posdat,
                    destino_table="posdat",
                    destino_id=id_posdat,
                    importe=round(importe_nuevo - importe_prev, 2),
                    fecha=actual.get("fecha") or date.today(),
                    concepto=(
                        f"Edit importe posdat #{actual.get('num') or id_posdat} "
                        f"{importe_prev:.2f} → {importe_nuevo:.2f}"
                    )[:200],
                    usuario=usuario,
                    metadata={"importe_prev": importe_prev,
                              "importe_nuevo": importe_nuevo,
                              "prov": actual.get("prov"),
                              "num": actual.get("num")},
                )
            except Exception:
                # Si mov_doble falla por algo INESPERADO (no tabla missing —
                # eso ya está manejado), preferimos abortar la edición — el
                # importe no debería cambiar sin huella. Esto burbuja a la vista.
                raise
    return rc


def marcar_pagada(id_posdat: int, *, no_banco: int = 9, usuario: str = "web") -> int:
    """DEPRECADO (TMT 2026-05-14, #4) — usar /bancos/emitir-cheque?id_posdat=X.

    Originalmente esta función seteaba `banc=no_banco` sin generar el
    movimiento bancario ni el mov_doble correspondiente — la posdat
    quedaba "pagada" pero el saldo del banco no bajaba y /historial no
    veía nada.

    La vista `posdat.marcar_pagada` ahora redirige a
    `/bancos/emitir-cheque?id_posdat=X` (wizard atómico que hace bien
    todo: INSERT en transacciones_bancarias + UPDATE posdat.banc + mov_doble).

    Si algún script o tarea automática llama a esta función directo,
    levantamos ValueError para forzar migrarse al flujo correcto.
    """
    raise ValueError(
        "posdat.queries.marcar_pagada() está deprecada — usar "
        "bancos.emitir_cheque(tipo='proveedor', id_posdat=...) para "
        "emitir el cheque + cerrar la posdat atómicamente."
    )


def reabrir(id_posdat: int, usuario: str = "web") -> int:
    return db.execute(
        "UPDATE scintela.posdat SET banc=0, usuario_modifica=%s WHERE id_posdat=%s",
        (usuario, id_posdat),
    )


def anular(id_posdat: int, *, motivo: str = "", usuario: str = "web") -> int:
    """Soft-delete con trazabilidad (TMT 2026-05-14, #3).

    Reemplaza el `DELETE FROM scintela.posdat` original. Reglas:

      - Bloquea si `banc <> 0` (la posdat ya fue pagada con cheque o
        banco): hay que reversar el cheque emitido primero, sino la
        partida bancaria queda colgada.
      - Marca anulada=TRUE + motivo + fecha_anulacion en lugar de borrar.
      - Registra mov_doble tipo='posdat_anulada' linkeado al original
        para que aparezca en /historial.
      - Todo atómico.

    `motivo` se valida en la vista (longitud mínima). Acá sólo se persiste.
    """
    pd = db.fetch_one(
        "SELECT id_posdat, num, prov, importe, banc, fecha, anulada "
        "FROM scintela.posdat WHERE id_posdat = %s",
        (id_posdat,),
    )
    if not pd:
        raise ValueError(f"Posdat id={id_posdat} no existe.")
    if pd.get("anulada") is True:
        raise ValueError("La posdat ya está anulada.")
    banc = int(pd.get("banc") or 0)
    if banc != 0:
        raise ValueError(
            f"Posdat ya pagada con cheque (banc={banc}). Reversá el "
            f"cheque emitido primero desde /bancos o /cheques antes de "
            f"anular la posdat."
        )

    with db.tx() as conn:
        rc = db.execute(
            """
            UPDATE scintela.posdat
               SET anulada = TRUE,
                   motivo_anulacion = %s,
                   fecha_anulacion = CURRENT_TIMESTAMP,
                   usuario_modifica = %s
             WHERE id_posdat = %s
            """,
            (motivo[:200] if motivo else None, usuario[:50], id_posdat),
            conn=conn,
        )
        # Registrar mov_doble del reverso linkeado al original (si existe).
        try:
            import mov_doble as _md
            md_orig = db.fetch_one(
                """
                SELECT id_mov_doble, importe FROM scintela.mov_doble
                 WHERE destino_table = 'posdat'
                   AND destino_id    = %s
                   AND estado        = 'activo'
                 ORDER BY id_mov_doble DESC LIMIT 1
                """,
                (id_posdat,), conn=conn,
            )
            _md.registrar(
                conn=conn,
                tipo="posdat_anulada",
                origen_table="posdat",
                origen_id=id_posdat,
                destino_table="posdat",
                destino_id=id_posdat,
                importe=float(pd.get("importe") or 0),
                fecha=pd.get("fecha") or date.today(),
                concepto=(
                    f"ANULACION posdat #{pd.get('num') or id_posdat} "
                    f"{pd.get('prov') or ''}"
                    + (f" — {motivo}" if motivo else "")
                )[:200],
                usuario=usuario,
                metadata={"motivo": motivo or "",
                          "id_posdat": id_posdat,
                          "num": pd.get("num"),
                          "prov": pd.get("prov")},
                id_original=(md_orig or {}).get("id_mov_doble"),
            )
        except Exception:
            # Si mov_doble explota por algo inesperado, dejamos burbujar para
            # que el caller vea la falla — la anulación necesita historial.
            raise
    return rc


def buscar(
    *,
    prov: str | None = None,
    q: str = "",
    solo_abiertas: bool = True,
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # #18 (TMT 2026-05-14): "solo_abiertas" = deuda viva sin instrumentar.
    # Antes filtraba banc<>9, pero eso incluía banc=10/32 (cheques emitidos
    # modernos PC) — esos NO son deuda abierta, ya fueron pagados desde el
    # banco. La definición correcta es banc=0 (POSDAT_DEUDA_VIVA_WHERE).
    rows = db.fetch_all(
        """
        SELECT pd.id_posdat, pd.num, pd.fecha, pd.fechad, pd.prov, pd.importe,
               pd.banc, pd.concepto, pd.clave,
               COALESCE(p.nombre, '') AS proveedor
        FROM scintela.posdat pd
        LEFT JOIN scintela.proveedor p ON p.codigo_prov = pd.prov
        WHERE (%(prov)s IS NULL OR UPPER(pd.prov) = UPPER(%(prov)s))
          AND (%(q)s IS NULL
               OR UPPER(COALESCE(pd.concepto,'')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(p.nombre,''))    LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(pd.prov,''))     LIKE UPPER(%(like)s))
          AND (NOT %(solo_abiertas)s OR COALESCE(pd.banc,0) = 0)
          AND (%(desde)s::date IS NULL OR pd.fechad >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR pd.fechad <= %(hasta)s::date)
          -- Filtro de soft-delete (migración 0027): siempre excluye anuladas.
          AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
        ORDER BY pd.fechad, pd.id_posdat
        LIMIT %(limite)s
        """,
        {
            "prov": prov or None,
            "q": q or None, "like": like,
            "solo_abiertas": solo_abiertas,
            "desde": desde or None, "hasta": hasta or None,
            "limite": limite,
        },
    ) or []

    # Saldo acumulado = deuda corrida hasta la fecha de vencimiento. Útil
    # para planificar flujo: "al 15 de junio ya vencieron $ X de posdatados".
    # (TMT 2026-05-12)
    acum = 0.0
    for r in rows:
        if (r.get("banc") or 0) != 9:  # banc=9 ya pagada
            acum += float(r.get("importe") or 0)
        r["saldo_acumulado"] = acum
    return rows


def resumen(prov: str | None = None) -> dict:
    """Total de deuda abierta (banc<>9) y número de partidas.

    Excluye anuladas (soft-delete) — migración 0027.

    TMT 2026-05-18: si pasa `prov`, filtra por proveedor — para que el
    KPI hero refleje el total del filtro activo en /posdat?prov=XX.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total_abierto,
               COUNT(*)                  AS partidas_abiertas
        FROM scintela.posdat
        WHERE COALESCE(banc, 0) <> 9
          AND COALESCE(importe, 0) > 0
          AND (anulada IS NOT TRUE OR anulada IS NULL)
          AND (%(prov)s IS NULL OR UPPER(prov) = UPPER(%(prov)s))
        """,
        {"prov": prov or None},
    )
    return row or {"total_abierto": 0, "partidas_abiertas": 0}
