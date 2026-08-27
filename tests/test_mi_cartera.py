"""Portal de vendedores — /mi-cartera.

Lo que protegen estos tests, en orden de gravedad:
  1. Que un vendedor NO pueda abrir la ficha de un cliente ajeno tipeando el
     código en la barra de direcciones (`cliente_es_mio`).
  2. Que el código de vendedor salga de la SESIÓN y no del querystring — que
     `?vend=OTRO` no haga nada si el que lo manda es un vendedor.
  3. Que el vendedor NO vea su % de comisión (decisión de la dueña
     2026-08-03). El CUPO sí lo ve desde el 2026-08-05 — ver
     `test_el_cupo_y_el_descuento_le_llegan_al_vendedor`.
  4. Que los períodos (semana comercial / mes / año) y el prorrateo de la meta
     den lo que dicen que dan.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

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


def test_el_cupo_y_el_descuento_le_llegan_al_vendedor(app, monkeypatch):
    """⚖️ DECISIÓN REVERTIDA el 2026-08-05.

    El 03/08 la dueña decidió que el vendedor NO viera el cupo, y acá había un
    `pop("cupo")` con su test. Andrés, por WhatsApp: *"por favor es clave eso,
    mostrar cupos y descuentos del cliente / los vendedores tienen apegarse a
    los cupos y ordenar a los clientes"*, y ella lo aprobó.

    El razonamiento del cambio: el que decide cuánto cargarle a un cliente es
    el vendedor, parado en el local. Si no ve el cupo, "apegarse al cupo" es
    una instrucción sin instrumento.

    Este test existe para que la reversión sea EXPLÍCITA: si mañana alguien
    vuelve a poner el `pop`, falla acá y encuentra por qué se sacó.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries,
        "estado_cuenta_cliente",
        lambda cod: {"cliente": {"codigo_cli": cod, "nombre": "X", "cupo": 20000,
                                 "descuento": 8.5}},
    )
    with app.test_request_context("/mi-cartera/cliente/TDV"):
        data = views._cargar_cliente("PPR", "TDV")
    assert data["cliente"]["cupo"] == 20000
    assert data["cliente"]["descuento"] == 8.5


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
    # El cupo SÍ se muestra desde el 2026-08-05 (ver
    # test_el_cupo_y_el_descuento_le_llegan_al_vendedor).
    assert b"20.000" in r.data

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


def _tabla(html: str) -> str:
    """El `<table class="ec">` de facturas, sin el CSS ni el resto del cascarón.

    Buscar una palabra en el HTML entero engaña: el `<style>` del template
    viaja al navegador con sus comentarios, y ahí aparecen "Abonado" y
    "Vencida" aunque la tabla no los dibuje. Un test que dice "esta columna ya
    no está" tiene que mirar la tabla.
    """
    import re

    m = re.search(r'<table class="ec">.*?</table>', html, re.S)
    assert m, "no está la tabla del estado de cuenta"
    return m.group(0)


def _encabezados(html: str) -> list[str]:
    """Los rótulos de las columnas, en orden."""
    import re

    return [re.sub(r"<[^>]+>", "", th).strip()
            for th in re.findall(r"<th[^>]*>(.*?)</th>", _tabla(html), re.S)]


def test_la_ficha_tiene_las_columnas_del_estado_de_cuenta(ficha):
    """Las mismas COLUMNAS que la oficina, reacomodadas para 390 px.

    Fecha y Número no son dos columnas sino una, «Factura»: eran las dos más
    anchas de la tabla para decir dos cosas que se leen juntas (cuál es la
    factura). Apiladas liberan 33 px, que es lo que hacía falta para que las
    cifras entren cómodas. Ningún dato se pierde — se reacomodan.
    """
    assert _encabezados(ficha) == ["Factura", "Importe", "Retenc.", "Abonado",
                                   "Saldo · acum."]
    assert "Totales (3)" in ficha

    # La fecha sigue estando, adentro de la celda de la factura, con los días.
    assert "15/01/26" in ficha
    # Y la palabra "Vencida" ya no va: la fecha en rojo lo dice, y decirlo dos
    # veces costaba un renglón por fila. (Se busca en el CUERPO: los
    # comentarios del CSS del template también viajan al navegador.)
    assert "Vencida" not in _tabla(ficha)


