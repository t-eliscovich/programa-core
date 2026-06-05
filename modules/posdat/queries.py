"""Consultas de posdat (deuda viva con proveedores)."""
from calendar import monthrange
from datetime import date, datetime, timedelta

import db
from filters import today_ec
from periodo_guard import asegurar_fecha_abierta


# TMT 2026-05-28: el server EC2 corre en UTC. Para el cálculo de offsets
# de YY usamos zona Ecuador (UTC-5) — sino, una posdat creada de noche
# en Ecuador se ve en el día siguiente del server y el offset queda
# off-by-one. La hora exacta no importa, solo la fecha calendario EC.
def _hoy_ec() -> date:
    """Fecha calendario en zona Ecuador (UTC-5)."""
    return (datetime.utcnow() - timedelta(hours=5)).date()


# ---------------------------------------------------------------------------
# YY display-time (migración 0061 — TMT 2026-05-28).
# El importe de las posdat prov='YY' se calcula al renderizar como
#   importe_persistido + cuota_diaria × dias_habiles(baseline_date, hoy)
# y se RESETEA a 0 cuando se cruza un mes (lazy en primer hit).
# Ver docs/PLAN_YY_CUOTA_DIARIA_2026_05_28.md para detalle.
# ---------------------------------------------------------------------------


def _dias_habiles_entre(desde: date, hasta: date) -> int:
    """Cuenta lunes-viernes en el intervalo (desde, hasta]. Excluye `desde`,
    incluye `hasta`. Devuelve 0 si hasta <= desde.

    Ejemplos (todos con desde=jue 28/05/2026):
        hasta=28/05 (mismo día)  → 0
        hasta=29/05 (viernes)    → 1
        hasta=30/05 (sábado)     → 1
        hasta=31/05 (domingo)    → 1
        hasta=01/06 (lunes)      → 2
    """
    if hasta is None or desde is None or hasta <= desde:
        return 0
    n = 0
    d = desde
    while d < hasta:
        d = d + timedelta(days=1)
        if d.weekday() < 5:  # 0=L .. 4=V
            n += 1
    return n


def _ultimo_dia_del_mes(d: date) -> date:
    """Último día calendario del mes que contiene `d`."""
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


# TMT 2026-06-03 dueña: REMOVED _ejecutar_cierre_mensual_yy.
# Antes hacía cierre mensual lazy (reset importe=0 + mov_doble audit).
# Decisión: PC debe acumular perpetuo como dBase (REPLACE IMPORTE+cuota
# diaria, sin reset). Los mov_doble 'posdat_yy_cierre_mes' previamente
# generados (mayo-junio 2026) quedan EN scintela.mov_doble como audit
# histórico inmutable. /admin/debug-ustock los sigue contando.


def _aplicar_display_time_yy(rows: list[dict], hoy: date | None = None) -> None:
    """Aplica la fórmula display-time IN-PLACE sobre filas YY y RT:

        importe_display = importe_persistido
                        + cuota_diaria × dias_habiles(baseline_date, hoy)

    TMT 2026-06-03 dueña: SACADO el cierre mensual lazy. Antes esto
    "limpiaba" el importe persistido a 0 cada cambio de mes y registraba
    un mov_doble tipo='posdat_yy_cierre_mes' con el acumulado. La dueña
    pidió que PC se comporte exactamente como dBase: acumular perpetuo
    (dBase MENU.PRG L283-333 hace REPLACE IMPORTE+cuota DAILY y nunca
    cierra). Con esa decisión:
      - importe_persistido queda como base permanente (no se resetea).
      - El display = base + cuota × días hábiles desde baseline.
      - Los mov_doble 'posdat_yy_cierre_mes' previos quedan EN
        scintela.mov_doble como audit histórico (no se borran).

    También extendido a prov='RT' — antes RT estaba afuera del display
    porque el cierre rompía con sus volúmenes, y el importe persistido
    se actualizaba manualmente. Con la lógica nueva (sin reset) RT acumula
    coherentemente como en dBase.

    Sólo opera sobre filas con prov IN ('YY','RT') AND baseline_date IS NOT NULL.
    El resto queda intacto (posdat regulares, YY/RT legacy sin baseline).
    """
    hoy = hoy or _hoy_ec()
    for r in rows:
        prov_upper = (r.get("prov") or "").strip().upper()
        if prov_upper not in ("YY", "RT"):
            continue
        base_date = r.get("baseline_date")
        if not base_date:
            continue
        cd = float(r.get("cuota_diaria") or 0)
        if cd <= 0:
            r["importe_base"] = float(r.get("importe") or 0)
            r["dias_offset"] = 0
            continue
        importe_pers = float(r.get("importe") or 0)
        # NOTA arquitectura (2026-06-03): no hay cierre mensual lazy. Si
        # baseline_date está varios meses atrás, se acumulan todos los
        # días hábiles entre baseline y hoy (perpetuo, como dBase).
        offset = _dias_habiles_entre(base_date, hoy)
        r["importe_base"] = round(importe_pers, 2)
        r["importe"] = round(importe_pers + cd * offset, 2)
        r["dias_offset"] = offset

