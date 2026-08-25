"""Lecturas y refresh de la pantalla LO PARADO.

La pantalla lee SÓLO de Postgres (abre instantánea). El refresh es el que va a
Asinfo, y es explícito: un botón.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

import db
from filters import today_ec
from modules.mi_cartera.queries import _ES_MI_CLIENTE as _mc_es_mi_cliente

from . import asinfo_parado

_LOG = logging.getLogger("programa_core.analisis")

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

    Cada fila trae también `color_nombre` — vacío si el color no está en el
    catálogo (ver `nombres_de_color`).
    """
    return db.fetch_all(
        """
        SELECT c.subcategoria, c.color,
               -- ⭐ El NOMBRE del color al lado del código (dueña 24/08/2026:
               -- *"pero quiero codigo y color"*). El código de 3 letras es el
               -- del producto en Asinfo; el nombre es el que usa el cliente por
               -- teléfono. Van los dos.
               --
               -- La única lista de nombres que hay es `tinto_costos` (la carga
               -- el sync de formulas_app, que le anexa " · Categoria"). Cubre
               -- 210 de los 222 códigos en saldo: el que no está sale con el
               -- código solo, nunca con un nombre inventado.
               --
               -- ⚠ Va en ESTA consulta y no en una segunda: dos lecturas
               -- pueden contestar cosas distintas del mismo momento.
               COALESCE(UPPER(LEFT(nom.n, 1)) || LOWER(SUBSTRING(nom.n FROM 2)), '')
                                                             AS color_nombre,
               -- ⭐ Los tres grupos chicos van juntos, con la sigla FCP (dueña
               -- 18/08/2026: "franela cuellos y punos poneles FCP o algo asi").
               -- El nombre entero ocupaba tres renglones en la columna Grupo y
               -- desalineaba toda la fila. Entre los tres son 773 kg —el 1,5%—
               -- y como grupos sueltos ocupaban tres renglones del resumen
               -- para decir casi nada. Se unen ACÁ, en la lectura, y no en el
               -- refresh: así el dato crudo de Asinfo queda intacto y el día
               -- que uno crezca se separa cambiando una línea.
               CASE WHEN f.categoria IN ('Franela', 'Cuellos', 'Puños')
                    THEN 'FCP' ELSE f.categoria END AS categoria,
               c.fecha_marcado, c.kg_al_marcar,
               COALESCE(f.stock_kg, 0)    AS stock_kg,
               COALESCE(f.kg_vendidos, 0) AS kg_vendidos,
               f.ultima_venta,
               COALESCE(f.clientes, 0)    AS clientes,
               f.anio_pista,
               COALESCE(f.kg_primera, 0) AS kg_primera,
               COALESCE(f.kg_segunda, 0) AS kg_segunda,
               COALESCE(f.kg_tubular, 0) AS kg_tubular,
               COALESCE(f.kg_abierta, 0) AS kg_abierta,
               -- ⭐ La forma, ya resuelta acá: TUB, ABI, las dos, o vacío
               -- cuando el lote no lo dice. La pantalla y la hoja impresa
               -- muestran lo mismo sin repetir el `if` en dos plantillas.
               CASE WHEN COALESCE(f.kg_tubular, 0) > 0
                     AND COALESCE(f.kg_abierta, 0) > 0 THEN 'TUB ABI'
                    WHEN COALESCE(f.kg_tubular, 0) > 0 THEN 'TUB'
                    WHEN COALESCE(f.kg_abierta, 0) > 0 THEN 'ABI'
                    ELSE '' END                        AS forma,
               f.motivo,
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
          -- ⚠ LATERAL con LIMIT 1: el catálogo tiene el mismo código repetido
          -- (una fila por clase de color) y sin el tope la fila se duplicaría.
          LEFT JOIN LATERAL (
              SELECT SPLIT_PART(tc.color, ' · ', 1) AS n
                FROM scintela.tinto_costos tc
               WHERE UPPER(TRIM(tc.cod)) = UPPER(TRIM(c.color))
                 AND COALESCE(TRIM(tc.color), '') <> ''
               LIMIT 1) nom ON TRUE
         ORDER BY COALESCE(f.stock_kg, 0) DESC, c.subcategoria, c.color
        """
    )


def con_puntos(filas: list[dict]) -> list[dict]:
    """Le pega a cada fila lo que vale su tela, y ordena por eso.

    ⭐ Ordenada por PUNTOS y no por kilos (dueña 24/08/2026, "idem para la
    pantalla de saldos"): la pregunta que se hace el que abre esta lista es a
    qué tela conviene ir, y 300 kg de una tela de 10 puntos valen más que 3.000
    de una de 1. El orden por kilos ponía arriba justo lo que sale solo.

    ⚠ Una tela sin puntaje vale 1, no 0: un kilo vendido nunca puede contar
    cero. Sólo pasa si la cohorte creció después de congelar los puntos.
    """
    puntos = puntos_por_tela()
    for f in filas:
        p = puntos.get(f["subcategoria"], {})
        f["puntos"] = int(p.get("puntos", 1))
        f["nivel"] = p.get("nivel_nombre", "")
        f["puntos_fila"] = float(f["stock_kg"] or 0) * f["puntos"]
    return sorted(filas, key=lambda f: -f["puntos_fila"])


def llamados_por_tela(cartera_de: str | None = None) -> dict[str, list[dict]]:
    """
    Los candidatos, agrupados por TELA (no por tela × color: el color no entra
    en la llamada).

    `cartera_de` deja SÓLO los clientes de ese vendedor, según Programa Core.
    Es lo que hace que la misma pantalla sirva para la oficina y para el
    vendedor sin ser dos pantallas.
    """
    if cartera_de:
        filas = db.fetch_all(
            f"""SELECT l.* FROM scintela.parado_llamado l
                  JOIN scintela.cliente c
                    ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(l.codigo_cli))
                 WHERE {_ES_MI_CLIENTE}
                 ORDER BY l.subcategoria, l.kg DESC""",
            {"cartera": cartera_de})
    else:
        filas = db.fetch_all(
            "SELECT * FROM scintela.parado_llamado ORDER BY subcategoria, kg DESC")
    out: dict[str, list[dict]] = defaultdict(list)
    for f in filas:
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
        "kg_segunda": sum(float(f["kg_segunda"]) for f in filas),
        "n_segunda": sum(1 for f in filas if float(f["kg_segunda"]) > 0),
        "puntos": sum(float(f.get("puntos_fila") or 0) for f in filas),
    }


def _fijar_base(conn=None) -> dict[str, float] | None:
    """
    Los kilos por grupo del día de la largada. Se escriben una sola vez.

    ⚠ Se calcula con `stock_kg + kg_vendidos`, no con el stock pelado: si el
    25 nadie aprieta «Actualizar» y el primero en apretarlo es el 27, los
    kilos que se vendieron esos dos días ya no están en bodega y la meta
    saldría más chica de lo que había el día de la largada.
    """
    largada = date.fromisoformat(config("largada", "2026-08-25"))
    if today_ec() < largada:
        return None
    ya = db.fetch_all("SELECT categoria FROM scintela.parado_base", conn=conn)
    if ya:
        return None
    base: dict[str, float] = defaultdict(float)
    for f in items():
        if f["categoria"]:
            base[f["categoria"]] += (float(f["stock_kg"] or 0)
                                     + float(f["kg_vendidos"] or 0))
    for categoria, kg in base.items():
        db.execute(
            """INSERT INTO scintela.parado_base (categoria, kg, fijada_el)
               VALUES (%s, %s, %s)
               ON CONFLICT (categoria) DO NOTHING""",
            (categoria, round(kg, 2), today_ec()), conn=conn)
    return dict(base)