def test_las_columnas_son_fijas_aunque_el_cliente_no_tenga_el_dato(
        vendedor_logueado, monkeypatch):
    """Tamara 2026-08-26: *"si no tiene nada una '-' pero consistente. entran
    todas las columnas"*.

    Entre el 17 y el 26 de agosto la columna sin un solo dato se escondía. El
    ancho que ganaba costaba algo peor: la tabla cambiaba de forma de un
    cliente al otro, así que el vendedor que abre tres fichas seguidas tiene
    que volver a buscar dónde quedó cada cifra, y una columna ausente se lee
    como que el dato no existe. Es la misma tabla que la hoja impresa y que la
    oficina, donde las columnas nunca se movieron.

    El test mira el caso más pelado: un cliente sin UNA retención y sin UN
    abono. Las seis columnas tienen que estar igual, con guiones.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)

    def _pelado(cod):
        d = _ec_con_facturas(cod)
        for f in d["facturas"]:
            f["abono"] = 0.0
            f["retencion"] = 0.0
        d["totales"]["abono"] = 0.0
        d["totales"]["retencion"] = 0.0
        return d

    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente", _pelado)
    html = vendedor_logueado.get("/mi-cartera/cliente/TDV").data.decode()

    assert _encabezados(html) == ["Factura", "Importe", "Retenc.", "Abonado",
                                  "Saldo · acum."]
    # 3 filas + el pie, en las dos columnas, todas con guión.
    tabla = _tabla(html)
    assert tabla.count('class="mono ret"') == 4
    assert tabla.count('class="mono ab"') == 4
    assert tabla.count("—") >= 8


def test_el_acumulado_corre_y_la_ultima_fila_da_el_saldo(ficha):
    """El corazón del asunto: sin ACUM esto es una lista, no un estado de cuenta.

    Corre de la más vieja a la más nueva (dueña 2026-06-11), así que la última
    fila TIENE que dar el saldo de hoy: 0 + 300 + 250 = 550. Si el acumulado
    arrancara del otro lado o se salteara una fila, el vendedor le muestra al
    cliente un número que no cierra con el papel que le deja.
    """
    import re

    # Desde el 27/08/2026 el acumulado corre en un <small> DENTRO de la celda
    # del saldo (como columna propia la tabla se salía de la pantalla del
    # celular). Se leen todas las cifras de la tabla en orden: el orden por
    # fila sigue siendo importe, abonado, saldo, acum.
    nums = re.findall(r"([\d.]+,\d\d)", _tabla(ficha))
    # La tercera factura no tiene abono: ahí va un guión, que no aporta número.
    assert nums[0:4] == ["1.000,00", "1.000,00", "0,00", "0,00"]
    assert nums[4:8] == ["500,00", "200,00", "300,00", "300,00"]
    assert nums[8:11] == ["250,00", "250,00", "550,00"]
    # Y la estructura: el acumulado de la última fila va apilado bajo el saldo.
    assert re.search(r'class="mono fuerte">250,00\s*<small[^>]*>550,00</small>',
                     _tabla(ficha))
    # Y el pie, con el total de la oficina.
    assert "Totales (3)" in ficha
    assert "1.750,00" in ficha


def test_la_retencion_es_una_columna_y_cierra_la_aritmetica(vendedor_logueado, monkeypatch):
    """Tamara 2026-08-17: *"la aplicacion de los vendedores no muestra ...
    retenciones de los clientes"* → *"te falta la columna de retenciones"*.

    La retención tiene columna propia desde la mig 0179 (07/08) y baja el
    saldo sin sumarse al abono: `saldo = importe − abono − retencion`. La
    oficina y la hoja impresa ya la mostraban; la ficha del portal no, y esa
    falta NO se leía como "falta una columna" — se leía como que las CIFRAS
    estaban mal. Con MYU el vendedor veía importe 5.678,68, abonado en guión
    y saldo 5.579,92, y no había forma de explicar los 98,76 de diferencia
    mirando la pantalla.

    Por eso el test no se conforma con que el rótulo exista: exige que la
    fila CIERRE. Un test que sólo busca la palabra "Retenc." pasa igual con
    la columna dibujada y vacía, que es justo el bug de al lado.

    Va entre Importe y Abonado, el orden que pidió Andrés el 07/08 y el que
    ya usa `informes/_estado_cuenta_impreso.html`.
    """
    import re

    from modules.mi_cartera import views

    def _ec(cod):
        d = _ec_con_facturas(cod)
        # La 101: 500 − 200 de abono − 98,76 de retención = 201,24.
        d["facturas"][1]["retencion"] = 98.76
        d["facturas"][1]["saldo"] = 201.24
        d["totales"].update(retencion=98.76, saldo=451.24, saldo_neto=451.24)
        return d

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente", _ec)
    ficha = vendedor_logueado.get("/mi-cartera/cliente/TDV").data.decode()

    assert "Retenc." in ficha, "falta la columna de retención"

    filas = re.findall(r"<tr[^>]*>(.*?)</tr>", ficha, re.S)
    rets = [re.search(r'class="mono ret">(.*?)</td>', f).group(1).strip()
            for f in filas if 'class="mono ret"' in f]
    # Tres facturas + el pie. Sin retención va un guión, igual que el abono:
    # un "0,00" alineado con las cifras pide la misma atención y no dice nada.
    assert rets == ["—", "98,76", "—", "98,76"]

    # Y la fila cierra: importe − retención − abono = saldo.
    fila_101 = next(f for f in filas if "500,00" in f)
    numeros = re.findall(r"([\d.]+,\d\d)", fila_101)
    assert numeros[:4] == ["500,00", "98,76", "200,00", "201,24"]


def test_el_abonado_es_una_columna_y_sin_abono_va_un_guion(ficha):
    """Dueña 2026-08-05: *"agregar la columna de abono"*.

    Estuvo como renglón suelto bajo cada fila para ahorrar ancho en 390 px, y
    eso hacía lo contrario de lo que buscaba: el abono es el dato que el
    vendedor CONTRASTA contra el importe cuando el cliente le discute un
    saldo, y contrastar dos números exige tenerlos uno al lado del otro. Un
    renglón debajo se lee como una nota al pie.

    Sin abono va un guión y no «0,00»: un cero alineado con las cifras pide la
    misma atención que ellas y no dice nada. Ese era el motivo REAL de la
    decisión vieja, y sobrevive — sin costar la columna.
    """
    import re

    # Ya no existe el renglón suelto: si vuelve, el dato queda duplicado.
    assert "Abonado $" not in ficha
    assert "sub-ab" not in ficha

    filas = re.findall(r"<tr[^>]*>(.*?)</tr>", ficha, re.S)
    abonos = [re.search(r'class="mono ab">(.*?)</td>', f).group(1).strip()
              for f in filas if 'class="mono ab"' in f]
    # Dos facturas con abono, una sin, y el pie con el total.
    assert abonos == ["1.000,00", "200,00", "—", "1.200,00"]


def test_el_numero_de_factura_es_el_mismo_que_en_la_oficina(ficha):
    """La hoja de la oficina muestra 101; el portal mostraba 001-099-000000101.

    Dos rótulos para la misma factura obligan al vendedor a traducir mientras
    discute con el cliente. Se usa la misma expresión que el parcial impreso.

    ⚠ Se mira lo que se VE, no el HTML crudo: desde el 26/08/2026 el link lleva
    el número completo en el `href` para no abrir la factura equivocada
    —`numf` se repite y en 288 documentos vale cero—. Eso no se lee en
    pantalla; lo que no puede aparecer es el número largo en el TEXTO.
    """
    import re as _re
    visible = _re.sub(r"<[^>]+>", " ", ficha)
    assert "001-099" not in visible


def test_la_ficha_cierra_como_el_dbase(ficha):
    """Los tres renglones de CUENTA.PRG L365-392, igual que la hoja impresa."""
    assert "Cheques a depositar" in ficha
    assert "80,00" in ficha
    # TOTAL = saldo neto + cheques por cobrar = 550 + 80.
    assert "630,00" in ficha


def test_la_ficha_llama_a_cada_numero_por_separado(vendedor_logueado, monkeypatch):
    """🐞 TMT 2026-08-24. El campo teléfono de ATE trae DOS números separados
    por un espacio ("032511676 0990010980") y la ficha armaba un solo link
    pegándolos: el celular marcaba "0325116760990010980".

    El caso de al lado —"0989 506 447", que es UN número escrito con
    espacios— es el que prohíbe arreglarlo cortando por el espacio; lo cubre
    `test_el_vendedor_ve_como_contactar_al_cliente`.
    """
    from modules.mi_cartera import views

    def _dos_numeros(cod):
        d = _ec_con_facturas(cod)
        d["cliente"]["telefono"] = "032511676 0990010980"
        return d

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente", _dos_numeros)
    html = vendedor_logueado.get("/mi-cartera/cliente/TDV").data.decode()
    assert 'href="tel:032511676"' in html
    assert 'href="tel:0990010980"' in html
    assert "0325116760990010980" not in html


# ---------------------------------------------------------------------------
# El resumen entra en UNA pantalla — TMT 2026-08-24, en un Android:
# *"se ve mal, ocupa más de una pantalla"* → *"el resumen del cliente"*.
#
# La regla: no se saca un dato, se saca el renglón que REPITE un número que ya
# está a la vista. Estos tests fijan las dos mitades de esa regla, porque la
# mitad fácil de romper es la segunda (que con datos reales SÍ salga todo).
# ---------------------------------------------------------------------------


def _sin_vencido_ni_cheques(cod):
    d = _ec_con_facturas(cod)
    t = d["totales"]
    t.update(saldo_vencido=0.0, n_vencidas=0, cheques_por_cobrar=0.0,
             cheques_a_depositar=0.0)
    for f in d["facturas"]:
        f["vencimiento"] = date(2099, 1, 1)
    return d


def _hero(html: str) -> str:
    """Sólo el recuadro violeta, desde el hero hasta los botones.

    ⚠ Las aserciones NO pueden mirar la página entera: los comentarios del CSS
    del portal nombran los mismos rótulos ("Cheques por cobrar", "Total") y un
    `not in html` daba verde/rojo por un comentario."""
    i = html.index('<div class="hero">')
    return html[i:html.index('<div class="acc4', i)]


def _ficha_de(cliente_client, monkeypatch, datos):
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente", datos)
    r = cliente_client.get("/mi-cartera/cliente/TDV")
    assert r.status_code == 200
    return r.data.decode()


def test_sin_vencido_ni_cheques_el_hero_no_repite_el_saldo(
        vendedor_logueado, monkeypatch):
    """"Por vencer" y "Total" son, en ese caso, el mismo saldo grande de
    arriba dicho por segunda y tercera vez. Se dicen en la bajada."""
    hero = _hero(_ficha_de(vendedor_logueado, monkeypatch, _sin_vencido_ni_cheques))
    assert "Por vencer" not in hero
    assert "Cheques por cobrar" not in hero
    assert "Total" not in hero
    # Pero el dato NO se pierde: se dice con palabras.
    assert "ninguna vencida" in hero
    assert "sin cheques en cartera" in hero


def test_con_vencido_y_cheques_salen_las_cuatro_celdas(ficha):
    """Y acá cada una dice algo distinto, así que salen enteras."""
    hero = _hero(ficha)
    for rotulo in ("Vencido", "Por vencer", "Cheques por cobrar", "Total"):
        assert rotulo in hero
    assert "sin cheques en cartera" not in hero


def test_el_boton_de_imprimir_no_es_un_caracter_raro(ficha):
    """El "⎙" (U+2399) no existe en las tipografías de Android: en el celular
    del vendedor el botón salía con el cuadradito del glifo que falta. Mismo
    problema que el emoji de WhatsApp el 20/08, mismo arreglo: máscara SVG."""
    i = ficha.index('<div class="acc4')
    assert "\u2399" not in ficha[i:i + 900]
    assert "--ico-pr" in ficha


def test_el_vendedor_ve_como_contactar_al_cliente(ficha):
    """Dueña 2026-08-04: el contacto sí, va a visitarlo."""
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

    # TMT 2026-08-26: la hoja se arma con `estado_cuenta_lote` —los estados de
    # cuenta de todos los clientes en una consulta, no uno por uno—. El fake
    # recibe la LISTA de códigos, que es además lo que fija el orden.
    def _ec_lote(cods):
        vistos.extend(cods)
        return {cod: {"cliente": {"codigo_cli": cod, "nombre": cod, "cupo": 5},
                      "facturas": [], "cheques": [], "anticipos": [],
                      "totales": _totales()} for cod in cods}

    monkeypatch.setattr(views.informes_queries, "estado_cuenta_lote", _ec_lote)
    r = vendedor_logueado.get("/mi-cartera/imprimir")
    assert r.status_code == 200
    assert b"Imprimir estados de cuenta" in r.data
    # Mismo orden que el lote de la oficina: ALFABÉTICO POR CÓDIGO desde el
    # 04/08 (pedido de Alex). Acá había quedado por saldo descendente —
    # BBB (900) antes que AAA (100)— y las dos rutas rendean la MISMA hoja.
    assert vistos == ["AAA", "BBB"]


def test_el_volver_de_la_hoja_no_manda_al_vendedor_a_un_404(vendedor_logueado,
                                                             monkeypatch):
    """⭐ TMT 2026-08-27: "a veces clickean algo como ver estado de cuenta y al
    volver para atrás dice error 404".

    El respaldo del botón «← Volver» de la hoja compartida era
    /informes/estado-cuenta — la landing de la OFICINA, que el allowlist le
    404ea al vendedor — y `volver_a` cae al respaldo EXACTAMENTE cuando el
    navegador no manda referrer: la PWA, una pestaña nueva, un arranque en
    frío. Por eso era "a veces": navegando normal, el referrer lo salvaba y el
    botón volvía bien. Para un vendedor el respaldo es SU lista de clientes;
    para la oficina no cambia nada."""
    import re

    from modules.mi_cartera import views

    monkeypatch.setattr(
        q, "mis_clientes",
        lambda vend: [{"codigo_cli": "AAA", "nombre": "Uno", "saldo": 100.0,
                       "vencido": 0, "provincia": "", "n_facturas": 1,
                       "vence_mas_viejo": None}],
    )
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_lote",
        lambda cods: {cod: {"cliente": {"codigo_cli": cod, "nombre": cod,
                                        "cupo": 5},
                            "facturas": [], "cheques": [], "anticipos": [],
                            "totales": _totales()} for cod in cods})
    # SIN referrer, que es el caso que rompía.
    r = vendedor_logueado.get("/mi-cartera/imprimir")
    assert r.status_code == 200
    m = re.search(r'href="([^"]+)"[^>]*>← Volver', r.data.decode())
    assert m, "no está el botón Volver"
    assert m.group(1) == "/mi-cartera/clientes", (
        f"el Volver del vendedor manda a {m.group(1)}, que su allowlist 404ea")


def test_la_lista_de_clientes_sale_alfabetica_por_codigo(vendedor_logueado, monkeypatch):
    """TMT 2026-08-11: *"que se ordene alfabéticamente"* + *"ordena por codigo todo"*.

    Por CÓDIGO — el MISMO orden que la hoja impresa. Los dos clientes de acá
    tienen el código y el nombre cruzados a propósito: si se ordenara por
    nombre, la lista saldría al revés y el test no lo notaría.
    """
    monkeypatch.setattr(
        q, "mis_clientes",
        lambda vend: [{"codigo_cli": "ZZZ", "nombre": "Almacén", "saldo": 900.0,
                       "vencido": 900.0, "provincia": "", "n_facturas": 1,
                       "vence_mas_viejo": None},
                      {"codigo_cli": "AAA", "nombre": "Zapatería", "saldo": 100.0,
                       "vencido": 0, "provincia": "", "n_facturas": 1,
                       "vence_mas_viejo": None}],
    )
    html = vendedor_logueado.get("/mi-cartera/clientes").data.decode()
    assert html.index("Zapatería") < html.index("Almacén")


def test_el_cuadradito_de_la_lista_dice_el_CODIGO_de_3_letras(
        vendedor_logueado, monkeypatch):
    """TMT 2026-08-11: *"todos lados donde aparece el código, mantené nuestros
    códigos que tienen 3 letras, no de a dos"*.

    Eran las iniciales del NOMBRE: dos letras decorativas. Mismo cambio que ya
    se había hecho en el Inicio el 05/08.
    """
    monkeypatch.setattr(
        q, "mis_clientes",
        lambda vend: [{"codigo_cli": "RTU", "nombre": "ROSA TUAPANTA",
                       "saldo": 100.0, "vencido": 0, "provincia": "PICHINCHA",
                       "n_facturas": 1, "vence_mas_viejo": None}],
    )
    html = vendedor_logueado.get("/mi-cartera/clientes").data.decode()
    assert '<div class="ini cod">RTU</div>' in html
    assert '<div class="ini">RT</div>' not in html
    # Y el renglón de abajo NO lo repite: quedó sólo la provincia.
    fila = html.split('class="rowitem"')[1]
    assert "<span>RTU" not in fila
    assert "<span>PICHINCHA</span>" in fila


def test_el_redondel_de_la_cuenta_dice_el_codigo_del_vendedor(
        vendedor_logueado, monkeypatch):
    """El avatar decía "PP" (iniciales de Patricio Proaño). Su código es PPR."""
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Patricio Proaño")
    monkeypatch.setattr(q, "comision_meses", lambda *a, **k: _MESES_DEMO)
    monkeypatch.setattr(q, "comision_por_cliente", lambda *a, **k: [])
    html = vendedor_logueado.get("/mi-cartera/comision").data.decode()
    assert '<span class="avatar">PPR</span>' in html
    assert '<span class="avatar">PP</span>' not in html


def test_las_alertas_del_inicio_siguen_yendo_por_vencido(vendedor_logueado, monkeypatch):
    """El Inicio muestra los 5 de MAYOR vencido.

    Colgaba del ORDER BY de `mis_clientes`. Al ordenar la lista de clientes
    alfabéticamente, si el orden se heredara de la query el Inicio pasaría a
    mostrar cinco vencidos cualesquiera — sin error y sin síntoma.
    """
    from modules.mi_cartera import views

    def _fila(cod, nombre, venc):
        return {"codigo_cli": cod, "nombre": nombre, "saldo": venc,
                "vencido": venc, "provincia": "", "n_facturas": 1,
                "vence_mas_viejo": None}

    monkeypatch.setattr(
        q, "mis_clientes",
        lambda vend: [_fila("AAA", "Almacén", 10.0), _fila("ZZZ", "Zapatería", 900.0)],
    )
    capturado = {}
    orig = views.render_template

    def _render(nombre_tpl, **ctx):
        if nombre_tpl == "mi_cartera/inicio.html":
            capturado["alertas"] = ctx["alertas"]
        return orig(nombre_tpl, **ctx)

    monkeypatch.setattr(views, "render_template", _render)
    vendedor_logueado.get("/mi-cartera")
    assert [c["codigo_cli"] for c in capturado["alertas"]] == ["ZZZ", "AAA"]


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
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: 334_524.01)
    monkeypatch.setattr(q, "ventas_kg_en_meses", lambda vend, anio, meses: 750.34)

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
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: 90_000.0)
    ctx = views._anio_vs_meta("RMY", date(2026, 8, 3))
    assert ctx["vendido_anio"] == 90_000.0 and ctx["nota_anio"] == ""


def test_sin_meta_no_hay_nota_ni_anillo(app, monkeypatch):
    from datetime import date

    from modules.mi_cartera import views

    monkeypatch.setattr(q, "meta_anio", lambda vend, anio: None)
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: 5.0)
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
    assert "coalesce(monto, 0) <> 0" in vistos["sql"]  # sin migrar aún


def test_cheque_sin_numero_no_queda_pelado(vendedor_logueado, monkeypatch):
    """Hay filas sin N° de cheque (depósitos directos del dBase).

    Sin fallback la ficha mostraba «Ch. · DEP.PICH.» — visto en vivo con MWI.
    TMT 2026-08-05 (Andrés: "los números de los cheques se ven feo"): el
    fallback NO puede ser el id interno de la base. El vendedor le muestra
    esta pantalla al cliente y "#4242" se lee como si fuera el número de SU
    cheque. La celda queda VACÍA (2ª vuelta: una columna de rayas apiladas pesa
    más que el dato que falta) y la fila se identifica por fecha, importe y
    banco. El dato no va a aparecer: ningún DBF de cheques del dBase tiene
    columna de número.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_cliente",
        lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "X", "provincia": ""},
            "facturas": [],
            "cheques": [{"id_cheque": 4242, "no_cheque": "  ", "importe": 10.0,
                         "stat": "Z", "fechad": None, "fechaout": None,
                         "por_cobrar": True, "nombre_banco": "DEP.PICH."}],
            "anticipos": [],
            "totales": _totales(cheques_total=10.0, cheques_por_cobrar=10.0),
        },
    )
    r = vendedor_logueado.get("/mi-cartera/cliente/X?tab=cheques")
    assert r.status_code == 200
    assert b"#4242" not in r.data, "el id interno de la base no va en pantalla"
    assert "Ch. ·".encode() not in r.data


