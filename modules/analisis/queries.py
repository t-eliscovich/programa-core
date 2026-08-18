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
        SELECT c.subcategoria, c.color,
               -- ⭐ Los tres grupos chicos van juntos. Dueña 17/08/2026: "los
               -- ultimos y mas chicos unilos todos en una categoria franela
               -- cuellos y punos". Entre los tres son 361 kg — menos del 1% —
               -- y como grupos sueltos ocupaban tres renglones del resumen
               -- para decir casi nada. Se unen ACÁ, en la lectura, y no en el
               -- refresh: así el dato crudo de Asinfo queda intacto y el día
               -- que uno crezca se separa cambiando una línea.
               CASE WHEN f.categoria IN ('Franela', 'Cuellos', 'Puños')
                    THEN 'Franela, cuellos y puños' ELSE f.categoria END AS categoria,
               c.fecha_marcado, c.kg_al_marcar,
               COALESCE(f.stock_kg, 0)    AS stock_kg,
               COALESCE(f.kg_vendidos, 0) AS kg_vendidos,
               f.ultima_venta,
               COALESCE(f.clientes, 0)    AS clientes,
               f.anio_pista,
               COALESCE(f.kg_primera, 0) AS kg_primera,
               COALESCE(f.kg_segunda, 0) AS kg_segunda,
               -- ⭐ El % se calcula EN LA QUERY, sobre el mismo conjunto de
               -- filas que se muestra. Calcularlo en el template contra un
               -- total traído aparte es cómo dos números del mismo cuadro
               -- terminan sin sumar 100.
               100 * COALESCE(f.stock_kg, 0)
                   / NULLIF(SUM(COALESCE(f.stock_kg, 0)) OVER (), 0) AS pct_total,
               SUM(COALESCE(f.stock_kg, 0))
                   OVER (PARTITION BY c.subcategoria)                 AS kg_tela,
               100 * SUM(COALESCE(f.stock_kg, 0)) OVER (PARTITION BY c.subcategoria)
                   / NULLIF(SUM(COALESCE(f.stock_kg, 0)) OVER (), 0) AS pct_tela,
               COUNT(*) OVER (PARTITION BY c.subcategoria)            AS colores_tela,
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
        "kg_segunda": sum(float(f["kg_segunda"]) for f in filas),
        "n_segunda": sum(1 for f in filas if float(f["kg_segunda"]) > 0),
    }


def por_grupo(filas: list[dict]) -> list[dict]:
    """
    El resumen de arriba: cuánto pesa cada grupo de producto.

    Se calcula sobre las MISMAS filas que se dibujan abajo, no con una consulta
    aparte. Si fueran dos consultas, el resumen y la tabla podrían no coincidir
    el día que una de las dos cambie de criterio, y no habría ningún síntoma.
    """
    tot = sum(float(f["stock_kg"]) for f in filas) or 1
    g: dict[str, dict] = {}
    for f in filas:
        # ⚠ `n_items` y NO `items`: en Jinja `g.items` resuelve el MÉTODO del
        # diccionario antes que la clave, y la tabla imprime
        # "<built-in method items of dict object at 0x…>" donde va el número.
        # Ya pasó una vez en la tarjeta de arriba; esto es la segunda. El test
        # ahora recorre TODOS los diccionarios que van a un template.
        d = g.setdefault(f["categoria"] or "(sin grupo)", {
            "grupo": f["categoria"] or "(sin grupo)",
            "n_items": 0, "kg": 0.0, "kg_segunda": 0.0, "subgrupos": set()})
        d["n_items"] += 1
        d["kg"] += float(f["stock_kg"])
        d["kg_segunda"] += float(f["kg_segunda"])
        d["subgrupos"].add(f["subcategoria"])
    for d in g.values():
        d["pct"] = 100 * d["kg"] / tot
        d["subgrupos"] = len(d["subgrupos"])
    return sorted(g.values(), key=lambda d: -d["kg"])


