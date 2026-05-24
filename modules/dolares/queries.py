"""Queries de scintela.dolares — anticipos en USD."""
from datetime import date

import db
from periodo_guard import asegurar_fecha_abierta

# Alias retro-compat para sentinels (sortKey usa _date.min).
_date = date


def lista(
    desde: str | None = None,
    hasta: str | None = None,
    cta: str | None = None,
    solo_vivos: bool = True,
    limite: int = 1000,
) -> list[dict]:
    """Movimientos en scintela.dolares (anticipos USD).

    Cada fila tiene: fecha, cta (3-char), concepto, importe, st, clave,
    saldo_acumulado (running por cuenta).

    Convención del campo `st`:
        NULL / '' / ' ' → anticipo VIVO (suma a ANTICIPOS del balance).
        cualquier otra letra → aplicado/cancelado/convertido.

    `solo_vivos=True` filtra a los anticipos abiertos.

    Saldo acumulado (TMT 2026-05-12): running balance POR CUENTA, calculado
    sobre el universo filtrado. Si filtrás por cuenta, ves su corrida; si
    no, cada fila muestra el saldo de SU cuenta hasta ese movimiento.
    """
    rows = db.fetch_all(
        """
        SELECT d.id_dolares, d.fecha, d.cta, d.concepto, d.importe,
               d.st, d.clave, d.usuario_crea
        FROM scintela.dolares d
        WHERE (%(desde)s::date IS NULL OR d.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR d.fecha <= %(hasta)s::date)
          AND (%(cta)s IS NULL OR UPPER(d.cta) = UPPER(%(cta)s))
          AND (NOT %(solo_vivos)s
               OR d.st IS NULL OR d.st IN ('', ' '))
        ORDER BY d.fecha DESC, d.id_dolares DESC
        LIMIT %(limite)s
        """,
        {
            "desde": desde or None, "hasta": hasta or None,
            "cta": cta or None,
            "solo_vivos": bool(solo_vivos),
            "limite": int(limite),
        },
    ) or []

    # Running balance por cuenta: ordenar ASC, acumular, marcar cada fila,
    # devolver DESC (que es el orden original). Bug TMT 2026-05-12: el
    # fallback `or ""` mezclaba date con string y rompía el sort —
    # `_date.min` mantiene el tipo consistente.
    rows_asc = sorted(rows, key=lambda r: (r.get("fecha") or _date.min,
                                            r.get("id_dolares") or 0))
    acum_por_cta: dict[str, float] = {}
    for r in rows_asc:
        cta_key = (r.get("cta") or "").strip().upper()
        acum_por_cta[cta_key] = acum_por_cta.get(cta_key, 0.0) + float(r.get("importe") or 0)
        r["saldo_acumulado"] = acum_por_cta[cta_key]
    # Volver al orden DESC.
    return list(reversed(rows_asc))


def por_cuenta(solo_vivos: bool = True) -> list[dict]:
    """Anticipos agregados por cuenta (cliente).

    Devuelve una fila por cuenta con: cta, total_vivo, total_aplicado,
    n_vivos, n_aplicados, ult_fecha (último movimiento), primer_fecha
    (anticipo más antiguo aún vivo). Ordenado por total_vivo DESC para
    que el cliente con más saldo aparezca primero.

    `solo_vivos=False` incluye cuentas que sólo tienen movimientos
    aplicados (útil para vista histórica).
    """
    return db.fetch_all(
        """
        SELECT UPPER(TRIM(cta)) AS cta,
               COALESCE(SUM(CASE WHEN COALESCE(st,'') IN ('', ' ')
                                 THEN importe ELSE 0 END), 0)        AS total_vivo,
               COALESCE(SUM(CASE WHEN COALESCE(st,'') NOT IN ('', ' ')
                                 THEN importe ELSE 0 END), 0)        AS total_aplicado,
               SUM(CASE WHEN COALESCE(st,'') IN ('', ' ') THEN 1 ELSE 0 END) AS n_vivos,
               SUM(CASE WHEN COALESCE(st,'') NOT IN ('', ' ') THEN 1 ELSE 0 END) AS n_aplicados,
               MAX(fecha)                                            AS ult_fecha,
               MIN(CASE WHEN COALESCE(st,'') IN ('', ' ')
                        THEN fecha END)                              AS primer_vivo
        FROM scintela.dolares
        WHERE cta IS NOT NULL AND TRIM(cta) <> ''
        GROUP BY UPPER(TRIM(cta))
        HAVING NOT %(solo_vivos)s
            OR SUM(CASE WHEN COALESCE(st,'') IN ('', ' ') THEN 1 ELSE 0 END) > 0
        ORDER BY total_vivo DESC, cta ASC
        """,
        {"solo_vivos": bool(solo_vivos)},
    )


