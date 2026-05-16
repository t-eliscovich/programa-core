"""Queries de gastos (scintela.xgast).

xgast es similar a compra: tiene saldo y stat. La diferencia es que compras
son materias primas / mercadería, mientras que xgast son gastos generales
(luz, agua, sueldos, contadora, etc).

Vocabulario de stat (legacy):
    'A' o NULL = pagado al contado (saldo = 0)
    'P'        = postdatado / pendiente de pago (saldo > 0)
    'Y'        = anulado (legacy)

Categorías (`doc`) sugeridas — son códigos cortos:
    SER = servicios públicos (luz, agua, teléfono, internet)
    SUE = sueldos / cargas sociales
    HON = honorarios profesionales (contadora, abogado)
    IMP = impuestos
    ALQ = alquiler / expensas
    MAN = mantenimiento / reparaciones
    OTR = otro
"""
from datetime import date, timedelta

import db
from periodo_guard import asegurar_fecha_abierta

# Categorías canónicas para el dropdown de alta de gasto.
CATEGORIAS = (
    ("SER", "Servicios públicos"),
    ("SUE", "Sueldos / cargas"),
    ("HON", "Honorarios"),
    ("IMP", "Impuestos"),
    ("ALQ", "Alquiler / expensas"),
    ("MAN", "Mantenimiento"),
    ("OTR", "Otro"),
)
CATEGORIAS_SET = {c[0] for c in CATEGORIAS}


# ─────────────────────────────────────────────────────────────────────────
# Categorías V1..V9 del PRG legacy + auto-sugerencia desde concepto.
#
# `xgast.num` es la categoría 1-9 (NO un correlativo). El balance la usa
# para armar GTEJ (V1+V2+V3) / GTIN (V4+V5+V6) / GGF (V7+V8+V9) y los
# costos por kg en /informes/balance.
#
# TMT 2026-05-15: la lista `KEYWORDS_TO_CATEGORIA` la construimos con
# Tamara mirando los gastos reales que carga. La idea: cubrir el 80% de
# los casos con palabras cortas. Si una palabra no matchea, el form le
# pide al usuario que elija manualmente.
# ─────────────────────────────────────────────────────────────────────────
CATEGORIAS_V19 = (
    (1, "V1 — Personal tejeduría"),
    (2, "V2 — Gas/Comb. tejeduría"),
    (3, "V3 — Gs. varios tejeduría"),
    (4, "V4 — Personal tintorería"),
    (5, "V5 — Gas/Comb. tintorería"),
    (6, "V6 — Gs. varios tintorería"),
    (7, "V7 — Personal admin"),
    (8, "V8 — Servicios admin"),
    (9, "V9 — Gs. varios admin"),
)