def test_el_portal_solo_lista_los_cheques_en_cartera(vendedor_logueado, monkeypatch):
    """El portal imprime la MISMA hoja que la oficina, así que corta igual.

    TMT 2026-08-05 (Andrés). Un cheque ya depositado no es plata que el
    cliente deba: listarlo hace pensar que se le cobra dos veces.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_cliente",
        lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "X", "provincia": ""},
            "facturas": [],
            "cheques": [
                {"id_cheque": 1, "no_cheque": "111", "importe": 10.0,
                 "stat": "Z", "fechad": None, "fechaout": None,
                 "por_cobrar": True, "nombre_banco": "PICHINCHA"},
                {"id_cheque": 2, "no_cheque": "222", "importe": 99.0,
                 "stat": "B", "fechad": None, "fechaout": None,
                 "por_cobrar": False, "nombre_banco": "PICHINCHA"},
            ],
            "anticipos": [],
            "totales": _totales(cheques_total=109.0, cheques_por_cobrar=10.0),
        },
    )
    r = vendedor_logueado.get("/mi-cartera/cliente/X?tab=cheques")
    assert r.status_code == 200
    assert b"111" in r.data
    assert b"222" not in r.data, "el cheque depositado no va en la hoja del cliente"
    assert "Total (1 cheque)".encode() in r.data


def test_sin_cheques_en_cartera_no_dice_que_no_tiene_ninguno(
    vendedor_logueado, monkeypatch,
):
    """Un cliente con todo cobrado no "no tiene cheques cargados"."""
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_cliente",
        lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "X", "provincia": ""},
            "facturas": [],
            "cheques": [{"id_cheque": 2, "no_cheque": "222", "importe": 99.0,
                         "stat": "B", "fechad": None, "fechaout": None,
                         "por_cobrar": False, "nombre_banco": "PICHINCHA"}],
            "anticipos": [],
            "totales": _totales(cheques_total=99.0, cheques_depositados=99.0),
        },
    )
    r = vendedor_logueado.get("/mi-cartera/cliente/X?tab=cheques")
    assert "No tiene cheques en cartera".encode() in r.data
    assert "no tiene cheques cargados".encode() not in r.data


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
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: 750.34)
    monkeypatch.setattr(q, "meta_periodo", lambda *a, **k: None)
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Roberto Miranda")
    monkeypatch.setattr(q, "por_cobrar",
                        lambda vend: {"saldo": 0, "vencido": 0, "n_clientes": 0})

    monkeypatch.setattr(q, "ventas_kg_por_semana",
                        lambda *a, **k: [{"semana": date(2026, 8, 3), "total": 750.34}])
    with app.test_request_context("/mi-cartera?vend=RMY"):
        g.user, g.permisos = {"vend": "RMY"}, {"micartera.ver"}
        assert 'class="bars"' not in views.inicio()

    monkeypatch.setattr(
        q, "ventas_kg_por_semana",
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


# ---------------------------------------------------------------------------
# La venta del vendedor se mide en KILOS (dueña 2026-08-05)
# ---------------------------------------------------------------------------
# *"En las ventas de mes por favor poner los kilos, las metas se mide en
# kilos (esto arreglas en la app y en metas que ponemos en el programa)"*.
#
# El kilo es la unidad en la que se negocia el precio, se planifica la
# producción y se pone la meta. En dólares, "vendió 10% más" mezcla dos
# noticias distintas: vendió más tela, o vendió la misma tela más cara.


def _captura_sql(monkeypatch, filas):
    """Devuelve un dict que se va llenando con el SQL que la query ejecutó."""
    visto = {}

    def _one(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split()).lower()
        return filas

    monkeypatch.setattr(q.db, "fetch_one", _one)
    return visto


def test_las_ventas_del_portal_suman_kilos_y_no_dolares(monkeypatch):
    """El corazón del cambio: la columna que se suma es `f.kg`.

    Es lo único que no se ve mirando la pantalla — kilos y dólares son dos
    números que se suman igual, y una tarjeta que dijera "3.667,80 kg"
    sumando importes no tendría ningún síntoma.
    """
    visto = _captura_sql(monkeypatch, {"total": 3667.8})
    assert q.ventas_kg("PPR", date(2026, 8, 1), date(2026, 8, 31)) == 3667.8
    assert "sum(f.kg)" in visto["sql"]
    assert "sum(f.importe)" not in visto["sql"]
    # Y sigue sin contar anuladas ni el backfill de Asinfo.
    assert "f.stat <> 'x'" in visto["sql"]
    assert "asinfo-backfill" in visto["sql"]


def test_las_barras_por_semana_estan_en_la_misma_unidad_que_el_anillo(monkeypatch):
    """Si el anillo dijera kilos y las barras dólares, la barra más alta
    podría no ser la semana en la que más se vendió."""
    visto = {}

    def _all(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split()).lower()
        return [{"semana": date(2026, 8, 3), "total": 900.5}]

    monkeypatch.setattr(q.db, "fetch_all", _all)
    assert q.ventas_kg_por_semana("PPR", date(2026, 8, 1), date(2026, 8, 31)) == [
        {"semana": date(2026, 8, 3), "total": 900.5}
    ]
    assert "sum(f.kg)" in visto["sql"] and "sum(f.importe)" not in visto["sql"]


def test_el_anio_a_medio_cargar_tambien_se_compara_en_kilos(monkeypatch):
    visto = _captura_sql(monkeypatch, {"total": 44738.3})
    assert q.ventas_kg_en_meses("PPR", 2026, [7]) == 44738.3
    assert "sum(f.kg)" in visto["sql"] and "sum(f.importe)" not in visto["sql"]


def _inicio_render(app, monkeypatch, *, vendido, meta, semanas=None):
    from modules.mi_cartera import views

    monkeypatch.setattr(views, "today_ec", lambda: date(2026, 8, 5))
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: vendido)
    monkeypatch.setattr(q, "meta_periodo", lambda *a, **k: meta)
    monkeypatch.setattr(q, "ventas_kg_por_semana", lambda *a, **k: semanas or [])
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: 12345.67)
    monkeypatch.setattr(q, "comision", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision_mes", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Patricio Proaño")
    monkeypatch.setattr(q, "por_cobrar",
                        lambda vend: {"saldo": 0, "vencido": 0, "n_clientes": 0})
    with app.test_request_context("/mi-cartera?vend=PPR&periodo=mes"):
        g.user, g.permisos = {"vend": "PPR"}, {"micartera.ver"}
        return views.inicio()


def test_la_tarjeta_de_ventas_dice_kilos_y_no_pesos(app, monkeypatch):
    """La unidad va escrita AL LADO del número, no sólo en el título.

    El vendedor mira esta tarjeta pegada a Cobrado, Por cobrar y Comisión,
    que siguen en dólares. Un número grande sin unidad, rodeado de pesos, se
    lee como pesos — y 3.667 kg leídos como $3.667 es una conversación
    imposible con la dueña.
    """
    html = _inicio_render(app, monkeypatch, vendido=3667.8, meta=5000.0)
    ventas = html.split("Ventas en kilos")[1].split('<div class="kpis">')[0]
    assert "3.667,80" in ventas and "kg" in ventas
    assert "de 5.000,00 kg" in ventas
    # Ni un signo de dólar dentro de la tarjeta de ventas.
    assert "$" not in ventas
    # Pero abajo, Cobrado sigue siendo plata.
    assert "$ 12.345,67" in html


def test_el_ritmo_tambien_habla_en_kilos(app, monkeypatch):
    """"$ 4.100 arriba del ritmo" con una meta en kilos no significa nada."""
    html = _inicio_render(app, monkeypatch, vendido=3667.8, meta=5000.0)
    assert "kg arriba del ritmo" in html or "kg abajo del ritmo" in html


def test_sin_meta_cargada_igual_se_ven_los_kilos(app, monkeypatch):
    """Sin meta no hay anillo (un 0% falso es peor que nada), pero el
    facturado del período tiene que seguir a la vista — y en kilos."""
    html = _inicio_render(app, monkeypatch, vendido=3667.8, meta=None)
    ventas = html.split("Ventas en kilos")[1].split('<div class="kpis">')[0]
    assert "3.667,80" in ventas and "kg" in ventas
    assert "$" not in ventas
    assert "sin meta cargada" in ventas


# ── La meta se GUARDA en kilos ─────────────────────────────────────────────


def test_la_columna_de_la_meta_se_resuelve_en_runtime(monkeypatch):
    """El deploy NO corre migraciones: entre que el código llega al server y
    que alguien aprieta "Aplicar pendientes", la columna todavía se llama
    `monto`. Hardcodear `kg` dejaría la pantalla de metas rota justo en esa
    ventana, que es cuando nadie está mirando.
    """
    q._reset_cache_columna_meta()
    monkeypatch.setattr(q.db, "fetch_one", lambda *a, **k: None)
    assert q._columna_meta() == "monto"

    q._reset_cache_columna_meta()
    monkeypatch.setattr(q.db, "fetch_one", lambda *a, **k: {"ok": 1})
    assert q._columna_meta() == "kg"
    q._reset_cache_columna_meta()


def test_el_negativo_de_la_columna_caduca_y_el_positivo_no(monkeypatch):
    """⭐ El bug del 2026-08-03 con `usuario.vend`: la migración se aplicó
    bien (exit 0) y la app siguió convencida de que la columna no existía,
    porque había guardado el "no está" para toda la vida del proceso. Único
    síntoma: un guardado que se rechaza pidiendo correr una migración que ya
    corriste. Nada en los logs.

    Una columna que existe no desaparece → el positivo es definitivo. El
    negativo cambia solo en el momento en que alguien aprieta "Aplicar
    pendientes" → tiene que caducar.
    """
    q._reset_cache_columna_meta()
    llamadas = {"n": 0}
    reloj = {"t": 1000.0}

    # ⚠ El reloj se mueve de verdad, no se pisa el timestamp guardado. Poner
    # `_COL_META_CHEQUEADO_EN = 0` saltea el chequeo del TTL en vez de
    # ejercitarlo: con esa versión, un TTL de mil millones de segundos pasaba
    # el test igual. (Mutante que sobrevivió al escribirlo, 2026-08-05.)
    monkeypatch.setattr(q.time, "monotonic", lambda: reloj["t"])

    def _one(*a, **k):
        llamadas["n"] += 1
        return None

    monkeypatch.setattr(q.db, "fetch_one", _one)
    assert q._columna_meta() == "monto" and llamadas["n"] == 1
    # Dentro del TTL no vuelve a preguntar…
    reloj["t"] += q._COL_META_TTL_NEGATIVO / 2
    assert q._columna_meta() == "monto" and llamadas["n"] == 1
    # …pero pasado el TTL sí, y ahí ve la columna nueva.
    reloj["t"] += q._COL_META_TTL_NEGATIVO
    monkeypatch.setattr(q.db, "fetch_one", lambda *a, **k: {"ok": 1})
    assert q._columna_meta() == "kg"

    # Y el TTL tiene que ser CORTO: un negativo que dura una hora es lo mismo
    # que uno que dura para siempre, porque la migración se corre y se prueba
    # en el mismo minuto.
    assert q._COL_META_TTL_NEGATIVO <= 300

    # Y el positivo ya no se vuelve a preguntar nunca.
    def _explota(*a, **k):
        raise AssertionError("el positivo no se re-consulta")

    monkeypatch.setattr(q.db, "fetch_one", _explota)
    assert q._columna_meta() == "kg"
    q._reset_cache_columna_meta()


def test_guardar_meta_escribe_kilos_en_la_columna_que_existe(monkeypatch):
    q._reset_cache_columna_meta()
    monkeypatch.setattr(q.db, "fetch_one", lambda *a, **k: {"ok": 1})
    visto = {}

    def _exec(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split()).lower()
        visto["params"] = params

    monkeypatch.setattr(q.db, "execute", _exec)
    q.guardar_meta("rmy", 2026, 8, "1200", usuario="tamara")
    assert "insert into scintela.vendedor_meta" in visto["sql"]
    assert "(codigo, anio, mes, kg," in visto["sql"]
    assert "monto" not in visto["sql"]
    assert visto["params"][:4] == ("RMY", 2026, 8, "1200")
    q._reset_cache_columna_meta()


def test_la_migracion_0163_renombra_y_no_convierte_dolares_a_kilos():
    """Convertir la meta vieja ($10.000 de RMY) a kilos dividiéndola por un
    precio promedio inventaría un número que nadie decidió. Se borra y la
    dueña la recarga.

    Y se RENOMBRA la columna en vez de agregar una al lado: una tabla con
    `monto` y `kg` a la vez es una invitación a que la mitad del código lea
    la equivocada, y el nombre viejo no dejaría rastro de que el significado
    cambió.
    """
    from pathlib import Path

    sql = Path("migrations/0163_vendedor_meta_en_kilos.sql").read_text()
    assert "rename column monto to kg" in sql.lower()
    assert "delete from scintela.vendedor_meta" in sql.lower()
    # El DELETE NO puede ser pelado: entre el deploy y la migración la dueña
    # ya puede haber cargado kilos, y un DELETE sin WHERE se los lleva.
    borrado = sql.lower().split("delete from scintela.vendedor_meta")[1]
    assert "where" in borrado.split(";")[0]
    # Idempotente: si ya corrió, no vuelve a borrar.
    assert "not exists" in sql.lower() and "column_name  = 'kg'" in sql


def test_la_pantalla_de_metas_de_la_oficina_dice_kilos():
    """Dueña: *"esto arreglas en la app y en metas que ponemos en el
    programa"*. Una grilla de doce cajas vacías sin unidad se llena en
    dólares por costumbre — y el error recién se ve en el celular del
    vendedor, semanas después."""
    from pathlib import Path

    tpl = Path("modules/mi_cartera/templates/mi_cartera/metas.html").read_text()
    assert "KILOS" in tpl
    assert 'placeholder="kg"' in tpl
    assert "kg_es" in tpl and "money_es" not in tpl


# ---------------------------------------------------------------------------
# El buscador de clientes (dueña 2026-08-05)
# ---------------------------------------------------------------------------
# *"la opción de buscar el cliente para que no tengan que desplazarse para
# encontrar"*. El buscador YA existía: el problema era que con 94 clientes se
# iba con el scroll y encima había que apretar Enter y esperar la recarga.


_CLIENTES_DEMO = [
    {"codigo_cli": "LMS", "nombre": "LUIS ALBERTO MIRANDA SADAREAGA",
     "provincia": "TUNGURAHUA", "saldo": 64161.49, "vencido": 64161.49,
     "vence_mas_viejo": date(2026, 6, 1), "n_facturas": 12},
    {"codigo_cli": "BAN", "nombre": "BASILIO NAULA", "provincia": "TUNGURAHUA",
     "saldo": 42495.92, "vencido": 0.0, "vence_mas_viejo": None,
     "n_facturas": 5},
]


def test_la_lista_muestra_el_cupo_y_el_porcentaje_usado(vendedor_logueado,
                                                        monkeypatch):
    """⭐ TMT 2026-08-20: el cupo y lo usado, EN LA LISTA.

    Estaban sólo adentro de la ficha: para saber a quién le puede cargar más
    mercadería, el vendedor tenía que entrar cliente por cliente. Es el dato
    con el que decide parado en el local, así que va en la fila.

    El % es el MISMO de la ficha —`usado` = saldo neto + cheques por cobrar,
    sobre el cupo— con los mismos cortes 80/100.
    """
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Patricio Proaño")
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [
        {"codigo_cli": "ATE", "nombre": "TENESACA", "provincia": "TUNGURAHUA",
         "saldo": 14465.13, "vencido": 0.0, "vence_mas_viejo": None,
         "n_facturas": 13, "cupo": 10000, "usado": 14465.13},
        {"codigo_cli": "AVE", "nombre": "VERDUGO", "provincia": "PICHINCHA",
         "saldo": 10440.33, "vencido": 0.0, "vence_mas_viejo": None,
         "n_facturas": 4, "cupo": 25000, "usado": 10440.33},
        {"codigo_cli": "DJB", "nombre": "JUSTO", "provincia": "PICHINCHA",
         "saldo": 9900.0, "vencido": 0.0, "vence_mas_viejo": None,
         "n_facturas": 2, "cupo": 10000, "usado": 9900.0},
    ])
    html = vendedor_logueado.get("/mi-cartera/clientes").data.decode()

    assert "Cupo $ 10.000" in html and "Cupo $ 25.000" in html
    # Pasado de cupo: rojo. Holgado: verde. Entre 80 y 100: ámbar.
    assert '"mal"' in html and "145 %" in html      # 14.465 / 10.000
    assert '"bien"' in html and "42 %" in html      # 10.440 / 25.000
    assert '"ojo"' in html and "99 %" in html       # 9.900 / 10.000


def test_sin_cupo_cargado_la_fila_no_dice_nada(vendedor_logueado, monkeypatch):
    """⚠ 57 de los 93 clientes de PPR no tienen cupo cargado.

    "Sin cupo asignado" repetido 57 veces es ruido que empuja para abajo las
    filas que sí dicen algo. El aviso vive en la FICHA, que es donde se va a
    mirar ese cliente en particular. Mismo criterio que el resto del portal:
    sin meta no hay anillo, con una sola semana no hay barras.
    """
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Patricio Proaño")
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [
        {"codigo_cli": "BAC", "nombre": "ACHIG", "provincia": "COTOPAXI",
         "saldo": 2061.78, "vencido": 0.0, "vence_mas_viejo": None,
         "n_facturas": 1, "cupo": 0, "usado": 2061.78},
    ])
    html = vendedor_logueado.get("/mi-cartera/clientes").data.decode()
    assert "ACHIG" in html
    assert 'class="cupo"' not in html
    assert "sin cupo" not in html.lower().split("<style")[0]


def test_lo_usado_de_la_lista_se_cuenta_IGUAL_que_en_la_ficha():
    """⭐ El candado del número: la lista y la ficha tienen que dar el MISMO
    porcentaje del mismo cliente.

    `_USADO_DEL_CUPO` no puede tener su propia lista de estados de cheque
    escrita a mano — sale de `informes.queries`, que es donde viven los stats
    canónicos. Y tiene que restar los espejos de anticipo NB=98: un cliente
    con saldo a favor debe MENOS, no lo mismo.
    """
    from modules.informes.queries import STATS_CHEQUE_POR_COBRAR, _sql_stat_in
    from modules.mi_cartera.queries import _USADO_DEL_CUPO

    assert _sql_stat_in(STATS_CHEQUE_POR_COBRAR) in _USADO_DEL_CUPO
    assert "COALESCE(no_banco, 0) = 98" in _USADO_DEL_CUPO
    # Y los anticipos NO se cuentan dos veces: el bloque de cheques reales los
    # excluye, igual que hace `estado_cuenta_cliente`.
    assert "COALESCE(no_banco, 0) <> 98" in _USADO_DEL_CUPO


def _clientes_html(vendedor_logueado, monkeypatch, url="/mi-cartera/clientes"):
    monkeypatch.setattr(q, "mis_clientes", lambda vend: _CLIENTES_DEMO)
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Patricio Proaño")
    r = vendedor_logueado.get(url)
    assert r.status_code == 200
    return r.data.decode()


def test_el_buscador_vive_en_el_appbar_para_no_irse_con_el_scroll(
        vendedor_logueado, monkeypatch):
    """El appbar ya es `position:sticky`. Hacer sticky el `<form>` dentro de
    `main` obligaría a clavarle un `top:` igual al alto del appbar, que cambia
    con el largo del nombre del vendedor: una constante mágica que se rompe
    sola.
    """
    html = _clientes_html(vendedor_logueado, monkeypatch)
    appbar = html.split('<header class="appbar">')[1].split("</header>")[0]
    assert 'id="q"' in appbar and 'name="q"' in appbar, \
        "el buscador tiene que estar DENTRO del appbar sticky"


def test_el_buscador_filtra_al_tipear_sin_volver_al_servidor(
        vendedor_logueado, monkeypatch):
    """Las 94 filas ya están en la página: buscar entre ellas es esconder
    nodos, no una consulta."""
    html = _clientes_html(vendedor_logueado, monkeypatch)
    assert "addEventListener('input'" in html
    # Cada fila lleva con qué se la busca y cuánto suma al total.
    assert 'data-buscar="luis alberto miranda sadareaga lms"' in html
    assert 'data-saldo="64161.49"' in html


def test_el_filtro_al_tipear_mira_lo_mismo_que_el_del_servidor(
        vendedor_logueado, monkeypatch):
    """Si miraran campos distintos, tipear y apretar Enter daría dos listas
    diferentes — y el vendedor deja de creerle a las dos.

    El del servidor mira nombre + código, en minúsculas, por subcadena.
    """
    html = _clientes_html(vendedor_logueado, monkeypatch)
    for fila in _CLIENTES_DEMO:
        esperado = f"{fila['nombre']} {fila['codigo_cli']}".lower()
        assert f'data-buscar="{esperado}"' in html
    # Y el servidor sigue filtrando igual (sin JS, con Enter).
    cuerpo = _clientes_html(vendedor_logueado, monkeypatch,
                            "/mi-cartera/clientes?q=basilio")
    assert "BASILIO NAULA" in cuerpo and "LUIS ALBERTO" not in cuerpo


def test_el_encabezado_cuenta_lo_que_se_ve_no_lo_que_se_cargo(
        vendedor_logueado, monkeypatch):
    """Un encabezado que dice "94 con saldo · $551.674" sobre una lista de
    tres filas es peor que no tener contador. Ya pasó el 2026-08-03 con el
    filtro Vencidos, que decía "22 con saldo"."""
    html = _clientes_html(vendedor_logueado, monkeypatch)
    assert 'id="sub-conteo"' in html
    # El JS lo reescribe con las filas visibles y el total recalculado.
    assert "sub.textContent" in html
    assert "por cobrar" in html


def test_enter_no_recarga_la_pantalla_ya_filtrada(
        vendedor_logueado, monkeypatch):
    """La lista ya está filtrada: una vuelta al servidor sólo agrega medio
    segundo y hace saltar el scroll al tope."""
    html = _clientes_html(vendedor_logueado, monkeypatch)
    assert "preventDefault()" in html
    # Pero el form SIGUE siendo un form: sin JS, Enter manda y el servidor
    # filtra. Sacarlo dejaría a un celular sin JS sin ninguna búsqueda.
    assert 'id="form-buscar"' in html and 'method="get"' in html


def test_el_appbar_queda_pegado_arriba_con_la_lista_larga():
    """⭐ El appbar decía `position:sticky` desde el día uno y NUNCA lo fue.

    `html,body{height:100%}` deja el <body> con el alto de UNA pantalla aunque
    el contenido mida diez, y un `sticky` está preso dentro de su bloque
    contenedor: pasada la primera pantalla se despega y se va con el scroll.
    Sin error, sin warning, y en el inspector la regla se ve aplicada.

    Se destapó el 2026-08-05 al mudar el buscador al appbar para que dejara de
    irse con el scroll — y comprobando en vivo que se iba igual. Con los 94
    clientes de PPR: body 667 px, documento 6.034 px.

    El tabbar de abajo sí andaba (`bottom:0` queda pegado igual), y por eso el
    bug pasó desapercibido meses. Que un sticky funcione no dice nada del otro.
    """
    from pathlib import Path

    # TMT 2026-08-24: los estilos se mudaron a `_estilos.html` para que el
    # portal del cliente use LOS MISMOS y no una copia. Ver modo.py.
    css = Path("modules/mi_cartera/templates/mi_cartera/_estilos.html").read_text()
    assert "html,body{min-height:100%}" in css
    assert "html,body{height:100%}" not in css, \
        "height:100% en el body vuelve a romper el sticky del appbar"
    # Y el appbar tiene que seguir declarándose sticky: las dos cosas juntas
    # son el arreglo, ninguna alcanza sola.
    assert "position:sticky; top:0" in css


def test_el_atributo_hidden_le_gana_al_display_del_portal():
    """🚨 `hidden` vale display:none, pero lo trae la hoja del NAVEGADOR — y
    cualquier regla del cascarón le gana por origen, no por especificidad.

    `.rowitem{display:flex}` dejaba el `hidden` del buscador sin ningún
    efecto: el filtro marcaba bien las filas, el encabezado decía "1
    encontrado · $ 42.495,92" y abajo seguían los 94 clientes. Lo reportó la
    dueña el 2026-08-05 — *"el filtro no funciona: me dice 1 pero no me
    filtra abajo los clientes"*.

    ⭐ Se me escapó porque verifiqué `elemento.hidden`, que devuelve el
    ATRIBUTO, no si el elemento se ve. Lo que hay que mirar es
    `offsetParent === null`.

    El guard es global y con `!important` a propósito: el problema no es de
    `.rowitem`, es de cualquier selector de este archivo que fije `display`.
    """
    from pathlib import Path

    # TMT 2026-08-24: los estilos se mudaron a `_estilos.html` para que el
    # portal del cliente use LOS MISMOS y no una copia. Ver modo.py.
    css = Path("modules/mi_cartera/templates/mi_cartera/_estilos.html").read_text()
    assert "[hidden]{display:none!important}" in css, \
        "sin este guard, cualquier `display:` del cascarón anula los `hidden`"


# ---------------------------------------------------------------------------
# Mi comisión: dos pestañas (dueña 2026-08-05)
# ---------------------------------------------------------------------------
# *"este mes a mes está muy abajo, pongamos arriba otra tab o algo así"*.
# El mes a mes estaba al pie, debajo del desglose "De qué cobranzas", que mide
# tanto como clientes le cobró el vendedor: con veinte clientes queda a media
# pantalla de scroll. Un dato de resumen no puede vivir detrás de un detalle
# de largo variable.


_MESES_DEMO = [
    {"anio": 2026, "mes": 8, "monto": 591.26},
    {"anio": 2026, "mes": 7, "monto": 3062.75},
    {"anio": 2026, "mes": 6, "monto": 1531.96},
    {"anio": 2026, "mes": 5, "monto": 0.0},
    {"anio": 2026, "mes": 4, "monto": 0.0},
    {"anio": 2026, "mes": 3, "monto": 0.0},
    {"anio": 2026, "mes": 2, "monto": 0.0},
    {"anio": 2026, "mes": 1, "monto": 0.0},
]


def _comision_html(vendedor_logueado, monkeypatch, url="/mi-cartera/comision"):
    monkeypatch.setattr(q, "comision_meses", lambda *a, **k: _MESES_DEMO)
    monkeypatch.setattr(q, "comision_por_cliente", lambda *a, **k: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Roberto Miranda")
    r = vendedor_logueado.get(url)
    assert r.status_code == 200
    return r.data.decode()


def _seg(html):
    """El bloque del control segmentado — el primero de la pantalla."""
    return html.split('<div class="seg">')[1].split("</div>")[0]


def test_la_comision_abre_en_el_mes_con_las_dos_pestanas_arriba(
        vendedor_logueado, monkeypatch):
    """Las pestañas van ANTES que todo: son la respuesta a "¿qué miro?"."""
    html = _comision_html(vendedor_logueado, monkeypatch)
    seg = _seg(html)
    assert "Este mes" in seg and "Mes a mes" in seg
    # Arranca en el mes, y esa pestaña es la marcada.
    assert 'class="on"' in seg.split("Mes a mes")[0]
    # El mes a mes ya NO está al pie de la pantalla del mes.
    assert "Mes a mes ·" not in html
    # Y el desglose, que es lo largo, sigue estando.
    assert "De qué cobranzas" in html


def test_el_desglose_de_la_comision_dice_el_CODIGO_de_3_letras(
        vendedor_logueado, monkeypatch):
    """Mismo pedido del 11/08 que la lista y el Inicio: tres letras, no dos."""
    monkeypatch.setattr(q, "comision_meses", lambda *a, **k: _MESES_DEMO)
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Patricio Proaño")
    monkeypatch.setattr(
        q, "comision_por_cliente",
        lambda *a, **k: [{"codigo_cli": "RTU", "nombre": "ROSA TUAPANTA",
                          "cobrado": 100.0, "comision": 3.0,
                          "cobros": [{"fecha": date(2026, 8, 3), "doc": "1",
                                      "banco": "PICHINCHA", "importe": 100.0}]}],
    )
    html = vendedor_logueado.get("/mi-cartera/comision").data.decode()
    assert '<span class="ini cod">RTU</span>' in html
    assert '<span class="ini">RT</span>' not in html


def test_la_pestana_mes_a_mes_muestra_el_ano_y_no_el_desglose(
        vendedor_logueado, monkeypatch):
    html = _comision_html(vendedor_logueado, monkeypatch,
                          "/mi-cartera/comision?tab=meses")
    assert "Mes a mes ·" in html
    assert "Agosto" in html and "Julio" in html and "Junio" in html
    # La otra pestaña no se renderiza a la vez: si estuviera, el usuario
    # scrollearía igual y las pestañas no habrían resuelto nada.
    assert "De qué cobranzas" not in html


def test_los_meses_sin_comision_no_se_listan_pero_se_dicen(
        vendedor_logueado, monkeypatch):
    """Enero a mayo daban cinco filas de "$ 0,00" que ocupaban media pantalla
    y empujaban abajo los meses que sí tienen plata. Un cero pide la misma
    atención que una cifra y no dice nada — mismo criterio que el guión de la
    columna Abonado.

    Pero se AVISA cuántos son: sin la línea, un año que arranca en junio
    parece haber perdido los meses de antes.
    """
    html = _comision_html(vendedor_logueado, monkeypatch,
                          "/mi-cartera/comision?tab=meses")
    for mes_cero in ("Enero", "Febrero", "Marzo", "Abril", "Mayo"):
        assert mes_cero not in html, f"{mes_cero} está en 0 y no debería listarse"
    assert "5 meses sin comisión" in html


def test_cambiar_de_pestana_no_te_saca_del_mes_que_estabas_mirando(
        vendedor_logueado, monkeypatch):
    """Perder el mes al tocar una pestaña es el motivo por el que la gente
    deja de tocarlas."""
    html = _comision_html(vendedor_logueado, monkeypatch,
                          "/mi-cartera/comision?anio=2026&mes=6")
    seg = _seg(html)
    assert "anio=2026&amp;mes=6" in seg or "anio=2026&mes=6" in seg
    # Y desde el mes a mes, volver al detalle abre el mes elegido, no el de hoy.
    otra = _comision_html(vendedor_logueado, monkeypatch,
                          "/mi-cartera/comision?tab=meses&anio=2026&mes=6")
    assert "tab=mes&amp;anio=2026&amp;mes=7" in otra \
        or "tab=mes&anio=2026&mes=7" in otra



# ---------------------------------------------------------------------------
# La tarjeta de CRÉDITO — cupo, % usado, descuento, bloqueo
# ---------------------------------------------------------------------------
# Andrés 2026-08-05: *"por favor es clave eso, mostrar cupos y descuentos del
# cliente"* + *"los vendedores tienen apegarse a los cupos y ordenar a los
# clientes"*.


def test_la_ficha_muestra_cupo_usado_y_descuento(ficha):
    """El fixture: cupo 20.000, saldo neto 550 + 80 de cheques = 630 usado.

    El % sale de la MISMA cuenta que la ficha de la oficina —saldo neto MÁS
    cheques por cobrar, sobre el cupo—, no del saldo de facturas a secas. Un
    cheque en cartera todavía no se cobró: ocupa cupo. Si el vendedor y la
    dueña vieran dos porcentajes distintos del mismo cliente, la discusión no
    se puede tener.
    """
    assert "Crédito" in ficha
    assert "20.000" in ficha          # el cupo
    assert "630,00" in ficha          # lo usado = 550 + 80
    assert "3 % del cupo" in ficha    # 630 / 20.000 = 3,15 %


def test_el_boton_de_whatsapp_dice_lo_que_hace_y_no_es_una_flechita(
        app, vendedor_logueado, monkeypatch):
    """⭐ TMT 2026-08-20: *"los vendedores no tienen el botón de enviar por
    whatsapp"*.

    Lo tenían desde el 04/08: era un cuadradito verde con un "⤴" arriba a la
    derecha, apretado contra el de imprimir. La dueña —que sabía que el botón
    se había hecho— miró la pantalla y lo dio por faltante. **Un ícono que
    nadie reconoce es un botón que no existe.**

    Así que baja al cuerpo, debajo de los saldos, con el nombre entero. El
    test fija las dos mitades de lo que lo hace reconocible: el rótulo y que
    ya NO sea la flechita.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente",
                        _ec_con_facturas)
    # El botón sólo se dibuja si el servidor puede generar PDFs; en la suite no
    # hay navegador instalado, así que se dice que sí.
    monkeypatch.setitem(app.jinja_env.globals, "pdf_disponible", lambda: True)
    ficha = vendedor_logueado.get("/mi-cartera/cliente/TDV").data.decode()

    assert "data-wa-pdf" in ficha
    assert "⤴" not in ficha, "volvió el ícono que nadie entiende"
    assert 'class="a4 wa"' in ficha
    # ⭐ Los cuatro botones entran en UNA fila (TMT 26/08), y los cuatro
    # llevan RÓTULO: un ícono sin rótulo es un botón que no existe.
    for rotulo in ("WhatsApp", "Imagen", "Imprimir", "PDF"):
        assert ">" + rotulo + "<" in ficha, rotulo


