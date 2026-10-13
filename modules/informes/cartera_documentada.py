"""Cartera DOCUMENTADA por vendedor: cuánto de lo que deben sus clientes ya
está en cheques por cobrar.

TMT 2026-10-13 — pedido de Andrés para la reunión de vendedores (*"cuánto está
documentada, es decir en facturas y cheques — los vendedores tienen la
obligación de documentar"*). Tamara: sólo el porcentaje, y de varios meses.

Los números son los MISMOS de Estados de cuenta › Vendedor (misma consulta,
misma agrupación): `saldo` = "Saldo facturas" y `cheques` = "Cheques por
cobrar" de la ficha de cada cliente. Si este % saliera de otra cuenta, la
pantalla y la foto dirían cosas distintas del mismo vendedor.

Esa pantalla sólo sabe el HOY. Por eso la foto diaria (health/all) llama a
`guardar_foto()`: una fila por vendedor y por día en
`scintela.cartera_documentada_foto`. El mes se lee con la foto del último día.
"""
from __future__ import annotations

import db

SIN_VENDEDOR = "~"


def por_vendedor(filas: list[dict]) -> list[dict]:
    """Agrupa las filas de la pantalla (con `saldo` y `cheques` de la ficha)
    por vendedor, con la MISMA clave que Estados de cuenta › Vendedor: un
    vendedor inactivo o vacío cae en '(sin vendedor)'.

    Pura (sin base): ``[{vend, vendedor, clientes, saldo, cheques, pct}]``,
    ordenada por código, con '(sin vendedor)' al final. `pct` = cheques sobre
    (saldo + cheques), en %, o None si el total es cero.
    """
    from modules.informes.views import _ec_group_key

    grupos: dict[str, dict] = {}
    for r in filas or []:
        k, nombre = _ec_group_key("vendedor", r)
        g = grupos.setdefault(k, {"vend": k, "vendedor": nombre, "clientes": 0,
                                  "saldo": 0.0, "cheques": 0.0})
        g["clientes"] += 1
        g["saldo"] += float(r.get("saldo") or 0)
        g["cheques"] += float(r.get("cheques") or 0)
    salida = []
    for g in grupos.values():
        g["saldo"] = round(g["saldo"], 2)
        g["cheques"] = round(g["cheques"], 2)
        g["pct"] = pct(g["saldo"], g["cheques"])
        salida.append(g)
    return sorted(salida, key=lambda g: (g["vend"] == SIN_VENDEDOR, g["vend"]))


def pct(saldo: float, cheques: float) -> float | None:
    total = float(saldo or 0) + float(cheques or 0)
    if total == 0:
        return None
    return round(float(cheques or 0) * 100 / total, 1)


def de_hoy() -> list[dict]:
    """Las filas de hoy, con los números de la ficha (igual que la pantalla)."""
    from modules.informes import queries

    filas = queries.estado_cuenta_clientes_saldos() or []
    return por_vendedor(queries.con_totales_de_la_ficha(filas))


def guardar_foto(fecha) -> dict:
    """Graba (o pisa) la foto del día. Idempotente: corre varias veces por día."""
    grupos = de_hoy()
    for g in grupos:
        db.execute(
            """
            INSERT INTO scintela.cartera_documentada_foto
                   (fecha, vend, vendedor, clientes, saldo, cheques)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (fecha, vend) DO UPDATE
               SET vendedor = EXCLUDED.vendedor, clientes = EXCLUDED.clientes,
                   saldo = EXCLUDED.saldo, cheques = EXCLUDED.cheques,
                   creado_en = now()
            """,
            (fecha, g["vend"], (g["vendedor"] or "")[:80], g["clientes"],
             g["saldo"], g["cheques"]),
        )
    return {"fecha": str(fecha), "vendedores": len(grupos),
            "pct_total": pct(sum(g["saldo"] for g in grupos),
                             sum(g["cheques"] for g in grupos))}


def historia_mensual() -> list[dict]:
    """La foto del ÚLTIMO día guardado de cada mes, por vendedor."""
    return db.fetch_all(
        """
        SELECT f.fecha, f.vend, f.vendedor, f.clientes, f.saldo, f.cheques
          FROM scintela.cartera_documentada_foto f
          JOIN (SELECT date_trunc('month', fecha) AS mes, MAX(fecha) AS fecha
                  FROM scintela.cartera_documentada_foto GROUP BY 1) u
            ON u.fecha = f.fecha
         ORDER BY f.fecha, f.vend
        """
    ) or []