def resumen() -> dict:
    """Totales para los KPIs.

    Devuelve:
        total_vivos: Σ importe WHERE st vacío (= ANTICIPOS del balance).
        n_vivos: cantidad de anticipos vivos.
        total_aplicados: Σ importe WHERE st no vacío.
        n_aplicados: cantidad de aplicados/cancelados.
        n_ctas_vivos: cantidad distinta de cta con anticipos vivos.
    """
    row = db.fetch_one(
        """
        SELECT
            COALESCE(SUM(CASE WHEN COALESCE(st,'') IN ('', ' ')
                              THEN importe ELSE 0 END), 0)        AS total_vivos,
            SUM(CASE WHEN COALESCE(st,'') IN ('', ' ') THEN 1 ELSE 0 END) AS n_vivos,
            COALESCE(SUM(CASE WHEN COALESCE(st,'') NOT IN ('', ' ')
                              THEN importe ELSE 0 END), 0)        AS total_aplicados,
            SUM(CASE WHEN COALESCE(st,'') NOT IN ('', ' ') THEN 1 ELSE 0 END) AS n_aplicados,
            COUNT(DISTINCT CASE WHEN COALESCE(st,'') IN ('', ' ')
                                THEN UPPER(cta) END)              AS n_ctas_vivos
        FROM scintela.dolares
        """
    )
    if not row:
        return {"total_vivos": 0.0, "n_vivos": 0, "total_aplicados": 0.0,
                "n_aplicados": 0, "n_ctas_vivos": 0}
    return {
        "total_vivos":      float(row["total_vivos"] or 0),
        "n_vivos":          int(row["n_vivos"] or 0),
        "total_aplicados":  float(row["total_aplicados"] or 0),
        "n_aplicados":      int(row["n_aplicados"] or 0),
        "n_ctas_vivos":     int(row["n_ctas_vivos"] or 0),
    }


# ──────────────────────────────────────────────────────────────────────────
# BAP — conversión lote anticipos USD → compra (replica BANCOS.PRG:733-819)
#
# Lógica dBase: la dueña entra un proveedor (CTA), se listan todos los
# anticipos con st='' (vivos), suma BB, y si dice S, INSERTA una compra
# con `comprobante=NUMER`, marca todos los anticipos como st='B'. Acá:
#   - `anticipos_pendientes_por_proveedor()` lista por codigo_prov.
#   - `convertir_a_compra()` ejecuta la conversión atómica.
# ──────────────────────────────────────────────────────────────────────────