def test_la_ficha_manda_el_estado_de_cuenta_como_FOTO(app, vendedor_logueado, monkeypatch):
    """⭐ TMT 2026-08-25: *"el botón enviar por WhatsApp no le sirve a ciertos
    vendedores. Generando y luego no abre el pdf"*.

    Van tres arreglos del botón verde. El camino corto depende de tres cosas
    que no están en todos los teléfonos —el permiso que deja el toque, saber
    armar un `File`, tener menú de compartir— y donde falta alguna, el vendedor
    se queda sin el archivo.

    Este link no depende de ninguna: es una navegación común a la misma URL del
    PDF, que el servidor manda `inline`. El teléfono lo abre en su visor y
    desde ahí el botón de compartir DEL APARATO lo manda a WhatsApp. Un paso
    más, y anda siempre: *"hacemos más inclusivo para que a cualquiera le
    funcione"*.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente",
                        _ec_con_facturas)
    monkeypatch.setitem(app.jinja_env.globals, "pdf_disponible", lambda: True)
    ficha = vendedor_logueado.get("/mi-cartera/cliente/TDV").data.decode()

    # ⭐ El camino que SÍ anda en todos los teléfonos: la hoja como foto.
    # Alex Velastegui, 25/08, con el PDF ya generado en la mano: *"desde el pdf
    # q genera no permite enviar por wsp"* → Tamara: *"creo que foto y
    # compartir como imagen si no?"*. En un teléfono mandar una FOTO lo sabe
    # hacer cualquiera; mandar un DOCUMENTO, no.
    assert ">Imagen<" in ficha
    assert '/mi-cartera/cliente/TDV/imagen"' in ficha, "no apunta a la imagen"
    # ⭐ Y NADA de instrucciones debajo: TMT 26/08 mandó borrar el renglón que
    # explicaba el gesto. El botón se llama "Imagen" y el gesto se aprende una
    # vez; el cartelito se leía todos los días.
    assert "mantenela apretada" not in ficha
    assert 'class="acc4-nota"' not in ficha
    # El PDF no se saca —el que lo prefiere lo tiene— pero es el más chico.
    assert '/mi-cartera/cliente/TDV/pdf"' in ficha


def test_con_muchas_facturas_la_ficha_manda_al_PDF(app, vendedor_logueado, monkeypatch):
    """⚠ WhatsApp recomprime las fotos y le achica el lado largo a ~1600 px.
    Medido con el navegador de verdad: un estado de cuenta de 60 facturas sale
    de 1100 × 2757 y WhatsApp lo deja al 58%, o sea con la letra apretada. Para
    esos clientes el PDF es mejor.

    El vendedor no tiene manera de saber eso mirando la pantalla, así que se lo
    dice la pantalla. Misma regla que el botón verde: puede no poder, pero no
    puede quedarse callado.
    """
    import datetime

    from modules.mi_cartera import views

    base = _ec_con_facturas("TDV")
    f0 = base["facturas"][0]
    base["facturas"] = [dict(f0, id_factura=i, numf=100000 + i,
                             fecha=datetime.date(2026, 1 + i % 7, 1 + i % 27))
                        for i in range(60)]
    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente",
                        lambda c: base)
    monkeypatch.setitem(app.jinja_env.globals, "pdf_disponible", lambda: True)
    ficha = vendedor_logueado.get("/mi-cartera/cliente/TDV").data.decode()

    assert "mejor mandá el PDF" in ficha
    assert "mantenela apretada" not in ficha, "sigue empujando a la foto"
    # La foto NO se saca: se puede mandar igual, sólo deja de ser la recomendada.
    assert ">Imagen<" in ficha
    assert 'class="acc4 flojo"' in ficha, "el verde de la foto sigue prendido"


def test_sin_motor_la_ficha_no_ofrece_ni_el_boton_ni_la_foto(
        app, vendedor_logueado, monkeypatch):
    """Los dos caminos terminan en la misma URL, así que los dos se esconden
    con el mismo `pdf_disponible()`. Un link que siempre da 503 es peor que no
    tenerlo — y era el argumento del botón desde el 04/08."""
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente",
                        _ec_con_facturas)
    monkeypatch.setitem(app.jinja_env.globals, "pdf_disponible", lambda: False)
    ficha = vendedor_logueado.get("/mi-cartera/cliente/TDV").data.decode()

    # Se busca el BOTÓN, no la palabra: los comentarios de la hoja de estilos
    # nombran WhatsApp y viajan al navegador.
    assert "data-wa-pdf" not in ficha
    assert 'class="a4 img"' not in ficha
    assert 'class="a4 pdf"' not in ficha
    # Imprimir no pasa por el motor: sigue estando.
    assert ">Imprimir<" in ficha


def test_la_ficha_no_muestra_la_forma_de_pago(ficha):
    """TMT 2026-08-20: *"no hace falta la C"*.

    `cliente.pago` dice "C" en 3.516 de los 3.900 clientes y está vacío en casi
    todo el resto: no distingue a nadie. Ocupaba uno de los cuatro lugares de
    la tarjeta de Crédito —el lugar más caro de la ficha, al lado del cupo— para
    mostrar siempre la misma letra.
    """
    assert "Forma de pago" not in ficha


def test_los_cheques_en_cartera_OCUPAN_cupo(vendedor_logueado, monkeypatch):
    """⭐ Un cheque en cartera todavía no se cobró: ocupa cupo.

    `_usado` = saldo neto MÁS cheques por cobrar, igual que `_total_fc` de la
    ficha de la oficina. Si el vendedor contara sólo las facturas, vería un
    riesgo MENOR que el real justo en los clientes que más cheques tienen
    dando vueltas — y la dueña, mirando la otra pantalla, vería otro número.
    (Es el mismo bug que la oficina tuvo el 03/08 con `cheques_cartera`.)

    Números elegidos para que las dos cuentas se distingan de verdad: sobre un
    cupo de 1.000, con 500 de facturas y 300 en cheques, contar todo da 80 %
    —ámbar, la primera señal— y contar sólo facturas da 50 %, en verde. Con el
    fixture general (630 sobre 20.000) las dos daban 3 % y el test no probaba
    nada: el mutante sobrevivió.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_cliente",
        lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "Con Cheques SA", "cupo": 1000},
            "facturas": [], "cheques": [], "anticipos": [],
            "totales": _totales(saldo=500.0, saldo_neto=500.0,
                                cheques_por_cobrar=300.0),
        },
    )
    html = vendedor_logueado.get("/mi-cartera/cliente/CCH").data.decode()
    assert "800,00" in html            # 500 + 300
    assert "80 % del cupo" in html
    assert "50 % del cupo" not in html
    # Y a 80 % ya avisa: ámbar, no verde.
    cred = html.split('class="cred"')[1].split("</div></div>")[0]
    assert 'class="ojo"' in cred and 'class="bien"' not in cred