# Filtro reutilizable de "no anuladas": toda query de listado/balance/saldo
# tiene que excluir las filas soft-deleted (migración 0027). NULL = legacy
# pre-migración, equivalente a FALSE.
POSDAT_NO_ANULADA_WHERE = "(anulada IS NOT TRUE OR anulada IS NULL)"


# TMT 2026-05-28: chequeo de la columna `baseline_date` per-request.
# Si todavía no corrió la migración 0061, los SELECT no la incluyen y
# el código funciona como antes (sin display-time). Una vez que la
# migración corre, el siguiente request la detecta y el display-time
# se activa solo.
#
# Por qué NO cacheamos: el primer intento fue cachear en module-level,
# pero los workers que arrancaron antes de la migración quedaban con
# cache=False permanente hasta restart de Waitress → /posdat?tab=yy
# devolvía 500 hasta que se reiniciaba el server. Un SELECT a
# information_schema por request es barato (índice nativo de PG).
def _baseline_col_exists() -> bool:
    """¿Existe scintela.posdat.baseline_date? Chequeo per-request."""
    try:
        row = db.fetch_one(
            """
            SELECT 1 AS x
              FROM information_schema.columns
             WHERE table_schema = 'scintela'
               AND table_name   = 'posdat'
               AND column_name  = 'baseline_date'
            """
        )
        return bool(row)
    except Exception:  # noqa: BLE001
        return False


