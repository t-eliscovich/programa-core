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


def items(conn=None) -> list[dict]:
    """
    La cohorte entera, con la foto de hoy pegada al lado.

    ⚠ `conn` no es un detalle: la meta y los puntos se congelan DENTRO de la
    transacción del refresco, y sin pasarle esa misma conexión leerían la foto
    ANTERIOR —la única que está commiteada— y se congelarían sobre un universo
    que ya no es el que se acaba de escribir.

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
               -- ⚠ El grupo cae al PUNTAJE cuando la foto no lo tiene. La
               -- tela que se vendió entera ya no viene en la consulta de
               -- parados, así que su fila de la foto se escribe sin grupo y la
               -- lista mostraba "—" justo en los renglones que hay que mirar
               -- —los vendidos— (dueña 25/08/2026: "¿por qué a los vendidos se
               -- nos fue grupo y forma?"). `parado_punto` lo guarda por tela y
               -- está congelado desde la largada.
               CASE WHEN COALESCE(f.categoria, pp.categoria)
                         IN ('Franela', 'Cuellos', 'Puños')
                    THEN 'FCP' ELSE COALESCE(f.categoria, pp.categoria)
               END                                    AS categoria,
               c.fecha_marcado, c.kg_al_marcar,
               COALESCE(f.stock_kg, 0)    AS stock_kg,
               COALESCE(f.kg_vendidos, 0) AS kg_vendidos,
               -- ⭐ Lo vendido, abierto por CATEGORÍA (dueña 25/08/2026:
               -- "poneme la categoría de lo vendido"). La fila se abre en dos
               -- cuando el color tiene kilos de primera y de segunda, así que
               -- lo vendido tiene que poder abrirse igual: si no, las dos
               -- líneas mostrarían el mismo total y una de las dos mentiría.
               -- ⚠ Sólo lo que CUENTA, como en la tabla de Vendidos y en el
               -- ranking: los kilos que quedaron afuera por el tope no son
               -- saldo destrabado.
               COALESCE(v.pri, 0)         AS kg_vend_pri,
               COALESCE(v.seg, 0)         AS kg_vend_seg,
               f.ultima_venta,
               COALESCE(f.clientes, 0)    AS clientes,
               f.anio_pista,
               COALESCE(f.kg_primera, 0) AS kg_primera,
               COALESCE(f.kg_segunda, 0) AS kg_segunda,
               COALESCE(f.kg_tubular, 0) AS kg_tubular,
               COALESCE(f.kg_abierta, 0) AS kg_abierta,
               COALESCE(f.kg_tub_pri, 0) AS kg_tub_pri,
               COALESCE(f.kg_tub_seg, 0) AS kg_tub_seg,
               COALESCE(f.kg_abi_pri, 0) AS kg_abi_pri,
               COALESCE(f.kg_abi_seg, 0) AS kg_abi_seg,
               -- ⭐ La forma, ya resuelta acá: TUB, ABI, las dos, o vacío
               -- cuando el lote no lo dice. La pantalla y la hoja impresa
               -- muestran lo mismo sin repetir el `if` en dos plantillas.
               -- ⚠ El ELSE no es vacío: cae a la forma de la TELA, que sale
               -- de todos sus lotes. Sin eso, la que se vendió entera —o la que
               -- tiene el lote sin el atributo— mostraba "—", y toda tela
               -- terminada es tubular o abierta (dueña 25/08/2026).
               -- ⚠ NO hay caso «las dos»: una tela es tubular o abierta
               -- (dueña 25/08/2026). El refresco ya dobló los kilos de la
               -- forma minoritaria sobre la que manda, así que estas dos
               -- columnas no pueden ser las dos mayores que cero. Sostener
               -- acá el caso mezclado sería decir que puede pasar.
               CASE WHEN COALESCE(f.kg_tubular, 0) > 0 THEN 'TUB'
                    WHEN COALESCE(f.kg_abierta, 0) > 0 THEN 'ABI'
                    ELSE COALESCE(f.forma, '') END     AS forma,
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
          LEFT JOIN scintela.parado_punto pp
                 ON pp.subcategoria = c.subcategoria
          LEFT JOIN LATERAL (
              SELECT SUM(pv.kg) FILTER (WHERE pv.calidad = 'SEG') AS seg,
                     SUM(pv.kg) FILTER (WHERE pv.calidad IS DISTINCT FROM 'SEG')
                                                                  AS pri
                FROM scintela.parado_venta pv
               WHERE pv.subcategoria = c.subcategoria AND pv.color = c.color
                 AND pv.cuenta) v ON TRUE
          -- ⚠ LATERAL con LIMIT 1: el catálogo tiene el mismo código repetido
          -- (una fila por clase de color) y sin el tope la fila se duplicaría.
          LEFT JOIN LATERAL (
              SELECT SPLIT_PART(tc.color, ' · ', 1) AS n
                FROM scintela.tinto_costos tc
               WHERE UPPER(TRIM(tc.cod)) = UPPER(TRIM(c.color))
                 AND COALESCE(TRIM(tc.color), '') <> ''
               LIMIT 1) nom ON TRUE
         -- ⭐ Las apagadas no se muestran. Son las que no vendieron nada en 12
         -- meses por un motivo que no es estar paradas: se hicieron hace menos
         -- de `asinfo_parado.DIAS_QUIETO` días, o tienen un pedido esperando.
         -- La fila no se borra —la cohorte no borra nunca— pero no está en la
         -- lista ni en la competencia, y vuelve sola el día que la tela cumpla
         -- los días o el pedido salga.
         WHERE NOT c.fuera
           -- ⭐ Y tampoco las que están en CERO (dueña 25/08/2026: "sacar las
           -- que están en 0 también"): sin kilos en bodega y sin un kilo
           -- vendido no hay nada que ofrecer ni nada que mostrar — la tela se
           -- fue de la bodega por un ajuste, un traslado o un recuento.
           --
           -- ⚠ La que se vendió SÍ se queda, aunque hoy tenga 0 kg: ésa es la
           -- que hay que ver ("si empezamos a venderlas, que no se nos vayan de
           -- la lista"), y es la única forma de saber si la competencia
           -- funcionó.
           AND (COALESCE(f.stock_kg, 0) > 0 OR COALESCE(f.kg_vendidos, 0) > 0)
         ORDER BY COALESCE(f.stock_kg, 0) DESC, c.subcategoria, c.color
        """, conn=conn
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


