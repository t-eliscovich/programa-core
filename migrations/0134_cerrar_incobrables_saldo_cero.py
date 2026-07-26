"""Cerrada = nada: snapshot de incobrables + zero-saldo en las facturas 'T'.

TMT 2026-07-26 (duena: "Si esta cerrada no deberia tener nada" +
"Si pero ajustalo para el futuro").

CONTEXTO
--------
El dBase legacy marca algunas facturas como STAT='T' (cancelada por el total)
pero deja un SALDO residual > 0: son castigos / incobrables que alguien cerro
para dejar de perseguirlos, sin poner el saldo en cero. Al importar, PC copiaba
ese estado tal cual, asi que quedaban facturas "cerradas con deuda".

Esto NO afecta la cartera: tanto PC (`stat IN (Z,A) AND saldo>0`) como el dBase
(`STAT $ "ZA"`) excluyen las 'T'. Ninguno cuenta esa plata. Es solo dato sucio.

QUE HACE (idempotente)
----------------------
1. Crea `scintela.incobrable_castigo` y ARCHIVA ahi cada factura 'T' con
   saldo>0 ANTES de tocarla (numf, cliente, importe, abono, saldo castigado).
   Asi no se pierde el registro de cuanto se dejo de cobrar por cliente.
2. Pone `saldo = 0` en esas facturas 'T'. El abono real se conserva; la
   diferencia importe-abono queda como castigo en la tabla de arriba, no como
   saldo vivo. La cartera no se mueve un centavo.

FUTURO: el importador (`scripts/import_dbf._map_factura`) ya normaliza stat 'T'
-> saldo 0 desde 2026-07-26, asi que ninguna importacion nueva vuelve a traer
una factura cerrada con saldo. Esta migracion limpia lo que ya estaba adentro.

REVERSIBLE: el detalle vive en `scintela.incobrable_castigo`; las filas tocadas
quedan con usuario_modifica='mig-0134'.

GUARD to_regclass OBLIGATORIO: no-op en CI/test donde scintela.* no existe.
Prints ASCII (consola Windows cp1252).
"""
from __future__ import annotations


def run(conn) -> None:
    cur = conn.cursor()

    # No-op en CI/test (scintela.* ausente).
    cur.execute("SELECT to_regclass('scintela.factura')")
    if cur.fetchone()[0] is None:
        print("0134: scintela.factura ausente (CI/test) - no-op")
        return

    # 1) Tabla de archivo de castigos (snapshot de lo que se dejo de cobrar).
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scintela.incobrable_castigo (
            id_factura      INTEGER PRIMARY KEY,
            numf            INTEGER,
            codigo_cli      TEXT,
            fecha           DATE,
            importe         NUMERIC(14,2),
            abono           NUMERIC(14,2),
            saldo_castigado NUMERIC(14,2),
            fecha_castigo   DATE NOT NULL DEFAULT CURRENT_DATE,
            usuario_crea    TEXT
        )
        """
    )

    # Archiva las 'T' con saldo residual > 0.01 que todavia no esten archivadas.
    cur.execute(
        """
        INSERT INTO scintela.incobrable_castigo
            (id_factura, numf, codigo_cli, fecha, importe, abono,
             saldo_castigado, usuario_crea)
        SELECT f.id_factura, f.numf, f.codigo_cli, f.fecha,
               f.importe, f.abono, f.saldo, 'mig-0134'
          FROM scintela.factura f
         WHERE UPPER(TRIM(COALESCE(f.stat, ''))) = 'T'
           AND COALESCE(f.saldo, 0) > 0.01
        ON CONFLICT (id_factura) DO NOTHING
        """
    )
    n_arch = cur.rowcount

    # Resumen por cliente para el log de deploy (auditable).
    cur.execute(
        """
        SELECT codigo_cli, COUNT(*) AS n, SUM(saldo_castigado) AS total
          FROM scintela.incobrable_castigo
         WHERE usuario_crea = 'mig-0134'
         GROUP BY codigo_cli
         ORDER BY SUM(saldo_castigado) DESC
        """
    )
    filas = cur.fetchall()
    total_global = sum(float(r[2] or 0) for r in filas)
    print(f"0134: archivadas {n_arch} facturas 'T' con saldo residual "
          f"(total castigado ${total_global:,.2f}) en incobrable_castigo")
    for cod, n, total in filas[:15]:
        print(f"0134:   {str(cod):<6} {int(n):>4} fact  ${float(total or 0):>14,.2f}")

    # 2) Zero-saldo en esas facturas (cerrada = nada). No mueve cartera (T ya
    #    esta excluida de Z+A). El abono real se conserva.
    cur.execute(
        """
        UPDATE scintela.factura
           SET saldo = 0, usuario_modifica = 'mig-0134'
         WHERE UPPER(TRIM(COALESCE(stat, ''))) = 'T'
           AND COALESCE(saldo, 0) > 0.01
        """
    )
    n_zero = cur.rowcount
    print(f"0134: saldo->0 en {n_zero} facturas cerradas (T). Cartera intacta.")
