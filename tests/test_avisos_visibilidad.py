"""Quién ve cada novedad — la campanita para INT (TMT 2026-08-19).

Dueña: *"¿podés habilitar la campanita para INT? que vean despachos y facturas.
y que vean de cualquier notificación que sea páginas que ellos están
habilitados"*.

Lo que protegen estos tests:

· **El permiso sale de la RUTA, no de una lista escrita a mano.** Si mañana
  alguien cambia el decorador de una pantalla, la campanita se entera sola.
· **INT ve lo suyo y no ve lo que no puede abrir**: un aviso que lleva a un 404
  enseña a ignorar la campanita.
· **Los roles wildcard y los procesos de fondo no filtran nada** (la dueña
  sigue viendo todo, y el que escribe avisos no depende de un request).
· **El aviso de despacho sin factura lleva al cuadre del día**, que es la
  pantalla que el rol que carga las facturas SÍ puede abrir.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.avisos import queries
from modules.avisos import visibilidad as vis

#: Lo que tiene INT, recortado a lo que toca este archivo (config/roles.py).
PERMISOS_INT = {
    "facturas.ver", "facturas.crear", "cheques.ver", "clientes.ver",
    "stock.ver", "tejeduria.ver", "tintura.ver", "cartera.ver",
}


@pytest.fixture
def como_int(app):
    """Un request logueado como INT."""
    with app.test_request_context("/"):
        from flask import g
        g.user = {"id_usuario": 9, "username": "maribel", "nombre_rol": "INT"}
        g.permisos = set(PERMISOS_INT)
        yield


def test_toda_fuente_conocida_tiene_permiso_de_respaldo():
    """Una fuente nueva sin entrada desaparecería en silencio para todos.

    El `url` manda, pero un aviso puede no tener url (o apuntar a una pantalla
    que se renombró): ahí entra el fallback. Fail-closed sin red = la novedad
    se pierde y nadie se entera.
    """
    faltan = set(queries.FUENTES) - set(vis.FUENTE_PERMISO)
    assert not faltan, f"fuentes sin permiso de respaldo: {sorted(faltan)}"


def test_hacen_falta_LAS_DOS_llaves_la_pantalla_y_el_tema(app):
    """Dos avisos de la MISMA fuente pueden pedir permisos distintos."""
    with app.test_request_context("/"):
        # El cuadre del día pide facturas.ver, igual que el tema ventas.
        assert vis.permisos_del_aviso(
            "ventas", "/facturas/dia?fecha=2026-08-18") == {"facturas.ver"}
        # La explicación de la utilidad pide informes.ver ADEMÁS del tema.
        assert vis.permisos_del_aviso(
            "ventas", "/informes/dia") == {"informes.ver", "facturas.ver"}


def test_url_que_no_resuelve_cae_en_la_fuente(app):
    with app.test_request_context("/"):
        assert vis.permisos_del_aviso(
            "importaciones", "/pantalla-que-ya-no-existe") == {"compras.ver"}
        assert vis.permisos_del_aviso("importaciones", None) == {"compras.ver"}


def test_int_ve_despachos_y_facturas_y_no_ve_importaciones(como_int):
    items = [
        {"fuente": "ventas", "url": "/facturas/dia?fecha=2026-08-18"},
        {"fuente": "ventas", "url": "/facturas?desde=2026-08-18&hasta=2026-08-18"},
        {"fuente": "tejeduria", "url": "/produccion-tejeduria-asinfo"},
        {"fuente": "quimicos", "url": "/compras"},
        # ⭐ El caso que reportó la dueña el 19/08 mirando la campanita de
        # Maribel: un aviso de HILO LOCAL ("se cargó a compras") cuyo url es
        # /importaciones, que pide `stock.ver` — un permiso que INT sí tiene.
        # La pantalla sola lo dejaba pasar; el tema es el que lo frena.
        {"fuente": "hilo-local", "url": "/importaciones"},
        {"fuente": "importaciones", "url": "/importaciones"},
        # /dolares no tiene decorador (se controla adentro): ahí la única
        # llave es el tema, importaciones → compras.ver.
        {"fuente": "importaciones", "url": "/dolares"},
    ]
    quedan = [a["url"] for a in vis.filtrar(items)]
    assert quedan == [
        "/facturas/dia?fecha=2026-08-18",
        "/facturas?desde=2026-08-18&hasta=2026-08-18",
        "/produccion-tejeduria-asinfo",
    ]


def test_lo_que_no_se_puede_resolver_se_esconde_en_vez_de_mandar_a_un_404(como_int):
    """Fail-closed: la dueña (wildcard) lo ve igual y puede arreglarlo."""
    assert vis.filtrar([{"fuente": "loquesea", "url": None}]) == []


def test_el_wildcard_ve_todo(app):
    with app.test_request_context("/"):
        from flask import g
        g.user = {"id_usuario": 1, "username": "tamara"}
        g.permisos = {"*"}
        assert not vis.hay_que_filtrar()
        items = [{"fuente": "importaciones", "url": "/importaciones"},
                 {"fuente": "nueva", "url": None}]
        assert vis.filtrar(items) == items


def test_fuera_de_un_request_no_filtra_nada():
    """El que ESCRIBE avisos corre en un hilo de fondo, sin g.user."""
    assert vis.hay_que_filtrar() is False
    items = [{"fuente": "importaciones", "url": "/importaciones"}]
    assert vis.filtrar(items) == items


def test_el_filtro_por_tema_de_novedades_esconde_lo_que_no_puede_abrir(como_int):
    visibles = vis.fuentes_visibles(queries.FUENTES)
    assert "ventas" in visibles and "tejeduria" in visibles
    assert "importaciones" not in visibles and "hilo-local" not in visibles


def test_listar_pide_mas_filas_cuando_va_a_filtrar(como_int):
    """El LIMIT corta ANTES del filtro: sin margen, quedaban 3 de 15.

    Es el bug que este test evita: 12 avisos de importaciones adelante y un
    INT viendo tres novedades cuando le tocaban quince.
    """
    filas = ([{"fuente": "quimicos", "url": "/compras", "nivel": "ok"}] * 12
             + [{"fuente": "ventas", "url": "/facturas/dia", "nivel": "ok"}] * 15)
    with patch.object(queries.db, "fetch_all", return_value=filas) as f, \
            patch.object(queries, "_tiene_archivado", return_value=True), \
            patch.object(queries, "_tiene_leido_por_usuario", return_value=False):
        out = queries.listar(limite=15)
    assert f.call_args[0][1][-1] == 60          # 15 × 4, pedidas de más
    assert len(out) == 15                        # y devueltas justas
    assert all(a["fuente"] == "ventas" for a in out)


def test_el_aviso_de_despacho_sin_factura_lleva_al_cuadre_del_dia():
    """Llevaba a /informes/dia, que pide `informes.ver` — el permiso que NO
    tiene el rol que carga las facturas."""
    from modules.asinfo import despacho_sin_factura as dsf

    caso = {"numero": "DES-000095789", "dia": "2026-08-18", "kg": 122.8,
            "nota": "", "creado": "09:27", "horas": 26.0}
    with patch.object(dsf, "sin_cargar_de_dias_anteriores", return_value=[caso]), \
            patch.object(dsf, "_ahora_ec") as ahora, \
            patch("modules.avisos.queries.avisar", return_value=True) as avisar:
        ahora.return_value.hour = 23
        dsf.revisar_si_toca()
    assert avisar.call_args.kwargs["url"] == "/facturas/dia?fecha=2026-08-18"


def test_novedades_ya_no_pide_compras_ver(app, monkeypatch):
    """La pantalla daba 404 a INT desde el 05/08, cuando perdió `compras.ver`."""
    @app.before_request
    def _login_int():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 9
        g.user = {"id_usuario": 9, "username": "maribel", "id_rol": 4,
                  "nombre_rol": "INT", "activo": True}
        g.permisos = set(PERMISOS_INT)

    monkeypatch.setattr(queries, "listar", lambda **k: [])
    monkeypatch.setattr(queries, "n_no_leidos", lambda: 0)
    monkeypatch.setattr(queries, "_tiene_archivado", lambda: False)
    r = app.test_client().get("/novedades")
    assert r.status_code == 200