# Keywords → num. El primero que matchea gana. Match por substring case-
# insensitive sobre el concepto entero. Las palabras-multipalabra (ej.
# "SUELDO TEJ") priman sobre las simples (ej. "SUELDO") porque están más
# arriba en la lista. Conservador a propósito: si no estás seguro, el
# form lo pregunta.
KEYWORDS_TO_CATEGORIA: tuple[tuple[str, int], ...] = (
    # ── V1 personal tejeduría ──────────────────────────────────────────
    ("SUELDO TEJ", 1), ("SUELDOS TEJ", 1), ("PERSONAL TEJ", 1),
    # ── V2 gas/comb tejeduría ──────────────────────────────────────────
    ("GAS TEJ", 2), ("COMB TEJ", 2), ("COMBUSTIBLE TEJ", 2),
    # ── V3 gs varios tejeduría ─────────────────────────────────────────
    ("REPUESTO TEJ", 3), ("MANTEN TEJ", 3), ("MANT TEJ", 3),
    ("INSUMO TEJ", 3), ("HILADO REPAR", 3),
    # ── V4 personal tintorería ─────────────────────────────────────────
    ("SUELDO TIN", 4), ("SUELDOS TIN", 4), ("PERSONAL TIN", 4),
    # ── V5 gas/comb/servicios tintorería ───────────────────────────────
    ("EEQ", 5), ("EMAAP", 5), ("ELECTRIC", 5),
    ("LUZ", 5), ("AGUA", 5), ("GAS TIN", 5),
    ("DIESEL", 5), ("COMBUS", 5), ("FUEL", 5),
    # ── V6 gs varios tintorería ────────────────────────────────────────
    ("PINTUR", 6), ("CC PINTUR", 6),  # match temprano
    ("QUIMIC", 6), ("COLORANTE EXTRA", 6),
    ("REPUESTO TIN", 6), ("MANTEN TIN", 6), ("MANT TIN", 6),
    ("INSUMO TIN", 6),
    # ── V7 personal admin ──────────────────────────────────────────────
    ("SUELDO ADM", 7), ("SUELDOS ADM", 7), ("PERSONAL ADM", 7),
    ("HONORARIO", 7), ("ABOGADO", 7), ("CONTAD", 7),
    # ── V8 servicios admin ─────────────────────────────────────────────
    ("TELEFON", 8), ("CELULAR", 8), ("INTERNET", 8),
    ("CABLE", 8), ("CHEQUERA", 8), ("BANCAR", 8),
    # ── V9 gs varios admin / impuestos / etc ───────────────────────────
    ("SRI", 9), ("IMPUEST", 9), ("MUNICIP", 9), ("IESS", 9),
    ("JUBILAC", 9), ("INCOBRA", 9), ("INTERES", 9),
    ("PAPELERI", 9), ("OFICINA", 9), ("ALQUILER", 9),
    ("ANDRES BUCHELI", 9), ("AB ", 9),
    # ── Sueldos genérico (default V7 si no hay TEJ/TIN/ADM explícito) ──
    ("SUELDO", 7), ("SUELDOS", 7), ("PERSONAL", 7),

    # ── Prefijos cortos legacy (revisado con dueña 2026-05-15) ──────────
    # "SU ADM" / "SU CAJA" / "KK SU CAJA" → personal admin = V7
    ("SU ADM", 7), ("SU CAJA", 7), ("SUCAJA", 7),
    # "KKSU NOMBRE" / "CCSU NOMBRE" → adelanto sueldo a empleado (V7).
    # Va ANTES de "KK " y "CC " para que matchee primero.
    ("KKSU ", 7), ("CCSU ", 7),
    # "SS ..." → IESS o insumos varios. Si vino sin INSUMO/IESS literal,
    # lo tratamos como gastos generales (V9). Va antes que SS de IESS.
    ("SS ", 9),
    # GASOLINA / GASO ... → combustible tin (V5). Cubrimos las abreviaturas
    # típicas que usa la dueña.
    ("GASOLIN", 5), ("GASO ", 5), ("NAFTA", 5),
    # "CC ..." (CC = PROV.FABRICA, vende insumos para tintura/máquinas) → V6
    ("CC ", 6),
    # "GS ..." / "GAS ..." → gastos varios de planta. Por default los
    # consideramos GASTOS GENERALES ADMIN (V9); si fuera tintorería/tej
    # específico, otros keywords más arriba ganan.
    ("GS ", 9), ("GAS ", 9),
    # "KK ..." → kilometraje/transporte (V3 varios tejeduría histórico).
    ("KK ", 3),
)


def sugerir_categoria(concepto: str) -> int | None:
    """Devuelve el primer num 1-9 cuyo keyword matchea en `concepto`.

    Match case-insensitive por substring sobre todo el concepto.
    Devuelve None si nada matchea — el form debe pedirle al usuario que
    elija manual.
    """
    if not concepto:
        return None
    up = concepto.upper()
    for kw, num in KEYWORDS_TO_CATEGORIA:
        if kw in up:
            return num
    return None


