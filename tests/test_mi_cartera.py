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
    assert "#4242".encode() in r.data
    assert "Ch. ·".encode() not in r.data


def test_el_rotulo_dice_lo_que_se_esta_mostrando(vendedor_logueado, monkeypatch):
    """Con el filtro Vencidos puesto, el encabezado decía «22 con saldo»."""
    monkeypatch.setattr(
        q, "mis_clientes",
        lambda vend: [{"codigo_cli": "A", "nombre": "Uno", "saldo": 10.0,
                       "vencido": 5.0, "provincia": "", "n_facturas": 1,
                       "vence_mas_viejo": None}],
    )
    assert "con vencido".encode() in vendedor_logueado.get(
        "/mi-cartera/clientes?f=vencidos").data
    assert "con saldo".encode() in vendedor_logueado.get(
        "/mi-cartera/clientes").data
    assert "encontrado".encode() in vendedor_logueado.get(
        "/mi-cartera/clientes?q=uno").data
