"""Las 5 sugerencias del día para la competencia, arriba del Inicio.

Dueña 28/09/2026, con la competencia frenada (del 16/09 al 28/09 salieron
~800 kg, contra ~500 kg POR DÍA las dos primeras semanas):

    *"veamos que vendio cada uno en estos dias y ofrezcamos parecido para
    clientes que ya les vendieron… un anuncio 5 sugerencias para la
    competencia. esta tela en estos colores a x persona. y ya manda el
    mensaje por whatsapp"*

Y en la vuelta siguiente: *"queremos sacar cantidad no puntos"* · *"que
cambie diario"* · *"agrega para trackear en uso de la app esto"*.

La regla (corrida contra la data en vivo el 28/09/2026 antes de escribirla)
--------------------------------------------------------------------------
1. **Telas**: las que ESE vendedor vendió en la competencia (`parado_venta`
   que cuenta). Si con eso no llega a 5 — RMY y JQU vendieron poco y les
   salían 2 y 3 —, se completa con las otras telas de la lista que sus
   clientes compran.
2. **Clientes**: los de SU cartera (`cliente.vend`, el mismo criterio que
   todo /mi-cartera) que compraron esa tela: en la competencia o en el
   último año (`parado_llamado`).
3. **Colores**: hasta 3 de esa tela que están en saldo hoy (≥ 15 kg) y que
   ese cliente todavía no se llevó en la carrera. Los de más kilos primero.
4. **Orden**: por KILOS, no por puntos — lo que ese cliente suele llevar de
   esa tela (lo que compró en la carrera, o dos meses de lo que compró en el
   año), topeado por lo que hay.
5. Un cliente aparece una sola vez, y una tela como mucho dos veces.

Cambia una vez por día
----------------------
La primera vez que se abre el Inicio en el día se calculan y se GUARDAN
(`scintela.sugerencia_dia`). Durante el día no se mueven: las cinco de las 8
son las de las 18, y así se puede medir qué pasó con cada una. Es la única
escritura que hace el Inicio, y es best-effort: si falla, la tarjeta no
aparece y el resto de la pantalla sigue igual.

El mensaje
----------
Habla de usted y NO dice cuál color es de segunda (dueña 28/09/2026: *"no
mostremos segunda y habla con usted"*). La tarjeta sí se lo marca al
vendedor. El botón abre el WhatsApp del VENDEDOR con el texto y el número
del cliente ya cargados: lo manda él, desde su número, que es el que el
cliente conoce.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from urllib.parse import quote

import db

from .queries import _ES_MI_CLIENTE

_LOG = logging.getLogger("programa_core.mi_cartera.sugerencias")

CUANTAS = 5
COLORES_POR_SUGERENCIA = 3
#: Un color con menos de esto no se ofrece: no arma un pedido.
KG_MINIMO_COLOR = 15.0
#: "Lo que suele llevar": de lo que compró en el año, dos meses.
MESES_DE_COMPRA = 2
#: Piso de lo que puede llevar, para que un cliente chico no quede en cero.
KG_PISO = 10.0
MAX_POR_TELA = 2
#: Un cliente que salió no vuelve a salir por estos días (si hay otros).
DIAS_SIN_REPETIR = 3

_EMPRESA = re.compile(r"\b(S\.?A\.?S?|C(I|Í)A\.?|LTDA\.?|COMERCIAL\w*|EMPRESA|TEXTIL\w*)\b",
                      re.I)


# ── La regla, sin base de datos (para poder probarla sola) ───────────────────

def elegir(saldo: dict[str, list[dict]],
           compras: list[dict],
           llamados: list[dict],
           telas_que_vendio: set[str],
           cuantas: int = CUANTAS,
           recientes: dict[str, int] | None = None) -> list[dict]:
    """Las `cuantas` sugerencias de un vendedor.

    - `saldo`: tela → [{color, nombre, kg, segunda}] de lo que hay hoy.
    - `compras`: [{codigo_cli, tela, colores:set, kg}] lo que SUS clientes
      compraron en la competencia.
    - `llamados`: [{codigo_cli, tela, kg}] lo que SUS clientes compraron de
      cada tela en el último año.
    - `telas_que_vendio`: las telas que el vendedor vendió en la carrera.
    - `recientes`: cliente → hace cuántos días le salió por última vez. El
      que salió en los últimos `DIAS_SIN_REPETIR` días va al final, y
      entre ésos primero el que salió hace más.
    """
    pares: dict[tuple[str, str], dict] = {}
    for ll in llamados:
        k = (ll["codigo_cli"], ll["tela"])
        p = pares.setdefault(k, {"codigo_cli": k[0], "tela": k[1], "anio": 0.0,
                                 "compro": 0.0, "ya": set()})
        p["anio"] = max(p["anio"], float(ll.get("kg") or 0))
    for c in compras:
        k = (c["codigo_cli"], c["tela"])
        p = pares.setdefault(k, {"codigo_cli": k[0], "tela": k[1], "anio": 0.0,
                                 "compro": 0.0, "ya": set()})
        p["compro"] += float(c.get("kg") or 0)
        p["ya"] |= set(c.get("colores") or ())

    candidatas = []
    for p in pares.values():
        colores = sorted(
            (s for s in saldo.get(p["tela"], [])
             if s["color"] not in p["ya"] and float(s["kg"]) >= KG_MINIMO_COLOR),
            key=lambda s: -float(s["kg"]),
        )[:COLORES_POR_SUGERENCIA]
        if not colores:
            continue
        hay = sum(float(s["kg"]) for s in colores)
        suele = max(p["compro"], p["anio"] * MESES_DE_COMPRA / 12, KG_PISO)
        candidatas.append({
            "codigo_cli": p["codigo_cli"],
            "tela": p["tela"],
            "colores": [{"color": s["color"], "nombre": s.get("nombre") or "",
                         "kg": round(float(s["kg"]), 1),
                         "segunda": bool(s.get("segunda"))} for s in colores],
            "kg_posible": round(min(hay, suele), 1),
            "vendio": p["tela"] in telas_que_vendio,
        })

    # ⭐ QUE CAMBIE CADA DÍA (dueña 29/09/2026, "dale"). El 29/09 las 5 de
    # SEP eran exactamente las del 28: sin ventas en el medio, la regla
    # elegía lo mismo — cambiaba la fecha, no el contenido. El cliente que
    # salió en los últimos días pasa al final de la fila; si no alcanzan los
    # nuevos, vuelven primero los que salieron hace más tiempo.
    recientes = {k.upper().strip(): v for k, v in (recientes or {}).items()}

    def _reciente(c: dict) -> int:
        dias = recientes.get(c["codigo_cli"].upper().strip())
        return 0 if dias is None or dias > DIAS_SIN_REPETIR else DIAS_SIN_REPETIR + 1 - dias

    elegidas: list[dict] = []
    usados: set[str] = set()
    por_tela: dict[str, int] = {}
    # Primero los clientes que no salieron hace poco; dentro de eso, primero
    # las telas que él vendió y recién después el relleno.
    for vuelta in ((0, True), (0, False), (1, True), (1, False)):
        for c in sorted((c for c in candidatas
                         if (_reciente(c) > 0) == bool(vuelta[0]) and c["vendio"] is vuelta[1]),
                        key=lambda c: (_reciente(c), -c["kg_posible"], c["codigo_cli"], c["tela"])):
            if len(elegidas) >= cuantas:
                break
            if c["codigo_cli"] in usados or por_tela.get(c["tela"], 0) >= MAX_POR_TELA:
                continue
            elegidas.append(c)
            usados.add(c["codigo_cli"])
            por_tela[c["tela"]] = por_tela.get(c["tela"], 0) + 1
    return sorted(elegidas, key=lambda c: -c["kg_posible"])


def celular(*telefonos: str | None) -> str | None:
    """El primer celular ecuatoriano (09XXXXXXXX) en formato de WhatsApp
    (5939XXXXXXXX). Un fijo no sirve para WhatsApp: si no hay celular, None."""
    for t in telefonos:
        limpio = re.sub(r"[^0-9+ ]", " ", t or "")
        for pedazo in limpio.split():
            d = pedazo.lstrip("+")
            if d.startswith("593") and len(d) == 12 and d[3] == "9":
                return d
            if re.fullmatch(r"09\d{8}", d):
                return "593" + d[1:]
            # "+593 987654321": el código de país vino separado.
            if re.fullmatch(r"9\d{8}", d):
                return "593" + d
    return None


def nombre_de_pila(nombre: str | None) -> str:
    """"ANDRADE ZAMBRANO IDA ISABEL" → "Ida". Las fichas vienen apellidos
    primero. A una empresa, o a un nombre que no se puede partir con
    seguridad, no se lo saluda por el nombre: mejor "Hola" solo que "Hola
    Zambrano"."""
    n = (nombre or "").strip()
    partes = n.split()
    if len(partes) < 3 or _EMPRESA.search(n):
        return ""
    return partes[2].capitalize()


