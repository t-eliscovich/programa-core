"""0100 — Backfill 1 a 1: depósitos directos DEP.PICH (90) del 11/06/2026.

Contexto (pedido dueña 2026-06-11): las cobranzas con banco 90 cargadas
ANTES del deploy de paridad ALTAS.PRG quedaron stat='B' SIN movimiento
bancario en PC y SIN link chequextransaccion. La dueña: "prefiero que lo
hagas vos 1 a 1 así no cargamos información de más".

Regla 1-a-1 (lección 2026-06-11: comparar MOVIMIENTOS, no totales):
  [1] candidatos = cheques fecha=11/06, no_banco=90, stat='B', importe>0,
      SIN chequextransaccion.
  [2] para cada uno, buscar un mov banco DE del 11/06 en Pichincha con el
      MISMO importe (multiset, asignación única) que NO tenga cheque
      linkeado → si existe (lo trajo el sync dBase), SOLO crear el link.
  [3] si no existe → INSERT del movimiento (DOC='DE', concepto '1 ch.CLI',
      numreferencia=doc_banco o id_cheque, saldo running al tail) + link.
  [4] nada más se toca. Idempotente: con cxt creado, el cheque sale de [1].
"""
from datetime import date

DIA = date(2026, 6, 11)


def run(conn):
    cur = conn.cursor()
    for t in ("scintela.cheque", "scintela.transacciones_bancarias",
              "scintela.chequextransaccion", "scintela.banco"):
        cur.execute("SELECT to_regclass(%s)", (t,))
        if cur.fetchone()[0] is None:
            print(f"  0100 no-op: {t} no existe en esta DB (test/CI)")
            return

    # Banco real Pichincha (los códigos >=90 son virtuales del dropdown).
    cur.execute(
        "SELECT no_banco FROM scintela.banco "
        "WHERE no_banco < 90 AND nombre ILIKE %s ORDER BY no_banco LIMIT 1",
        ("%PICHIN%",),
    )
    row = cur.fetchone()
    banco_real = int(row[0]) if row else 10

    cur.execute(
        """
        SELECT c.id_cheque, UPPER(TRIM(c.codigo_cli)), c.importe,
               COALESCE(NULLIF(TRIM(c.doc_banco), ''), '')
          FROM scintela.cheque c
         WHERE c.fecha = %s AND c.no_banco = 90 AND c.stat = 'B'
           AND c.importe > 0
           AND NOT EXISTS (SELECT 1 FROM scintela.chequextransaccion x
                            WHERE x.id_cheque = c.id_cheque)
         ORDER BY c.id_cheque
        """,
        (DIA,),
    )
    cheques = cur.fetchall()
    if not cheques:
        print("  0100 no-op: sin cheques DEP.PICH (90) stat B del 11/06 sin link")
        return

    # Movs DE existentes del día sin cheque linkeado (posibles filas del sync).
    cur.execute(
        """
        SELECT tb.id_transaccion, tb.importe
          FROM scintela.transacciones_bancarias tb
         WHERE tb.no_banco = %s AND tb.fecha = %s
           AND UPPER(TRIM(COALESCE(tb.documento,''))) = 'DE'
           AND NOT EXISTS (SELECT 1 FROM scintela.chequextransaccion x
                            WHERE x.id_transaccion = tb.id_transaccion)
         ORDER BY tb.id_transaccion
        """,
        (banco_real, DIA),
    )
    libres = [{"id": r[0], "importe": float(r[1] or 0), "usado": False}
              for r in cur.fetchall()]

    # Saldo running al tail del banco (para los INSERT nuevos).
    cur.execute(
        """
        SELECT COALESCE(saldo, 0) FROM scintela.transacciones_bancarias
         WHERE no_banco = %s
         ORDER BY fecha DESC, id_transaccion DESC LIMIT 1
        """,
        (banco_real,),
    )
    row = cur.fetchone()
    saldo = float(row[0]) if row else 0.0

    n_link, n_ins = 0, 0
    for id_cheque, cli, importe, doc in cheques:
        imp = float(importe or 0)
        # [2] aparear con un mov existente del mismo importe (1 a 1).
        par = next((l for l in libres
                    if not l["usado"] and abs(l["importe"] - imp) < 0.01), None)
        if par:
            par["usado"] = True
            id_t = par["id"]
            accion = "link a mov existente"
            n_link += 1
        else:
            # [3] no está → crearlo (paridad ALTAS.PRG L170-186).
            saldo = round(saldo + imp, 2)
            cur.execute(
                """
                INSERT INTO scintela.transacciones_bancarias
                    (fecha, documento, concepto, importe, saldo, stat,
                     no_banco, prov, numreferencia, usuario_crea)
                VALUES (%s, 'DE', %s, %s, %s, 'A', %s, %s, %s, 'mig-0100')
                RETURNING id_transaccion
                """,
                (DIA, f"1 ch.{cli}"[:50], imp, saldo, banco_real,
                 cli[:5], (doc or str(id_cheque))[:40]),
            )
            id_t = cur.fetchone()[0]
            accion = "mov NUEVO"
            n_ins += 1
        cur.execute(
            """
            INSERT INTO scintela.chequextransaccion
                (id_cheque, id_transaccion, fecha, stat_ch, usuario_crea)
            VALUES (%s, %s, %s, 'D', 'mig-0100')
            """,
            (id_cheque, id_t, DIA),
        )
        print(f"  cheque #{id_cheque} {cli} ${imp:,.2f} -> {accion} (tb #{id_t})")
    print(f"  0100 listo: {len(cheques)} cheques / {n_link} linkeados / {n_ins} movs creados")
