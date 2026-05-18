"""Queries de comisiones de vendedores.

Replica MODIFICA.PRG PROCEDURE COMISION (línea 1770) — lista cobranzas
del mes filtradas por cliente.vend. En dBase los stats considerados
"cobrado/depositado" eran 'BWVCIK' — en Programa Core la equivalencia es
`cheque.stat IN ('B', 'A')` (B = depositado Pichincha moderno, A =
acreditado legacy). Ambos representan cheques ya cobrados en banco.

Comisión = cobranzas_mes * (pct_comision / 100). El dBase no calculaba
esto (la dueña lo hacía a mano); acá lo automatizamos.
"""
from datetime import date

import db


def lista(*, anio: int | None = None, mes: int | None = None) -> list[dict]:
    """Lista vendedores con totales del mes elegido.

    Si `anio`/`mes` no se pasan → usa el mes en curso.

    Cada fila incluye:
        codigo, nombre, pct_comision, activo,
        n_clientes, cobranzas_mes, ventas_mes, comision_mes
    """
    hoy = date.today()
    yy = int(anio) if anio else hoy.year
    mm = int(mes) if mes else hoy.month

    return db.fetch_all(
        """
        WITH
        clientes_por_vend AS (
            SELECT UPPER(TRIM(vend))    AS codigo,
                   COUNT(*)             AS n_clientes,
                   ARRAY_AGG(codigo_cli) AS codigos_cli
              FROM scintela.cliente
             WHERE vend IS NOT NULL AND TRIM(vend) <> ''
             GROUP BY UPPER(TRIM(vend))
        ),
        cobranzas_mes AS (
            -- Paridad MODIFICA.PRG L1834:
            --   FOR MONTH(FECHAD)=MES AND YEAR(FECHAD)=YYEAR AND STAT $ 'BWVCIK'
            -- En PC: stat IN (B, A) son los depositados/acreditados.
            SELECT UPPER(TRIM(c.vend))            AS codigo,
                   COALESCE(SUM(ch.importe), 0)   AS total
              FROM scintela.cheque ch
              JOIN scintela.cliente c ON c.codigo_cli = ch.codigo_cli
             WHERE EXTRACT(YEAR FROM ch.fechad)  = %(yy)s
               AND EXTRACT(MONTH FROM ch.fechad) = %(mm)s
               AND ch.stat IN ('B', 'A')
               AND c.vend IS NOT NULL AND TRIM(c.vend) <> ''
             GROUP BY UPPER(TRIM(c.vend))
        ),
        ventas_mes AS (
            -- Bonus PC: facturas emitidas del mes por vendedor (no del PRG).
            SELECT UPPER(TRIM(c.vend))            AS codigo,
                   COALESCE(SUM(f.importe), 0)    AS total
              FROM scintela.factura f
              JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
             WHERE EXTRACT(YEAR FROM f.fecha)  = %(yy)s
               AND EXTRACT(MONTH FROM f.fecha) = %(mm)s
               AND (f.stat IS NULL OR f.stat NOT IN ('X', 'Y'))
               AND c.vend IS NOT NULL AND TRIM(c.vend) <> ''
             GROUP BY UPPER(TRIM(c.vend))
        )
        SELECT v.codigo,
               v.nombre,
               v.pct_comision,
               v.activo,
               COALESCE(cv.n_clientes, 0)                                    AS n_clientes,
               COALESCE(co.total, 0)                                         AS cobranzas_mes,
               COALESCE(ve.total, 0)                                         AS ventas_mes,
               ROUND(COALESCE(co.total, 0)
                     * COALESCE(v.pct_comision, 0) / 100.0, 2)::numeric      AS comision_mes
          FROM scintela.vendedor v
          LEFT JOIN clientes_por_vend cv ON cv.codigo = v.codigo
          LEFT JOIN cobranzas_mes      co ON co.codigo = v.codigo
          LEFT JOIN ventas_mes         ve ON ve.codigo = v.codigo
         ORDER BY comision_mes DESC, cobranzas_mes DESC, v.codigo
        """,
        {"yy": yy, "mm": mm},
    ) or []


