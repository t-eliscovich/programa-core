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
