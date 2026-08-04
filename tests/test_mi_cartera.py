"""Portal de vendedores — /mi-cartera.

Lo que protegen estos tests, en orden de gravedad:
  1. Que un vendedor NO pueda abrir la ficha de un cliente ajeno tipeando el
     código en la barra de direcciones (`cliente_es_mio`).
  2. Que el código de vendedor salga de la SESIÓN y no del querystring — que
     `?vend=OTRO` no haga nada si el que lo manda es un vendedor.
  3. Que el vendedor NO vea el cupo del cliente ni su % de comisión (decisión
     de la dueña 2026-08-03).
  4. Que los períodos (semana comercial / mes / año) y el prorrateo de la meta
     den lo que dicen que dan.
"""
from __future__ import annotations

from datetime import date

import bcrypt
import pytest
from flask import g

from modules.mi_cartera import queries as q


def _hash(pw: str) -> bytes:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4))


# ---------------------------------------------------------------------------
# Períodos y ritmo
# ---------------------------------------------------------------------------


def test_semana_es_lunes_a_domingo():
    # 2026-08-05 es miércoles.
    desde, hasta, rot = q.rango_periodo("semana", date(2026, 8, 5))
    assert (desde, hasta) == (date(2026, 8, 3), date(2026, 8, 9))
    assert desde.weekday() == 0 and hasta.weekday() == 6
    assert rot == "Esta semana"


def test_mes_va_del_1_al_ultimo():
    desde, hasta, _ = q.rango_periodo("mes", date(2026, 2, 17))
    assert (desde, hasta) == (date(2026, 2, 1), date(2026, 2, 28))


def test_anio_completo():
    desde, hasta, rot = q.rango_periodo("anio", date(2026, 8, 5))
    assert (desde, hasta) == (date(2026, 1, 1), date(2026, 12, 31))
    assert rot == "2026"


def test_periodo_desconocido_cae_en_mes():
    assert q.rango_periodo("cualquiera", date(2026, 8, 5))[2] == "Este mes"


def test_avance_esperado():
    d, h, _ = q.rango_periodo("mes", date(2026, 8, 15))
    # Al día 15 de un mes de 31, transcurrió 15/31.
    assert q.avance_esperado(d, h, date(2026, 8, 15)) == pytest.approx(15 / 31)
    # Un día posterior al cierre del período no pasa de 1.
    assert q.avance_esperado(d, h, date(2026, 9, 20)) == 1.0
    # Período degenerado no divide por cero.
    assert q.avance_esperado(date(2026, 8, 2), date(2026, 8, 1), date(2026, 8, 1)) == 1.0


def test_meta_semanal_se_prorratea(monkeypatch):
    monkeypatch.setattr(q, "meta_mes", lambda *a, **k: 3100.0)
    # Agosto tiene 31 días → la semana vale 7/31 de la meta del mes.
    assert q.meta_periodo("PPR", "semana", date(2026, 8, 5)) == pytest.approx(700.0)
    assert q.meta_periodo("PPR", "mes", date(2026, 8, 5)) == 3100.0


def test_sin_meta_cargada_devuelve_none(monkeypatch):
    """Sin meta no hay anillo: un 0% falso es peor que no mostrar nada."""
    monkeypatch.setattr(q, "meta_mes", lambda *a, **k: None)
    assert q.meta_periodo("PPR", "mes", date(2026, 8, 5)) is None


# ---------------------------------------------------------------------------
# Pertenencia — el guard que evita la fuga por URL
# ---------------------------------------------------------------------------


def test_cliente_es_mio_sin_datos_es_false():
    assert q.cliente_es_mio("", "TDV") is False
    assert q.cliente_es_mio("PPR", "") is False


def test_cliente_es_mio_manda_el_vend_como_parametro(monkeypatch):
    visto = {}

    def _fetch_one(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split())
        visto["params"] = params
        return {"ok": 1}

    monkeypatch.setattr(q.db, "fetch_one", _fetch_one)
    assert q.cliente_es_mio("ppr", "TDV") is True
    # El código NUNCA se interpola en el SQL: va como parámetro.
    assert "ppr" not in visto["sql"] and "PPR" not in visto["sql"]
    assert visto["params"] == {"cod": "TDV", "vend": "ppr"}
    assert "c.vend" in visto["sql"]