def test_sin_cupo_cargado_dice_sin_cupo_asignado_y_no_cero(
        vendedor_logueado, monkeypatch):
    """⚠ El cupo está cargado en el ~10% de los clientes (EDG 29 de 339, JQU 5
    de 192). Un "$ 0" se lee como "no puede comprar nada", que es lo contrario
    de lo que pasa — y para bloquear existe `stop`, que es otro campo.

    Sin cupo tampoco se dibuja el % ni la barra: no hay contra qué medir.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_cliente",
        lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "Sin Cupo SA", "cupo": 0,
                        "descuento": 0},
            "facturas": [], "cheques": [], "anticipos": [],
            "totales": _totales(saldo=1000.0, saldo_neto=1000.0),
        },
    )
    html = vendedor_logueado.get("/mi-cartera/cliente/SCS").data.decode()
    assert "sin cupo asignado" in html
    assert "% del cupo" not in html
    assert 'class="barra"' not in html


def test_el_cliente_bloqueado_se_avisa_arriba_de_todo(
        vendedor_logueado, monkeypatch):
    """`stop = 'S'` es el campo con el que la oficina frena a un cliente.
    Andrés: *"los vendedores tienen apegarse a los cupos y ordenar a los
    clientes"* — enterarse de que está bloqueado DESPUÉS de tomarle el pedido
    no sirve de nada."""
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_cliente",
        lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "Frenado SA", "cupo": 5000,
                        "stop": "S", "descuento": 10},
            "facturas": [], "cheques": [], "anticipos": [],
            "totales": _totales(),
        },
    )
    html = vendedor_logueado.get("/mi-cartera/cliente/FRE").data.decode()
    assert "Cliente bloqueado" in html
    assert "10,0 %" in html   # el descuento igual se ve


def test_pasado_el_cupo_el_porcentaje_se_pinta_de_rojo(
        vendedor_logueado, monkeypatch):
    """Un 118% en gris no es un aviso. Los cortes son los MISMOS que la ficha
    de la oficina: ámbar desde 80, rojo desde 100."""
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(
        views.informes_queries, "estado_cuenta_cliente",
        lambda cod: {
            "cliente": {"codigo_cli": cod, "nombre": "Pasado SA", "cupo": 1000},
            "facturas": [], "cheques": [], "anticipos": [],
            "totales": _totales(saldo=1180.0, saldo_neto=1180.0),
        },
    )
    html = vendedor_logueado.get("/mi-cartera/cliente/PAS").data.decode()
    assert "118 % del cupo" in html
    assert 'class="mal"' in html
    # La barra se topea en 100: una barra de 118% se sale de la tarjeta.
    assert "width:118" not in html
    assert 'class="barra"><i class="mal"' in html.replace("\n", " ")
    import re
    assert re.search(r'class="barra">.*?width:100%', html, re.S)


# ---------------------------------------------------------------------------
# La grilla de metas de la oficina: vendido real y % de cumplimiento
# ---------------------------------------------------------------------------
# Andrés 2026-08-05: *"me gustaría que en la parte donde tenemos las metas le
# pongas los kilos vendidos reales y el % de cumplimiento de las ventas de cada
# vendedor"*. Una meta sin el real al lado es un número que nadie vuelve a
# mirar: hay que ir a otra pantalla a averiguar si se cumplió, y entonces no
# se averigua.


def _metas_html(app, fake_db, monkeypatch, hoy=None):
    from datetime import date as _date

    from modules.mi_cartera import views

    monkeypatch.setattr(views, "today_ec", lambda: hoy or _date(2026, 8, 5))
    monkeypatch.setattr(q, "vendedores_activos",
                        lambda: [{"codigo": "EDG", "nombre": "Edgar Ramirez"}])
    # ⭐ Decimal, NO int: `vendedor_meta.kg` es NUMERIC y psycopg lo devuelve
    # como Decimal. Con enteros de mentira el test pasaba y la pantalla daba
    # 500 en producción — `float / Decimal` es un TypeError en Jinja. El fake
    # tiene que traer el MISMO tipo que la base o no prueba la pantalla.
    monkeypatch.setattr(q, "metas_del_anio",
                        lambda anio: [{"codigo": "EDG", "mes": 7, "kg": Decimal("50000.00")},
                                      {"codigo": "EDG", "mes": 8, "kg": Decimal("44000.00")},
                                      {"codigo": "EDG", "mes": 12, "kg": Decimal("38000.00")}])
    monkeypatch.setattr(q, "ventas_kg_por_vendedor_mes",
                        lambda anio: {("EDG", 7): 54535.1, ("EDG", 8): 6571.5})

    rid = fake_db.add_role("Jefa", ["metas.editar"])
    uid = fake_db.add_user("jefa2", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    r = c.get("/comisiones/metas")
    assert r.status_code == 200
    return r.data.decode()


def test_la_grilla_de_metas_muestra_lo_vendido_y_el_cumplimiento(
        app, fake_db, monkeypatch):
    html = _metas_html(app, fake_db, monkeypatch)
    # Julio: 54.535 sobre 50.000 = 109 %, y en verde porque pasó la meta.
    # Los kilos van SIN decimales: en una grilla de 12 meses, los centavos de
    # un kilo cuestan ancho y no cambian ninguna decisión.
    assert "54.535" in html
    assert "109%" in html
    assert "text-emerald-600" in html


def test_el_mes_en_curso_no_se_pinta_de_rojo(app, fake_db, monkeypatch):
    """Agosto va 6.571 de 44.000 = 15 %: pintarlo de rojo el día 5 es una
    alarma falsa todos los meses. Va en gris y con un asterisco."""
    html = _metas_html(app, fake_db, monkeypatch)
    assert "6.572" in html
    assert "15%*" in html
    fila_agosto = html.split("6.572")[1].split("</td>")[0]
    assert "text-slate-400" in fila_agosto and "text-red-600" not in fila_agosto


def test_los_meses_que_no_pasaron_no_llevan_ni_real_ni_porcentaje(
        app, fake_db, monkeypatch):
    """Diciembre tiene meta cargada (38.000) y todavía no llegó. Un 0% ahí no
    dice que el vendedor va mal: dice que diciembre no llegó."""
    html = _metas_html(app, fake_db, monkeypatch)
    assert 'value="38000"' in html   # la meta sí se ve, es editable
    # …pero la celda de diciembre no lleva ni el real ni el %.
    dic = html.split('name="meta_EDG_12"')[1].split("</td>")[0]
    assert "%" not in dic and "text-slate-500" not in dic


def test_el_acumulado_del_anio_compara_like_con_like(app, fake_db, monkeypatch):
    """Sólo los meses que YA pasaron y que TIENEN meta.

    Julio (54.535 sobre 50.000) y agosto (6.571 sobre 44.000) → 61.106 sobre
    94.000 = 65 %. Si entrara diciembre —que tiene meta y no llegó— el
    denominador sería 132.000 y daría 46 %: el mismo error del anillo del
    portal, que llegó a marcar 3345 %.
    """
    html = _metas_html(app, fake_db, monkeypatch)
    # La última celda de la fila: la columna Año.
    fila = html.split('name="meta_EDG_1"')[1].split("</tr>")[0]
    anio = fila.split('name="meta_EDG_12"')[1].split("</td>")[1]
    assert "61.107" in anio          # lo vendido en los meses con meta
    assert "65%" in anio


def test_una_sola_query_para_los_kilos_de_todo_el_ano(monkeypatch):
    """72 celdas = 72 consultas si se llama a `ventas_kg()` en un loop. Es el
    mismo error que costó 3.190 ms en la pantalla de comisión."""
    llamadas = {"n": 0}

    def _all(sql, params=None, conn=None):
        llamadas["n"] += 1
        return [{"vend": "EDG", "mes": 7, "kg": 54535.1}]

    monkeypatch.setattr(q.db, "fetch_all", _all)
    assert q.ventas_kg_por_vendedor_mes(2026) == {("EDG", 7): 54535.1}
    assert llamadas["n"] == 1


def test_los_kilos_de_la_grilla_se_cuentan_igual_que_los_del_portal(monkeypatch):
    """Si la grilla de la dueña y el portal del vendedor contaran distinto, la
    conversación del mes siguiente es imposible."""
    visto = {}

    def _all(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split()).lower()
        return []

    monkeypatch.setattr(q.db, "fetch_all", _all)
    q.ventas_kg_por_vendedor_mes(2026)
    assert "sum(f.kg)" in visto["sql"] and "sum(f.importe)" not in visto["sql"]
    assert "f.stat <> 'x'" in visto["sql"]
    assert "asinfo-backfill" in visto["sql"]



# ---------------------------------------------------------------------------
# El rediseño de la grilla (dueña 2026-08-05)
# ---------------------------------------------------------------------------
# *"feos los dos diseños, esto no debería ocupar más de una pantalla… de las
# metas sacá el nombre del vendedor, agosto se disparan los bloques, hacelo
# elegante lindo simple"*.


def test_la_grilla_no_lleva_el_nombre_del_vendedor(app, fake_db, monkeypatch):
    """Son seis códigos que ella conoce de memoria y ocupaban el ancho de dos
    meses. El nombre queda en el `title`, por si entra alguien nuevo."""
    html = _metas_html(app, fake_db, monkeypatch)
    grilla = html.split("<tbody")[1].split("</tbody>")[0]
    assert "EDG" in grilla
    assert ">Edgar Ramirez<" not in grilla
    assert 'title="Edgar Ramirez"' in grilla


def test_todas_las_celdas_de_mes_miden_lo_mismo(app, fake_db, monkeypatch):
    """⭐ Agosto "se disparaba" porque su celda tenía TRES renglones (input +
    vendido + %) contra dos de las demás, y una fila se estira al alto de su
    celda más alta: UNA celda distinta desalinea la tabla entera.

    Ahora vendido y % van en el MISMO renglón, así que cada celda tiene
    exactamente dos: el input y una línea de texto. El test cuenta los `<div>`
    de la celda de agosto (la que tiene meta Y vendido) y los compara con los
    de julio (meta sin cargar, sólo vendido).
    """
    html = _metas_html(app, fake_db, monkeypatch)
    fila = html.split('name="meta_EDG_1"')[1].split("</tr>")[0]
    agosto = fila.split('name="meta_EDG_8"')[1].split("</td>")[0]
    julio = fila.split('name="meta_EDG_7"')[1].split("</td>")[0]
    assert agosto.count("<div") == julio.count("<div") == 1


def test_los_meses_no_se_pueden_ordenar(app, fake_db, monkeypatch):
    """Es una grilla de CARGA, no un listado. Ordenar por "el mes 04" no
    significa nada y las flechitas ↕ sobre doce números son ruido."""
    html = _metas_html(app, fake_db, monkeypatch)
    thead = html.split("<thead")[1].split("</thead>")[0]
    assert thead.count("data-no-sort") == 14   # Vend. + 12 meses + Año


def test_el_value_del_input_se_puede_volver_a_guardar(app, fake_db, monkeypatch):
    """🚨 El `value` de este input se MANDA de vuelta y `guardar_meta` lo pasa
    tal cual al NUMERIC. Formatearlo con `kg_es` lo dejaría como "44.000,00",
    que Postgres no acepta: un cambio de formato que rompe una escritura.

    Se muestra entero cuando es entero — que es como se cargan las metas.
    """
    import re

    html = _metas_html(app, fake_db, monkeypatch)
    for valor in re.findall(r'name="meta_[A-Z0-9]+_\d+"[^>]*?value="([^"]*)"', html):
        if valor:
            float(valor)   # revienta si tiene punto de miles o coma decimal
    assert 'value="44000"' in html

def test_las_pantallas_no_usan_clases_que_el_tailwind_congelado_no_tiene():
    """🚨 `static/tailwind.css` es un build JIT CONGELADO: una clase que no
    existía el día del build NO renderiza, y no avisa por ningún lado —
    no hay error, no hay warning, y en el inspector el elemento tiene la clase
    puesta.

    Mordió el 2026-08-05 rediseñando esta grilla: `w-[68px]`, `text-[10px]`,
    `py-1.5`, `w-16` y `border-l` no están en el build. Los inputs salieron con
    el ancho por defecto del navegador (~177 px en vez de 64) y la tabla se
    cortaba en el mes 06 — exactamente el problema que el rediseño venía a
    arreglar.

    Este test recorre las dos plantillas y falla si aparece una clase Tailwind
    con valor arbitrario (`algo-[...]`) o una de las que ya sabemos que
    faltan. Lo que haya que dimensionar va en el `<style>` propio de la
    plantilla, que siempre funciona.
    """
    import re
    from pathlib import Path

    # Verificadas contra static/tailwind.css el 2026-08-05.
    FALTAN = ("py-1.5", "py-0.5", "mt-0.5", "w-16", "border-l", "leading-none")
    for ruta in ("modules/mi_cartera/templates/mi_cartera/metas.html",
                 "modules/comisiones/templates/comisiones/lista.html"):
        txt = Path(ruta).read_text()
        cuerpo = txt.split("<style>")[0]        # el CSS propio no se revisa
        arbitrarias = re.findall(r'class="[^"]*?\b[a-z-]+\[[^\]]+\]', cuerpo)
        assert not arbitrarias, f"{ruta}: clases arbitrarias {arbitrarias}"
        for clase in FALTAN:
            assert f" {clase}" not in cuerpo and f'"{clase}' not in cuerpo, \
                f"{ruta}: «{clase}» no existe en el build congelado de Tailwind"


def test_lo_que_dimensiona_la_grilla_esta_en_css_propio():
    """El corolario del test de arriba: si el ancho del input no está en el
    `<style>` de la plantilla, está en una clase de Tailwind que puede no
    existir — y entonces no está en ningún lado."""
    from pathlib import Path

    css = Path("modules/mi_cartera/templates/mi_cartera/metas.html").read_text()
    css = css.split("<style>")[1]
    assert ".mt-in{" in css and "width:" in css.split(".mt-in{")[1].split("}")[0]


# ---------------------------------------------------------------------------
# Comisiones y Metas son dato de DUEÑOS (dueña 2026-08-05)
# ---------------------------------------------------------------------------
# *"podés dejar que comisiones y metas no lo vean usuarios INTELA"* → que las
# vean sólo Accionista y Administrador. Sueldos y metas del equipo comercial
# no son dato operativo.


def test_ningun_rol_operativo_ve_comisiones_ni_metas():
    """`comisiones.ver` y `metas.editar` no cuelgan de NINGÚN rol: los únicos
    que pasan son los wildcard (Accionista y Administrador).

    Se mira `config/roles.py`, que es la fuente única — no la base, que se
    siembra desde ahí.
    """
    from config.roles import ROLES

    for nombre, permisos in ROLES:
        if "*" in permisos:
            continue
        assert "comisiones.ver" not in permisos, f"{nombre} todavía ve Comisiones"
        assert "metas.editar" not in permisos, f"{nombre} todavía carga Metas"


def test_el_vendedor_sigue_viendo_SU_comision():
    """⭐ Lo que NO se puede romper al cerrar Comisiones: cada vendedor sigue
    viendo la suya. `/mi-cartera/comision` cuelga de `micartera.ver`, no de
    `comisiones.ver` — son dos pantallas distintas sobre los mismos datos, y
    ésa es exactamente la razón por la que se puede cerrar una sin tocar la
    otra.
    """
    from config.roles import ROLES

    vendedor = dict(ROLES)["Vendedor"]
    assert "micartera.ver" in vendedor
    assert "comisiones.ver" not in vendedor


def test_el_link_del_menu_se_gatea_con_su_propio_permiso():
    """Un link que aparece y da 404 es peor que no tener el link.

    El del menú se gateaba con `informes.ver`, que NO es el permiso de la
    pantalla: al cerrarle Comisiones a los roles operativos, a Gerente y
    Contabilidad les habría quedado el link puesto y muerto.
    """
    from pathlib import Path

    menu = Path("templates/base.html").read_text()
    linea = [x for x in menu.splitlines() if "comisiones.lista" in x and "nav_link" in x]
    assert linea, "no encontré el link de Comisiones en el menú"
    assert "tiene_permiso('comisiones.ver')" in linea[0]

    # Y las pestañas, igual: cada una detrás del permiso de SU pantalla.
    tabs = Path("templates/_partials/tabs_comisiones.html").read_text()
    assert "tiene_permiso(permiso)" in tabs
    assert "'comisiones.ver'" in tabs and "'metas.editar'" in tabs


def test_el_link_del_menu_va_debajo_de_estados_de_cuenta():
    """Federico 2026-08-20: "Comisiones sale de Modificar y va a Clientes y
    proveedores, debajo de Estados de cuenta"."""
    from pathlib import Path

    menu = Path("templates/base.html").read_text()
    i_datos = menu.index('data-key="datos"')
    i_edc = menu.index("informes.estado_cuenta_landing")
    i_com = menu.index("comisiones.lista")
    i_prov = menu.index("proveedores.lista")
    # dentro de la sección, entre Estados de cuenta y Proveedores
    assert i_datos < i_edc < i_com < i_prov
    # y ya no en "Modificar", que arranca antes
    assert menu.index('data-key="modificar"') < i_datos



