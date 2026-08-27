"""La columna Desde de /pedidos ordena por la FECHA, no por el día del mes.

Dueña 26/08/2026, sobre /pedidos: *"el desde acá ordena mal. Poné las fechas y
se va a ordenar bien"*.

Eran dos bugs encima del mismo click, los dos verificados en vivo antes de
tocar nada:

1. **El mes no contaba.** La celda dice "30 jul" y el sorteador
   (`static/sortable-tables.js`) adivina el tipo de la columna leyendo el texto:
   `parseDate` no entiende "30 jul", pero `parseNumber` sí saca un 30 de ahí.
   La columna se ordenaba como NÚMEROS y salía 3, 4, 18, 30 — julio detrás de
   agosto. La fecha entera va ahora en `data-sort-value`, en ISO, que es el
   formato que `parseDate` lee.

2. **Las telas se despegaban de su color.** En el corte por color cada color es
   una fila madre y sus telas y pedidos son filas HERMANAS. Al ordenar viajaban
   sueltas: medido en vivo, después de un click las telas quedaban colgadas de
   otro color. Ahora llevan `data-fila-hija` y viajan pegadas (la misma regla
   que arregló el "+" de /importaciones el 20/08).

Y de paso, los kilos: la celda de un color mixto dice "11 roll · 2.000 un" y de
ese texto salía un 11 — ordenaba por los rollos y se comía las unidades. El kilo
es la única escala en la que las dos unidades se comparan.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.pedidos import service

RAIZ = Path(__file__).resolve().parent.parent
JS = RAIZ / "static" / "sortable-tables.js"


def _fila(**kw):
    """Fila cruda de `_SQL_PENDIENTES`, con la forma que escribe el SQL."""
    base = {
        "categoria": "Fleece", "tela": "Fleece 96 Perchado",
        "codigo": "FE96CAF", "color": "CAF",
        "ped_kg": 447.0, "ped_rollos": 19.0, "ped_un": 0.0, "un_por_kg": None,
        "n_pedidos": 3, "n_clientes": 2, "mas_viejo": "2026-08-05",
        "inv_kg": 0.0, "prod_kg": 0.0, "n_ordenes": 0,
    }
    base.update(kw)
    return base


#: Tres colores cuyo DÍA del mes contradice a la fecha: por número sería
#: 4 · 18 · 30, y por fecha es 30 jul · 4 ago · 18 ago. Si alguien vuelve a
#: ordenar por el texto de la celda, estos tres lo cantan.
FILAS = [
    _fila(codigo="FE96CAF", color="CAF", mas_viejo="2026-07-30"),
    _fila(codigo="FE96NEG", color="NEG", mas_viejo="2026-08-04", ped_kg=235.0,
          ped_rollos=10.0),
    _fila(codigo="FE96AZS", color="AZS", mas_viejo="2026-08-18", ped_kg=100.0,
          ped_rollos=4.0),
]


def _login(app, fake_db):
    rid = fake_db.add_role("Tester", ["facturas.ver", "stock.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


@pytest.fixture(autouse=True)
def _sin_cache():
    service.reset_cache()
    yield
    service.reset_cache()


def _pantalla(app, fake_db, corte="color"):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(FILAS, True)):
        r = c.get(f"/pedidos?corte={corte}")
    assert r.status_code == 200
    return r.get_data(as_text=True)


class _Tabla(HTMLParser):
    """Las filas de la pantalla, con los atributos de cada celda.

    Se parsea el HTML en vez de buscar substrings: un test que busca
    `data-sort-value="2026-07-30"` pasa aunque el atributo haya quedado en la
    celda equivocada, que es exactamente el bug que se está arreglando.
    """

    def __init__(self):
        super().__init__()
        self.filas: list[dict] = []
        self._fila: dict | None = None
        self._celda: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._fila = {"clase": a.get("class", ""),
                          "hija": "data-fila-hija" in a, "celdas": []}
            self.filas.append(self._fila)
        elif tag == "td" and self._fila is not None:
            self._celda = {"orden": a.get("data-sort-value"), "texto": ""}
            self._fila["celdas"].append(self._celda)

    def handle_data(self, data):
        if self._celda is not None:
            self._celda["texto"] += data

    def handle_endtag(self, tag):
        if tag == "td":
            self._celda = None
        elif tag == "tr":
            self._fila = None


def _filas(html: str) -> list[dict]:
    p = _Tabla()
    p.feed(html)
    return p.filas


def _madres(html: str) -> list[dict]:
    return [f for f in _filas(html) if "ghead" in f["clase"]]


# ── 1. la fecha entera viaja en la celda ────────────────────────────────────

def test_la_celda_desde_lleva_la_fecha_entera_y_no_solo_el_dia(app, fake_db):
    html = _pantalla(app, fake_db)
    desde = [(f["celdas"][1]["orden"], f["celdas"][1]["texto"].strip())
             for f in _madres(html)]
    assert sorted(desde) == [
        ("2026-07-30", "30 jul"),
        ("2026-08-04", "4 ago"),
        ("2026-08-18", "18 ago"),
    ]


def test_en_el_corte_por_tela_la_fecha_tambien_va_entera(app, fake_db):
    html = _pantalla(app, fake_db, corte="tela")
    vistas = {c["orden"] for f in _filas(html) if not f["hija"]
              for c in f["celdas"] if (c["orden"] or "").startswith("2026-")}
    assert vistas == {"2026-07-30", "2026-08-04", "2026-08-18"}


# ── 2. las telas viajan con su color ────────────────────────────────────────

def test_las_telas_y_los_pedidos_viajan_pegados_a_su_color(app, fake_db):
    """Sin esto, un click en cualquier título deja la tela colgada de otro
    color: la fila madre se va a su lugar nuevo y la hermana se queda."""
    html = _pantalla(app, fake_db)
    sueltas = [f["clase"] for f in _filas(html)
               if "vr" in f["clase"] and not f["hija"]]
    assert sueltas == [], f"filas de tela sin data-fila-hija: {sueltas}"
    assert len(_madres(html)) == 3


def test_en_el_corte_por_tela_el_detalle_tambien_va_pegado(app, fake_db):
    html = _pantalla(app, fake_db, corte="tela")
    sueltas = [f["clase"] for f in _filas(html)
               if "det" in f["clase"] and not f["hija"]]
    assert sueltas == []


# ── 3. los kilos, para que rollos y unidades se comparen ────────────────────

def test_las_columnas_de_cantidad_se_ordenan_en_kilos(app, fake_db):
    """"11 roll · 2.000 un" no es un número: de ese texto salía un 11."""
    html = _pantalla(app, fake_db)
    pedidos = sorted(float(f["celdas"][2]["orden"]) for f in _madres(html))
    assert pedidos == [100.0, 235.0, 447.0]
    for f in _madres(html):
        for i in (3, 4, 5):
            assert f["celdas"][i]["orden"] is not None
            float(f["celdas"][i]["orden"])   # numérico o revienta


def test_el_faltante_del_color_no_mezcla_rollos_con_unidades(app, fake_db):
    """El valor de orden es el kilo, que es lo único sumable entre unidades."""
    html = _pantalla(app, fake_db)
    faltan = {f["celdas"][0]["texto"].split()[0]: float(f["celdas"][5]["orden"])
              for f in _madres(html)}
    assert faltan == {"CAF": 447.0, "NEG": 235.0, "AZS": 100.0}


# ── 4. el sorteador de verdad, con las dos formas del dato ──────────────────

_SCRIPT = """
global.document = {
  readyState: 'complete', addEventListener: () => {},
  querySelectorAll: () => [], createElement: () => ({}),
};
const m = require(process.argv[1]);
const texto = ['30 jul', '4 ago', '18 ago'];
const iso   = ['2026-07-30', '2026-08-04', '2026-08-18'];
const orden = (vals, tipo) => vals.slice()
    .map((v) => [v, tipo === 'date' ? m.parseDate(v) : m.parseNumber(v)])
    .sort((a, b) => m.comparadorFilas(tipo, 'asc')(a[1], b[1]))
    .map((x) => x[0]);