def por_codigo(codigo: str) -> dict | None:
    return db.fetch_one(
        """
        SELECT codigo, nombre, pct_comision, activo,
               fecha_crea, fecha_actualiza, usuario_actualiza
          FROM scintela.vendedor
         WHERE codigo = UPPER(TRIM(%s))
        """,
        (codigo,),
    )


def actualizar_pct(codigo: str, pct: float, usuario: str = "web") -> int:
    """Actualiza el % de comisión de un vendedor."""
    return db.execute(
        """
        UPDATE scintela.vendedor
           SET pct_comision      = %s,
               fecha_actualiza   = CURRENT_TIMESTAMP,
               usuario_actualiza = %s
         WHERE codigo = UPPER(TRIM(%s))
        """,
        (pct, usuario[:30], codigo),
    )


def actualizar_nombre(codigo: str, nombre: str, usuario: str = "web") -> int:
    return db.execute(
        """
        UPDATE scintela.vendedor
           SET nombre            = %s,
               fecha_actualiza   = CURRENT_TIMESTAMP,
               usuario_actualiza = %s
         WHERE codigo = UPPER(TRIM(%s))
        """,
        (nombre[:100], usuario[:30], codigo),
    )


def cobranzas_detalle(codigo: str, *, anio: int, mes: int) -> list[dict]:
    """Detalle de cheques cobrados del mes para un vendedor.

    Replica el LIST FECHAD, CLIENTE, IMPORTE, BANCO, STAT del PRG.
    """
    return db.fetch_all(
        """
        SELECT ch.id_cheque, ch.no_cheque, ch.fechad, ch.fecha,
               ch.importe, ch.stat,
               ch.codigo_cli,
               COALESCE(c.nombre, '')         AS cliente,
               COALESCE(b.nombre, ch.banco)   AS banco
          FROM scintela.cheque ch
          JOIN scintela.cliente c  ON c.codigo_cli = ch.codigo_cli
          LEFT JOIN scintela.banco b ON b.no_banco = ch.no_banco
         WHERE EXTRACT(YEAR FROM ch.fechad)  = %(yy)s
           AND EXTRACT(MONTH FROM ch.fechad) = %(mm)s
           AND ch.stat IN ('B', 'A')
           AND UPPER(TRIM(c.vend)) = UPPER(TRIM(%(codigo)s))
         ORDER BY ch.fechad, ch.id_cheque
        """,
        {"codigo": codigo, "yy": int(anio), "mm": int(mes)},
    ) or []


def ventas_detalle(codigo: str, *, anio: int, mes: int) -> list[dict]:
    """Detalle de facturas emitidas del mes para un vendedor."""
    return db.fetch_all(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.vencimiento,
               f.importe, f.saldo, f.stat,
               f.codigo_cli,
               COALESCE(c.nombre, '')   AS cliente
          FROM scintela.factura f
          JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
         WHERE EXTRACT(YEAR FROM f.fecha)  = %(yy)s
           AND EXTRACT(MONTH FROM f.fecha) = %(mm)s
           AND (f.stat IS NULL OR f.stat NOT IN ('X', 'Y'))
           AND UPPER(TRIM(c.vend)) = UPPER(TRIM(%(codigo)s))
         ORDER BY f.fecha, f.id_factura
        """,
        {"codigo": codigo, "yy": int(anio), "mm": int(mes)},
    ) or []


def crear(codigo: str, nombre: str, pct: float = 0, usuario: str = "web") -> dict:
    """Alta manual de vendedor (no auto-backfilleado)."""
    cod = codigo.strip().upper()[:3]
    if not cod:
        raise ValueError("Código requerido (3 letras).")
    return db.execute_returning(
        """
        INSERT INTO scintela.vendedor
            (codigo, nombre, pct_comision, fecha_crea, usuario_actualiza, fecha_actualiza)
        VALUES (%s, %s, %s, CURRENT_DATE, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (codigo) DO NOTHING
        RETURNING codigo
        """,
        (cod, nombre[:100], pct, usuario[:30]),
    ) or {"codigo": cod}
