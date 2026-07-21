"""Consultas de bancos / movimientos.

Siguiendo la regla de la skill: el saldo acumulado en transacciones_bancarias
está históricamente desconfiable. Mostramos el saldo stored y entre paréntesis
el saldo derivado (suma corrida). Durante la migración sirve como sanity check.
"""

import re

import db
from filters import today_ec
from periodo_guard import asegurar_fecha_abierta


class ActivaRequerida(Exception):
    """El movimiento es candidato a anticipo USD y falta contestar ACTIVA?.

    Réplica del prompt `@22,0 SAY "ACTIVA? " GET DOL` de BANCOS.PRG (~216):
    el dBase SIEMPRE pregunta antes de mandar a DOLARES. La vista atrapa
    esta excepción, muestra la misma pregunta (S/N) y re-submitea con la
    respuesta. Nada se graba hasta contestar (la tx hace rollback completo).
    """

    def __init__(self, cta: str):
        self.cta = cta
        super().__init__(f"ACTIVA? — candidato a anticipo USD cuenta {cta}")


def _detectar_cta_usd(concepto: str, prov: str | None) -> tuple[str | None, str]:
    """Detecta si un ND manual es un anticipo USD — paridad dBase BANCOS.PRG.

    Regla legacy (línea ~212):
        CASE (PROV='IN' AND LEFT(CC,2) ∈ TOT+'II') OR (DOC='ND' AND LEFT(CC,2) ∈ TOT)
        → INSERT en DOLARES con CTA=LEFT(CC,2), CONCEPTO=SUBSTR(CC,4,15)

    TOT (cuentas USD válidas) acá = DISTINCT cta ya existentes en
    scintela.dolares. Devuelve (cta, concepto_para_dolares); cta=None si el
    movimiento no es anticipo (queda solo en el banco, como siempre).
    """
    c = (concepto or "").strip().upper()
    p = (prov or "").strip().upper()
    if not c and not p:
        return None, c
    try:
        # TOT del dBase = códigos de proveedor activos (FABRICA.DBF). Acá:
        # proveedores activos ∪ cuentas que ya existen en dolares.
        rows = db.fetch_all(
            """
            SELECT DISTINCT UPPER(TRIM(cta)) AS cta FROM scintela.dolares
             WHERE TRIM(COALESCE(cta,'')) <> ''
            UNION
            SELECT DISTINCT UPPER(TRIM(codigo_prov)) FROM scintela.proveedor
             WHERE COALESCE(activo, '1') NOT IN ('0', 'N')
               AND LENGTH(TRIM(COALESCE(codigo_prov,''))) = 2
            """
        ) or []
        cuentas = {r["cta"] for r in rows}
    except Exception:
        return None, c
    # EXC del dBase (VARMEMO): RR/IN/PI/KK son marcadores, no cuentas.
    cuentas -= {"RR", "IN", "PI", "KK"}
    lc2 = c[:2]
    # 1) Prefijo explícito IN.XX / IN XX (mismo formato que emitir-cheque).
    m = re.match(r"^IN[. ]+([A-Z0-9]{2})(?:[. ]+(.*))?$", c)
    if m and (m.group(1) in cuentas or m.group(1) == "II"):
        return m.group(1), ((m.group(2) or "").strip() or c)[:50]
    # 2) dBase línea 212: (PROV='IN' AND LC2∈TOT+'II') OR (DOC='ND' AND LC2∈TOT).
    #    Misma condición amplia que el PRG — la decisión final la toma la
    #    pregunta ACTIVA? (S/N), exactamente como en el dBase.
    if len(c) >= 4 and c[2] in ". " and (
        lc2 in cuentas or (p == "IN" and lc2 == "II")
    ):
        return lc2, (c[3:18].strip() or c)[:50]
    return None, c


def _routear_mov_simple(
    conn,
    *,
    documento: str,
    importe_f: float,
    fecha,
    concepto_in: str,
    prov: str | None,
    usuario: str,
    no_banco: int | None = None,
    activa: bool | None = None,
    anticipo_prov: str | None = None,
) -> dict:
    """Side effects de un movimiento simple manual — IMITA el DO CASE de
    dBase BANCOS.PRG (líneas ~164-247), en el MISMO orden y con las mismas
    convenciones de prov/concepto. Todo corre en la misma transacción que el
    insert bancario: el banco baja y la contraparte sube A LA VEZ.

      PRG ~164  PROV='IN' + concepto 'HB…'  → libros de HABITAT (otra
                empresa): PC no los lleva — queda solo en banco, se avisa.
      PRG ~195  PROV = proveedor activo, o ND/CH con 'KK' en concepto
                → COMPRAS (pago directo: tipo H/T/Q/K del proveedor,
                no_banco=1|2, fechad=fecha del mov).
      PRG ~212  (PROV='IN' y LC2∈TOT+'II') o (ND y LC2∈TOT)
                → DOLARES (anticipo): cta=LEFT(concepto,2),
                concepto=SUBSTR(cc,4,15).
      PRG ~222  PROV='RR' o concepto 'RR …' → RETIROS (ret=+imp si ND,
                −imp si DE; de=SUBSTR(cc,4,2); concepto +' B.<cta>').
      PRG ~233  concepto 'CAJA…' → CAJA entrada E ('<doc> CTA.<cta>').
      PRG ~240  ND 'INOP…' → POSDAT nueva: fechad=hoy+120, importe=−imp,
                prov=SUBSTR(cc,6,2), banc=0.

    NO replicado: espejo posdatado ST='P' (el form manual no tiene fechad;
    los posdatados van por emitir-cheque / sync POSDAT).

    Devuelve {destino_table, destino_id, meta, side} — side es el texto
    para el flash; destino_* linkea el mov_doble (y el reverso deshace).
    """
    out = {"destino_table": None, "destino_id": None, "meta": {}, "side": None}
    c = (concepto_in or "").strip().upper()
    p = (prov or "").strip().upper()
    lc2 = c[:2]
    # CTA del dBase: 1=Pichincha, 2=Internacional.
    cta_banco = 1 if int(no_banco or 0) == 10 else 2

    # ── PRG ~164: Habitat — PC no lleva esos libros. Solo banco + aviso.
    if p == "IN" and lc2 == "HB":
        out["side"] = (
            "movimiento HABITAT: quedó solo en el banco (el Programa no "
            "lleva los libros de Habitat — registralo allá)"
        )
        return out

    # ── TMT 2026-06-15: anticipo de proveedor EXPLÍCITO (select del form
    # "¿Es anticipo de proveedor?"). NO depende de tipear el concepto en
    # formato mágico "AC 92" ni de la pregunta ACTIVA?. Solo ND. Fuerza la
    # fila en DOLARES (anticipo VIVO = st en blanco), mismo destino que el
    # dBase con DOC='ND' + PROV→DOL. El mov_doble lo linkea el caller.
    if documento == "ND" and (anticipo_prov or "").strip():
        cta_av = (anticipo_prov or "").strip().upper()[:2]
        row = db.execute_returning(
            """
            INSERT INTO scintela.dolares
                (fecha, cta, importe, concepto, usuario_crea)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id_dolares
            """,
            (fecha, cta_av, importe_f,
             (concepto_in or f"ANTICIPO {cta_av}")[:50], usuario[:50]),
            conn=conn,
        )
        id_dol = (row or {}).get("id_dolares")
        out.update(destino_table="dolares", destino_id=id_dol,
                   meta={"id_dolares": id_dol, "cta_usd": cta_av},
                   side=f"Anticipo USD #{id_dol} creado en cuenta {cta_av} (ver Anticipos)")
        return out

    # ── PRG ~195: COMPRAS (pago directo a proveedor desde el banco).
    proveedores: set[str] = set()
    tipos_prov: dict[str, str] = {}
    try:
        rows = db.fetch_all(
            """
            SELECT UPPER(TRIM(codigo_prov)) AS cod,
                   UPPER(TRIM(COALESCE(tipo,''))) AS tipo
              FROM scintela.proveedor
             WHERE COALESCE(activo, '1') NOT IN ('0', 'N')
               AND TRIM(COALESCE(codigo_prov,'')) <> ''
            """,
            conn=conn,
        ) or []
        proveedores = {r["cod"] for r in rows}
        tipos_prov = {r["cod"]: r["tipo"] for r in rows}
    except Exception:
        pass

    es_compra = (p and p in proveedores and p != "IN") or (
        documento == "ND" and "KK" in c
    )
    if es_compra and documento in ("ND", "CH"):
        # dBase: PROV de la compra = prov del banco; si DOC=ND lo pisa con
        # LC2; si 'KK' en concepto → 'KK'.
        prov_compra = p
        if documento == "ND" and (lc2 in proveedores or lc2 == "KK"):
            prov_compra = lc2
        if "KK" in c:
            prov_compra = "KK"
        # TYP del dBase: H (hilos) / T (tintorería) / Q (químicos) / K resto.
        t = (tipos_prov.get(prov_compra) or "")[:1]
        tipo_compra = t if t in ("H", "T", "Q") else "K"
        row = db.execute_returning(
            """
            INSERT INTO scintela.compra
                (fecha, fechad, codigo_prov, tipo, importe, concepto,
                 no_banco, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id_compra
            """,
            (today_ec(), fecha, prov_compra[:3], tipo_compra, importe_f,
             (concepto_in or f"PAGO {prov_compra}")[:200],
             cta_banco, usuario[:50]),
            conn=conn,
        )
        id_compra = (row or {}).get("id_compra")
        out.update(destino_table="compra", destino_id=id_compra,
                   meta={"id_compra": id_compra, "prov_compra": prov_compra},
                   side=f"Compra #{id_compra} a {prov_compra} registrada "
                        f"(tipo {tipo_compra}, pagada por banco)")
        return out

    # ── PRG ~212: DOLARES (anticipo USD) — solo ND. Igual que el dBase,
    # SIEMPRE pregunta ACTIVA? (S/N): None → la vista pregunta (nada se
    # graba todavía); False (N) → queda solo en banco; True (S) → crea.
    if documento == "ND":
        cta_usd, concepto_usd = _detectar_cta_usd(concepto_in, prov)
        if cta_usd and activa is None:
            raise ActivaRequerida(cta_usd)
        if cta_usd and activa:
            row = db.execute_returning(
                """
                INSERT INTO scintela.dolares
                    (fecha, cta, importe, concepto, usuario_crea)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id_dolares
                """,
                (fecha, cta_usd, importe_f,
                 (concepto_usd or f"ANTICIPO {cta_usd}")[:50], usuario[:50]),
                conn=conn,
            )
            id_dol = (row or {}).get("id_dolares")
            out.update(destino_table="dolares", destino_id=id_dol,
                       meta={"id_dolares": id_dol, "cta_usd": cta_usd},
                       side=f"Anticipo USD #{id_dol} creado en cuenta {cta_usd} (ver Anticipos)")
            return out
        if cta_usd:
            # ACTIVA?=N — como en el dBase, el CASE ya consumió el match:
            # queda solo en banco, no sigue a RR/CAJA/INOP.
            out["side"] = f"ACTIVA?=N — sin anticipo (cuenta {cta_usd}), solo banco"
            return out

    # ── PRG ~222: RETIROS (ND saca, DE devuelve).
    if documento in ("ND", "DE") and (p == "RR" or lc2 == "RR"):
        de_owner = c[3:5].strip() if lc2 == "RR" else (p if p != "RR" else "")
        ret = importe_f if documento == "ND" else -importe_f
        row = db.execute_returning(
            """
            INSERT INTO scintela.retiros
                (fecha, ret, de, concepto, usuario_crea)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id_retiro
            """,
            (fecha, ret, (de_owner or None) and de_owner[:5],
             (f"{concepto_in or 'RETIRO'} B.{cta_banco}")[:100], usuario[:50]),
            conn=conn,
        )
        id_ret = (row or {}).get("id_retiro")
        out.update(destino_table="retiros", destino_id=id_ret,
                   meta={"id_retiro": id_ret},
                   side=("Retiro" if ret >= 0 else "Devolución de retiro")
                        + f" #{id_ret} registrado (URET)")
        return out

    # ── PRG ~233: CAJA (solo ND con concepto CAJA…).
    if documento == "ND" and c.startswith("CAJA"):
        import caja_helpers
        resto = c[4:].strip(" .")
        mov_caja = caja_helpers.insert_movimiento_caja(
            conn,
            fecha=fecha,
            tipo="E",
            importe=importe_f,
            concepto=(f"ND CTA.{cta_banco}" + (f" {resto}" if resto else ""))[:100],
            usuario=usuario,
        )
        id_caja = mov_caja.get("id_caja")
        out.update(destino_table="caja", destino_id=id_caja,
                   meta={"id_caja": id_caja},
                   side=f"Entrada a caja #{id_caja} "
                        f"(saldo caja $ {mov_caja.get('saldo_nuevo', 0):,.2f})")
        return out

    # ── PRG ~240: INOP → POSDAT nueva (negativa, fechad +120 días).
    if documento == "ND" and c.startswith("INOP"):
        from datetime import timedelta
        row = db.execute_returning(
            """
            INSERT INTO scintela.posdat
                (fecha, fechad, prov, importe, concepto, banc, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, 0, %s)
            RETURNING id_posdat
            """,
            (today_ec(), today_ec() + timedelta(days=120),
             (c[5:7].strip() or None), -importe_f,
             (c[5:15].strip() or c)[:100], usuario[:50]),
            conn=conn,
        )
        id_pd = (row or {}).get("id_posdat")
        out.update(destino_table="posdat", destino_id=id_pd,
                   meta={"id_posdat_inop": id_pd},
                   side=f"Posdat #{id_pd} (INOP, −$ {importe_f:,.2f}, "
                        f"vence +120 días) creada")
        return out

    return out