def base_fijada() -> tuple[dict[str, float], date | None]:
    """Los kilos congelados por grupo y el día en que se fijaron."""
    filas = db.fetch_all(
        "SELECT categoria, kg, fijada_el FROM scintela.parado_base")
    if not filas:
        return {}, None
    return ({f["categoria"]: float(f["kg"]) for f in filas},
            min(f["fijada_el"] for f in filas))


# ── LOS PUNTOS: cuánto vale un kilo de cada tela ────────────────────────────

#: Puntos por kilo según el NIVEL de la tela. Decidido por la dueña el
#: 24/08/2026 sobre la medición de las 98 telas de la lista.
#:
#: ⭐ El nivel es por TELA y no por grupo. Los 8 grupos venden mucho más por mes
#: de lo que tienen parado —entre 0,1 y 0,9 meses— así que a nivel grupo no hay
#: ninguna señal: Poliester y Fleece dan casi igual. A nivel tela el abanico va
#: de 0,0 meses (Fleece 102 vende 54 t por mes y tiene 1.163 kg parados) a telas
#: que no vendieron un kilo en todo el año.
#:
#: ⭐ Tampoco por tela × color, y es a propósito: serían 732 números que ningún
#: vendedor puede recordar, y un color raro de una tela que sale bien quedaría
#: marcado como imposible. Dueña: "no está bien con fleece aunque sea color
#: raro, si no muy complicado".
#:
#: ⚠ El 1/4/10 no es una escala cualquiera: paga MÁS de lo que cuesta. Las
#: telas fáciles tienen 181 clientes que ya las compraron este año y las
#: difíciles 5. Con 1/2/3 al vendedor le seguía conviniendo lo fácil y de los
#: 18.496 kg clavados salían 356. El canje es real: cuanto más se paga lo
#: difícil, menos kilos totales salen y más de los que están de verdad clavados.
PUNTOS = {1: 1, 2: 4, 3: 10}

#: Cómo se llama cada nivel en la pantalla.
NIVELES = {1: "Fácil", 2: "Medio", 3: "Difícil"}

#: Los cortes, en MESES DE VENTA PARADOS = kilos en saldo ÷ kilos que la fábrica
#: vende de esa tela por mes.
MESES_FACIL = 1
MESES_MEDIO = 12


def _nivel(kg_base: float, kg_12m: float) -> tuple[int, float | None]:
    """El nivel de una tela y cuántos meses de venta tiene parados.

    ⚠ Sin venta en 12 meses NO es "cero meses parados", es el peor caso: no hay
    con qué dividir. Va derecho al nivel difícil y `meses` queda en None para
    que la pantalla diga "no se vendió" en vez de un número inventado.
    """
    if kg_12m < 1:
        return 3, None
    meses = kg_base / (kg_12m / 12)
    # ⭐ Se compara REDONDEADO a un decimal. Dueña 24/08/2026: *"11.98 es igual
    # que 12"*. Jersey Forro Spun daba 11,98 meses y caía en 4 puntos por dos
    # centésimas: sus 2.448 kg —el ítem más grande de la lista— valían 4 en vez
    # de 10 por una diferencia del 0,2%, que es menos que el error de medición
    # de la bodega. Ninguna otra tela cambia de nivel con esto.
    corte = round(meses, 1)
    if corte < MESES_FACIL:
        return 1, meses
    if corte < MESES_MEDIO:
        return 2, meses
    return 3, meses