def anticipos_pendientes_por_proveedor(
    tipos_filter: list[str] | None = None,
) -> list[dict]:
    """Agrupa anticipos vivos (`st` vacío) por codigo_prov.

    Replica el filtro `&SF CTA=PRO .AND. ST=' '` de BANCOS.PRG:759 y la
    sumatoria `&SAI BB` para mostrar el total por proveedor.

    TMT 2026-05-20 — `tipos_filter` opcional: lista de letras de
    `scintela.proveedor.tipo` para filtrar (ej: `['U']` para maquinaria,
    `['H']` para hilado). Sin filtro: devuelve todos los que tengan
    anticipos vivos. Si un cta no existe en scintela.proveedor (raro),
    cae afuera si hay filtro.

    Devuelve lista [{codigo_prov, total_usd, n_anticipos, ult_fecha,
                      primer_fecha, nombre, tipo}] ordenada por total_usd DESC.
    """
    tipos_norm = None
    if tipos_filter:
        tipos_norm = [t.strip().upper()[:1] for t in tipos_filter if t and t.strip()]
        tipos_norm = [t for t in tipos_norm if t]
    return db.fetch_all(
        """
        SELECT UPPER(TRIM(d.cta))       AS codigo_prov,
               COALESCE(SUM(d.importe), 0)  AS total_usd,
               COUNT(*)                     AS n_anticipos,
               MAX(d.fecha)                 AS ult_fecha,
               MIN(d.fecha)                 AS primer_fecha,
               MAX(COALESCE(p.nombre, ''))  AS nombre,
               MAX(COALESCE(p.tipo, ''))    AS tipo
          FROM scintela.dolares d
          LEFT JOIN scintela.proveedor p
                 ON UPPER(TRIM(p.codigo_prov)) = UPPER(TRIM(d.cta))
         WHERE (d.st IS NULL OR d.st IN ('', ' '))
           AND d.cta IS NOT NULL AND TRIM(d.cta) <> ''
           AND (%(tipos_norm)s::text[] IS NULL
                OR UPPER(COALESCE(p.tipo, '')) = ANY(%(tipos_norm)s::text[]))
         GROUP BY UPPER(TRIM(d.cta))
         HAVING COUNT(*) > 0
         ORDER BY total_usd DESC, codigo_prov ASC
        """,
        {"tipos_norm": tipos_norm},
    ) or []


def anticipos_pendientes_de_proveedor(codigo_prov: str) -> list[dict]:
    """Anticipos vivos de UN proveedor (para el form de selección)."""
    codigo_prov = (codigo_prov or "").strip().upper()
    return db.fetch_all(
        """
        SELECT id_dolares, fecha, importe, concepto, clave, st
          FROM scintela.dolares
         WHERE UPPER(TRIM(cta)) = %s
           AND (st IS NULL OR st IN ('', ' '))
         ORDER BY fecha ASC, id_dolares ASC
        """,
        (codigo_prov,),
    ) or []


