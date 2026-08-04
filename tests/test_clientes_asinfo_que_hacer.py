"""La columna «Qué hacer» de /admin/clientes-asinfo.

TMT 2026-08-04. La pantalla decía muy bien QUÉ PASA con cada código repetido
y no decía nada de QUÉ HACER, así que los 20 casos había que razonarlos de
cero uno por uno. Y no son el mismo problema: en 14 de los 20 la ficha con el
código bueno YA EXISTE en PC con el mismo RUC —es el mismo cliente cargado
dos veces, se borra la sobrante— y sólo en 6 son dos empresas reales
chocando, donde borrar perdería un cliente.

El corte entre esos dos mundos es el que fija estos tests. El caso que lo
hace no-trivial es JRP: dos fichas con el MISMO RUC que NO son la misma
persona (Jorge Rosero Pozo y Judith del Rocío Pantoja Pozo) porque una tiene
el RUC mal cargado. Si la pantalla mirara sólo el RUC diría "borrá una" y se
perdería un cliente real.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.admin_dbase import clientes_asinfo_view as v  # noqa: E402


def ficha(id_cliente, codigo, nombre, ruc, *, direccion1="", n_fact=0, n_chq=0):
    return {
        "id_cliente": id_cliente, "codigo_cli": codigo, "nombre": nombre,
        "ruc": ruc, "direccion1": direccion1, "vend": "", "activo": True,
        "n_facturas": n_fact, "n_cheques": n_chq,
    }


def grupo(fichas, mapa=None, asinfo_ok=True):
    """Un grupo tal cual lo arma la pantalla (pasa por `cruzar` de verdad)."""
    grupos = v.cruzar(fichas, mapa or {}, asinfo_ok)
    assert len(grupos) == 1, "los helpers de este test arman un solo código"
    return grupos[0]


# ---------------------------------------------------------------------------
# mismo_nombre — el corte entre "cargado dos veces" y "dos personas"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("ALEXA MAGDALENA PINCAY ESPIN", "ALEXA MAGDALENA PINCAY"),
    ("MODITEX S.A.", "MODITEX"),
    ("MODITEX CIA LTDA", "MODITEX SAS"),  # el ruido societario no distingue
    ("NORMA ISAURA ALBA USHIÑA", "NORMA ISAURA ALBA USHINA"),   # con y sin tilde
    ("ALMACENES JOSE PUEBLA 2", "ALMACENES JOSE PUEBLA 2"),
])
def test_reconoce_el_mismo_nombre_escrito_distinto(a, b):
    assert v.mismo_nombre(a, b)
    assert v.mismo_nombre(b, a), "tiene que ser simétrico"


@pytest.mark.parametrize("a,b", [
    ("JORGE ROSERO POZO", "JUDITH DEL ROCIO PANTOJA POZO"),  # el caso JRP
    ("LUIS ERNESTO CANAMAR", "LOLA EMPERATRIZ CISNEROS"),
    ("KATHY BRITO", "KAREN ALEJANDRA BENAVIDES"),
    ("YOLANDA MOROCHO", "YANELA MISHELL ORTIZ TINOCO"),
])
def test_no_confunde_dos_personas_distintas(a, b):
    assert not v.mismo_nombre(a, b)


def test_un_nombre_vacio_nunca_se_parece_a_nada():
    """Si no, dos fichas sin nombre se darían por duplicadas entre sí."""
    assert not v.mismo_nombre("", "MODITEX")
    assert not v.mismo_nombre("", "")


# ---------------------------------------------------------------------------
# Mismo RUC → o es la misma ficha dos veces, o hay un RUC mal cargado
# ---------------------------------------------------------------------------

def test_mismo_ruc_y_mismo_nombre_es_la_misma_ficha_cargada_dos_veces():
    g = grupo([
        ficha(29243, "ALP", "ALEXA MAGDALENA PINCAY ESPIN", "1202632590001"),
        ficha(30021, "ALP", "ALEXA MAGDALENA PINCAY", "1202632590001"),
    ])
    qh = g["que_hacer"]
    assert qh["clase"] == v.DUPLICADA
    assert "dos veces" in qh["titulo"]
    assert qh["acciones"] and qh["acciones"][0]["href"] == "/clientes?q=ALP"


def test_avisa_cuando_la_ficha_que_se_va_tiene_otra_direccion():
    """Se pierde el dato viejo — que lo sepa antes, no después (caso MTE)."""
    g = grupo([
        ficha(29170, "MTE", "MODITEX S.A.", "1791234567001", direccion1="CDLA. ALBORADA"),
        ficha(31562, "MTE", "MODITEX", "1791234567001", direccion1="CDLA. URDENOR"),
    ])
    qh = g["que_hacer"]
    assert qh["clase"] == v.DUPLICADA
    assert "direcciones" in qh["detalle"].lower()


def test_mismo_ruc_pero_dos_personas_no_manda_a_borrar():
    """El caso JRP. Borrar acá pierde un cliente real."""
    g = grupo([
        ficha(31088, "JRP", "JORGE ROSERO POZO", "1723252241001"),
        ficha(32066, "JRP", "JUDITH DEL ROCIO PANTOJA POZO", "1723252241001"),
    ])
    qh = g["que_hacer"]
    assert qh["clase"] == v.RUC_SOSPECHOSO
    assert "No borres" in qh["detalle"]
    assert all("clientes?q=" not in a["href"] for a in qh["acciones"])


# ---------------------------------------------------------------------------
# RUCs distintos → dos empresas reales
# ---------------------------------------------------------------------------

def test_si_asinfo_resuelve_manda_a_renombrar_a_la_otra():
    mapa = {"1700000001": [{"codigo": "LEC", "nombre_fiscal": "LUIS ERNESTO CANAMAR",
                            "nombre_comercial": "LEC", "es_cliente": True, "id_empresa": 1}]}
    g = grupo([
        ficha(29008, "LEC", "LUIS ERNESTO CANAMAR", "1700000001001"),
        ficha(29048, "LEC", "LOLA EMPERATRIZ CISNEROS", "1700000002001"),
    ], mapa)
    qh = g["que_hacer"]
    assert qh["clase"] == v.RENOMBRAR
    assert "LUIS ERNESTO CANAMAR" in qh["titulo"]
    hrefs = [a["href"] for a in qh["acciones"]]
    assert hrefs == ["/clientes/29048/cambiar-codigo"], \
        "sólo la que PIERDE el código se renombra"


def test_si_asinfo_no_conoce_a_ninguna_la_decision_es_de_la_duena():
    """El caso YMO: Asinfo no tiene ninguno de los dos RUC."""
    g = grupo([
        ficha(29995, "YMO", "YOLANDA MOROCHO", "1700000003001"),
        ficha(31038, "YMO", "YANELA MISHELL ORTIZ TINOCO", "1700000004001"),
    ])
    qh = g["que_hacer"]
    assert qh["clase"] == v.DECIDIR
    assert "no conoce" in qh["titulo"]
    assert len(qh["acciones"]) == 2, "cualquiera de las dos puede ser la que se mude"


def test_si_asinfo_les_da_otro_codigo_a_las_dos_lo_dice():
    mapa = {
        "1700000005": [{"codigo": "AAA", "nombre_fiscal": "UNO", "nombre_comercial": "AAA",
                        "es_cliente": True, "id_empresa": 1}],
        "1700000006": [{"codigo": "BBB", "nombre_fiscal": "DOS", "nombre_comercial": "BBB",
                        "es_cliente": True, "id_empresa": 2}],
    }
    g = grupo([
        ficha(1, "XYZ", "EMPRESA UNO", "1700000005001"),
        ficha(2, "XYZ", "EMPRESA DOS", "1700000006001"),
    ], mapa)
    qh = g["que_hacer"]
    assert qh["clase"] == v.DECIDIR
    assert "AAA" in qh["detalle"] and "BBB" in qh["detalle"]


def test_si_asinfo_le_da_el_mismo_codigo_a_las_dos_no_finge_resolverlo():
    mapa = {
        "1700000005": [{"codigo": "XYZ", "nombre_fiscal": "UNO", "nombre_comercial": "XYZ",
                        "es_cliente": True, "id_empresa": 1}],
        "1700000006": [{"codigo": "XYZ", "nombre_fiscal": "DOS", "nombre_comercial": "XYZ",
                        "es_cliente": True, "id_empresa": 2}],
    }
    g = grupo([
        ficha(1, "XYZ", "EMPRESA UNO", "1700000005001"),
        ficha(2, "XYZ", "EMPRESA DOS", "1700000006001"),
    ], mapa)
    assert g["que_hacer"]["clase"] == v.AMBIGUO
    assert g["que_hacer"]["acciones"] == []


def test_sin_ruc_lo_primero_es_cargar_el_ruc():
    g = grupo([
        ficha(1, "XYZ", "EMPRESA UNO", "1700000005001"),
        ficha(2, "XYZ", "EMPRESA DOS", ""),
    ], {"1700000005": [{"codigo": "AAA", "nombre_fiscal": "UNO", "nombre_comercial": "AAA",
                        "es_cliente": True, "id_empresa": 1}]})
    qh = g["que_hacer"]
    assert qh["clase"] == v.FALTA_RUC
    assert "#2" in qh["detalle"]


def test_sin_asinfo_no_inventa_un_veredicto():
    g = grupo([
        ficha(1, "XYZ", "EMPRESA UNO", "1700000005001"),
        ficha(2, "XYZ", "EMPRESA DOS", "1700000006001"),
    ], {}, asinfo_ok=False)
    qh = g["que_hacer"]
    assert qh["clase"] == v.PENDIENTE_ASINFO
    assert qh["acciones"] == []


def test_sin_asinfo_igual_resuelve_el_caso_del_mismo_ruc():
    """El RUC y el nombre son de PC: para eso no hace falta el puente."""
    g = grupo([
        ficha(29243, "ALP", "ALEXA MAGDALENA PINCAY ESPIN", "1202632590001"),
        ficha(30021, "ALP", "ALEXA MAGDALENA PINCAY", "1202632590001"),
    ], {}, asinfo_ok=False)
    assert g["que_hacer"]["clase"] == v.DUPLICADA


# ---------------------------------------------------------------------------
# La advertencia de plata mezclada
# ---------------------------------------------------------------------------

def test_avisa_que_el_cambio_de_codigo_va_a_ser_rechazado_si_hay_movimientos():
    """`plan_cambio_codigo` rechaza mientras el código tenga movimientos.

    Decirlo acá ahorra el viaje hasta la pantalla para comerse el error.
    """
    mapa = {"1700000001": [{"codigo": "BLP", "nombre_fiscal": "RAMIRO AYO",
                            "nombre_comercial": "BLP", "es_cliente": True, "id_empresa": 1}]}
    g = grupo([
        ficha(28929, "BLP", "RAMIRO AYO", "1700000001001", n_fact=75, n_chq=3),
        ficha(29142, "BLP", "BLANCA PENAHERRERA", "1700000002001", n_fact=75, n_chq=3),
    ], mapa)
    adv = g["que_hacer"]["advertencia"]
    assert "75 facturas" in adv and "3 cheques" in adv
    assert "rechazar" in adv


def test_sin_movimientos_no_hay_advertencia():
    mapa = {"1700000001": [{"codigo": "LEC", "nombre_fiscal": "LUIS ERNESTO CANAMAR",
                            "nombre_comercial": "LEC", "es_cliente": True, "id_empresa": 1}]}
    g = grupo([
        ficha(29008, "LEC", "LUIS ERNESTO CANAMAR", "1700000001001"),
        ficha(29048, "LEC", "LOLA EMPERATRIZ CISNEROS", "1700000002001"),
    ], mapa)
    assert g["que_hacer"]["advertencia"] == ""


def test_el_caso_duplicado_no_se_frena_por_los_movimientos():
    """Los movimientos cuelgan del CÓDIGO y el código sobrevive.

    Es exactamente el caso GUF que la dueña pidió borrar el 03/08 y la
    pantalla no la dejó.
    """
    g = grupo([
        ficha(1, "GUF", "GUADALUPE FIALLOS", "1700000009001", n_fact=55, n_chq=8),
        ficha(2, "GUF", "GUADALUPE FIALLOS SILVA", "1700000009001", n_fact=55, n_chq=8),
    ])
    qh = g["que_hacer"]
    assert qh["clase"] == v.DUPLICADA
    assert qh["advertencia"] == "", "acá borrar es seguro; no hay nada que advertir"


# ---------------------------------------------------------------------------
# Resumen y links
# ---------------------------------------------------------------------------

def test_el_resumen_separa_lo_que_se_arregla_solo_de_lo_que_necesita_decision():
    grupos = v.cruzar([
        ficha(1, "ALP", "ALEXA MAGDALENA PINCAY ESPIN", "1202632590001"),
        ficha(2, "ALP", "ALEXA MAGDALENA PINCAY", "1202632590001"),
        ficha(3, "YMO", "YOLANDA MOROCHO", "1700000003001"),
        ficha(4, "YMO", "YANELA MISHELL ORTIZ TINOCO", "1700000004001"),
    ], {}, True)
    r = v.resumen(grupos)
    assert r["duplicadas"] == 1
    assert r["necesitan_decision"] == 1


def test_todos_los_links_de_que_hacer_resuelven_contra_el_url_map():
    """El gotcha de la casa: los links son strings, un 404 no se ve al leer.

    Ver `tests/test_historial_links_resuelven.py` — mismo patrón.
    """
    from tests.test_routes_smoke import build_app

    casos = [
        # duplicada
        [ficha(1, "ALP", "ALEXA MAGDALENA PINCAY ESPIN", "1202632590001"),
         ficha(2, "ALP", "ALEXA MAGDALENA PINCAY", "1202632590001")],
        # ruc sospechoso
        [ficha(3, "JRP", "JORGE ROSERO POZO", "1723252241001"),
         ficha(4, "JRP", "JUDITH DEL ROCIO PANTOJA POZO", "1723252241001")],
        # decidir (dos empresas, Asinfo no las tiene)
        [ficha(5, "YMO", "YOLANDA MOROCHO", "1700000003001"),
         ficha(6, "YMO", "YANELA MISHELL ORTIZ TINOCO", "1700000004001")],
        # falta ruc
        [ficha(7, "XYZ", "EMPRESA UNO", "1700000005001"),
         ficha(8, "XYZ", "EMPRESA DOS", "")],
    ]
    hrefs = []
    for fichas in casos:
        for g in v.cruzar(fichas, {}, True):
            hrefs += [a["href"] for a in g["que_hacer"]["acciones"]]
    assert hrefs, "los casos de arriba tienen que producir links"

    app, deshacer = build_app()
    try:
        adaptador = app.url_map.bind("localhost")
        for href in hrefs:
            path = href.split("?")[0]
            try:
                adaptador.match(path, method="GET")
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"El link {href} no resuelve: {exc!r}") from exc
    finally:
        deshacer()