def lista_bancos() -> list[dict]:
    """Saldos por banco con opening balance.

    - `saldo_stored`: running balance de la última fila (= dBase legacy).
    - `saldo_derivado`: Σ entradas − Σ salidas desde 0 (no incluye opening).
    - `opening`: saldo de la PRIMERA fila menos su movimiento firmado.
      Si la chequera arrancó con un saldo previo (caja existente antes de
      la primera transacción cargada), opening > 0.

    Invariante: saldo_stored == opening + saldo_derivado. Si hay drift
    real (fila editada sin recomputar las posteriores) el template
    muestra el warning.
    """
    # Convención de documentos (TMT 2026-05-12): 'TR' (transferencia recibida)
    # se agregó cuando armamos /bancos/transferir — antes este CASE lo
    # ignoraba (caía en ELSE 0) y daba un drift falso entre saldo_stored y
    # saldo_derivado por cada transferencia banco→banco.
    return db.fetch_all(
        """
        SELECT b.no_banco, b.nombre,
               COALESCE((
                 SELECT t.saldo FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
                   -- TMT 2026-06-25: saldo ACTUAL = última fila con fecha <= hoy.
                   -- Excluye movimientos postdatados (fecha futura) con saldo viejo
                   -- que desfasaban el saldo (caso DEP/GS 30/06 → -51.788,80 vs dBase).
                   AND t.fecha <= CURRENT_DATE
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 1
               ), 0) AS saldo_stored,
               COALESCE((
                 SELECT SUM(CASE WHEN documento IN ('DE','AC','NC','TR') THEN importe
                                 WHEN documento IN ('CH','ND','DB')      THEN -importe
                                 ELSE 0 END)
                 FROM scintela.transacciones_bancarias
                 WHERE no_banco = b.no_banco
               ), 0) AS saldo_derivado,
               COALESCE((
                 SELECT t.saldo - CASE
                          WHEN t.documento IN ('DE','AC','NC','TR') THEN t.importe
                          WHEN t.documento IN ('CH','ND','DB')      THEN -t.importe
                          ELSE 0
                        END
                 FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco AND t.saldo IS NOT NULL
                 ORDER BY t.fecha ASC, t.id_transaccion ASC
                 LIMIT 1
               ), 0) AS opening
        FROM scintela.banco b
        ORDER BY b.no_banco
        """
    )


def bancos_operativos() -> list[dict]:
    """Subset de scintela.banco que sí se usa en operaciones diarias.

    Filtra el ruido del legacy dBase: muchos códigos de banco son meros
    rubros contables (DEP.PICH, CANCELA ANTICIPO, UKN, EFECTIVO, etc.) que
    no son cuentas bancarias reales. Esta función devuelve sólo las que
    tienen sentido para transferir / depositar / etc.

    Criterios (en orden):
      1. Match por nombre — los que la usuaria llama "Pichincha" o "Internacional".
      2. Tienen saldo distinto de cero, O movimientos en los últimos 6 meses.

    Si querés ver TODOS los bancos (incluyendo legacy/contables), usá `lista_bancos()`.
    """
    return db.fetch_all(
        """
        SELECT b.no_banco, b.nombre,
               COALESCE((
                 SELECT t.saldo FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
                   -- TMT 2026-06-25: saldo ACTUAL = última fila con fecha <= hoy.
                   -- Excluye movimientos postdatados (fecha futura) con saldo viejo
                   -- que desfasaban el saldo (caso DEP/GS 30/06 → -51.788,80 vs dBase).
                   AND t.fecha <= CURRENT_DATE
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 1
               ), 0) AS saldo
          FROM scintela.banco b
         WHERE
             -- Match por nombre: estos son los que la usuaria opera.
             -- Nota: los signos porcentaje viven duplicados aca abajo
             -- porque psycopg2 los procesa como placeholders incluso
             -- dentro de comentarios SQL. Ver docstring de la function.
             UPPER(COALESCE(b.nombre,'')) LIKE '%%PICHINC%%'
             OR UPPER(COALESCE(b.nombre,'')) = 'INTERNACI'
             OR UPPER(COALESCE(b.nombre,'')) = 'INTERNACIONAL'
             OR (
                 -- O bien tienen movimientos en los últimos 6 meses
                 -- (excluyendo bancos contables que tienen "DEP." o "ANTIC" en el nombre)
                 UPPER(COALESCE(b.nombre,'')) NOT LIKE 'DEP%%'
                 AND UPPER(COALESCE(b.nombre,'')) NOT LIKE '%%ANTIC%%'
                 AND UPPER(COALESCE(b.nombre,'')) NOT IN ('UKN', 'EFECTIVO', 'CANCELA ANTICIPO')
                 AND EXISTS (
                     SELECT 1 FROM scintela.transacciones_bancarias t
                      WHERE t.no_banco = b.no_banco
                        AND t.fecha >= CURRENT_DATE - INTERVAL '6 months'
                 )
             )
         ORDER BY b.no_banco
        """
    ) or []