def convertir_a_compra(
    *,
    codigo_prov: str,
    ids_anticipos: list[int],
    fecha=None,
    concepto: str = "",
    tipo_compra: str = "H",
    kg=None,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Convierte un lote de anticipos vivos a una compra (BAP).

    Replica BANCOS.PRG:803-816:
        - Suma los `importe` de los anticipos seleccionados.
        - `REPLA ALL ST WITH 'B'`  → marca los anticipos como consumidos.
        - `INSERT compra` con `IMPORTE=BB, KG=KK, CONCEPTO=NUMER`.

    En PG:
        1. Validar proveedor + IDs (todos del mismo proveedor, vivos).
        2. Sumar importes.
        3. Marcar anticipos seleccionados con `st='B'` (consumidos por BAP) —
           paridad exacta con dBase BANCOS.PRG:803-816. TMT 2026-05-15
           (decisión #8): antes habíamos puesto `'X'` para alinear con el
           vocabulario interno, pero la dueña pidió volver a `'B'` porque
           rompía la lectura cruzada con dBase y con scripts de auditoría
           que comparan dBase ↔ PC.
        4. Crear compra:
            - `comprobante = 'BAP<seq>'` con seq = MAX(numero)+1 sobre BAP.
            - `cuenta_pagada = 'A'` (pagada con anticipo previo).
            - `concepto` heredado del form (texto libre, ej. el N° de
              factura del proveedor).
            - sin posdat hermana (no hay deuda — ya estaba pagado).
        5. mov_doble `bap_anticipo_a_compra` con metadata de los IDs.
        Atómico vía `db.tx()`.

    Devuelve `{id_compra, numero, comprobante, importe_total, n_anticipos}`.
    """
    fecha = fecha or date.today()
    asegurar_fecha_abierta(fecha)

    codigo_prov = (codigo_prov or "").strip().upper()
    if not codigo_prov:
        raise ValueError("Código de proveedor requerido.")
    if not ids_anticipos:
        raise ValueError("Seleccioná al menos un anticipo.")

    ids_unique = sorted({int(i) for i in ids_anticipos if i})
    if not ids_unique:
        raise ValueError("IDs de anticipos inválidos.")

    tipo_norm = (tipo_compra or "H").upper().strip()[:1]
    if tipo_norm not in ("H", "K", "Q", "C", "A"):
        # `A` (anticipo) no tiene sentido como destino — ya estamos
        # convirtiendo desde anticipo. Default razonable: 'H' (hilado),
        # que es el caso más común para BAP en la fábrica.
        tipo_norm = "H"

    with db.tx() as conn:
        # TMT 2026-05-15 (re-audit C3): advisory lock para serializar la
        # asignación del seq BAP entre transacciones concurrentes. Sin esto,
        # dos `convertir_a_compra` simultáneas pueden generar el mismo
        # comprobante (no hay UNIQUE en compra.comprobante).
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtext('bap_seq_compra'))",
            conn=conn,
        )

        # 1) Validar proveedor.
        prov_row = db.fetch_one(
            "SELECT id_proveedor, COALESCE(nombre,'') AS nombre, tipo "
            "FROM scintela.proveedor WHERE codigo_prov = %s",
            (codigo_prov,), conn=conn,
        )
        if not prov_row:
            raise ValueError(f"Proveedor {codigo_prov!r} no existe.")

        # 2) Validar IDs: existen, son del mismo proveedor, todos vivos.
        # TMT 2026-05-15 (re-audit H3): FOR UPDATE para serializar contra
        # otra `convertir_a_compra` que apunte a los mismos ids. Orden
        # estable (id ASC) → no deadlock con otros consumidores.
        placeholder = ",".join(["%s"] * len(ids_unique))
        rows = db.fetch_all(
            f"""
            SELECT id_dolares, cta, importe, st, fecha
              FROM scintela.dolares
             WHERE id_dolares IN ({placeholder})
             ORDER BY id_dolares
             FOR UPDATE
            """,
            tuple(ids_unique), conn=conn,
        ) or []
        if len(rows) != len(ids_unique):
            faltan = set(ids_unique) - {int(r["id_dolares"]) for r in rows}
            raise ValueError(
                f"No encontré los anticipos: {sorted(faltan)}."
            )
        for r in rows:
            cta_r = (r.get("cta") or "").strip().upper()
            if cta_r != codigo_prov:
                raise ValueError(
                    f"Anticipo id={r['id_dolares']} es del proveedor "
                    f"{cta_r!r}, no {codigo_prov!r}."
                )
            st_r = (r.get("st") or "").strip()
            if st_r:
                raise ValueError(
                    f"Anticipo id={r['id_dolares']} ya está consumido "
                    f"(st='{st_r}')."
                )

        importe_total = sum(float(r.get("importe") or 0) for r in rows)
        if importe_total <= 0:
            raise ValueError(
                f"Total de anticipos seleccionados es 0 o negativo: "
                f"{importe_total:.2f}."
            )

        # 3) Marcar anticipos como consumidos con st='B' — paridad dBase
        # BANCOS.PRG:803-816 (`REPLA ALL ST WITH 'B'`).
        # TMT 2026-05-15 (decisión #8): revertido de 'X' a 'B' para mantener
        # paridad exacta con dBase. Los listados/reportes filtran por
        # `st IS NULL OR st = ''` para "vivos", así que cualquier valor
        # no-vacío marca consumido — pero la dueña usa la base con dBase y
        # con scripts de audit cross-DB que esperan 'B'.
        db.execute(
            f"""
            UPDATE scintela.dolares
               SET st = 'B',
                   usuario_modifica = %s,
                   fecha_modifica = CURRENT_TIMESTAMP
             WHERE id_dolares IN ({placeholder})
            """,
            (usuario[:50], *ids_unique),
            conn=conn,
        )

        # 4) Próximo número de compra y comprobante BAP.
        row_n = db.fetch_one(
            "SELECT COALESCE(MAX(numero), 0) + 1 AS siguiente FROM scintela.compra",
            conn=conn,
        )
        numero_compra = int(row_n["siguiente"]) if row_n else 1

        # Seq BAP. TMT 2026-05-15 (re-audit C3): la versión anterior usaba
        # COUNT(*) WHERE comprobante LIKE 'BAP%' — pero COUNT no es monótono
        # (si borrás un BAP, el próximo colisiona). Ahora extraemos el MAX
        # numérico real del sufijo. El advisory_xact_lock al inicio de la
        # tx ya garantiza serialización entre transacciones concurrentes.
        row_bap = db.fetch_one(
            """
            SELECT COALESCE(
                MAX(NULLIF(regexp_replace(comprobante, '^BAP', ''), '')::int),
                0
            ) AS maxseq
            FROM scintela.compra
            WHERE comprobante ~ '^BAP[0-9]+$'
            """,
            conn=conn,
        )
        seq_bap = int(row_bap.get("maxseq") or 0) + 1 if row_bap else 1
        comprobante = f"BAP{seq_bap}"[:20]

        # Plazo del proveedor para fechad (default 30 días).
        fechad_compra = fecha  # BAP ya está pagado; fechad = hoy es ok.

        concepto_compra = (
            concepto.strip()
            or f"BAP {codigo_prov} ({len(rows)} anticipos)"
        )[:50]

        compra = db.execute_returning(
            """
            INSERT INTO scintela.compra
                (fecha, id_proveedor, codigo_prov, tipo, comprobante,
                 kg, importe, numero, fecha_ing, fechad, concepto,
                 usuario_crea, cuenta_pagada, observacion)
            VALUES (%s, %s, %s, %s, %s,
                    %s, %s, %s, CURRENT_DATE, %s, %s,
                    %s, 'A', %s)
            RETURNING id_compra, numero
            """,
            (
                fecha, prov_row["id_proveedor"], codigo_prov,
                tipo_norm, comprobante,
                kg, importe_total, numero_compra,
                fechad_compra, concepto_compra,
                usuario[:50],
                (
                    f"BAP — consumió {len(rows)} anticipos USD "
                    f"(ids: {','.join(str(i) for i in ids_unique[:20])}"
                    f"{'…' if len(ids_unique) > 20 else ''})."
                    + (f" Motivo: {motivo[:100]}" if motivo else "")
                )[:200],
            ),
            conn=conn,
        ) or {}

        # 5) mov_doble bap_anticipo_a_compra. Origen: el primer anticipo
        # (representativo); destino: la compra creada. Metadata guarda
        # todos los ids.
        import mov_doble as _md
        primer_id_dolar = ids_unique[0]
        _md.registrar(
            conn=conn,
            tipo="bap_anticipo_a_compra",
            origen_table="dolares",
            origen_id=primer_id_dolar,
            destino_table="compra",
            destino_id=compra.get("id_compra"),
            importe=importe_total,
            fecha=fecha,
            concepto=(
                f"BAP {codigo_prov}: {len(rows)} anticipo(s) → compra "
                f"#{compra.get('numero')} ({comprobante})"
                + (f" — {motivo}" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "codigo_prov": codigo_prov,
                "ids_anticipos": ids_unique,
                "n_anticipos": len(rows),
                "importe_total": importe_total,
                "numero_compra": compra.get("numero"),
                "comprobante": comprobante,
                "tipo_compra": tipo_norm,
                "motivo": motivo or "",
            },
        )

    return {
        "id_compra":     compra.get("id_compra"),
        "numero_compra": compra.get("numero"),
        "comprobante":   comprobante,
        "codigo_prov":   codigo_prov,
        "importe_total": importe_total,
        "n_anticipos":   len(rows),
        "ids_anticipos": ids_unique,
        "tipo_compra":   tipo_norm,
    }
