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


def test_dias_habiles_no_cuenta_sabados_ni_domingos():
    # Agosto 2026 arranca sábado: 31 días de calendario, 21 hábiles.
    assert q.dias_habiles(date(2026, 8, 1), date(2026, 8, 31)) == 21
    # Una semana comercial completa son 5.
    assert q.dias_habiles(date(2026, 8, 3), date(2026, 8, 9)) == 5
    # Un fin de semana solo son 0 — y eso no puede reventar nada.
    assert q.dias_habiles(date(2026, 8, 1), date(2026, 8, 2)) == 0
    # Rango invertido = 0, no negativo.
    assert q.dias_habiles(date(2026, 8, 5), date(2026, 8, 1)) == 0


def test_avance_esperado():
    d, h, _ = q.rango_periodo("mes", date(2026, 8, 15))
    # Al 15 de agosto de 2026 van 10 días hábiles de los 21 del mes.
    assert q.avance_esperado(d, h, date(2026, 8, 15)) == pytest.approx(10 / 21)
    # Un día posterior al cierre del período no pasa de 1.
    assert q.avance_esperado(d, h, date(2026, 9, 20)) == 1.0
    # Período degenerado no divide por cero.
    assert q.avance_esperado(date(2026, 8, 2), date(2026, 8, 1), date(2026, 8, 1)) == 1.0
    # Un período de puro fin de semana tampoco.
    assert q.avance_esperado(date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 1)) == 1.0


def test_meta_semanal_se_prorratea(monkeypatch):
    monkeypatch.setattr(q, "meta_mes", lambda *a, **k: 3100.0)
    # Agosto 2026 tiene 21 días hábiles → la semana (5 hábiles) vale 5/21.
    assert q.meta_periodo("PPR", "semana", date(2026, 8, 5)) == pytest.approx(
        3100.0 * 5 / 21
    )
    assert q.meta_periodo("PPR", "mes", date(2026, 8, 5)) == 3100.0


@pytest.mark.parametrize("hoy", [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5),
                                 date(2026, 8, 6), date(2026, 8, 7)])
def test_la_semana_y_el_mes_no_pueden_decir_cosas_opuestas(monkeypatch, hoy):
    """El bug que reportó la dueña el 2026-08-04:

        *"eso de el ritmo está mal calculado. siendo 4 de agosto ¿cómo es que
        en semana estoy arriba del ritmo y en mes abajo? O lo sacás o le
        ponés bien la lógica."*

    Tenía razón y era el fin de semana. Agosto 2026 arranca SÁBADO: el mes
    prorrateaba por días de calendario, así que al 04/08 ya daba por
    transcurrido el 1 y el 2 —sábado y domingo, 0 facturas de domingo en la
    historia de la empresa— mientras la semana, que arranca el lunes 3, no
    los contaba. Los mismos $750,34 quedaban arriba del ritmo en una
    pantalla y abajo en la otra, el mismo día.

    Durante la PRIMERA semana del mes lo vendido en la semana y lo vendido
    en el mes son lo mismo, así que el delta contra el ritmo TIENE que ser
    idéntico. Es una identidad, no una tolerancia.
    """
    monkeypatch.setattr(q, "meta_mes", lambda *a, **k: 10_000.0)
    vendido = 750.34

    d_sem, h_sem, _ = q.rango_periodo("semana", hoy)
    d_mes, h_mes, _ = q.rango_periodo("mes", hoy)

    delta_sem = vendido - q.meta_periodo("V", "semana", hoy) * q.avance_esperado(
        d_sem, h_sem, hoy)
    delta_mes = vendido - q.meta_periodo("V", "mes", hoy) * q.avance_esperado(
        d_mes, h_mes, hoy)

    assert delta_sem == pytest.approx(delta_mes, abs=1e-9)
    # Y el signo, que es lo único que el vendedor mira.
    assert (delta_sem >= 0) == (delta_mes >= 0)


def test_el_ritmo_no_avanza_el_fin_de_semana():
    """Corolario: el viernes a la noche y el domingo a la noche el vendedor
    ve el MISMO objetivo. Antes el lunes arrancaba debiendo dos días."""
    d, h, _ = q.rango_periodo("mes", date(2026, 8, 7))
    viernes = q.avance_esperado(d, h, date(2026, 8, 7))
    domingo = q.avance_esperado(d, h, date(2026, 8, 9))
    assert viernes == domingo == pytest.approx(5 / 21)


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