def movimientos(
    no_banco: int,
    desde: str | None = None,
    hasta: str | None = None,
    limite: int = 500,
    cliente: str | None = None,
    monto: float | None = None,
    doc_num: str | None = None,
) -> list[dict]:
    """Lista movimientos del banco. Incluye linkage de reversos via
    mov_doble: si la fila tiene un mov_doble asociado (sea como origen o
    destino), trae estado ('activo'|'reversado'|'reverso'), usuario que
    hizo la operación, concepto largo del mov, y los punteros id_original
    (si esta fila es un reverso) / id_reverso (si fue reversada). El
    template usa esto para mostrar quién hizo la op y un badge claro de
    "reverso de #N" o "reversado por #N". TMT 2026-05-13.

    TMT 2026-05-28 dueña: filtros opcionales adicionales —
      - cliente: busca substring en el cliente emisor del cheque
        (vía chequextransaccion → cheque.codigo_cli → cliente.nombre).
        Naturalmente acota a filas DE (depósitos de cheques de cliente).
      - monto: importe exacto (match contra t.importe).
      - doc_num: substring en t.numreferencia (número del cheque emitido
        o del documento que el banco asignó).
    """
    cliente_like = f"%{(cliente or '').strip().upper()}%" if cliente else None
    doc_like = f"%{(doc_num or '').strip().upper()}%" if doc_num else None
    rows = db.fetch_all(
        """
        SELECT
            t.id_transaccion, t.fecha, t.documento, t.concepto, t.fechad,
            t.importe, t.saldo, t.stat, t.no_banco, t.no_cta, t.prov,
            -- TMT 2026-06-03: COALESCE numreferencia_manual > numreferencia
            -- (mig 0074). Edits web sobreviven al sync dBase.
            COALESCE(NULLIF(TRIM(t.numreferencia_manual), ''), t.numreferencia::TEXT) AS numreferencia,
            t.numreferencia_manual,
            t.usuario_crea, t.fecha_crea,
            md.id_mov_doble        AS mov_doble_id,
            md.estado              AS mov_estado,
            md.usuario             AS mov_usuario,
            md.concepto            AS mov_concepto,
            md.id_original         AS mov_id_original,
            md.id_reverso          AS mov_id_reverso
        FROM scintela.transacciones_bancarias t
        -- TMT 2026-07-07: FIX fan-out. El LEFT JOIN plano con `OR` duplicaba
        -- la fila cuando una tx tenía VARIOS mov_doble apuntándola — el caso
        -- clásico es un depósito "dep.N ch." que registra 1 mov_doble por
        -- cheque: la MISMA tx (mismo importe/saldo) aparecía N veces y el
        -- contador decía "Mostrando 221 de 180". LATERAL + LIMIT 1 garantiza
        -- una sola fila por tx, priorizando el mov_doble relevante para el
        -- badge de reverso (reverso/reversado) y, a igualdad, el más reciente.
        LEFT JOIN LATERAL (
            SELECT md.id_mov_doble, md.estado, md.usuario, md.concepto,
                   md.id_original, md.id_reverso
              FROM scintela.mov_doble md
             WHERE (md.origen_table  = 'transacciones_bancarias'
                    AND md.origen_id  = t.id_transaccion)
                OR (md.destino_table = 'transacciones_bancarias'
                    AND md.destino_id = t.id_transaccion)
             ORDER BY (CASE WHEN md.estado IN ('reverso', 'reversado')
                            THEN 0 ELSE 1 END),
                      md.id_mov_doble DESC
             LIMIT 1
        ) md ON TRUE
        WHERE t.no_banco = %(no_banco)s
          AND (%(desde)s::date IS NULL OR t.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR t.fecha <= %(hasta)s::date)
          AND (%(monto)s::numeric IS NULL OR t.importe = %(monto)s::numeric)
          AND (%(doc_like)s IS NULL OR
               UPPER(COALESCE(NULLIF(TRIM(t.numreferencia_manual),''), t.numreferencia::text, '')) LIKE %(doc_like)s)
          AND (%(cliente_like)s IS NULL OR EXISTS (
                SELECT 1
                  FROM scintela.chequextransaccion cxt
                  JOIN scintela.cheque c ON c.id_cheque = cxt.id_cheque
                  LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
                 WHERE cxt.id_transaccion = t.id_transaccion
                   AND (UPPER(COALESCE(cli.nombre,'')) LIKE %(cliente_like)s
                        OR UPPER(COALESCE(c.codigo_cli,'')) LIKE %(cliente_like)s)
          ))
        ORDER BY t.fecha DESC, t.id_transaccion DESC
        LIMIT %(limite)s
        """,
        {
            "no_banco": no_banco,
            "desde": desde or None,
            "hasta": hasta or None,
            "limite": limite,
            "monto": monto,
            "doc_like": doc_like,
            "cliente_like": cliente_like,
        },
    )
    # Enriquecer con info de conciliación bancaria (defensivo: si la tabla
    # no existe o falla la query, seguimos con rows sin flag).
    try:
        ids = [r["id_transaccion"] for r in rows if r.get("id_transaccion")]
        if ids:
            conc = db.fetch_all(
                """
                SELECT id_transaccion, id AS conciliacion_id, creado_en, usuario,
                       real_fecha, real_documento, estado
                  FROM scintela.banco_conciliacion_match
                 WHERE id_transaccion = ANY(%s)
                   AND (deshecho_en IS NULL OR deshecho_en IS NULL)
                """,
                (ids,),
            ) or []
            conc_by_id = {c["id_transaccion"]: c for c in conc}
            for r in rows:
                c = conc_by_id.get(r.get("id_transaccion"))
                if c:
                    r["conciliacion_id"] = c.get("conciliacion_id")
                    r["conciliado_en"] = c.get("creado_en")
                    r["conciliado_por"] = c.get("usuario")
                    r["conciliado_real_fecha"] = c.get("real_fecha")
                    r["conciliado_real_doc"] = c.get("real_documento")
                    r["conciliado_estado"] = c.get("estado")
    except Exception:
        pass  # fail-graceful: la vista funciona sin el badge si la tabla no está

    # TMT 2026-05-27 dueña: 'les pusiste el flag de conciliados en banco'.
    # Si el row tiene stat='*' del dBase (PICHINCH.DBF), es conciliado
    # historico. Mostrar el badge igual aunque no haya entry en
    # banco_conciliacion_match.
    for r in rows:
        if r.get("conciliacion_id"):
            continue
        if (r.get("stat") or "").strip() == "*":
            r["conciliacion_id"] = "dbase"
            r["conciliado_por"] = "dBase"
            r["conciliado_estado"] = "dbase"

    # TMT 2026-07-07 duena: 'con un + ver todos los movimientos' + 'no puedo
    # clickear para ir al cheque'. Para los depositos (DE) traemos los cheques
    # linkeados via chequextransaccion -> cheque en UNA query batch. El template
    # usa esto para (a) un expander '+' que muestra cheque por cheque en un
    # deposito consolidado 'dep.N ch.' y (b) un link directo a la ficha del
    # cheque (cheques.detalle). Fail-graceful: sin la tabla, no hay expander.
    try:
        de_ids = [
            r["id_transaccion"]
            for r in rows
            if r.get("id_transaccion")
            and (r.get("documento") or "").strip().upper() == "DE"
        ]
        if de_ids:
            chq = db.fetch_all(
                """
                SELECT cxt.id_transaccion,
                       c.id_cheque, c.no_cheque, c.importe, c.codigo_cli,
                       c.doc_banco, c.fechad, c.stat,
                       COALESCE(cli.nombre, '') AS cliente
                  FROM scintela.chequextransaccion cxt
                  JOIN scintela.cheque c ON c.id_cheque = cxt.id_cheque
                  LEFT JOIN scintela.cliente cli ON cli.codigo_cli = c.codigo_cli
                 WHERE cxt.id_transaccion = ANY(%s)
                 ORDER BY cxt.id_transaccion, c.no_cheque
                """,
                (de_ids,),
            ) or []
            by_tx: dict = {}
            for c in chq:
                by_tx.setdefault(c["id_transaccion"], []).append(c)
            for r in rows:
                lst = by_tx.get(r.get("id_transaccion"))
                if lst:
                    r["cheques"] = lst
                    r["n_cheques"] = len(lst)
    except Exception:
        pass  # fail-graceful: la vista funciona sin el expander si falla

    return rows


def banco_info(no_banco: int) -> dict | None:
    # TMT 2026-05-27 dueña: 'a lado de pichincha no me muestra el total'.
    # Agregamos saldo_stored (running balance último) al banco_info para
    # que /bancos/<id>/movimientos lo muestre en el header.
    return db.fetch_one(
        """
        SELECT b.no_banco, b.nombre,
               COALESCE((
                 SELECT t.saldo FROM scintela.transacciones_bancarias t
                 WHERE t.no_banco = b.no_banco
                   -- TMT 2026-06-25: saldo ACTUAL = última fila con fecha <= hoy.
                   -- Excluye movimientos postdatados (fecha futura) con saldo viejo
                   -- que desfasaban el saldo (caso DEP/GS 30/06 → -51.788,80 vs dBase).
                   AND t.fecha <= CURRENT_DATE
                 ORDER BY t.fecha DESC, t.id_transaccion DESC
                 LIMIT 1
               ), 0) AS saldo_stored
          FROM scintela.banco b
         WHERE b.no_banco = %s
        """,
        (no_banco,),
    )


# =====================================================================
# Emisión de cheque propio (chequera) — replica BANCOS.PRG::CHEQUERA
# =====================================================================
# El legacy disparaba cascadas mágicas según proveedor + concepto. En el
# nuevo app pedimos el destino EXPLÍCITO al usuario y aplicamos el side-effect
# correspondiente. Tipos válidos:
#
#   - "proveedor": pagás una posdat existente al proveedor X. La posdat
#     se marca pagada (banc=no_banco). Si no hay posdat o querés generar
#     una compra nueva, usá `compras.nueva` y luego volvé a esto.
#   - "retiro": el dueño retira plata. INSERT en `retiros` con doc='CH'.
#   - "caja": transferís plata del banco a la caja física. INSERT en `caja`
#     con tipo='E' (entrada de caja).
#   - "gasto": pagás un gasto general (luz, contadora, etc). INSERT en
#     `xgast` con saldo=0 (ya pagado).
#   - "otro": sólo se registra el movimiento bancario, sin side-effect en
#     otra tabla. Útil para casos atípicos (impuestos, transferencias entre
#     cuentas propias).
#
# TODOS comparten: INSERT en transacciones_bancarias con documento='CH'.

TIPOS_CHEQUE_EMITIDO = ("proveedor", "retiro", "caja", "gasto", "anticipo_usd", "otro")