def test_hay_migracion_que_saca_los_permisos_de_la_BASE():
    """🚨 `config/roles.py` es la fuente canónica, pero el que manda en runtime
    es `seguridad.permiso` en la base: `auth.load_logged_in_user` hace
    `SELECT nombre_opcion FROM seguridad.permiso WHERE id_rol = %s`.

    Editar `roles.py` y no correr nada deja la producción EXACTAMENTE IGUAL —
    y el código pasa todos los tests, incluido el que verifica roles.py. Sin
    esta migración, el cambio existía sólo en el repo.
    """
    from pathlib import Path

    mig = Path("migrations/0164_comisiones_y_metas_solo_duenos.py").read_text()
    assert "DELETE FROM seguridad.permiso" in mig
    assert "comisiones.ver" in mig and "metas.editar" in mig
    # Y no puede borrar de más: sólo esos dos permisos, de todos los roles.
    where = mig.split("DELETE FROM seguridad.permiso")[1].split('"""')[0]
    assert "nombre_opcion = ANY" in where


def test_requieren_atencion_muestra_el_CODIGO_del_cliente(app, monkeypatch):
    """Dueña 2026-08-05: *"acá poné el código de cliente que son 3 letras"*.

    El círculo tenía las INICIALES DEL NOMBRE: decorativas, dos letras que el
    vendedor no usa para nada. El código es el handle real — el que dice por
    teléfono y el que busca.

    Y en esta lista es el único lugar donde puede aparecer: el renglón de
    abajo dice "N facturas abiertas", no el código.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(views, "today_ec", lambda: date(2026, 8, 5))
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: 100.0)
    monkeypatch.setattr(q, "meta_periodo", lambda *a, **k: None)
    monkeypatch.setattr(q, "ventas_kg_por_semana", lambda *a, **k: [])
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision_mes", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Edgar Ramirez")
    monkeypatch.setattr(q, "por_cobrar",
                        lambda vend: {"saldo": 0, "vencido": 0, "n_clientes": 1})
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [
        {"codigo_cli": "RTU", "nombre": "ROSA TUAPANTA", "provincia": "PICHINCHA",
         "saldo": 75947.37, "vencido": 53574.60, "vence_mas_viejo": date(2026, 6, 1),
         "n_facturas": 23},
    ])
    with app.test_request_context("/mi-cartera?vend=EDG&periodo=mes"):
        g.user, g.permisos = {"vend": "EDG"}, {"micartera.ver"}
        html = views.inicio()

    bloque = html.split("Requieren atención")[1]
    assert '<div class="ini cod">RTU</div>' in bloque
    # Y ya no las iniciales del nombre.
    assert '<div class="ini">RT</div>' not in bloque


# ---------------------------------------------------------------------------
# El portal tiene que dejar SALIR (dueña 2026-08-05)
# ---------------------------------------------------------------------------
# *"¿cómo me deslogueo de un usuario? ejemplo de un vendedor"*. No se podía:
# no había ni botón ni link. Tipear /logout a mano o borrar la cookie, con
# sesiones de 31 días.


def test_el_vendedor_puede_cerrar_sesion(vendedor_logueado, monkeypatch):
    """La otra mitad del bug del 2026-08-03: ese día se arregló que quien
    entraba con "👁 Ver como" pudiera volver, y quedó abierto el caso de una
    sesión REAL — el vendedor en su propio celular.

    ⭐ TMT 2026-08-05, sobre la primera versión: *"el salir debería ser un
    botón desde el nombre, no siempre visible ahí abajo"*. Estaba como cuarta
    pestaña del tabbar y eso está mal por dos razones: el tabbar es para lo
    que se usa TODOS LOS DÍAS —Inicio, Clientes, Comisión— y cerrar sesión se
    hace una vez cada mucho; y ponía la acción destructiva al lado de las de
    navegar, donde se toca sin querer. Ahora cuelga del avatar.
    """
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Roberto Miranda")
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "meta_periodo", lambda *a, **k: None)
    monkeypatch.setattr(q, "ventas_kg_por_semana", lambda *a, **k: [])
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision_mes", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "por_cobrar",
                        lambda vend: {"saldo": 0, "vencido": 0, "n_clientes": 0})

    html = vendedor_logueado.get("/mi-cartera").data.decode()

    # El tabbar es SÓLO para navegar: nada de salir ahí abajo.
    # ⚠ Antes esto contaba tres links. Se cambió por la regla de verdad —que no
    # haya una salida en el tabbar— cuando el 25/08/2026 entró una cuarta
    # pestaña de navegación (Competencia): contar links hacía fallar el test
    # por agregar una pantalla, que es justo lo que el test NO quiere impedir.
    tabbar = html.split('<nav class="tabbar">')[1].split("</nav>")[0]
    assert "/logout" not in tabbar
    assert "Salir" not in tabbar
    assert "<form" not in tabbar, "ninguna acción, sólo links de navegar"

    # Y la salida cuelga del avatar, en un <details> nativo (sin JS: en un
    # celular con mala señal es justo cuando uno quiere poder salir).
    cuenta = html.split('<details class="cuenta">')[1].split("</details>")[0]
    assert "/logout" in cuenta and "Salir" in cuenta
    assert 'class="avatar"' in cuenta
    # POST y con CSRF: un GET a /logout lo dispara cualquier <img> de un mail.
    assert 'method="post"' in cuenta
    assert "csrf_token" in cuenta


def test_la_ficha_tecnica_abre_en_otra_pestana(vendedor_logueado, monkeypatch):
    """La quinta pestaña (dueña 2026-08-25) lleva a un programa APARTE.

    ⚠ Tiene que abrir en otra pestaña: las fichas técnicas viven fuera de
    Programa Core, con su propio "Salir" y sin un "← volver". Abriendo encima,
    el vendedor queda afuera de Mi Cartera sin más salida que el botón de atrás
    del celular — el mismo bug que el candado del 2026-08-03, por otro camino.

    Y `rel="noopener"`, que es lo que impide que la pestaña nueva toque el
    `window.opener` de ésta.
    """
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Roberto Miranda")
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "meta_periodo", lambda *a, **k: None)
    monkeypatch.setattr(q, "ventas_kg_por_semana", lambda *a, **k: [])
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision_mes", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "por_cobrar",
                        lambda vend: {"saldo": 0, "vencido": 0, "n_clientes": 0})

    html = vendedor_logueado.get("/mi-cartera").data.decode()
    tabbar = html.split('<nav class="tabbar">')[1].split("</nav>")[0]

    # TMT 27/08: el rótulo es "F. técnica" — el largo envolvía a dos
    # renglones con 6 pestañas y agrandaba la barra.
    assert "F. técnica" in tabbar
    tags = [x for x in tabbar.split("<a ") if "fichas-tecnicas" in x]
    assert len(tags) == 1, "la ficha técnica es UN link del tabbar"
    assert 'target="_blank"' in tags[0]
    assert "noopener" in tags[0]


def test_la_ruta_de_salida_no_esta_bloqueada_por_el_allowlist():
    """Un botón que da 404 es peor que no tener botón. `/logout` tiene que
    seguir en PREFIJOS_INFRA — si alguien lo saca, el vendedor queda
    encerrado con el botón puesto."""
    from scope_vendedor import PREFIJOS_INFRA

    assert "/logout" in PREFIJOS_INFRA


# ---------------------------------------------------------------------------
# Posdatados: sólo Accionista y Administrador (dueña 2026-08-05)
# ---------------------------------------------------------------------------
# *"posdatados también sólo andres y accionistas"*. Andrés tiene el rol
# Administrador, que es wildcard, así que son los mismos dos roles que
# Comisiones y Metas.


def test_ningun_rol_conserva_permisos_de_posdat():
    """⚠ No era sólo lectura: INT (Alex, Irene, Maribel) tenía
    crear/editar/anular, o sea que OPERABA posdatados. Se preguntó antes de
    sacarlo y la decisión fue cerrarlo del todo.
    """
    from config.roles import ROLES

    for nombre, permisos in ROLES:
        if "*" in permisos:
            continue
        sobran = [p for p in permisos if p.startswith("posdat.")]
        assert not sobran, f"{nombre} todavía tiene {sobran}"


def test_hay_migracion_0165_para_la_base():
    """Igual que la 0164: `config/roles.py` no manda en runtime. Sin la
    migración el cambio existe sólo en el repo y producción queda idéntica,
    con los tests en verde."""
    from pathlib import Path

    mig = Path("migrations/0165_posdat_solo_duenos.py").read_text()
    assert "DELETE FROM seguridad.permiso" in mig
    for p in ("posdat.ver", "posdat.crear", "posdat.editar", "posdat.anular"):
        assert p in mig


def test_ningun_link_a_posdat_queda_suelto():
    """Un link que aparece y da 404 es peor que no tener el link.

    A /posdat se llega desde SIETE pantallas que otros roles siguen viendo:
    el menú, Balance (el rótulo "Pasivos"), Flujo (dos menciones), Deudas (la
    fila entera es clickeable, más el nombre del proveedor), Provisiones,
    check-totales, la vuelta desde Emitir cheque, y "Recientes" del menú
    —que guarda lo que cada uno abrió ANTES de perder el permiso—.

    Este test recorre las plantillas y exige que toda referencia a una ruta de
    posdat esté dentro de un `tiene_permiso`. Es la parte que no se ve
    cambiando permisos: la ruta queda cerrada y las puertas siguen pintadas.
    """
    import re
    from pathlib import Path

    raiz = Path(".")
    sueltos = []
    for tpl in list(raiz.glob("templates/**/*.html")) + list(raiz.glob("modules/**/*.html")):
        if "modules/posdat/" in str(tpl):
            continue          # adentro del módulo la ruta ya está gateada
        txt = tpl.read_text()
        if "url_for('posdat." not in txt:
            continue
        if "tiene_permiso('posdat." not in txt:
            sueltos.append(str(tpl))
    assert not sueltos, f"linkean a posdat sin gatear: {sueltos}"


# ---------------------------------------------------------------------------
# El mapa de accesos (dueña 2026-08-05)
# ---------------------------------------------------------------------------
# *"¿tenemos una lista de las páginas y accesos?"*. No la había: para saber
# quién ve qué había que leer `config/roles.py` y cruzarlo a mano con los
# decoradores de cada vista.


def test_el_permiso_queda_anotado_en_la_vista():
    """⭐ La pieza que hace que la lista no pueda mentir: `requiere_permiso`
    deja el permiso en la función envuelta.

    Sin esto habría que leer el código con expresiones regulares, y una lista
    escrita a mano se desactualiza el día que alguien cambia un decorador y
    nadie se entera.
    """
    from auth import requiere_permiso

    @requiere_permiso("algo.ver")
    def _vista():  # pragma: no cover — no se ejecuta, se inspecciona
        return "ok"

    assert _vista._permiso == "algo.ver"


def test_el_mapa_sale_de_las_rutas_reales(app):
    """Se arma recorriendo el `url_map`, no una constante. Si mañana se agrega
    una pantalla con `@requiere_permiso`, aparece sola."""
    from modules.usuarios import accesos

    m = accesos.mapa(app)
    porp = {p["permiso"]: p["rutas"] for p in m["permisos"]}

    assert "/mi-cartera" in porp["micartera.ver"]
    assert "/usuarios" in porp["usuarios.admin"]
    # Y las que cerramos hoy siguen apareciendo, con su ruta.
    assert any("/comisiones" in r for r in porp["comisiones.ver"])
    assert any("/posdat" in r for r in porp["posdat.ver"])


def test_el_mapa_denuncia_las_rutas_sin_permiso(app):
    """La auditoría del 2026-08-03 —31 rutas alcanzables por cualquier usuario
    logueado— corriendo sola cada vez que se abre la pantalla, en vez de una
    vez y a mano.
    """
    from modules.usuarios import accesos

    m = accesos.mapa(app)
    # TMT 2026-08-09: `sin_permiso` pasó a significar "sin candado Y SIN
    # REVISAR". Las conocidas se miraron una por una y viven en ACEPTADAS con
    # su motivo, así que hoy esta lista está VACÍA a propósito — es la señal
    # de que no hay nada nuevo. Lo que el detector tiene que seguir viendo es
    # el conjunto entero, y eso es lo que se afirma acá.
    detectadas = set(m["sin_permiso"]) | {a["ruta"] for a in m["aceptadas"]}
    assert detectadas, "o se arreglaron todas, o el detector dejó de mirar"

    # ⭐ Y NO acusa a lo que a propósito no pide permiso. Un aviso rojo con
    # `/login` y `/logout` adentro enseña a ignorar el aviso rojo — mismo
    # criterio que el ⚠ que se sacó del panel de coherencia el 30/07.
    # La lista se REUSA de scope_vendedor: dos listas para la misma idea se
    # despegan a la primera que alguien edite.
    from scope_vendedor import PREFIJOS_INFRA

    for r in detectadas:
        assert not r.startswith(PREFIJOS_INFRA), f"{r} no pide permiso a propósito"
    # Pero las pantallas de verdad sin gate SÍ se denuncian. El ejemplo
    # cambió el 2026-08-09: `/tablero` sólo redirige y `/operaciones` filtra
    # card por card, así que ninguna de las dos era una pantalla "sin gate" —
    # el estado de cuenta sí lo es, y es la que hay que ver.
    assert any(r.startswith("/informes/estado-cuenta") for r in detectadas)


def test_el_wildcard_abre_todo_y_un_rol_pelado_no():
    """`puede()` tiene que respetar el `*`, que es como entran Accionista y
    Administrador — si no, la tabla mostraría en gris justo a los dos que sí
    pueden."""
    from modules.usuarios import accesos

    duena = {"wildcard": True, "permisos": set()}
    operativo = {"wildcard": False, "permisos": {"cheques.ver"}}
    assert accesos.puede(duena, "posdat.ver")
    assert accesos.puede(operativo, "cheques.ver")
    assert not accesos.puede(operativo, "posdat.ver")


def test_desde_usuarios_se_llega_al_mapa():
    """Una ruta sin pantalla es una función que no existe. El link va JUNTO al
    alta, que es donde aparece la pregunta "¿qué le doy con este rol?"."""
    from pathlib import Path

    tpl = Path("modules/usuarios/templates/usuarios/lista.html").read_text()
    assert "usuarios.mapa_accesos" in tpl


def test_los_permisos_van_agrupados_por_sustantivo(app):
    """Dueña 2026-08-05: *"me podés agrupar"*. 58 filas alfabéticas se leen
    como un log: `posdat.anular` y `posdat.ver` quedaban a ocho renglones de
    distancia y había que ir y volver para contestar "¿quién toca
    posdatados?".

    ⭐ El grupo sale del NOMBRE del permiso, no de un mapa de secciones escrito
    a mano. Un mapa habría quedado más lindo —"Cobranza", "Producción"— y se
    habría desactualizado con el primer permiso nuevo, que es justo lo que
    esta pantalla existe para evitar.
    """
    from modules.usuarios import accesos

    m = accesos.mapa(app)
    grupos = {g["nombre"]: g for g in m["grupos"]}

    assert len(m["grupos"]) < len(m["permisos"]), "agrupar tiene que juntar algo"
    # Todos los posdat.* caen en el mismo grupo y ninguno se pierde.
    assert {p["permiso"] for p in grupos["posdat"]["permisos"]} == {
        p["permiso"] for p in m["permisos"] if p["permiso"].startswith("posdat.")
    }
    # Y el total se conserva: agrupar no puede comerse una fila.
    assert sum(len(g["permisos"]) for g in m["grupos"]) == len(m["permisos"])


def test_dentro_del_grupo_las_acciones_van_por_PODER(app):
    """ver → crear → editar → anular, no alfabético.

    Leer no es lo mismo que borrar: alfabéticamente `anular` va primero y
    `ver` último, que es exactamente al revés de como se lee una tabla de
    permisos. La fila que importa mirar es la última.
    """
    from modules.usuarios import accesos

    grupos = {g["nombre"]: g for g in accesos.mapa(app)["grupos"]}
    acciones = [p["permiso"].split(".", 1)[1] for p in grupos["posdat"]["permisos"]]
    esperado = [a for a in ("ver", "crear", "editar", "anular") if a in acciones]
    assert acciones[:len(esperado)] == esperado


def test_no_se_acusa_a_las_rutas_que_chequean_por_dentro(app):
    """⭐ Una ruta sin `requiere_permiso` no es necesariamente una ruta
    abierta: varias chequean adentro. `/impersonate` pide Accionista con un
    `if`; `/dolares`, `/historial`, `/operaciones` y `/informes/deudas` llaman
    a `tiene_permiso` a mano.

    Meterlas en la misma lista roja que las que no chequean NADA es lo que
    convierte un aviso en decoración: 30 rutas acusadas, seis inocentes, y
    nadie la mira dos veces. Van en un bloque aparte.
    """
    from modules.usuarios import accesos

    m = accesos.mapa(app)
    propias = {c["ruta"] for c in m["control_propio"]}

    assert "/impersonate/<int:id_usuario>" in propias
    assert "/dolares" in propias
    # Y no pueden estar en los dos lados a la vez.
    assert not (propias & set(m["sin_permiso"]))


def test_abort_404_no_cuenta_como_control_de_permiso(app):
    """🚨 La trampa del detector, y por qué está calibrado hacia la alarma.

    Casi todas las vistas tienen un `abort(404)` para "ese cheque no existe",
    que no tiene NADA que ver con permisos. Contarlo como control marcaba
    como seguras a `/cheques/<id>/deshacer-deposito`,
    `/cheques/<id>/anular-error-carga` y compañía — que son ESCRITURAS y no
    chequean nada.

    Ante la duda la ruta se queda en rojo: una alarma de más se revisa y se
    descarta en un minuto; una de menos esconde el agujero para siempre, y
    encima con la pantalla diciendo que está todo bien.
    """
    from modules.usuarios import accesos

    assert "abort(404)" not in accesos._SEÑALES_DE_CONTROL
    m = accesos.mapa(app)
    # Siguen detectadas como "sin candado" — lo que cambió el 2026-08-09 es
    # que la dueña las revisó y decidió dejarlas, así que salen del rojo y
    # entran en ACEPTADAS con el motivo escrito. Detectarlas es lo que este
    # test protege; qué se hace con ellas es una decisión, no un invariante.
    detectadas = set(m["sin_permiso"]) | {a["ruta"] for a in m["aceptadas"]}
    assert "/cheques/<int:id_cheque>/deshacer-deposito" in detectadas
    assert "/cheques/<int:id_cheque>/anular-error-carga" in detectadas
    assert not any(
        c["ruta"].endswith("/deshacer-deposito") for c in m["control_propio"]
    ), "un abort(404) de 'no existe' no puede pasar por control de permiso"


def test_el_control_interno_se_lee_del_codigo_de_la_vista():
    """Se inspecciona la FUNCIÓN, no un listado aparte: si alguien le saca el
    `if` a una vista, la pantalla lo nota sola."""
    from modules.usuarios import accesos

    def con_control():  # pragma: no cover — se inspecciona, no se ejecuta
        if not tiene_permiso("x.ver"):  # noqa: F821
            return None
        return "ok"

    def sin_nada():  # pragma: no cover
        return "ok"

    assert accesos._control_interno(con_control) == "tiene_permiso"
    assert accesos._control_interno(sin_nada) is None


def test_int_pierde_activos_compras_gastos_iniciales_provisiones_retenciones():
    """Dueña 2026-08-05: *"quitá activos y quitá compras, quitá gastos, quitá
    iniciales, quitá provisiones, quitá retenciones de INT"*.

    Son 18 permisos de seis módulos, y NO es sólo lectura: se va también la
    operativa (compras.crear/editar/anular, gastos.*, activos.crear, etc.).
    Al rol le quedan 34: cobranza, cheques, facturas, bancos, caja, clientes,
    proveedores y tintorería.

    ⚠ TMT 2026-08-19: la ÚNICA excepción es
    `retenciones.cargar_en_factura` (mig 0205), que a pesar del prefijo NO es
    el módulo Retenciones: es el lapicito de la columna Retención en
    /facturas, o sea la retención de la factura que Alex tiene delante. La
    dueña lo pidió explícito (*"pero siempre tuvieron acceso, ¿por qué no se
    los das?"*). El módulo sigue cerrado para INT — eso es lo que este test
    cuida, y por eso la lista de abajo es nominal en vez de por prefijo.
    """
    from config.roles import ROLES

    d = dict(ROLES)
    fuera = ("activos.", "compras.", "gastos.", "iniciales.",
             "provisiones.", "retenciones.")
    excepciones = {"retenciones.cargar_en_factura"}
    assert not [p for p in d["INT"]
                if p.startswith(fuera) and p not in excepciones]
    # El MÓDULO Retenciones sigue cerrado: ni el listado, ni emitir, ni anular.
    for cerrado in ("retenciones.ver", "retenciones.emitir", "retenciones.anular"):
        assert cerrado not in d["INT"], f"a INT le volvió {cerrado}"
    # Lo que NO se tocó: sigue siendo el rol operativo de cobranza.
    for queda in ("cheques.ver", "facturas.ver", "bancos.ver", "caja.ver",
                  "clientes.ver"):
        assert queda in d["INT"], f"a INT no había que sacarle {queda}"
    # Y los otros roles quedan intactos.
    assert "compras.ver" in d["Compras"] and "activos.ver" in d["Contabilidad"]


def test_hay_migracion_0166():
    from pathlib import Path

    mig = Path("migrations/0166_int_sin_activos_compras_gastos.py").read_text()
    assert "DELETE FROM seguridad.permiso" in mig
    # Acotada al rol INT: si borrara por prefijo sin filtrar el rol, se
    # llevaría puesto a Contabilidad, Compras y Gerente.
    borrado = mig.split("DELETE FROM seguridad.permiso")[1].split('"""')[0]
    assert "id_rol = %s" in borrado


def test_recientes_gatea_cada_tipo_con_su_permiso():
    """"Recientes" guarda lo que CADA UNO abrió, así que sobrevive a que le
    saquen el permiso: el link queda en su historial y da 404. Mordió dos
    veces el mismo día — sacándole posdat y después retenciones al rol INT.
    """
    from pathlib import Path

    menu = Path("templates/base.html").read_text()
    bloque = menu.split("{% set url = None %}")[1].split("{% if url %}")[0]
    assert "tiene_permiso('posdat.editar')" in bloque
    assert "tiene_permiso('retenciones.anular')" in bloque


def test_la_salida_no_depende_de_javascript(vendedor_logueado, monkeypatch):
    """Es un `<details>` nativo, no un dropdown con script.

    Abre y cierra solo, anda con teclado, y no depende de que cargue un JS —
    que en un celular con mala señal es exactamente cuando uno quiere poder
    salir. El portal ya evita JS donde puede.
    """
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Felipe Lopez")
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "meta_periodo", lambda *a, **k: None)
    monkeypatch.setattr(q, "ventas_kg_por_semana", lambda *a, **k: [])
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision_mes", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "por_cobrar",
                        lambda vend: {"saldo": 0, "vencido": 0, "n_clientes": 0})

    html = vendedor_logueado.get("/mi-cartera").data.decode()
    cuenta = html.split('<details class="cuenta">')[1].split("</details>")[0]
    assert "onclick" not in cuenta and "<script" not in cuenta
    assert "<summary" in cuenta
    # El menú dice de quién es la sesión: si se cierra la equivocada, no hay
    # deshacer.
    assert "Felipe Lopez" in cuenta


