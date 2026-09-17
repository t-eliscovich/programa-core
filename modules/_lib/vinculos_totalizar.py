"""Qué cobro pagó qué factura — aunque después se haya totalizado la cuenta.

TMT 2026-09-16 (dueña, sobre la factura 177617 de MTM, cancelada y sin un solo
cheque aplicado): *"no borres vínculos!!"*, y más claro todavía: **"puede
totalizarse, pero no que se pierda qué cheque/depósito/transferencia pagó qué
factura"**.

El TOTALIZAR re-liquida FIFO toda la cuenta del cliente y borra las filas de
`chequesxfact`. Eso NO cambia: después del reparto el vínculo viejo apuntaría a
una factura que ese cheque ya no paga, y dejarlo VIVO movería números que nadie
pidió mover —el rewind de TOTF as-of (`informes/views.py`) re-suma
`chequesxfact` para el balance a una fecha pasada, y el health de facturas
sobre-aplicadas (`admin_dbase/salud_view.py`) suma lo mismo para alarmar—.

Lo que cambia es que el vínculo ya no se PIERDE: antes de borrarlo se copia a
`scintela.chequesxfact_totalizado`, una tabla de sólo historia que nadie suma,
y las fichas de la factura y del cheque lo muestran con la fecha de la corrida.

⚠ La tabla se bootstrapea en caliente (`CREATE TABLE IF NOT EXISTS`): el deploy
no corre migraciones — mismo patrón que `saldo_snapshot.py`.

⚠ Las corridas viejas ya habían dormido sus links en `mov_doble.metadata`, así
que la tabla arranca con ellos (`backfill_desde_metadata`): 593 de los 608
borrados desde el 07/07. Los 15 restantes son de las 8 primeras corridas, que
no guardaron el detalle — de ésos no hay nada, y no se inventa.
"""
from __future__ import annotations

import db