def _fijar_puntos(conn=None) -> None:
    """
    Escribe los puntos de cada tela. Congelados el día de la largada.

    ⭐ Antes de la largada se reescriben en cada refresco: la pantalla es una
    previsualización. Desde la largada se escriben UNA vez y no se tocan más.
    Si el nivel se recalculara solo, un vendedor que saca 500 kg de una tela le
    baja los meses parados a esa tela, la tela cae de nivel, y él mismo se
    recorta los puntos a mitad de camino.

    ⚠ `kg_base` es `stock + ya vendido`, igual que la meta en kilos: si nadie
    entra el día de la largada y el primer refresco es dos días después, los
    kilos que salieron en el medio ya no están en bodega y el puntaje saldría
    calculado sobre menos tela de la que había.
    """
    fila = db.fetch_one(
        "SELECT MIN(fijado_el) AS f FROM scintela.parado_punto", conn=conn)
    if fila and fila["f"]:
        # ⭐ CONGELADOS DESDE LA PRIMERA ESCRITURA (dueña 24/08/2026: "hacelo
        # ahora que no va a cambiar para mañana"). Antes se reescribían en cada
        # refresco hasta el día de la largada, y eso los movía: la ventana de
        # 12 meses de Asinfo corre todos los días, y Jersey Forro Spun estaba
        # clavada en 12,0 meses —justo en la línea entre 4 y 10 puntos—, así
        # que sola movía la bolsa un 6%. Con la presentación ya impresa, el
        # puntaje no puede seguir cambiando abajo.
        return
    venta12 = asinfo_parado.venta_por_tela()
    if not venta12:
        # fail-CLOSED: sin el dato de ventas TODAS las telas darían "difícil"
        # y la bolsa de puntos se triplicaría en silencio.
        return
    agg: dict[str, dict] = {}
    for f in items():
        d = agg.setdefault(f["subcategoria"], {"cat": None, "kg": 0.0})
        d["kg"] += float(f["stock_kg"] or 0) + float(f["kg_vendidos"] or 0)
        if f["categoria"]:
            d["cat"] = f["categoria"]
    db.execute("DELETE FROM scintela.parado_punto", conn=conn)
    hoy = today_ec()
    for sub, d in agg.items():
        v = venta12.get(sub) or {}
        k12 = float(v.get("kg") or 0)
        nivel, meses = _nivel(d["kg"], k12)
        db.execute(
            """INSERT INTO scintela.parado_punto
                   (subcategoria, categoria, kg_base, kg_12m, kg_seg_12m, meses,
                    nivel, puntos, fijado_el)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (sub, d["cat"], round(d["kg"], 2), round(k12, 2),
             round(float(v.get("seg") or 0), 2),
             round(meses, 2) if meses is not None else None,
             nivel, PUNTOS[nivel], hoy), conn=conn)


def _puntos_provisorios() -> dict[str, dict]:
    """Todas las telas a 1 punto: la competencia de kilos de siempre.

    ⚠ Es la red, no el plan. Se usa sólo si la tabla está vacía Y tampoco se
    pudo llenar (Asinfo caído justo en ese momento). Con la tabla vacía la meta
    daría 0 puntos y la pantalla mostraría a los siete al 0% para siempre; con
    un 500 no mostraría nada. Un punto por kilo es una degradación que se
    entiende sola y que se corrige sin tocar nada apenas Asinfo conteste.
    """
    agg: dict[str, dict] = {}
    for f in items():
        d = agg.setdefault(f["subcategoria"], {
            "categoria": f["categoria"], "kg_base": 0.0, "kg_12m": 0.0,
            "meses": None, "nivel": 1, "nivel_nombre": NIVELES[1], "puntos": 1})
        d["kg_base"] += float(f["stock_kg"] or 0) + float(f["kg_vendidos"] or 0)
        if f["categoria"]:
            d["categoria"] = f["categoria"]
    return agg


def abrir_por_forma(filas: list[dict]) -> list[dict]:
    """Una fila por tela × color × FORMA, para la hoja que se sale a vender.

    ⭐ Dueña 25/08/2026: *"no, una cantidad por tubular. una cantidad por
    abierta… dos lineas para el color cuando hay ambas telas"*. Tubular y
    abierta no son la misma tela: se cortan distinto y el cliente pide una o la
    otra. Un renglón que dice 200 kg cuando son 90 tubulares y 110 abiertas
    promete algo que puede no estar.

    ⚠ Los kilos por forma vienen del LOTE y el total de la fila de otra tabla
    (`saldo_producto`); las dos cierran al 0,006% pero no son la misma
    consulta. Si sobra o falta algo, va en una tercera línea SIN forma en vez
    de repartirse a ojo: un kilo inventado en la columna de la izquierda es
    peor que un renglón que dice "no sé de qué forma es".
    """
    salida: list[dict] = []
    for f in filas:
        tub = float(f.get("kg_tubular") or 0)
        abi = float(f.get("kg_abierta") or 0)
        total = float(f.get("stock_kg") or 0)
        if not tub or not abi:
            salida.append(f)
            continue
        for forma, kg in (("TUB", tub), ("ABI", abi)):
            if kg <= 0:
                continue
            g = dict(f)
            g["stock_kg"] = kg
            g["forma_fila"] = forma
            g["kg_tubular"] = kg if forma == "TUB" else 0
            g["kg_abierta"] = kg if forma == "ABI" else 0
            g["puntos_fila"] = kg * float(f.get("puntos") or 1)
            salida.append(g)
        resto = round(total - tub - abi, 2)
        if abs(resto) >= 1:
            g = dict(f)
            g["stock_kg"] = resto
            g["forma_fila"] = ""
            g["kg_tubular"] = 0
            g["kg_abierta"] = 0
            g["puntos_fila"] = resto * float(f.get("puntos") or 1)
            salida.append(g)
    return salida


def bolsa_congelada(puntos: dict[str, dict] | None = None) -> float:
    """Los puntos que hay EN JUEGO en toda la competencia. Uno solo, y fijo.

    ⭐ Dueña 25/08/2026: *"en juego es distinto que la presentacion, puntos"*.
    Había DOS números con el mismo nombre:

      · el de la Competencia — `kg_base × puntos`, con los kilos CONGELADOS el
        día que se fijó el puntaje. Es contra el que se corre la carrera.
      · el de Saldos — los kilos de la foto de HOY por sus puntos. Ése se mueve
        todos los días con la bodega, así que nunca vuelve a coincidir con un
        número impreso.

    Las dos pantallas usan ésta y muestran el mismo. Lo que queda por vender ya
    se ve en los kilos y en la columna Puntos de la tabla; para eso no hace
    falta un segundo total que compita con el primero.
    """
    puntos = puntos if puntos is not None else puntos_por_tela()
    return sum(float(p.get("kg_base") or 0) * float(p.get("puntos") or 0)
               for p in puntos.values())


def puntos_por_tela() -> dict[str, dict]:
    """Los puntos congelados, por tela.

    ⚠ Si la tabla está vacía (recién deployado, sin ningún refresco todavía) se
    llena acá mismo: sin esto la pantalla mostraría meta 0 hasta que corriera el
    refresco automático, que puede tardar tres horas.

    ⚠ Y si tampoco se puede llenar —Asinfo caído— la pantalla NO se cae: se
    degrada a un punto por kilo. Una pantalla que devuelve 500 el día de la
    largada es peor que una que muestra la competencia vieja.
    """
    filas = db.fetch_all("SELECT * FROM scintela.parado_punto")
    if not filas:
        try:
            _fijar_puntos()
        except Exception:
            _LOG.exception("no se pudieron fijar los puntos de las telas")
        filas = db.fetch_all("SELECT * FROM scintela.parado_punto")
    if not filas:
        return _puntos_provisorios()
    return {f["subcategoria"]: {
        "categoria": f["categoria"],
        "kg_base": float(f["kg_base"] or 0),
        "kg_12m": float(f["kg_12m"] or 0),
        "meses": float(f["meses"]) if f["meses"] is not None else None,
        "nivel": int(f["nivel"]),
        "nivel_nombre": NIVELES.get(int(f["nivel"]), ""),
        "puntos": int(f["puntos"]),
    } for f in filas}


def por_grupo(filas: list[dict]) -> list[dict]:
    """
    El resumen de arriba: cuánto pesa cada grupo de producto.

    Se calcula sobre las MISMAS filas que se dibujan abajo, no con una consulta
    aparte. Si fueran dos consultas, el resumen y la tabla podrían no coincidir
    el día que una de las dos cambie de criterio, y no habría ningún síntoma.
    """
    # ⚠ Fuera las filas sin stock Y sin grupo: son restos que ya no existen y
    # sumaban un renglón "(sin grupo) · 0 kg". Una fila con 0 kg y CON grupo sí
    # entra: es un ítem que se vendió entero y tiene que seguir a la vista.
    filas = [f for f in filas
             if float(f["stock_kg"]) > 0 or f.get("categoria")]
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
            "n_items": 0, "kg": 0.0, "kg_segunda": 0.0, "puntos": 0.0,
            "subgrupos": set()})
        d["n_items"] += 1
        d["kg"] += float(f["stock_kg"])
        d["kg_segunda"] += float(f["kg_segunda"])
        d["puntos"] += float(f.get("puntos_fila") or 0)
        d["subgrupos"].add(f["subcategoria"])
    for d in g.values():
        d["pct"] = 100 * d["kg"] / tot
        d["subgrupos"] = len(d["subgrupos"])
    return sorted(g.values(), key=lambda d: -d["kg"])


# ── Por CLIENTE: la hoja que se lleva el vendedor ───────────────────────────

#: Los órdenes posibles de la hoja. El default es ALFABÉTICO POR CÓDIGO (dueña
#: 18/08/2026: "cuando es 'a quien ofrecerle que' ordena alfabeticamente").
#:
#: ⭐ Por código y no por nombre, aunque la ficha muestre el nombre grande: es
#: el mismo orden con el que sale la hoja de estado de cuenta de la oficina y
#: la de /mi-cartera, y el vendedor tiene el papel y el celular delante a la
#: vez. Dos alfabéticos distintos para el mismo cliente son peor que uno solo.
#: Ordenar por oportunidad sigue estando en el desplegable.
ORDENES = {
    "oportunidad": "kilos para ofrecerle",
    "codigo": "código",
    "provincia": "provincia",
}


def por_cliente(vend: str | None = None, orden: str = "codigo",
                cartera_de: str | None = None) -> dict:
    """
    Da vuelta la pantalla: en vez de tela → clientes, cliente → telas.

    ⭐ `vend='INTELA'` trae lo que NO es de ningún vendedor: el mostrador y las
    facturas sin vendedor asignado. Es el 51,3% de las ventas de estas telas y
    hasta ahora no se podía mirar por separado (dueña 18/08/2026: "me haces uno
    para intela? osea mostrador y todo lo que no sea vendedores"). Se resuelve
    con `vend_pc IS NULL` y no inventándole un código: Intela no está en
    `scintela.vendedor` y no debería estarlo, porque no es una persona.

    `cartera_de` acota a la cartera de un vendedor SEGÚN PROGRAMA CORE
    (`cliente.vend`), que es lo que tiene que usar la hoja del propio vendedor.
    `vend` acota por el vendedor de la última factura de ASINFO, que es lo que
    usa la oficina para repartir. ⚠ No son lo mismo y por eso son dos
    parámetros: si la hoja del vendedor usara el de Asinfo, un cliente podría
    salirle a él acá y a otro en /mi-cartera.

    ⚠ `kg_potencial` NO es aditivo entre clientes. Una misma tela parada aparece
    en la lista de todos los que la compran, así que sumar la columna da mucho
    más que el stock real. Sirve para ORDENAR, no para prometer. La hoja lo dice.

    Los "improbables" (el último que compró esa tela lo hizo hace dos años o
    más) van aparte y al final: mezclados, ensucian una lista que el vendedor
    tiene que poder creer.
    """
    filas = db.fetch_all(
        f"""
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
          LEFT JOIN scintela.cliente c
            ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(l.codigo_cli))
         WHERE (%(vend)s IS NULL
                OR (%(vend)s = 'INTELA' AND l.vend_pc IS NULL)
                OR l.vend_pc = %(vend)s)
           AND (%(cartera)s IS NULL OR {_ES_MI_CLIENTE})
         ORDER BY l.codigo_cli, f.kg_parado DESC
        """,
        {"vend": vend or None, "cartera": cartera_de or None},
    )

    anio = today_ec().year
    # ⭐ La hoja del vendedor también lleva los PUNTOS (dueña 24/08/2026: "es el
    # único papel que se lleva a la calle y es justo donde falta"). Sin esto,
    # el que sale a vender con el papel en la mano no sabe cuál de las telas de
    # ese cliente vale diez veces más que la de al lado.
    puntos = puntos_por_tela()
    clientes: dict[str, dict] = {}
    for f in filas:
        c = clientes.setdefault(f["codigo_cli"], {
            "codigo": f["codigo_cli"], "nombre": f["nombre"],
            "provincia": f["provincia"], "vend_pc": f["vend_pc"],
            "telas": [], "kg_potencial": 0.0, "puntos_potencial": 0.0,
            "improbable": True,
        })
        f["puntos"] = int(puntos.get(f["subcategoria"], {}).get("puntos", 1))
        f["puntos_parado"] = float(f["kg_parado"] or 0) * f["puntos"]
        c["telas"].append(f)
        c["kg_potencial"] += float(f["kg_parado"] or 0)
        c["puntos_potencial"] += f["puntos_parado"]
        if f["anio"] >= anio - 1:
            c["improbable"] = False

    # ⚠ Las telas de cada cliente van por PUNTOS, no por kilos: arriba de la
    # ficha tiene que estar la que más conviene ofrecerle, no la más pesada.
    for c in clientes.values():
        c["telas"].sort(key=lambda t: -t["puntos_parado"])

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


def por_cliente_plano(vend: str | None = None, orden: str = "codigo",
                      cartera_de: str | None = None) -> list[dict]:
    """
    La misma hoja, aplanada a una fila por CLIENTE × TELA, para Excel.

    Sale de `por_cliente()` y no de una consulta propia: si fueran dos caminos,
    el archivo y la pantalla podrían decir cosas distintas del mismo día.

    ⚠ `kg_parado` se repite en cada fila del mismo cliente y entre clientes:
    sumar esa columna en Excel da mucho más que el stock real. Va una columna
    `orden_en_la_hoja` para poder reconstruir el ranking sin sumar nada.
    """
    filas = []
    r = por_cliente(vend, orden, cartera_de=cartera_de)
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
                    "puntos_potencial": c["puntos_potencial"],
                    "subcategoria": t["subcategoria"],
                    "colores_parados": t["colores_parados"],
                    "kg_parado": t["kg_parado"],
                    "puntos": t["puntos"],
                    "puntos_parado": t["puntos_parado"],
                    "kg_cliente": t["kg_cliente"],
                    "ultima_compra": t["ultima_compra"],
                    "anio": t["anio"],
                })
    return filas


# ── Refresh ─────────────────────────────────────────────────────────────────

def cuenta_el_kilo(motivo: str | None, calidad: str | None) -> bool:
    """Si un kilo vendido puntúa en la competencia.

    ⭐ Dueña 24/08/2026: *"solo tiene que ponerse la de segunda en la
    competencia, la de primera no cuenta"*. Un ítem entra a la lista por uno de
    dos motivos, y el kilo tiene que ser de la misma clase que el motivo:

      parado  — la tela × color entera está quieta: TODOS sus kilos entraron a
                la lista, así que todos puntúan.
      segunda — la tela se vende bien y lo único parado son sus kilos SEG: sólo
                la SEG puntúa. Contar la PRI sería dar puntos por vender tela
                que sale sola, que es exactamente lo que entrar sólo con la SEG
                buscaba evitar (370 ítems, 16.124 kg al 24/08/2026).

    ⚠ El motivo es el CONGELADO en la cohorte, no el de la foto de hoy: si
    mañana la tela entera se para, el ítem no puede cambiar de regla en la
    mitad de la carrera. Sin motivo guardado cuenta todo, que es como venía
    antes de la migración 0212.
    """
    return motivo != "segunda" or calidad == "SEG"


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

    # ⭐ TODO se cuenta desde la LARGADA, no desde que cada fila entró a la
    # lista. Dueña 18/08/2026: "hace todo desde 25/08".
    #
    # Antes cada fila medía desde su propia `fecha_marcado`, así que la pantalla
    # de Saldos contaba desde el 13/08 y la competencia desde el 25: dos números
    # distintos para "lo vendido", y el primero que los comparara iba a
    # preguntar cuál era el bueno. `fecha_marcado` sigue guardada — es cuándo
    # entró cada tela— pero ya no manda sobre la cuenta.
    desde_f: date = date.fromisoformat(config("largada", "2026-08-25"))
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
                       (subcategoria, color, fecha_marcado, kg_al_marcar, motivo)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (subcategoria, color) DO NOTHING""",
                (p["subcategoria"], p["color"], hoy, p["stock_kg"],
                 p.get("motivo")), conn=conn)
            # ⚠ El INSERT de arriba no toca a los que ya estaban (ON CONFLICT
            # DO NOTHING), y sin esto los 734 ítems que entraron antes de la
            # migración 0212 se quedaban con el motivo en NULL para siempre —
            # o sea contando TODA la primera, que es justo lo que se vino a
            # arreglar. Se escribe UNA vez y después no se vuelve a mover: la
            # regla de un ítem no puede cambiar en la mitad de la carrera.
            db.execute(
                """UPDATE scintela.parado_cohorte SET motivo = %s
                    WHERE subcategoria = %s AND color = %s
                      AND motivo IS NULL""",
                (p.get("motivo"), p["subcategoria"], p["color"]), conn=conn)

        cohorte = db.fetch_all(
            "SELECT subcategoria, color, fecha_marcado, motivo "
            "FROM scintela.parado_cohorte", conn=conn)

        # 2 · cuánto se vendió de cada uno DESDE SU PROPIA fecha de marcado
        vendido: dict[tuple[str, str], float] = defaultdict(float)
        marcado = {(c["subcategoria"], c["color"]): c["fecha_marcado"] for c in cohorte}
        motivo_de = {(c["subcategoria"], c["color"]): c.get("motivo")
                     for c in cohorte}

        def _cuenta(k: tuple[str, str], calidad: str | None) -> bool:
            return cuenta_el_kilo(motivo_de.get(k), calidad)

        for v in ventas:
            k = (v["subcategoria"], v["color"])
            if (k in marcado and _fecha(v["fecha"]) >= desde_f
                    and _cuenta(k, v.get("calidad"))):
                vendido[k] += float(v["kg"] or 0)   # ya viene abierto por vendedor

        # 3 · la foto se rehace entera
        #
        # ⚠ Antes de borrarla se guarda la CATEGORÍA de cada fila. Un ítem que
        # se vendió entero ya no viene en `parados`, así que su grupo quedaba en
        # NULL y en la pantalla aparecía como "—": justo las filas "resuelto",
        # que son las que uno quiere mirar para ver si la competencia funciona.
        grupo_previo = {(r["subcategoria"], r["color"]): r["categoria"]
                        for r in db.fetch_all(
                            "SELECT subcategoria, color, categoria "
                            "FROM scintela.parado_foto WHERE categoria IS NOT NULL",
                            conn=conn)}
        stock = {(p["subcategoria"], p["color"]): p for p in par}
        db.execute("DELETE FROM scintela.parado_foto", conn=conn)
        for k in marcado:
            p = stock.get(k)
            db.execute(
                """INSERT INTO scintela.parado_foto
                       (subcategoria, color, stock_kg, kg_vendidos, ultima_venta,
                        clientes, anio_pista, kg_primera, kg_segunda, categoria,
                        motivo, kg_tubular, kg_abierta)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (k[0], k[1], (p or {}).get("stock_kg") or 0, vendido.get(k, 0),
                 (p or {}).get("ultima_venta"), total_cli.get(k[0], 0),
                 anio_de.get(k[0]), (p or {}).get("kg_primera") or 0,
                 (p or {}).get("kg_segunda") or 0,
                 (p or {}).get("categoria") or grupo_previo.get(k),
                 (p or {}).get("motivo"),
                 (p or {}).get("kg_tubular") or 0,
                 (p or {}).get("kg_abierta") or 0), conn=conn)

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
            if k in marcado and _fecha(v["fecha"]) >= desde_f:
                db.execute(
                    """INSERT INTO scintela.parado_venta
                           (subcategoria, color, vend_pc, vendedor, fecha, kg,
                            calidad, cuenta)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (k[0], k[1], v.get("vend_pc"), v.get("vendedor") or "Intela",
                     _fecha(v["fecha"]), v.get("kg") or 0,
                     v.get("calidad"), _cuenta(k, v.get("calidad"))), conn=conn)

        db.execute("DELETE FROM scintela.parado_share", conn=conn)
        for s in asinfo_parado.share_por_grupo():
            db.execute(
                """INSERT INTO scintela.parado_share
                       (categoria, vend_pc, vendedor, kg, pct)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (categoria, vendedor) DO NOTHING""",
                (s["categoria"], s.get("vend_pc"), s["vendedor"],
                 s.get("kg") or 0, round(s.get("pct") or 0, 2)), conn=conn)

        # ⭐ LA META SE CONGELA el día de la largada. Antes se reconstruía en
        # cada lectura ("lo que hay hoy + lo vendido"), y el 18/08 —sin una
        # sola venta— bajó de 52.407 a 51.654 kg por ajustes de bodega: el
        # porcentaje de los siete se movía sin que nadie vendiera. Se escribe
        # UNA vez, en el primer refresco del día de la largada o después.
        _fijar_base(conn)
        # ⭐ Y los PUNTOS de cada tela, con la misma regla: antes de la
        # largada se reescriben en cada refresco (es una previsualización),
        # desde la largada se escriben una vez y no se tocan más.
        _fijar_puntos(conn)

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


