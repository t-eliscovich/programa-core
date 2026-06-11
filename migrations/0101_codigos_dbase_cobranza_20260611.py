"""0101 — Códigos de banco emisor del 11/06 = dBase (CHEQUES.DBF 11/06 20:37).

Pedido dueña: "podés encontrar los cheques en el dbase y ver qué estado son
90 99 o qué y los reemplazamos por eso". Tablita PC vs DBF apareada por
(cliente, importe):

  [A] CAMBIAR código 99->90 (cargados EFECTIVO, el dBase dice DEP.PICH):
      GPU 350.00 / FES 100.00 / MIM 3000.00
      -> compensa la caja (S CORR), crea el mov DE en Pichincha real
         (saldo running) + link chequextransaccion, no_banco=90.
  [B] stat B->C para TODOS los 99 del 11/06 (paridad PASOCAJA: el dBase
      los tiene C; PC los creó B con el código viejo). La caja ya existe.
  [C] PIB -177.17: el dBase lo tiene NB=90 stat B con su mov NEGATIVO en
      PICHINCHA -> flip Z->B + mov DE -177.17 + link (paridad exacta).
  [D] Solo REPORTE (no se toca): CEL PC 430.80 vs dBase 430.00 (importe
      difiere 0.80 — corregir a mano el que esté mal) y los PC-sin-par
      (GPU 150 / MOR 943, 800, 800 / TPZ 310, 200, 70) que el dBase aún
      no tiene tipeados.

Idempotente: [A] exige no_banco=99 sin cxt; [B] exige stat='B'; [C] exige
stat='Z'. Re-correr es no-op.
"""
from datetime import date

DIA = date(2026, 6, 11)
CAMBIAR_A_90 = [("GPU", 350.00), ("FES", 100.00), ("MIM", 3000.00)]


def _pichincha_real(cur):
    cur.execute(
        "SELECT no_banco FROM scintela.banco "
        "WHERE no_banco < 90 AND nombre ILIKE %s ORDER BY no_banco LIMIT 1",
        ("%PICHIN%",),
    )
    r = cur.fetchone()
    return int(r[0]) if r else 10


def _saldo_tail(cur, banco):
    cur.execute(
        "SELECT COALESCE(saldo,0) FROM scintela.transacciones_bancarias "
        "WHERE no_banco = %s ORDER BY fecha DESC, id_transaccion DESC LIMIT 1",
        (banco,),
    )
    r = cur.fetchone()
    return float(r[0]) if r else 0.0


def _insert_de(cur, banco, imp, saldo, cli, numref):
    cur.execute(
        """
        INSERT INTO scintela.transacciones_bancarias
            (fecha, documento, concepto, importe, saldo, stat,
             no_banco, prov, numreferencia, usuario_crea)
        VALUES (%s, 'DE', %s, %s, %s, 'A', %s, %s, %s, 'mig-0101')
        RETURNING id_transaccion
        """,
        (DIA, f"1 ch.{cli}"[:50], imp, saldo, banco, cli[:5], str(numref)[:40]),
    )
    return cur.fetchone()[0]