def test_en_la_vista_previa_no_hay_boton_de_salir(app, monkeypatch):
    """Con `?vend=` la que mira es la dueña: no hay sesión de vendedor que
    cerrar y el botón la sacaría de la suya."""
    from modules.mi_cartera import views

    monkeypatch.setattr(views, "today_ec", lambda: date(2026, 8, 5))
    for f, v in (("ventas_kg", 0.0), ("cobrado", 0.0), ("comision_mes", 0.0)):
        monkeypatch.setattr(q, f, lambda *a, _v=v, **k: _v)
    monkeypatch.setattr(q, "meta_periodo", lambda *a, **k: None)
    monkeypatch.setattr(q, "ventas_kg_por_semana", lambda *a, **k: [])
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Felipe Lopez")
    monkeypatch.setattr(q, "por_cobrar",
                        lambda vend: {"saldo": 0, "vencido": 0, "n_clientes": 0})

    with app.test_request_context("/mi-cartera?vend=FL1"):
        g.user, g.permisos = {"username": "tamara"}, {"*"}
        html = views.inicio()
    assert "/logout" not in html
    assert '<details class="cuenta">' not in html
    assert 'class="avatar"' in html      # el círculo sigue, sin menú


def test_hay_migracion_0167_para_cupos():
    """El cupo lo tocan sólo Andrés y accionistas — decisión del 2026-07-09
    que quedó a medias: la mig 0123 se lo sacó a Cobranzas, Ventas y QC pero
    no a INT, que entonces se llamaba "Alex".

    Lo destapó /usuarios/accesos al cruzar el repo con la base: 35 permisos en
    producción contra 34 en el archivo. Esa diferencia de uno era ésta.
    """
    from pathlib import Path

    mig = Path("migrations/0167_cupos_editar_fuera_de_int.py").read_text()
    assert "DELETE FROM seguridad.permiso" in mig
    assert "cupos.editar" in mig


