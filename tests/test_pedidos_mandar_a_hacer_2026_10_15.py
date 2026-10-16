"""«Mandar a hacer» desde /pedidos (Tipo de tela) → formulas_app «A fabricar».

Tamara 15/10/2026: la columna Mandar a hacer la completa la oficina (sin
sugerido), se revisa y se manda; le llega a Jonathan en formulas («A
fabricar»), que lo marca en proceso / hecho. Los pedidos sin memo de ese
color quedan "Enviados a producción" — no son memo.
"""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from modules._lib import formulas_fabricar
from modules.pedidos import service, views
from tests.test_pedidos import _fila


@pytest.fixture(autouse=True)
def _sin_cache():
    service.reset_cache()
    yield
    service.reset_cache()


def _ped(numero, cliente="GIC", cantidad=4):
    """Fila cruda de _SQL_PEDIDOS_TODOS (forma de la FUENTE)."""
    return {"codigo": "FE96CAF", "numero": numero, "cliente": cliente + " S.A.",
            "codigo_cliente": cliente, "fecha": "2026-10-02", "cantidad": cantidad,
            "unidad": 51, "aca_min": None, "aca_max": None}


def _af(numero, estado="pendiente", codigo="FE96CAF", acabado="", cantidad=7):
    return {"id": 1, "envio": "AF-1", "enviado_por": "tamara",
            "enviado_en": datetime(2026, 10, 15, 16, 0, tzinfo=UTC),
            "categoria": "Fleece", "tela": "Fleece 96 Perchado", "acabado": acabado,
            "color": "CAF", "codigo": codigo, "cantidad": cantidad, "unidad": "roll",
            "rib_kg": 0.0,
            "pedidos": [{"numero": numero, "cliente": "GIC", "cantidad": 4}],
            "estado": estado, "fecha_fmt": "15/10", "hora_fmt": "15/10 11:00",
            "en_proceso_fmt": "", "terminado_fmt": ""}


def _login(app, fake_db, permisos=("facturas.ver", "stock.ver", "pedidos.enviar_memo")):
    rid = fake_db.add_role("Tester", list(permisos))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _fake_asinfo(peds):
    def fake(_db, sql, **_kw):
        if "ORDER BY pr.codigo, v.fecha" in sql:
            return (peds, True)
        return ([_fila()], True)
    return fake


# ── funciones puras ─────────────────────────────────────────────────────────

def test_solo_los_renglones_vivos_cubren_pedidos():
    filas = [_af("P1", "pendiente"), _af("P2", "en_proceso"), _af("P3", "terminado")]
    assert set(formulas_fabricar.pedidos_enviados(filas)) == {"P1", "P2"}


def test_la_marca_del_color_es_el_envio_vivo_mas_nuevo():
    nuevo, viejo = _af("P1", cantidad=9), _af("P2", cantidad=3)
    out = formulas_fabricar.vivos_por_clave([nuevo, viejo], service.clave_renglon)
    assert out[service.clave_renglon("FE96CAF", "")]["cantidad"] == 9


def test_sin_bridge_mandar_no_rompe():
    with patch.object(formulas_fabricar.formulas_memos, "disponible", return_value=False):
        assert formulas_fabricar.mandar([{"cantidad": 1}], "x") == (False, "sin_bridge", "")
        assert formulas_fabricar.envios() == ([], False)


# ── la pantalla ─────────────────────────────────────────────────────────────

def test_un_pedido_mandado_a_produccion_no_cuenta_como_sin_memo(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1"), _ped("PDCL-2", "BEB")])), \
         patch.object(views.formulas_fabricar, "envios", return_value=([_af("PDCL-1")], True)):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    assert "Enviado a producción" in body
    assert "Mandado 15/10 · 7" in body


def test_el_boton_aparece_solo_con_bridge_y_permiso(app, fake_db):
    c = _login(app, fake_db, permisos=("facturas.ver", "stock.ver"))
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.formulas_fabricar, "disponible", return_value=True):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    assert 'id="editar-hacer"' not in body
    c2 = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.formulas_fabricar, "disponible", return_value=True):
        body = c2.get("/pedidos?corte=tela").get_data(as_text=True)
    assert 'id="editar-hacer"' in body