def _totales(**cambios) -> dict:
    """Totales del estado de cuenta para los fakes de estos tests.

    Salen de `informes.queries.totales_estado_cuenta_en_cero()`, la MISMA
    función que usa la rama "cliente inexistente" de producción — no de un
    diccionario escrito a mano acá. Un fake a mano se queda corto de claves
    en cuanto la pantalla usa una más, y entonces el template revienta recién
    en producción con el test en verde.
    """
    from modules.informes import queries as iq

    t = iq.totales_estado_cuenta_en_cero()
    t.update(cambios)
    return t


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
            "totales": _totales(),
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


# ---------------------------------------------------------------------------
# La ficha del cliente ES un estado de cuenta
# ---------------------------------------------------------------------------
# Dueña 2026-08-04: *"estado de cuenta se tiene que parecer más al estado de
# cuenta del programa"*. Un estado de cuenta no es una lista de facturas: es
# un libro mayor — columnas alineadas, un acumulado que corre y un total al
# pie. Estos tests son lo que impide que vuelva a ser una lista linda.


def _ec_con_facturas(cod):
    """Tres facturas: una pagada, una con abono parcial y una viva."""
    return {
        "cliente": {"codigo_cli": cod, "nombre": "Textiles del Valle",
                    "provincia": "PICHINCHA", "canton": "QUITO",
                    "ruc": "1790012345001", "telefono": "0989 506 447",
                    "mail": {"mail": "compras@tdv.com", "origen": "asinfo",
                             "etiqueta": "Tomado de Asinfo", "alternativo": ""},
                    "cupo": 20000,
                    "direccion1": "AV. AMAZONAS", "direccion2": "N32-14"},
        "facturas": [
            {"id_factura": 1, "numf": 100, "numf_completo": None,
             "fecha": date(2026, 1, 15), "vencimiento": date(2026, 2, 14),
             "importe": 1000.0, "abono": 1000.0, "saldo": 0.0,
             "stat": "A", "tipo": "FA"},
            {"id_factura": 2, "numf": 101, "numf_completo": "001-099-000000101",
             "fecha": date(2026, 3, 10), "vencimiento": date(2026, 4, 9),
             "importe": 500.0, "abono": 200.0, "saldo": 300.0,
             "stat": "A", "tipo": "FA"},
            {"id_factura": 3, "numf": 102, "numf_completo": "001-099-000000102",
             "fecha": date(2026, 7, 27), "vencimiento": date(2026, 8, 26),
             "importe": 250.0, "abono": 0.0, "saldo": 250.0,
             "stat": "A", "tipo": "FA"},
        ],
        "cheques": [], "anticipos": [],
        "totales": _totales(importe=1750.0, abono=1200.0, saldo=550.0,
                            saldo_neto=550.0, saldo_vivo=550.0,
                            saldo_vencido=300.0, n_vencidas=1,
                            cheques_por_cobrar=80.0, cheques_a_depositar=80.0),
    }


@pytest.fixture()
def ficha(vendedor_logueado, monkeypatch):
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente",
                        _ec_con_facturas)
    r = vendedor_logueado.get("/mi-cartera/cliente/TDV")
    assert r.status_code == 200
    return r.data.decode()


def test_la_ficha_tiene_las_columnas_del_estado_de_cuenta(ficha):
    """Los mismos rótulos que `informes/_estado_cuenta_impreso.html`."""
    for rotulo in ("Fecha", "Número", "Importe", "Saldo", "Acum.", "Totales"):
        assert rotulo in ficha, f"falta la columna «{rotulo}»"


def test_el_acumulado_corre_y_la_ultima_fila_da_el_saldo(ficha):
    """El corazón del asunto: sin ACUM esto es una lista, no un estado de cuenta.

    Corre de la más vieja a la más nueva (dueña 2026-06-11), así que la última
    fila TIENE que dar el saldo de hoy: 0 + 300 + 250 = 550. Si el acumulado
    arrancara del otro lado o se salteara una fila, el vendedor le muestra al
    cliente un número que no cierra con el papel que le deja.
    """
    import re

    nums = re.findall(r">\s*([\d.]+,\d\d)\s*</td>", ficha)
    # (importe, saldo, acum) por fila, en orden.
    assert nums[0:3] == ["1.000,00", "0,00", "0,00"]
    assert nums[3:6] == ["500,00", "300,00", "300,00"]
    assert nums[6:9] == ["250,00", "250,00", "550,00"]
    # Y el pie, con el total de la oficina.
    assert "Totales (3)" in ficha
    assert "1.750,00" in ficha