def por_id(id_posdat: int) -> dict | None:
    # NOTA: NO filtramos `anulada` acá — `por_id` se usa también desde la
    # vista de confirmar_anulacion y desde scripts de auditoría que pueden
    # querer ver una fila anulada. Los listados/balances sí filtran.
    # Coma pegada DENTRO del fragmento opcional para evitar coma huérfana
    # cuando la columna no existe (bug del primer hotfix).
    baseline_col = ", pd.baseline_date" if _baseline_col_exists() else ""
    return db.fetch_one(
        f"""
        SELECT pd.id_posdat, pd.num, pd.fecha, pd.fechad, pd.prov, pd.importe,
               pd.banc, pd.concepto, pd.clave,
               pd.anulada, pd.motivo_anulacion, pd.fecha_anulacion{baseline_col},
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
    # TMT 2026-05-20 v3 — relajar validaciones para tab YY (pedido dueña:
    # "que no me bloquee"). YY es un "proveedor virtual" para
    # provisiones / cuotas mensuales; los importes pueden ser 0 al
    # inicio y la dueña los completa inline. No requiere FK a
    # scintela.proveedor ni concepto obligatorio.
    es_yy = prov == "YY"
    if importe is None or (float(importe) <= 0 and not es_yy):
        raise ValueError("Importe debe ser mayor que cero.")
    if not concepto and not es_yy:
        raise ValueError("Concepto requerido.")
    if not es_yy and not db.fetch_one(
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

    # TMT 2026-05-28 (migración 0061): YY nuevas nacen con baseline_date = fecha
    # de creación. Así la fórmula display-time las arranca en offset=0 desde
    # ya. Sin baseline, el cron viejo todavía las podría incrementar (filtra
    # IS NULL) — preferimos snapshot HOY desde el alta. Si la migración 0061
    # aún no corrió, INSERT sin esa columna (el código sigue funcionando).
    if es_yy and _baseline_col_exists():
        return db.execute_returning(
            """
            INSERT INTO scintela.posdat
                (num, fecha, fechad, prov, importe, banc, concepto, clave,
                 usuario_crea, baseline_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id_posdat, num
            """,
            (
                num, fecha, fechad, prov, importe, banc,
                concepto_full,
                (clave or None) and clave[:3],
                usuario,
                fecha,  # baseline_date inicial = fecha de creación
            ),
        ) or {}

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
    prov: str | None = None,       # TMT 2026-05-19 v8 — editable para banc=9
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

    # Mergear extras al concepto si vinieron (sólo tipo/compr/no_comp del
    # caller, NO la marca de auditoría del importe — esa quedó como una
    # mala idea: ensuciaba el concepto y, peor, cuando el caller no
    # mandaba concepto, terminaba REEMPLAZANDO el concepto original con
    # SÓLO la marca [ED imp_prev:X nuevo:Y]. TMT 2026-05-20: el audit del
    # importe ya queda registrado en mov_doble (tipo='posdat_edit_importe')
    # y en /historial — no hace falta también desfigurar el concept.
    extras_parts = [
        f"[{tipo}]" if tipo else None,
        compr or None,
        no_comp or None,
    ]
    extras = " ".join(x for x in extras_parts if x)

    # Regla: SOLO actualizamos `concepto` si el caller mandó algo explícito
    # (concepto explícito o algún campo extra como tipo/compr/no_comp).
    # Si concepto is None Y no hay extras, dejamos la columna intacta —
    # ANTES la combinación de "importe cambia + concepto None" wipeaba
    # el concept con el marker de audit. Bug visto en row #151
    # (concepto quedó como "[ED imp_prev:5000 nuevo:9500]").
    if concepto is not None or extras:
        concepto_base = concepto if concepto is not None else (actual.get("concepto") or "")
        concepto_full = (concepto_base + (" " + extras if extras else "")).strip()
        if concepto_full:
            campos.append("concepto = %s")
            params.append(concepto_full[:100])

    if fechad is not None:
        campos.append("fechad = %s")
        params.append(fechad)
    if importe is not None:
        campos.append("importe = %s")
        params.append(importe_nuevo)
        # TMT 2026-05-28 (migración 0061): si es una posdat YY y le
        # cambian el importe a mano, resetear baseline_date = HOY para que
        # la fórmula display-time no acumule offsets viejos sobre el valor
        # nuevo. Sin esto, la dueña edita "86100 → 90000" y al renderizar
        # ve "90000 + cuota × días_desde_baseline_viejo" = sorpresa.
        # Skip si la columna aún no existe (migración no aplicada).
        # Usamos _hoy_ec() (UTC-5) en lugar de CURRENT_DATE para alinear
        # con lo que ve la dueña en pantalla.
        prov_actual = (actual.get("prov") or "").strip().upper()
        if prov_actual == "YY" and _baseline_col_exists():
            campos.append("baseline_date = %s")
            params.append(_hoy_ec())
    # TMT 2026-05-19 v8 — `prov` editable. Antes estaba bloqueado por
    # regla legacy (no cambiar matching con proveedor), pero la dueña
    # pide editar todos los campos. Solo aplica si viene; vacío → NULL.
    if prov is not None:
        campos.append("prov = %s")
        params.append((prov or "").strip().upper()[:5] or None)
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
                    fecha=actual.get("fecha") or today_ec(),
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
                fecha=pd.get("fecha") or today_ec(),
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
    tab: str = "posdatados",
) -> list[dict]:
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # #18 (TMT 2026-05-14): "solo_abiertas" = deuda viva sin instrumentar.
    # Antes filtraba banc<>9, pero eso incluía banc=10/32 (cheques emitidos
    # modernos PC) — esos NO son deuda abierta, ya fueron pagados desde el
    # banco. La definición correcta es banc=0 (POSDAT_DEUDA_VIVA_WHERE).
    #
    # TMT 2026-05-20 — split en dos tabs (pedido dueña):
    #   tab='posdatados' → excluye prov='YY' (deudas a proveedor reales).
    #   tab='yy'         → solo prov='YY' (gastos forzados / provisiones).
    tab_norm = (tab or "posdatados").strip().lower()
    # Coma pegada DENTRO del fragmento opcional para evitar coma huérfana
    # cuando la columna no existe (bug del primer hotfix).
    baseline_col = ", pd.baseline_date" if _baseline_col_exists() else ""
    rows = db.fetch_all(
        f"""
        SELECT pd.id_posdat, pd.num, pd.fecha, pd.fechad, pd.prov, pd.importe,
               pd.banc, pd.concepto, pd.clave{baseline_col},
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
          -- TMT 2026-05-20: filtro de tab (YY vs resto).
          -- TMT 2026-05-27 dueña: prov='RT' (retenciones) también cuenta
          -- como provisión YY. 'hace que con las migraciones esto ya no
          -- se toque' → filtro a nivel código, no migrar data.
          AND (
                (%(tab)s = 'yy'         AND UPPER(COALESCE(pd.prov,'')) IN ('YY','RT'))
             OR (%(tab)s = 'posdatados' AND UPPER(COALESCE(pd.prov,'')) NOT IN ('YY','RT'))
          )
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
            "tab": tab_norm,
        },
    ) or []

    # TMT 2026-05-20 — match a scintela.provisiones HECHO EN PYTHON.
    # Originalmente lo intenté con LEFT JOIN LATERAL, pero un comportamiento
    # de psycopg2 con el escape de '%%' rompió en prod (500: error 61ea4d2e).
    # En Python es más simple + defensivo: cargo todas las provisiones (son
    # ~10-20 filas) UNA vez por request y matcheo en memoria. Si la tabla no
    # existe o falla, los posdat siguen funcionando sin cuota_mensual.
    provisiones_lookup: list[dict] = []
    try:
        provisiones_lookup = db.fetch_all(
            "SELECT id_provisiones, concepto, importe, periodo_aplica "
            "FROM scintela.provisiones"
        ) or []
    except Exception:  # noqa: BLE001
        provisiones_lookup = []

    # Ordenar por longitud DESC para que el "más específico" gane (igual
    # que el ORDER BY LENGTH del LATERAL original).
    provisiones_lookup.sort(
        key=lambda p: len((p.get("concepto") or "").strip()), reverse=True,
    )

    def _match_provision(concepto_pd: str) -> dict | None:
        """Devuelve la provisión que matchea por concepto (starts-with
        bidireccional, case-insensitive, longitud ≥ 3)."""
        cn = (concepto_pd or "").strip().upper()
        if len(cn) < 3:
            return None
        for pr in provisiones_lookup:
            cp = (pr.get("concepto") or "").strip().upper()
            if len(cp) < 3:
                continue
            if cn.startswith(cp) or cp.startswith(cn):
                return pr
        return None

    for r in rows:
        match = _match_provision(r.get("concepto") or "")
        if match:
            # TMT 2026-05-28 dueña: 'en vez de mensual hagamos cuota diaria'
            # La tabla scintela.provisiones.importe ahora guarda la cuota
            # DIARIA directamente (antes era mensual). NO dividir por 30.
            # cuota_mensual queda como estimación visual (× 30).
            diaria = float(match.get("importe") or 0)
            r["id_provisiones"]    = match.get("id_provisiones")
            r["cuota_diaria"]      = diaria
            r["cuota_mensual"]     = diaria  # legacy: el template/JS leen cuota_mensual; mantenemos el valor diario que la dueña ve y edita
            r["provision_periodo"] = match.get("periodo_aplica")
        else:
            # TMT 2026-05-29 dueña: RT (IVA) suma 8400/día hábil según
            # dBase MENU.PRG L333 (REPLA IMPORTE+8400). Hardcoded acá
            # porque RT no está en scintela.provisiones. Otros posdat
            # sin match en provisiones quedan sin cuota.
            r["id_provisiones"]    = None
            r["provision_periodo"] = None
            r["cuota_diaria"]      = None
            r["cuota_mensual"]     = None
            prov_upper = (r.get("prov") or "").strip().upper()
            if prov_upper == "RT":
                r["cuota_diaria"]  = 8400.0
                r["cuota_mensual"] = 8400.0

    # TMT 2026-05-28 (migración 0061): aplicar la fórmula display-time
    # YY ANTES de calcular saldo_acumulado, así el acumulado refleja el
    # importe que la dueña ve en pantalla.
    # Defensivo: si el cálculo display-time tira por cualquier razón
    # (mov_doble fail, fila YY rara, edge case del calendario), preferimos
    # mostrar el importe persistido y NO romper toda la pantalla con un
    # 500. El log queda con el traceback para debug.
    try:
        _aplicar_display_time_yy(rows)
    except Exception:  # noqa: BLE001
        import logging as _lg
        _lg.getLogger(__name__).exception(
            "_aplicar_display_time_yy explotó — fallback a importe persistido"
        )

    # Saldo acumulado = deuda corrida hasta la fecha de vencimiento. Útil
    # para planificar flujo: "al 15 de junio ya vencieron $ X de posdatados".
    # (TMT 2026-05-12)
    acum = 0.0
    for r in rows:
        if (r.get("banc") or 0) != 9:  # banc=9 ya pagada
            acum += float(r.get("importe") or 0)
        r["saldo_acumulado"] = acum
    return rows