def _enumerar(cosas: list[str]) -> str:
    if len(cosas) <= 1:
        return "".join(cosas)
    return ", ".join(cosas[:-1]) + " y " + cosas[-1]


def mensaje(sug: dict) -> str:
    """El texto que se abre en WhatsApp. De usted, y sin decir cuál es de
    segunda."""
    pila = nombre_de_pila(sug.get("nombre"))
    colores = _enumerar([c["color"] for c in sug["colores"]])
    return (f"Hola{' ' + pila if pila else ''}, ¿cómo está? Le cuento que "
            f"tenemos {sug['tela']} en {colores}, lista para entregar. "
            f"¿Le separo?")


def link_whatsapp(sug: dict) -> str | None:
    if not sug.get("whatsapp"):
        return None
    return f"https://wa.me/{sug['whatsapp']}?text={quote(mensaje(sug))}"


# ── Lectura de la base ───────────────────────────────────────────────────────

def _saldo() -> dict[str, list[dict]]:
    """Lo que hay hoy, tela por tela — la MISMA lista que Saldos."""
    from modules.analisis import queries as aq

    out: dict[str, list[dict]] = {}
    for f in aq.items():
        kg = float(f.get("stock_kg") or 0)
        if kg <= 0:
            continue
        out.setdefault(f["subcategoria"], []).append({
            "color": f["color"], "nombre": f.get("color_nombre") or "",
            "kg": kg, "segunda": (f.get("motivo") == "segunda"),
        })
    return out