def test_comision_devuelve_solo_el_monto(monkeypatch):
    """La dueña 2026-08-03: el vendedor no ve su %. Ni la base (deja despejarlo)."""
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: 10_000.0)
    monkeypatch.setattr(q, "_pct_comision", lambda vend: 3.0)
    assert q.comision("PPR", date(2026, 8, 1), date(2026, 8, 31)) == 300.0


# ---------------------------------------------------------------------------
# El vendedor sale de la SESIÓN, no del querystring
# ---------------------------------------------------------------------------


def _vend_en(app, path, user, permisos):
    from modules.mi_cartera.views import _vend_actual

    with app.test_request_context(path):
        g.user = user
        g.permisos = permisos
        try:
            return _vend_actual()
        except Exception as e:  # werkzeug NotFound
            return type(e).__name__


def test_vendedor_usa_su_codigo_e_ignora_el_querystring(app):
    """El ataque obvio: PPR pide ?vend=EDG para ver la cartera del otro."""
    assert _vend_en(app, "/mi-cartera?vend=EDG", {"vend": "PPR"}, set()) == "PPR"


def test_duena_puede_previsualizar(app):
    assert _vend_en(app, "/mi-cartera?vend=edg", {"vend": None}, {"*"}) == "EDG"


def test_duena_sin_vend_en_la_url_no_inventa_uno(app):
    assert _vend_en(app, "/mi-cartera", {"vend": None}, {"*"}) == "NotFound"


def test_usuario_comun_no_entra_ni_con_querystring(app):
    assert _vend_en(
        app, "/mi-cartera?vend=PPR", {"vend": None}, {"facturas.ver", "cheques.ver"}
    ) == "NotFound"


# ---------------------------------------------------------------------------
# Rutas — end to end con sesión real
# ---------------------------------------------------------------------------


@pytest.fixture
def vendedor_logueado(app, client, fake_db):
    rid = fake_db.add_role("Vendedor", ["micartera.ver"])
    fake_db.add_user("pablo", _hash("Vendedor2026"), rid, vend="PPR")
    r = client.post("/login", data={"username": "pablo", "password": "Vendedor2026"})
    assert r.status_code in (302, 303)
    return client


def test_cliente_ajeno_da_404(vendedor_logueado, monkeypatch):
    """La fuga que este portal tiene que hacer imposible."""
    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: False)
    assert vendedor_logueado.get("/mi-cartera/cliente/AJENO").status_code == 404
    assert vendedor_logueado.get("/mi-cartera/cliente/AJENO/imprimir").status_code == 404


def test_el_vendedor_no_llega_a_las_pantallas_del_programa(vendedor_logueado):
    """El allowlist de scope_vendedor.py, ejercitado de punta a punta."""
    for path in ("/facturas", "/cheques", "/informes/estado-cuenta", "/usuarios",
                 "/vendedores/metas", "/anticipos/nuevo"):
        assert vendedor_logueado.get(path).status_code == 404, path


def test_la_raiz_lo_manda_a_su_portal(vendedor_logueado):
    r = vendedor_logueado.get("/")
    assert r.status_code in (301, 302, 303)
    assert r.headers["Location"].endswith("/mi-cartera")


def test_el_cupo_del_cliente_no_sale_del_backend(app, monkeypatch):
    """No alcanza con no pintarlo en el template: no tiene que salir del server."""
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries,
        "estado_cuenta_cliente",
        lambda cod: {"cliente": {"codigo_cli": cod, "nombre": "X", "cupo": 20000}},
    )
    with app.test_request_context("/mi-cartera/cliente/TDV"):
        data = views._cargar_cliente("PPR", "TDV")
    assert "cupo" not in data["cliente"]