def run(conn):
    cur = conn.cursor()
    for t in ("scintela.cheque", "scintela.transacciones_bancarias",
              "scintela.chequextransaccion", "scintela.caja"):
        cur.execute("SELECT to_regclass(%s)", (t,))
        if cur.fetchone()[0] is None:
            print(f"  0101 no-op: {t} no existe (test/CI)")
            return
    # Guard anti-entorno: sin los cheques del 11/06 no hay nada que hacer.
    cur.execute(
        "SELECT COUNT(*) FROM scintela.cheque "
        "WHERE fecha = %s AND no_banco IN (90, 99)", (DIA,))
    if (cur.fetchone() or [0])[0] == 0:
        print("  0101 no-op: sin cheques 90/99 del 11/06 en esta DB")
        return

    pich = _pichincha_real(cur)
    saldo = _saldo_tail(cur, pich)

    # [A] 99 -> 90 con migracion de movimientos
    for cli, imp in CAMBIAR_A_90:
        cur.execute(
            """
            SELECT id_cheque, COALESCE(NULLIF(TRIM(doc_banco),''),'') FROM scintela.cheque
             WHERE fecha = %s AND UPPER(TRIM(codigo_cli)) = %s
               AND ABS(importe - %s) < 0.01 AND no_banco = 99
               AND NOT EXISTS (SELECT 1 FROM scintela.chequextransaccion x
                                WHERE x.id_cheque = scintela.cheque.id_cheque)
             ORDER BY id_cheque
            """,
            (DIA, cli, imp),
        )
        rows = cur.fetchall()
        if len(rows) != 1:
            print(f"  [A] {cli} {imp:,.2f}: {len(rows)} candidatos -> SKIP (revisar a mano)")
            continue
        id_cheque, doc = rows[0]
        # compensar caja (la entrada E del alta)
        cur.execute(
            "SELECT id_caja FROM scintela.caja "
            "WHERE id_cheque = %s AND tipo = 'E' AND ABS(importe - %s) < 0.01 "
            "ORDER BY id_caja LIMIT 1",
            (id_cheque, imp),
        )
        if cur.fetchone():
            cur.execute(
                """
                INSERT INTO scintela.caja
                    (fecha, tipo, importe, concepto, saldo, id_cheque, usuario_crea)
                VALUES (%s, 'S', %s, %s, NULL, %s, 'mig-0101')
                """,
                (DIA, imp, f"CORR ch{id_cheque} 99->90"[:80], id_cheque),
            )
        saldo = round(saldo + imp, 2)
        id_t = _insert_de(cur, pich, imp, saldo, cli, doc or id_cheque)
        cur.execute(
            "INSERT INTO scintela.chequextransaccion "
            "(id_cheque, id_transaccion, fecha, stat_ch, usuario_crea) "
            "VALUES (%s, %s, %s, 'D', 'mig-0101')",
            (id_cheque, id_t, DIA),
        )
        cur.execute(
            "UPDATE scintela.cheque SET no_banco=90, banco='DEP.PICH.', stat='B', "
            "fechaing=%s, fechaout=NULL, "
            "observacion = COALESCE(observacion||' | ','')||%s, "
            "usuario_modifica='mig-0101', fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (DIA, "[E] mig-0101: 99 -> 90 segun dBase (caja compensada, mov banco creado)", id_cheque),
        )
        print(f"  [A] {cli} {imp:,.2f}: cheque #{id_cheque} 99->90, caja S, DE tb #{id_t}")

    # [B] stat B->C para todos los 99 del dia (paridad PASOCAJA)
    cur.execute(
        """
        UPDATE scintela.cheque
           SET stat='C', fechaout=%s,
               usuario_modifica='mig-0101', fecha_modifica=CURRENT_TIMESTAMP
         WHERE fecha = %s AND no_banco = 99 AND stat = 'B'
        """,
        (DIA, DIA),
    )
    print(f"  [B] {cur.rowcount} cheques EFECTIVO del 11/06: stat B -> C (paridad dBase)")

    # [C] PIB -177.17 -> NB90 stat B + mov negativo (paridad exacta dBase)
    cur.execute(
        """
        SELECT id_cheque FROM scintela.cheque
         WHERE fecha = %s AND UPPER(TRIM(codigo_cli)) = 'PIB'
           AND ABS(importe - (-177.17)) < 0.01 AND no_banco = 90 AND stat = 'Z'
           AND NOT EXISTS (SELECT 1 FROM scintela.chequextransaccion x
                            WHERE x.id_cheque = scintela.cheque.id_cheque)
         ORDER BY id_cheque LIMIT 1
        """,
        (DIA,),
    )
    r = cur.fetchone()
    if r:
        id_cheque = r[0]
        saldo = round(saldo - 177.17, 2)
        id_t = _insert_de(cur, pich, -177.17, saldo, "PIB", id_cheque)
        cur.execute(
            "INSERT INTO scintela.chequextransaccion "
            "(id_cheque, id_transaccion, fecha, stat_ch, usuario_crea) "
            "VALUES (%s, %s, %s, 'D', 'mig-0101')",
            (id_cheque, id_t, DIA),
        )
        cur.execute(
            "UPDATE scintela.cheque SET stat='B', fechaing=%s, "
            "observacion = COALESCE(observacion||' | ','')||%s, "
            "usuario_modifica='mig-0101', fecha_modifica=CURRENT_TIMESTAMP "
            "WHERE id_cheque=%s",
            (DIA, "[E] mig-0101: Z -> B segun dBase (mov DE negativo creado)", id_cheque),
        )
        print(f"  [C] PIB -177.17: cheque #{id_cheque} Z->B + DE negativo tb #{id_t}")
    else:
        print("  [C] PIB -177.17 ya estaba B o no se encontro -> skip")

    # [D] reporte
    print("  [D] REVISAR A MANO (no se toco):")
    print("      CEL: PC 430.80 vs dBase 430.00 (importe difiere 0.80)")
    print("      PC sin par en dBase: GPU 150 / MOR 943, 800, 800 / TPZ 310, 200, 70")
    print("  0101 listo")