def emitir_cheque(
    *,
    tipo: str,
    no_banco: int,
    importe,
    fecha,
    no_cheque: str = "",
    beneficiario: str = "",
    concepto: str = "",
    # Específico por tipo:
    id_posdat: int | None = None,        # tipo='proveedor': cierra esta posdat (1 sola — legacy)
    id_posdats: list[int] | None = None, # tipo='proveedor': N posdats (multi-select 2026-05-27).
                                          # Si vienen ambos, gana id_posdats. Multi-proveedor
                                          # permitido; diferencia con importe → anticipo/saldo.
    de_socio: str | None = None,         # tipo='retiro': código de socio (ej "TM")
    es_postdatado: bool = False,         # tipo='proveedor' o 'gasto': dejarlo en posdat futuro
    fechad=None,                         # fecha de cobro si postdatado
    usuario: str = "web",
    xgast_num: int | None = None,        # TMT 2026-05-19 v4 audit: categoría V1..V9
                                          # cuando tipo='gasto'. Sin esto el xgast quedaba
                                          # con num=NULL → invisible en /informes/gastos.
) -> dict:
    """Emite un cheque propio en el banco `no_banco`.

    Devuelve `{id_transaccion, side_effect: <descripción>}`.

    Lanza ValueError si los datos son inválidos para el tipo elegido.
    """
    if tipo not in TIPOS_CHEQUE_EMITIDO:
        raise ValueError(f"Tipo inválido: {tipo!r}. Usá: {', '.join(TIPOS_CHEQUE_EMITIDO)}")
    if not no_banco:
        raise ValueError("Banco origen requerido.")
    importe_f = float(importe or 0)
    if importe_f <= 0:
        raise ValueError("Importe debe ser mayor a cero.")
    asegurar_fecha_abierta(fecha)

    banco_row = db.fetch_one(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco WHERE no_banco = %s",
        (no_banco,),
    )
    if not banco_row:
        raise ValueError(f"Banco no_banco={no_banco} no existe.")

    side_effect = "ninguno"
    extras: dict = {}

    with db.tx() as conn, conn.cursor() as cur:
        # 1) Registro común — usar bank_helpers para que compute saldo
        # consistentemente (mismo path que transferir / reversar). El raw
        # INSERT antiguo dependía del trigger y dejaba saldo=0 cuando éste
        # no estaba aplicado en la DB. TMT 2026-05-13.
        import bank_helpers
        bh_row = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco,
            no_cta=None,
            fecha=fecha,
            documento="CH",
            importe=importe_f,  # bank_helpers espera ABS, el signo lo aplica internamente
            concepto=(concepto or beneficiario or f"Cheque {no_cheque}").strip()[:50],
            prov=(beneficiario or "")[:5].upper() if beneficiario else None,
            numreferencia=(int(no_cheque) if (no_cheque or "").strip().isdigit() else None),
            usuario=usuario[:50],
            fechad=(fechad if (es_postdatado and fechad) else None),
            stat="A",
        )
        id_transaccion = bh_row["id_transaccion"]

        # 2) Side effect específico por tipo
        if tipo == "proveedor":
            # TMT 2026-05-27 dueña: 'cuando emito cheques, puedes dejarme
            # seleccionar multiples proveedores'. Normalizamos a una lista
            # única — multi gana sobre single, single arma lista de uno,
            # ninguno = lista vacía. Multi-proveedor está permitido (1
            # cheque puede cerrar obligaciones de varios proveedores).
            posdats_a_cerrar: list[int] = []
            if id_posdats:
                posdats_a_cerrar = [int(x) for x in id_posdats if x]
            elif id_posdat:
                posdats_a_cerrar = [int(id_posdat)]

            if posdats_a_cerrar:
                # Cerrar todas las posdats seleccionadas — el cheque puede
                # cubrir N obligaciones. La diferencia entre suma neta y
                # importe del cheque queda anotada en `extras` para que la
                # view la registre como anticipo (positivo) o saldo
                # pendiente (negativo) — decisión dueña 2026-05-27.
                cur.execute(
                    """
                    SELECT id_posdat, prov, importe
                      FROM scintela.posdat
                     WHERE id_posdat = ANY(%s)
                    """,
                    (posdats_a_cerrar,),
                )
                rows = cur.fetchall() or []
                rows_dicts = [
                    {"id_posdat": r[0], "prov": r[1], "importe": float(r[2] or 0)}
                    if isinstance(r, list | tuple)
                    else {"id_posdat": r["id_posdat"], "prov": r["prov"], "importe": float(r.get("importe") or 0)}
                    for r in rows
                ]
                suma_posdats = sum(p["importe"] for p in rows_dicts)
                provs_unicos = sorted({p["prov"] for p in rows_dicts if p.get("prov")})

                cur.execute(
                    """
                    UPDATE scintela.posdat
                       SET banc = %s,
                           fecha_modifica = CURRENT_TIMESTAMP,
                           usuario_modifica = %s
                     WHERE id_posdat = ANY(%s)
                    """,
                    (no_banco, usuario[:50], posdats_a_cerrar),
                )

                # Diferencia entre lo que firma el cheque (importe_f) y lo
                # que netea la suma de obligaciones (suma_posdats). NCs
                # negativas ya restaron en suma_posdats. Si la dueña
                # selecciona NCs sin facturas, suma_posdats puede ser < 0.
                diff = round(importe_f - suma_posdats, 2)

                multi_provs_msg = (
                    f" ({len(provs_unicos)} provs: {', '.join(provs_unicos[:5])}"
                    + ("…" if len(provs_unicos) > 5 else "")
                    + ")"
                ) if len(provs_unicos) > 1 else ""

                if len(posdats_a_cerrar) == 1 and abs(diff) < 0.01:
                    side_effect = f"Posdat #{posdats_a_cerrar[0]} cerrada (banc={no_banco})"
                elif abs(diff) < 0.01:
                    side_effect = (
                        f"{len(posdats_a_cerrar)} posdats cerradas (banc={no_banco}){multi_provs_msg}"
                    )
                elif diff > 0:
                    side_effect = (
                        f"{len(posdats_a_cerrar)} posdat(s) cerrada(s){multi_provs_msg}; "
                        f"sobra $ {diff:.2f} → anticipo a {provs_unicos[0] if provs_unicos else beneficiario or '—'}"
                    )
                else:
                    side_effect = (
                        f"{len(posdats_a_cerrar)} posdat(s) cerrada(s){multi_provs_msg}; "
                        f"falta $ {(-diff):.2f} → última con saldo pendiente"
                    )
                extras["id_posdats"] = posdats_a_cerrar
                extras["suma_posdats"] = suma_posdats
                extras["diff_cheque_vs_posdats"] = diff
                extras["provs"] = provs_unicos
                # Backward compat — primer id para callers que esperan singular.
                extras["id_posdat"] = posdats_a_cerrar[0]
            else:
                side_effect = "Sin posdat asociada — sólo movimiento bancario"

        elif tipo == "retiro":
            cur.execute(
                """
                INSERT INTO scintela.retiros
                    (fecha, nb, ret, de, concepto, clave, usuario_crea, id_transaccion_bancaria)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id_retiro
                """,
                (
                    fecha, no_banco, importe_f, (de_socio or "")[:5],
                    (concepto or "")[:100], (de_socio or "")[:5],
                    usuario[:50], id_transaccion,
                ),
            )
            ret = cur.fetchone()
            id_retiro = ret[0] if isinstance(ret, list | tuple) else (ret.get("id_retiro") if ret else None)
            side_effect = f"Retiro #{id_retiro} registrado"
            extras["id_retiro"] = id_retiro

        elif tipo == "caja":
            # Usar caja_helpers para que compute saldo running. El raw
            # INSERT anterior dejaba saldo NULL. TMT 2026-05-13.
            import caja_helpers
            ch_row = caja_helpers.insert_movimiento_caja(
                conn,
                fecha=fecha,
                tipo="E",
                importe=importe_f,
                concepto=(concepto or "Transferencia banco→caja")[:100],
                clave="BCO",
                usuario=usuario[:50],
            )
            id_caja = ch_row["id_caja"]
            side_effect = f"Caja #{id_caja} (entrada de banco)"
            extras["id_caja"] = id_caja

        elif tipo == "gasto":
            # Crear el gasto YA PAGADO (saldo=0) o pendiente si postdatado.
            # TMT 2026-05-19 v4 audit — incluir `num` para que aparezca en
            # /informes/gastos V1..V9. Antes el num quedaba NULL → fila
            # invisible en el matriz (bug clase $220K).
            saldo_xgast = importe_f if es_postdatado else 0.0
            stat_xgast = "P" if es_postdatado else "C"  # P=pendiente, C=cancelado
            num_xgast = None
            if xgast_num is not None:
                try:
                    n = int(xgast_num)
                    if 1 <= n <= 9:
                        num_xgast = n
                except (TypeError, ValueError):
                    num_xgast = None
            # Fallback: si no vino xgast_num explícito, intentar inferir
            # del concepto vía el matcher de gastos. Si tampoco matchea,
            # queda NULL (legacy — visible solo en /gastos).
            if num_xgast is None and concepto:
                try:
                    from modules.gastos.queries import sugerir_categoria as _sug
                    num_xgast = _sug(concepto)
                except Exception:
                    num_xgast = None
            cur.execute(
                """
                INSERT INTO scintela.xgast
                    (fecha, doc, prov, concepto, importe, saldo, stat,
                     fechad, clave, usuario_crea, num)
                VALUES (%s, 'CH', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id_xgast
                """,
                (
                    fecha,
                    (beneficiario or "")[:5].upper() if beneficiario else None,
                    (concepto or "Gasto pagado con cheque")[:100],
                    importe_f, saldo_xgast, stat_xgast,
                    fechad if es_postdatado else fecha,
                    (beneficiario or "")[:3] if beneficiario else None,
                    usuario[:50],
                    num_xgast,
                ),
            )
            gx = cur.fetchone()
            id_xgast = gx[0] if isinstance(gx, list | tuple) else (gx.get("id_xgast") if gx else None)
            side_effect = (
                f"Gasto #{id_xgast} registrado"
                + (" (pendiente — posdatado)" if es_postdatado else " (pagado)")
                + (f" V{num_xgast}" if num_xgast else " (sin categoría — clasificar después en /gastos)")
            )
            extras["id_xgast"] = id_xgast
            extras["num_xgast"] = num_xgast

        elif tipo == "anticipo_usd":
            # TMT 2026-05-17: paridad dBase BANCOS.PRG > CHEQUERA con concepto
            # `IN.<CT>` o `IN <CT>` — emite un cheque a un proveedor en cuenta
            # dólares. La fila en `scintela.dolares` representa el anticipo
            # entregado (cuando llegue la factura del proveedor, se aplica vía
            # BAP). `beneficiario` es el código de cuenta USD (2 letras, ej. MP).
            cta_usd = (beneficiario or "")[:5].upper()
            if not cta_usd:
                raise ValueError(
                    "Anticipo USD requiere código de cuenta dólares (2 letras, "
                    "ej. MP). Usá el campo beneficiario o tipeá IN.<CT> en concepto."
                )
            cur.execute(
                """
                INSERT INTO scintela.dolares
                    (fecha, cta, importe, concepto, usuario_crea)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id_dolares
                """,
                (
                    fecha, cta_usd, importe_f,
                    (concepto or f"Anticipo USD cta {cta_usd}")[:50],
                    usuario[:50],
                ),
            )
            dr = cur.fetchone()
            id_dolares = dr[0] if isinstance(dr, list | tuple) else (dr.get("id_dolares") if dr else None)
            side_effect = f"Anticipo USD #{id_dolares} (cuenta {cta_usd})"
            extras["id_dolares"] = id_dolares

        # tipo == "otro" → no side-effect (el INSERT en transacciones_bancarias ya alcanza)

        # Registrar movimiento doble si tuvo side effect (TMT 2026-05-12).
        id_mov_doble = None
        if id_transaccion:
            destino_table, destino_id = None, None
            if tipo == "proveedor" and extras.get("id_posdat"):
                destino_table, destino_id = "posdat", extras["id_posdat"]
            elif tipo == "retiro" and extras.get("id_retiro"):
                destino_table, destino_id = "retiros", extras["id_retiro"]
            elif tipo == "caja" and extras.get("id_caja"):
                destino_table, destino_id = "caja", extras["id_caja"]
            elif tipo == "gasto" and extras.get("id_xgast"):
                destino_table, destino_id = "xgast", extras["id_xgast"]
            elif tipo == "anticipo_usd" and extras.get("id_dolares"):
                destino_table, destino_id = "dolares", extras["id_dolares"]
            # TMT 2026-07-08 (dueña "todo movimiento en historial + reversible"):
            # ANTES sólo se registraba mov_doble si había side-effect. Un cheque
            # 'otro' o 'proveedor' SIN posdat salía del banco INVISIBLE en
            # /historial y sin botón de reverso (egreso de caja sin rastro).
            # Ahora SIEMPRE registramos: si no hay contraparte, el destino es la
            # propia fila bancaria. El reverso (bancos.reversar_cheque_emitido)
            # compensa el CH con una NC igual — con o sin side-effect.
            if not destino_table:
                destino_table, destino_id = "transacciones_bancarias", id_transaccion
            import mov_doble as _md
            id_mov_doble = _md.registrar(
                conn=conn,
                tipo=f"cheque_emitido_{tipo}",
                origen_table="transacciones_bancarias",
                origen_id=id_transaccion,
                destino_table=destino_table,
                destino_id=destino_id,
                importe=importe_f,
                fecha=fecha,
                concepto=(concepto or beneficiario or "")[:200],
                usuario=usuario,
                metadata={"no_banco": no_banco,
                          "no_cheque": no_cheque,
                          "beneficiario": beneficiario},
            )

    return {
        "id_transaccion":  id_transaccion,
        "no_banco":        no_banco,
        "banco_nombre":    banco_row.get("nombre") or "",
        "tipo":            tipo,
        "importe":         importe_f,
        "side_effect":     side_effect,
        "id_mov_doble":    id_mov_doble,
        **extras,
    }