def clasificar_desde_caja(
    *,
    id_caja: int,
    num: int,
    usuario: str = "web",
) -> dict:
    """Asigna categoría V1-V9 a un egreso de caja creando fila xgast +
    mov_doble. NO toca la fila de caja (ya existe y representa el flujo
    real de plata); sólo agrega la traza contable de "esta plata es un
    gasto de categoría N".

    Validaciones:
      - id_caja debe existir y ser tipo='S' (egreso).
      - num ∈ {1..9}.
      - No debe haber ya un mov_doble que linkee esta caja a un xgast
        (idempotencia: si ya está clasificado, devolver el existente).

    Devuelve `{id_xgast, id_mov_doble, num, ya_existia: bool}`.
    """
    if num not in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
        raise ValueError(f"Categoría inválida: {num}. Debe ser 1..9.")

    caja = db.fetch_one(
        "SELECT id_caja, fecha, tipo, importe, concepto, clave "
        "FROM scintela.caja WHERE id_caja = %s",
        (id_caja,),
    )
    if not caja:
        raise ValueError(f"Caja id={id_caja} no existe.")
    if (caja.get("tipo") or "").upper() != "S":
        raise ValueError(
            f"Caja id={id_caja} es tipo={caja.get('tipo')!r}, no 'S'. "
            "Sólo egresos pueden clasificarse como gasto."
        )

    # Idempotencia: si ya existe un mov_doble que linkea esta caja con un
    # xgast, devolver el existente sin crear duplicado.
    existente = db.fetch_one(
        """
        SELECT id_mov_doble, destino_id
          FROM scintela.mov_doble
         WHERE origen_table='caja' AND origen_id = %s
           AND destino_table='xgast'
           AND estado='activo'
         LIMIT 1
        """,
        (id_caja,),
    )
    if existente:
        return {
            "id_xgast": int(existente["destino_id"]),
            "id_mov_doble": int(existente["id_mov_doble"]),
            "num": num,
            "ya_existia": True,
        }

    fecha = caja["fecha"]
    importe = float(caja["importe"] or 0)
    concepto = (caja.get("concepto") or "").strip()
    clave = (caja.get("clave") or "").strip() or None

    # Si hay una compra falsa creada por el dispatcher (concepto matcheó
    # "PR PINTURA" con PR=proveedor), la anulamos para evitar doble-
    # contabilización (la compra cuenta como MP, el xgast como gasto).
    compra_anulada = None
    md_caja_compra = db.fetch_one(
        """
        SELECT id_mov_doble, destino_id
          FROM scintela.mov_doble
         WHERE origen_table='caja' AND origen_id=%s
           AND destino_table='compra' AND estado='activo'
         ORDER BY id_mov_doble DESC LIMIT 1
        """,
        (id_caja,),
    )

    with db.tx() as conn:
        if md_caja_compra and md_caja_compra.get("destino_id"):
            id_compra_falsa = int(md_caja_compra["destino_id"])
            db.execute(
                "UPDATE scintela.compra "
                "   SET stat = 'X', "
                "       observacion = COALESCE(observacion, '') || %s, "
                "       usuario_modifica = %s, "
                "       fecha_modifica = CURRENT_TIMESTAMP "
                " WHERE id_compra = %s",
                (
                    f" [reclasif como gasto V{num} desde caja #{id_caja}]",
                    usuario, id_compra_falsa,
                ),
                conn=conn,
            )
            db.execute(
                "UPDATE scintela.mov_doble "
                "   SET estado = 'reversado' "
                " WHERE id_mov_doble = %s",
                (md_caja_compra["id_mov_doble"],),
                conn=conn,
            )
            compra_anulada = id_compra_falsa

        row = db.execute_returning(
            """
            INSERT INTO scintela.xgast
                (fecha, doc, prov, concepto, num, fechad, importe, saldo,
                 stat, clave, usuario_crea)
            VALUES (%s, 'OTR', NULL, %s, %s, %s, %s, 0, 'A',
                    %s, %s)
            RETURNING id_xgast
            """,
            (
                fecha, concepto[:100], num, fecha, importe,
                (clave or None) and clave[:3].upper(),
                usuario,
            ),
            conn=conn,
        ) or {}
        id_xgast = int(row["id_xgast"])

        import mov_doble as _md
        id_md = _md.registrar(
            conn=conn,
            tipo="caja_s_to_xgast",
            origen_table="caja",
            origen_id=id_caja,
            destino_table="xgast",
            destino_id=id_xgast,
            importe=importe,
            fecha=fecha,
            concepto=(f"Clasificar caja #{id_caja} como gasto V{num}: "
                      f"{concepto}")[:200],
            usuario=usuario,
            metadata={"num_categoria": num,
                      "concepto_original": concepto},
        )

    return {
        "id_xgast": id_xgast,
        "id_mov_doble": id_md,
        "num": num,
        "ya_existia": False,
        "compra_anulada": compra_anulada,
    }