def telas_a_sacar(filas: list[dict], puntos: dict[str, dict] | None = None) -> list[dict]:
    """
    Qué hay que sacar, tela por tela, sin un solo cliente adentro.

    Es lo que le faltaba al vendedor para poder ofrecer: la pantalla le decía
    quién le compró qué, pero no cuántos kilos hay ni de qué colores. Sin
    clientes, esta lista es información de fábrica y la puede ver cualquiera.
    """
    # ⭐ El puntaje va al lado. Es la única lista donde el vendedor ve la tela y
    # lo que vale un kilo de ella en el mismo renglón: sin eso, para saber si
    # conviene ir a buscarla tendría que acordarse de memoria de 98 números.
    #
    # ⚠ Los puntos entran por parámetro y no se leen acá adentro: la pantalla
    # ya los tiene y buscarlos de nuevo sería una segunda lectura que puede
    # contradecir a la primera.
    puntos = puntos or {}
    g: dict[str, dict] = {}
    for f in filas:
        if float(f["stock_kg"]) <= 0:
            continue
        p = puntos.get(f["subcategoria"], {})
        d = g.setdefault(f["subcategoria"], {
            "tela": f["subcategoria"], "grupo": f["categoria"],
            "kg": 0.0, "colores": [],
            "puntos": int(p.get("puntos", 1)),
            "nivel": p.get("nivel_nombre", "")})
        d["kg"] += float(f["stock_kg"])
        d["colores"].append((f["color"], f.get("color_nombre") or "",
                             float(f["stock_kg"])))
    for d in g.values():
        d["colores"].sort(key=lambda c: -c[2])
        d["n_colores"] = len(d["colores"])
        # `lista` es la que dibuja la pantalla (código + nombre); `colores`
        # sigue siendo el texto de siempre, que es lo que va al Excel.
        d["colores_lista"] = [{"cod": c, "nombre": n} for c, n, _ in d["colores"]]
        d["colores"] = ", ".join(c for c, _, _ in d["colores"])
        d["puntos_total"] = d["kg"] * d["puntos"]
    # ⚠ Ordenada por PUNTOS y no por kilos: es la lista de qué conviene ir a
    # buscar, y una tela de 4.329 kg a 10 puntos vale más que una de 3.442 a 1.
    return sorted(g.values(), key=lambda d: -d["puntos_total"])


