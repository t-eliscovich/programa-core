"""La deuda de los códigos de cliente repetidos no se olvida sola.

TMT 2026-08-24 (dueña): *"resolver los 7 códigos de cliente duplicados por las
pantallas. ponelo como alarma en programa core"*.

Por qué importa, y por qué el aviso vale la pena: **todo el sistema JOINea por
`codigo_cli`**, no por `id_cliente`. Dos fichas con el mismo string de 3 letras
suman la plata de las dos empresas: una comisión se infló $ 4.341,86 y el
estado de cuenta de GUF mostró el mismo saldo dos veces. La migración 0155 puso
el índice único que impide crear repetidos NUEVOS, pero dejó 7 exceptuados
porque cada uno necesita una decisión que el programa no puede tomar.

El bloque no arregla nada: se asegura de que la deuda siga a la vista mientras
esté abierta.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.admin_dbase import health_audit_view as h  # noqa: E402

_SIETE = [
    {"codigo_cli": "BLP", "n_fichas": 2, "n_facturas": 75, "n_cheques": 3},
    {"codigo_cli": "BRC", "n_fichas": 2, "n_facturas": 18, "n_cheques": 0},
    {"codigo_cli": "JQS", "n_fichas": 2, "n_facturas": 0, "n_cheques": 0},
]


def _login_admin(app, fake_db):
    rid = fake_db.add_role("Admin", ["usuarios.admin"])
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _con_filas(filas):
    """Pisa SÓLO la consulta de los códigos repetidos.

    🚨 `fetch_all` también lo usa el login: pisarlo entero deja la sesión sin
    rol y el test pasa por el motivo equivocado.
    """
    original = h.db.fetch_all

    def _fake(sql, params=None, *a, **k):
        if "dup.n_fichas" in (sql or ""):
            return filas
        return original(sql, params, *a, **k)
    return _fake


# ---------------------------------------------------------------------------
# El texto del aviso
# ---------------------------------------------------------------------------


def test_el_titulo_dice_cuantos_son():
    titulo, detalle = h._texto_repetidos(_SIETE)
    assert titulo == "3 codigos de cliente repetidos"
    assert "6 fichas" in detalle
    assert "BLP, BRC, JQS" in detalle


def test_uno_solo_no_dice_codigos():
    """Un aviso que dice '1 codigos' es un aviso que nadie escribió."""
    titulo, _ = h._texto_repetidos(_SIETE[:1])
    assert titulo == "1 codigo de cliente repetido"


def test_el_detalle_cuenta_la_plata_mezclada():
    """Los movimientos son el motivo del aviso: sin ellos el código repetido
    es feo, con ellos suma plata que no es de nadie."""
    _, detalle = h._texto_repetidos(_SIETE)
    assert "96 facturas y cheques mezclados" in detalle


def test_sin_movimientos_no_inventa_el_renglon():
    _, detalle = h._texto_repetidos([_SIETE[2]])
    assert "mezclados" not in detalle


# ---------------------------------------------------------------------------
# El aviso en la campanita
# ---------------------------------------------------------------------------


def test_la_clave_lleva_los_codigos_y_no_la_fecha():
    """⭐ Es una DEUDA, no una novedad diaria: mientras la lista sea la misma
    el aviso no se repite, y en cuanto se resuelve uno vuelve a avisar
    diciendo cuántos quedan."""
    visto: dict = {}
    with patch("modules.avisos.avisar", lambda **kw: visto.update(kw) or True):
        h._avisar_repetidos(_SIETE)
    assert visto["clave"] == "clientes:codigo-repetido:BLP|BRC|JQS"
    assert visto["nivel"] == "alerta"
    assert visto["fuente"] == "clientes"
    assert visto["url"] == "/admin/clientes-asinfo/"


def test_la_clave_cambia_cuando_se_resuelve_uno():
    a: dict = {}
    b: dict = {}
    with patch("modules.avisos.avisar", lambda **kw: a.update(kw) or True):
        h._avisar_repetidos(_SIETE)
    with patch("modules.avisos.avisar", lambda **kw: b.update(kw) or True):
        h._avisar_repetidos(_SIETE[:2])
    assert a["clave"] != b["clave"], (
        "si la clave no cambia, resolver un código no vuelve a avisar nunca")


def test_sin_repetidos_no_avisa_nada():
    visto: list = []
    with patch("modules.avisos.avisar", lambda **kw: visto.append(kw) or True):
        h._avisar_repetidos([])
    assert visto == []


def test_avisar_nunca_rompe_al_health():
    with patch("modules.avisos.avisar", side_effect=RuntimeError("buzón caído")):
        h._avisar_repetidos(_SIETE)      # no levanta


# ---------------------------------------------------------------------------
# El bloque del health
# ---------------------------------------------------------------------------


def test_con_repetidos_el_health_se_pone_en_rojo(app, fake_db):
    c = _login_admin(app, fake_db)
    with patch.object(h.db, "fetch_all", side_effect=_con_filas(_SIETE)), \
            patch("modules.avisos.avisar", lambda **kw: True):
        r = c.get("/admin/health/codigos-duplicados").get_json()
    assert r["ok"] is False
    assert r["stats"]["n_codigos"] == 3
    assert r["stats"]["codigos"] == ["BLP", "BRC", "JQS"]
    assert r["alerts"][0]["category"] == "codigo_cliente_repetido"
    assert "/admin/clientes-asinfo/" in r["alerts"][0]["msg"]


def test_sin_repetidos_el_health_esta_en_verde(app, fake_db):
    c = _login_admin(app, fake_db)
    with patch.object(h.db, "fetch_all", side_effect=_con_filas([])):
        r = c.get("/admin/health/codigos-duplicados").get_json()
    assert r["ok"] is True
    assert r["alerts"] == []
    assert r["stats"]["n_codigos"] == 0


def test_el_bloque_entra_al_health_general():
    """🚨 Un bloque que existe pero no está en /all no lo mira nadie: el cron
    diario pega un solo curl a /admin/health/all."""
    import inspect
    src = inspect.getsource(h.health_all)
    assert "codigos_duplicados()" in src, "el bloque no se llama en /all"
    assert '"codigos_duplicados": data15' in src, "el bloque no sale en el JSON"
    assert 'data15["ok"]' in src, (
        "el bloque no entra al `ok` general: el panel quedaría verde con "
        "códigos repetidos abiertos")


def test_la_consulta_normaliza_como_joinea_el_sistema():
    """Si la consulta mirara `codigo_cli` pelado, un espacio al final
    escondería el repetido — la misma razón por la que el índice único de la
    0155 va sobre UPPER(TRIM(...)).

    🚨 El assert mira el `GROUP BY` de la CTE `dup`, no el SQL entero: la
    consulta normaliza en tres lugares y buscar el texto suelto pasaba igual
    con la parte que importa rota. Un test con la FORMA del chequeo no
    protege nada.
    """
    sql = h.SQL_CODIGOS_REPETIDOS
    dup = sql[sql.index("WITH dup AS"):sql.index("fact AS")]
    assert dup.count("UPPER(TRIM(codigo_cli))") == 2, (
        "la CTE `dup` tiene que normalizar en el SELECT y en el GROUP BY")
    assert "HAVING COUNT(*) > 1" in dup


# ---------------------------------------------------------------------------
# Quién ve el aviso — la mitad que se olvida siempre
# ---------------------------------------------------------------------------


def test_alex_puede_abrir_la_pantalla_que_resuelve():
    """🚨 Un aviso que lleva a una pantalla que el que opera no puede abrir es
    peor que no avisar: la campanita es fail-closed y ni se lo muestra.

    La pantalla dejó de colgar de `admin_dbase.ver` —que no lo tiene NINGÚN
    rol, sólo los wildcard— y pasó a `clientes.duplicados` (mig 0210)."""
    from config.roles import ROLES

    perms = dict(ROLES)
    assert "clientes.duplicados" in perms["INT"], (
        "INT es el rol de Alex, que es quien resuelve los códigos repetidos")
    assert "admin_dbase.ver" not in perms["INT"], (
        "el permiso propio existe justamente para NO abrirle el panel de "
        "administración entero")


def test_la_pantalla_pide_el_permiso_propio():
    from modules.admin_dbase import clientes_asinfo_detalle_view as d
    from modules.admin_dbase import clientes_asinfo_view as v

    assert v.run._permiso == "clientes.duplicados"
    assert d.detalle._permiso == "clientes.duplicados"


def test_el_aviso_le_llega_a_alex(app, fake_db):
    """Las DOS llaves de la campanita: el permiso del tema (`clientes.ver`) y
    el de la pantalla a la que lleva. Alex tiene los dos; el que sólo mira, no."""
    from modules.avisos import visibilidad as vis

    with app.test_request_context("/"):
        from flask import g
        g.user = {"usuario": "alex"}
        g.permisos = {"clientes.ver", "clientes.duplicados"}
        assert vis.puede_ver("clientes", "/admin/clientes-asinfo/") is True

        g.permisos = {"clientes.ver"}
        assert vis.puede_ver("clientes", "/admin/clientes-asinfo/") is False


def test_la_migracion_le_da_el_permiso_a_int():
    """`config/roles.py` es la fuente canónica, pero el que manda en runtime
    es `seguridad.permiso`: sin la migración el permiso existe sólo en el repo
    y la pantalla queda cerrada hasta para la dueña, con los tests en verde."""
    import importlib.util
    from pathlib import Path

    ruta = Path(__file__).resolve().parents[1] / "migrations" / (
        "0210_permiso_clientes_duplicados.py")
    assert ruta.exists(), "sin migración, el permiso no existe en producción"
    spec = importlib.util.spec_from_file_location("mig0210", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.PERMISO == "clientes.duplicados"
    assert "INT" in mod.ROLES_QUE_LO_RECIBEN


def test_la_url_del_aviso_resuelve_de_verdad(app):
    """🚨 Sin la BARRA FINAL el `url_map` no resuelve (RequestRedirect), y la
    campanita se cae al permiso del TEMA: el aviso se lo mostraría a cualquiera
    con `clientes.ver`, que es justo lo que el permiso propio vino a evitar.

    En el navegador el link anda igual porque Flask redirige — o sea que el
    error NO se ve clickeando ni leyendo el código."""
    visto: dict = {}
    with patch("modules.avisos.avisar", lambda **kw: visto.update(kw) or True):
        h._avisar_repetidos(_SIETE)
    adapter = app.url_map.bind("localhost")
    endpoint, _ = adapter.match(visto["url"], method="GET")
    assert endpoint == "admin_clientes_asinfo.run"
