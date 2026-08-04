"""Imprimir la lista de Conciliados desde la propia pantalla de conciliación.

TMT 2026-08-04 (dueña, apenas cerró su primera conciliación entera):

    *"necesito imprimir todo lo conciliado … para tener de respaldo y saber
    rapidamente si ya fue o no conciliado"*

Primero se le armó una pantalla aparte para eso. La respuesta fue clara:

    *"de ninguna manera. no me armes otro lugar para imprimir … quiero
    imprimir desde la pantalla de conciliados. hacelo facil simple"*

Así que no hay endpoint nuevo ni hoja nueva: se imprime la pestaña
Conciliados tal como está. Lo único que hace falta es que el papel salga
ENTERO, y de eso se ocupa el `@media print` del propio tab.

🚨 EL BUG QUE ESTOS TESTS CUIDAN: el contenedor de la tabla tiene
`max-height: calc(100vh - 380px)` con scroll. Al imprimir, el navegador
recorta a lo que entra en la caja — una hoja con ~15 filas de 250 y ningún
aviso de que faltan las otras 235. Ya pasó igual en resumen-dia y en
estado-cuenta (nota del 2026-07-07 en base.html). Un respaldo incompleto
que parece completo es peor que no tener respaldo.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TAB = os.path.join(
    _REPO_ROOT, "modules", "conciliacion", "templates", "conciliacion",
    "_banco_v2_tab_conciliados.html",
)


def _fuente() -> str:
    with open(_TAB, encoding="utf-8") as fh:
        return fh.read()


def _bloque_print() -> str:
    """El cuerpo del `@media print` del tab."""
    src = _fuente()
    i = src.index("@media print")
    return src[i:]


# ── El botón ──────────────────────────────────────────────────────────


def test_hay_un_boton_de_imprimir_en_la_pestana():
    """No hay pantalla aparte: el botón vive acá y llama a window.print()."""
    src = _fuente()
    assert "window.print()" in src, (
        "desapareció el botón de imprimir de la pestaña Conciliados")


def test_el_boton_no_sale_en_el_papel():
    """Un botón impreso es tinta al pedo. `no-print` lo saca."""
    src = _fuente()
    i = src.index("window.print()")
    boton = src[max(0, i - 400):i + 200]
    assert "no-print" in boton


# ── Lo que hace que el papel salga entero ─────────────────────────────


def test_la_caja_con_scroll_se_abre_al_imprimir():
    """🚨 Sin esto se imprime UNA hoja de 250 matches y nadie se entera."""
    src = _fuente()
    assert 'id="conciliados-scroll"' in src, (
        "el contenedor perdió su id — el @media print no lo puede abrir")
    bloque = _bloque_print()
    assert "#conciliados-scroll" in bloque
    # Las dos reglas: la altura Y el overflow. Con una sola no alcanza.
    reglas = bloque[bloque.index("#conciliados-scroll"):]
    assert re.search(r"max-height:\s*none\s*!important", reglas), (
        "sigue el max-height de pantalla al imprimir: el papel se corta en "
        "la primera hoja")
    assert re.search(r"overflow:\s*visible\s*!important", reglas), (
        "sin overflow:visible el contenido que quedó fuera de la caja no "
        "pagina — se recorta igual")


def test_la_hoja_sale_ACOSTADA():
    """Son 9 columnas. En vertical, o se corta la última fuera de la hoja o
    entran todas pero con los IMPORTES en "-13.483…" — el dato que no puede
    faltar en un respaldo. Se vio en el PDF de prueba antes de acostarla."""
    bloque = _bloque_print()
    assert re.search(r"@page\s*\{[^}]*landscape", bloque), (
        "la hoja volvió a vertical: los importes se imprimen truncados")


def test_los_anchos_de_columna_suman_100():
    """Con `table-layout: fixed`, los anchos de PANTALLA (w-24, w-20…) están
    en px y suman más que la hoja: el navegador los achica a todos por igual
    y el DOCUMENTO queda en "1225175…" — justo con lo que ella contesta
    "¿este ya fue conciliado?". Por eso se re-reparten en % — y si no suman
    100, alguna columna se come a otra."""
    bloque = _bloque_print()
    assert "table-layout: fixed" in bloque
    anchos = [int(m) for m in re.findall(
        r"#conciliados-scroll td:nth-child\(\d+\) \{ width: (\d+)%", bloque)]
    assert len(anchos) == 9, f"hay {len(anchos)} anchos y la tabla tiene 9 columnas"
    assert sum(anchos) == 100, f"los anchos suman {sum(anchos)}%, no 100%"


def test_las_filas_quedan_en_UNA_linea():
    """Dejando wrapear los conceptos, cada fila pasa a 4 líneas y 33 matches
    ocupan 4 hojas en vez de 1. Se imprimen truncados igual que en pantalla,
    a propósito."""
    bloque = _bloque_print()
    assert re.search(r"white-space:\s*nowrap\s*!important", bloque)
    assert re.search(r"text-overflow:\s*ellipsis\s*!important", bloque)


def test_los_TOTALES_se_imprimen():
    """🚨 base.html esconde TODOS los `<header>`, no sólo el de la app — y el
    título y los totales (banco / programa / diferencia) viven en el
    `<header>` de la tarjeta. Se imprimía la lista SIN totales."""
    src = _fuente()
    assert 'id="conciliados-card"' in src
    bloque = _bloque_print()
    assert re.search(
        r"#conciliados-card\s*>\s*header\s*\{\s*display:\s*block\s*!important",
        bloque), "el encabezado con los totales no se imprime"


def test_el_encabezado_se_repite_en_cada_hoja():
    """250 matches son varias hojas; sin thead repetido, de la 2 en adelante
    son columnas de números sin título."""
    assert "table-header-group" in _bloque_print()


def test_los_deshechos_NO_se_imprimen():
    """`base.html` ABRE los <details> al imprimir, así que el panel de
    deshechos saldría aunque esté cerrado — y es justo la lista opuesta a la
    que se está imprimiendo."""
    src = _fuente()
    i = src.index("Panel Deshechos")
    detalle = src[i:i + 400]
    assert "no-print" in detalle


# ── La pantalla, renderizada de verdad ────────────────────────────────


def _match(**kw):
    base = {
        "tipo": "match", "id": 1, "estado": "matched",
        "creado_en": datetime(2026, 8, 3, 9, 22), "usuario": "tamara",
        "confirm_batch_id": None, "real_fecha": date(2026, 7, 27),
        "real_documento": "122517504", "real_monto": 13483.12,
        "real_tipo": "D", "real_concepto": "PAGO SENAE 51782571",
        "id_transaccion": 5001, "tx_firma": "",
        "tb_fecha": date(2026, 7, 27), "tb_documento": "AC",
        "tb_importe": 13483.12, "tb_usuario_crea": "", "tb_numreferencia": "",
        "tb_concepto": "ANTICIPO AC 34/26", "tb_prov": "", "historico_id": None,
    }
    base.update(kw)
    return base


@pytest.fixture
def app_logueada():
    from tests.test_routes_smoke import build_app
    app, deshacer = build_app()

    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    try:
        yield app
    finally:
        deshacer()


def test_la_pestana_renderea_con_el_boton_y_el_estilo(app_logueada, monkeypatch):
    """End-to-end de plantilla: la pantalla real trae las dos cosas."""
    from modules.conciliacion import banco_v2_view as v

    monkeypatch.setattr(v._sesion, "sesion_abierta", lambda nb, usuario=None: {
        "id": 60, "no_banco": 10, "abierta_en": datetime(2026, 8, 3, 9, 22),
        "cerrada_en": None, "extracto_nombre": "mov.xlsx",
    })
    monkeypatch.setattr(v._sesion, "estado_sesion", lambda s, nb: {})
    monkeypatch.setattr(v._sesion, "cargar_movs", lambda s: [])
    monkeypatch.setattr(v._sesion, "matches_de_sesion", lambda s: [_match()])
    monkeypatch.setattr(v._sesion, "deshechos_de_sesion", lambda s: [])
    monkeypatch.setattr(v._bp, "calcular", lambda nb: {
        "saldo": 0.0, "saldo_si_concilio_todo": 0.0, "neto_pendientes": 0.0,
        "pendientes_banco_creditos": 0.0, "pendientes_banco_debitos": 0.0,
        "n_pendientes": 0,
    })
    monkeypatch.setattr(v._db, "fetch_one", lambda *a, **k: {"n": 1})
    monkeypatch.setattr(v._db, "fetch_all", lambda *a, **k: [])

    rv = app_logueada.test_client().get(
        "/conciliacion/banco-v2?sesion_id=60&tab=conciliados")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "window.print()" in body, "la pantalla no trae el botón de imprimir"
    assert "#conciliados-scroll" in body, "la pantalla no trae el @media print"
    assert "PAGO SENAE 51782571" in body


# ── La columna Usr: la CLAVE de 3 letras, no el nombre ────────────────


def test_la_columna_usr_muestra_la_clave_no_el_username():
    """TMT 2026-08-04 (dueña, mirando la hoja): *"pone clave TAM ALX etc no
    el nombre completo"*. La columna es de 3 letras: con el username crudo
    ("tamara") el papel salía cortado en "tam…"."""
    src = _fuente()
    assert "m.usuario_clave" in src, (
        "la columna Usr volvió al username crudo — en papel se corta")
    # En las DOS filas: la resumen del grupo y la individual.
    assert src.count("m.usuario_clave") == 2


def test_la_query_trae_la_clave_con_fallback():
    """⚠ El fallback importa: hay matches viejos de usuarios que no están en
    el set canónico (FED/TAM/ALX/ADR) y los del sync vienen sin usuario.
    Sin fallback la celda quedaría vacía y se perdería quién concilió."""
    import inspect

    from modules.conciliacion import sesion as _s

    sql = inspect.getsource(_s.matches_de_sesion)
    assert "usuario_clave" in sql
    assert "seguridad.usuario" in sql, "falta el JOIN que trae la clave"
    assert "LEFT JOIN seguridad.usuario" in sql, (
        "el JOIN tiene que ser LEFT: un match sin usuario en la tabla no "
        "puede desaparecer de la lista de conciliados")
    assert "LEFT(TRIM(COALESCE(m.usuario, '')), 3)" in sql, (
        "sin el fallback, los matches de usuarios no canónicos salen sin Usr")


def test_la_pestana_imprime_la_clave(app_logueada, monkeypatch):
    """Render real: llega "TAM" a la pantalla, no "tamara"."""
    from modules.conciliacion import banco_v2_view as v

    monkeypatch.setattr(v._sesion, "sesion_abierta", lambda nb, usuario=None: {
        "id": 60, "no_banco": 10, "abierta_en": datetime(2026, 8, 3, 9, 22),
        "cerrada_en": None, "extracto_nombre": "mov.xlsx",
    })
    monkeypatch.setattr(v._sesion, "estado_sesion", lambda s, nb: {})
    monkeypatch.setattr(v._sesion, "cargar_movs", lambda s: [])
    monkeypatch.setattr(v._sesion, "matches_de_sesion", lambda s: [
        _match(usuario="tamara", usuario_clave="TAM")])
    monkeypatch.setattr(v._sesion, "deshechos_de_sesion", lambda s: [])
    monkeypatch.setattr(v._bp, "calcular", lambda nb: {
        "saldo": 0.0, "saldo_si_concilio_todo": 0.0, "neto_pendientes": 0.0,
        "pendientes_banco_creditos": 0.0, "pendientes_banco_debitos": 0.0,
        "n_pendientes": 0,
    })
    monkeypatch.setattr(v._db, "fetch_one", lambda *a, **k: {"n": 1})
    monkeypatch.setattr(v._db, "fetch_all", lambda *a, **k: [])

    body = app_logueada.test_client().get(
        "/conciliacion/banco-v2?sesion_id=60&tab=conciliados"
    ).get_data(as_text=True)
    assert ">TAM" in body or "TAM\n" in body or "TAM " in body
    assert "tamara" not in body.split('id="conciliados-scroll"')[1], (
        "la tabla sigue mostrando el nombre completo")


# ── El encabezado del papel ───────────────────────────────────────────


def test_los_totales_salen_en_UNA_fila_de_tres():
    """base.html aplasta todos los `.grid` a `display:block`: las tres
    tarjetas de totales caían una abajo de la otra y cada una en tres
    renglones — doce líneas de números sueltos antes de la lista. Dueña
    2026-08-04: *"osea todo esto, lo podes hacer mas elegante"*."""
    bloque = _bloque_print()
    i = bloque.index("#conciliados-card > header .grid")
    regla = bloque[i:i + 200]
    assert re.search(r"display:\s*flex\s*!important", regla), (
        "los totales volvieron a caer uno abajo del otro")


def test_el_titulo_no_le_gana_al_resto():
    """Dueña: *"el titulo podria ser mas chico no?"*. base.html lo imprime en
    12pt; acá se baja. Y el rótulo de la lista tiene que quedar POR DEBAJO
    del título, si no la jerarquía se lee al revés."""
    bloque = _bloque_print()
    h1 = re.search(r"main h1 \{ font-size: (\d+(?:\.\d+)?)pt", bloque)
    rotulo = re.search(
        r"#conciliados-card > header strong \{ font-size: (\d+(?:\.\d+)?)pt",
        bloque)
    assert h1 and rotulo, "faltan los tamaños del encabezado impreso"
    assert float(h1.group(1)) < 12, "el título sigue en el 12pt de base.html"
    assert float(rotulo.group(1)) < float(h1.group(1))