@pytest.mark.parametrize(
    "path",
    [
        "/mi-cartera",
        "/mi-cartera?periodo=semana",
        "/mi-cartera?periodo=anio",
        "/mi-cartera/clientes",
        "/mi-cartera/clientes?f=vencidos",
        "/mi-cartera/clientes?q=zzz",
        "/mi-cartera/comision",
        "/mi-cartera/comision?periodo=anio",
    ],
)
def test_las_pantallas_renderizan(vendedor_logueado, path):
    """Smoke: sin datos, el portal abre igual (y no explota un template)."""
    r = vendedor_logueado.get(path)
    assert r.status_code == 200
    assert b"Mi Cartera" in r.data or b"cartera" in r.data.lower()


def test_la_ficha_del_cliente_renderiza(vendedor_logueado, monkeypatch):
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_cliente",
        lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "Textiles del Valle",
                        "provincia": "Quito", "ruc": "17", "direccion1": "",
                        "cupo": 20000},
            "facturas": [], "cheques": [], "anticipos": [],
            "totales": {"saldo_vivo": 0, "saldo_vencido": 0, "n_vencidas": 0,
                        "cheques_cartera": 0, "saldo_a_favor": 0, "importe": 0,
                        "abono": 0, "saldo": 0},
        },
    )
    r = vendedor_logueado.get("/mi-cartera/cliente/TDV")
    assert r.status_code == 200
    assert b"Textiles del Valle" in r.data
    # El cupo no viaja al navegador.
    assert b"20000" not in r.data and b"20.000" not in r.data

    # La impresión usa EL MISMO template que la de la oficina
    # (/informes/estado-cuenta/imprimir) — dueña 2026-08-03: "Imprimir tiene
    # que imprimir lo mismo que acá". Si alguien le hace una hoja propia al
    # portal, este test se cae.
    p = vendedor_logueado.get("/mi-cartera/cliente/TDV/imprimir")
    assert p.status_code == 200
    assert b"Imprimir estados de cuenta" in p.data
    assert b"Textiles del Valle" in p.data
    # Read-only: sin `interactivo`, el parcial no dibuja los dropdowns Z/A/T/X.
    assert b"estado_cuenta_factura_set_stat" not in p.data
    assert b"<select" not in p.data
    # Ni chrome de escritorio ni links a pantallas que él no puede abrir.
    assert b'id="sidebar"' not in p.data
    assert b"/cheques/" not in p.data


def test_imprimir_todos_usa_el_template_de_la_oficina(vendedor_logueado, monkeypatch):
    """Equivalente a /informes/estado-cuenta/imprimir?por=vendedor&sel=PPR."""
    from modules.mi_cartera import views

    monkeypatch.setattr(
        q, "mis_clientes",
        lambda vend: [{"codigo_cli": "AAA", "nombre": "Uno", "saldo": 100.0,
                       "vencido": 0, "provincia": "", "n_facturas": 1,
                       "vence_mas_viejo": None},
                      {"codigo_cli": "BBB", "nombre": "Dos", "saldo": 900.0,
                       "vencido": 0, "provincia": "", "n_facturas": 1,
                       "vence_mas_viejo": None}],
    )
    vistos = []

    def _ec(cod):
        vistos.append(cod)
        return {"cliente": {"codigo_cli": cod, "nombre": cod, "cupo": 5},
                "facturas": [], "cheques": [], "anticipos": [],
                "totales": {"saldo": 0, "saldo_neto": 0, "saldo_vivo": 0,
                            "saldo_vencido": 0, "n_vencidas": 0, "importe": 0,
                            "abono": 0, "kg": 0, "cheques_cartera": 0,
                            "cheques_rebotados": 0, "saldo_a_favor": 0}}

    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente", _ec)
    r = vendedor_logueado.get("/mi-cartera/imprimir")
    assert r.status_code == 200
    assert b"Imprimir estados de cuenta" in r.data
    # Mismo orden que el lote de la oficina: saldo descendente.
    assert vistos == ["BBB", "AAA"]