def caja_egresos_sin_clasificar(limite: int = 200) -> list[dict]:
    """Lista egresos de caja (tipo='S') del mes en curso que NO tienen
    fila xgast linkeada vía mov_doble. Usado por el banner en /caja y la
    UI de clasificación.
    """
    return db.fetch_all(
        """
        SELECT c.id_caja, c.fecha, c.importe, c.concepto, c.clave
          FROM scintela.caja c
         WHERE c.tipo = 'S'
           AND c.fecha >= date_trunc('month', CURRENT_DATE)
           AND NOT EXISTS (
             SELECT 1 FROM scintela.mov_doble md
              WHERE md.origen_table='caja' AND md.origen_id=c.id_caja
                AND md.destino_table='xgast'
                AND md.estado='activo'
           )
           AND NOT EXISTS (
             -- Excluir egresos que YA tienen otro side-effect "puro"
             -- (transfer banco, retiro socio, dolares anticipo). Esos
             -- definitivamente NO son gastos.
             --
             -- TMT 2026-05-15: `compra` NO se excluye porque el parser
             -- puede crear una compra falsa cuando el concepto empieza
             -- con 2 letras que coinciden con un código de proveedor
             -- (ej "CC PINTURA" → compra a CC=PROV.FABRICA). En esos
             -- casos la usuaria igual quiere clasificarlo como gasto
             -- V1..V9. La función `clasificar_desde_caja` detecta si
             -- hay compra previa y la anula antes de crear el xgast.
             SELECT 1 FROM scintela.mov_doble md
              WHERE md.origen_table='caja' AND md.origen_id=c.id_caja
                AND md.destino_table IN ('transacciones_bancarias',
                                         'retiros', 'dolares')
                AND md.estado='activo'
           )
           -- TMT 2026-05-15: excluir por PREFIJO de concepto los movimientos
           -- que claramente NO son gastos aunque la data legacy no tenga
           -- mov_doble retroactivo (PICH→banco, INTER→banco internacional,
           -- RR→retiro socio, IN.→anticipos USD, INHB→capital). El parser
           -- moderno crea su mov_doble correctamente; este filtro cubre
           -- la data importada del DBF que no tiene esos links.
           -- TMT 2026-05-15: pasamos los prefijos como parámetros para
           -- evitar líos con psycopg2 y el escape de %.
           AND UPPER(TRIM(COALESCE(c.concepto, ''))) NOT LIKE %s
           AND UPPER(TRIM(COALESCE(c.concepto, ''))) NOT LIKE %s
           AND UPPER(TRIM(COALESCE(c.concepto, ''))) NOT LIKE %s
           AND UPPER(TRIM(COALESCE(c.concepto, ''))) NOT LIKE %s
           AND UPPER(TRIM(COALESCE(c.concepto, ''))) NOT LIKE %s
         ORDER BY c.fecha DESC, c.id_caja DESC
         LIMIT %s
        """,
        ("PICH%", "INTER%", "RR%", "IN.%", "INHB%", limite),
    ) or []


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
) -> list[dict]:
    """Histórico de gastos filtrable por concepto/proveedor/doc + fecha."""
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    rows = db.fetch_all(
        """
        SELECT g.id_xgast, g.fecha, g.fechad, g.doc, g.prov,
               g.concepto, g.num, g.importe, g.saldo, g.stat, g.clave,
               COALESCE(p.nombre, '') AS proveedor
        FROM scintela.xgast g
        LEFT JOIN scintela.proveedor p ON p.codigo_prov = g.prov
        WHERE (%(q)s IS NULL
               OR UPPER(COALESCE(g.concepto,'')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(g.prov,'')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(g.doc,'')) LIKE UPPER(%(like)s)
               OR UPPER(COALESCE(p.nombre,'')) LIKE UPPER(%(like)s)
               OR CAST(COALESCE(g.num, 0) AS TEXT) LIKE %(like)s)
          AND (%(desde)s::date IS NULL OR g.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR g.fecha <= %(hasta)s::date)
        ORDER BY g.fecha DESC, g.id_xgast DESC
        LIMIT %(limite)s
        """,
        {
            "q": q or None, "like": like,
            "desde": desde or None, "hasta": hasta or None,
            "limite": limite,
        },
    ) or []
    # Running total cronológico (ascendente) — total gastado corrido.
    # TMT 2026-05-13.
    from datetime import date as _date
    rows_asc = sorted(rows, key=lambda r: (r.get("fecha") or _date.min,
                                           r.get("id_xgast") or 0))
    acum = 0.0
    for r in rows_asc:
        acum += float(r.get("importe") or 0)
        r["saldo_acumulado"] = acum
    return list(reversed(rows_asc))


def totales_por_mes(meses: int = 12) -> list[dict]:
    """Resumen mensual de gastos para un mini-chart o vista de tendencia."""
    return db.fetch_all(
        """
        SELECT date_trunc('month', fecha)::date AS mes,
               SUM(importe) AS total,
               COUNT(*)     AS n_gastos
        FROM scintela.xgast
        WHERE fecha >= CURRENT_DATE - (%s || ' months')::interval
        GROUP BY 1
        ORDER BY 1 DESC
        """,
        (max(1, min(int(meses or 12), 60)),),
    ) or []


def proximo_numero() -> int:
    """Siguiente número de gasto (MAX+1). Fallback a 1."""
    row = db.fetch_one(
        "SELECT COALESCE(MAX(num), 0) + 1 AS siguiente FROM scintela.xgast"
    )
    return int(row["siguiente"]) if row else 1


