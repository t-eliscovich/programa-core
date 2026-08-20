"""Grupos de clientes — columna Grupo, lapicito y carga masiva (TMT 2026-08-19).

La dueña trajo las fotos del cuaderno de la oficina: *"Estos clientes son de un
mismo grupo. en clientes una columna mas que diga grupo y ponemos el primero
codigo que aparece en lista. Esto tiene que ser editable. En la pantalla de
cliente abajo del RUC dice grupo: y todos los codigos que son de su mismo
grupo. Usar tambien para impresion por grupos, estos clientes juntos"*.

`scintela.grupo_cliente` ya existía y ya la leían `/cartera/grupos` y
`/informes/estado-cuenta/grupos`; lo que no existía era CÓMO CARGARLA (se hacía
por SQL a mano). Contrato que fijan estos tests:

  * El parser del xlsx lee GRUPO + CÓDIGO, acepta y descarta la fila del propio
    cabeza (GRUPO == CÓDIGO), y ante un código en DOS grupos distintos no elige
    ninguno.
  * `_raiz` aplana: nada de nietos, y una cadena circular no cuelga el request.
  * Sin `grupos.editar`, las tres puertas (lapicito, carga masiva, aplicar) son
    404.
"""
from __future__ import annotations

import io
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.clientes.grupos import _raiz  # noqa: E402
from modules.clientes.grupos_xlsx import parse_grupos_xlsx  # noqa: E402


def _xlsx(filas: list[list], headers: list[str] | None = None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["grupos de clientes — cuaderno de la oficina"])  # fila decorativa
    ws.append(headers or ["GRUPO", "CÓDIGO", "CLIENTE", "REVISAR"])
    for f in filas:
        ws.append(f)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- parser ---

def test_parser_lee_grupo_y_codigo_y_descarta_la_fila_del_cabeza():
    data = _xlsx([
        ["ECH", "ECH", "ECHEVERRIA CARLA", ""],   # el cabeza: se acepta y se ignora
        ["ECH", "ALX", "ALOMIA ALEX", ""],
        ["ECH", "BRO", "BRAVO ROSA", ""],
    ])
    filas, avisos = parse_grupos_xlsx(data)
    assert [(f.grupo, f.codigo) for f in filas] == [("ECH", "ALX"), ("ECH", "BRO")]
    assert avisos == []


def test_parser_normaliza_a_mayusculas_y_saltea_la_fila_incompleta():
    data = _xlsx([
        ["ech", " alx ", "", ""],
        ["", "BRO", "sin grupo", ""],
        [None, None, None, None],   # fila en blanco: separador, no es un error
    ])
    filas, avisos = parse_grupos_xlsx(data)
    assert [(f.grupo, f.codigo) for f in filas] == [("ECH", "ALX")]
    assert len(avisos) == 1 and "BRO" not in avisos[0]


def test_parser_codigo_en_dos_grupos_no_carga_ninguno():
    """El caso que hace daño de verdad.

    Si el mismo cliente aparece en dos grupos y elegimos uno, su saldo se
    imprime en el grupo equivocado y se suma al total del que no es — y nadie
    se entera, porque la hoja sale bien formada. Se salta y se avisa.
    """
    data = _xlsx([
        ["ECH", "ALX", "", ""],
        ["EEC", "ALX", "", ""],
        ["ECH", "BRO", "", ""],
    ])
    filas, avisos = parse_grupos_xlsx(data)
    assert [(f.grupo, f.codigo) for f in filas] == [("ECH", "BRO")]
    assert any("ALX" in a for a in avisos)


def test_parser_fila_duplicada_identica_no_es_un_aviso():
    data = _xlsx([["ECH", "ALX", "", ""], ["ECH", "ALX", "", ""]])
    filas, avisos = parse_grupos_xlsx(data)
    assert [(f.grupo, f.codigo) for f in filas] == [("ECH", "ALX")]
    assert avisos == []


def test_parser_sin_encabezados_no_inventa_nada():
    data = _xlsx([["ECH", "ALX"]], headers=["PADRE", "HIJO"])
    filas, avisos = parse_grupos_xlsx(data)
    assert filas == []
    assert avisos and "encabezados" in avisos[0].lower()


def test_parser_archivo_que_no_es_xlsx():
    filas, avisos = parse_grupos_xlsx(b"esto no es un xlsx")
    assert filas == []
    assert avisos


# ------------------------------------------------------------------ raiz ---