# ── el envío ────────────────────────────────────────────────────────────────

def test_el_servidor_arma_los_renglones_y_saca_los_pedidos_con_memo(app, fake_db):
    c = _login(app, fake_db)
    mandado = {}

    def fake_mandar(lineas, usuario):
        mandado["lineas"], mandado["usuario"] = lineas, usuario
        return True, "enviado", "AF-x"

    clave = service.clave_renglon("FE96CAF", "")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1"), _ped("PDCL-2", "BEB")])), \
         patch.object(views.formulas_memos, "estados",
                      return_value={"PDCL-2": {"estado": "pendiente"}}), \
         patch.object(views.service, "por_pedido", return_value=([], False)), \
         patch.object(views.formulas_fabricar, "envios", return_value=([], True)), \
         patch.object(views.formulas_fabricar, "mandar", side_effect=fake_mandar):
        r = c.post("/pedidos/mandar-a-hacer",
                   data={"cat": "Fleece", f"c_{clave}": "7", f"r_{clave}": "30",
                         "c_NOEXISTE_X": "3"})
    assert r.status_code == 302
    (ln,) = mandado["lineas"]
    assert (ln["codigo"], ln["cantidad"], ln["tela"]) == ("FE96CAF", 7.0, "Fleece 96 Perchado")
    assert ln["rib_kg"] == 30.0
    assert [p["numero"] for p in ln["pedidos"]] == ["PDCL-1"]   # el del memo, afuera
    assert mandado["usuario"] == "test"