def _compras(vend: str) -> list[dict]:
    filas = db.fetch_all(
        f"""
        SELECT c.codigo_cli, v.subcategoria AS tela,
               ARRAY_AGG(DISTINCT v.color) AS colores, SUM(v.kg) AS kg
          FROM scintela.parado_venta v
          JOIN scintela.factura f ON f.numf = v.numf AND f.fecha = v.fecha
          JOIN scintela.cliente c
            ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(f.codigo_cli))
         WHERE v.cuenta AND v.kg > 0 AND {_ES_MI_CLIENTE}
         GROUP BY c.codigo_cli, v.subcategoria
        """, {"vend": vend}) or []
    return [{**f, "colores": set(f["colores"] or [])} for f in filas]


def _telas_que_vendio(vend: str) -> set[str]:
    filas = db.fetch_all(
        """SELECT DISTINCT subcategoria FROM scintela.parado_venta
            WHERE cuenta AND kg > 0 AND UPPER(TRIM(vend_pc)) = UPPER(TRIM(%(vend)s))""",
        {"vend": vend}) or []
    return {f["subcategoria"] for f in filas}


def _llamados(vend: str, hoy: date) -> list[dict]:
    filas = db.fetch_all(
        f"""
        SELECT c.codigo_cli, l.subcategoria AS tela, l.kg
          FROM scintela.parado_llamado l
          JOIN scintela.cliente c
            ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(l.codigo_cli))
         WHERE {_ES_MI_CLIENTE} AND l.anio >= %(desde)s
           AND UPPER(TRIM(l.codigo_cli)) <> 'VPM'
        """, {"vend": vend, "desde": hoy.year - 1}) or []
    return filas


def _fichas(codigos: list[str]) -> dict[str, dict]:
    """Nombre y celular de cada cliente. El celular que el cliente cargó en
    el portal gana sobre el teléfono viejo de la ficha."""
    if not codigos:
        return {}
    filas = db.fetch_all(
        """
        SELECT UPPER(TRIM(c.codigo_cli)) AS codigo_cli, c.nombre, c.telefono,
               p.celular
          FROM scintela.cliente c
          LEFT JOIN scintela.portal_datos_cliente p
                 ON UPPER(TRIM(p.codigo_cli)) = UPPER(TRIM(c.codigo_cli))
         WHERE UPPER(TRIM(c.codigo_cli)) = ANY(%(cods)s)
        """, {"cods": [c.upper().strip() for c in codigos]}) or []
    return {f["codigo_cli"]: f for f in filas}


def _recientes(vend: str, hoy: date) -> dict[str, int]:
    """Cliente → hace cuántos días le salió a este vendedor por última vez."""
    filas = db.fetch_all(
        """SELECT UPPER(TRIM(codigo_cli)) AS codigo_cli, MAX(fecha) AS ultima
             FROM scintela.sugerencia_dia
            WHERE vend = %(v)s AND fecha < %(h)s AND fecha >= %(h)s - %(n)s
            GROUP BY 1""", {"v": vend, "h": hoy, "n": DIAS_SIN_REPETIR}) or []
    return {f["codigo_cli"]: (hoy - f["ultima"]).days for f in filas}


