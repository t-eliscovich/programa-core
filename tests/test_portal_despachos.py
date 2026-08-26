"""Los despachos que ve el cliente en el portal.

TMT 2026-08-26, fase 3 del plan del portal. Cuatro reglas, y las cuatro se
rompen en silencio:

⭐ **El código Y EL RUC del cliente van adentro de la consulta**, también al
abrir una guía suelta. Dos motivos, los dos medidos: los números de guía van
uno atrás del otro —si el filtro se hiciera después, cambiarle un dígito a la
URL sería ver el despacho de otro— y hay `nombre_comercial` repetidos en
empresas de contribuyentes DISTINTOS (`PRE`, `MCS`), así que el código de 3
letras solo no identifica a nadie.

⭐ **Sin RUC no se muestran despachos.** Cuando no se puede probar de quién es
la mercadería, la respuesta no es adivinar.

⭐ **Lo devuelto se marca pero NO se resta.** La factura cobra el bruto y la
devolución se corrige con otro documento: si acá se restara, esta pantalla
diría kilos distintos de los de la factura que el cliente tiene en la mano.

⭐ **Asinfo caído no tumba el portal.** La pantalla lo dice y el estado de
cuenta sigue vivo.

⭐ **Una guía se muestra AGRUPADA por tela.** Hay un renglón por rollo: AJO
tiene 4.241 renglones en 12 meses, y "qué me mandaron" no se contesta con 20
renglones que repiten cuatro veces la misma tela.
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
from pathlib import Path

if not hasattr(_dt, "UTC"):          # el sandbox a veces corre python 3.10
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from modules.asinfo import despachos_cliente as dsp  # noqa: E402

TPL = ROOT / "modules" / "portal" / "templates" / "portal"
#: Las del vendedor. Los parciales compartidos viven acá y el portal los ve
#: porque `registro_portal.py` le presta la carpeta a Jinja.
VEND_TPL = ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
LISTA_COMPARTIDA = (VEND_TPL / "_despachos_lista.html").read_text(encoding="utf-8")
GUIA_COMPARTIDA = (VEND_TPL / "_despachos_guia.html").read_text(encoding="utf-8")
VISTAS_VEND = (ROOT / "modules" / "mi_cartera" / "views.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _sin_cache():
    dsp.reset_cache()
    yield
    dsp.reset_cache()


class Falso:
    """Un Asinfo de mentira que ANOTA la consulta que le hicieron."""

    def __init__(self, filas=(), revienta=False):
        self.filas = list(filas)
        self.revienta = revienta
        self.consultas: list[str] = []

    def __call__(self, db, sql, max_results=None, **k):
        self.consultas.append(sql)
        if self.revienta:
            raise RuntimeError("Metabase no contesta")
        return list(self.filas)


def _asinfo(monkeypatch, falso):
    from modules._lib import metabase_client
    monkeypatch.setattr(metabase_client, "fetch_dataset", falso)
    return falso


#: El RUC de AJO. Va en cada llamada porque el código solo no identifica.
RUC = "1793217341001"

UNA_GUIA = [{"guia": "DES-000096562", "dia": "2026-08-26", "categoria": "Fleece",
             "kg": 354.25, "cuantos": 20, "devueltos": 0,
             "factura": "001-099-000182687"}]

#: La misma guía, con las dos cosas que viajan juntas en un camión: telas
#: (rollos) y cuellos (unidades). Asinfo las devuelve en renglones separados
#: porque la consulta agrupa por categoría.
GUIA_MIXTA = UNA_GUIA + [{"guia": "DES-000096562", "dia": "2026-08-26",
                          "categoria": "Cuellos", "kg": 8.4, "cuantos": 3,
                          "devueltos": 0, "factura": "001-099-000182687"}]

ROLLOS = [
    {"dia": "2026-08-26", "producto": "Fleece 96 Perchado JFP", "pcod": "FE96JFP",
     "categoria": "Fleece", "lote": "2/8-0004177689", "kg": 21.4, "devuelto": 0,
     "factura": "001-099-000182687"},
    {"dia": "2026-08-26", "producto": "Fleece 96 Perchado JFP", "pcod": "FE96JFP",
     "categoria": "Fleece", "lote": "2/8-0004177690", "kg": 21.1, "devuelto": 0,
     "factura": "001-099-000182687"},
    {"dia": "2026-08-26", "producto": "Fleece 96 Perchado JFP", "pcod": "FE96JFP",
     "categoria": "Fleece", "lote": "2/8-0004177690", "kg": 20.5, "devuelto": 0,
     "factura": "001-099-000182687"},
    {"dia": "2026-08-26", "producto": "Kiana 415x90 BOT", "pcod": "KI41BOT",
     "categoria": "Poliester", "lote": "4/4-0003973799", "kg": 22.0, "devuelto": 0,
     "factura": "001-099-000182687"},
]

#: Cuellos, Rib y Puños NO son rollos: van en unidades. Ver `POR_UNIDAD`.
ACCESORIOS = [
    {"dia": "2026-08-26", "producto": "Cuellos T40 MAR", "pcod": "C40MAR",
     "categoria": "Cuellos", "lote": "0004212300", "kg": 3.55, "devuelto": 0,
     "factura": "001-099-000182687"},
    {"dia": "2026-08-26", "producto": "Rib Acanalado JOS", "pcod": "RAJOS",
     "categoria": "Rib", "lote": "0004212311", "kg": 4.85, "devuelto": 0,
     "factura": "001-099-000182687"},
    {"dia": "2026-08-26", "producto": "Puños MAR", "pcod": "PUMAR",
     "categoria": "Puños", "lote": "0004212306", "kg": 2.40, "devuelto": 0,
     "factura": "001-099-000182687"},
]


# ---------------------------------------------------------------------------
# El cliente va adentro de la consulta
# ---------------------------------------------------------------------------


def test_el_codigo_del_cliente_va_en_el_where(monkeypatch):
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    dsp.de_cliente("AJO", RUC)
    sql = f.consultas[0]
    assert "'AJO'" in sql
    assert "nombre_comercial" in sql, "la llave PC↔Asinfo es nombre_comercial"


def test_una_guia_suelta_TAMBIEN_se_pide_con_el_cliente(monkeypatch):
    """⭐ El punto: los números van uno atrás del otro. Sin el cliente adentro
    de la consulta, cambiarle un dígito a la URL sería ver lo de otro."""
    f = _asinfo(monkeypatch, Falso(ROLLOS))
    dsp.guia("AJO", RUC, "DES-000096562")
    sql = f.consultas[0]
    assert "'AJO'" in sql
    assert "'DES-000096562'" in sql


def test_una_guia_que_no_es_suya_no_existe(monkeypatch):
    """Y se dice "no existe", no "es de otro": si contestara de quién es, el
    portal sería una guía telefónica de despachos."""
    _asinfo(monkeypatch, Falso([]))
    r = dsp.guia("AJO", RUC, "DES-000096562")
    assert r["ok"] is True
    assert r["existe"] is False


@pytest.mark.parametrize("basura", ["AJO'; DROP", "AJ O", "", "A" * 11, "aj'--"])
def test_un_codigo_raro_no_llega_a_la_consulta(monkeypatch, basura):
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    assert dsp.de_cliente(basura, RUC)["ok"] is False
    assert f.consultas == [], "se interpoló algo sin validar"


@pytest.mark.parametrize("basura", ["DES-1", "'; DROP", "DES-000096562 OR 1=1", ""])
def test_un_numero_de_guia_raro_no_llega_a_la_consulta(monkeypatch, basura):
    f = _asinfo(monkeypatch, Falso(ROLLOS))
    assert dsp.guia("AJO", RUC, basura)["existe"] is False
    assert f.consultas == []


def test_el_RUC_va_en_el_where_JUNTO_con_el_codigo(monkeypatch):
    """🚨 El hallazgo de la auditoría del 26/08: `nombre_comercial` NO es único.
    `PRE` son dos empresas de contribuyentes distintos (Rodríguez Paredes y
    Ponce Chávez) y `MCS` también (Chanatasig, con 24 guías, contra MCS Dyeing
    Finishing Machinery). Con el código solo, el día que a cualquiera de los
    dos le entre una guía, el otro la ve. Y esto va a internet.

    Filtrar además por RUC no pierde nada: de las 7.326 guías de 3 meses, las
    7.326 matchean por código Y por RUC."""
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    dsp.de_cliente("AJO", RUC)
    sql = f.consultas[0]
    assert "identificacion" in sql
    assert "'1793217341'" in sql, "van los 10 primeros dígitos, como ruc10"


def test_el_RUC_tambien_va_al_abrir_UNA_guia(monkeypatch):
    f = _asinfo(monkeypatch, Falso(ROLLOS))
    dsp.guia("AJO", RUC, "DES-000096562")
    assert "identificacion" in f.consultas[0]


def test_sin_RUC_no_se_muestran_despachos(monkeypatch):
    """⭐ Cuando no se puede probar de quién es la mercadería, la respuesta no
    es adivinar por un código de 3 letras que se repite. Son 11 clientes de
    3.986, y la pantalla les dice que llamen."""
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    r = dsp.de_cliente("AJO", "")
    assert r["sin_ruc"] is True
    assert r["guias"] == []
    assert f.consultas == [], "sin RUC ni se pregunta"


def test_un_RUC_corto_no_alcanza(monkeypatch):
    """Cuatro dígitos no identifican a nadie: sería el código de 3 letras otra
    vez, pero peor."""
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    assert dsp.de_cliente("AJO", "1793")["sin_ruc"] is True
    assert f.consultas == []


def test_la_cedula_pelada_y_el_RUC_entero_son_el_mismo_cliente(monkeypatch):
    """En la ficha a veces está la cédula y en Asinfo el RUC. Los dos lados se
    recortan al mismo largo, así que la consulta sale igual."""
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    dsp.de_cliente("AJO", "1793217341")
    dsp.de_cliente("AJO", "1793217341001")
    assert len(f.consultas) == 1, "es el mismo cliente: la segunda sale de la caché"


def test_la_cache_no_mezcla_dos_clientes_con_el_MISMO_codigo(monkeypatch):
    """El caso `PRE`: mismo código, contribuyentes distintos. Si el RUC no
    estuviera en la clave de la caché, el segundo vería lo del primero."""
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    dsp.de_cliente("PRE", "1720826609001")
    dsp.de_cliente("PRE", "1315104255001")
    assert len(f.consultas) == 2
    assert "'1315104255'" in f.consultas[1]


def test_el_despacho_anulado_no_se_muestra(monkeypatch):
    """Un despacho anulado volvió a bodega: no es parte de lo que se llevó."""
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    dsp.de_cliente("AJO", RUC)
    assert "fecha_anulacion IS NULL" in f.consultas[0]


# ---------------------------------------------------------------------------
# Asinfo caído
# ---------------------------------------------------------------------------


def test_si_asinfo_no_contesta_la_pantalla_lo_dice(monkeypatch):
    _asinfo(monkeypatch, Falso(revienta=True))
    r = dsp.de_cliente("AJO", RUC)
    assert r["ok"] is False
    assert r["guias"] == []


def test_si_asinfo_no_contesta_la_guia_tampoco_levanta(monkeypatch):
    _asinfo(monkeypatch, Falso(revienta=True))
    assert dsp.guia("AJO", RUC, "DES-000096562")["ok"] is False


def test_el_error_NO_se_cachea(monkeypatch):
    """🚨 Cachear un "no contestó" convierte un hipo de Metabase en cinco
    minutos de pantalla vacía para todos."""
    f = _asinfo(monkeypatch, Falso(revienta=True))
    dsp.de_cliente("AJO", RUC)
    dsp.de_cliente("AJO", RUC)
    assert len(f.consultas) == 2, "se cacheó un error"


def test_lo_que_salio_bien_SI_se_cachea(monkeypatch):
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    dsp.de_cliente("AJO", RUC)
    dsp.de_cliente("AJO", RUC)
    assert len(f.consultas) == 1


def test_la_cache_no_mezcla_clientes(monkeypatch):
    """🚨 La que de verdad importa: si la clave de la caché no tuviera el
    código, el segundo cliente vería los despachos del primero."""
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    dsp.de_cliente("AJO", RUC)
    dsp.de_cliente("BED", "0999999999001")
    assert len(f.consultas) == 2
    assert "'BED'" in f.consultas[1]


# ---------------------------------------------------------------------------
# Cómo se muestra
# ---------------------------------------------------------------------------


def test_la_guia_se_agrupa_por_tela_y_cuenta_los_rollos(monkeypatch):
    _asinfo(monkeypatch, Falso(ROLLOS))
    r = dsp.guia("AJO", RUC, "DES-000096562")
    assert [t["producto"] for t in r["telas"]] == ["Fleece 96 Perchado JFP",
                                                   "Kiana 415x90 BOT"]
    fleece = r["telas"][0]
    assert fleece["cuantos"] == 3
    assert fleece["por_unidad"] is False
    assert r["rollos"] == 4 and r["unidades"] == 0


def test_cuellos_rib_y_punos_van_en_UNIDADES(monkeypatch):
    """⭐ TMT 26/08: *"cuando sea cuellos o RIB ponele una u"*. Y el dato le da
    la razón: medido sobre 3 meses, Puños pesa 2,40 kg por renglón, Cuellos
    2,72 y Rib 6,09, contra las diez telas que están todas entre 18 y 21,3 —
    o sea, un rollo. No son rollos, son piezas."""
    _asinfo(monkeypatch, Falso(ACCESORIOS))
    r = dsp.guia("AJO", RUC, "DES-000096562")
    assert r["unidades"] == 3
    assert r["rollos"] == 0
    assert all(t["por_unidad"] for t in r["telas"])


def test_la_categoria_manda_aunque_venga_con_enie_o_en_minuscula():
    """`Puños` viene del maestro de Asinfo tal como lo escribieron. Si la
    comparación fuera literal, los puños se contarían como rollos."""
    assert dsp._por_unidad("Puños") is True
    assert dsp._por_unidad("PUNOS") is True
    assert dsp._por_unidad(" rib ") is True
    assert dsp._por_unidad("Cuellos") is True
    assert dsp._por_unidad("Fleece") is False
    assert dsp._por_unidad("") is False


def test_una_guia_con_telas_y_cuellos_cuenta_las_dos_cosas(monkeypatch):
    """Viajan juntas en el mismo camión: la lista tiene que decir las dos."""
    _asinfo(monkeypatch, Falso(GUIA_MIXTA))
    g = dsp.de_cliente("AJO", RUC)["guias"]
    assert len(g) == 1, "las dos categorías son UNA guía, no dos"
    assert g[0]["rollos"] == 20 and g[0]["unidades"] == 3


def test_la_pantalla_NO_muestra_kilos():
    """⭐ TMT 26/08: *"o sea nada de kilos, reemplazalo por rollos"*. El cliente
    cuenta bultos cuando le llega el camión. Los kilos siguen en el estado de
    cuenta y en la factura, que es donde se discute la plata."""
    for texto in (LISTA_COMPARTIDA, GUIA_COMPARTIDA):
        sin_comentarios = re.sub(r"\{#.*?#\}", "", texto, flags=re.S)
        # 🚨 `kg` suelto y no `"kg" in ...`: la palabra "background" lo trae
        # adentro, y el test pasaba a rojo por la hoja de estilos.
        assert not re.search(r"\bkg\b", sin_comentarios)
        assert "num_es" not in sin_comentarios, "no quedó ni un número con decimales"
        assert "rollo" in sin_comentarios.lower()


def test_los_kilos_igual_se_traen(monkeypatch):
    """No se muestran, pero se siguen trayendo: son los que tienen que dar
    iguales a `scintela.factura.kg` — la red de toda esta consulta."""
    _asinfo(monkeypatch, Falso(UNA_GUIA))
    assert dsp.de_cliente("AJO", RUC)["kg"] == 354.25


def test_los_LOTES_no_van(monkeypatch):
    """TMT 26/08: *"esto no hace falta: Lotes: 209367 · 209468"*. Eran dos
    números por tela que el cliente no le pide a esta pantalla — el lote está
    impreso en la etiqueta del rollo que tiene enfrente. No se traen ni se
    muestran."""
    f = _asinfo(monkeypatch, Falso(ROLLOS))
    tela = dsp.guia("AJO", RUC, "DES-000096562")["telas"][0]
    assert "lotes" not in tela
    assert "codigo_lote" not in f.consultas[0], "ni se le pide a Asinfo"
    # 🪞 Sin los comentarios: el ⛔ que explica por qué se sacaron dice
    # "Lote", y el test se encontraba a sí mismo.
    assert "Lote" not in re.sub(r"\{#.*?#\}", "", GUIA_COMPARTIDA, flags=re.S)


def test_las_telas_van_de_mayor_a_menor(monkeypatch):
    """Lo que más pesa primero: es lo que el cliente vino a mirar."""
    _asinfo(monkeypatch, Falso(ROLLOS))
    kilos = [t["kg"] for t in dsp.guia("AJO", RUC, "DES-000096562")["telas"]]
    assert kilos == sorted(kilos, reverse=True)


def test_lo_devuelto_se_MARCA_pero_no_se_descuenta(monkeypatch):
    """🚨 El segundo hallazgo del 26/08. `cantidad_devuelta` sí es mercadería
    que volvió —875 líneas y 15.033 kg en 3 meses—, pero la factura cobra lo
    que SALIÓ: la devolución se corrige con otro documento (medido en MTR, guía
    DES-000096186: factura por $354,99 y devolución por los mismos $354,99 al
    día siguiente).

    Cruzado contra `scintela.factura.kg`: los kilos coinciden en 515 de 515
    facturas. Si acá se descontara, esta pantalla diría algo distinto de la
    factura que el cliente tiene en la mano."""
    rollos = [{**ROLLOS[0], "devuelto": 21.4},
              {**ROLLOS[1], "devuelto": 0},
              {**ROLLOS[3], "devuelto": 22.0}]
    _asinfo(monkeypatch, Falso(rollos))
    r = dsp.guia("AJO", RUC, "DES-000096562")
    assert r["rollos"] == 3, "los rollos son los que salieron, sin descontar"
    assert r["devueltos"] == 2
    fleece = next(t for t in r["telas"] if t["producto"].startswith("Fleece"))
    assert fleece["cuantos"] == 2 and fleece["devueltos"] == 1


def test_la_lista_tambien_dice_cuantos_volvieron(monkeypatch):
    _asinfo(monkeypatch, Falso([{**UNA_GUIA[0], "devueltos": 2}]))
    r = dsp.de_cliente("AJO", RUC)
    assert r["guias"][0]["rollos"] == 20
    assert r["guias"][0]["devueltos"] == 2
    assert r["devueltos"] == 2


def test_la_pantalla_nombra_lo_devuelto():
    """Si el dato estuviera y no se mostrara, el cliente vería como entregado
    un rollo que él sabe que devolvió — y llama."""
    for texto in (LISTA_COMPARTIDA, GUIA_COMPARTIDA):
        assert "devolvió" in texto.lower()


def test_los_numeros_largos_se_dicen_por_sus_ultimos_digitos():
    """TMT 26/08: *"pongamos de todo los últimos números, así ocupa menos"*.
    Es como se nombra un papel al hablar, y es la misma forma que ya usa la
    ficha de la factura."""
    assert dsp.corto("DES-000096562") == "96562"
    assert dsp.corto("001-099-000182687") == "182687"
    assert dsp.corto("2/8-0004177689") == "177689"
    assert dsp.corto("0004212301") == "212301"
    assert dsp.corto("") == ""


def test_el_numero_ENTERO_sigue_estando(monkeypatch):
    """🚨 El corto es un RÓTULO, no una llave: el link y el `title` llevan el
    número completo, que es el que el cliente compara contra su guía de papel.
    Poner el recorte en la URL sería buscar por un número que puede repetirse."""
    _asinfo(monkeypatch, Falso(UNA_GUIA))
    g = dsp.de_cliente("AJO", RUC)["guias"][0]
    assert g["numero"] == "DES-000096562" and g["corto"] == "96562"
    assert 'href="{{ url_guia }}{{ g.numero }}"' in LISTA_COMPARTIDA
    assert 'title="{{ g.numero }}"' in LISTA_COMPARTIDA


def test_una_guia_sin_factura_todavia_no_es_un_error(monkeypatch):
    """La mercadería va adelante del papel: al día siguiente ya está."""
    _asinfo(monkeypatch, Falso([{**UNA_GUIA[0], "factura": ""}]))
    assert dsp.de_cliente("AJO", RUC)["guias"][0]["factura"] == ""


def test_una_fila_sin_kilos_no_entra(monkeypatch):
    """Ni rompe: una guía de 0 kg no es una guía."""
    _asinfo(monkeypatch, Falso([{**UNA_GUIA[0], "kg": 0},
                                {**UNA_GUIA[0], "kg": "no es un número"}]))
    assert dsp.de_cliente("AJO", RUC)["guias"] == []


def test_el_periodo_se_acota(monkeypatch):
    """Ni 0 meses ni 500: el que escribe cualquier cosa en la URL ve un
    período razonable, no una consulta de diez años."""
    f = _asinfo(monkeypatch, Falso(UNA_GUIA))
    assert dsp.de_cliente("AJO", RUC, meses=0)["meses"] == dsp.MESES_DEFAULT
    assert dsp.de_cliente("AJO", RUC, meses=500)["meses"] == dsp.MESES_MAX
    assert len(f.consultas) == 2


# ---------------------------------------------------------------------------
# Las pantallas
# ---------------------------------------------------------------------------


def _app_portal():
    import os
    from unittest.mock import patch

    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        return build_app()


def test_sin_sesion_los_despachos_mandan_a_la_puerta():
    app, deshacer = _app_portal()
    try:
        r = app.test_client().get("/despachos")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/ingresar")
    finally:
        deshacer()


def test_la_guia_de_otro_da_404(monkeypatch):
    """La vista traduce "no es suya" a 404. Con el cliente ya adentro de la
    consulta, esto es el segundo candado, no el único."""
    app, deshacer = _app_portal()
    try:
        _asinfo(monkeypatch, Falso([]))       # Asinfo no devuelve nada: no es suya
        c = app.test_client()
        with c.session_transaction() as s:
            s["portal_cliente"] = "AJO"
        # La vista le pide el RUC a la ficha del cliente; acá no hay base.
        from modules.portal import acceso
        monkeypatch.setattr(acceso, "cliente", lambda cod: {"ruc": RUC})
        assert c.get("/despacho/DES-000000001").status_code == 404
    finally:
        deshacer()


def test_el_estado_de_cuenta_linkea_los_despachos():
    """Si la pantalla existiera y nadie llegara a ella, sería lo mismo que no
    existir."""
    pantalla = (TPL / "estado_cuenta.html").read_text(encoding="utf-8")
    sin_comentarios = re.sub(r"\{#.*?#\}", "", pantalla, flags=re.S)
    assert '"/despachos"' in sin_comentarios


def test_la_pantalla_muestra_la_tela_los_rollos_y_la_factura():
    """Lo que quedó después de tres podas: sin kilos y sin lotes."""
    assert "Factura" in LISTA_COMPARTIDA and "Rollos" in LISTA_COMPARTIDA
    assert "Tela" in GUIA_COMPARTIDA and "rollo" in GUIA_COMPARTIDA


def test_los_rollos_y_las_unidades_van_en_COLUMNAS_SEPARADAS():
    """TMT 26/08: *"mejor hagamos columna rollos y columna unidades, se ve
    feo"*. Apiladas en la misma celda son dos cosas distintas peleando por un
    lugar."""
    for texto in (LISTA_COMPARTIDA, GUIA_COMPARTIDA):
        sin_comentarios = re.sub(r"\{#.*?#\}", "", texto, flags=re.S)
        assert ">Rollos<" in sin_comentarios
        assert ">Unidades<" in sin_comentarios


# ---------------------------------------------------------------------------
# El vendedor mira LO MISMO
# ---------------------------------------------------------------------------


def test_el_cliente_y_su_vendedor_miran_EL_MISMO_parcial():
    """⭐ TMT 26/08: *"los despachos también deberíamos agregar para los
    vendedores"*. Y no una copia: si el vendedor y el cliente vieran dos listas
    distintas, la discusión no se puede tener. Misma razón por la que
    `_movimientos.html` y `_que_se_llevo.html` viven en `mi_cartera`."""
    pantallas = [(TPL / "despachos.html").read_text(encoding="utf-8"),
                 (TPL / "despacho.html").read_text(encoding="utf-8"),
                 (VEND_TPL / "despacho.html").read_text(encoding="utf-8"),
                 # La lista del vendedor es la PESTAÑA de la ficha (26/08):
                 # el parcial se incluye desde `_movimientos.html`.
                 (VEND_TPL / "_movimientos.html").read_text(encoding="utf-8")]
    for pantalla in pantallas:
        assert "_despachos_lista.html" in pantalla or \
               "_despachos_guia.html" in pantalla


def test_las_pantallas_del_vendedor_no_copian_la_tabla():
    """La tabla se escribe UNA vez. Si alguna se dibujara la suya, se separan a
    la primera corrección."""
    for carpeta, archivo in ((TPL, "despachos.html"), (TPL, "despacho.html"),
                             (VEND_TPL, "despacho.html")):
        texto = re.sub(r"\{#.*?#\}", "", (carpeta / archivo).read_text(encoding="utf-8"),
                       flags=re.S)
        assert "<table" not in texto, f"{carpeta.name}/{archivo} copió la tabla"


def test_el_guard_del_vendedor_corre_ANTES_de_pedirle_nada_a_asinfo():
    """🚨 `_cargar_cliente` es el que verifica que el cliente sea SUYO. Si se
    llamara después, tipear el código de un cliente ajeno mostraría su
    mercadería — el mismo cuidado que el botón de cortar el acceso."""
    for funcion, pedido in (("def cliente(", "dsp.de_cliente("),
                            ("def despacho(", "dsp.guia(")):
        cuerpo = VISTAS_VEND[VISTAS_VEND.index(funcion):]
        cuerpo = cuerpo[:cuerpo.index("\n@")]
        guard = "_cargar_cliente(" if funcion == "def cliente(" else "_ficha_del_cliente("
        assert cuerpo.index(guard) < cuerpo.index(pedido), funcion


def test_el_vendedor_llega_desde_la_ficha_de_su_cliente():
    """Una pantalla a la que no lleva ningún link es una pantalla que no
    existe. Y con rótulo: un ícono suelto no se encuentra."""
    ficha = (VEND_TPL / "cliente.html").read_text(encoding="utf-8")
    # ⭐ TMT 26/08: *"«despachos» debería estar después de cheques, no uno
    # aparte"*. Ya no es un botón arriba de la ficha: es la tercera pestaña,
    # al lado de Facturas y Cheques.
    assert "despachos_tab = True" in ficha
    tabs = (VEND_TPL / "_movimientos.html").read_text(encoding="utf-8")
    assert "?tab=despachos" in tabs
    assert ">Despachos</a>" in tabs


def test_las_dos_pantallas_del_vendedor_piden_el_permiso_de_siempre():
    """Sin el permiso, `micartera.ver` no gatea nada y la pantalla nueva sería
    la puerta de atrás del portal de vendedores."""
    assert VISTAS_VEND.count('@requiere_permiso("micartera.ver")') >= 2
    for ruta in ("/despachos\")", "/despacho/<numero>\")"):
        i = VISTAS_VEND.index(ruta)
        assert '@requiere_permiso("micartera.ver")' in VISTAS_VEND[i:i + 220]