def resumen(filas: list[dict], kg_base: float | None = None) -> dict:
    """Los números de las tarjetas. Se calculan sobre las filas ya leídas para
    que la tarjeta y la tabla no puedan decir cosas distintas.

    ⭐ Las tres cifras se leen de corrido: AL ARRANCAR − VENDIDO = QUEDA (dueña
    25/08/2026: *"si teníamos 54k, lo que se vendió no puede ser 1k. o
    deberíamos tener meta inicial, vendido, cuánto queda actualmente"*). La
    pantalla mostraba el stock de hoy y lo vendido, dos números que no se
    tocaban entre sí, y no había manera de ver el movimiento.

    `kg_base` son los kilos CONGELADOS de la largada, los mismos contra los que
    corre la competencia. Sin ellos —antes de la largada— el arranque se
    reconstruye como stock de hoy + lo vendido, que cierra por construcción.

    ⚠ `kg_movido` es lo que la resta NO explica: la bodega se mueve por cosas
    que no son estas ventas (ajustes, producción que entra a una tela de la
    lista, tela que sale sin factura). Va a la vista y no escondido: la primera
    vez que los tres números no cierren, la pregunta va a ser justamente ésa.
    """
    # ⚠ La clave NO se puede llamar `items`: en Jinja `resumen.items` resuelve
    # primero el MÉTODO del diccionario, así que la tarjeta imprimía
    # "<built-in method items of dict object at 0x…>" en vez del número. No da
    # error — renderiza 200 y queda un texto absurdo donde va una cifra.
    kg = sum(float(f["stock_kg"]) for f in filas)
    vendido = sum(float(f["kg_vendidos"]) for f in filas)
    inicial = float(kg_base) if kg_base else kg + vendido
    return {
        "n_items": len(filas),
        "kg": kg,
        "kg_vendidos": vendido,
        "kg_inicial": inicial,
        "kg_movido": round(inicial - vendido - kg, 2),
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
    for f in items(conn):
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


def kg_al_arrancar() -> float | None:
    """Los kilos que había el día de la largada, los CONGELADOS.

    Son los mismos que mide la competencia: si la pantalla de Saldos reconstruye
    su propio "al arrancar" con la foto de hoy, los dos números se separan en
    cuanto la bodega se mueva y nadie sabe cuál es el bueno. `None` mientras no
    se hayan fijado (antes de la largada, o entre la migración que los rehace y
    el primer refresco).
    """
    kg, _ = base_fijada()
    return round(sum(kg.values()), 2) if kg else None


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
    for f in items(conn):
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


def abrir_en_lineas(filas: list[dict]) -> list[dict]:
    """Una línea por tela × color × FORMA × CALIDAD, para la hoja que se sale a
    vender.

    ⭐ Dueña 25/08/2026: *"dos lineas para el color cuando hay ambas telas"* y,
    enseguida, *"idem con PRI y SEG como tubular y abierta, no es lo mismo"*.
    Tubular y abierta se cortan distinto; primera y segunda se venden a precios
    distintos. Un renglón de 171 kg que en realidad son 95 tubulares de segunda
    y 76 abiertas de primera promete cuatro cosas a la vez.

    Un color que viene de una sola manera NO se abre: queda como estaba y su
    fila ya lo dice todo.

    ⚠ Los kilos por forma y calidad salen del LOTE y el total de la fila de
    otra tabla (`saldo_producto`): cierran al 0,006% pero no son la misma
    consulta. Lo que sobre o falte va en una línea aparte SIN forma ni calidad,
    y sólo si pasa de un kilo. Un kilo repartido a ojo en la columna de la
    izquierda es peor que un renglón que dice "no sé de qué es".
    """
    combos = (("TUB", "PRI", "kg_tub_pri"), ("TUB", "SEG", "kg_tub_seg"),
              ("ABI", "PRI", "kg_abi_pri"), ("ABI", "SEG", "kg_abi_seg"))
    salida: list[dict] = []
    for f in filas:
        total = float(f.get("stock_kg") or 0)
        partes = [(forma, cal, float(f.get(col) or 0)) for forma, cal, col in combos]
        vivas = [p for p in partes if p[2] > 0]
        if len(vivas) <= 1:
            salida.append(f)
            continue
        for forma, cal, kg in vivas:
            salida.append(_linea(f, kg, forma, cal))
        resto = round(total - sum(p[2] for p in vivas), 2)
        if abs(resto) >= 1:
            salida.append(_linea(f, resto, "", ""))
    return salida


def _linea(f: dict, kg: float, forma: str, cal: str) -> dict:
    """Una de las líneas en que se abre un color: sus kilos y sus puntos."""
    g = dict(f)
    g["stock_kg"] = kg
    g["forma_fila"] = forma
    g["cal_fila"] = cal
    g["kg_tubular"] = kg if forma == "TUB" else 0
    g["kg_abierta"] = kg if forma == "ABI" else 0
    g["kg_primera"] = kg if cal == "PRI" else 0
    g["kg_segunda"] = kg if cal == "SEG" else 0
    # ⭐ Y lo vendido de ESA categoría, no el total del color: la línea de
    # primera no puede llevarse los kilos que se vendieron de segunda.
    # La línea del resto —la que no tiene calidad— no muestra ninguno: no se
    # sabe de cuál era.
    g["kg_vendidos"] = (float(f.get("kg_vend_pri") or 0) if cal == "PRI"
                        else float(f.get("kg_vend_seg") or 0) if cal == "SEG"
                        else 0.0)
    # ⚠ Y los dos detalles también, no sólo el total: la píldora de categoría de
    # una fila VENDIDA se dibuja mirándolos —del stock no se puede, no queda
    # lote— y si quedaran los del ítem entero, la línea de primera de un color
    # que vendió las dos calidades diría "SEG" o se quedaría sin píldora.
    g["kg_vend_pri"] = g["kg_vendidos"] if cal == "PRI" else 0.0
    g["kg_vend_seg"] = g["kg_vendidos"] if cal == "SEG" else 0.0
    g["puntos_fila"] = kg * float(f.get("puntos") or 1)
    return g

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

def _quien_vendio(v: dict) -> str:
    """El vendedor de una venta, en los términos de la competencia.

    ⭐ Dueña 25/08/2026, viendo "Bedon Hector" en la tabla de vendidos: *"bedón
    no es un vendedor"*. En Asinfo firman ventas nombres que no están entre los
    siete que compiten —bajas, gente de otra área, cargas viejas—: esas ventas
    las hizo LA CASA. Es la misma decisión del 17/08 que juntó "Cía. Ltda.
    Intela" y las facturas sin vendedor bajo el nombre Intela.

    ⚠ Se normaliza al ESCRIBIR `parado_venta`, no al mostrar: así el ranking, el
    cuadro por grupo y la tabla de vendidos cuentan lo mismo. Mostrarlo sólo en
    la pantalla dejaba kilos que aparecían como de Intela y no sumaban en su
    fila.
    """
    quien = (v.get("vendedor") or "").strip()
    return quien if quien in COMPETIDORES else "Intela"


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

    ⚠ La única excepción la escribe el refresco: a un ítem de tela RECIÉN HECHA
    o PEDIDA se le corrige el motivo a `segunda` aunque ya estuviera congelado
    en `parado` — sus kilos de primera nunca debieron contar. Y no vuelve a
    `parado` cuando la tela cumple los meses: quedarse en la regla más
    exigente no le regala puntos a nadie, y volver sí.
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
    todas = asinfo_parado.parados()
    lla = asinfo_parado.llamados()
    # ⭐ La forma de cada tela × color, de TODOS sus lotes (dueña 25/08/2026:
    # "no hay chance que sea —, es tub o abi"). La del stock de hoy no alcanza:
    # la tela vendida entera no tiene lote con saldo que mirar.
    forma_de = asinfo_parado.formas()
    ultima_antes_de = asinfo_parado.ultima_venta_antes(
        config("largada", "2026-08-25"))

    def _una_sola_forma(p: dict) -> dict:
        """Los kilos de la forma minoritaria pasan a la que manda.

        ⭐ Dueña 25/08/2026: *"telas no pueden ser tub y abi al mismo tiempo"*.
        `formas()` ya elige una sola sigla para la fila, pero los kilos venían
        abiertos en cuatro (tub/abi × pri/seg) y con eso la lista partía la fila
        en DOS renglones —uno tubular y otro abierto— de la misma tela. Un
        renglón de 3 kg abiertos que en realidad son lotes mal marcados no es
        una fila de la lista: es ruido con el que nadie puede salir a vender.

        Los kilos no se pierden, se mudan: el total de la fila no cambia y la
        apertura por PRI/SEG —que sí es real— se mantiene.
        """
        forma = forma_de.get((p["subcategoria"], p["color"]))
        if forma not in ("TUB", "ABI"):
            return p
        de, a = ("abi", "tub") if forma == "TUB" else ("tub", "abi")
        for cal in ("pri", "seg"):
            sobra = float(p.get(f"kg_{de}_{cal}") or 0)
            if sobra:
                p[f"kg_{a}_{cal}"] = float(p.get(f"kg_{a}_{cal}") or 0) + sobra
                p[f"kg_{de}_{cal}"] = 0
        junto = float(p.get("kg_tubular") or 0) + float(p.get("kg_abierta") or 0)
        if forma == "TUB":
            p["kg_tubular"], p["kg_abierta"] = junto, 0
        else:
            p["kg_abierta"], p["kg_tubular"] = junto, 0
        return p

    todas = [_una_sola_forma(p) for p in todas]

    # ⭐ NI LA RECIÉN HECHA, NI LA PEDIDA, NI LA QUE SE SIGUE TEJIENDO SON TELA
    # PARADA (dueña 25/08/2026: "no es saldo si se seguía produciendo ese color
    # x tela").
    # Asinfo ya las viene marcando; acá se parten en dos y cada mitad tiene su
    # destino: las que entran, a la lista; las que no, apagadas en la cohorte.
    # Las dos razones se cuentan por separado — en la pantalla no significan lo
    # mismo: una se arregla sola con el tiempo, la otra cuando salga el pedido.
    par = [p for p in todas if p.get("entra", True)]

    def kg_en_bodega(fs) -> float:
        """Los kilos que esas telas tienen HOY en la bodega.

        ⚠ Nombre largo a propósito: se llamaba `kg` y el loop de las ventas la
        pisó con un float. Los tests lo agarraron, pero el nombre corto en una
        función de 150 líneas es una trampa puesta para el próximo.
        """
        return round(sum(float(f.get("stock_bodega") or 0) for f in fs), 2)


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
        # ⚠⚠ QUÉ SE PUEDE APAGAR. Las banderas hablan de la tela, no del motivo
        # por el que el ítem entró a la lista, y desde que se calculan para TODA
        # la bodega —hizo falta para la tela ya vendida— contestan también por
        # telas que nunca fueron un saldo parado. Sin este filtro, cualquier
        # tela que se venda bien y esté en producción quedaba marcada `fuera`, y
        # con ella se borraban sus ventas: el 25/08/2026 la tabla de Vendidos
        # apareció VACÍA y los vendedores perdieron sus puntos.
        #
        # Sólo se apaga lo que entró como `parado`. Un ítem de SEGUNDA entró por
        # kilos que son un saldo se venda la tela o no, así que ninguna de las
        # tres banderas lo descalifica — y si vendió toda su segunda, eso es
        # justo lo que hay que premiar.
        motivo_previo = {
            (c["subcategoria"], c["color"]): c.get("motivo")
            for c in db.fetch_all(
                "SELECT subcategoria, color, motivo FROM scintela.parado_cohorte",
                conn=conn)}

        # ⚠ Se APAGA por la bandera, no por "no está en la lista de hoy". La
        # tela que se vendió entera tampoco está en la lista —no le queda un
        # kilo— y ésa tiene que quedarse: vender lo parado es exactamente lo que
        # la competencia premia. La que se saca es la que no debió entrar nunca.
        fuera = [p for p in todas
                 if not p.get("entra", True)
                 and (p.get("nueva") or p.get("pedida") or p.get("produciendo"))
                 and (motivo_previo.get((p["subcategoria"], p["color"]))
                      or "parado") == "parado"]
        nuevas = [p for p in fuera if p.get("nueva")]
        pedidas = [p for p in fuera if p.get("pedida")]
        produciendo = [p for p in fuera if p.get("produciendo")]

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
                      AND (motivo IS NULL
                           OR (%s AND motivo = 'parado'))""",
                (p.get("motivo"), p["subcategoria"], p["color"],
                 bool(p.get("nueva") or p.get("pedida")
                      or p.get("produciendo"))), conn=conn)
            # ⭐ Y se vuelve a encender la que había quedado afuera: cumplió
            # los días en bodega, o su pedido ya salió (o envejeció). Hoy sí
            # está parada.
            # Vuelve con su `fecha_marcado` original — nunca se fue de la
            # cohorte, sólo estaba apagada.
            db.execute(
                """UPDATE scintela.parado_cohorte SET fuera = FALSE
                    WHERE subcategoria = %s AND color = %s AND fuera""",
                (p["subcategoria"], p["color"]), conn=conn)

        # ⚠ Apagar, no borrar. La cohorte es deliberadamente inmutable ("si
        # empezamos a venderlas, que no se nos vayan de la lista"), así que la
        # que nunca debió entrar se marca y deja de contar en todos lados, pero
        # la fila queda: el día que sus rollos cumplan los meses vuelve sola,
        # con la fecha en que se marcó.
        for p in fuera:
            db.execute(
                """UPDATE scintela.parado_cohorte SET fuera = TRUE
                    WHERE subcategoria = %s AND color = %s""",
                (p["subcategoria"], p["color"]), conn=conn)

        # ⚠⚠ EL ÍTEM QUE ASINFO YA NO DEVUELVE TAMBIÉN NECESITA MOTIVO.
        #
        # El UPDATE de arriba corre sobre `par`, o sea sobre lo que HOY está
        # parado. El ítem que dejó de calificar —se movió, o se vendió antes de
        # la largada— no aparece más en esa consulta, así que su motivo se
        # queda en NULL para siempre. Y sin motivo, `cuenta_el_kilo()` cuenta
        # TODO, primera incluida: el día que ese ítem vuelva a tener stock y se
        # venda, paga kilos que nunca fueron un saldo.
        #
        # Medidos el 25/08/2026: 9 ítems de la cohorte original del 17/08 —antes
        # de que la columna existiera— con 1.129 kg al marcar. Tres de ellos
        # tienen kilos de verdad en la bodega hoy (Fleece Lycra ELE 243, Alemania
        # BAN 216, Jersey 3.5 POS 141).
        #
        # Se les escribe `segunda`, la regla MÁS EXIGENTE: quedarse en la más
        # exigente no le regala puntos a nadie, y volver sí. Es la misma
        # decisión que ya se tomó para la tela recién hecha.
        db.execute(
            "UPDATE scintela.parado_cohorte SET motivo = 'segunda' "
            " WHERE motivo IS NULL", conn=conn)

        cohorte = db.fetch_all(
            "SELECT subcategoria, color, fecha_marcado, kg_al_marcar, motivo "
            "FROM scintela.parado_cohorte WHERE NOT fuera", conn=conn)

        # 2 · cuánto se vendió de cada uno DESDE SU PROPIA fecha de marcado
        vendido: dict[tuple[str, str], float] = defaultdict(float)
        marcado = {(c["subcategoria"], c["color"]): c["fecha_marcado"] for c in cohorte}
        motivo_de = {(c["subcategoria"], c["color"]): c.get("motivo")
                     for c in cohorte}

        def _cuenta(k: tuple[str, str], calidad: str | None) -> bool:
            return cuenta_el_kilo(motivo_de.get(k), calidad)

        # ⭐ EL TOPE DE CADA ÍTEM: no se puede sacar más de lo que había parado.
        #
        # Dueña 25/08/2026, viendo a Intela con 554 puntos de una sola venta:
        # *"está mal que sigue contando una tela que había 0 en saldo"*. Jersey 3
        # BLA tenía unos kilos viejos de blanco y 490 tejidos el 17/07: la regla
        # de la antigüedad mira el ÍTEM, así que lo dejaba entrar entero y los
        # kilos de julio puntuaban como si hubieran estado clavados.
        #
        # El tope son los kilos que esa tela × color ya tenía en la bodega al
        # corte —los únicos que estaban parados—. Lo que se venda por encima es
        # tela que se tejió después: se vende igual, pero no destraba nada y no
        # suma. Con el tope, además, «vendido» nunca puede ser más que «al
        # arrancar», que es lo que hacía que las tres cifras de la pantalla no
        # cerraran.
        #
        # ⚠ Sin dato de Asinfo no hay tope (None y no 0): un ítem que falte en
        # la consulta dejaría a alguien sin sus puntos sin que nadie sepa por
        # qué.
        # ⚠ Y el tope se cruza con lo que el ítem tenía CUANDO ENTRÓ a la
        # lista (dueña 25/08/2026: "tiene que contar solo kgs que estaban en la
        # competencia para empezar"). Son dos preguntas distintas y hay que
        # pasar las dos: `kg_antes` dice cuántos kilos estaban parados hace 90
        # días, y `kg_al_marcar` cuántos había el día que la tela entró a la
        # competencia. Un ítem que tenía 30 kg viejos y recibió 500 el 20/08
        # pasa el primero y no el segundo: esos 500 nunca estuvieron en juego.
        marcado_kg = {(c["subcategoria"], c["color"]): c["kg_al_marcar"]
                      for c in cohorte if c.get("kg_al_marcar") is not None}
        tope = {}
        for p in todas:
            if p.get("kg_antes") is None:
                continue          # sin dato de Asinfo no hay tope de antigüedad
            k = (p["subcategoria"], p["color"])
            topes = [float(p["kg_antes"])]
            if k in marcado_kg:
                topes.append(float(marcado_kg[k]))
            tope[k] = min(topes)
        # ⚠⚠ EL ÍTEM QUE ASINFO YA NO DEVUELVE TAMBIÉN TIENE TOPE, y es el que
        # le pusimos nosotros el día que entró.
        #
        # `todas` es lo que hay HOY en la bodega: el ítem que se vendió entero
        # —o que salió de la bodega— desaparece de esa consulta. Sin esta
        # línea se quedaba sin tope, y "sin tope" significaba contar TODO lo
        # que se venda de esa tela × color de acá al cierre: si mañana se teje
        # de nuevo, esos kilos nuevos puntúan como si hubieran estado clavados.
        # Es el mismo agujero de Jersey 3 BLA (554 puntos de una venta de tela
        # recién hecha) pero por la puerta de atrás. Medidos el 25/08/2026: 9
        # ítems, 1.129 kg al marcar.
        #
        # `kg_al_marcar` no es un dato que falte: es NUESTRO número congelado
        # el día que el ítem entró a la lista, y la regla de la dueña es
        # exactamente ésa — "tiene que contar solo kgs que estaban en la
        # competencia para empezar".
        for k, kg in marcado_kg.items():
            tope.setdefault(k, float(kg))

        # Los renglones que van a la competencia, EN ORDEN DE FECHA y con el
        # tope ya aplicado. Se arman una sola vez: la cuenta del resumen y la
        # tabla de «Qué vendió» tienen que salir del mismo reparto o van a decir
        # cosas distintas.
        usado: dict[tuple[str, str], float] = defaultdict(float)
        renglones: list[tuple[dict, float]] = []
        for v in sorted(ventas, key=lambda x: _fecha(x["fecha"])):
            k = (v["subcategoria"], v["color"])
            if k not in marcado or _fecha(v["fecha"]) < desde_f:
                continue
            kilos = float(v["kg"] or 0)
            cuenta_kg = 0.0
            if _cuenta(k, v.get("calidad")):
                t = tope.get(k)
                if kilos < 0 or t is None:
                    # ⚠ La DEVOLUCIÓN resta siempre, tope o no: si no, un
                    # vendedor factura hasta el tope, el cliente devuelve y los
                    # puntos quedan.
                    cuenta_kg = kilos
                else:
                    cuenta_kg = min(kilos, max(0.0, t - usado[k]))
                usado[k] += cuenta_kg
            vendido[k] += cuenta_kg
            renglones.append((v, cuenta_kg))

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
        # ⭐ LA ÚLTIMA VENTA ES LA DE ANTES DE LA COMPETENCIA (dueña
        # 25/08/2026: "última venta tiene que tener la fecha antes de la
        # competencia").
        #
        # Es la columna que justifica que el ítem sea un saldo: cuánto hace que
        # nadie lo pedía. Con la fecha de Asinfo a secas, el primer kilo que se
        # vende en la competencia la pisa con la de HOY y la fila borra sola la
        # prueba de que estaba clavada — encima con la venta que la competencia
        # acaba de premiar. Lo vendido después de la largada ya se muestra
        # aparte, en la columna Vendido y en la tabla del pie.
        #
        # ⚠ El primer intento fue conservar la que ya tenía la foto. No alcanza:
        # el día de la largada la foto YA se había refrescado con la fecha de
        # hoy, así que las ocho telas que se vendieron ese día quedaron con la
        # columna en "—" y no había de dónde sacarla ("¿por qué cuellos no tiene
        # última?"). Ahora se le pide a Asinfo con el corte adentro.
        stock = {(p["subcategoria"], p["color"]): p for p in par}
        db.execute("DELETE FROM scintela.parado_foto", conn=conn)
        for k in marcado:
            p = stock.get(k)
            db.execute(
                """INSERT INTO scintela.parado_foto
                       (subcategoria, color, stock_kg, kg_vendidos, ultima_venta,
                        clientes, anio_pista, kg_primera, kg_segunda, categoria,
                        motivo, kg_tubular, kg_abierta,
                        kg_tub_pri, kg_tub_seg, kg_abi_pri, kg_abi_seg, forma)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s)""",
                (k[0], k[1], (p or {}).get("stock_kg") or 0, vendido.get(k, 0),
                 ultima_antes_de.get(k),
                 total_cli.get(k[0], 0),
                 anio_de.get(k[0]), (p or {}).get("kg_primera") or 0,
                 (p or {}).get("kg_segunda") or 0,
                 (p or {}).get("categoria") or grupo_previo.get(k),
                 (p or {}).get("motivo"),
                 (p or {}).get("kg_tubular") or 0,
                 (p or {}).get("kg_abierta") or 0,
                 (p or {}).get("kg_tub_pri") or 0,
                 (p or {}).get("kg_tub_seg") or 0,
                 (p or {}).get("kg_abi_pri") or 0,
                 (p or {}).get("kg_abi_seg") or 0,
                 forma_de.get(k)), conn=conn)

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
        for v, cuenta_kg in renglones:
            k = (v["subcategoria"], v["color"])
            kilos = float(v["kg"] or 0)
            # ⭐ Una venta puede quedar PARTIDA por el tope: los kilos que
            # destrabaron saldo cuentan y el resto no. Van como dos renglones y
            # no como uno a medias, para que la suma de la tabla siga siendo la
            # suma de sus filas.
            partes = [(cuenta_kg, True)] if abs(cuenta_kg - kilos) < 0.005 else [
                (cuenta_kg, True), (round(kilos - cuenta_kg, 2), False)]
            for parte, cuenta in partes:
                if parte == 0 and cuenta:
                    continue
                db.execute(
                    """INSERT INTO scintela.parado_venta
                           (subcategoria, color, vend_pc, vendedor, fecha, kg,
                            calidad, cuenta, numf)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (k[0], k[1], v.get("vend_pc"), _quien_vendio(v),
                     _fecha(v["fecha"]), parte,
                     v.get("calidad"), cuenta, v.get("numf")), conn=conn)

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
                      ok = TRUE, detalle = %s, nuevas = %s, nuevas_kg = %s,
                      pedidas = %s, pedidas_kg = %s,
                      produciendo = %s, produciendo_kg = %s
                WHERE id = 1""",
            (len(marcado), len(lla),
             f"{len(par)} parados hoy · {len(lla)} candidatos · afuera "
             f"{len(nuevas)} recientes, {len(produciendo)} en producción "
             f"y {len(pedidas)} pedidas",
             len(nuevas), kg_en_bodega(nuevas),
             len(pedidas), kg_en_bodega(pedidas),
             len(produciendo), kg_en_bodega(produciendo)), conn=conn)

    return {"items": len(marcado), "llamados": len(lla), "parados_hoy": len(par),
            "nuevas": len(nuevas), "nuevas_kg": kg_en_bodega(nuevas),
            "pedidas": len(pedidas), "pedidas_kg": kg_en_bodega(pedidas),
            "produciendo": len(produciendo),
            "produciendo_kg": kg_en_bodega(produciendo)}


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


# ⚠ Acá vivía `_meta_pct()`. Se borró el 25/08/2026 con la pantalla de metas:
# desde el 24/08 no hay metas (dueña: "ya que es por puntos, saquemos la meta,
# ideal es + puntos gana"), y la función seguía calculando `meta_pct`,
# `meta_kg` y `meta_pts` para nadie. Lo que sí se usa —y se queda— es
# `kg_base`, los kilos congelados del día de la largada.


def vendido_detalle(desde) -> dict[str, list[dict]]:
    """Qué vendió cada uno, renglón por renglón. `{vendedor: [filas]}`.

    ⭐ Dueña 25/08/2026, mirando el tablero el día de la largada: *"intela ya
    tiene 65kg. quiero ver exacto que vendio"*. El ranking decía el total y la
    fila que se abre lo partía por grupo; qué tela y qué color no estaba en
    ninguna pantalla.

    ⚠ SÓLO lo que puntúa. La primera versión mostraba también los kilos que no
    suman, en gris y con el motivo, para que nadie tuviera que preguntar por qué
    512 kg dieron 0 puntos. Dueña 25/08/2026: *"lo que no cuenta para puntos ni
    lo muestres"* — en una pantalla de competencia, una lista donde la mayoría
    de los renglones no cuenta se lee como un error del programa, no como una
    explicación.

    ⚠ Consecuencia asumida: el que facturó una tela de la lista y no la ve acá
    va a preguntar. La respuesta es la regla de `cuenta_el_kilo` — eran kilos de
    PRIMERA de un ítem que entró sólo por su segunda.
    """
    filas = db.fetch_all(
        """SELECT v.vendedor, v.subcategoria, v.color, v.calidad, v.cuenta,
                  v.fecha, v.numf, SUM(v.kg) AS kg,
                  SUM(v.kg * COALESCE(p.puntos, 1)) AS puntos,
                  -- ⭐ El color con su nombre y la FORMA, para que el detalle
                  -- que se abre tenga las mismas columnas que la lista de
                  -- saldos (dueña 25/08/2026: "color, forma, categoría, kg
                  -- tienen que ser columnas").
                  COALESCE(UPPER(LEFT(nom.n, 1)) || LOWER(SUBSTRING(nom.n FROM 2)), '')
                                                    AS color_nombre,
                  MAX(CASE WHEN COALESCE(f.kg_tubular, 0) > 0 THEN 'TUB'
                           WHEN COALESCE(f.kg_abierta, 0) > 0 THEN 'ABI'
                           ELSE COALESCE(f.forma, '') END) AS forma_fila
             FROM scintela.parado_venta v
             LEFT JOIN scintela.parado_punto p ON p.subcategoria = v.subcategoria
             LEFT JOIN scintela.parado_foto f
                    ON f.subcategoria = v.subcategoria AND f.color = v.color
             LEFT JOIN LATERAL (
                 SELECT SPLIT_PART(tc.color, ' · ', 1) AS n
                   FROM scintela.tinto_costos tc
                  WHERE UPPER(TRIM(tc.cod)) = UPPER(TRIM(v.color))
                    AND COALESCE(TRIM(tc.color), '') <> ''
                  LIMIT 1) nom ON TRUE
            WHERE v.fecha >= %s AND v.cuenta
            GROUP BY v.vendedor, v.subcategoria, v.color, v.calidad, v.cuenta,
                     v.fecha, v.numf, nom.n
            ORDER BY SUM(v.kg) DESC""", (desde,)) or []
    out: dict[str, list[dict]] = defaultdict(list)
    for f in filas:
        f["puntos"] = float(f.get("puntos") or 0) if f.get("cuenta") else 0.0
        out[f.get("vendedor") or "Intela"].append(f)
    return out


def vendidos(desde) -> list[dict]:
    """Lo vendido de la lista, renglón por renglón, para la tabla de Saldos.

    Dueña 25/08/2026: *"abajo de saldos poné una tabla que se llame vendidos, y
    ponés la misma info + día vendido + vendedor"*. La pantalla decía cuántos
    kilos se habían vendido —un número en una tarjeta— pero no qué, cuándo ni
    quién: para verlo había que ir a la Competencia y abrir vendedor por
    vendedor.

    ⚠ SÓLO lo que puntúa, igual que en la Competencia (*"lo que no cuenta ni lo
    muestres"*). Los kilos que quedaron afuera por el tope, o los de primera de
    un ítem que entró por su segunda, no aparecen: en una pantalla de saldos,
    una lista donde la mitad de los renglones no cuenta se lee como un error del
    programa.

    ⚠ El color y su nombre salen como en `items()` —del catálogo de tinto, con
    LIMIT 1— para que las dos tablas escriban el mismo color igual.
    """
    return db.fetch_all(
        """
        SELECT v.subcategoria, v.color, v.calidad, v.fecha,
               v.vendedor, v.vend_pc, v.numf,
               COALESCE(UPPER(LEFT(nom.n, 1)) || LOWER(SUBSTRING(nom.n FROM 2)), '')
                                                          AS color_nombre,
               -- ⚠ El grupo sale de la foto y, si la tela ya no tiene foto
               -- —se vendió entera—, del PUNTAJE, que lo guarda por tela. Sin
               -- el fallback la mayoría de los renglones decía "—": la tabla
               -- muestra justamente lo que se vendió, que es lo que primero
               -- deja de tener foto.
               CASE WHEN COALESCE(f.categoria, p.categoria)
                         IN ('Franela', 'Cuellos', 'Puños')
                    THEN 'FCP' ELSE COALESCE(f.categoria, p.categoria) END
                                                          AS categoria,
               -- ⭐ La FORMA, igual que en la fila de arriba (dueña 25/08/2026:
               -- "agregá forma dentro de la tabla de vendidos").
               --
               -- ⚠ Sale de la ficha de la tela, NO de la venta: la forma vive
               -- en el rollo y la línea de factura no guarda el lote
               -- (`dfc.id_lote` viene en NULL, igual que para la calidad). Así
               -- que es la forma de lo que HAY, no la de lo que salió. Cuando
               -- la tela se vendió entera no queda rollo que mirar y la columna
               -- dice "—": preferible a inventar una sigla.
               MAX(CASE WHEN COALESCE(f.kg_tubular, 0) > 0 THEN 'TUB'
                        WHEN COALESCE(f.kg_abierta, 0) > 0 THEN 'ABI'
                        ELSE COALESCE(f.forma, '') END)    AS forma_fila,
               SUM(v.kg)                                   AS kg,
               -- ⭐ Lo que QUEDA de esa tela × color, para que las dos tablas
               -- de la pantalla se lean juntas: arriba «Kg en saldo | Vendido»
               -- y acá lo mismo, en las mismas dos columnas. Sin esto, la
               -- tabla de abajo no dice si la tela se agotó o si todavía queda
               -- para seguir ofreciendo.
               MAX(COALESCE(f.stock_kg, 0))                AS queda,
               MAX(COALESCE(p.puntos, 1))                  AS puntos,
               SUM(v.kg * COALESCE(p.puntos, 1))           AS puntos_fila
          FROM scintela.parado_venta v
          LEFT JOIN scintela.parado_punto p ON p.subcategoria = v.subcategoria
          LEFT JOIN scintela.parado_foto f
                 ON f.subcategoria = v.subcategoria AND f.color = v.color
          LEFT JOIN LATERAL (
              SELECT SPLIT_PART(tc.color, ' · ', 1) AS n
                FROM scintela.tinto_costos tc
               WHERE UPPER(TRIM(tc.cod)) = UPPER(TRIM(v.color))
                 AND COALESCE(TRIM(tc.color), '') <> ''
               LIMIT 1) nom ON TRUE
         WHERE v.fecha >= %s AND v.cuenta
         GROUP BY v.subcategoria, v.color, v.calidad, v.fecha, v.vendedor,
                  v.vend_pc, v.numf, nom.n, f.categoria, p.categoria
         -- Lo último arriba: es una lista de lo que va pasando, no un ranking.
         ORDER BY v.fecha DESC, SUM(v.kg) DESC
        """, (desde,)) or []


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
            # ⚠ No puede pasar: `_quien_vendio()` manda a Intela todo lo que no
            # firma uno de los siete, y se normaliza al ESCRIBIR `parado_venta`.
            # El `continue` queda como red —un kilo de un desconocido no puede
            # entrar al ranking— pero ya no se cuenta ni se muestra: la pantalla
            # decía "152 kg los vendió alguien que ya no compite" y esa frase
            # sostenía un caso que se arregló. Si volviera a pasar, lo avisa la
            # alarma `competencia_vendedor_ajeno` de /admin/health/competencia.
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
        # ⭐ Cada renglón vendido va DENTRO de su grupo (dueña 25/08/2026:
        # "quería ver el pique especial de lo vendido dentro de pique, no una
        # tabla aparte"). El cuadro decía "Pique · 38 kg" y la tela estaba en
        # otra tabla más abajo: había que buscar a mano de cuál de los ocho
        # grupos salía cada renglón.
        # ⚠ `lineas_por_grupo` y no `por_grupo`: ese nombre ya es una función
        # de este módulo, y asignarlo acá adentro la convierte en variable local
        # para TODA la función — la llamada de más arriba explotaba con
        # UnboundLocalError. Es el segundo shadowing de la tarde.
        lineas = detalle_vendido.get(d["vendedor"], [])
        lineas_por_grupo: dict[str, list] = defaultdict(list)
        for ln in lineas:
            lineas_por_grupo[cat_de.get(ln["subcategoria"], "(sin grupo)")].append(ln)
        for g in d["detalle"]:
            g["lineas"] = lineas_por_grupo.pop(g["grupo"], [])
        # ⚠ Lo que no cae en ninguno de los grupos del cuadro NO se pierde: se
        # muestra como un grupo más. Si no, un renglón vendido desaparecería de
        # la pantalla y los puntos del ranking no cerrarían con lo que se ve.
        for cat, ls in sorted(lineas_por_grupo.items()):
            d["detalle"].append({
                "grupo": cat,
                "kg": sum(float(x["kg"] or 0) for x in ls),
                "puntos": sum(float(x["puntos"] or 0) for x in ls),
                "lineas": ls})
        # ⭐ EL GRUPO EN QUE NO VENDIÓ NADA NO SE MUESTRA (dueña 25/08/2026: "si
        # no vendió nada de las otras telas, no mostrarlas"). Los ocho grupos
        # siempre estaban, así que el que había vendido una sola tela abría su
        # fila y veía un renglón y siete ceros: la pantalla decía siete veces lo
        # mismo —"acá no hizo nada"— y escondía lo único que sí hizo. Los grupos
        # están enteros arriba, en «Los saldos», que es donde se va a buscar
        # dónde hay puntos.
        d["detalle"] = [g for g in d["detalle"]
                        if float(g.get("kg") or 0) or float(g.get("puntos") or 0)]
        d["vendido"] = lineas

    for g in grupos:
        g["liquidado"] = liq_kg.get(g["grupo"], 0.0)
        g["liquidado_pts"] = liq_pts.get(g["grupo"], 0.0)
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
        # ⚠ Se llama `fijada_el` y no `meta_fijada_el`: no es la fecha de una
        # meta —desde el 24/08 no hay— sino la del día en que se congelaron los
        # KILOS contra los que corre la competencia.
        "fijada_el": fijada_el,
        # Los puntos que hay en juego. No es la meta de nadie: es el tamaño
        # de la bolsa, para saber contra qué se lee un 6.700.
        "puntos_en_juego": sum(g["puntos_base"] for g in grupos),
        "liquidado": liquidado,
        "liquidado_pts": sum(g["liquidado_pts"] for g in grupos),
        "puntos_valor": PUNTOS,
        "semanas": semanas["filas"],
        "meses": meses,
        # ⚠ `competidores` se sacó del contexto el 25/08/2026: la pantalla
        # nunca lo usó. La lista sigue viva como constante del módulo.
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

    ⭐ Va por KILOS y no por puntos, al revés del ranking grande. Es a propósito:
    son dos carreras distintas. La del año premia repartir entre tipos de tela;
    la del mes premia sacar kilos, y le da algo para ganar al que viene último.
    Sin eso, el que se descuelga en octubre no vuelve a mirar la pantalla.

    ⚠ NO es "sin tope". Cuenta los mismos kilos que puntúan (`v.cuenta`): los
    que quedaron afuera por el tope no son saldo destrabado ni acá ni allá. La
    frase "sin tope" venía del TOPE POR GRUPO —la regla del 17/08 que se sacó el
    24— y quedó describiendo otra cosa.

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
        """El mes de la largada cuenta junto con el siguiente.

        ⚠ `largada.month + 1` reventaba con una largada en DICIEMBRE: `date(a,
        13, 1)` tira ValueError y se cae la pantalla entera. Hoy no pasa —la
        largada es el 25/08— pero es una trampa puesta para el próximo que
        corra una competencia, y cuesta dos líneas no dejarla."""
        if m != primer_mes:
            return m
        return (date(largada.year + 1, 1, 1) if largada.month == 12
                else date(largada.year, largada.month + 1, 1))

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