def no_banco_pichincha() -> int:
    """Resuelve el no_banco de Pichincha por nombre (fallback 10).

    Intela opera SOLO Pichincha para débitos manuales. La convención
    histórica es no_banco=10, pero lo resolvemos por nombre para no
    hardcodear si alguna vez cambia el id en el seed.
    """
    row = db.fetch_one(
        "SELECT no_banco FROM scintela.banco "
        "WHERE UPPER(COALESCE(nombre,'')) LIKE '%%PICHINC%%' "
        "ORDER BY no_banco LIMIT 1"
    )
    return int(row["no_banco"]) if row and row.get("no_banco") is not None else 10


def registrar_debito_posdat(
    *,
    id_posdat: int,
    importe=None,
    fecha=None,
    no_cheque: str = "",
    usuario: str = "web",
) -> dict:
    """Manda a debitar un posdatado al banco Pichincha (pedido dueña 2026-07-08).

    Caso de uso: hay posdatados (cheques propios que la dueña emitió) que va
    a MANDAR A DEBITAR al banco. Al registrarlos, el monto:
      - sale de Pasivos (el posdat pasa de banc=0 → banc=Pichincha), y
      - baja el saldo de Pichincha (movimiento ND = nota de débito).

    Atómico (una sola tx):
      1. INSERT ND en Pichincha por el importe (signo −1 → resta saldo).
      2. UPDATE posdat SET banc=<pichincha> (deja de ser deuda viva banc=0).
      3. mov_doble tipo='nota_debito' linkeando la fila bancaria con el posdat.
         metadata.id_posdat_debito permite que el reverso reabra el posdat
         (ver reversar_movimiento_simple).

    Es la contracara del `emitir_cheque(tipo='proveedor')` pero como ND (la
    dueña eligió "nota de débito", no "cheque") y en UN click desde /posdat.
    Reversible por /historial o los movimientos de Pichincha (ND → NC + el
    posdat vuelve a banc=0).

    Devuelve dict {id_transaccion, id_mov_doble, no_banco, importe, saldo_nuevo}.
    Lanza ValueError si el posdat no existe, ya fue debitado (banc!=0), está
    anulado o el importe es inválido.
    """
    import bank_helpers
    import mov_doble as _md

    if not id_posdat:
        raise ValueError("id_posdat requerido.")

    pd = db.fetch_one(
        """
        SELECT id_posdat, num, prov, importe, concepto,
               COALESCE(banc, 0) AS banc,
               (anulada IS TRUE) AS anulada
          FROM scintela.posdat
         WHERE id_posdat = %s
        """,
        (id_posdat,),
    )
    if not pd:
        raise ValueError(f"Posdatado #{id_posdat} no existe.")
    if pd.get("anulada"):
        raise ValueError(f"Posdatado #{id_posdat} está anulado.")
    if int(pd.get("banc") or 0) != 0:
        raise ValueError(
            f"Posdatado #{id_posdat} ya no es deuda viva (banc={pd.get('banc')}). "
            "Probablemente ya fue debitado o pagado."
        )

    # Importe: por defecto el del posdat; si viene uno explícito debe coincidir
    # (defensa contra que la fila cambie entre el render y el submit).
    importe_pd = round(float(pd.get("importe") or 0), 2)
    importe_f = round(float(importe), 2) if importe is not None else importe_pd
    if importe_f <= 0:
        raise ValueError("El importe a debitar debe ser mayor a cero.")
    if importe is not None and abs(importe_f - importe_pd) > 0.01:
        raise ValueError(
            f"El importe enviado (${importe_f:.2f}) no coincide con el del "
            f"posdatado (${importe_pd:.2f}). Refrescá la lista y probá de nuevo."
        )

    fecha_deb = fecha or today_ec()
    asegurar_fecha_abierta(fecha_deb)

    no_banco = no_banco_pichincha()
    prov = (pd.get("prov") or "").strip().upper() or None
    concepto_pd = (pd.get("concepto") or "").strip()
    concepto = (f"Débito posdat {prov or ''} {concepto_pd}").strip()[:50]
    numref = int(no_cheque) if (no_cheque or "").strip().isdigit() else None

    with db.tx() as conn:
        mov = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco,
            no_cta=None,
            fecha=fecha_deb,
            documento="ND",
            importe=importe_f,
            concepto=concepto,
            prov=prov,
            numreferencia=numref,
            usuario=usuario,
        )
        id_transaccion = mov.get("id_transaccion")

        db.execute(
            """
            UPDATE scintela.posdat
               SET banc = %s,
                   fecha_modifica = CURRENT_TIMESTAMP,
                   usuario_modifica = %s
             WHERE id_posdat = %s
            """,
            (no_banco, usuario[:50], id_posdat),
            conn=conn,
        )

        id_mov_doble = _md.registrar(
            conn=conn,
            tipo="nota_debito",
            origen_table="transacciones_bancarias",
            origen_id=id_transaccion,
            destino_table="posdat",
            destino_id=id_posdat,
            importe=importe_f,
            fecha=fecha_deb,
            concepto=concepto[:200],
            usuario=usuario,
            metadata={
                "no_banco": no_banco,
                "documento": "ND",
                "prov": prov or "",
                # Clave para que el reverso reabra el posdat (banc→0):
                "id_posdat_debito": int(id_posdat),
                "no_cheque": (no_cheque or "").strip(),
            },
        )

    return {
        "id_transaccion": id_transaccion,
        "id_mov_doble": id_mov_doble,
        "no_banco": no_banco,
        "banco_nombre": "Pichincha",
        "importe": importe_f,
        "saldo_nuevo": mov.get("saldo_nuevo"),
        "id_posdat": int(id_posdat),
    }


def posdat_abiertas_de(prov: str | None = None) -> list[dict]:
    """Posdats abiertas para el wizard de emitir cheque.

    Filtros:
      - `banc = 0`: deuda viva SIN cheque emitido. Excluye banc=9 (legacy
        ya con cheque), banc=10/32 (modernos con cheque PC) — sino el
        wizard ofrecería posdats ya pagadas, abriendo doble-pago (bug #R2
        audit 2026-05-14).
      - `anulada IS NOT TRUE`: soft-delete (migration 0027) excluido.
      - opcional `prov`: filtrar por proveedor específico.
    """
    return db.fetch_all(
        """
        SELECT p.id_posdat, p.fecha, p.fechad, p.prov, p.importe,
               p.concepto, p.num, p.clave,
               COALESCE(pr.nombre, '') AS proveedor
        FROM scintela.posdat p
        LEFT JOIN scintela.proveedor pr ON pr.codigo_prov = p.prov
        WHERE COALESCE(p.banc, 0) = 0
          AND (p.anulada IS NOT TRUE OR p.anulada IS NULL)
          AND (%(prov)s IS NULL OR UPPER(p.prov) = UPPER(%(prov)s))
        ORDER BY p.fechad ASC, p.id_posdat ASC
        LIMIT 200
        """,
        {"prov": prov or None},
    ) or []


def conceptos_frecuentes_egresos(limite: int = 50) -> list[dict]:
    """Top conceptos usados en cheques propios (egresos del banco).

    Para autocomplete del form emitir-cheque. Filtra documentos de SALIDA
    (CH/ND/etc.) — los DE/TR/IN son entradas y no aplican.
    """
    return db.fetch_all(
        """
        SELECT TRIM(concepto) AS concepto,
               COUNT(*)        AS usos
          FROM scintela.transacciones_bancarias
         WHERE UPPER(TRIM(COALESCE(documento, ''))) NOT IN
               ('DE','TR','XX','NC','IN')
           AND COALESCE(concepto, '') <> ''
         GROUP BY TRIM(concepto)
         ORDER BY usos DESC, concepto
         LIMIT %s
        """,
        (limite,),
    ) or []


def proveedores_activos(limite: int = 500) -> list[dict]:
    """Lista de proveedores activos para autocomplete (cheque a proveedor).

    Devuelve `codigo_prov`, `nombre`. Ordenado alfabético.
    """
    return db.fetch_all(
        """
        SELECT codigo_prov, COALESCE(nombre, '') AS nombre
          FROM scintela.proveedor
         WHERE COALESCE(activo, '1') NOT IN ('0', 'N')
         ORDER BY codigo_prov
         LIMIT %s
        """,
        (limite,),
    ) or []


def proveedores_op_saldos(limite: int = 500) -> list[dict]:
    """Proveedores con saldo de anticipo/OP (posdat banc=0) — para el datalist
    del destino 'Posdato (INOP)' en la N/D (pedido Andres 2026-06-18: poder
    ESCOGER la cuenta OP por proveedor en vez de tipear el codigo de memoria).

    saldo_op = SUM(posdat.importe) de las posdat NO bancarias vivas del prov
    (el mismo bucket de los 'IN OP'). Solo devuelve proveedores con saldo != 0.
    """
    return db.fetch_all(
        """
        SELECT pr.codigo_prov,
               COALESCE(NULLIF(TRIM(pr.nombre), ''), pr.codigo_prov) AS nombre,
               op.saldo_op AS saldo_op
          FROM scintela.proveedor pr
          JOIN (
              SELECT UPPER(TRIM(prov)) AS prov,
                     ROUND(SUM(importe)::numeric, 2) AS saldo_op
                FROM scintela.posdat
               WHERE COALESCE(banc, 0) = 0
                 AND (anulada IS NOT TRUE OR anulada IS NULL)
               GROUP BY UPPER(TRIM(prov))
              HAVING ABS(ROUND(SUM(importe)::numeric, 2)) > 0.005
          ) op ON op.prov = UPPER(TRIM(pr.codigo_prov))
         WHERE COALESCE(pr.activo, '1') NOT IN ('0', 'N')
         ORDER BY ABS(op.saldo_op) DESC, pr.codigo_prov
         LIMIT %s
        """,
        (limite,),
    ) or []