console.log(JSON.stringify({
  tipo_texto: texto.map((v) => (isNaN(m.parseDate(v)) ? 'number' : 'date')),
  por_texto: orden(texto, 'number'),
  por_iso: orden(iso, 'date'),
}));
"""


def _node() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node no disponible en este entorno")
    out = subprocess.run([node, "-e", _SCRIPT, str(JS)],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_el_texto_de_la_celda_se_ordena_como_numero_y_por_eso_mentia():
    """La prueba de que el arreglo tenía que ser el atributo y no otra cosa."""
    r = _node()
    assert r["tipo_texto"] == ["number"] * 3      # "30 jul" no es una fecha
    assert r["por_texto"] == ["4 ago", "18 ago", "30 jul"]   # julio al final


def test_con_la_fecha_iso_el_orden_es_el_del_calendario():
    assert _node()["por_iso"] == ["2026-07-30", "2026-08-04", "2026-08-18"]


# ── 5. ninguna celda de fecha puede quedarse sin su valor de orden ─────────

#: "30 jul", "4 ago" — la forma en que la pantalla escribe una fecha.
_FECHA_CORTA = re.compile(
    r"^\d{1,2} (ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)$")


@pytest.mark.parametrize("corte", ["color", "tela"])
def test_toda_celda_con_una_fecha_lleva_su_valor_de_orden(app, fake_db, corte):
    """La red para la fecha que se agregue mañana en otra columna.

    Se mira el HTML RENDERIZADO y no la plantilla: en la plantilla el `class`
    de estas celdas trae un `>` adentro del `{% if %}` y cualquier regex se
    corta ahí — pasaría en verde sin haber mirado nada.
    """
    html = _pantalla(app, fake_db, corte=corte)
    celdas = [c for f in _filas(html) for c in f["celdas"]
              if _FECHA_CORTA.match(c["texto"].strip())]
    assert celdas, "la pantalla no dibujó ninguna fecha: el test no probó nada"
    sin_orden = [c["texto"].strip() for c in celdas if not c["orden"]]
    assert sin_orden == [], f"fechas sin data-sort-value: {sin_orden}"