def test_raiz_aplana_la_cadena():
    """A->B->C tiene que resolver a C.

    Importa porque las dos consultas que YA leen esta tabla
    (`cartera.aging_por_grupo`, `informes.estado_cuenta_clientes_saldos`) dan
    UN solo salto: con una cadena de dos, A imprimiría en el grupo B y B en el
    grupo C, o sea la misma casa partida en dos hojas.
    """
    padres = {"A": "B", "B": "C"}
    assert _raiz("A", padres) == "C"
    assert _raiz("B", padres) == "C"
    assert _raiz("C", padres) == "C"


def test_raiz_no_cuelga_con_un_ciclo():
    """La tabla se cargó a mano durante meses: A->B y B->A es posible.

    Sin el freno de ciclo esto es un `while True` adentro de un request: la
    pantalla no da error, se queda colgada hasta el timeout.
    """
    assert _raiz("A", {"A": "B", "B": "A"}) in ("A", "B")
    assert _raiz("A", {"A": "A"}) == "A"


# ---------------------------------------------------------------- permiso ---

def test_sin_permiso_grupos_editar_es_404():
    """Mover un cliente de grupo mueve plata de una hoja impresa a otra.

    Por eso tiene permiso propio y no cuelga de `clientes.editar`: la dueña
    puede cerrarlo sin cerrar toda la ficha del cliente. El 404 (y no el 403)
    es la convención de `@requiere_permiso` en toda la app.
    """
    from tests.test_routes_smoke import build_app

    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        # Pisar auth.load_logged_in_user acá NO sirve: app.py lo importó por
        # valor en el import (gotcha documentado). Un before_request propio
        # registrado DESPUÉS de create_app() corre último y pisa g.permisos.
        @app.before_request
        def _como_int_sin_grupos():
            from flask import g
            g.user = {"id_usuario": 2, "username": "int", "id_rol": 3,
                      "nombre_rol": "INT", "activo": True}
            g.permisos = {"clientes.ver", "clientes.editar"}

        c = app.test_client()
        assert c.get("/clientes/grupos-carga").status_code == 404
        assert c.post("/clientes/grupos-carga").status_code == 404
        assert c.post("/clientes/grupos-carga/aplicar").status_code == 404
        assert c.post("/clientes/ECH/grupo", data={"grupo": "EEC"}).status_code == 404
        # Y el listado NO le muestra ni el link de la carga masiva ni el
        # lapicito de la columna (gatear los LINKS, no sólo la ruta).
        html = c.get("/clientes").data.decode()
        assert "grupos-carga" not in html
        assert "cli-grupo-form" not in html
        # La pantalla de grupos SÍ se ve (es `clientes.ver`): saber quién está
        # con quién es parte de mirar la cartera. Lo que no aparece es cómo
        # tocarlo.
        html_g = c.get("/clientes/grupos").data.decode()
        # Se chequea el ACTION del form, no la clase CSS: el `<style>` de la
        # pantalla define `.grp-add` para todos (es una hoja sola) y buscar la
        # clase daría un falso positivo — el test pasaría a medir el CSS en vez
        # de medir quién puede escribir.
        assert "/agregar" not in html_g
        assert "/grupo\"" not in html_g
        assert c.post("/clientes/grupos/ECH/agregar", data={"cod": "ALX"}).status_code == 404
    finally:
        deshacer()


def test_el_listado_linkea_la_pantalla_de_grupos_y_no_la_carga_masiva():
    from tests.test_routes_smoke import build_app

    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        @app.before_request
        def _login_wildcard():
            from flask import g
            g.user = {"id_usuario": 1, "username": "test", "id_rol": 1,
                      "nombre_rol": "Dueño", "activo": True}
            g.permisos = {"*"}

        c = app.test_client()
        assert c.get("/clientes/grupos-carga").status_code == 200
        html = c.get("/clientes").data.decode()
        # TMT 2026-08-19 (dueña): *"¿podés eliminar carga masiva de grupos? que
        # sirva solo para vos eso"*. La ruta sigue viva —es como se cargó el
        # cuaderno entero— pero NO se ofrece en la pantalla ni siquiera a quien
        # tiene el permiso. Lo que se linkea es la pantalla de grupos.
        assert "grupos-carga" not in html
        assert "/clientes/grupos" in html
        # La columna existe SIEMPRE, aunque la lista venga vacía: si el
        # encabezado dependiera de que haya filas, la pantalla sin resultados
        # parecería no tener la columna.
        assert ">Grupo</th>" in html
        assert c.get("/clientes/grupos").status_code == 200
    finally:
        deshacer()