def test_el_contador_del_inicio_usa_el_mismo_criterio_que_la_lista(monkeypatch):
    """El Inicio decía 34 clientes y la lista mostraba 33 (verificado con RMY).

    Un cliente cuyas facturas netean a cero (una NC que cancela una factura)
    entraba en el COUNT(DISTINCT) del Inicio pero no en la lista, que filtra
    por saldo neto. Los dos tienen que contar lo mismo.
    """
    visto = {}

    def _fetch_one(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split()).lower()
        return {"saldo": 100, "vencido": 0, "n_clientes": 33}

    monkeypatch.setattr(q.db, "fetch_one", _fetch_one)
    assert q.por_cobrar("RMY")["n_clientes"] == 33
    assert "having coalesce(sum(f.saldo), 0) <> 0" in visto["sql"]
    assert "count(distinct" not in visto["sql"]


def test_la_lista_de_comisiones_deja_editar_el_nombre():
    """El nombre del vendedor tenía ruta para guardarse (`actualizar_nombre`,
    docstring: "inline edit desde la lista") pero NINGUNA pantalla con el
    campo. Resultado: los 6 quedaron sin nombre desde mayo y el portal
    saludaba "Hola, RMY".

    ⭐ Una ruta sin pantalla es una función que no existe.
    """
    from pathlib import Path

    tpl = Path("modules/comisiones/templates/comisiones/lista.html").read_text()
    assert "comisiones.actualizar_nombre" in tpl
    assert 'name="nombre"' in tpl


# ---------------------------------------------------------------------------
# La meta del AÑO a medio cargar — el 3345%
# ---------------------------------------------------------------------------


def test_meta_del_anio_a_medio_cargar_se_compara_like_con_like(app, monkeypatch):
    """Encontrado mirando el portal en vivo el 2026-08-03.

    La dueña cargó UNA meta (agosto, $10.000). `meta_anio` suma los meses
    cargados → $10.000. La pantalla lo comparaba contra las ventas del AÑO
    ENTERO ($334.524) y el anillo marcaba **3345%**.

    Ahora se comparan los meses que TIENEN meta contra la suma de esas metas.
    """
    from datetime import date

    from modules.mi_cartera import views

    monkeypatch.setattr(q, "meta_anio", lambda vend, anio: 10_000.0)
    monkeypatch.setattr(q, "meses_con_meta", lambda vend, anio: [8])
    monkeypatch.setattr(q, "ventas", lambda *a, **k: 334_524.01)
    monkeypatch.setattr(q, "ventas_en_meses", lambda vend, anio, meses: 750.34)

    ctx = views._anio_vs_meta("RMY", date(2026, 8, 3))
    assert ctx["vendido_anio"] == 750.34
    assert ctx["meta_anio"] == 10_000.0
    assert ctx["nota_anio"] == "1 mes cargado"
    assert round(ctx["vendido_anio"] * 100 / ctx["meta_anio"]) == 8


def test_con_los_12_meses_cargados_es_el_anio_entero(app, monkeypatch):
    from datetime import date

    from modules.mi_cartera import views

    monkeypatch.setattr(q, "meta_anio", lambda vend, anio: 120_000.0)
    monkeypatch.setattr(q, "meses_con_meta", lambda vend, anio: list(range(1, 13)))
    monkeypatch.setattr(q, "ventas", lambda *a, **k: 90_000.0)
    ctx = views._anio_vs_meta("RMY", date(2026, 8, 3))
    assert ctx["vendido_anio"] == 90_000.0 and ctx["nota_anio"] == ""


def test_sin_meta_no_hay_nota_ni_anillo(app, monkeypatch):
    from datetime import date

    from modules.mi_cartera import views

    monkeypatch.setattr(q, "meta_anio", lambda vend, anio: None)
    monkeypatch.setattr(q, "ventas", lambda *a, **k: 5.0)
    ctx = views._anio_vs_meta("RMY", date(2026, 8, 3))
    assert ctx["meta_anio"] is None and ctx["nota_anio"] == ""


def test_meses_con_meta_ignora_los_ceros(monkeypatch):
    """Una meta en 0 es "no cargada", no "meta cero": si contara, el mes
    entraría a la comparación con denominador 0."""
    vistos = {}

    def _fetch_all(sql, params=None, conn=None):
        vistos["sql"] = " ".join(sql.split()).lower()
        return [{"mes": 8}]

    monkeypatch.setattr(q.db, "fetch_all", _fetch_all)
    assert q.meses_con_meta("RMY", 2026) == [8]
    assert "coalesce(monto, 0) <> 0" in vistos["sql"]