def crear(
    *,
    fecha: date,
    concepto: str,
    importe,
    doc: str | None = None,
    prov: str | None = None,
    fechad: date | None = None,
    pagado: bool = True,
    num: int | None = None,
    clave: str | None = None,
    usuario: str = "web",
) -> dict:
    """Alta de gasto.

    Reglas:
        - Si `pagado=True` (default): stat='A', saldo=0 (pagado contado).
        - Si `pagado=False`: stat='P', saldo=importe (pendiente, requiere
          `fechad` futura para indicar cuándo se va a pagar).
        - `doc` es la categoría (SER/SUE/HON/IMP/ALQ/MAN/OTR). Si llega
          algo distinto, se pone en OTR para no perder el dato.
        - `prov` es opcional — código de proveedor si lo hay (ej: la
          empresa eléctrica). Vacío para gastos sin proveedor formal.

    Devuelve `{id_xgast, num}`.
    """
    asegurar_fecha_abierta(fecha)

    concepto = (concepto or "").strip()
    if not concepto:
        raise ValueError("Concepto del gasto requerido.")
    importe_num = float(importe or 0)
    if importe_num <= 0:
        raise ValueError("Importe debe ser mayor que cero.")

    if num is None:
        num = proximo_numero()

    # Normalizar categoría
    doc_norm = (doc or "").upper().strip()
    if doc_norm and doc_norm not in CATEGORIAS_SET:
        doc_norm = "OTR"
    doc_final = doc_norm or "OTR"

    if pagado:
        stat = "A"
        saldo = 0.0
        fechad_final = fechad or fecha
    else:
        stat = "P"
        saldo = importe_num
        if fechad is None:
            raise ValueError("Si el gasto no es pagado, requiere fecha de pago (fechad).")
        if fechad <= fecha:
            raise ValueError("La fecha de pago tiene que ser posterior a la fecha del gasto.")
        fechad_final = fechad

    with db.tx() as conn:
        row = db.execute_returning(
            """
            INSERT INTO scintela.xgast
                (fecha, doc, prov, concepto, num, fechad, importe, saldo, stat,
                 clave, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s)
            RETURNING id_xgast, num
            """,
            (
                fecha,
                doc_final,
                (prov or None) and prov[:5].upper(),
                concepto[:100],
                num,
                fechad_final,
                importe_num,
                saldo,
                stat,
                (clave or None) and clave[:3].upper(),
                usuario,
            ),
            conn=conn,
        ) or {}
        # Historial unificado: todo gasto queda registrado en mov_doble
        # como auto-referencia para que aparezca en /historial. Si falla,
        # rollback total. TMT 2026-05-13.
        #
        # Bug B fix (TMT 2026-05-16): cuando `pagado=True`, el flujo original
        # creaba solo un mov_doble `gasto_simple` sin tocar caja. Resultado:
        # el gasto se cargaba como pagado pero la caja seguía igual — pero al
        # reversar, la lógica de `anular()` SÍ devolvía la plata a caja
        # (línea 709-720), generando dinero de la nada. Asimétrico y
        # peligroso. Ahora con `pagado=True` SIEMPRE creamos la caja salida
        # correspondiente y linkeamos vía `caja_s_to_xgast`, así el ciclo
        # alta + reverso es simétrico.
        if row.get("id_xgast"):
            import mov_doble as _md
            if pagado:
                # 1) caja salida real
                import caja_helpers as _ch
                caja_row = _ch.insert_movimiento_caja(
                    conn,
                    fecha=fecha,
                    tipo="S",
                    importe=importe_num,
                    concepto=(f"Gasto #{num} {doc_final} — {concepto}")[:100],
                    clave="GAS",
                    usuario=usuario,
                )
                id_caja = caja_row.get("id_caja") if caja_row else None
                # 2) link caja → xgast vía mov_doble (mismo flujo histórico)
                _md.registrar(
                    conn=conn,
                    tipo="caja_s_to_xgast",
                    origen_table="caja",
                    origen_id=id_caja,
                    destino_table="xgast",
                    destino_id=row["id_xgast"],
                    importe=importe_num,
                    fecha=fecha,
                    concepto=(f"Gasto #{num} {doc_final} — {concepto}")[:200],
                    usuario=usuario,
                    metadata={"doc": doc_final,
                              "prov": prov or "",
                              "pagado": True,
                              "fechad": fechad_final.isoformat() if fechad_final else None},
                )
            else:
                # Gasto a crédito (no pagado) — sigue creando un xgast→xgast
                # self-loop + posdat aparte (lógica original sin cambios).
                _md.registrar(
                    conn=conn,
                    tipo="gasto_a_posdat",
                    origen_table="xgast",
                    origen_id=row["id_xgast"],
                    destino_table="xgast",
                    destino_id=row["id_xgast"],
                    importe=importe_num,
                    fecha=fecha,
                    concepto=(f"Gasto #{num} {doc_final} — {concepto}")[:200],
                    usuario=usuario,
                    metadata={"doc": doc_final,
                              "prov": prov or "",
                              "pagado": False,
                              "fechad": fechad_final.isoformat() if fechad_final else None},
                )
    return row


def resumen(desde: str | None = None, hasta: str | None = None) -> dict:
    """Total + n + ticket promedio del filtro actual."""
    desde_d = desde or (date.today() - timedelta(days=90)).isoformat()
    hasta_d = hasta or date.today().isoformat()
    row = db.fetch_one(
        """
        SELECT COALESCE(SUM(importe), 0) AS total,
               COALESCE(SUM(saldo), 0)   AS saldo_pendiente,
               COUNT(*)                  AS n
        FROM scintela.xgast
        WHERE fecha BETWEEN %s::date AND %s::date
        """,
        (desde_d, hasta_d),
    ) or {}
    n = int(row.get("n") or 0)
    total = float(row.get("total") or 0)
    return {
        "n":               n,
        "total":           total,
        "saldo_pendiente": float(row.get("saldo_pendiente") or 0),
        "ticket_promedio": (total / n) if n else 0.0,
        "desde":           desde_d,
        "hasta":           hasta_d,
    }


