"""Lecturas y refresh de la pantalla LO PARADO.

La pantalla lee SÓLO de Postgres (abre instantánea). El refresh es el que va a
Asinfo, y es explícito: un botón.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import db
from filters import today_ec

from . import asinfo_parado

# ── Lectura ─────────────────────────────────────────────────────────────────

def estado() -> dict:
    """Cuándo se actualizó por última vez y cómo le fue."""
    return db.fetch_one("SELECT * FROM scintela.parado_refresh WHERE id = 1") or {}


def items() -> list[dict]:
    """
    La cohorte entera, con la foto de hoy pegada al lado.

    ⭐ LEFT JOIN contra `parado_foto` a propósito: un ítem que se vendió entero
    deja de estar en la foto y aun así tiene que seguir en la lista. Ése es el
    pedido: "si empezamos a venderlas, que no se nos vayan de la lista".
    """
    return db.fetch_all(
        """
        SELECT c.subcategoria, c.color, c.fecha_marcado, c.kg_al_marcar,
               COALESCE(f.stock_kg, 0)    AS stock_kg,
               COALESCE(f.kg_vendidos, 0) AS kg_vendidos,
               f.ultima_venta,
               COALESCE(f.clientes, 0)    AS clientes,
               f.anio_pista,
               CASE WHEN COALESCE(f.kg_vendidos, 0) > 0
                     AND COALESCE(f.stock_kg, 0) < 20      THEN 'resuelto'
                    WHEN COALESCE(f.kg_vendidos, 0) > 0    THEN 'empezó a moverse'
                    ELSE 'sigue parado' END                AS estado
          FROM scintela.parado_cohorte c
          LEFT JOIN scintela.parado_foto f
                 ON f.subcategoria = c.subcategoria AND f.color = c.color
         ORDER BY COALESCE(f.stock_kg, 0) DESC, c.subcategoria, c.color
        """
    )


def llamados_por_tela() -> dict[str, list[dict]]:
    """Los candidatos, agrupados por TELA (no por tela × color: el color no
    entra en la llamada)."""
    out: dict[str, list[dict]] = defaultdict(list)
    for f in db.fetch_all(
        "SELECT * FROM scintela.parado_llamado ORDER BY subcategoria, kg DESC"
    ):
        out[f["subcategoria"]].append(f)
    return dict(out)


def resumen(filas: list[dict]) -> dict:
    """Los números de las tarjetas. Se calculan sobre las filas ya leídas para
    que la tarjeta y la tabla no puedan decir cosas distintas."""
    # ⚠ La clave NO se puede llamar `items`: en Jinja `resumen.items` resuelve
    # primero el MÉTODO del diccionario, así que la tarjeta imprimía
    # "<built-in method items of dict object at 0x…>" en vez del número. No da
    # error — renderiza 200 y queda un texto absurdo donde va una cifra.
    return {
        "n_items": len(filas),
        "kg": sum(float(f["stock_kg"]) for f in filas),
        "kg_vendidos": sum(float(f["kg_vendidos"]) for f in filas),
        "movidos": sum(1 for f in filas if float(f["kg_vendidos"]) > 0),
        "sin_pista": sum(1 for f in filas if not f["clientes"]),
        "kg_sin_pista": sum(float(f["stock_kg"]) for f in filas if not f["clientes"]),
    }


# ── Refresh ─────────────────────────────────────────────────────────────────

def actualizar() -> dict:
    """
    Trae todo de Asinfo y deja la caché al día. Devuelve un resumen.

    Orden deliberado: primero se traen las TRES consultas y recién después se
    escribe. Si Metabase falla a mitad, no se tocó ni una fila — una cohorte
    escrita a medias es peor que una vieja, porque no se nota.
    """
    hoy = today_ec()
    par = asinfo_parado.parados()
    lla = asinfo_parado.llamados()

    desde = db.fetch_one(
        "SELECT MIN(fecha_marcado) AS f FROM scintela.parado_cohorte") or {}
    desde_f: date = desde.get("f") or hoy
    ventas = asinfo_parado.vendido_desde(desde_f.isoformat())

    # clientes por tela y de qué año salieron
    total_cli, anio_de = {}, {}
    for f in lla:
        total_cli[f["subcategoria"]] = f.get("clientes_total") or 0
        anio_de[f["subcategoria"]] = f.get("anio")

    with db.tx() as conn:
        # 1 · la cohorte SÓLO crece
        for p in par:
            db.execute(
                """INSERT INTO scintela.parado_cohorte
                       (subcategoria, color, fecha_marcado, kg_al_marcar)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (subcategoria, color) DO NOTHING""",
                (p["subcategoria"], p["color"], hoy, p["stock_kg"]), conn=conn)

        cohorte = db.fetch_all(
            "SELECT subcategoria, color, fecha_marcado FROM scintela.parado_cohorte",
            conn=conn)

        # 2 · cuánto se vendió de cada uno DESDE SU PROPIA fecha de marcado
        vendido: dict[tuple[str, str], float] = defaultdict(float)
        marcado = {(c["subcategoria"], c["color"]): c["fecha_marcado"] for c in cohorte}
        for v in ventas:
            k = (v["subcategoria"], v["color"])
            f = marcado.get(k)
            if f and _fecha(v["fecha"]) >= f:
                vendido[k] += float(v["kg"] or 0)

        # 3 · la foto se rehace entera
        stock = {(p["subcategoria"], p["color"]): p for p in par}
        db.execute("DELETE FROM scintela.parado_foto", conn=conn)
        for k in marcado:
            p = stock.get(k)
            db.execute(
                """INSERT INTO scintela.parado_foto
                       (subcategoria, color, stock_kg, kg_vendidos, ultima_venta,
                        clientes, anio_pista)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (k[0], k[1], (p or {}).get("stock_kg") or 0, vendido.get(k, 0),
                 (p or {}).get("ultima_venta"), total_cli.get(k[0], 0),
                 anio_de.get(k[0])), conn=conn)

        # 4 · los llamados también
        db.execute("DELETE FROM scintela.parado_llamado", conn=conn)
        for f in lla:
            db.execute(
                """INSERT INTO scintela.parado_llamado
                       (subcategoria, codigo_cli, nombre, provincia, vendedor,
                        vend_pc, kg, ultima_compra, colores, anio)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (subcategoria, codigo_cli) DO NOTHING""",
                (f["subcategoria"], f["codigo"], f.get("nombre"), f.get("provincia"),
                 (f.get("vendedor") or "").strip(), f.get("vend_pc"), f.get("kg") or 0,
                 f.get("ultima_compra"), f.get("colores") or 0, f["anio"]), conn=conn)

        db.execute(
            """UPDATE scintela.parado_refresh
                  SET actualizado = NOW(), items = %s, llamados = %s,
                      ok = TRUE, detalle = %s
                WHERE id = 1""",
            (len(marcado), len(lla),
             f"{len(par)} parados hoy · {len(lla)} candidatos"), conn=conn)

    return {"items": len(marcado), "llamados": len(lla), "parados_hoy": len(par)}


def _fecha(v):
    """Metabase devuelve las fechas como texto ISO; Postgres, como `date`."""
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])