# ---------------------------------------------------------------------------
# INT ve producción sin ver compras (dueña 2026-08-05)
# ---------------------------------------------------------------------------
# *"INT sí tiene que poder ver todo lo de stock, tejeduría, Asinfo también"* —
# el mismo día en que le sacamos `compras.*`.


def test_tejeduria_tiene_permiso_propio_y_no_cuelga_de_compras():
    """⭐ La pantalla pedía `compras.ver` porque cada OF de tejeduría CREA una
    compra. Pero eso es de dónde salen los datos, no quién debería mirarlos:
    ver la producción por tejedor no es ver el libro de compras de la empresa.

    Atadas al mismo permiso había que elegir entre darle las dos a INT o
    ninguna — que es exactamente el problema que aparece cuando un permiso
    nombra el ORIGEN del dato en vez de la pantalla.
    """
    from pathlib import Path

    src = Path("modules/tejeduria_asinfo/views.py").read_text()
    tab = src.split("def tab(")[0]
    assert 'requiere_permiso("tejeduria.ver")' in tab
    assert 'requiere_permiso("compras.ver")' not in tab
    # Lo que ESCRIBE no se aflojó: INT mira, no carga.
    assert 'requiere_permiso("compras.crear")' in src
    assert 'requiere_permiso("tarifas.editar")' in src


def test_int_ve_stock_tejeduria_y_asinfo():
    """Las seis pantallas de "Producción y stocks" que sí le tocan."""
    from config.roles import ROLES

    d = dict(ROLES)
    for p in ("stock.ver",        # Terminado Asinfo, Inventario, Ingreso de hilado
              "tintura.ver",      # Stock químicos, Producción Tintorería
              "tejeduria.ver"):   # Tejeduría Asinfo
        assert p in d["INT"], f"INT necesita {p}"
    # Y sigue SIN compras: ese era el punto.
    assert not [x for x in d["INT"] if x.startswith("compras.")]


def test_los_que_ya_veian_tejeduria_no_la_pierden():
    """Cambiar el decorador sin darles el permiso nuevo les corta el acceso —
    el mismo error que la 0044 evitó cuando `comisiones.ver` se separó de
    `informes.ver`."""
    from config.roles import ROLES

    d = dict(ROLES)
    for rol in ("Gerente", "Contabilidad", "Compras", "Bodega", "Lectura"):
        assert "compras.ver" in d[rol]
        assert "tejeduria.ver" in d[rol], f"{rol} perdería Tejeduría"


def test_hay_migracion_0168_que_no_deja_a_nadie_afuera():
    from pathlib import Path

    mig = Path("migrations/0168_tejeduria_ver.py").read_text()
    # Hereda de compras.ver Y suma INT aparte.
    assert "WHERE nombre_opcion = 'compras.ver'" in mig
    assert "nombre_rol = 'INT'" in mig
    assert "ON CONFLICT" in mig


def test_la_pantalla_de_prueba_prueba_los_DOS_formatos(app, vendedor_logueado, monkeypatch):
    """🚨 TMT 2026-08-25: el teléfono de Patricio contestó *"Puede compartir
    archivos: sí"* y el compartir falló igual.

    Probar sólo con el PDF no distingue "este aparato no comparte" de "este
    aparato no comparte PDFs" — dos problemas con dos arreglos distintos. Con
    los dos formatos, una sola foto de la pantalla contesta cuál es.
    """
    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(q, "mis_clientes",
                        lambda vend: [{"codigo_cli": "TDV", "nombre": "Textiles"}])
    r = vendedor_logueado.get("/mi-cartera/prueba-envio")
    assert r.status_code == 200
    p = r.data.decode()

    assert "Probar PDF" in p and "Probar foto" in p
    assert "/pdf" in p and "/imagen" in p, "no le pasa las dos URLs"
    # Lo que hace falta de esa pantalla es el ERROR EXACTO, no un sí/no.
    assert "comoFalla" in p
    assert "NO se abrió" in p


def test_la_ficha_prepara_la_hoja_mientras_el_vendedor_la_lee(
        app, vendedor_logueado, monkeypatch):
    """⭐ TMT 2026-08-26 (dueña): *"podemos hacer más rápido lo de mandar
    imagen, pdf y whatsapp desde vendedor. tarda mucho tiempo"*.

    Ésta es la pantalla desde la que el vendedor manda, así que el archivo se
    va a pedir casi seguro: se empieza a preparar al ABRIR la ficha y no cuando
    el dedo toca el botón. Cuando toca —tres, cinco segundos después— la hoja
    ya está, y en el teléfono el PRIMER toque abre WhatsApp.
    """
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente",
                        _ec_con_facturas)
    monkeypatch.setitem(app.jinja_env.globals, "pdf_disponible", lambda: True)
    ficha = vendedor_logueado.get("/mi-cartera/cliente/TDV").data.decode()

    boton = ficha.split("<button", 1)[1].split(">", 1)[0]
    assert "data-wa-pdf" in boton and "data-precargar" in boton
