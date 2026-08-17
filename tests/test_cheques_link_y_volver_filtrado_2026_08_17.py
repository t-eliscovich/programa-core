"""El cheque del listado es un LINK, y volver devuelve al MISMO filtro.

Pedido de la dueña (17/08/2026):

    "En la pantalla de cheques, que cada cheque sea un link. Y si filtré por
     un cliente ejemplo BED pueda abrir el cheque en otra pestaña. También
     que cuando pongo volver a cheques o flechita para atrás me lleve de
     vuelta a lo filtrado, o sea BED, no todos los cheques."

Dos cosas distintas:

1. LINK DE VERDAD. La fila ya "navegaba" al detalle, pero por un listener de
   JS sobre `data-href` — no había ningún `<a href>`. Con eso NO andan el
   botón del medio, ni "abrir en pestaña nueva" del menú derecho, ni "copiar
   dirección del enlace". Ahora cada fila tiene anchors reales.

2. VOLVER AL FILTRO. El detalle mandaba a `/cheques` pelado. Abierto en una
   PESTAÑA NUEVA no hay historial que la flechita del navegador pueda
   desandar, así que el filtro se perdía sí o sí. El listado ahora le cuelga
   `?volver=<su propia URL>` al link y el detalle lo usa para el breadcrumb y
   el botón "← Volver a Cheques".
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
LISTA = (RAIZ / "modules/cheques/templates/cheques/lista.html").read_text(encoding="utf-8")
DETALLE = (RAIZ / "modules/cheques/templates/cheques/detalle.html").read_text(encoding="utf-8")

HOY = date.today()
CLI = "LKV"  # cliente propio de este archivo (no pisa al 'UIU' de los otros)


# ── 1. La URL de vuelta: qué acepta y qué no ──────────────────────────
@pytest.mark.parametrize(
    "crudo, esperado",
    [
        ("/cheques?cliente=BED", "/cheques?cliente=BED"),
        ("/cheques?cliente=BED&estado=todos&page=2", "/cheques?cliente=BED&estado=todos&page=2"),
        ("/cheques", "/cheques"),
        ("  /cheques?cliente=BED  ", "/cheques?cliente=BED"),
    ],
)
def test_volver_conserva_el_filtro_del_listado(crudo, esperado):
    from app import create_app
    from modules.cheques.views import _volver_a_lista

    with create_app().test_request_context("/"):
        assert _volver_a_lista(crudo) == esperado


@pytest.mark.parametrize(
    "crudo",
    [
        None,
        "",
        "   ",
        "https://malicioso.example/cheques",   # absoluta
        "//malicioso.example/cheques",         # protocol-relative
        "/facturas?cliente=BED",               # otra pantalla
        "/cheques/123",                        # el detalle, no el listado
        "javascript:alert(1)",
    ],
)
def test_volver_cae_al_listado_pelado_si_no_es_el_listado(crudo):
    """Nunca mandamos a un destino que no sea ESTE listado.

    Es un `href` que se le pinta al usuario: si aceptáramos cualquier cosa,
    un link armado a mano convertiría el botón "Volver" en una redirección a
    un sitio ajeno.
    """
    from app import create_app
    from modules.cheques.views import _volver_a_lista

    with create_app().test_request_context("/"):
        assert _volver_a_lista(crudo) == "/cheques"


# ── 2. Los templates: anchors de verdad, no sólo data-href ────────────
def test_la_fila_del_listado_arma_la_url_con_volver():
    assert "{% set _volver = request.full_path.rstrip('?') %}" in LISTA
    assert (
        "{% set _url_cheque = url_for('cheques.detalle', id_cheque=c.id_cheque, volver=_volver) %}"
        in LISTA
    )
    assert 'data-href="{{ _url_cheque }}"' in LISTA


def test_cada_fila_tiene_al_menos_dos_anchors_reales():
    """`data-href` + JS no alcanza: tiene que haber `<a href>`.

    Uno en la fecha (columna que existe SIEMPRE) y otro pegado al N° de
    cheque — que en los estados editables es un `<input>`, así que ahí el
    link es la flechita ↗ de al lado.
    """
    anchors = re.findall(r'<a\s+href="\{\{\s*_url_cheque\s*\}\}"', LISTA)
    assert len(anchors) >= 2, f"esperaba ≥2 <a href> por fila, encontré {len(anchors)}"


def test_el_detalle_vuelve_por_volver_url_y_no_al_listado_pelado():
    assert '<a href="{{ volver_url }}" class="hover:underline">Cheques</a>' in DETALLE
    assert 'href="{{ volver_url }}"' in DETALLE
    # Ningún link de "volver" puede seguir apuntando al listado sin filtro.
    assert "url_for('cheques.lista')" not in DETALLE


def test_guardar_una_edicion_no_pierde_el_filtro():
    """Los `next` del detalle vuelven a ESTA pantalla arrastrando el ?volver=."""
    assert (
        "{% set _self = url_for('cheques.detalle', id_cheque=ch.id_cheque, "
        "volver=request.args.get('volver') or None) %}" in DETALLE
    )
    assert 'name="next" value="{{ _self }}"' in DETALLE


# ── 3. Punta a punta contra Postgres real ─────────────────────────────
@pytest.fixture
def real_app(migrated_db):
    from app import create_app
    from extensions import limiter

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    limiter.enabled = False
    return app


@pytest.fixture
def auth_client(real_app, real_db_conn):
    import datetime as _dt

    cur = real_db_conn.cursor()
    cur.execute(
        "INSERT INTO seguridad.rol (nombre_rol, descripcion) VALUES ('test-link','t') "
        "ON CONFLICT (nombre_rol) DO UPDATE SET descripcion=EXCLUDED.descripcion RETURNING id_rol"
    )
    id_rol = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s,'*') ON CONFLICT DO NOTHING",
        (id_rol,),
    )
    cur.execute(
        "INSERT INTO seguridad.usuario (username, password_hash, id_rol, activo) "
        "VALUES ('linktest','x',%s,TRUE) "
        "ON CONFLICT (username) DO UPDATE SET id_rol=EXCLUDED.id_rol, activo=TRUE RETURNING id_usuario",
        (id_rol,),
    )
    id_usuario = cur.fetchone()[0]
    real_db_conn.commit()

    client = real_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = id_usuario
        sess["last_activity"] = _dt.datetime.now(_dt.UTC).isoformat()
    return client


@pytest.fixture
def cheque_del_cliente(real_app):
    import db

    db.execute(
        "INSERT INTO scintela.cliente (codigo_cli, nombre, stop, pago, cupo, activo) "
        "VALUES (%s,'LINK test','N',30,100000,TRUE) ON CONFLICT (codigo_cli) DO NOTHING",
        (CLI,),
    )
    db.execute(
        "INSERT INTO scintela.banco (no_banco, nombre) VALUES (1,'PICHINCHA') ON CONFLICT DO NOTHING"
    )
    row = db.execute_returning(
        "INSERT INTO scintela.cheque (fecha, no_cheque, codigo_cli, importe, fechad, stat, no_banco, pasaconta) "
        "VALUES (%s,'L0001',%s,777.77,%s,'Z',1,'0') RETURNING id_cheque",
        (HOY, CLI, HOY - timedelta(days=1)),
    )
    id_cheque = row["id_cheque"]
    yield id_cheque
    db.execute("DELETE FROM scintela.cheque WHERE id_cheque=%s", (id_cheque,))


@pytest.mark.db
def test_listado_filtrado_pinta_un_anchor_abrible_en_otra_pestania(auth_client, cheque_del_cliente):
    html = auth_client.get(f"/cheques?cliente={CLI}").get_data(as_text=True)
    # Un <a href> de verdad (no un data-href) apuntando al detalle del cheque.
    assert re.search(
        rf'<a\s+href="/cheques/{cheque_del_cliente}\?volver=[^"]*cliente[^"]*"', html
    ), "la fila no trae un <a href> al detalle con el filtro colgado"


@pytest.mark.db
def test_el_detalle_abierto_desde_el_filtro_vuelve_al_filtro(auth_client, cheque_del_cliente):
    html = auth_client.get(
        f"/cheques/{cheque_del_cliente}", query_string={"volver": f"/cheques?cliente={CLI}"}
    ).get_data(as_text=True)
    assert f'href="/cheques?cliente={CLI}"' in html
    assert "Volver a Cheques" in html


@pytest.mark.db
def test_el_detalle_entrado_a_mano_sigue_volviendo_al_listado(auth_client, cheque_del_cliente):
    """Sin ?volver= (link viejo, favorito, historial) no se rompe nada."""
    html = auth_client.get(f"/cheques/{cheque_del_cliente}").get_data(as_text=True)
    assert 'href="/cheques"' in html