def test_aplicar_payload_invalido_es_400(monkeypatch):
    """El payload del Confirmar se re-valida línea a línea.

    Es un campo hidden: llega tal cual lo mande el navegador. Sólo COD=COD.
    """
    from tests.test_routes_smoke import build_app

    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        @app.before_request
        def _login_wildcard2():
            from flask import g
            g.user = {"id_usuario": 1, "username": "test", "id_rol": 1,
                      "nombre_rol": "Dueño", "activo": True}
            g.permisos = {"*"}

        from modules.clientes import views
        monkeypatch.setattr(
            views.grupos_mod, "asignar",
            lambda *a, **k: pytest.fail("no tiene que escribir"),
        )
        c = app.test_client()
        r = c.post("/clientes/grupos-carga/aplicar",
                   data={"payload": "ALX=ECH; DROP TABLE"})
        assert r.status_code == 400
    finally:
        deshacer()


# ---------------------------------------------------------------------------
# TMT 2026-08-20 (dueña), tres pedidos sobre lo cargado el 19:
#   1. *"no puedo editar cuando es solo un cliente"*  -> el recuadro recortaba
#   2. *"dejar cargar nuevos grupos manualmente... elige la cabeza él"*
#   3. *"ordena tambien la cabeza siempre arriba en los grupos"*
# ---------------------------------------------------------------------------


def _app_wildcard():
    from tests.test_routes_smoke import build_app

    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_wildcard_20():
        from flask import g
        g.user = {"id_usuario": 1, "username": "test", "id_rol": 1,
                  "nombre_rol": "Dueño", "activo": True}
        g.permisos = {"*"}

    return app, deshacer


def test_la_tabla_de_clientes_no_recorta_el_campito_del_lapicito():
    """Con UNA sola fila el campito quedaba cortado por la mitad.

    El form del lapicito se abre `position:absolute` DEBAJO de la fila. El
    recuadro de la tabla tenía `overflow-hidden`: con muchas filas el campito
    caía sobre las de abajo y se veía igual, pero buscando un cliente por su
    código —que es como se usa la pantalla— la tabla mide una fila y el
    recuadro le cortaba la mitad. El test mira el recuadro que envuelve a la
    tabla, no la pantalla entera: el sidebar también tiene `overflow-hidden` y
    ese sí va.
    """
    import re

    app, deshacer = _app_wildcard()
    try:
        html = app.test_client().get("/clientes").data.decode()
        m = re.search(r'<div class="([^"]*)">\s*<table class="[^"]*cli-tabla', html)
        assert m, "no encontré el recuadro que envuelve la tabla de clientes"
        assert "overflow-hidden" not in m.group(1)
    finally:
        deshacer()


def test_el_campito_del_lapicito_no_flota():
    """*"se sigue sin ver el editar (no quiero scrollear)"* (dueña, 20/08).

    Flotando (`position:absolute`) el campito depende de que nada lo recorte y
    de que la fila no esté contra el borde de la pantalla. Ahora abre al lado
    del código, en el flujo normal de la celda: no hay forma de que quede
    afuera de la vista.
    """
    app, deshacer = _app_wildcard()
    try:
        html = app.test_client().get("/clientes").data.decode()
        bloque = html.split(".cli-grupo-form {")[1].split("}")[0]
        assert "position: absolute" not in bloque
        assert "inline-flex" in bloque
    finally:
        deshacer()


def test_la_cabeza_va_siempre_primera(monkeypatch):
    """Aunque su código sea el último del abecedario.

    ZUR es la cabeza y AZU un integrante: por código puro AZU iba primero y la
    etiqueta «cabeza» quedaba en el medio de la lista.
    """
    from modules.clientes import grupos as grupos_mod

    monkeypatch.setattr(grupos_mod.db, "fetch_all", lambda *a, **k: [
        {"padre": "ZUR", "hijo": "AZU", "nombre_padre": "ZURITA MARIA",
         "nombre_hijo": "ZURITA ANDRADE"},
        {"padre": "ZUR", "hijo": "ZUB", "nombre_padre": "ZURITA MARIA",
         "nombre_hijo": "ZURITA BENITEZ"},
    ])
    (grupo,) = grupos_mod.todos_los_grupos()
    assert [i["codigo"] for i in grupo["integrantes"]] == ["ZUR", "AZU", "ZUB"]
    assert grupo["integrantes"][0]["es_padre"] is True