# ── Por CLIENTE: la hoja que se lleva el vendedor ───────────────────────────

#: Los órdenes posibles de la hoja. El default es `oportunidad` porque la hoja
#: existe para decidir POR QUIÉN EMPEZAR: si el vendedor corta a la mitad, que
#: haya cortado por lo chico. Alfabético sirve para ENCONTRAR a alguien, que es
#: otra tarea y la resuelve el buscador.
ORDENES = {
    "oportunidad": "los kilos parados de las telas que compra",
    "codigo": "alfabético por código",
    "provincia": "agrupado por provincia",
}


def por_cliente(vend: str | None = None, orden: str = "oportunidad") -> dict:
    """
    Da vuelta la pantalla: en vez de tela → clientes, cliente → telas.

    ⚠ `kg_potencial` NO es aditivo entre clientes. Una misma tela parada aparece
    en la lista de todos los que la compran, así que sumar la columna da mucho
    más que el stock real. Sirve para ORDENAR, no para prometer. La hoja lo dice.

    Los "improbables" (el último que compró esa tela lo hizo hace dos años o
    más) van aparte y al final: mezclados, ensucian una lista que el vendedor
    tiene que poder creer.
    """
    filas = db.fetch_all(
        """
        SELECT l.codigo_cli, l.nombre, l.provincia, l.vend_pc, l.subcategoria,
               l.kg AS kg_cliente, l.ultima_compra, l.anio,
               f.kg_parado, f.colores_parados
          FROM scintela.parado_llamado l
          JOIN (SELECT subcategoria, SUM(stock_kg) AS kg_parado,
                       STRING_AGG(color, ', ' ORDER BY stock_kg DESC) AS colores_parados
                  FROM scintela.parado_foto
                 WHERE stock_kg > 0
                 GROUP BY subcategoria) f
            ON f.subcategoria = l.subcategoria
         WHERE (%s IS NULL OR l.vend_pc = %s)
         ORDER BY l.codigo_cli, f.kg_parado DESC
        """,
        (vend or None, vend or None),
    )

    anio = today_ec().year
    clientes: dict[str, dict] = {}
    for f in filas:
        c = clientes.setdefault(f["codigo_cli"], {
            "codigo": f["codigo_cli"], "nombre": f["nombre"],
            "provincia": f["provincia"], "vend_pc": f["vend_pc"],
            "telas": [], "kg_potencial": 0.0, "improbable": True,
        })
        c["telas"].append(f)
        c["kg_potencial"] += float(f["kg_parado"] or 0)
        if f["anio"] >= anio - 1:
            c["improbable"] = False

    for c in clientes.values():
        c["telas"].sort(key=lambda t: -float(t["kg_parado"] or 0))

    vivos = [c for c in clientes.values() if not c["improbable"]]
    dudosos = [c for c in clientes.values() if c["improbable"]]

    llaves = {
        "oportunidad": lambda c: -c["kg_potencial"],
        "codigo": lambda c: c["codigo"],
        "provincia": lambda c: (c["provincia"] or "", -c["kg_potencial"]),
    }
    clave = llaves.get(orden, llaves["oportunidad"])
    vivos.sort(key=clave)
    dudosos.sort(key=clave)
    return {"clientes": vivos, "improbables": dudosos, "orden": orden}


