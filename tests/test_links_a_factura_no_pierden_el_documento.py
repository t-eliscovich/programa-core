"""Ningún link a una factura puede perder de vista QUÉ documento es.

Dueña 26/08/2026: *"que hayamos puesto 5 números en vez de todo el número no
tiene que perder de vista si es nota de entrega, devolución, factura"*.

Medido ese día sobre las 36.569 facturas de producción:

  · `numf` (el número corto, el que va en la URL) **NO identifica**: hay 2.064
    números repetidos entre 4.416 documentos —el 12%—, sobre todo notas de
    entrega (2.134) y notas de crédito (1.238), que llevan numeración propia y
    chocan con las facturas viejas. Y en **288 documentos vale CERO**.
  · `numf_completo` (`NTEN-10919`, `001-099-000182741`) **no se repite NUNCA**:
    cero duplicados. Ése es el identificador.
  · Quedan **1.833 documentos sin `numf_completo`**. Para ésos el único
    identificador es la PK interna — y pasarla sola tampoco alcanza, porque el
    id de una factura coincide con el numf de OTRA en **6.426 casos**.

De ahí las dos puertas sin ambigüedad de `/facturas/<numf>`: `?doc=` (el número
completo) y `?id=` (la PK interna, explícita). Este test vigila que todo link
use una de las dos — para que el próximo que se escriba no pueda olvidarse.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RAIZ = Path(__file__).resolve().parents[1]

#: Dónde empieza un `url_for('facturas.detalle', …)`.
_INICIO = re.compile(r"url_for(\()\s*['\"]facturas\.detalle['\"]")


def _plantillas():
    for carpeta in ("modules", "templates"):
        yield from (RAIZ / carpeta).rglob("*.html")


def _links(texto: str):
    """Cada `url_for` ENTERO, contando paréntesis.

    ⚠ Con un `[^)]*\)` a secas, `id_factura=(f.numf or f.id_factura)` cortaba
    en el primer paréntesis y el `doc=` que venía después no se veía: el test
    marcaba como rotos links que estaban bien.
    """
    for m in _INICIO.finditer(texto):
        i, nivel = m.start(1), 0   # el paréntesis de `url_for`, no el de adentro
        for j in range(i, len(texto)):
            if texto[j] == "(":
                nivel += 1
            elif texto[j] == ")":
                nivel -= 1
                if nivel == 0:
                    yield " ".join(texto[m.start():j + 1].split())
                    break


def test_todo_link_a_una_factura_dice_cual_es():
    sin_desempate = []
    for f in _plantillas():
        for link in _links(f.read_text(encoding="utf-8")):
            if "doc=" not in link and "id=" not in link.replace("id_factura=", ""):
                sin_desempate.append(f"{f.relative_to(RAIZ)}: {link[:110]}")
    assert not sin_desempate, (
        "estos links mandan sólo el número corto, que no identifica el "
        "documento — tienen que llevar `doc=` (el número completo) o `id=` "
        "(la PK interna):\n  " + "\n  ".join(sin_desempate))


def test_los_links_escritos_a_mano_tambien():
    """Los que arman la URL con texto, sin `url_for`."""
    crudo = re.compile(r'href="/facturas/\{\{[^"]*"', re.S)
    sin = []
    for f in _plantillas():
        for m in crudo.finditer(f.read_text(encoding="utf-8")):
            link = " ".join(m.group(0).split())
            if "doc=" not in link:
                sin.append(f"{f.relative_to(RAIZ)}: {link[:110]}")
    assert not sin, "\n  ".join(sin)


def test_la_ruta_tiene_las_dos_puertas():
    import inspect

    from modules.facturas import views
    fuente = inspect.getsource(views.detalle)
    assert "por_numf_completo(doc)" in fuente, "falta la puerta del número completo"
    assert "por_id_interno" in fuente, "falta la puerta de la PK interna"
    i_doc, i_id = fuente.index("por_numf_completo"), fuente.index("por_id_interno")
    i_amb = fuente.index("las_del_mismo_numero")
    assert i_doc < i_amb and i_id < i_amb, (
        "las puertas sin ambigüedad tienen que probarse ANTES de resolver por "
        "el número corto")


def test_el_vendedor_y_el_cliente_buscan_por_el_numero_completo():
    """Las dos fichas comparten el parcial, y las dos rutas tienen que
    desempatar igual: si no, el vendedor y el cliente miran documentos
    distintos discutiendo por teléfono."""
    import inspect

    from modules.mi_cartera import views as mc
    from modules.portal import views as po
    for fuente, quien in ((inspect.getsource(mc._factura_de), "el vendedor"),
                          (inspect.getsource(po.factura), "el cliente")):
        assert "numf_completo" in fuente, f"{quien} busca sólo por el número corto"


def test_el_parcial_compartido_manda_el_numero_completo():
    t = (RAIZ / "modules" / "mi_cartera" / "templates" / "mi_cartera" /
         "_movimientos.html").read_text(encoding="utf-8")
    i = t.index("url_for(factura_endpoint")
    assert "doc=f.numf_completo" in t[i:i + 200], (
        "el parcial que usan el vendedor y el cliente no manda el número "
        "completo")


def test_si_el_numero_completo_puede_faltar_va_el_id_tambien():
    """`doc=f.numf_completo or None` no alcanza solo.

    Dueña 04/09/2026, sobre la NC 10970 de AJ2 en /facturas: *"la de abajo ni
    abre"*. Esa NC no tiene `numf_completo`, así que el `doc=` se evaporaba y el
    link quedaba `/facturas/10970` pelado — que da para dos (la factura de BED
    de ese día) — y la ficha la mandaba de vuelta a la MISMA lista. Un loop.
    Cuando el número completo puede faltar, el `id=` tiene que ir igual.
    """
    sin_id = []
    for f in _plantillas():
        for link in _links(f.read_text(encoding="utf-8")):
            puede_faltar = re.search(r"doc=[^,)]*\bor None", link)
            if puede_faltar and "id=" not in link.replace("id_factura=", ""):
                sin_id.append(f"{f.relative_to(RAIZ)}: {link[:110]}")
    assert not sin_id, (
        "estos links pierden el `doc=` cuando la factura no tiene número "
        "completo y quedan ambiguos — falta el `id=`:\n  " + "\n  ".join(sin_id))


def test_los_redirects_del_codigo_tambien_desempatan():
    """Los `url_for("facturas.detalle", …)` de las vistas Python (después de
    editar, anular, generar el XML del SRI) también tienen que llevar `id=` o
    `doc=`: el 04/09/2026 los del SRI mandaban la PK interna PELADA, y la ficha
    prefiere el numf — o sea que podían abrir OTRA factura."""
    sin = []
    for f in (RAIZ / "modules").rglob("*.py"):
        texto = f.read_text(encoding="utf-8")
        for link in _links(texto):
            if "**_extra" in link:
                continue  # el redirect canónico de la ficha: ya lleva doc o id
            if "doc=" not in link and "id=" not in link.replace("id_factura=", ""):
                sin.append(f"{f.relative_to(RAIZ)}: {link[:110]}")
    assert not sin, "\n  ".join(sin)


def test_el_historial_manda_el_id():
    """El Historial SABE el id interno del movimiento; la URL lo lleva."""
    from modules.historial import queries as hq

    url, _ = hq.link_origen({"origen_table": "factura", "origen_id": 32132},
                            factura_numfs={32132: 10970})
    assert url == "/facturas/10970?id=32132"


def test_recientes_guarda_la_pk_real_de_la_factura():
    """El menú Recientes linkea con `?id=` (la PK interna), así que lo que se
    guarda tiene que ser la PK — no el número de la URL, que es el numf. Con
    el numf ahí, `por_id_interno(10970)` abría otra factura. 04/09/2026."""
    import inspect

    from modules.facturas import views
    fuente = inspect.getsource(views.detalle)
    i = fuente.index("rec.registrar(")
    assert '"factura", _id_real' in fuente[i:i + 80], (
        "Recientes tiene que guardar la PK (_id_real), no id_factura de la URL")