# ── La COMPETENCIA ──────────────────────────────────────────────────────────

#: Los que compiten. Los seis vendedores de `scintela.vendedor` más Intela —el
#: mostrador—, que compite como uno más por decisión de la dueña ("hay una
#: vendedora dedicada"). ⚠ Intela es el 51,3% de las ventas de estas telas, así
#: que va a estar arriba: es una decisión tomada con el dato a la vista.
#: Cualquier otro vendedor que aparezca en una venta suma al grupo pero no al
#: ranking — son bajas históricas con kilos residuales.
COMPETIDORES = ["Intela", "Proaño Patricio", "Lopez Felipe", "Miranda Roberto",
                "Quintero Jose", "Ramirez Edgar", "Proaño Sebastián"]


def config(clave: str, default: str) -> str:
    r = db.fetch_one(
        "SELECT valor FROM scintela.parado_config WHERE clave = %s", (clave,))
    return (r or {}).get("valor") or default


def _meta_pct(grupos: list[dict], override: dict[str, float], total_pct: float) -> None:
    """
    La meta de cada grupo, en %.

    ⭐ La dueña pone el TOTAL y ese total se reparte entre los grupos según lo
    que pesa cada uno — o sea, todos con la misma exigencia. Con el total en
    100%, cada grupo despeja sus propios kilos.

    La primera versión hacía otra cosa: le ponía a cada grupo su propio peso
    como meta (Jersey 31,5% → sacarle el 31,5%). Daba un total de 21,7% que
    nadie decidió y una meta de 4 kg para el grupo más chico. Era una fórmula
    que se muerde la cola.

    `override` (tabla `parado_meta`) pisa el % de un grupo puntual.
    """
    for g in grupos:
        g["meta_pct"] = float(override.get(g["grupo"], total_pct))
        g["meta_es_manual"] = g["grupo"] in override
        # ⭐ Sobre los kilos CONGELADOS del día de la largada, no sobre los de
        # hoy: si no, un ajuste de bodega le mueve el puntaje a los siete.
        g["meta_kg"] = g.get("kg_base", g["kg"]) * g["meta_pct"] / 100