def test_grupo_nuevo_escribe_por_el_mismo_camino(monkeypatch):
    """El alta de un grupo nuevo pega contra `asignar`, no contra la tabla.

    Así hereda las validaciones (los dos códigos existen, sin nietos) en vez de
    tener las suyas propias, que es como se desincronizan.
    """
    from modules.clientes import views

    escrito = []
    monkeypatch.setattr(views.grupos_mod, "grupo_de", lambda cod: None)
    monkeypatch.setattr(
        views.grupos_mod, "asignar",
        lambda hijo, padre, usuario="web": (escrito.append((hijo, padre)), (True, "ok"))[1],
    )
    app, deshacer = _app_wildcard()
    try:
        c = app.test_client()
        r = c.post("/clientes/grupos/nuevo", data={"cabeza": "zur", "cod": "azu"})
        assert r.status_code == 302
        assert escrito == [("AZU", "ZUR")]
    finally:
        deshacer()


def test_grupo_nuevo_avisa_si_el_grupo_ya_existe(monkeypatch):
    """*"si pongo cabeza que exista me diga, este ya existe"* (dueña, 20/08).

    Antes lo agregaba callada al grupo que ya estaba: la pantalla no mentía
    —el cliente quedaba bien— pero ella creía haber armado un grupo nuevo. Y
    si la cabeza que escribe está ADENTRO de otro grupo, tampoco se escribe:
    `asignar` lo aplanaría a la raíz y el grupo terminaría llamándose distinto
    de lo que ella tipeó.
    """
    from modules.clientes import views

    monkeypatch.setattr(
        views.grupos_mod, "asignar",
        lambda *a, **k: pytest.fail("no tiene que escribir"),
    )
    app, deshacer = _app_wildcard()
    try:
        c = app.test_client()
        monkeypatch.setattr(views.grupos_mod, "grupo_de", lambda cod: "PUE")
        with c:
            r = c.post("/clientes/grupos/nuevo",
                       data={"cabeza": "PUE", "cod": "JN1"},
                       follow_redirects=False)
            from flask import get_flashed_messages
            msgs = " ".join(get_flashed_messages())
        assert r.status_code == 302
        assert "ya existe" in msgs

        monkeypatch.setattr(views.grupos_mod, "grupo_de", lambda cod: "ZUR")
        with c:
            c.post("/clientes/grupos/nuevo", data={"cabeza": "AZU", "cod": "JN1"})
            from flask import get_flashed_messages
            msgs2 = " ".join(get_flashed_messages())
        assert "ya está adentro del grupo ZUR" in msgs2
    finally:
        deshacer()


def test_grupo_nuevo_con_la_misma_cabeza_y_cliente_no_escribe(monkeypatch):
    """Un grupo de uno solo no es un grupo — y `asignar` lo rebotaría igual,
    pero con un mensaje que habla de padres e hijos."""
    from modules.clientes import views

    monkeypatch.setattr(
        views.grupos_mod, "asignar",
        lambda *a, **k: pytest.fail("no tiene que escribir"),
    )
    monkeypatch.setattr(
        views.grupos_mod, "quitar",
        lambda *a, **k: pytest.fail("no tiene que escribir"),
    )
    monkeypatch.setattr(views.grupos_mod, "grupo_de", lambda cod: None)
    app, deshacer = _app_wildcard()
    try:
        c = app.test_client()
        assert c.post("/clientes/grupos/nuevo",
                      data={"cabeza": "ABC", "cod": "abc"}).status_code == 302
        assert c.post("/clientes/grupos/nuevo",
                      data={"cabeza": "", "cod": "ABC"}).status_code == 302
        assert c.post("/clientes/grupos/nuevo",
                      data={"cabeza": "ABC", "cod": ""}).status_code == 302
    finally:
        deshacer()


def test_grupo_nuevo_sin_permiso_es_404():
    from tests.test_routes_smoke import build_app

    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        @app.before_request
        def _como_int_sin_grupos_20():
            from flask import g
            g.user = {"id_usuario": 2, "username": "int", "id_rol": 3,
                      "nombre_rol": "INT", "activo": True}
            g.permisos = {"clientes.ver"}

        c = app.test_client()
        assert c.post("/clientes/grupos/nuevo",
                      data={"cabeza": "ABC", "cod": "DEF"}).status_code == 404
        # Y el form tampoco se le ofrece.
        assert "grupos/nuevo" not in c.get("/clientes/grupos").data.decode()
    finally:
        deshacer()