def crear_movimiento_simple(
    *,
    no_banco: int,
    documento: str,
    importe: float,
    fecha,
    concepto: str = "",
    prov: str | None = None,
    usuario: str = "web",
    permitir_duplicado: bool = False,
    activa: bool | None = None,
    anticipo_prov: str | None = None,
) -> dict:
    """Crea un movimiento bancario "simple" (DE / NC / ND).

    Pedido Tamara 2026-05-19: la pantalla de Bancos ahora tiene 4 acciones
    (Emitir cheque + Depositar + NC + ND). Las 3 últimas usan este helper.

    Argumentos:
        documento: 'DE' (depósito), 'NC' (nota de crédito), 'ND' (nota de débito).
        importe:   positivo siempre — el signo lo aplica bank_helpers.
        prov:      opcional, código de proveedor relacionado (informativo).

    Signos (`bank_helpers.signo_documento`):
        DE → +1 (suma al saldo)
        NC → +1 (suma al saldo)
        ND → −1 (resta del saldo)

    Atómico: insert + mov_doble en la misma tx. Devuelve dict con
    `id_transaccion`, `saldo_nuevo`, `id_mov_doble`.
    """
    import bank_helpers
    import mov_doble as _md

    documento = (documento or "").upper().strip()
    if documento not in ("DE", "NC", "ND"):
        raise ValueError(
            f"documento debe ser DE, NC o ND (recibido: {documento!r})"
        )
    if not no_banco:
        raise ValueError("no_banco requerido")
    importe_f = abs(float(importe or 0))
    if importe_f <= 0:
        raise ValueError("Importe debe ser > 0.")
    if not fecha:
        raise ValueError("fecha requerida")
    asegurar_fecha_abierta(fecha)

    concepto_in = (concepto or "").strip()[:50]

    # TMT 2026-06-09: dedupe SILENCIOSO contra doble carga manual. El bug de
    # los "movimientos dobles" del 09/06 (29016/29017, 29018/29019): Enter en
    # el campo importe disparaba el submit implícito ANTES de tipear el
    # concepto → quedaba una fila pelada, y el usuario la volvía a cargar
    # completa. Regla quirúrgica — si EL MISMO usuario cargó en los últimos
    # 15 minutos un movimiento idéntico (banco + doc + fecha + importe):
    #   a) si el existente quedó SIN concepto y este trae → completamos el
    #      existente (UPDATE) en vez de insertar otro;
    #   b) si los conceptos coinciden (o este viene vacío) → devolvemos el
    #      existente tal cual (doble click / re-submit).
    # Repetidos legítimos (ej. dos ND de 9.026,00 el mismo día, conceptos
    # 'IN OP AI 11' vs 'IN OP AI 14') tienen conceptos distintos → pasan y
    # se insertan normal. Sin checkboxes ni pasos extra (pedido Tamara).
    if not permitir_duplicado:
        prev = db.fetch_one(
            """
            SELECT id_transaccion, TRIM(COALESCE(concepto,'')) AS concepto,
                   saldo, prov
              FROM scintela.transacciones_bancarias
             WHERE no_banco = %s
               AND UPPER(TRIM(COALESCE(documento,''))) = %s
               AND fecha = %s
               AND ROUND(importe::numeric, 2) = ROUND(%s::numeric, 2)
               AND TRIM(COALESCE(usuario_crea,'')) = %s
               AND COALESCE(fecha_crea, NOW()) > NOW() - INTERVAL '15 minutes'
             ORDER BY id_transaccion DESC
             LIMIT 1
            """,
            (no_banco, documento, fecha, importe_f, (usuario or "").strip()),
        )
        if prev:
            prev_conc = (prev.get("concepto") or "").strip()
            if not prev_conc and concepto_in:
                # (a) completar la fila pelada en vez de duplicar. El concepto
                # nuevo puede revelar un side effect dBase (anticipo/retiro/
                # caja) que la fila pelada no creó → router acá también.
                with db.tx() as conn_dd:
                    db.execute(
                        """
                        UPDATE scintela.transacciones_bancarias
                           SET concepto = %s,
                               prov = COALESCE(NULLIF(TRIM(COALESCE(prov,'')),''), %s),
                               usuario_modifica = %s,
                               fecha_modifica = NOW()
                         WHERE id_transaccion = %s
                        """,
                        (concepto_in, (prov or None), usuario[:50],
                         prev["id_transaccion"]),
                        conn=conn_dd,
                    )
                    ruta_dd = _routear_mov_simple(
                        conn_dd,
                        documento=documento,
                        importe_f=importe_f,
                        fecha=fecha,
                        concepto_in=concepto_in,
                        prov=prov,
                        usuario=usuario,
                        no_banco=no_banco,
                        activa=activa,
                        anticipo_prov=anticipo_prov,
                    )
                    if ruta_dd["destino_id"]:
                        import json as _json
                        db.execute(
                            """
                            UPDATE scintela.mov_doble
                               SET destino_table = %s,
                                   destino_id = %s,
                                   metadata = COALESCE(metadata, '{}'::jsonb)
                                              || %s::jsonb
                             WHERE origen_table = 'transacciones_bancarias'
                               AND origen_id = %s
                               AND estado = 'activo'
                            """,
                            (ruta_dd["destino_table"], ruta_dd["destino_id"],
                             _json.dumps(ruta_dd["meta"] or {}),
                             prev["id_transaccion"]),
                            conn=conn_dd,
                        )
                        if ruta_dd["destino_table"] == "retiros":
                            db.execute(
                                "UPDATE scintela.retiros "
                                "SET id_transaccion_bancaria = %s "
                                "WHERE id_retiro = %s",
                                (prev["id_transaccion"], ruta_dd["destino_id"]),
                                conn=conn_dd,
                            )
                return {
                    "id_transaccion": prev["id_transaccion"],
                    "saldo_nuevo": float(prev.get("saldo") or 0),
                    "id_mov_doble": None,
                    "id_dolares": ruta_dd["meta"].get("id_dolares"),
                    "cta_usd": ruta_dd["meta"].get("cta_usd"),
                    "side_effect": ruta_dd["side"],
                    "no_banco": no_banco,
                    "importe": importe_f,
                    "dedupe": "completado",
                }
            if prev_conc == concepto_in or not concepto_in:
                # (b) idempotente: ya estaba cargado.
                return {
                    "id_transaccion": prev["id_transaccion"],
                    "saldo_nuevo": float(prev.get("saldo") or 0),
                    "id_mov_doble": None,
                    "no_banco": no_banco,
                    "importe": importe_f,
                    "dedupe": "ya_existia",
                }
            # conceptos distintos y no vacíos → repetido legítimo, sigue.

    # Tipo de mov_doble: 1 a 1 con documento para que el dispatcher de
    # /historial sepa cómo reversarlo. Convención:
    #   DE → "deposito"
    #   NC → "nota_credito"
    #   ND → "nota_debito"
    tipo_md = {
        "DE": "deposito",
        "NC": "nota_credito",
        "ND": "nota_debito",
    }[documento]

    concepto_clean = concepto_in

    # TMT 2026-06-09 (pedido Tamara, paridad dBase): el movimiento manual se
    # rutea IGUAL que BANCOS.PRG y TODO en la misma transacción — el banco
    # baja y la contraparte (anticipo USD / retiro / caja) sube a la vez.
    # Si no matchea ningún caso, queda solo en el banco (como siempre).
    with db.tx() as conn:
        mov = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco,
            no_cta=None,
            fecha=fecha,
            documento=documento,
            importe=importe_f,
            concepto=concepto_clean,
            prov=(prov or None),
            usuario=usuario,
        )

        ruta = _routear_mov_simple(
            conn,
            documento=documento,
            importe_f=importe_f,
            fecha=fecha,
            concepto_in=concepto_in,
            prov=prov,
            usuario=usuario,
            no_banco=no_banco,
            activa=activa,
            anticipo_prov=anticipo_prov,
        )

        # Auto-link: origen = la fila bancaria. Destino = la contraparte
        # creada por el router (dolares/retiros/caja — visible en /historial
        # y el reverso deshace ambos), sino la fila bancaria misma.
        id_md = _md.registrar(
            conn=conn,
            tipo=tipo_md,
            origen_table="transacciones_bancarias",
            origen_id=mov.get("id_transaccion"),
            destino_table=ruta["destino_table"] or "transacciones_bancarias",
            destino_id=ruta["destino_id"] or mov.get("id_transaccion"),
            importe=importe_f,
            fecha=fecha,
            concepto=f"{documento} {concepto_clean}".strip()[:200],
            usuario=usuario,
            metadata={
                "no_banco": no_banco,
                "documento": documento,
                "prov": prov or "",
                **(ruta["meta"] or {}),
            },
        )

        # Link inverso retiros→banco (columna dedicada).
        if ruta["destino_table"] == "retiros" and ruta["destino_id"]:
            db.execute(
                "UPDATE scintela.retiros SET id_transaccion_bancaria = %s "
                "WHERE id_retiro = %s",
                (mov.get("id_transaccion"), ruta["destino_id"]),
                conn=conn,
            )

    return {
        "id_transaccion": mov.get("id_transaccion"),
        "saldo_nuevo":    mov.get("saldo_nuevo"),
        "id_mov_doble":   id_md,
        "documento":      documento,
        "importe":        importe_f,
        "no_banco":       no_banco,
        "id_dolares":     ruta["meta"].get("id_dolares"),
        "cta_usd":        ruta["meta"].get("cta_usd"),
        "side_effect":    ruta["side"],
    }


