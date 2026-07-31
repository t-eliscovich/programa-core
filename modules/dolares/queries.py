"""Queries de scintela.dolares — anticipos en USD."""
from datetime import date

import db
from filters import today_ec
from periodo_guard import asegurar_fecha_abierta

# Alias retro-compat para sentinels (sortKey usa _date.min).
_date = date


def lista(
    desde: str | None = None,
    hasta: str | None = None,
    cta: str | None = None,
    solo_vivos: bool = True,
    limite: int = 1000,
    q: str | None = None,
) -> list[dict]:
    """Movimientos en scintela.dolares (anticipos USD).

    Cada fila tiene: fecha, cta (3-char), concepto, importe, st, clave,
    saldo_acumulado (running por cuenta).

    Convención del campo `st`:
        NULL / '' / ' ' → anticipo VIVO (suma a ANTICIPOS del balance).
        cualquier otra letra → aplicado/cancelado/convertido.

    `solo_vivos=True` filtra a los anticipos abiertos.

    `q` (TMT 2026-06-09, pedido Tamara): filtro por CONCEPTO, substring
    case-insensitive (ILIKE).

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
          AND (%(q)s IS NULL
               OR d.concepto ILIKE '%%' || %(q)s || '%%')
          AND (NOT %(solo_vivos)s
               OR d.st IS NULL OR d.st IN ('', ' '))
        ORDER BY d.fecha DESC, d.id_dolares DESC
        LIMIT %(limite)s
        """,
        {
            "desde": desde or None, "hasta": hasta or None,
            "cta": cta or None,
            "q": (q or "").strip() or None,
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


def anticipos_vivos(tipos_filter: list[str] | None = None) -> list[dict]:
    """Todos los anticipos vivos (`st` vacío) de TODOS los proveedores.

    Pensada para la conciliación split-screen (/dolares/convertir-lote): en
    vez de agrupar por proveedor y navegar de a uno, devolvemos el detalle de
    cada anticipo vivo para pintarlos todos juntos y filtrarlos en el cliente.

    Cada fila: id_dolares, fecha, cta (= codigo_prov, 3 chars), concepto,
    importe, clave, nombre (del proveedor).

    `tipos_filter` (ej. ['H'] hilado) filtra por `scintela.proveedor.tipo`,
    mismo criterio que `anticipos_pendientes_por_proveedor`. Sin filtro:
    todos los anticipos vivos.
    """
    tipos_norm = None
    if tipos_filter:
        tipos_norm = [t.strip().upper()[:1] for t in tipos_filter if t and t.strip()]
        tipos_norm = [t for t in tipos_norm if t]
    return db.fetch_all(
        """
        SELECT d.id_dolares, d.fecha, UPPER(TRIM(d.cta)) AS cta,
               d.concepto, d.importe, d.clave,
               MAX(COALESCE(p.nombre, '')) OVER (
                   PARTITION BY UPPER(TRIM(d.cta))
               ) AS nombre
          FROM scintela.dolares d
          LEFT JOIN scintela.proveedor p
                 ON UPPER(TRIM(p.codigo_prov)) = UPPER(TRIM(d.cta))
         WHERE (d.st IS NULL OR d.st IN ('', ' '))
           AND d.cta IS NOT NULL AND TRIM(d.cta) <> ''
           AND (%(tipos_norm)s::text[] IS NULL
                OR UPPER(COALESCE(p.tipo, '')) = ANY(%(tipos_norm)s::text[]))
         ORDER BY UPPER(TRIM(d.cta)) ASC, d.fecha ASC, d.id_dolares ASC
        """,
        {"tipos_norm": tipos_norm},
    ) or []


def tipos_por_cuenta(codigos: list[str]) -> dict[str, str]:
    """Mapa cuenta (codigo_prov) → tipo de proveedor, en una sola query.

    Se usa para marcar en la lista de anticipos qué cuentas son de químicos
    (`tipo='Q'`) sin un N+1. Las cuentas sin proveedor quedan fuera del mapa.
    """
    codigos = sorted({(c or "").strip().upper() for c in codigos if c and c.strip()})
    if not codigos:
        return {}
    rows = db.fetch_all(
        """
        SELECT UPPER(TRIM(codigo_prov))            AS cta,
               UPPER(TRIM(COALESCE(tipo, '')))     AS tipo
          FROM scintela.proveedor
         WHERE UPPER(TRIM(codigo_prov)) = ANY(%s)
        """,
        (codigos,),
    ) or []
    return {r["cta"]: r["tipo"] for r in rows if r.get("cta")}


def es_proveedor_quimico(codigo_prov: str) -> bool:
    """True si el proveedor es de químicos (`scintela.proveedor.tipo='Q'`).

    Fuente de verdad para tipar la compra (Q vs H) en la conversión desde la
    barra de selección — NO se confía en el frente para decidir el tipo.
    """
    if not codigo_prov or not codigo_prov.strip():
        return False
    return tipos_por_cuenta([codigo_prov]).get(codigo_prov.strip().upper()) == "Q"


def _etiqueta_anticipos(rows: list[dict], codigo_prov: str) -> str:
    """Cómo se llama esto para un humano: "AC 22", "AC 22/28", o "AC".

    TMT 2026-07-31 (dueña: *"acá nada dice AC 22, ¿cómo sé qué anticipo es?"*).
    El historial decía *"BAP AC: 3 anticipo(s) → compra #10117 (BAP22)"*: no
    nombraba el anticipo por ningún lado, y encima el comprobante "BAP22" se
    lee igual que el concepto "AC 22" sin tener nada que ver. Ahora la línea
    empieza por el nombre con el que la fábrica llama a esa importación.

    El concepto del anticipo es texto libre con el número adelante ("22 SALDO",
    "22 CAE", "58/26 SALDO"): se toma ese prefijo.
    """
    import re as _re

    refs = []
    for r in rows or []:
        m = _re.match(r"\s*(\d{1,6})", str(r.get("concepto") or ""))
        if m and m.group(1) not in refs:
            refs.append(m.group(1))
    prov = (codigo_prov or "").strip().upper()
    if not refs:
        return prov
    refs.sort(key=lambda x: int(x))
    return f"{prov} {'/'.join(refs[:4])}" + ("…" if len(refs) > 4 else "")


def _anticipos_sin_recepcion(rows: list[dict], codigo_prov: str) -> list[str]:
    """De los anticipos que se van a convertir, cuáles son de mercadería que
    Asinfo TODAVÍA NO marcó recibida.

    Devuelve una lista de etiquetas legibles ("AC 22 · IM-0000578") para
    ponerlas en el mensaje. Lista vacía = todo llegó, o no se pudo averiguar.

    **Fail-OPEN a propósito**: si Asinfo no contesta, no bloquea. Un puente
    caído no puede frenarle el trabajo a nadie; el costo de dejar pasar una es
    mucho menor que el de trabar la carga de todo el día. TMT 2026-07-31.
    """
    try:
        from modules.importaciones import service as _imp_svc

        filas = [dict(r, cta=codigo_prov) for r in (rows or [])]
        _imp_svc.adjuntar_recepcion_asinfo(filas)
        # Si NINGUNA enganchó con Asinfo, el cruce no funcionó — no es que la
        # mercadería no haya llegado. No bloquear por eso.
        if not any(f.get("im_numero") for f in filas):
            return []
        faltan = []
        for f in filas:
            if f.get("im_numero") and not f.get("fecha_recepcion_im"):
                _ref = f.get("ref")
                faltan.append(
                    f"{codigo_prov} {_ref if _ref is not None else '?'}"
                    f" · {f.get('im_numero')}"
                )
        # Sin repetidos y en orden estable, que el mensaje se lea.
        return sorted(set(faltan))
    except Exception:  # noqa: BLE001 -- fail-open: nunca frenar por el puente
        return []


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
    permitir_sin_recibir: bool = False,
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
    fecha = fecha or today_ec()
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
            SELECT id_dolares, cta, importe, st, fecha, concepto
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

        # ── GUARD: no convertir mercadería que TODAVÍA NO LLEGÓ ────────────
        # TMT 2026-07-31 (dueña: *"¿podemos, la próxima que cargue compra sin
        # recibir, avisarle a Andrés?"*).
        #
        # Qué pasó: se convirtieron los anticipos de AC 22 (IM-0000578) —
        # 53.860,75 + 3.545,52 — cuando esa importación NO figura recibida en
        # Asinfo: la mercadería está navegando. La conversión saca el anticipo
        # del activo y crea una compra PAGADA, así que no nace ningún pasivo y
        # no entra ni un kilo al stock. El patrimonio bajó 53.861 contra nada
        # — una pérdida ficticia que costó media tarde encontrar.
        #
        # El anticipo de algo en tránsito es un activo legítimo y tiene que
        # seguir siéndolo hasta que la mercadería llegue. La conversión
        # AUTOMÁTICA ya respeta esta regla (sólo mira recepciones); ésta es la
        # misma regla para el camino manual.
        #
        # Se puede forzar (`permitir_sin_recibir=True`) porque hay casos
        # legítimos —una importación vieja que Asinfo nunca marcó, un cruce que
        # no engancha— pero deja de ser silencioso: queda en Novedades.
        _sin_recibir = _anticipos_sin_recepcion(rows, codigo_prov)
        if _sin_recibir and not permitir_sin_recibir:
            raise ValueError(
                "Esta mercadería todavía no llegó, así que el anticipo tiene "
                "que seguir siendo un anticipo: convertirlo ahora borra el "
                "activo y baja la utilidad sin que haya pasado nada "
                f"({', '.join(_sin_recibir)}). Si igual hay que cargarla, "
                "tildá «convertir aunque no haya llegado» y queda anotado en "
                "Novedades."
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

        # Cómo lo lee un humano: "AC 22", no "BAP18". TMT 2026-07-31.
        _etiqueta = _etiqueta_anticipos(rows, codigo_prov)

        concepto_compra = (
            concepto.strip()
            or f"{_etiqueta} ({len(rows)} anticipos)"
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
                    f"{_etiqueta} — consumió {len(rows)} anticipos USD "
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
            # La línea del historial. Empieza por el nombre que usa la fábrica
            # ("AC 22") y NO lleva el comprobante, que se confundía con él.
            # TMT 2026-07-31 (dueña: "acá nada dice AC 22, ¿cómo sé qué
            # anticipo es?" + "dejá de usar BAP que nadie sabe qué es").
            concepto=(
                f"{_etiqueta} · {len(rows)} anticipo(s) → compra "
                f"#{compra.get('numero')}"
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

    # Si se forzó sobre mercadería que no llegó, que NO quede en silencio: el
    # que lo mire mañana tiene que poder ver por qué bajó la utilidad sin que
    # entrara un kilo. Fuera de la transacción y fail-soft: un aviso no puede
    # tumbar una conversión que ya se hizo. TMT 2026-07-31.
    if _sin_recibir:
        try:
            from modules.avisos import queries as _avisos
            _avisos.avisar(
                fuente="importaciones",
                nivel="alerta",
                titulo=(f"{codigo_prov} · $ {importe_total:,.2f} · "
                        f"se cargó a compras SIN que llegue la mercadería"),
                detalle=(
                    f"{', '.join(_sin_recibir)} todavía no figura recibida en "
                    "Asinfo. El anticipo salió del activo y no entró stock en "
                    "su lugar, así que la utilidad bajó ese importe sin que "
                    f"pasara nada. Lo cargó {usuario}. Si fue un error, se "
                    "deshace desde el anticipo."
                ),
                importe=importe_total,
                url="/importaciones?recep=pendiente",
                clave=f"convertido-sin-recibir:{compra.get('id_compra')}",
            )
        except Exception:  # noqa: BLE001 -- el aviso nunca rompe la operación
            pass

    return {
        "id_compra":     compra.get("id_compra"),
        "numero_compra": compra.get("numero"),
        "comprobante":   comprobante,
        "codigo_prov":   codigo_prov,
        "importe_total": importe_total,
        "n_anticipos":   len(rows),
        "ids_anticipos": ids_unique,
        "tipo_compra":   tipo_norm,
        "sin_recibir":   _sin_recibir,
    }


def reversar_conversion(id_mov_doble: int, *, motivo: str = "", usuario: str = "web") -> dict:
    """Deshace una conversión BAP (anticipo USD → compra). Inverso EXACTO de
    convertir_a_compra():

      1. Restaura los anticipos consumidos a "vivo" (st='B' → st='').
      2. Elimina la compra BAP que se había creado.
      3. Registra el reverso en mov_doble y marca el original como reversado.

    Lee los ids de los anticipos y la compra desde el mov_doble original
    (tipo='bap_anticipo_a_compra'). Atómico. Idempotente-seguro: si la compra
    ya no existe (p. ej. un sync la borró) avisa con ValueError claro.
    """
    import json as _json

    md = db.fetch_one(
        "SELECT id_mov_doble, tipo, destino_id, estado, metadata "
        "FROM scintela.mov_doble WHERE id_mov_doble = %s",
        (id_mov_doble,),
    )
    if not md:
        raise ValueError("Movimiento no encontrado.")
    if (md.get("tipo") or "") != "bap_anticipo_a_compra":
        raise ValueError(
            f"Este movimiento no es una conversión BAP (tipo={md.get('tipo')!r})."
        )
    if (md.get("estado") or "") in ("reverso", "reversado"):
        raise ValueError("Esta conversión ya está reversada.")

    meta = md.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    ids = [int(x) for x in (meta.get("ids_anticipos") or []) if x]
    id_compra = md.get("destino_id")
    if not id_compra:
        raise ValueError("No encuentro la compra creada por la conversión.")

    with db.tx() as conn:
        compra = db.fetch_one(
            "SELECT id_compra, comprobante, importe, codigo_prov "
            "FROM scintela.compra WHERE id_compra = %s FOR UPDATE",
            (id_compra,), conn=conn,
        )
        if not compra:
            raise ValueError(
                f"La compra #{id_compra} ya no existe (¿la borró un sync, o ya "
                f"se deshizo?). No hay nada que reversar."
            )
        # Guard: si la compra dejó de ser BAP (fue editada a otro comprobante),
        # no la tocamos automáticamente.
        if not str(compra.get("comprobante") or "").upper().startswith("BAP"):
            raise ValueError(
                f"La compra #{id_compra} ya no es BAP (comprobante "
                f"{compra.get('comprobante')!r}) — revisala a mano."
            )

        # 1) Restaurar anticipos a vivo (solo los que siguen consumidos 'B').
        restaurados = 0
        if ids:
            ph = ", ".join(["%s"] * len(ids))
            restaurados = db.execute(
                f"UPDATE scintela.dolares "
                f"   SET st = '', usuario_modifica = %s, "
                f"       fecha_modifica = CURRENT_TIMESTAMP "
                f" WHERE id_dolares IN ({ph}) AND COALESCE(st, '') = 'B'",
                (usuario[:50], *ids), conn=conn,
            )

        # 2) Borrar la compra creada por la conversión.
        db.execute(
            "DELETE FROM scintela.compra WHERE id_compra = %s",
            (id_compra,), conn=conn,
        )

        # 3) mov_doble reverso (marca el original como reversado).
        import mov_doble as _md
        _md.registrar(
            conn=conn,
            tipo="bap_anticipo_a_compra_reverso",
            origen_table="compra",
            origen_id=id_compra,
            destino_table="dolares",
            destino_id=(ids[0] if ids else id_compra),
            importe=float(compra.get("importe") or 0),
            fecha=today_ec(),
            concepto=(
                f"REVERSO BAP {compra.get('codigo_prov') or ''}: compra "
                f"#{id_compra} ({compra.get('comprobante')}) → "
                f"{restaurados} anticipo(s) restaurados"
                + (f" — {motivo}" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={
                "id_compra": id_compra,
                "ids_anticipos": ids,
                "restaurados": restaurados,
                "motivo": motivo or "",
            },
            id_original=id_mov_doble,
        )

    return {
        "id_compra": id_compra,
        "comprobante": compra.get("comprobante"),
        "restaurados": restaurados,
    }


# ---------------------------------------------------------------------------
# EDITAR el concepto de un anticipo vivo — TMT 2026-07-29 (dueña).
#
# Hasta hoy un anticipo sólo se podía CARGAR o CANCELAR: no había forma de
# corregirle el concepto. Hace falta para poder escribirle el AÑO de la
# importación (`58/26`), que es lo que desambigua el número reusado entre
# campañas. Ver concepto_parser.parse_ref_anticipo.
#
# Sólo anticipos VIVOS (st vacío): uno ya convertido a compra o cancelado no
# se toca — cambiarle el concepto movería una compra ya cargada de importación
# sin dejar rastro. Para ésos, el camino es deshacer la conversión.
# ---------------------------------------------------------------------------

def editar_concepto(
    *, id_dolares: int, concepto: str, usuario: str = "web",
) -> dict:
    """Cambia el CONCEPTO de un anticipo vivo. Devuelve {cta, anterior, nuevo}."""
    nuevo = (concepto or "").strip()[:100]
    if not nuevo:
        raise ValueError("El concepto no puede quedar vacío.")
    with db.tx() as conn:
        row = db.fetch_one(
            "SELECT id_dolares, cta, concepto, st FROM scintela.dolares "
            " WHERE id_dolares = %s FOR UPDATE",
            (int(id_dolares),), conn=conn,
        )
        if not row:
            raise ValueError(f"No encontré el anticipo id={id_dolares}.")
        st = (row.get("st") or "").strip()
        if st:
            raise ValueError(
                f"El anticipo ya está consumido o cancelado (st='{st}'): "
                "no se puede editar. Deshacé la conversión primero."
            )
        anterior = (row.get("concepto") or "").strip()
        if anterior == nuevo:
            return {"cta": row.get("cta"), "anterior": anterior, "nuevo": nuevo,
                    "cambio": False}
        db.execute(
            "UPDATE scintela.dolares "
            "   SET concepto = %s, usuario_modifica = %s, "
            "       fecha_modifica = CURRENT_TIMESTAMP "
            " WHERE id_dolares = %s",
            (nuevo, usuario[:50], int(id_dolares)), conn=conn,
        )
    return {"cta": row.get("cta"), "anterior": anterior, "nuevo": nuevo,
            "cambio": True}