def test_cheque_sin_numero_no_queda_pelado(vendedor_logueado, monkeypatch):
    """Hay filas sin N° de cheque (depósitos directos del dBase). Sin
    fallback, la ficha mostraba «Ch. · DEP.PICH.» — visto en vivo con MWI."""
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_cliente",
        lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "X", "provincia": ""},
            "facturas": [],
            "cheques": [{"id_cheque": 4242, "no_cheque": "  ", "importe": 10.0,
                         "stat": "B", "fechad": None, "fechaout": None,
                         "nombre_banco": "DEP.PICH."}],
            "anticipos": [],
            "totales": {"saldo_vivo": 0, "saldo_vencido": 0, "n_vencidas": 0,
                        "cheques_cartera": 0, "saldo_a_favor": 0},
        },
    )
    r = vendedor_logueado.get("/mi-cartera/cliente/X?tab=cheques")
    assert r.status_code == 200
    assert b"#4242" in r.data
    assert "Ch. ·".encode() not in r.data


def test_el_rotulo_dice_lo_que_se_esta_mostrando(vendedor_logueado, monkeypatch):
    """Con el filtro Vencidos puesto, el encabezado decía «22 con saldo»."""
    monkeypatch.setattr(
        q, "mis_clientes",
        lambda vend: [{"codigo_cli": "A", "nombre": "Uno", "saldo": 10.0,
                       "vencido": 5.0, "provincia": "", "n_facturas": 1,
                       "vence_mas_viejo": None}],
    )
    assert b"con vencido" in vendedor_logueado.get(
        "/mi-cartera/clientes?f=vencidos").data
    assert b"con saldo" in vendedor_logueado.get(
        "/mi-cartera/clientes").data
    assert b"encontrado" in vendedor_logueado.get(
        "/mi-cartera/clientes?q=uno").data


# ---------------------------------------------------------------------------
# Alta de usuarios pre-llenada por link
# ---------------------------------------------------------------------------


def test_el_alta_se_puede_prellenar_por_link(app, client, fake_db, monkeypatch):
    """`/usuarios/nuevo?username=...&rol=Vendedor&vend=RMY` abre el formulario
    ya cargado, para dar de alta una tanda sin tipear seis veces lo mismo.

    ⚠ La CONTRASEÑA no se pre-llena y no debe viajar nunca en una URL: queda
    en el historial del navegador, en los logs del server y en el Referer.
    """
    from modules.usuarios import queries as uq

    rid = fake_db.add_role("Vendedor", ["micartera.ver", "usuarios.admin"])
    fake_db.add_user("jefa", _hash("Admin20261"), rid)
    monkeypatch.setattr(uq, "roles_disponibles",
                        lambda: [{"id_rol": rid, "nombre_rol": "Vendedor"}])
    monkeypatch.setattr(uq, "vendedores_disponibles",
                        lambda: [{"codigo": "RMY", "nombre": "Roberto Miranda"}])
    client.post("/login", data={"username": "jefa", "password": "Admin20261"})

    r = client.get("/usuarios/nuevo?username=ROBERTO&rol=vendedor&vend=rmy")
    assert r.status_code == 200
    html = r.data.decode()
    assert 'value="roberto"' in html          # normalizado a minúsculas
    assert f'value="{rid}"' in html and "selected" in html
    assert 'value="RMY"' in html
    # Y el campo de contraseña sigue vacío.
    assert 'name="password" class="border rounded px-2 py-1 mt-1"' in html \
        or 'type="password"' in html
    assert "value=\"36Patricio8\"" not in html