def reversar_movimiento_simple(
    *,
    id_mov_doble: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversa un movimiento simple (deposito/nota_credito/nota_debito).

    Lee el mov_doble original, mira su documento y compensa con el
    documento de signo opuesto:
      DE(+1) → reversado con CH(-1)
      NC(+1) → reversado con CH(-1)
      ND(-1) → reversado con NC(+1)

    Marca el mov_doble original como `estado='reversado'` + `id_reverso`.
    Atómico.
    """
    import bank_helpers
    import mov_doble as _md

    if not id_mov_doble:
        raise ValueError("id_mov_doble requerido.")

    # TMT 2026-07-21: la columna se llama fecha_operacion (no `fecha`) — el
    # SELECT viejo tiraba UndefinedColumn y el reverso nunca pudo correr.
    md_orig = db.fetch_one(
        """
        SELECT id_mov_doble, tipo, origen_id, destino_id, importe,
               fecha_operacion AS fecha, concepto, metadata, estado
          FROM scintela.mov_doble
         WHERE id_mov_doble = %s
        """,
        (id_mov_doble,),
    )
    if not md_orig:
        raise ValueError(f"mov_doble #{id_mov_doble} no existe.")
    if md_orig.get("estado") != "activo":
        raise ValueError(
            f"mov_doble #{id_mov_doble} no está activo "
            f"(estado={md_orig.get('estado')!r})."
        )
    tipo_orig = (md_orig.get("tipo") or "").strip()
    if tipo_orig not in ("deposito", "nota_credito", "nota_debito"):
        raise ValueError(
            f"mov_doble #{id_mov_doble} no es un movimiento simple "
            f"(tipo={tipo_orig!r})."
        )

    # Leer el documento original desde la transacción bancaria linkeada.
    tx_orig = db.fetch_one(
        """
        SELECT id_transaccion, no_banco, documento, importe AS importe_orig, fecha
          FROM scintela.transacciones_bancarias
         WHERE id_transaccion = %s
        """,
        (md_orig.get("origen_id"),),
    )
    if not tx_orig:
        raise ValueError(
            f"Transacción origen #{md_orig.get('origen_id')} no existe."
        )

    doc_orig = (tx_orig.get("documento") or "").upper().strip()
    # Documento de reverso (signo opuesto).
    doc_reverso = {"DE": "CH", "NC": "CH", "ND": "NC"}.get(doc_orig)
    if not doc_reverso:
        raise ValueError(
            f"No sé cómo reversar documento {doc_orig!r} (esperaba DE/NC/ND)."
        )

    importe_f = abs(float(md_orig.get("importe") or 0))
    fecha_rev = today_ec()
    asegurar_fecha_abierta(fecha_rev)

    motivo_clean = (motivo or "").strip()
    concepto_rev = (
        f"REVERSO {tipo_orig} #{id_mov_doble}"
        + (f" — {motivo_clean}" if motivo_clean else "")
    )[:50]

    with db.tx() as conn:
        mov_rev = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=int(tx_orig["no_banco"]),
            no_cta=None,
            fecha=fecha_rev,
            documento=doc_reverso,
            importe=importe_f,
            concepto=concepto_rev,
            usuario=usuario,
        )

        # TMT 2026-06-09: si el movimiento original creó una contraparte
        # (paridad dBase: anticipo USD / retiro / caja), el reverso la
        # deshace también — todo en la misma transacción.
        meta = md_orig.get("metadata") or {}
        if isinstance(meta, str):
            import json as _json
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = {}

        # Anticipo USD → st='X' (anulado, deja de sumar a ANTICIPOS). Si ya
        # fue consumido por un BAP (st='B'), bloqueamos con mensaje claro.
        id_dol = meta.get("id_dolares")
        if id_dol:
            dol = db.fetch_one(
                "SELECT id_dolares, st FROM scintela.dolares WHERE id_dolares = %s",
                (id_dol,), conn=conn,
            )
            if dol:
                st_dol = (dol.get("st") or "").strip()
                if st_dol:
                    raise ValueError(
                        f"El anticipo USD #{id_dol} ligado a esta ND ya fue "
                        f"aplicado (st='{st_dol}') — resolvé el anticipo en "
                        f"/dolares antes de reversar la ND."
                    )
                db.execute(
                    "UPDATE scintela.dolares SET st = 'X', "
                    "usuario_modifica = %s, fecha_modifica = NOW() "
                    "WHERE id_dolares = %s",
                    (usuario[:50], id_dol), conn=conn,
                )

        # Retiro → borrar la fila (URET vuelve a su valor).
        id_ret = meta.get("id_retiro")
        if id_ret:
            db.execute(
                "DELETE FROM scintela.retiros WHERE id_retiro = %s",
                (id_ret,), conn=conn,
            )

        # Compra pagada directa → borrar la fila.
        id_compra = meta.get("id_compra")
        if id_compra:
            db.execute(
                "DELETE FROM scintela.compra WHERE id_compra = %s",
                (id_compra,), conn=conn,
            )

        # Posdat debitado a banco (registrar_debito_posdat) → reabrir
        # (banc=0) para que vuelva a ser deuda viva en /posdat. TMT 2026-07-08.
        id_pd_deb = meta.get("id_posdat_debito")
        if id_pd_deb:
            db.execute(
                """
                UPDATE scintela.posdat
                   SET banc = 0,
                       fecha_modifica = CURRENT_TIMESTAMP,
                       usuario_modifica = %s
                 WHERE id_posdat = %s
                """,
                (usuario[:50], id_pd_deb), conn=conn,
            )

        # Posdat INOP → anular (soft-delete, recuperable).
        id_pd_inop = meta.get("id_posdat_inop")
        if id_pd_inop:
            db.execute(
                """
                UPDATE scintela.posdat
                   SET anulada = TRUE,
                       motivo_anulacion = %s,
                       fecha_anulacion = CURRENT_TIMESTAMP,
                       usuario_modifica = %s
                 WHERE id_posdat = %s
                """,
                (f"Reverso ND INOP (mov_doble #{id_mov_doble})",
                 usuario[:50], id_pd_inop), conn=conn,
            )

        # Caja → borrar la entrada + walk-forward de saldos de caja.
        id_caja = meta.get("id_caja")
        if id_caja:
            import caja_helpers
            db.execute(
                "DELETE FROM scintela.caja WHERE id_caja = %s",
                (id_caja,), conn=conn,
            )
            try:
                caja_helpers.recompute_saldos_desde(conn, ancla_id=int(id_caja))
            except Exception:
                pass  # sin filas posteriores no hay nada que recomputar

        # mov_doble del reverso, linkeado al original via id_original.
        id_md_rev = _md.registrar(
            conn=conn,
            tipo=f"reverso_{tipo_orig}",
            origen_table="transacciones_bancarias",
            origen_id=mov_rev.get("id_transaccion"),
            destino_table="transacciones_bancarias",
            destino_id=mov_rev.get("id_transaccion"),
            importe=importe_f,
            fecha=fecha_rev,
            concepto=concepto_rev,
            usuario=usuario,
            metadata={
                "motivo": motivo_clean,
                "doc_orig": doc_orig,
                "doc_reverso": doc_reverso,
                "no_banco": int(tx_orig["no_banco"]),
            },
            id_original=id_mov_doble,
        )

    return {
        "id_transaccion_reverso": mov_rev.get("id_transaccion"),
        "saldo_nuevo":            mov_rev.get("saldo_nuevo"),
        "id_mov_doble_reverso":   id_md_rev,
        "doc_orig":               doc_orig,
        "doc_reverso":            doc_reverso,
    }


def transferir_entre_bancos(
    *,
    no_banco_origen: int,
    no_banco_destino: int,
    importe: float,
    fecha,
    concepto: str = "",
    usuario: str = "web",
) -> dict:
    """Mueve plata de un banco al otro. Atómico.

    Inserta:
      1) CH (egreso) en el banco origen
      2) DE (ingreso) en el banco destino
    Mismo importe, misma fecha, conceptos vinculados ("TR a/de banco X").
    Saldos auto-actualizados por el trigger.
    """
    import bank_helpers
    if not no_banco_origen or not no_banco_destino:
        raise ValueError("Banco origen y destino requeridos.")
    if no_banco_origen == no_banco_destino:
        raise ValueError("Origen y destino son el mismo banco.")
    importe_f = abs(float(importe or 0))
    if importe_f <= 0:
        raise ValueError("Importe debe ser > 0.")

    bancos = {
        int(b["no_banco"]): (b.get("nombre") or "")
        for b in (db.fetch_all(
            "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco"
        ) or [])
    }
    nombre_origen  = bancos.get(no_banco_origen)
    nombre_destino = bancos.get(no_banco_destino)
    if not nombre_origen or not nombre_destino:
        raise ValueError(f"Banco no encontrado: {no_banco_origen}/{no_banco_destino}")

    concepto_base = (concepto or "").strip()
    concepto_origen  = (f"TR a {nombre_destino}" +
                        (f" — {concepto_base}" if concepto_base else ""))[:50]
    concepto_destino = (f"TR de {nombre_origen}" +
                        (f" — {concepto_base}" if concepto_base else ""))[:50]

    id_mov_doble = None
    with db.tx() as conn:
        mov_origen = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco_origen,
            no_cta=None,
            fecha=fecha,
            documento="CH",
            importe=importe_f,
            concepto=concepto_origen,
            usuario=usuario,
        )
        mov_destino = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=no_banco_destino,
            no_cta=None,
            fecha=fecha,
            documento="TR",  # transferencia recibida (entrada)
            importe=importe_f,
            concepto=concepto_destino,
            usuario=usuario,
        )

        # Registrar en historial unificado.
        import mov_doble as _md
        id_mov_doble = _md.registrar(
            conn=conn,
            tipo="transfer_banco_banco",
            origen_table="transacciones_bancarias",
            origen_id=mov_origen.get("id_transaccion"),
            destino_table="transacciones_bancarias",
            destino_id=mov_destino.get("id_transaccion"),
            importe=importe_f,
            fecha=fecha,
            concepto=f"{nombre_origen} → {nombre_destino} {concepto_base}".strip(),
            usuario=usuario,
            metadata={"no_banco_origen": no_banco_origen,
                      "no_banco_destino": no_banco_destino},
        )

    return {
        "origen": {"no_banco": no_banco_origen, "nombre": nombre_origen,
                   "id_transaccion": mov_origen["id_transaccion"],
                   "saldo_nuevo": mov_origen["saldo_nuevo"]},
        "destino": {"no_banco": no_banco_destino, "nombre": nombre_destino,
                    "id_transaccion": mov_destino["id_transaccion"],
                    "saldo_nuevo": mov_destino["saldo_nuevo"]},
        "importe": importe_f,
        "id_mov_doble": id_mov_doble,
    }


def reversar_transferencia(
    *,
    id_mov_doble: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversa una transferencia banco↔banco previamente registrada.

    Toma el `id_mov_doble` de un movimiento tipo='transfer_banco_banco' activo
    y compensa atómicamente ambos lados:
      - En el banco origen (que tenía CH egreso): inserta NC (ingreso).
      - En el banco destino (que tenía TR ingreso): inserta CH (egreso).
    Marca el mov_doble original como 'reversado' y registra el reverso
    linkeado con `id_original`.

    Regla de signos:
      CH(-1) compensado con NC(+1) → neto cero.
      TR(+1) compensado con CH(-1) → neto cero.

    TMT 2026-05-13.
    """
    import bank_helpers
    import mov_doble as _md

    motivo = (motivo or "").strip()
    fecha_rev = today_ec()
    asegurar_fecha_abierta(fecha_rev)

    md = db.fetch_one(
        """
        SELECT id_mov_doble, tipo, origen_table, origen_id,
               destino_table, destino_id, importe, estado, metadata
          FROM scintela.mov_doble
         WHERE id_mov_doble = %s
        """,
        (id_mov_doble,),
    )
    if not md:
        raise ValueError(f"mov_doble {id_mov_doble} no existe.")
    if md.get("tipo") != "transfer_banco_banco":
        raise ValueError(
            f"mov_doble #{id_mov_doble} no es una transferencia banco↔banco "
            f"(tipo={md.get('tipo')!r})."
        )
    if md.get("estado") != "activo":
        raise ValueError(
            f"La transferencia #{id_mov_doble} ya está en estado "
            f"{md.get('estado')!r} — no se puede reversar otra vez."
        )

    # Origen es la tx de egreso (CH); destino la de ingreso (TR/DE).
    tx_orig = db.fetch_one(
        "SELECT id_transaccion, no_banco, importe, documento FROM "
        "scintela.transacciones_bancarias WHERE id_transaccion = %s",
        (md["origen_id"],),
    )
    tx_dest = db.fetch_one(
        "SELECT id_transaccion, no_banco, importe, documento FROM "
        "scintela.transacciones_bancarias WHERE id_transaccion = %s",
        (md["destino_id"],),
    )
    if not tx_orig or not tx_dest:
        raise ValueError(
            "No encuentro las transacciones origen/destino — datos rotos."
        )

    importe_abs = abs(float(md.get("importe") or tx_orig.get("importe") or 0))
    if importe_abs <= 0:
        raise ValueError("Importe original = 0, nada que reversar.")

    with db.tx() as conn:
        # 1) Compensación en el banco ORIGEN: NC (ingreso) que cancela el CH.
        comp_origen = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=tx_orig["no_banco"],
            no_cta=None,
            fecha=fecha_rev,
            documento="NC",
            importe=importe_abs,
            concepto=(f"REVERSO transfer #{id_mov_doble}"
                      + (f" — {motivo}" if motivo else ""))[:50],
            usuario=usuario,
        )
        # 2) Compensación en el banco DESTINO: CH (egreso) que cancela el TR.
        comp_destino = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=tx_dest["no_banco"],
            no_cta=None,
            fecha=fecha_rev,
            documento="CH",
            importe=importe_abs,
            concepto=(f"REVERSO transfer #{id_mov_doble}"
                      + (f" — {motivo}" if motivo else ""))[:50],
            usuario=usuario,
        )
        # 3) Registrar reverso linkeado al original.
        id_md_rev = _md.registrar(
            conn=conn,
            tipo="reverso_transfer_banco_banco",
            origen_table="transacciones_bancarias",
            origen_id=comp_origen["id_transaccion"],
            destino_table="transacciones_bancarias",
            destino_id=comp_destino["id_transaccion"],
            importe=importe_abs,
            fecha=fecha_rev,
            concepto=("REVERSO transferencia banco→banco"
                      + (f" — {motivo}" if motivo else ""))[:200],
            usuario=usuario,
            metadata={"motivo": motivo or "",
                      "id_mov_doble_original": id_mov_doble},
            id_original=id_mov_doble,
        )

    return {
        "id_mov_doble_original": id_mov_doble,
        "id_mov_doble_reverso":  id_md_rev,
        "compensacion_origen":   comp_origen["id_transaccion"],
        "compensacion_destino":  comp_destino["id_transaccion"],
        "importe":               importe_abs,
        "no_banco_origen":       tx_orig["no_banco"],
        "no_banco_destino":      tx_dest["no_banco"],
    }