def por_cliente_plano(vend: str | None = None, orden: str = "oportunidad") -> list[dict]:
    """
    La misma hoja, aplanada a una fila por CLIENTE × TELA, para Excel.

    Sale de `por_cliente()` y no de una consulta propia: si fueran dos caminos,
    el archivo y la pantalla podrían decir cosas distintas del mismo día.

    ⚠ `kg_parado` se repite en cada fila del mismo cliente y entre clientes:
    sumar esa columna en Excel da mucho más que el stock real. Va una columna
    `orden_en_la_hoja` para poder reconstruir el ranking sin sumar nada.
    """
    filas = []
    r = por_cliente(vend, orden)
    for grupo, dudoso in (("candidato", False), ("improbable", True)):
        for i, c in enumerate(r["improbables"] if dudoso else r["clientes"], 1):
            for t in c["telas"]:
                filas.append({
                    "tipo": grupo,
                    "orden_en_la_hoja": i,
                    "codigo": c["codigo"], "nombre": c["nombre"],
                    "provincia": c["provincia"],
                    "vendedor": c["vend_pc"] or "mostrador",
                    "kg_potencial": c["kg_potencial"],
                    "subcategoria": t["subcategoria"],
                    "colores_parados": t["colores_parados"],
                    "kg_parado": t["kg_parado"],
                    "kg_cliente": t["kg_cliente"],
                    "ultima_compra": t["ultima_compra"],
                    "anio": t["anio"],
                })
    return filas


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
                vendido[k] += float(v["kg"] or 0)   # ya viene abierto por vendedor

        # 3 · la foto se rehace entera
        stock = {(p["subcategoria"], p["color"]): p for p in par}
        db.execute("DELETE FROM scintela.parado_foto", conn=conn)
        for k in marcado:
            p = stock.get(k)
            db.execute(
                """INSERT INTO scintela.parado_foto
                       (subcategoria, color, stock_kg, kg_vendidos, ultima_venta,
                        clientes, anio_pista, kg_primera, kg_segunda, categoria)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (k[0], k[1], (p or {}).get("stock_kg") or 0, vendido.get(k, 0),
                 (p or {}).get("ultima_venta"), total_cli.get(k[0], 0),
                 anio_de.get(k[0]), (p or {}).get("kg_primera") or 0,
                 (p or {}).get("kg_segunda") or 0, (p or {}).get("categoria")),
                conn=conn)

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

        # ── la competencia ──
        db.execute("DELETE FROM scintela.parado_venta", conn=conn)
        for v in ventas:
            k = (v["subcategoria"], v["color"])
            f = marcado.get(k)
            if f and _fecha(v["fecha"]) >= f:
                db.execute(
                    """INSERT INTO scintela.parado_venta
                           (subcategoria, color, vend_pc, vendedor, fecha, kg)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (k[0], k[1], v.get("vend_pc"), v.get("vendedor") or "Intela",
                     _fecha(v["fecha"]), v.get("kg") or 0), conn=conn)

        db.execute("DELETE FROM scintela.parado_share", conn=conn)
        for s in asinfo_parado.share_por_grupo():
            db.execute(
                """INSERT INTO scintela.parado_share
                       (categoria, vend_pc, vendedor, kg, pct)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (categoria, vendedor) DO NOTHING""",
                (s["categoria"], s.get("vend_pc"), s["vendedor"],
                 s.get("kg") or 0, round(s.get("pct") or 0, 2)), conn=conn)

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


# ── La COMPETENCIA ──────────────────────────────────────────────────────────

#: Los que compiten. Los seis vendedores de `scintela.vendedor` más Intela —el
#: mostrador—, que compite como uno más por decisión de la dueña ("hay una
#: vendedora dedicada"). ⚠ Intela es el 51,3% de las ventas de estas telas, así
#: que va a estar arriba: es una decisión tomada con el dato a la vista.
#: Cualquier otro vendedor que aparezca en una venta suma al grupo pero no al
#: ranking — son bajas históricas con kilos residuales.
COMPETIDORES = ["Intela", "Proaño Patricio", "Lopez Felipe", "Miranda Roberto",
                "Quintero Jose", "Ramirez Edgar", "Proaño Sebastián"]


def _meta_pct(grupos: list[dict], override: dict[str, float]) -> None:
    """
    La meta de cada grupo, en %.

    ⭐ Sale del PESO del grupo en el stock parado. Dueña: "meta segun cantidad
    de ese grupo % en el total del stock". O sea: Jersey es el 31,5% de lo
    parado, así que la meta de Jersey es sacar el 31,5% de sus propios kilos.
    Los grupos gordos tienen que despejar proporcionalmente más, que es donde
    está el problema.

    `override` (tabla `parado_meta`) pisa el automático cuando hace falta.
    """
    for g in grupos:
        g["meta_pct"] = float(override.get(g["grupo"], g["pct"]))
        g["meta_es_manual"] = g["grupo"] in override
        g["meta_kg"] = g["kg"] * g["meta_pct"] / 100