def test_una_sola_semana_no_dibuja_barras(app, monkeypatch):
    """Una sola barra no es un gráfico: es un rectángulo de color.

    Las barras comparan semanas entre sí; los primeros días del mes hay una
    sola y no hay nada que comparar. Visto a 390 px el 2026-08-03.
    """
    from datetime import date

    from modules.mi_cartera import views

    monkeypatch.setattr(views, "today_ec", lambda: date(2026, 8, 3))
    monkeypatch.setattr(q, "ventas", lambda *a, **k: 750.34)
    monkeypatch.setattr(q, "meta_periodo", lambda *a, **k: None)
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Roberto Miranda")
    monkeypatch.setattr(q, "por_cobrar",
                        lambda vend: {"saldo": 0, "vencido": 0, "n_clientes": 0})

    monkeypatch.setattr(q, "ventas_por_semana",
                        lambda *a, **k: [{"semana": date(2026, 8, 3), "total": 750.34}])
    with app.test_request_context("/mi-cartera?vend=RMY"):
        g.user, g.permisos = {"vend": "RMY"}, {"micartera.ver"}
        assert 'class="bars"' not in views.inicio()

    monkeypatch.setattr(
        q, "ventas_por_semana",
        lambda *a, **k: [{"semana": date(2026, 8, 3), "total": 750.34},
                         {"semana": date(2026, 8, 10), "total": 1200.0}])
    with app.test_request_context("/mi-cartera?vend=RMY"):
        g.user, g.permisos = {"vend": "RMY"}, {"micartera.ver"}
        assert 'class="bars"' in views.inicio()


# ---------------------------------------------------------------------------
# Usuarios desactivados: se esconden, no se borran
# ---------------------------------------------------------------------------


def _cliente_admin(app, client, fake_db, monkeypatch, filas):
    from modules.usuarios import queries as uq

    rid = fake_db.add_role("Accionista", ["*"])
    fake_db.add_user("jefa", _hash("Admin20261"), rid)
    monkeypatch.setattr(uq, "listar", lambda: filas)
    client.post("/login", data={"username": "jefa", "password": "Admin20261"})
    return client


FILAS_USUARIOS = [
    {"id_usuario": 1, "username": "tamara", "email": None, "activo": True,
     "id_rol": 1, "nombre_rol": "Accionista", "clave": "TAM", "vend": None},
    {"id_usuario": 2, "username": "teliscovich@gmail.com", "email": None,
     "activo": False, "id_rol": 1, "nombre_rol": "Accionista", "clave": None,
     "vend": None},
    {"id_usuario": 3, "username": "feliscovich@gmail.com", "email": None,
     "activo": False, "id_rol": 1, "nombre_rol": "Accionista", "clave": None,
     "vend": None},
]


def test_los_desactivados_no_ensucian_la_lista(app, client, fake_db, monkeypatch):
    """Dueña 2026-08-03: "podés eliminar el repetido que no está activo".

    No se borran —una cuenta borrada deja la bitácora firmada por alguien que
    ya no existe— pero tampoco tienen por qué ocupar la pantalla.
    """
    c = _cliente_admin(app, client, fake_db, monkeypatch, FILAS_USUARIOS)
    html = c.get("/usuarios").data.decode()
    assert "tamara" in html
    assert "teliscovich@gmail.com" not in html
    # El texto lo parte Jinja en varias líneas; se compara normalizado.
    assert "2 usuarios desactivados" in " ".join(html.split())


def test_se_pueden_ver_los_desactivados_a_proposito(app, client, fake_db, monkeypatch):
    c = _cliente_admin(app, client, fake_db, monkeypatch, FILAS_USUARIOS)
    html = c.get("/usuarios?inactivos=1").data.decode()
    assert "teliscovich@gmail.com" in html and "feliscovich@gmail.com" in html


def test_sin_desactivados_no_aparece_el_cartel(app, client, fake_db, monkeypatch):
    c = _cliente_admin(app, client, fake_db, monkeypatch, [FILAS_USUARIOS[0]])
    assert "desactivado" not in c.get("/usuarios").data.decode()


def test_no_existe_ninguna_ruta_para_BORRAR_un_usuario(app):
    """Desactivar es reversible; borrar no. Si alguien agrega un botón de
    borrar, que sea a propósito y no de arrastre — este test lo va a frenar.
    """
    rutas = [str(r) for r in app.url_map.iter_rules() if "usuario" in str(r).lower()]
    assert rutas, "no encontré ninguna ruta de usuarios: el test dejó de vigilar"
    for r in rutas:
        assert "borrar" not in r and "eliminar" not in r and "delete" not in r