def reversar_cheque_emitido(
    *,
    id_transaccion: int,
    motivo: str = "",
    usuario: str = "web",
) -> dict:
    """Reversa un cheque emitido (de chequera) con todos sus side effects.

    Operación atómica (TMT 2026-05-12 Fase K):
      1. Lee la transacción original (documento='CH'). Si ya fue reversada
         (existe una ND apuntando), error.
      2. INSERT compensación bancaria documento='ND' (nota de débito) por
         el mismo importe en signo OPUESTO.
      3. Según el tipo de side effect (lookup via mov_doble):
         - proveedor con id_posdat → reabre la posdat (banc=0).
         - retiro → INSERT scintela.retiros con importe NEGATIVO.
         - caja   → INSERT scintela.caja TIPO='S' (cancela la entrada).
         - gasto  → marca xgast con stat='Y' (anulado).
         - otro / sin side effect → sólo compensación bancaria.
      4. Registra reverso en mov_doble enlazado al original.

    Devuelve dict con info de lo reversado.
    """
    import bank_helpers
    import mov_doble as _md

    motivo = (motivo or "").strip()
    if not motivo:
        raise ValueError("Motivo requerido para reversar el cheque.")
    fecha_rev = today_ec()
    asegurar_fecha_abierta(fecha_rev)

    tx = db.fetch_one(
        """
        SELECT id_transaccion, no_banco, documento, importe, concepto,
               prov, numreferencia, fecha, stat
          FROM scintela.transacciones_bancarias
         WHERE id_transaccion = %s
        """,
        (id_transaccion,),
    )
    if not tx:
        raise ValueError(f"Transacción {id_transaccion} no existe.")
    doc = (tx.get("documento") or "").strip().upper()
    if doc != "CH":
        raise ValueError(
            f"Esta transacción no es un cheque emitido (documento={doc!r}). "
            "Sólo se puede reversar con esta operación cheques de chequera."
        )
    # Detectar doble reverso: si ya hay una mov_doble del tipo reverso_emitido
    # que apunta al original, abortar.
    ya = db.fetch_one(
        """
        SELECT id_mov_doble FROM scintela.mov_doble
         WHERE origen_table = 'transacciones_bancarias'
           AND origen_id = %s
           AND estado = 'reversado'
         LIMIT 1
        """,
        (id_transaccion,),
    )
    if ya:
        raise ValueError(
            f"El cheque emitido (tx #{id_transaccion}) ya fue reversado."
        )

    # Buscar el mov_doble original para saber qué side effect deshacer.
    md_orig = _md.buscar_por_origen(
        origen_table="transacciones_bancarias",
        origen_id=id_transaccion,
    )
    importe_orig = float(tx.get("importe") or 0)
    importe_abs = abs(importe_orig)
    if importe_abs <= 0:
        raise ValueError("Importe original = 0, no hay nada que reversar.")

    side_revertido = None
    with db.tx() as conn:
        # 1) Compensación bancaria — NC (Nota de Crédito) ingresa la plata
        # de vuelta al banco. ANTES usaba 'ND' (Nota de Débito) que es un
        # documento de EGRESO → restaba el saldo otra vez en lugar de
        # devolverlo. signo_documento('ND')=-1, signo_documento('NC')=+1.
        # TMT 2026-05-13.
        mov_comp = bank_helpers.insert_movimiento_bancario(
            conn,
            no_banco=tx["no_banco"],
            no_cta=None,
            fecha=fecha_rev,
            documento="NC",
            importe=importe_abs,
            concepto=(f"REVERSO ch tx#{id_transaccion} — {motivo}")[:50],
            prov=tx.get("prov"),
            numreferencia=tx.get("numreferencia"),
            usuario=usuario,
        )
        id_compensacion = mov_comp.get("id_transaccion")

        # 2) Side effect inverso según mov_doble original.
        if md_orig:
            tipo_orig = md_orig.get("tipo") or ""
            dest_table = md_orig.get("destino_table")
            dest_id = md_orig.get("destino_id")

            if tipo_orig == "cheque_emitido_proveedor" and dest_table == "posdat":
                # Reabrir la posdat — pasar banc de no_banco a 0.
                db.execute(
                    "UPDATE scintela.posdat SET banc=0, "
                    "    fecha_modifica=CURRENT_TIMESTAMP, usuario_modifica=%s "
                    "WHERE id_posdat=%s",
                    (usuario[:50], dest_id),
                    conn=conn,
                )
                side_revertido = {"tipo": "posdat_reabierta", "id_posdat": dest_id}

            elif tipo_orig == "cheque_emitido_retiro" and dest_table == "retiros":
                # INSERT retiro con importe negativo (compensación contable).
                # El campo `nb` lo dejamos NULL — no toca un banco real (el
                # reverso bancario ya se hizo arriba).
                rev_row = db.execute_returning(
                    """
                    INSERT INTO scintela.retiros
                        (fecha, ret, de, concepto, clave, usuario_crea)
                    SELECT %s, -ret, de,
                           ('REVERSO id ' || id_retiro || ' — ' || %s)::varchar,
                           clave, %s
                      FROM scintela.retiros
                     WHERE id_retiro = %s
                    RETURNING id_retiro
                    """,
                    (fecha_rev, motivo[:60], usuario[:50], dest_id),
                    conn=conn,
                ) or {}
                side_revertido = {"tipo": "retiro_compensado",
                                  "id_retiro_compensacion": rev_row.get("id_retiro"),
                                  "id_retiro_original": dest_id}

            elif tipo_orig == "cheque_emitido_caja" and dest_table == "caja":
                # INSERT caja salida (S) que cancela la entrada original.
                # Usar caja_helpers para que saldo running se compute.
                # TMT 2026-05-13.
                import caja_helpers
                rev_row = caja_helpers.insert_movimiento_caja(
                    conn,
                    fecha=fecha_rev,
                    tipo="S",
                    importe=importe_abs,
                    concepto=(f"REVERSO caja#{dest_id} — {motivo}")[:80],
                    clave="REV",
                    usuario=usuario[:50],
                )
                side_revertido = {"tipo": "caja_compensada",
                                  "id_caja_compensacion": rev_row.get("id_caja"),
                                  "id_caja_original": dest_id}

            elif tipo_orig == "cheque_emitido_gasto" and dest_table == "xgast":
                # Marcar el xgast como anulado (stat='Y') — no inserta nuevo.
                db.execute(
                    "UPDATE scintela.xgast SET stat='Y', "
                    "    usuario_modifica=%s "
                    "WHERE id_xgast=%s",
                    (usuario[:50], dest_id),
                    conn=conn,
                )
                side_revertido = {"tipo": "xgast_anulado", "id_xgast": dest_id}

            elif tipo_orig == "cheque_emitido_anticipo_usd" and dest_table == "dolares":
                # TMT 2026-07-08: anular la fila de anticipo en scintela.dolares
                # (st='X') para que deje de sumar a ANTICIPOS — el banco ya se
                # compensó arriba con la NC. Si ya fue consumida por un BAP
                # (st no vacío ≠ 'X') bloqueamos con mensaje claro.
                dol = db.fetch_one(
                    "SELECT st FROM scintela.dolares WHERE id_dolares=%s",
                    (dest_id,), conn=conn,
                )
                if dol:
                    _st = (dol.get("st") or "").strip()
                    if _st and _st != "X":
                        raise ValueError(
                            f"El anticipo USD #{dest_id} ligado a este cheque ya "
                            f"fue aplicado (st='{_st}') — resolvelo en /dolares "
                            f"antes de reversar el cheque."
                        )
                    db.execute(
                        "UPDATE scintela.dolares SET st='X', usuario_modifica=%s, "
                        "fecha_modifica=NOW() WHERE id_dolares=%s",
                        (usuario[:50], dest_id), conn=conn,
                    )
                side_revertido = {"tipo": "anticipo_usd_anulado", "id_dolares": dest_id}

        # 3) Registrar reverso en mov_doble (siempre, aunque no haya side effect).
        id_md_rev = _md.registrar(
            conn=conn,
            tipo="reverso_cheque_emitido",
            origen_table="transacciones_bancarias",
            origen_id=id_compensacion,
            destino_table=(md_orig or {}).get("destino_table", "transacciones_bancarias"),
            destino_id=(md_orig or {}).get("destino_id", id_transaccion),
            importe=importe_abs,
            fecha=fecha_rev,
            concepto=f"REVERSO ch tx#{id_transaccion} — {motivo}",
            usuario=usuario,
            id_original=(md_orig or {}).get("id_mov_doble"),
            metadata={"id_transaccion_original": id_transaccion,
                      "motivo": motivo,
                      "side_effect_revertido": side_revertido},
        )

    return {
        "id_transaccion_original":    id_transaccion,
        "id_transaccion_compensacion": id_compensacion,
        "no_banco":                   tx["no_banco"],
        "importe":                    importe_abs,
        "side_effect_revertido":      side_revertido,
        "id_mov_doble_reverso":       id_md_rev,
    }