def vendido_detalle(desde) -> dict[str, list[dict]]:
    """Qué vendió cada uno, renglón por renglón. `{vendedor: [filas]}`.

    ⭐ Dueña 25/08/2026, mirando el tablero el día de la largada: *"intela ya
    tiene 65kg. quiero ver exacto que vendio"*. El ranking decía el total y la
    fila que se abre lo partía por grupo; qué tela y qué color no estaba en
    ninguna pantalla.

    ⭐ Van TAMBIÉN los kilos que NO puntúan, con el motivo. Es lo que evita la
    conversación de la semana que viene: un vendedor que facturó 512 kg de una
    tela de la lista y ve 0 puntos necesita leer POR QUÉ en la misma pantalla.
    Son kilos de PRIMERA de un ítem que entró sólo por su segunda (ver
    `cuenta_el_kilo`).
    """
    filas = db.fetch_all(
        """SELECT v.vendedor, v.subcategoria, v.color, v.calidad, v.cuenta,
                  v.fecha, SUM(v.kg) AS kg,
                  SUM(v.kg * COALESCE(p.puntos, 1)) AS puntos
             FROM scintela.parado_venta v
             LEFT JOIN scintela.parado_punto p ON p.subcategoria = v.subcategoria
            WHERE v.fecha >= %s
            GROUP BY v.vendedor, v.subcategoria, v.color, v.calidad, v.cuenta,
                     v.fecha
            ORDER BY v.cuenta DESC, SUM(v.kg) DESC""", (desde,)) or []
    out: dict[str, list[dict]] = defaultdict(list)
    for f in filas:
        f["puntos"] = float(f.get("puntos") or 0) if f.get("cuenta") else 0.0
        out[f.get("vendedor") or "Intela"].append(f)
    return out