def calcular(vend: str, hoy: date) -> list[dict]:
    elegidas = elegir(_saldo(), _compras(vend), _llamados(vend, hoy),
                      _telas_que_vendio(vend), recientes=_recientes(vend, hoy))
    fichas = _fichas([e["codigo_cli"] for e in elegidas])
    for e in elegidas:
        f = fichas.get(e["codigo_cli"].upper().strip(), {})
        e["nombre"] = f.get("nombre") or ""
        e["whatsapp"] = celular(f.get("celular"), f.get("telefono"))
    return elegidas


def _leer(vend: str, hoy: date) -> list[dict]:
    filas = db.fetch_all(
        """SELECT * FROM scintela.sugerencia_dia
            WHERE fecha = %(f)s AND vend = %(v)s ORDER BY orden""",
        {"f": hoy, "v": vend}) or []
    for f in filas:
        if isinstance(f["colores"], str):
            f["colores"] = json.loads(f["colores"])
        f["kg_posible"] = float(f["kg_posible"])
    return filas


def del_dia(vend: str, hoy: date) -> list[dict]:
    """Las de hoy: si ya se armaron, esas; si no, se arman y se guardan.

    Best-effort: cualquier error devuelve [] y la tarjeta no aparece.
    """
    vend = (vend or "").upper().strip()
    if not vend:
        return []
    try:
        filas = _leer(vend, hoy)
        if filas:
            return _con_links(filas)
        for i, s in enumerate(calcular(vend, hoy), start=1):
            # ON CONFLICT: si dos pestañas abren el Inicio a la vez, gana la
            # primera y la otra lee lo mismo.
            db.execute(
                """
                INSERT INTO scintela.sugerencia_dia
                    (fecha, vend, orden, codigo_cli, nombre, tela, colores,
                     kg_posible, whatsapp)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (fecha, vend, orden) DO NOTHING
                """,
                (hoy, vend, i, s["codigo_cli"], (s["nombre"] or "")[:200],
                 s["tela"], json.dumps(s["colores"], ensure_ascii=False),
                 s["kg_posible"], s["whatsapp"]))
        return _con_links(_leer(vend, hoy))
    except Exception:  # noqa: BLE001 — la tarjeta nunca tumba el Inicio
        _LOG.warning("no pude armar las sugerencias de %s", vend, exc_info=True)
        return []


def _con_links(filas: list[dict]) -> list[dict]:
    for f in filas:
        f["link"] = link_whatsapp(f)
    return filas


def una(vend: str, fecha: date, orden: int) -> dict | None:
    filas = [f for f in _leer(vend, fecha) if int(f["orden"]) == int(orden)]
    return _con_links(filas)[0] if filas else None


def anotar_whatsapp(vend: str, fecha: date, orden: int) -> None:
    db.execute(
        """UPDATE scintela.sugerencia_dia
              SET whatsapp_veces = whatsapp_veces + 1,
                  whatsapp_primero = COALESCE(whatsapp_primero, now())
            WHERE fecha = %s AND vend = %s AND orden = %s""",
        (fecha, vend, orden))


def pendientes(vend: str, hoy: date) -> int:
    """La CAMPANITA (dueña 28/09/2026: *"poneles una campanita"*).

    El primer día, de seis vendedores sólo dos abrieron el Inicio: los otros
    entraron directo a Clientes o a Pedidos y nunca vieron la tarjeta. La
    campanita va en TODAS sus pantallas —Mi Cartera y Competencia— y dice
    cuántas sugerencias hay hoy, hasta que abre el Inicio ese día (abrirlo es
    verlas: la tarjeta está arriba de todo).

    "Abrió el Inicio" sale de `uso_pantalla`, lo mismo que mide «Uso de la
    app». Best-effort: si algo falla, 0 y no hay campanita.
    """
    vend = (vend or "").upper().strip()
    if not vend:
        return 0
    try:
        hay = del_dia(vend, hoy)
        if not hay:
            return 0
        # ⚠ Abrir el Inicio DESPUÉS de que se armaron: el 28/09 FL1, RMY y
        # EDG lo habían abierto a la mañana, antes de que existieran, y con
        # "lo abrió hoy" a secas la campanita no les salía nunca.
        vio = db.fetch_one(
            """SELECT 1 AS si FROM scintela.uso_pantalla u
                WHERE u.vend = %(v)s AND u.pantalla = 'mi_cartera.inicio'
                  AND u.ts >= (SELECT MIN(s.creado_en)
                                 FROM scintela.sugerencia_dia s
                                WHERE s.fecha = %(h)s AND s.vend = %(v)s)
                LIMIT 1""", {"v": vend, "h": hoy})
        return 0 if vio else len(hay)
    except Exception:  # noqa: BLE001
        _LOG.debug("campanita de %s", vend, exc_info=True)
        return 0