def por_id(id_xgast: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT g.id_xgast, g.fecha, g.fechad, g.doc, g.prov, g.concepto,
               g.num, g.importe, g.saldo, g.stat, g.clave,
               COALESCE(p.nombre, '') AS proveedor
          FROM scintela.xgast g
          LEFT JOIN scintela.proveedor p ON p.codigo_prov = g.prov
         WHERE g.id_xgast = %s
        """,
        (id_xgast,),
    )


def anular(id_xgast: int, *, motivo: str = "", usuario: str = "web") -> dict:
    """Marca un gasto como anulado (stat='Y') Y compensa el side-effect.

    Reglas (TMT 2026-05-14, #8 — antes no se compensaba la caja/banco):
      - El gasto tiene que existir y no estar ya en stat='Y'.
      - Si era pagado al contado: compensa el movimiento original según
        la cuenta usada:
          * cuenta='caja'         → INSERT caja tipo='E' (ingreso).
          * cuenta='pichincha'    → INSERT tx_bancarias doc='NC' banco=1.
          * cuenta='internacional'→ INSERT tx_bancarias doc='NC' banco=2.
          * cuenta='P' (posdat)   → reabrir la posdat hermana (banc=0).
      - La detección de "cuenta usada" se hace por:
          1) `xgast.cuenta_pagada` si existe la columna,
          2) en su defecto, inspección del mov_doble original
             (tipo='gasto_*' → mira destino_table + metadata).
      - Registra mov_doble del reverso linkeado al original (full audit).
      - Atomic: todo en una sola db.tx().

    Devuelve {id_xgast, stat_previo, stat_nuevo, side_effects_reversados}.
    """
    motivo = (motivo or "").strip()
    g_row = db.fetch_one(
        "SELECT id_xgast, num, doc, importe, stat, fecha, fechad, prov "
        "FROM scintela.xgast WHERE id_xgast = %s",
        (id_xgast,),
    )
    if not g_row:
        raise ValueError(f"Gasto id={id_xgast} no existe.")
    if (g_row.get("stat") or "").upper() == "Y":
        raise ValueError("El gasto ya está anulado.")

    stat_prev = (g_row.get("stat") or "").upper() or "A"
    importe_gasto = float(g_row.get("importe") or 0)
    obs_marca = f"[ANULADO {motivo}]" if motivo else "[ANULADO]"
    fecha_rev = date.today()

    # Buscar el mov_doble original para entender cómo se pagó. El alta
    # registra tipo='gasto_simple' (pagado) o 'gasto_a_posdat' (no pagado).
    # Pero el modelo NO guarda directamente "cuenta_pagada" en xgast (la
    # tabla del legacy no la tiene); la pista vive en el concepto + en el
    # `stat` original:
    #   stat='A' o NULL  → pagado al contado: necesitamos saber si fue
    #                       caja, banco-pichincha o banco-internacional.
    #                       El módulo `concepto_parser` puede inferir
    #                       desde el concepto si lo creó el flow de caja
    #                       con side-effect; sino mirá el destino del
    #                       mov_doble original.
    #   stat='P'         → pendiente (todavía no pagado): no hay nada que
    #                       compensar bancariamente. Sólo marca anulado.
    import mov_doble as _md

    side_effects_reversados: list[str] = []
    with db.tx() as conn:
        db.execute(
            """
            UPDATE scintela.xgast
               SET stat = 'Y',
                   concepto = CASE WHEN LENGTH(COALESCE(concepto, '')) <= 80
                                   THEN COALESCE(concepto, '') || ' ' || %s
                                   ELSE concepto END,
                   usuario_modifica = %s,
                   fecha_modifica = CURRENT_TIMESTAMP
             WHERE id_xgast = %s
            """,
            (obs_marca, usuario, id_xgast),
            conn=conn,
        )

        # ── Compensación del side-effect según cómo se pagó ──────────────
        # Si el gasto era 'P' (pendiente), no hay compensación bancaria —
        # sólo se anula el registro y, si existe una posdat hermana, se
        # marca también (no es flujo actualmente usado pero defensivo).
        md_orig = db.fetch_one(
            """
            SELECT id_mov_doble, tipo, destino_table, destino_id, importe,
                   metadata
              FROM scintela.mov_doble
             WHERE origen_table = 'xgast'
               AND origen_id    = %s
               AND tipo LIKE 'gasto_%%'
               AND estado       = 'activo'
             ORDER BY id_mov_doble DESC LIMIT 1
            """,
            (id_xgast,), conn=conn,
        )

        if stat_prev == "A":
            # Gasto pagado al contado. Tratamos de inferir la cuenta de
            # pago desde concepto_parser (mismo motor que usa caja.crear).
            try:
                import concepto_parser
                concepto_orig = ""
                # Re-leer el concepto del xgast (puede tener marca [ANULADO]
                # ya, pero queremos el original sin marca).
                cr = db.fetch_one(
                    "SELECT concepto FROM scintela.xgast WHERE id_xgast = %s",
                    (id_xgast,), conn=conn,
                )
                concepto_orig = (cr or {}).get("concepto") or ""
                # Sacar la marca [ANULADO...] para parsear el original.
                concepto_orig = (concepto_orig.split("[ANULADO")[0]).strip()

                provs_validos = {
                    (r.get("codigo_prov") or "").strip().upper()
                    for r in (db.fetch_all(
                        "SELECT codigo_prov FROM scintela.proveedor"
                    ) or [])
                }
                bancos_map: dict = {}
                for b in db.fetch_all(
                    "SELECT no_banco, COALESCE(nombre, '') AS nombre "
                    "FROM scintela.banco"
                ) or []:
                    n = (b.get("nombre") or "").upper().strip()
                    if "PICHINC" in n:
                        bancos_map.setdefault("PICHINCHA", int(b["no_banco"]))
                    if "INTER" in n:
                        bancos_map.setdefault("INTERNACIONAL", int(b["no_banco"]))
                parsed = concepto_parser.parse_concepto(
                    concepto_orig,
                    {"provs_validos": provs_validos, "bancos": bancos_map},
                )
            except Exception:
                parsed = {"tipo": "none"}

            ptipo = (parsed or {}).get("tipo")
            if ptipo == "transfer_banco":
                # Pago vía banco — compensación con NC.
                import bank_helpers
                no_banco = parsed.get("no_banco")
                if no_banco:
                    bank_helpers.insert_movimiento_bancario(
                        conn,
                        no_banco=int(no_banco),
                        no_cta=None,
                        fecha=fecha_rev,
                        documento="NC",
                        importe=importe_gasto,
                        concepto=(f"REVERSO gasto #{g_row.get('num') or id_xgast}")[:50],
                        usuario=usuario,
                        stat="A",
                    )
                    side_effects_reversados.append(
                        f"banco{no_banco} +${importe_gasto:.2f}"
                    )
            elif ptipo == "compra_proveedor" or ptipo == "retiro_socio" or ptipo == "dolares":
                # Casos raros para gasto — dejamos auditado pero no
                # auto-compensamos (el usuario tiene que ir al módulo
                # específico). En la práctica los gastos no van por estas
                # vías; defensivo.
                side_effects_reversados.append(
                    f"(no auto-compensado: tipo={ptipo})"
                )
            else:
                # Default: pago en caja (gasto típico — luz, agua, etc.).
                # Compensación = ENTRADA en caja por el mismo importe.
                import caja_helpers
                caja_helpers.insert_movimiento_caja(
                    conn,
                    fecha=fecha_rev, tipo="E",
                    importe=importe_gasto,
                    concepto=(f"REVERSO gasto #{g_row.get('num') or id_xgast}")[:80],
                    clave="REV", usuario=usuario,
                )
                side_effects_reversados.append(f"caja +${importe_gasto:.2f}")

        elif stat_prev == "P":
            # Gasto pendiente (postdatado). Si hay una posdat hermana
            # (prov+num), reabrirla (banc=0) — o si ya fue pagada con
            # cheque, hay que reversar primero. La práctica típica es que
            # gastos pendientes NO tengan posdat hermana (xgast es
            # auto-contenido en el legacy), pero es defensivo.
            if g_row.get("prov") and g_row.get("num") is not None:
                posd = db.fetch_one(
                    "SELECT id_posdat, banc FROM scintela.posdat "
                    "WHERE prov = %s AND num = %s "
                    "  AND (anulada IS NOT TRUE OR anulada IS NULL)",
                    (g_row["prov"], g_row["num"]),
                    conn=conn,
                )
                if posd:
                    if (posd.get("banc") or 0) != 0:
                        raise ValueError(
                            f"El gasto pendiente tiene posdat hermana "
                            f"pagada con cheque (banc={posd['banc']}). "
                            f"Reversá el cheque emitido primero."
                        )
                    # Si banc=0, ya está abierta — no hay nada que reabrir.
                    side_effects_reversados.append("posdat (ya abierta)")

        # Registrar el mov_doble del reverso. R2: NO suprimir — si falla,
        # abortamos la anulación entera.
        _md.registrar(
            conn=conn,
            tipo="reverso_gasto_anulado",
            origen_table="xgast",
            origen_id=id_xgast,
            destino_table="xgast",
            destino_id=id_xgast,
            importe=float((md_orig or {}).get("importe") or importe_gasto),
            fecha=g_row.get("fecha") or fecha_rev,
            concepto=(
                f"ANULACION gasto #{g_row.get('num') or id_xgast}"
                + (f" — {motivo}" if motivo else "")
                + (f" [{', '.join(side_effects_reversados)}]"
                   if side_effects_reversados else "")
            )[:200],
            usuario=usuario,
            metadata={"motivo": motivo or "",
                      "id_xgast": id_xgast,
                      "doc": g_row.get("doc"),
                      "stat_previo": stat_prev,
                      "side_effects_reversados": side_effects_reversados},
            id_original=(md_orig or {}).get("id_mov_doble"),
        )
    return {
        "id_xgast": id_xgast,
        "stat_previo": stat_prev,
        "stat_nuevo": "Y",
        "side_effects_reversados": side_effects_reversados,
    }


def desclasificar(
    id_xgast: int,
    *,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Deshace una clasificación de caja_s → xgast SIN tocar la caja S.

    Caso de uso: la dueña clasificó un egreso de caja como V9 (varios admin)
    pero después se dio cuenta que era V5 (combustible). El reverso correcto
    NO es anular la caja S — ese egreso es real, salió plata. Es desclasificar
    el xgast (anularlo) y devolver la caja S al pool de "pendientes de
    clasificar" para que la dueña la asigne a la categoría correcta vía
    /gastos/clasificar/<id_caja>.

    Acciones atómicas:
      1. Encuentra el mov_doble `caja_s_to_xgast` que linkea esta xgast.
      2. Marca xgast.stat = 'Y' (soft delete, preserva auditoría).
      3. Registra reverso mov_doble linkeado al original — el INSERT marca
         automáticamente al mov_doble original como `estado='reversado'` +
         `id_reverso`. Como el queries.clasificar_desde_caja filtra
         `estado='activo'`, la caja vuelve a aparecer como huérfana.

    NO toca scintela.caja — la fila S permanece (representa plata que sí
    salió de caja). Sólo deshacemos la "etiqueta" de gasto.

    Si el gasto NO viene de clasificación (no hay mov_doble caja_s_to_xgast),
    levanta ValueError — usar `anular()` en su lugar.

    TMT 2026-05-16 — handler para el único tipo huérfano del dispatcher
    de reverso. Antes la dueña tenía que ir a la DB a mano.
    """
    motivo = (motivo or "").strip()
    g_row = db.fetch_one(
        """
        SELECT id_xgast, num, doc, concepto, importe, stat, fecha
          FROM scintela.xgast WHERE id_xgast = %s
        """,
        (id_xgast,),
    )
    if not g_row:
        raise ValueError(f"Gasto id={id_xgast} no existe.")
    if (g_row.get("stat") or "").upper() == "Y":
        raise ValueError("El gasto ya está anulado.")

    md_orig = db.fetch_one(
        """
        SELECT id_mov_doble, origen_id, importe
          FROM scintela.mov_doble
         WHERE destino_table = 'xgast'
           AND destino_id    = %s
           AND tipo          = 'caja_s_to_xgast'
           AND estado        = 'activo'
         ORDER BY id_mov_doble DESC
         LIMIT 1
        """,
        (id_xgast,),
    )
    if not md_orig:
        raise ValueError(
            f"Gasto id={id_xgast} no es una clasificación de caja. "
            f"Para anular este gasto usá /gastos/{id_xgast}/anular."
        )

    import mov_doble as _md
    obs_marca = (f"[DESCLASIFICADO {motivo}]" if motivo
                 else "[DESCLASIFICADO]")
    importe_md = float(md_orig.get("importe")
                       or g_row.get("importe") or 0)
    id_caja_origen = int(md_orig["origen_id"])

    with db.tx() as conn:
        db.execute(
            """
            UPDATE scintela.xgast
               SET stat = 'Y',
                   concepto = CASE WHEN LENGTH(COALESCE(concepto, '')) <= 80
                                   THEN COALESCE(concepto, '') || ' ' || %s
                                   ELSE concepto END,
                   usuario_modifica = %s,
                   fecha_modifica = CURRENT_TIMESTAMP
             WHERE id_xgast = %s
            """,
            (obs_marca, usuario, id_xgast),
            conn=conn,
        )
        _md.registrar(
            conn=conn,
            tipo="reverso_caja_s_to_xgast",
            origen_table="xgast",
            origen_id=id_xgast,
            destino_table="caja",
            destino_id=id_caja_origen,
            importe=importe_md,
            fecha=date.today(),
            concepto=(
                f"DESCLASIFICAR xgast #{id_xgast} "
                f"(V{g_row.get('num') or '?'}) ← caja #{id_caja_origen}"
                + (f" — {motivo}" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "motivo":   motivo or "",
                "id_caja":  id_caja_origen,
                "num_v":    g_row.get("num"),
                "concepto_original": g_row.get("concepto"),
            },
            id_original=int(md_orig["id_mov_doble"]),
        )

    return {
        "id_xgast":      id_xgast,
        "id_caja":       id_caja_origen,
        "stat_previo":   (g_row.get("stat") or "").upper(),
        "stat_nuevo":    "Y",
        "num_v_previo":  g_row.get("num"),
    }