def competencia() -> dict:
    """
    El tablero de la competencia: quién va ganando y cuánto falta.

    Todo se calcula sobre las MISMAS filas de `items()` que muestra la pantalla
    de Saldos. Si saliera de otra consulta, el termómetro de acá y el total de
    allá podrían no coincidir el mismo día.

    ⭐ Desde el 24/08/2026 el puesto sale de los PUNTOS y no de los kilos: un
    kilo vale 1, 4 o 10 según lo difícil que sea colocar esa tela (ver `PUNTOS`).
    Los kilos siguen a la vista y el premio del mes se sigue corriendo en kilos,
    pero la carrera del año se corre en puntos.
    """
    filas = items()
    # ⚠ Fuera las filas de 0 kg sin grupo: son restos de la cohorte que ya no
    # están en la foto (p. ej. la tela cruda que se sacó). Sumaban un renglón
    # "(sin grupo) · 0 kg" que sólo confunde.
    filas = [f for f in filas if float(f["stock_kg"]) > 0 or f["categoria"]]
    grupos = por_grupo(filas)
    override = {r["categoria"]: float(r["pct"]) for r in db.fetch_all(
        "SELECT categoria, pct FROM scintela.parado_meta")}
    total_pct = float(config("meta_total_pct", "100"))
    largada = date.fromisoformat(config("largada", "2026-08-25"))
    cierre = date.fromisoformat(config("cierre", "2026-12-31"))
    # ⭐ Si la meta ya está congelada, manda ella; antes de la largada la
    # pantalla es una previsualización y se calcula con lo que hay hoy.
    congelada, fijada_el = base_fijada()
    if congelada:
        vistos = {g["grupo"] for g in grupos}
        for grupo in congelada:
            if grupo not in vistos:
                # un grupo que se despejó entero sigue teniendo meta
                grupos.append({"grupo": grupo, "n_items": 0, "kg": 0.0,
                               "kg_segunda": 0.0, "subgrupos": 0, "pct": 0.0})
        for g in grupos:
            g["kg_base"] = congelada.get(g["grupo"], 0.0)
    else:
        for g in grupos:
            g["kg_base"] = g["kg"]
    _meta_pct(grupos, override, total_pct)

    # ⭐ La bolsa de puntos de cada grupo sale de sus TELAS, una por una. No se
    # puede sacar del total de kilos del grupo: adentro de Poliester conviven
    # Kiana Mundial (1 punto) y Microfibra (10), y son el mismo grupo.
    puntos = puntos_por_tela()
    cat_de = {sub: (p["categoria"] or "(sin grupo)") for sub, p in puntos.items()}
    pts_base: dict[str, float] = defaultdict(float)
    for sub, p in puntos.items():
        pts_base[cat_de[sub]] += p["kg_base"] * p["puntos"]
    for g in grupos:
        g["puntos_base"] = pts_base.get(g["grupo"], 0.0)
        g["meta_pts"] = g["puntos_base"] * g["meta_pct"] / 100

    # ⭐ Sólo cuentan las ventas desde la LARGADA. La cohorte se marcó el 13/08
    # y la competencia arranca el 25: sin el corte, esos días le regalarían
    # kilos a quien justo vendió algo.
    #
    # ⚠ Se agrupa por TELA y no por grupo porque el puntaje es de la tela. El
    # grupo sale de `parado_punto`, que está congelado y completo: si saliera de
    # `parado_foto`, una tela vendida entera desaparecería de la foto y sus
    # kilos se caerían del ranking.
    vendido = db.fetch_all(
        """SELECT v.vendedor, v.subcategoria, SUM(v.kg) AS kg,
                  MAX(v.fecha) AS ultima
             FROM scintela.parado_venta v
            WHERE v.fecha >= %s AND v.cuenta
            GROUP BY v.vendedor, v.subcategoria""", (largada,))
    semanas = _semanas(largada)
    share = db.fetch_all("SELECT * FROM scintela.parado_share")

    # ⚠ Los ocho grupos se crean para TODOS aunque nadie haya vendido nada de
    # uno: la fila que se abre tiene que mostrar los ocho, también con cero.
    # Un grupo que falta se lee como "no existe", no como "no vendí".
    tabla = {v: {"vendedor": v, "kg": 0.0, "puntos": 0.0,
                 "ultima": None, "grupos": {}} for v in COMPETIDORES}
    for g in grupos:
        for v in COMPETIDORES:
            tabla[v]["grupos"][g["grupo"]] = {
                "grupo": g["grupo"], "kg": 0.0, "puntos": 0.0}

    detalle_vendido = vendido_detalle(largada)
    fuera = 0.0
    liq_kg: dict[str, float] = defaultdict(float)
    liq_pts: dict[str, float] = defaultdict(float)
    for r in vendido:
        v, sub, kg = r["vendedor"], r["subcategoria"], float(r["kg"] or 0)
        cat = cat_de.get(sub, "(sin grupo)")
        # ⚠ Una tela sin puntaje vale 1, no 0: un kilo vendido nunca puede
        # contar cero. Sólo pasa si la cohorte creció después de congelar.
        pts = kg * int(puntos.get(sub, {}).get("puntos", 1))
        liq_kg[cat] += kg
        liq_pts[cat] += pts
        if v not in tabla:
            fuera += kg          # bajas históricas: suman al grupo, no al ranking
            continue
        tabla[v]["kg"] += kg
        tabla[v]["puntos"] += pts
        if cat in tabla[v]["grupos"]:
            tabla[v]["grupos"][cat]["kg"] += kg
            tabla[v]["grupos"][cat]["puntos"] += pts
        if r["ultima"] and (not tabla[v]["ultima"] or r["ultima"] > tabla[v]["ultima"]):
            tabla[v]["ultima"] = r["ultima"]

    # ⭐ SE FUE EL TOPE POR GRUPO (24/08/2026). Existía para obligar a tocar los
    # ocho grupos: sin él, el que tenía un cliente grande de Jersey llegaba al
    # 100% sin mirar el resto. Los puntos hacen el mismo trabajo sin necesidad
    # de explicar un tope, y lo hacen mejor: el tope igualaba a todas las telas
    # de un grupo, y adentro de un grupo hay telas que salen solas y telas que
    # no salió una en un año.
    #
    # ⭐ Y SE FUE LA META DEL RANKING (24/08/2026). Dueña: "ya que es por
    # puntos, saquemos la meta, ideal es + puntos gana". Tiene razón, y de
    # hecho la meta ya no decidía nada: los siete tenían la MISMA (los puntos
    # totales sobre 7), así que ordenar por "% de su meta" era dividir a todos
    # por la misma constante y daba exactamente el mismo orden que ordenar por
    # puntos. Era una cuenta de más en la pantalla que no movía a nadie de
    # puesto. Los puntos en juego se siguen mostrando como referencia —cuánto
    # hay— pero no como vara de nadie.
    #
    # ⚠ El desempate por NOMBRE no es cosmético: al arrancar están todos en
    # cero, y sin un criterio fijo el orden de los empatados lo decide el
    # diccionario. Entonces "subió dos puestos" sería ruido, porque el puesto de
    # la semana pasada se recalcula con el mismo sort.
    ranking = sorted(tabla.values(),
                     key=lambda d: (-d["puntos"], d["vendedor"]))
    lider = ranking[0]["puntos"] if ranking else 0
    for i, d in enumerate(ranking, 1):
        d["puesto"] = i
        d["vend_yo"] = False
        # La barrita compara contra el primero, no contra una meta: es la
        # distancia que hay que remontar, que es la pregunta que se hace el que
        # va cuarto.
        d["pct_lider"] = 100 * d["puntos"] / lider if lider else 0
        d["detalle"] = sorted(d["grupos"].values(), key=lambda x: -x["puntos"])
        d["vendido"] = detalle_vendido.get(d["vendedor"], [])

    for g in grupos:
        g["liquidado"] = liq_kg.get(g["grupo"], 0.0)
        g["liquidado_pts"] = liq_pts.get(g["grupo"], 0.0)
        g["pct_meta"] = (100 * g["liquidado_pts"] / g["puntos_base"]
                         if g["puntos_base"] else 0)
        g["share"] = sorted(
            [s for s in share if s["categoria"] == g["grupo"]],
            key=lambda s: -float(s["pct"] or 0))[:4]

    _movimiento(ranking, semanas["por_vendedor"], semanas["puntos_por_vendedor"])
    meses = _meses(largada, cierre)
    hay_hoy = sum(g["kg"] for g in grupos)
    liquidado = sum(g["liquidado"] for g in grupos)
    return {
        "hoy": today_ec(),
        "largada": largada,
        "cierre": cierre,
        "dias_para_el_cierre": (cierre - today_ec()).days,
        "total_pct": total_pct,
        "grupos": grupos,
        "ranking": ranking,
        "kg_parado": hay_hoy,
        # "Había" no se guarda: se reconstruye. Lo que hay hoy más lo que se
        # vendió ES lo que había — y así los tres números cierran siempre entre
        # ellos, que es lo que alguien va a chequear de un vistazo.
        # Congelada: los kilos del día de la largada. Sin congelar (previa a la
        # largada): lo que hay hoy más lo que se vendió, que ES lo que había.
        "kg_al_largar": (sum(congelada.values()) if congelada
                         else hay_hoy + liquidado),
        "meta_fijada_el": fijada_el,
        "meta_kg": sum(g["meta_kg"] for g in grupos),
        # Los puntos que hay en juego. No es la meta de nadie: es el tamaño
        # de la bolsa, para saber contra qué se lee un 6.700.
        "puntos_en_juego": sum(g["puntos_base"] for g in grupos),
        "liquidado": liquidado,
        "liquidado_pts": sum(g["liquidado_pts"] for g in grupos),
        "puntos_valor": PUNTOS,
        "kg_fuera_del_ranking": fuera,
        "semanas": semanas["filas"],
        "meses": meses,
        "competidores": COMPETIDORES,
    }


def _semanas(largada: date) -> dict:
    """
    Semana a semana desde la largada. Dueña: "esto vamos a ir midiendo semana a
    semana".

    Las semanas van de LUNES a domingo (`date_trunc('week')` de Postgres) y se
    listan de la más nueva a la más vieja: la que importa es la de arriba.
    """
    # ⚠ Los PUNTOS de la semana también salen de acá: `_movimiento` compara el
    # puesto de hoy contra el de la semana pasada, y el puesto se decide por
    # puntos. Descontando kilos se calcularía un puesto viejo que nunca existió.
    filas = db.fetch_all(
        """SELECT date_trunc('week', v.fecha)::date AS semana,
                  v.vendedor, SUM(v.kg) AS kg,
                  SUM(v.kg * COALESCE(p.puntos, 1)) AS puntos
             FROM scintela.parado_venta v
             LEFT JOIN scintela.parado_punto p
                    ON p.subcategoria = v.subcategoria
            WHERE v.fecha >= %s AND v.cuenta
            GROUP BY 1, 2
            ORDER BY 1 DESC""", (largada,))
    por_semana: dict = {}
    por_vendedor: dict = {}
    puntos_por_vendedor: dict = {}
    for f in filas:
        s = por_semana.setdefault(f["semana"], {"semana": f["semana"], "kg": 0.0,
                                                "detalle": {}})
        kg = float(f["kg"] or 0)
        s["kg"] += kg
        s["detalle"][f["vendedor"]] = kg
        por_vendedor.setdefault(f["vendedor"], {})[f["semana"]] = kg
        puntos_por_vendedor.setdefault(f["vendedor"], {})[f["semana"]] = float(
            f.get("puntos") or 0)

    orden = sorted(por_semana.values(), key=lambda s: s["semana"], reverse=True)
    acum = sum(s["kg"] for s in orden)
    for s in orden:                      # acumulado hasta el final de esa semana
        s["acumulado"] = acum
        acum -= s["kg"]
    return {"filas": orden, "por_vendedor": por_vendedor,
            "puntos_por_vendedor": puntos_por_vendedor}