def test_el_abonado_sale_solo_cuando_hay_abono(ficha):
    """La decisión de diseño que hace entrar la tabla en 390 px.

    En la oficina «Abonado» es una columna fija; acá es un renglón que aparece
    únicamente en las filas que tienen abono. La información no se pierde
    —perderla sería inaceptable, es la respuesta a "pero yo te pagué"— pero
    diecisiete «0,00» no pueden costar el ancho de una columna que sí habla.
    """
    assert ficha.count("Abonado $") == 3   # dos filas con abono + el pie
    assert "Abonado $ 200,00" in ficha
    assert "Abonado $ 1.000,00" in ficha
    assert "Abonado $ 1.200,00" in ficha   # total


def test_el_numero_de_factura_es_el_mismo_que_en_la_oficina(ficha):
    """La hoja de la oficina muestra 101; el portal mostraba 001-099-000000101.

    Dos rótulos para la misma factura obligan al vendedor a traducir mientras
    discute con el cliente. Se usa la misma expresión que el parcial impreso.
    """
    assert "001-099" not in ficha


def test_la_ficha_cierra_como_el_dbase(ficha):
    """Los tres renglones de CUENTA.PRG L365-392, igual que la hoja impresa."""
    assert "Cheques a depositar" in ficha
    assert "80,00" in ficha
    # TOTAL = saldo neto + cheques por cobrar = 550 + 80.
    assert "630,00" in ficha


def test_el_vendedor_ve_como_contactar_al_cliente_pero_no_su_cupo(ficha):
    """Dueña 2026-08-04: el contacto sí (va a visitarlo); el cupo no."""
    assert "AV. AMAZONAS N32-14" in ficha
    assert "QUITO, PICHINCHA" in ficha
    assert "1790012345001" in ficha
    # Tocables: desde el celular, llamar es un toque.
    assert 'href="tel:0989506447"' in ficha
    assert 'href="mailto:compras@tdv.com"' in ficha
    # `cliente.mail` es un DICT (mail + origen + etiqueta), no un string: la
    # pantalla salió a producción el 04/08 imprimiendo el repr entero de
    # Python — "{'mail': '...', 'origen': 'asinfo_completa', 'etiqueta': ...}"
    # — en el renglón del mail. Se muestra la dirección, no la estructura.
    assert "origen" not in ficha and "'mail':" not in ficha
    # Y la línea que no se cruza.
    assert "20000" not in ficha and "20.000" not in ficha


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
                "totales": _totales()}

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