def competencia() -> dict:
    """
    El tablero de la competencia: quién va ganando y cuánto falta.

    Todo se calcula sobre las MISMAS filas de `items()` que muestra la pantalla
    de Lo parado. Si saliera de otra consulta, el termómetro de acá y el total
    de allá podrían no coincidir el mismo día.
    """
    filas = items()
    grupos = por_grupo(filas)
    override = {r["categoria"]: float(r["pct"]) for r in db.fetch_all(
        "SELECT categoria, pct FROM scintela.parado_meta")}
    _meta_pct(grupos, override)

    vendido = db.fetch_all(
        """SELECT v.vendedor, f.categoria, SUM(v.kg) AS kg,
                  MAX(v.fecha) AS ultima
             FROM scintela.parado_venta v
             JOIN scintela.parado_foto f
               ON f.subcategoria = v.subcategoria AND f.color = v.color
            GROUP BY v.vendedor, f.categoria""")
    share = db.fetch_all("SELECT * FROM scintela.parado_share")

    # ⭐ La meta del grupo se parte en PARTES IGUALES entre los competidores.
    # Repartirla por lo que cada uno ya vende premiaría quedarse en lo de
    # siempre, que es lo contrario de lo que busca la competencia.
    n = len(COMPETIDORES) or 1
    por_grupo_meta = {g["grupo"]: g["meta_kg"] / n for g in grupos}

    tabla = {v: {"vendedor": v, "meta": 0.0, "kg": 0.0, "ultima": None,
                 "grupos": {}} for v in COMPETIDORES}
    for g in grupos:
        for v in COMPETIDORES:
            tabla[v]["meta"] += por_grupo_meta[g["grupo"]]
            tabla[v]["grupos"][g["grupo"]] = {
                "meta": por_grupo_meta[g["grupo"]], "kg": 0.0}

    fuera = 0.0
    for r in vendido:
        v, cat, kg = r["vendedor"], r["categoria"], float(r["kg"] or 0)
        if v not in tabla:
            fuera += kg          # bajas históricas: suman al grupo, no al ranking
            continue
        tabla[v]["kg"] += kg
        if cat in tabla[v]["grupos"]:
            tabla[v]["grupos"][cat]["kg"] += kg
        if r["ultima"] and (not tabla[v]["ultima"] or r["ultima"] > tabla[v]["ultima"]):
            tabla[v]["ultima"] = r["ultima"]

    ranking = sorted(tabla.values(),
                     key=lambda d: -(d["kg"] / d["meta"] if d["meta"] else 0))
    for i, d in enumerate(ranking, 1):
        d["puesto"] = i
        d["pct"] = 100 * d["kg"] / d["meta"] if d["meta"] else 0

    liquidado = {}
    for r in vendido:
        liquidado[r["categoria"]] = liquidado.get(r["categoria"], 0) + float(r["kg"] or 0)
    for g in grupos:
        g["liquidado"] = liquidado.get(g["grupo"], 0)
        g["pct_meta"] = 100 * g["liquidado"] / g["meta_kg"] if g["meta_kg"] else 0
        g["share"] = sorted(
            [s for s in share if s["categoria"] == g["grupo"]],
            key=lambda s: -float(s["pct"] or 0))[:4]

    return {
        "hoy": today_ec(),
        "grupos": grupos,
        "ranking": ranking,
        "kg_parado": sum(g["kg"] for g in grupos),
        "meta_kg": sum(g["meta_kg"] for g in grupos),
        "liquidado": sum(g["liquidado"] for g in grupos),
        "kg_fuera_del_ranking": fuera,
        "sin_tocar": [g for g in grupos if not g["liquidado"]],
    }