# ---------------------------------------------------------------------------
# Comisión: sólo mensual, y con el detalle de qué la generó
# ---------------------------------------------------------------------------


COBROS = [
    {"origen": "CHE", "doc": "101731", "fecha": date(2026, 8, 3), "importe": 35.64,
     "codigo_cli": "MWI", "cliente": "MARIO W INNOVANOVENTA", "banco": "DEP.PICH."},
    {"origen": "CHE", "doc": "101737", "fecha": date(2026, 8, 3), "importe": 50.00,
     "codigo_cli": "MWI", "cliente": "MARIO W INNOVANOVENTA", "banco": "DEP.PICH."},
    {"origen": "EFE", "doc": "88", "fecha": date(2026, 8, 1), "importe": 200.00,
     "codigo_cli": "ADG", "cliente": "MOLRIV ADELA", "banco": ""},
]


def test_la_comision_se_agrupa_por_cliente_y_muestra_cada_cobro(monkeypatch):
    """Dueña 2026-08-03: "quieren saber de qué clientes están ganando esta
    comisión, que la comisión diga de qué cobranza es"."""
    monkeypatch.setattr(q, "cobros_del_mes", lambda *a, **k: COBROS)
    monkeypatch.setattr(q, "_pct_comision", lambda vend: 1.0)

    g = q.comision_por_cliente("RMY", 2026, 8)
    # Ordenados por lo cobrado, de mayor a menor.
    assert [x["codigo_cli"] for x in g] == ["ADG", "MWI"]
    assert g[0]["cobrado"] == 200.0 and g[0]["comision"] == 2.0
    assert g[1]["cobrado"] == 85.64 and g[1]["comision"] == 0.86
    # Cada cliente trae sus cobros uno por uno, en orden de fecha.
    assert len(g[1]["cobros"]) == 2
    assert g[1]["cobros"][0]["doc"] == "101731"
    assert g[1]["cobros"][0]["es_cheque"] is True
    assert g[0]["cobros"][0]["es_cheque"] is False


def test_la_suma_del_desglose_es_la_comision_del_mes(monkeypatch):
    """Si el detalle no suma el total, el vendedor deja de creerle a los dos."""
    monkeypatch.setattr(q, "cobros_del_mes", lambda *a, **k: COBROS)
    monkeypatch.setattr(q, "_pct_comision", lambda vend: 1.0)
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: sum(c["importe"] for c in COBROS))

    g = q.comision_por_cliente("RMY", 2026, 8)
    total_detalle = sum(x["comision"] for x in g)
    total_mes = q.comision("RMY", date(2026, 8, 1), date(2026, 8, 31))
    assert abs(total_detalle - total_mes) < 0.02


def test_la_comision_no_deja_pedir_un_mes_futuro(vendedor_logueado, monkeypatch):
    """La comisión de un mes que no pasó es 0 y confunde."""
    from modules.mi_cartera import views

    monkeypatch.setattr(views, "today_ec", lambda: date(2026, 8, 3))
    monkeypatch.setattr(q, "comision_por_cliente", lambda *a, **k: [])
    monkeypatch.setattr(q, "comision", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision_meses", lambda *a, **k: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Roberto Miranda")

    html = vendedor_logueado.get("/mi-cartera/comision?anio=2026&mes=12").data.decode()
    assert "Agosto 2026" in html and "Diciembre 2026" not in html
    # Y en el mes actual no hay flecha "siguiente".
    assert "mes=9" not in html


def test_la_comision_ya_no_tiene_selector_de_semana_ni_de_ano(vendedor_logueado,
                                                              monkeypatch):
    """Una comisión semanal no se paga: el número no significaba nada."""
    from modules.mi_cartera import views

    monkeypatch.setattr(views, "today_ec", lambda: date(2026, 8, 3))
    monkeypatch.setattr(q, "comision_por_cliente", lambda *a, **k: [])
    monkeypatch.setattr(q, "comision", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision_meses", lambda *a, **k: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Roberto Miranda")

    html = vendedor_logueado.get("/mi-cartera/comision").data.decode()
    assert "periodo=semana" not in html and "periodo=anio" not in html