def test_el_nombre_ya_cargado_se_muestra_como_texto(app, fake_db, monkeypatch):
    """Dueña 2026-08-04: *"no me hagas editable los nombres, dejalos como están"*.

    Los seis ya están cargados. Seis cajas de texto abiertas en la pantalla
    que se mira todos los meses no agregan nada y sí ofrecen pisar un nombre
    sin querer.

    Pero el campo NO desaparece: queda detrás del lapicito, y sale abierto
    solo en las filas donde falta el nombre. Si se borrara del todo volvería
    el bug de ayer —una ruta sin pantalla es una función que no existe— y un
    vendedor nuevo no tendría dónde ponerle el nombre.
    """
    from modules.comisiones import views as cviews

    filas = [
        {"codigo": "RMY", "nombre": "Roberto Miranda", "pct_comision": 1.0,
         "activo": True, "n_clientes": 3, "ventas_mes": 0, "cobranzas_mes": 0,
         "comision_mes": 0},
        # Recién sembrado: el nombre viene igual al código (así quedaron los
        # seis en mayo) — para la pantalla eso es "sin nombre".
        {"codigo": "NUE", "nombre": "NUE", "pct_comision": 0,
         "activo": True, "n_clientes": 0, "ventas_mes": 0, "cobranzas_mes": 0,
         "comision_mes": 0},
    ]
    monkeypatch.setattr(cviews.queries, "lista", lambda **k: filas)

    rid = fake_db.add_role("Tester", ["comisiones.ver"])
    uid = fake_db.add_user("jefa", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    html = c.get("/comisiones").data.decode()

    # El que YA tiene nombre: texto plano, y su formulario arranca oculto.
    assert ">Roberto Miranda</span>" in html
    assert 'id="nom-RMY-form"' in html
    forma_rmy = html.split('id="nom-RMY-form"')[1].split(">")[0]
    assert "hidden" in forma_rmy and "inline-flex" not in forma_rmy

    # El que NO lo tiene: la caja abierta, vacía y lista para escribir.
    forma_nue = html.split('id="nom-NUE-form"')[1].split(">")[0]
    assert "inline-flex" in forma_nue and "hidden" not in forma_nue
    assert "— sin nombre —" in html
    # Y el código repetido como nombre no se muestra como si fuera un nombre.
    assert ">NUE</span>" not in html


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
            "totales": _totales(cheques_total=10.0, cheques_depositados=10.0),
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


def test_la_suma_del_desglose_ES_la_comision_del_mes(monkeypatch):
    """EXACTAMENTE igual, no "parecido".

    Con la cartera real de RMY en agosto, redondear el total por un lado
    (round(257,82 × 3%) = 7,73) y cada cliente por el otro (3,60 + 2,57 +
    1,57 = 7,74) daba números distintos en la misma pantalla. Un centavo
    alcanza para que el vendedor deje de creerle a los dos.
    """
    monkeypatch.setattr(q, "cobros_del_mes", lambda *a, **k: COBROS_CENTAVO)
    monkeypatch.setattr(q, "_pct_comision", lambda vend: 3.0)

    g = q.comision_por_cliente("RMY", 2026, 8)
    assert q.comision_mes("RMY", 2026, 8) == round(sum(x["comision"] for x in g), 2)

    # Y el caso concreto que lo destapó: el redondeo del total NO coincide.
    cobrado = sum(c["importe"] for c in COBROS_CENTAVO)
    assert round(cobrado * 3.0 / 100.0, 2) != q.comision_mes("RMY", 2026, 8)


COBROS_CENTAVO = [
    {"origen": "EFE", "doc": "1204", "fecha": date(2026, 8, 1), "importe": 120.00,
     "codigo_cli": "ADG", "cliente": "MOLRIV ADELA", "banco": ""},
    {"origen": "CHE", "doc": "101731", "fecha": date(2026, 8, 3), "importe": 35.64,
     "codigo_cli": "MWI", "cliente": "MARIO W", "banco": "DEP.PICH."},
    {"origen": "CHE", "doc": "101737", "fecha": date(2026, 8, 3), "importe": 50.00,
     "codigo_cli": "MWI", "cliente": "MARIO W", "banco": "DEP.PICH."},
    {"origen": "CHE", "doc": "101690", "fecha": date(2026, 8, 2), "importe": 52.18,
     "codigo_cli": "FLA", "cliente": "FRANCO LAUTARO", "banco": "DEP.PICH."},
]


def test_el_mes_a_mes_usa_la_misma_cuenta_que_el_desglose(monkeypatch):
    """Si la lista de meses contara distinto, agosto valdría dos cosas.

    ⭐ Desde el 2026-08-04 la lista mes a mes ya NO recorre el detalle mes por
    mes (eran 8 queries pesadas por pantalla, 3.190 ms): sale de UNA sola
    consulta agregada por (mes, cliente). Este test es lo que garantiza que
    la optimización no cambió el número: el agregado se agrupa por CLIENTE y
    se redondea por cliente, igual que el desglose. Si alguien lo "mejora"
    sumando el mes entero antes de aplicar el %, vuelve el $7,73 vs $7,74.
    """
    from modules.comisiones import queries as cq

    monkeypatch.setattr(q, "cobros_del_mes", lambda *a, **k: COBROS_CENTAVO)
    monkeypatch.setattr(q, "_pct_comision", lambda vend: 3.0)

    # El agregado que devolvería Postgres para los mismos cobros.
    por_cli: dict[str, float] = {}
    for c in COBROS_CENTAVO:
        por_cli[c["codigo_cli"]] = por_cli.get(c["codigo_cli"], 0.0) + c["importe"]
    monkeypatch.setattr(
        cq, "cobranzas_por_cliente_anio",
        lambda codigo, *, anio, hasta_mes=12: [
            {"mes": 8, "codigo_cli": k, "cobrado": v} for k, v in por_cli.items()
        ],
    )

    filas = q.comision_meses("RMY", 2026, 8)
    assert filas[0] == {"anio": 2026, "mes": 8, "monto": q.comision_mes("RMY", 2026, 8)}
    # Los meses sin cobranza salen en 0, no se saltean: la lista es el año.
    assert [f["mes"] for f in filas] == [8, 7, 6, 5, 4, 3, 2, 1]
    assert all(f["monto"] == 0 for f in filas[1:])


def test_el_mes_a_mes_hace_UNA_query_y_no_una_por_mes(monkeypatch):
    """La regresión de performance, medida en la unidad que importa: queries.

    Dueña 2026-08-04: *"también está súper lento"*. /mi-cartera/comisión
    tardaba 3.190 ms contra 162 ms del Inicio, y no era la base: era que la
    columna mes a mes pedía el detalle completo una vez por mes.
    """
    from modules.comisiones import queries as cq

    llamadas = []
    monkeypatch.setattr(q, "_pct_comision", lambda vend: 1.0)
    monkeypatch.setattr(
        cq, "cobranzas_por_cliente_anio",
        lambda codigo, *, anio, hasta_mes=12: llamadas.append((codigo, anio,
                                                               hasta_mes)) or [],
    )
    # Y que nadie vuelva al detalle por mes por la puerta de atrás.
    def _prohibido(*a, **k):
        raise AssertionError("comision_meses volvió a pedir el detalle por mes")

    monkeypatch.setattr(q, "cobros_del_mes", _prohibido)

    q.comision_meses("RMY", 2026, 8)
    assert llamadas == [("RMY", 2026, 8)]


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


# ---------------------------------------------------------------------------
# "Ver como" desde el celular
# ---------------------------------------------------------------------------


def test_ver_como_por_GET_muestra_una_pantalla_no_un_405(app, client, fake_db,
                                                          monkeypatch):
    """Dueña 2026-08-03, desde el celular: "ver como no me funciona" + un
    **405 Method Not Allowed** crudo de Flask.

    La ruta era POST-only y la confirmación era un `confirm()` de JavaScript.
    Ahora el GET muestra una pantalla de confirmación de verdad — el mismo
    patrón que el resto de lo irreversible de la app — y sólo el POST cambia
    la sesión.
    """
    rid = fake_db.add_role("Accionista", ["*"])
    fake_db.add_user("jefa", _hash("Admin20261"), rid)
    otro = fake_db.add_user("roberto", _hash("Vendedor2026"), rid, vend="RMY")
    client.post("/login", data={"username": "jefa", "password": "Admin20261"})

    import db as dbmod

    real_fetch_one = dbmod.fetch_one

    def _fetch_one(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "u.id_usuario, u.username, r.nombre_rol" in s:
            return {"id_usuario": 1, "username": "jefa", "nombre_rol": "Accionista"}
        if "select id_usuario, username, activo" in s:
            return {"id_usuario": otro, "username": "roberto", "activo": True}
        return real_fetch_one(sql, params, conn)

    monkeypatch.setattr(dbmod, "fetch_one", _fetch_one)

    r = client.get(f"/impersonate/{otro}")
    assert r.status_code == 200, "el GET tiene que abrir la confirmación, no 405"
    html = r.data.decode()
    assert "roberto" in html
    # Y la pantalla postea de verdad, con CSRF.
    assert f'action="/impersonate/{otro}"' in html and "csrf_token" in html


def test_la_lista_de_usuarios_ya_no_usa_confirm_de_javascript():
    """Un `confirm()` depende del navegador, se porta distinto en el celular y
    cuando falla no deja rastro. La app confirma con PANTALLAS."""
    from pathlib import Path

    tpl = Path("modules/usuarios/templates/usuarios/lista.html").read_text()
    bloque = tpl[tpl.index("Ver como") - 900:tpl.index("Ver como")]
    assert "confirm(" not in bloque


def test_la_lista_de_usuarios_se_apila_en_el_celular():
    """A 390 px la tabla medía 932 y el botón quedaba 393 px fuera del borde."""
    from pathlib import Path

    tpl = Path("modules/usuarios/templates/usuarios/lista.html").read_text()
    assert "@media (max-width: 720px)" in tpl
    assert "min-width: 0 !important" in tpl, (
        "sin anular min-w-full la tabla sigue siendo más ancha que la pantalla"
    )


def test_en_la_comision_el_cheque_sin_numero_tampoco_queda_pelado(monkeypatch):
    """Mismo caso que en la ficha del cliente: "Ch. —" no identifica nada.
    Visto en el desglose de RMY."""
    monkeypatch.setattr(q, "cobros_del_mes", lambda *a, **k: [
        {"origen": "CHE", "id_origen": 9001, "doc": "  ", "fecha": date(2026, 8, 3),
         "importe": 257.82, "codigo_cli": "ERN", "cliente": "ELENA ROSARIO",
         "banco": "DEP.PICH."}])
    monkeypatch.setattr(q, "_pct_comision", lambda vend: 1.0)
    cobro = q.comision_por_cliente("RMY", 2026, 8)[0]["cobros"][0]
    assert cobro["doc"] is None and cobro["id_origen"] == 9001
