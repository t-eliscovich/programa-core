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
           m.usuario                     AS usuario_totalizar,
           ch.no_cheque,
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
