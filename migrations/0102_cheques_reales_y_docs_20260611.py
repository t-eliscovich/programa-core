"""0102 — Cheques reales mal cargados como EFECTIVO + docs de banco (11/06).

Confirmado por la dueña contra CHEQUES.DBF (11/06 20:37):
  TPZ  70.00 = cheque BOLIVARIANO (NB 37, Z, fecha 02/06, fechad 12/06)
  TPZ 310.00 = cheque BANECUADOR  (NB 66, Z, fecha 20/05, fechad 12/06)
  TPZ 200.00 = cheque BANECUADOR  (NB 66, Z, fecha 28/05, fechad 12/06)
  MOR 943.00 = cheque PICHINCHA   (NB 10, Z, fecha 13/07, fechad 13/07)
  MOR 800.00 = cheque PICHINCHA   (NB 10, Z, fecha 09/06, fechad 12/06)
  MOR 800.00 = cheque PICHINCHA   (NB 10, Z, fecha 22/06, fechad 22/06)
En PC entraron como 99 EFECTIVO (stat B, o C si ya corrio la 0101) con
entrada en caja -> [A] compensa la caja (S CORR) y re-codifica el cheque
con los datos del dBase (vuelve a cartera Z, sin mov banco: se depositan
por el flujo normal cuando toque).

[B] Docs de banco que paso la dueña (cheque.doc_banco + numreferencia y
numreferencia_manual del mov linkeado, para que concilien por referencia):
  GPU  350.00 -> 121569883   LMS 2549.67 -> 124025552
  RYU 4582.64 -> 126395676

Idempotente: [A] exige no_banco=99; [B] solo escribe si difiere.
NO se toca: GPU 150 (falta tipearlo en el dBase) ni CEL 430.80 (es el
dBase el que tiene 430.00 mal -> corregir alla antes del proximo sync).
"""
from datetime import date

DIA = date(2026, 6, 11)

# (cli, importe, no_banco, banco_txt, fecha_cheque, fechad)
RECODIFICAR = [
    ("TPZ", 70.00, 37, "BOLIVARIA", date(2026, 6, 2), date(2026, 6, 12)),
    ("TPZ", 310.00, 66, "BANECUADOR", date(2026, 5, 20), date(2026, 6, 12)),
    ("TPZ", 200.00, 66, "BANECUADOR", date(2026, 5, 28), date(2026, 6, 12)),
    ("MOR", 943.00, 10, "PICHINCHA", date(2026, 7, 13), date(2026, 7, 13)),
    ("MOR", 800.00, 10, "PICHINCHA", date(2026, 6, 9), date(2026, 6, 12)),
    ("MOR", 800.00, 10, "PICHINCHA", date(2026, 6, 22), date(2026, 6, 22)),
]

DOCS = [
    ("GPU", 350.00, "121569883"),
    ("LMS", 2549.67, "124025552"),
    ("RYU", 4582.64, "126395676"),
]


def run(conn):
    cur = conn.cursor()
    for t in ("scintela.cheque", "scintela.caja", "scintela.chequextransaccion"):
        cur.execute("SELECT to_regclass(%s)", (t,))
        if cur.fetchone()[0] is None:
            print(f"  0102 no-op: {t} no existe (test/CI)")
            return
    cur.execute(
        "SELECT COUNT(*) FROM scintela.cheque WHERE fecha = %s AND no_banco IN (90, 99)",
        (DIA,))
    if (cur.fetchone() or [0])[0] == 0:
        print("  0102 no-op: sin cheques del 11/06 en esta DB")
        return

    # [A] re-codificar — agrupado por (cli, importe) para asignar los
    # duplicados (MOR 800 x2) en orden de id_cheque.
    pend = {}
    for spec in RECODIFICAR:
        pend.setdefault((spec[0], round(spec[1], 2)), []).append(spec)
    for (cli, imp), specs in pend.items():
        cur.execute(
            """
            SELECT id_cheque FROM scintela.cheque
             WHERE fecha = %s AND UPPER(TRIM(codigo_cli)) = %s
               AND ABS(importe - %s) < 0.01 AND no_banco = 99
               AND stat IN ('B', 'C')
               AND NOT EXISTS (SELECT 1 FROM scintela.chequextransaccion x
                                WHERE x.id_cheque = scintela.cheque.id_cheque)
             ORDER BY id_cheque
            """,
            (DIA, cli, imp),
        )
        ids = [r[0] for r in cur.fetchall()]
        if len(ids) != len(specs):
            print(f"  [A] {cli} {imp:,.2f}: {len(ids)} candidatos vs {len(specs)} esperados -> SKIP")
            continue
        for id_cheque, (_, _, nb, banco_txt, f_cheque, f_dep) in zip(ids, specs):
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
                    VALUES (%s, 'S', %s, %s, NULL, %s, 'mig-0102')
                    """,
                    (DIA, imp, f"CORR ch{id_cheque} 99->{nb}"[:80], id_cheque),
                )
            cur.execute(
                """
                UPDATE scintela.cheque
                   SET no_banco=%s, banco=%s, stat='Z',
                       fecha=%s, fechad=%s, fechaing=NULL, fechaout=NULL,
                       observacion = COALESCE(observacion||' | ','')||%s,
                       usuario_modifica='mig-0102', fecha_modifica=CURRENT_TIMESTAMP
                 WHERE id_cheque=%s
                """,
                (nb, banco_txt, f_cheque, f_dep,
                 f"[E] mig-0102: era cheque {banco_txt} no efectivo (dBase); caja compensada",
                 id_cheque),
            )
            print(f"  [A] {cli} {imp:,.2f}: cheque #{id_cheque} -> {nb} {banco_txt} Z fechad {f_dep}")

    # [B] docs de banco + referencia en el mov linkeado
    for cli, imp, doc in DOCS:
        cur.execute(
            """
            UPDATE scintela.cheque
               SET doc_banco = %s,
                   usuario_modifica='mig-0102', fecha_modifica=CURRENT_TIMESTAMP
             WHERE fecha = %s AND UPPER(TRIM(codigo_cli)) = %s
               AND ABS(importe - %s) < 0.01 AND no_banco = 90
               AND COALESCE(doc_banco, '') IS DISTINCT FROM %s
            RETURNING id_cheque
            """,
            (doc, DIA, cli, imp, doc),
        )
        ids = [r[0] for r in cur.fetchall()]
        n_tb = 0
        for id_cheque in ids:
            cur.execute(
                """
                UPDATE scintela.transacciones_bancarias tb
                   SET numreferencia = %s, numreferencia_manual = %s
                  FROM scintela.chequextransaccion x
                 WHERE x.id_transaccion = tb.id_transaccion
                   AND x.id_cheque = %s
                """,
                (doc, doc, id_cheque),
            )
            n_tb += cur.rowcount
        print(f"  [B] {cli} {imp:,.2f}: doc {doc} en {len(ids)} cheque(s) + {n_tb} mov(s) banco")
    print("  0102 listo. Recordar: GPU 150 falta tipear en dBase; CEL corregir 430.00->430.80 en dBase")
