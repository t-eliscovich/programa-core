"""El "← Volver" devuelve a la pantalla ANTERIOR, con su filtro.

TMT 2026-08-17 (dueña, mirando el Historial filtrado por "bed"): *"En todas
las pantallas cuando arme un filtro y clickeo en algo, cuando regreso debería
volver a la pantalla anterior con el filtro incluido. Ejemplo si acá clickeo
cualquier factura y luego vuelvo, debería funcionar."*

Dos familias de test acá:

  · el CONTRATO de volver.py — de dónde saca el destino y, sobre todo, qué
    rechaza (es entrada no confiable: `?volver=` y `Referer` los elige quien
    manda el link);
  · el BARRIDO: que ningún template haya quedado con un "← Volver" fijo. Es
    el mismo criterio que `test_historial_links_resuelven.py` — el bug
    original era invisible desde el código porque estaba repartido en 44
    strings a mano, así que el test tiene que recorrerlos TODOS, no el que la
    dueña pisó.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import volver as V  # noqa: E402

HISTORIAL_FILTRADO = "/historial?q=bed&tipo=&estado=Todos"


# ─────────────────────── el contrato de volver_a() ────────────────────────

def test_el_referrer_filtrado_gana_sobre_el_destino_fijo(app):
    """EL caso de la dueña: Historial?q=bed → factura → Volver."""
    with app.test_request_context(
        "/facturas/10863",
        headers={"Referer": f"http://localhost{HISTORIAL_FILTRADO}"},
    ):
        assert V.volver_a("/facturas") == HISTORIAL_FILTRADO


def test_el_parametro_volver_gana_sobre_el_referrer(app):
    """`?volver=` es explícito: lo puso el listado, vale más que el header."""
    with app.test_request_context(
        f"/facturas/10863?volver={quote(HISTORIAL_FILTRADO, safe='')}",
        headers={"Referer": "http://localhost/cheques"},
    ):
        assert V.volver_a("/facturas") == HISTORIAL_FILTRADO


def test_sin_referrer_ni_volver_cae_al_destino_fijo(app):
    """Link pegado o pestaña nueva: el botón sigue funcionando como antes."""
    with app.test_request_context("/facturas/10863"):
        assert V.volver_a("/facturas") == "/facturas"


def test_no_vuelve_a_uno_mismo(app):
    """Después de un POST que redirige a la ficha, el referrer ES la ficha.

    Sin este corte el botón "Volver" recargaría la misma pantalla — que es
    justamente lo que se siente como "el botón no hace nada".
    """
    with app.test_request_context(
        "/facturas/10863",
        headers={"Referer": "http://localhost/facturas/10863"},
    ):
        assert V.volver_a("/facturas") == "/facturas"


def test_el_querystring_del_referrer_se_conserva_entero(app):
    """No alcanza con volver a /historial: el filtro vive en el querystring."""
    crudo = "/historial?q=bed&desde=2026-08-01&hasta=2026-08-17&tipo=Factura+emitida"
    with app.test_request_context(
        "/facturas/10863", headers={"Referer": f"http://localhost{crudo}"}
    ):
        assert V.volver_a("/facturas") == crudo


# ─────────────────── qué RECHAZA (entrada no confiable) ───────────────────

@pytest.mark.parametrize("hostil", [
    "//evil.com/phishing",          # protocol-relative: el clásico open-redirect
    "https://evil.com/phishing",    # host ajeno
    "http://localhost.evil.com/x",  # host que sólo se PARECE al nuestro
    "javascript:alert(1)",          # esquema ejecutable
    "/historial\r\nSet-Cookie: a=b",  # inyección de header
    "facturas",                     # relativa sin barra
    "",
    None,
])
def test_no_acepta_destinos_hostiles(app, hostil):
    with app.test_request_context("/facturas/10863"):
        assert V.destino_seguro(hostil) is None
        assert V.volver_a("/facturas") == "/facturas"


def test_no_acepta_una_pantalla_que_no_existe(app):
    """Un `?volver=` a una ruta inventada mandaría a la dueña a un 404."""
    with app.test_request_context("/facturas/10863?volver=/pantalla-inventada"):
        assert V.volver_a("/facturas") == "/facturas"


def test_no_acepta_una_ruta_que_solo_acepta_post(app):
    """Volver por GET a un endpoint POST-only da 405, no la pantalla anterior."""
    solo_post = [
        str(r.rule) for r in app.url_map.iter_rules()
        if "POST" in (r.methods or set()) and "GET" not in (r.methods or set())
        and "<" not in str(r.rule)
    ]
    if not solo_post:
        pytest.skip("no hay rutas POST-only sin parámetros")
    with app.test_request_context(f"/facturas/10863?volver={solo_post[0]}"):
        assert V.volver_a("/facturas") == "/facturas"


# ───────────────────────── la etiqueta no miente ──────────────────────────

def test_si_el_destino_no_cambio_el_texto_queda_igual(app):
    """El cartel se mueve sólo cuando el destino se movió."""
    with app.test_request_context("/facturas/10863"):
        assert V.volver_texto("/facturas", "← Volver a Facturas") == "← Volver a Facturas"


def test_si_el_destino_cambio_el_texto_lo_dice(app):
    with app.test_request_context(
        "/facturas/10863",
        headers={"Referer": f"http://localhost{HISTORIAL_FILTRADO}"},
    ):
        assert V.volver_texto("/facturas", "← Volver") == "← Volver al Historial"


def test_un_destino_desconocido_no_inventa_nombre(app):
    """Preferimos "← Volver" a secas antes que un nombre inventado."""
    with app.test_request_context(
        "/facturas/10863", headers={"Referer": "http://localhost/usuarios/accesos"}
    ):
        assert V.volver_texto("/facturas", "← Volver") == "← Volver"


def test_los_nombres_del_mapa_apuntan_a_secciones_que_existen(app):
    """Si alguien renombra una sección, el mapa de nombres no puede quedar viejo.

    Se chequea que EXISTA alguna ruta bajo ese prefijo, no que el prefijo sea
    una pantalla en sí: `/cartera` y `/conciliacion` son secciones (sus
    pantallas son `/cartera/aging`, `/conciliacion/banco-v2`), y el nombre se
    busca por el primer tramo del path justamente para cubrirlas.
    """
    rutas = [str(r.rule) for r in app.url_map.iter_rules()]
    huerfanas = [
        raiz for raiz in V.NOMBRE_DE_PANTALLA
        if not any(r == raiz or r.startswith(raiz + "/") for r in rutas)
    ]
    assert huerfanas == [], f"NOMBRE_DE_PANTALLA nombra secciones inexistentes: {huerfanas}"


# ──────────────────────────── el barrido ──────────────────────────────────

#: Botones que a propósito NO son "volver a la pantalla anterior": son
#: CANCELAR una acción (quedarse en el flujo) o salir de un modo. Si mañana
#: alguien agrega uno nuevo, este test lo va a marcar y hay que decidir a
#: conciencia en cuál de las dos listas va.
CANCELAR_NO_ES_VOLVER = {
    "nuevo_confirmar.html",              # ← Volver al form (conserva lo tipeado)
    "banco_v2_preview.html",             # ← Volver sin confirmar
    "banco_v2_impuestos_preview.html",   # ← Volver sin confirmar
    "banco_v2_diferencia_preview.html",  # ← Volver sin agregar
    "cargar_preview.html",               # ← Volver sin cargar
    "confirmar_impersonate.html",        # ← Volver a mi cuenta (salir del "ver como")
    "base.html",                         # ídem, el cartel del "ver como"
    "lista.html",                        # /compras: "← Volver" = LIMPIAR el filtro
}

ANCLA_VOLVER = re.compile(
    r'<a\s+href="\{\{\s*(?P<href>[^"]*?)\s*\}\}"(?P<attrs>(?:(?!</a>)(?!<a\b).)*?)>'
    r'(?P<label>(?:(?!</a>)(?!<a\b).)*?)</a>',
    re.S,
)


def _anclas_volver():
    raiz = Path(_REPO_ROOT)
    for f in sorted(raiz.rglob("*.html")):
        if ".venv" in f.parts or "node_modules" in f.parts:
            continue
        txt = f.read_text(encoding="utf-8")
        if "Volver" not in txt:
            continue
        for m in ANCLA_VOLVER.finditer(txt):
            label = re.sub(r"\s+", " ", m.group("label")).strip()
            if not re.match(r"^(?:&larr;|←)\s*Volver\b", label):
                continue
            yield f, m.group("href"), label


def test_ningun_boton_volver_quedo_con_destino_fijo():
    """Barrido: todo "← Volver" pasa por volver_a(), salvo los CANCELAR.

    Éste es el test que hace que el arreglo no se despinte. Los botones son
    strings a mano repartidos en 40 archivos: sin un barrido, el próximo que
    alguien agregue nace con el bug y nadie se entera hasta que la dueña lo
    clickea.
    """
    fijos = [
        f"{f.name}: {label}"
        for f, href, label in _anclas_volver()
        if f.name not in CANCELAR_NO_ES_VOLVER and "volver_a(" not in href
    ]
    assert fijos == [], (
        "estos '← Volver' vuelven a un destino fijo y pierden el filtro:\n  "
        + "\n  ".join(fijos)
    )


def test_los_cancelar_siguen_teniendo_destino_fijo():
    """La contracara: que el barrido no se lleve puesto un "Cancelar".

    "Volver sin confirmar" tiene que quedarse en su flujo. Si algún día pasa
    por volver_a(), es un bug: cancelar una carga te mandaría a cualquier lado.
    """
    movidos = [
        f"{f.name}: {label}"
        for f, href, label in _anclas_volver()
        if f.name in CANCELAR_NO_ES_VOLVER and "volver_a(" in href
    ]
    assert movidos == [], f"estos 'Cancelar' se volvieron navegación: {movidos}"
