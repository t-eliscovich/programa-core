"""Los pendientes HISTÓRICOS del tab Impuestos también se pueden agrupar.

Historia (17/08/2026). Alex le dijo a Tamara: *"con negativos sí funciona y
con positivos no"*. El signo era una coincidencia. El tab Impuestos muestra
JUNTAS dos poblaciones — los renglones del extracto que se subió en esta
sesión y los pendientes históricos que se arrastran de sesiones anteriores
(`estado_sesion` los mezcla en el bucket `impuestos`) — pero el POST resolvía
las firmas SÓLO contra `cargar_movs(sesion)`, o sea el extracto. Cualquier
histórico tildado rebotaba con *"ya no están como pendientes (alguien los
concilió mientras tenías la pantalla abierta)"*, que además contaba una
historia falsa: nadie los había conciliado, nunca se habían buscado.

Los cuatro que no entraban (2 × "REV IVA+COMIS" y 2 × "IVA CAUSADO SERVICIO",
$5,72 en total) eran del 17/06 y del 12/08, fuera de la ventana del extracto
14/08→17/08. Todos daban `value="-1"` en el checkbox: la marca del histórico.

Lo que NO se toca: la resolución sigue siendo POR FIRMA. El índice fue el que
causó el bulto de $7.404,88 del 07/08 y sigue siendo sólo fallback.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.conciliacion import sesion as _sesion  # noqa: E402
from modules.conciliacion.parser_banco import MovBanco  # noqa: E402

# Las cuatro filas reales de la sesión #66 que no se dejaban tildar.
HIST_ROWS = [
    {"id": 9001, "fecha": date(2026, 8, 12), "documento": "35841596",
     "concepto": "REV IVA+COMIS B10 # 500287 110826", "monto": Decimal("2.49"),
     "tipo": "C", "oficina": "CENTRO SERVIC. OPE", "detalle": None},
    {"id": 9002, "fecha": date(2026, 6, 17), "documento": "32877241",
     "concepto": "REV IVA+COMIS B10 # 2 16062026", "monto": Decimal("2.49"),
     "tipo": "C", "oficina": "", "detalle": None},
    {"id": 9003, "fecha": date(2026, 8, 12), "documento": "35835512",
     "concepto": "IVA CAUSADO SERVICIO", "monto": Decimal("0.37"),
     "tipo": "C", "oficina": "CENTRO SERVIC. OPE", "detalle": None},
    {"id": 9004, "fecha": date(2026, 6, 17), "documento": "32877296",
     "concepto": "IVA CAUSADO SERVICIO", "monto": Decimal("0.37"),
     "tipo": "C", "oficina": "", "detalle": None},
]

# Un renglón del extracto de la sesión, para que la mezcla sea la de verdad.
DEL_EXTRACTO = MovBanco(
    fecha=date(2026, 8, 15), concepto="COMISION MANTENIMIENTO CUENTA",
    documento="70001", monto=Decimal("12.50"), saldo=Decimal("0"),
    codigo="001045", tipo="D", oficina="AG. NORTE",
)


def _sin_db(monkeypatch, rows=HIST_ROWS):
    """El backlog histórico sin tocar la base."""
    monkeypatch.setattr(_sesion, "_cargar_historicos_pendientes", lambda nb: list(rows))


def test_historicos_como_movs_firma_igual_que_la_pantalla(monkeypatch):
    """El histórico tiene que firmar EXACTO como lo firma el template, si no
    la resolución vuelve a fallar en silencio."""
    _sin_db(monkeypatch)
    movs = _sesion.historicos_como_movs(10)
    assert len(movs) == 4
    assert [_sesion.firma_mov(m) for m in movs] == [
        "2026-08-12|35841596|2.49|C",
        "2026-06-17|32877241|2.49|C",
        "2026-08-12|35835512|0.37|C",
        "2026-06-17|32877296|0.37|C",
    ]
    # Decimal, no float: el monto viaja a `confirmar_match` y de ahí a un
    # WHERE monto = %s contra una columna numeric.
    assert all(isinstance(m.monto, Decimal) for m in movs)


def test_un_historico_tildado_resuelve_y_no_rebota(monkeypatch):
    """EL BUG: cuatro créditos históricos, cero filas resueltas, mensaje de
    'ya no están como pendientes'."""
    from modules.conciliacion import banco_v2_view as _v

    _sin_db(monkeypatch)
    sigs = [_sesion.firma_mov(m) for m in _sesion.historicos_como_movs(10)]

    subset, faltantes = _v._resolver_reals_impuestos(
        [DEL_EXTRACTO], sigs, [], etapa="test",
    )
    assert faltantes == [], "un histórico del tab no puede ser un 'faltante'"
    assert len(subset) == 4
    assert sum(m.monto for m in subset) == Decimal("5.72")


def test_el_extracto_sigue_resolviendo_igual(monkeypatch):
    """Lo que ya funcionaba (los débitos del extracto, el caso de Alex) no
    cambia: se resuelve por firma contra el payload de la sesión."""
    from modules.conciliacion import banco_v2_view as _v

    _sin_db(monkeypatch)
    subset, faltantes = _v._resolver_reals_impuestos(
        [DEL_EXTRACTO], [_sesion.firma_mov(DEL_EXTRACTO)], [], etapa="test",
    )
    assert faltantes == [] and subset == [DEL_EXTRACTO]


def test_una_firma_que_no_existe_en_ningun_lado_sigue_siendo_faltante(monkeypatch):
    """El freno del 07/08 no se aflojó: mirar también en los históricos NO es
    dejar pasar cualquier cosa. Una firma que no está en NINGUNA de las dos
    poblaciones tiene que volver como faltante para que el endpoint frene."""
    from modules.conciliacion import banco_v2_view as _v

    _sin_db(monkeypatch)
    subset, faltantes = _v._resolver_reals_impuestos(
        [DEL_EXTRACTO], ["2026-08-15|99999999|3099.52|C"], [], etapa="test",
    )
    assert subset == []
    assert faltantes == ["2026-08-15|99999999|3099.52|C"]


def test_un_historico_conciliado_deja_de_resolver(monkeypatch):
    """`_cargar_historicos_pendientes` sólo trae los que siguen pendientes.
    Si se concilia entre el preview y el confirmar, la firma tiene que
    volver como faltante — que es el aviso que SÍ corresponde."""
    from modules.conciliacion import banco_v2_view as _v

    _sin_db(monkeypatch)
    sig = _sesion.firma_mov(_sesion.historicos_como_movs(10)[0])
    _sin_db(monkeypatch, rows=HIST_ROWS[1:])  # el 9001 ya se concilió
    subset, faltantes = _v._resolver_reals_impuestos(
        [DEL_EXTRACTO], [sig], [], etapa="test",
    )
    assert subset == [] and faltantes == [sig]


def test_la_fila_historica_del_template_lleva_la_firma_que_resuelve(monkeypatch):
    """El puente completo: lo que el HTML pone en `data-sig` para un item
    histórico (idx = -1) es exactamente lo que el server sabe resolver."""
    import pathlib

    from jinja2 import Environment, FileSystemLoader

    import filters as _filters
    from modules.conciliacion import banco_v2_view as _v

    dirs = [str(p) for p in pathlib.Path(".").glob("modules/*/templates")] + ["templates"]
    env = Environment(loader=FileSystemLoader(dirs), autoescape=True)
    env.globals.update(url_for=lambda *a, **k: "#", csrf_token=lambda: "x")
    for nombre in dir(_filters):
        fn = getattr(_filters, nombre)
        if callable(fn) and not nombre.startswith("_"):
            env.filters[nombre] = fn

    tpl = env.get_template("conciliacion/_banco_v2_tab_impuestos.html")
    item = {"mov": _sesion._hist_to_mov_like(HIST_ROWS[0]), "cat": None,
            "idx": -1, "es_historico": True, "id_historico": 9001}
    html = tpl.render(buckets={"impuestos": [item]}, sesion={"id": 66})

    sig = re.search(r'data-sig="([^"]+)"', html)
    assert sig, "el checkbox del histórico perdió el data-sig"
    assert re.search(r'value="-1"', html), "el histórico ya no viene con idx -1"

    _sin_db(monkeypatch)
    subset, faltantes = _v._resolver_reals_impuestos([], [sig.group(1)], [], etapa="test")
    assert faltantes == []
    assert len(subset) == 1 and subset[0].documento == "35841596"