def _meses(largada: date, cierre: date) -> list[dict]:
    """
    El premio del MES, por kilos totales. Dueña 18/08/2026: "un premio mensual
    por kg totales quizás?".

    ⭐ Va por KILOS y sin tope, al revés del ranking grande. Es a propósito: son
    dos carreras distintas. La del año premia repartir entre tipos de tela; la
    del mes premia sacar kilos, y le da algo para ganar al que viene último en
    el porcentaje. Sin eso, el que se descuelga en octubre no vuelve a mirar la
    pantalla.

    ⚠ Agosto y septiembre cuentan JUNTOS: del 25 al 31 de agosto hay cinco días
    hábiles, y un "premio del mes" por esa semana no es un mes.
    """
    filas = db.fetch_all(
        """SELECT date_trunc('month', v.fecha)::date AS mes,
                  v.vendedor, SUM(v.kg) AS kg
             FROM scintela.parado_venta v
            WHERE v.fecha >= %s AND v.cuenta
            GROUP BY 1, 2""", (largada,))
    if not filas:
        return []

    primer_mes = date(largada.year, largada.month, 1)
    def bucket(m):
        return date(largada.year, largada.month + 1, 1) if m == primer_mes else m

    por_mes: dict = {}
    for f in filas:
        m = bucket(f["mes"])
        d = por_mes.setdefault(m, {"mes": m, "kg": 0.0, "tabla": {}})
        kg = float(f["kg"] or 0)
        d["kg"] += kg
        d["tabla"][f["vendedor"]] = d["tabla"].get(f["vendedor"], 0.0) + kg

    hoy = today_ec()
    mes_actual = bucket(date(hoy.year, hoy.month, 1))
    salida = []
    for m in sorted(por_mes, reverse=True):
        d = por_mes[m]
        orden = sorted(d["tabla"].items(), key=lambda x: (-x[1], x[0]))
        d["podio"] = [{"vendedor": v, "kg": kg} for v, kg in orden[:3]]
        d["ganador"] = orden[0][0] if orden else None
        # ⭐ Dueña 20/08/2026: "acá me gustaría tener a todos ordenados por kg".
        # El podio de tres dejaba afuera a la mitad de la tabla, y justo el que
        # va último es el que más necesita ver dónde está parado. Van los SIETE,
        # también los que todavía no vendieron nada: un cero en la lista dice
        # más que no figurar.
        d["ranking"] = [
            {"puesto": i, "vendedor": v, "kg": d["tabla"].get(v, 0.0)}
            for i, v in enumerate(
                sorted(COMPETIDORES,
                       key=lambda v: (-d["tabla"].get(v, 0.0), v)), 1)]
        d["cerrado"] = m < mes_actual and m <= cierre
        salida.append(d)
    return salida


def _movimiento(ranking: list[dict], por_vendedor: dict,
                puntos_por_vendedor: dict | None = None) -> None:
    """
    Cuánto subió o bajó cada uno respecto de la semana pasada.

    ⭐ Sin esto, el que va cuarto no tiene ningún motivo para volver a abrir la
    pantalla. El puesto de la semana pasada se recalcula descontando lo que cada
    uno vendió ESTA semana — no hace falta guardar el ranking viejo.
    """
    if not por_vendedor:
        for r in ranking:
            r["movimiento"] = 0
            r["kg_semana"] = 0.0
        return
    ultima = max(s for v in por_vendedor.values() for s in v)
    pts_semana = puntos_por_vendedor or {}
    # ⚠ Se descuentan PUNTOS, no kilos: el puesto se decide por puntos, así que
    # restar kilos daría un puesto de la semana pasada que nunca existió — y el
    # "subió dos puestos" sería inventado.
    antes = sorted(
        ranking,
        key=lambda r: (-max(0.0, r["puntos"]
                            - pts_semana.get(r["vendedor"], {}).get(ultima, 0)),
                       r["vendedor"]))
    puesto_antes = {r["vendedor"]: i for i, r in enumerate(antes, 1)}
    for r in ranking:
        r["kg_semana"] = por_vendedor.get(r["vendedor"], {}).get(ultima, 0.0)
        r["movimiento"] = puesto_antes[r["vendedor"]] - r["puesto"]


#: Predicado canónico de pertenencia cliente→vendedor.
#:
#: ⭐ Se IMPORTA de `mi_cartera.queries` en vez de copiarse. Escrito a mano acá,
#: el día que allá cambien el criterio —un TRIM, un COALESCE, lo que sea— las
#: dos pantallas le mostrarían al vendedor carteras distintas y nadie se
#: enteraría. Lo único que se cambia es el nombre del parámetro, porque acá la
#: query ya usa %(vend)s para otra cosa (el vendedor de Asinfo).
_ES_MI_CLIENTE = _mc_es_mi_cliente.replace("%(vend)s", "%(cartera)s")


def mis_clientes_parado(vend: str) -> list[dict]:
    """
    Los clientes DEL VENDEDOR que alguna vez compraron alguna de las telas
    paradas. Dueña 17/08/2026: "podríamos mostrarles de sus clientes cuales
    compraron de estas telas en el pasado" · "cada vendedor tiene sus clientes".

    ⚠ La cartera sale de `scintela.cliente.vend` —la de Programa Core— y NO del
    vendedor que figura en la última factura de Asinfo. Son cosas distintas y a
    veces no coinciden: usando la de Asinfo, un cliente podría salirle a uno acá
    y a otro en /mi-cartera.

    ⚠ Devuelve [] con `vend` vacío, nunca la lista entera. Un scope que falla
    abierto no da error: muestra de más y nadie se entera.
    """
    if not vend:
        return []
    return db.fetch_all(
        f"""
        SELECT l.codigo_cli,
               MAX(l.nombre)                         AS nombre,
               MAX(l.provincia)                      AS provincia,
               COUNT(DISTINCT l.subcategoria)        AS telas,
               STRING_AGG(DISTINCT l.subcategoria, ', ') AS lista_telas,
               MAX(l.ultima_compra)                  AS ultima_compra,
               SUM(l.kg)                             AS kg
          FROM scintela.parado_llamado l
          JOIN scintela.cliente c
            ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(l.codigo_cli))
         WHERE {_ES_MI_CLIENTE}
         GROUP BY l.codigo_cli
         ORDER BY SUM(l.kg) DESC
        """,
        {"cartera": vend},
    )