TABLA = "scintela.chequesxfact_totalizado"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLA} (
    id               bigserial PRIMARY KEY,
    id_mov_doble     bigint      NOT NULL,
    fecha_totalizar  date        NOT NULL,
    codigo_cli       text,
    id_cheque        bigint      NOT NULL,
    id_fact          bigint      NOT NULL,
    importe          numeric(14,2),
    fechaing         date,
    no_banco         integer,
    tipo             text,
    creado_en        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cxf_tot_mov_par
    ON {TABLA} (id_mov_doble, id_cheque, id_fact, importe);
CREATE INDEX IF NOT EXISTS ix_cxf_tot_fact   ON {TABLA} (id_fact);
CREATE INDEX IF NOT EXISTS ix_cxf_tot_cheque ON {TABLA} (id_cheque);
CREATE INDEX IF NOT EXISTS ix_cxf_tot_cli    ON {TABLA} (codigo_cli);
"""

#: El backfill corre UNA vez por proceso: la tabla es chica y el ON CONFLICT la
#: hace idempotente, pero no hace falta pagarlo en cada visita a una ficha.
_listo = False


def asegurar_tabla(conn=None) -> bool:
    """Crea la tabla si falta y trae lo que ya estaba dormido en la metadata."""
    global _listo
    if _listo:
        return True
    try:
        db.execute(_DDL, conn=conn)
        backfill_desde_metadata(conn=conn)
        _listo = True
        return True
    except Exception as _e:                              # pragma: no cover
        from modules._lib.silencios import avisar
        avisar(__name__, "asegurar_tabla", _e)
        return False


def backfill_desde_metadata(conn=None) -> int:
    """Copia los links que las corridas viejas dejaron en `mov_doble.metadata`.

    Idempotente: el índice único por (mov, cheque, factura, importe) frena las
    repeticiones. El importe entra en la clave a propósito — un cheque aplicado
    dos veces a la misma factura (parcial + resto) son DOS vínculos, y perder
    uno sería la misma pérdida que estamos arreglando.
    """
    # Lo que entró antes del filtro de abajo (10 filas en producción el
    # 17/09/2026, de dos corridas deshechas) se va: esos vínculos están vivos.
    db.execute(
        f"""
        DELETE FROM {TABLA} t
         USING scintela.mov_doble m
         WHERE m.id_mov_doble = t.id_mov_doble
           AND m.estado <> 'activo'
        """,
        conn=conn,
    )
    return db.execute(
        f"""
        INSERT INTO {TABLA}
              (id_mov_doble, fecha_totalizar, codigo_cli, id_cheque, id_fact,
               importe, fechaing, no_banco, tipo)
        SELECT m.id_mov_doble,
               m.fecha_creacion::date,
               COALESCE(l->>'codigo_cli', m.metadata->>'codigo_cli'),
               (l->>'id_cheque')::bigint,
               (l->>'id_fact')::bigint,
               NULLIF(l->>'importe', '')::numeric,
               NULLIF(l->>'fechaing', '')::date,
               NULLIF(l->>'no_banco', '')::integer,
               l->>'tipo'
          FROM scintela.mov_doble m
          CROSS JOIN LATERAL jsonb_array_elements(m.metadata -> 'links') l
         WHERE m.tipo = 'totalizar_estado_cuenta'
           -- 17/09/2026: un totalizar DESHECHO ya repuso sus vínculos vivos;
           -- traerlo acá los mostraba como "soltados" y, peor, el botón de
           -- reponer los contaba dos veces (MHC: 9 cheques en dos corridas).
           AND m.estado = 'activo'
           AND m.metadata ? 'links'
           AND (l->>'id_cheque') IS NOT NULL
           AND (l->>'id_fact') IS NOT NULL
        ON CONFLICT DO NOTHING
        """,
        conn=conn,
    ) or 0


def guardar(conn, *, id_mov_doble: int, fecha, codigo_cli: str,
            links: list[dict]) -> int:
    """Guarda los vínculos que este totalizar está por borrar.

    Va DENTRO de la transacción del totalizar: si el totalizar se cae, no queda
    el historial de algo que no pasó.
    """
    if not links:
        return 0
    asegurar_tabla(conn=conn)
    n = 0
    for lk in links:
        n += db.execute(
            f"""
            INSERT INTO {TABLA}
                  (id_mov_doble, fecha_totalizar, codigo_cli, id_cheque,
                   id_fact, importe, fechaing, no_banco, tipo)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            """,
            (id_mov_doble, fecha, lk.get("codigo_cli") or codigo_cli,
             lk.get("id_cheque"), lk.get("id_fact"), lk.get("importe"),
             lk.get("fechaing"), lk.get("no_banco"), lk.get("tipo")),
            conn=conn,
        ) or 0
    return n


def olvidar_corrida(conn, id_mov_doble: int) -> int:
    """Saca el historial de un totalizar DESHECHO.

    El ↺ repone los vínculos en la tabla viva: dejarlos también acá los
    mostraría dos veces, una de ellas diciendo que se soltaron.
    """
    try:
        return db.execute(
            f"DELETE FROM {TABLA} WHERE id_mov_doble = %s",
            (id_mov_doble,), conn=conn) or 0
    except Exception as _e:                              # pragma: no cover
        from modules._lib.silencios import avisar
        avisar(__name__, "olvidar_corrida", _e)
        return 0


# ── Lectura: las dos puntas del vínculo ─────────────────────────────────────
#
# El número, el banco y el estado se resuelven contra las tablas VIVAS: si el
# cheque cambió de estado desde entonces, se muestra el de hoy.
#
# ⚠ El `NOT EXISTS`: si ese par volvió a aplicarse, ya se ve en "Aplicaciones
# de cheques" — mostrarlo acá sería la misma aplicación dos veces.
_SQL_BASE = f"""
    SELECT t.id_mov_doble,
           t.fecha_totalizar,
           t.id_cheque,
           t.id_fact,
           t.importe                     AS aplicado,
           t.fechaing,
           m.usuario                     AS usuario_totalizar,
           ch.no_cheque,
           ch.doc_banco,
           ch.fecha                      AS cheque_fecha,
           ch.importe                    AS cheque_importe,
           ch.stat                       AS cheque_stat,
           COALESCE(NULLIF(ch.banco, ''), b.nombre, '') AS cheque_banco,
           f.numf,
           f.numf_completo,
           f.codigo_cli
      FROM {TABLA} t
      LEFT JOIN scintela.mov_doble m ON m.id_mov_doble = t.id_mov_doble
      LEFT JOIN scintela.cheque   ch ON ch.id_cheque   = t.id_cheque
      LEFT JOIN scintela.banco    b  ON b.no_banco     = ch.no_banco
      LEFT JOIN scintela.factura  f  ON f.id_factura   = t.id_fact
     WHERE {{filtro}}
       AND NOT EXISTS (
             SELECT 1 FROM scintela.chequesxfact x
              WHERE x.id_cheque = t.id_cheque AND x.id_fact = t.id_fact)
     ORDER BY t.fecha_totalizar DESC, t.id_cheque
"""


def _leer(filtro: str, valor: int) -> list[dict]:
    """Best-effort: un historial que falla no puede voltear una ficha."""
    if not asegurar_tabla():
        return []
    try:
        return db.fetch_all(_SQL_BASE.format(filtro=filtro), (valor,))
    except Exception as _e:                              # pragma: no cover
        from modules._lib.silencios import avisar
        avisar(__name__, "_leer", _e)
        return []


def de_la_factura(id_factura: int) -> list[dict]:
    """Cheques que pagaban esta factura hasta que corrió el totalizar."""
    return _leer("t.id_fact = %s", int(id_factura))


def del_cheque(id_cheque: int) -> list[dict]:
    """Facturas que pagaba este cheque hasta que corrió el totalizar."""
    return _leer("t.id_cheque = %s", int(id_cheque))


# ── Las corridas del totalizar de un cliente, con el antes y el después ─────
#
# TMT 2026-09-16 (dueña, sobre la tabla que le pasé por chat): *"esta tablita
# que me mandás la quiero ver en el estado de cuenta en algún lado, dónde se ve
# en el programa"*. No se veía en ninguna: el botón "Ver totalizadas" lista las
# T en gris y la pantalla de deshacer sólo dice cuántas cambian.
_SQL_CORRIDAS = """
    SELECT id_mov_doble,
           fecha_creacion::date AS fecha,
           usuario,
           importe              AS pool,
           metadata
      FROM scintela.mov_doble
     WHERE tipo = 'totalizar_estado_cuenta'
       AND estado <> 'reversado'
       AND metadata ->> 'codigo_cli' = %s
     ORDER BY fecha_creacion DESC
"""


def corridas_del_cliente(codigo_cli: str) -> list[dict]:
    """Cada totalizar del cliente con sus facturas: cómo estaban y cómo quedaron.

    El `antes` trae importe/abono/saldo/stat por factura y el `despues` sólo lo
    que cambió, así que se mergean por id. Las corridas previas al 19/08 no
    guardaron ninguno de los dos: van igual, con la fecha y sin detalle — que
    la fila exista es lo que explica por qué esa cuenta se ve así.
    """
    try:
        filas = db.fetch_all(_SQL_CORRIDAS, ((codigo_cli or "").upper(),)) or []
    except Exception as _e:                              # pragma: no cover
        from modules._lib.silencios import avisar
        avisar(__name__, "corridas_del_cliente", _e)
        return []

    out = []
    for f in filas:
        md = f.get("metadata") or {}
        if isinstance(md, str):                          # jsonb como texto
            import json
            try:
                md = json.loads(md)
            except ValueError:
                md = {}
        antes = md.get("antes") or []
        despues = {d.get("id"): d for d in (md.get("despues") or [])}
        # Los cheques que soltó, contados por factura: la otra mitad de la
        # historia ("¿y quién pagaba esto?").
        sueltos: dict[int, int] = {}
        for lk in (md.get("links") or []):
            sueltos[lk.get("id_fact")] = sueltos.get(lk.get("id_fact"), 0) + 1
        facturas = []
        for a in antes:
            d = despues.get(a.get("id")) or {}
            facturas.append({
                "id_factura": a.get("id"),
                "numf": a.get("numf"),
                "importe": a.get("importe"),
                "abono_antes": a.get("abono"),
                "saldo_antes": a.get("saldo"),
                "stat_antes": a.get("stat"),
                "abono_despues": d.get("abono"),
                "saldo_despues": d.get("saldo"),
                "stat_despues": d.get("stat"),
                "cambio": (d.get("abono") != a.get("abono")
                           or d.get("stat") != a.get("stat")),
                "cheques_sueltos": sueltos.get(a.get("id"), 0),
            })
        out.append({
            "id_mov_doble": f.get("id_mov_doble"),
            "fecha": f.get("fecha"),
            "usuario": f.get("usuario"),
            "pool": f.get("pool"),
            "n_facturas": md.get("n_facturas") or len(facturas),
            "n_links_borrados": md.get("n_links_borrados") or 0,
            "facturas": facturas,
        })
    return out


# ── Volver a aplicar los cobros al reparto nuevo ────────────────────────────
#
# 🚨 TMT 2026-09-16 (dueña, cuando entendió por qué la 177617 no mostraba
# ningún cheque): *"no entiendo por qué pasó, arreglalo esta vez como puedas
# pero no debería volver a pasar"*.
#
# Por qué pasaba: el totalizar reparte los abonos de nuevo, y como el vínculo
# viejo ya no coincide con la factura que ese cheque paga, lo BORRABA. La
# salida no era borrar: era volver a aplicarlo al reparto nuevo.
#
# El reparto es el mismo criterio que usa el totalizar —de la factura más
# vieja a la más nueva— y los cobros entran por su fecha, del más viejo al más
# nuevo: así el cheque de julio paga la factura de mayo, no la de agosto. Un
# cobro puede quedar PARTIDO entre dos facturas; eso no es un invento nuevo,
# `aplicar_a_factura` ya inserta una fila por aplicación.
#
# ⚠ Nunca se aplica más que el abono de la factura ni más que el importe del
# cobro: el health de facturas sobre-aplicadas suma esta tabla.


def repartir(facturas: list[dict], cobros: list[dict]) -> list[dict]:
    """Decide qué pedazo de cada cobro va a cada factura. Sin tocar la base.

    `facturas`: [{"id_fact", "abono"}] EN EL ORDEN del reparto (la más vieja
    primero). `cobros`: [{"id_cheque", "importe", ...}] en orden de llegada.

    Devuelve [{"id_fact", "id_cheque", "importe", ...}] — una fila por pedazo.

    Lo que sobra de abono queda sin vínculo a propósito: es abono que no vino
    de un cobro de Programa Core (el backfill histórico de Asinfo, el
    dbf-import). Inventarle un cheque sería peor que dejarlo sin uno.
    """
    pendientes = [dict(c, _resto=round(float(c.get("importe") or 0), 2))
                  for c in cobros
                  if round(float(c.get("importe") or 0), 2) > 0]
    salida = []
    for f in facturas:
        falta = round(float(f.get("abono") or 0), 2)
        if falta <= 0:
            continue
        for c in pendientes:
            if falta <= 0:
                break
            if c["_resto"] <= 0:
                continue
            pedazo = round(min(falta, c["_resto"]), 2)
            if pedazo <= 0:
                continue
            c["_resto"] = round(c["_resto"] - pedazo, 2)
            falta = round(falta - pedazo, 2)
            salida.append({
                "id_fact": f["id_fact"],
                "id_cheque": c.get("id_cheque"),
                "importe": pedazo,
                "fechaing": c.get("fechaing"),
                "codigo_cli": c.get("codigo_cli"),
                "no_banco": c.get("no_banco"),
                "tipo": c.get("tipo"),
            })
    return salida


def reaplicar(conn, *, facturas: list[dict], cobros: list[dict],
              usuario: str = "web") -> int:
    """Escribe en `chequesxfact` el reparto que decide `repartir()`.

    ⚠ Sólo cobros VIVOS: reponerle un vínculo a un cheque anulado es la
    aplicación fantasma que describe el reverso del totalizar — el abono deja
    de cuadrar y bloquea futuras anulaciones con un falso "cheque vivo".

    Devuelve cuántos vínculos escribió; `reaplicar_ids` devuelve además cuáles.
    """
    return len(reaplicar_ids(conn, facturas=facturas, cobros=cobros,
                             usuario=usuario))


def reaplicar_ids(conn, *, facturas: list[dict], cobros: list[dict],
                  usuario: str = "web") -> list[int]:
    """Como `reaplicar`, pero devuelve los `id_chequexfact` que insertó.

    🚨 17/09/2026: el ↺ del totalizar reponía los vínculos viejos SIN sacar
    estos —no sabía que existían— y cada cheque quedaba aplicado dos veces
    (probado en local: X 200 de 100, Y 100 de 50). Los ids van a la metadata
    del totalizar para que el reverso sepa exactamente qué borrar.
    """
    plan = repartir(facturas, cobros)
    ids: list[int] = []
    for p in plan:
        vivo = db.fetch_one(
            "SELECT importe, codigo_cli, no_banco, fecha, fechaing"
            "  FROM scintela.cheque"
            " WHERE id_cheque = %s AND COALESCE(stat, '') <> 'X'",
            (p["id_cheque"],), conn=conn)
        if not vivo:
            continue
        # Nunca dos veces el mismo pedazo: si ya está, no se repite.
        ya = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.chequesxfact"
            " WHERE id_cheque = %s AND id_fact = %s AND importe = %s",
            (p["id_cheque"], p["id_fact"], p["importe"]), conn=conn) or {}
        if int(ya.get("n") or 0):
            continue
        fact = db.fetch_one(
            "SELECT stat, abono, saldo, vencimiento FROM scintela.factura"
            " WHERE id_factura = %s", (p["id_fact"],), conn=conn) or {}
        fila = db.fetch_one(
            "INSERT INTO scintela.chequesxfact"
            "  (id_cheque, id_fact, fechaing, codigo_cli, importe, no_banco,"
            "   tipo, stat_f, fecha_venci_f, abono_f, saldo_f, usuario_crea)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " RETURNING id_chequexfact",
            # ⚠ `fechaing` es NOT NULL: un link viejo sin fecha —los hay—
            # tiraba la reposición entera. Cae a la del cobro. 16/09/2026.
            (p["id_cheque"], p["id_fact"],
             p.get("fechaing") or vivo.get("fechaing") or vivo.get("fecha"),
             p.get("codigo_cli") or vivo.get("codigo_cli"), p["importe"],
             p.get("no_banco") or vivo.get("no_banco"), p.get("tipo"),
             (fact.get("stat") or "").strip(), fact.get("vencimiento"),
             fact.get("abono"), fact.get("saldo"), usuario),
            conn=conn,
        ) or {}
        ids.append(int(fila.get("id_chequexfact") or 0))
    return ids


def _plan_reponer(codigo_cli: str, conn=None) -> dict:
    """Qué haría "volver a ponerlos en sus facturas" para este cliente.

    Devuelve {"facturas", "cobros", "plan"}; no escribe nada.

    🚨 17/09/2026, dos bugs medidos contra producción:

    · Las facturas del reparto eran las que TENÍAN vínculo antes (las del
      historial). Pero el totalizar muda el abono a las más viejas, que
      muchas veces nunca tuvieron cheque: en MTM la 176310 y la 176386
      quedaron T con 550 y 579 de abono y el botón no las alcanzaba (57
      facturas, $118.604 en total). Ahora las facturas son las de la CORRIDA
      (el `antes` del mov_doble), que es lo que el totalizar tocó.

    · Los cobros se contaban por fila del historial: un cheque que figura en
      dos corridas (MHC, 9 casos) entraba dos veces, y uno ya re-aplicado a
      OTRA factura entraba entero de nuevo. Ahora es un cobro por cheque, y
      lo que le queda por vincular es lo que el historial dice que pagó (sin
      repetir un pedazo) menos lo que YA tiene vivo, nunca más que su importe.
    """
    codigo_cli = (codigo_cli or "").strip().upper()
    ids_corrida = db.fetch_all(
        """
        SELECT DISTINCT (a->>'id')::bigint AS id_fact
          FROM scintela.mov_doble m
          CROSS JOIN LATERAL jsonb_array_elements(m.metadata -> 'antes') a
         WHERE m.tipo = 'totalizar_estado_cuenta'
           AND m.estado = 'activo'
           AND m.metadata ->> 'codigo_cli' = %s
        """,
        (codigo_cli,), conn=conn) or []
    ids = {int(r["id_fact"]) for r in ids_corrida if r.get("id_fact")}
    # Corridas sin `antes` (anteriores al 07/08): al menos las facturas que
    # el historial nombra.
    for r in db.fetch_all(
            f"SELECT DISTINCT id_fact FROM {TABLA} WHERE codigo_cli = %s",
            (codigo_cli,), conn=conn) or []:
        ids.add(int(r["id_fact"]))
    facturas = []
    if ids:
        facturas = db.fetch_all(
            """
            SELECT f.id_factura AS id_fact,
                   ROUND(COALESCE(f.abono, 0) - COALESCE((
                       SELECT SUM(x.importe) FROM scintela.chequesxfact x
                        WHERE x.id_fact = f.id_factura), 0), 2) AS abono
              FROM scintela.factura f
             WHERE f.codigo_cli = %s
               AND f.id_factura = ANY(%s)
               AND COALESCE(f.importe, 0) > 0
             ORDER BY f.fecha ASC, f.id_factura ASC
            """,
            (codigo_cli, sorted(ids)), conn=conn) or []
    cobros = db.fetch_all(
        f"""
        WITH pedazos AS (
            SELECT DISTINCT t.id_cheque, t.id_fact, t.importe
              FROM {TABLA} t
             WHERE t.codigo_cli = %s
        ),
        hist AS (
            SELECT id_cheque, SUM(importe) AS pagado
              FROM pedazos GROUP BY id_cheque
        )
        SELECT h.id_cheque,
               ROUND(LEAST(h.pagado, COALESCE(c.importe, 0))
                     - COALESCE((SELECT SUM(x.importe)
                                   FROM scintela.chequesxfact x
                                  WHERE x.id_cheque = h.id_cheque), 0),
                     2)                                        AS importe,
               COALESCE(c.fechaing, c.fecha)                   AS fechaing,
               c.codigo_cli, c.no_banco,
               (SELECT MIN(t.tipo) FROM {TABLA} t
                 WHERE t.id_cheque = h.id_cheque)              AS tipo
          FROM hist h
          JOIN scintela.cheque c ON c.id_cheque = h.id_cheque
         WHERE COALESCE(c.stat, '') <> 'X'
         ORDER BY COALESCE(c.fechaing, c.fecha) NULLS LAST, h.id_cheque
        """,
        (codigo_cli,), conn=conn) or []
    cobros = [c for c in cobros if round(float(c.get("importe") or 0), 2) > 0]
    return {"facturas": facturas, "cobros": cobros,
            "plan": repartir(facturas, cobros)}


def reponer_cliente(codigo_cli: str, usuario: str = "web") -> dict:
    """Reconstruye los vínculos de un cliente YA totalizado, sin mover plata.

    Para lo de atrás: toma los cobros que el historial guardó, los reparte
    sobre los abonos sin vínculo de las facturas que el totalizar tocó y
    escribe los vínculos. No toca abono, saldo ni stat de ninguna factura —
    sólo dice quién pagó qué. El criterio está en `_plan_reponer`.
    """
    codigo_cli = (codigo_cli or "").strip().upper()
    asegurar_tabla()
    with db.tx() as conn:
        # Las facturas de la corrida, lockeadas: que no entre una cobranza
        # entre que se mira el abono sin vínculo y se escribe.
        db.execute(
            "SELECT id_factura FROM scintela.factura"
            " WHERE codigo_cli = %s FOR UPDATE", (codigo_cli,), conn=conn)
        p = _plan_reponer(codigo_cli, conn=conn)
        n = reaplicar(conn, facturas=p["facturas"], cobros=p["cobros"],
                      usuario=usuario)
    return {"codigo_cli": codigo_cli, "cobros": len(p["cobros"]),
            "vinculos": n}


def cuantos_por_reponer(codigo_cli: str) -> int:
    """Cobros de este cliente que el botón "volver a ponerlos" vincularía.

    Es lo que decide si el botón aparece: en una cuenta sana no hay nada que
    reponer y el botón no tiene por qué estar. 17/09/2026: se cuenta el PLAN
    (lo que de verdad escribiría), no las filas del historial — con filas
    viejas ya re-aplicadas a otra factura el botón quedaba para siempre.
    """
    if not asegurar_tabla():
        return 0
    try:
        return len({p["id_cheque"] for p in
                    _plan_reponer(codigo_cli)["plan"]})
    except Exception as _e:                              # pragma: no cover
        from modules._lib.silencios import avisar
        avisar(__name__, "cuantos_por_reponer", _e)
        return 0