def test_sin_cantidades_no_manda_nada(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(views.formulas_fabricar, "mandar") as m:
        r = c.post("/pedidos/mandar-a-hacer", data={"cat": "Fleece", "c_X_X": "0"})
    assert r.status_code == 302 and not m.called


def test_mandar_pide_permiso(app, fake_db):
    c = _login(app, fake_db, permisos=("facturas.ver",))
    with patch.object(views.formulas_fabricar, "mandar") as m:
        r = c.post("/pedidos/mandar-a-hacer", data={"c_X_X": "3"})
    assert r.status_code in (403, 404) and not m.called


def test_el_historial_se_dibuja_y_sin_bridge_avisa(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(views.formulas_fabricar, "envios",
                      return_value=([_af("PDCL-1", "en_proceso")], True)):
        body = c.get("/pedidos/mandados").get_data(as_text=True)
    assert "EN PROCESO" in body and "PDCL-1" in body
    with patch.object(views.formulas_fabricar, "envios", return_value=([], False)):
        body = c.get("/pedidos/mandados").get_data(as_text=True)
    assert "No pude hablar con formulas_app" in body


def test_se_puede_mandar_solo_rib(app, fake_db):
    """Producir: Rollos | Rib (kg) — un color puede llevar sólo rib."""
    c = _login(app, fake_db)
    mandado = {}
    clave = service.clave_renglon("FE96CAF", "")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.formulas_memos, "estados", return_value={}), \
         patch.object(views.formulas_fabricar, "envios", return_value=([], True)), \
         patch.object(views.formulas_fabricar, "mandar",
                      side_effect=lambda lineas, u: (mandado.setdefault("l", lineas), (True, "enviado", "x"))[1]):
        c.post("/pedidos/mandar-a-hacer", data={"cat": "Fleece", f"r_{clave}": "25"})
    (ln,) = mandado["l"]
    assert (ln["cantidad"], ln["rib_kg"]) == (0.0, 25.0)


def test_la_hoja_impresa_va_una_tela_por_pagina_sin_pedidos(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    hoja = body[body.index('<div class="imp">'):]
    hoja = hoja[:hoja.index('<div class="hbar')]
    assert 'class="imp-tela chica"' in hoja    # 1 color: va junto con otras chicas
    assert "PDCL-1" not in hoja                     # sin la columna de pedidos
    assert "Producir rollos" in hoja and "Producir rib (kg)" in hoja


def test_se_cancela_lo_pendiente_desde_el_historial(app, fake_db):
    """Tamara 15/10/2026: "que se pueda cancelar un pedido enviado"."""
    c = _login(app, fake_db)
    with patch.object(views.formulas_fabricar, "envios",
                      return_value=([_af("PDCL-1", "pendiente")], True)):
        body = c.get("/pedidos/mandados").get_data(as_text=True)
    assert "Cancelar envío" in body
    with patch.object(views.formulas_fabricar, "cancelar", return_value=(1, "cancelado")) as m:
        r = c.post("/pedidos/mandados/cancelar", data={"envio": "AF-1"})
    assert r.status_code == 302
    m.assert_called_once_with("test", envio="AF-1", id_=None)


def test_lo_que_la_fabrica_ya_tomo_no_ofrece_cancelar(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(views.formulas_fabricar, "envios",
                      return_value=([_af("PDCL-1", "en_proceso")], True)):
        body = c.get("/pedidos/mandados").get_data(as_text=True)
    assert 'class="btn-canc"' not in body


def test_un_renglon_cancelado_ya_no_cubre_sus_pedidos():
    filas = [_af("P1", "cancelado")]
    assert formulas_fabricar.pedidos_enviados(filas) == {}


def test_el_nombre_del_color_va_al_lado_del_codigo(app, fake_db):
    """Tamara 15/10/2026: "poné también el nombre del color" — en pantalla
    y en la hoja impresa."""
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1")])), \
         patch.object(views.service, "nombres_color", return_value={"CAF": "CAFE OSCURO"}):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    assert body.count("CAFE OSCURO") >= 2          # tabla de pantalla + hoja impresa


# ── 16/10/2026: orden de tintura en el historial, pedidos en cero afuera ───

class _Cur:
    def __init__(self, filas=None, falla=None):
        self.filas, self.falla, self.sql = filas or [], falla, ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.falla:
            raise self.falla
        self.sql = sql

    def fetchall(self):
        return self.filas


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur


def test_cada_renglon_trae_la_orden_de_tintura_que_salio_de_el():
    filas = [{"id": 1}, {"id": 2}]
    cur = _Cur([(1, "T-4381"), (1, "T-4390")])
    with patch.object(formulas_fabricar.formulas_memos, "_conn", return_value=_Conn(cur)):
        formulas_fabricar._con_ordenes(filas)
    assert filas[0]["ordenes"] == ["T-4381", "T-4390"]
    assert filas[1]["ordenes"] == []
    assert "orden_fabricar" in cur.sql


def test_sin_permiso_para_las_ordenes_el_historial_sigue():
    filas = [{"id": 1}]
    cur = _Cur(falla=RuntimeError("permission denied for table orden_fabricar"))
    with patch.object(formulas_fabricar.formulas_memos, "_conn", return_value=_Conn(cur)):
        formulas_fabricar._con_ordenes(filas)
    assert filas[0]["ordenes"] == []
    vacias = []
    formulas_fabricar._con_ordenes(vacias)
    assert vacias == []


def test_la_orden_se_ve_en_el_historial(app, fake_db):
    c = _login(app, fake_db)
    f = _af("PDCL-1", estado="en_proceso")
    f["ordenes"] = ["T-4381"]
    with patch.object(views.formulas_fabricar, "envios", return_value=([f], True)):
        r = c.get("/pedidos/mandados")
    assert "orden T-4381" in r.get_data(as_text=True)


def test_un_pedido_sin_nada_pendiente_no_se_manda(app, fake_db):
    c = _login(app, fake_db)
    mandado = {}

    def fake_mandar(lineas, usuario):
        mandado["lineas"] = lineas
        return True, "enviado", "AF-x"

    clave = service.clave_renglon("FE96CAF", "")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1"), _ped("PDCL-0", cantidad=0)])), \
         patch.object(views.formulas_memos, "estados", return_value={}), \
         patch.object(views.service, "por_pedido", return_value=([], False)), \
         patch.object(views.formulas_fabricar, "envios", return_value=([], True)), \
         patch.object(views.formulas_fabricar, "mandar", side_effect=fake_mandar):
        c.post("/pedidos/mandar-a-hacer", data={"cat": "Fleece", f"c_{clave}": "2"})
    (ln,) = mandado["lineas"]
    assert [p["numero"] for p in ln["pedidos"]] == ["PDCL-1"]