def resumen(
    prov: str | None = None,
    *,
    q: str = "",
    solo_abiertas: bool = True,
    desde: str | None = None,
    hasta: str | None = None,
    tab: str = "posdatados",
) -> dict:
    """Total de deuda abierta y número de partidas.

    TMT 2026-05-19 — item 18 (pedido dueña): el resumen ahora matchea
    exactamente lo que devuelve `buscar()`, así "X partidas" coincide con
    las filas visibles en el listado. Antes filtraba `importe > 0` y
    excluía las filas negativas (ajustes/anticipos a favor), dando
    contadores tipo "4 partidas" cuando se veían 8 filas en pantalla.

    Excluye anuladas (soft-delete) — migración 0027.
    """
    q_s = (q or "").strip()
    like = f"%{q_s}%" if q_s else None
    tab_norm = (tab or "posdatados").strip().lower()

    # TMT 2026-05-28 (migración 0061): para el tab YY el SUM(pd.importe)
    # del SQL NO refleja el display-time. Delegamos a buscar() y sumamos
    # los importes ya recalculados. El SQL original del resumen sólo
    # contaba prov='YY' (no RT), preservamos esa convención para que el
    # KPI "N provisiones" no cambie.
    #
    # Devolvemos Decimal (no float) para matchear el tipo que devuelve
    # SUM(numeric) de psycopg2 en el otro tab. Si se mezclan
    # (template: _pos_total + _yy_total) con un float, Python tira
    # TypeError: unsupported operand 'Decimal' + 'float' → 500.
    if tab_norm == "yy":
        from decimal import Decimal as _Dec
        filas = buscar(
            prov=prov, q=q_s, solo_abiertas=solo_abiertas,
            desde=desde, hasta=hasta, tab="yy",
        )
        yy_solo = [
            f for f in filas
            if (f.get("prov") or "").strip().upper() == "YY"
        ]
        total = _Dec("0")
        for f in yy_solo:
            try:
                total += _Dec(str(f.get("importe") or 0))
            except Exception:  # noqa: BLE001
                pass
        return {
            "total_abierto": total,
            "partidas_abiertas": len(yy_solo),
        }

    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(pd.importe), 0) AS total_abierto,
               COUNT(*)                     AS partidas_abiertas
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
           -- TMT 2026-06-03 audit fix: resumen() debe usar la misma
           -- regla que buscar() — RT también es tab=yy (memoria 2026-05-27).
           -- Antes resumen contaba RT como posdatado → total ≠ filas visibles.
           AND (
                (%(tab)s = 'yy'         AND UPPER(COALESCE(pd.prov,'')) IN ('YY','RT'))
             OR (%(tab)s = 'posdatados' AND UPPER(COALESCE(pd.prov,'')) NOT IN ('YY','RT'))
           )
           AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
        """,
        {
            "prov": prov or None,
            "q": q_s or None, "like": like,
            "solo_abiertas": solo_abiertas,
            "desde": desde or None, "hasta": hasta or None,
            "tab": tab_norm,
        },
    )
    return row or {"total_abierto": 0, "partidas_abiertas": 0}
