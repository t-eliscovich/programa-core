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
    assert ">Rollos<" in hoja and ">Rib kg<" in hoja


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


# ── 16/10/2026: el sugerido gris de Producir ───────────────────────────────

def _fs(aca, sin, inv=0, prod=0, color="GRI", codigo="FL102GRI"):
    return {"tela": "Fleece 102", "color": color, "codigo": codigo, "acabado": aca,
            "en_unidades": False, "un_por_kg": None, "sinmemo_d": sin,
            "inventario_d": inv, "produccion_d": prod,
            "hacer_d": max(0, sin - inv - prod)}


def test_lo_que_rota_suma_dos_semanas_de_venta():
    f = _fs("ABI", 14, prod=3)
    service.sugerido_producir([f], {("FL102GRI", "ABI"): 6 * service.KG_POR_ROLLO})
    assert (f["sug_base_d"], f["reponer_d"], f["sugerido_d"]) == (11, 6, 17)


def test_lo_que_no_rota_sugiere_solo_lo_pedido_y_nunca_negativo():
    a, b = _fs("ABI", 9, inv=2), _fs("TUB", 2, inv=9)
    service.sugerido_producir([a, b], None)
    assert (a["sugerido_d"], b["sugerido_d"]) == (7, 0)


def test_menos_de_seis_por_color_sube_a_seis_sumando_tub_y_abi():
    tub, abi = _fs("TUB", 3), _fs("ABI", 1)
    otro = _fs("ABI", 2, color="ROJ", codigo="FL102ROJ")
    service.sugerido_producir([tub, abi, otro], {})
    assert (tub["sugerido_d"], abi["sugerido_d"]) == (5, 1)   # 3+1 → 6, al que más pide
    assert otro["sugerido_d"] == 6


def test_un_pedido_sin_acabado_usa_la_venta_de_los_dos():
    f = _fs("", 0)
    service.sugerido_producir([f], {("FL102GRI", "ABI"): 2 * service.KG_POR_ROLLO,
                                    ("FL102GRI", "TUB"): 5 * service.KG_POR_ROLLO,
                                    ("OTRO", "ABI"): 999.0})
    assert f["reponer_d"] == 7 and f["sugerido_d"] == 7


def test_la_reposicion_sale_de_inventario_rotativo():
    from modules.inventario_rotativo import service as rot
    with patch.object(rot, "rotativo", return_value=(
            [{"codigo": "A", "acabado": "TUB", "punto_kg": 10.0},
             {"codigo": "A", "acabado": "TUB", "punto_kg": 5.0}], True)):
        assert views._punto_reposicion() == {("A", "TUB"): 15.0}
    with patch.object(rot, "rotativo", return_value=([], False)):
        assert views._punto_reposicion() == {}


# ── 16/10/2026: se guarda lo sugerido junto con lo mandado ─────────────────

class _CurIns(_Cur):
    def __init__(self, faltan=()):
        super().__init__()
        self.faltan, self.hechos = faltan, []

    def execute(self, sql, params=None):
        for c in self.faltan:
            if c in sql:
                raise RuntimeError(f'column "{c}" of relation "a_fabricar" does not exist')
        self.hechos.append((sql, params))


class _ConnIns(_Conn):
    def commit(self):
        pass


def _ln():
    return {"categoria": "Fleece", "tela": "Fleece 102", "acabado": "ABI", "color": "GRI",
            "color_nombre": "GRIS", "codigo": "FL102GRI", "cantidad": 17, "unidad": "roll",
            "rib_kg": 0, "pedidos": [], "sugerido": 17, "sugerido_venta": 6}


def test_al_mandar_se_guarda_lo_que_sugeria():
    cur = _CurIns()
    with patch.object(formulas_fabricar.formulas_memos, "disponible", return_value=True), \
         patch.object(formulas_fabricar.formulas_memos, "_conn", return_value=_ConnIns(cur)):
        ok, motivo, _ = formulas_fabricar.mandar([_ln()], "tamara")
    assert ok and motivo == "enviado"
    sql, vals = cur.hechos[0]
    assert "sugerido, sugerido_venta, cuellos, punos" in sql
    assert vals[-5:] == ("GRIS", 17, 6, 0, 0)


def test_si_formulas_no_tiene_las_columnas_nuevas_manda_igual():
    cur = _CurIns(faltan=("sugerido",))
    with patch.object(formulas_fabricar.formulas_memos, "disponible", return_value=True), \
         patch.object(formulas_fabricar.formulas_memos, "_conn", return_value=_ConnIns(cur)):
        ok, _, _ = formulas_fabricar.mandar([_ln()], "tamara")
    assert ok and "sugerido" not in cur.hechos[0][0] and len(cur.hechos[0][1]) == 11


def test_el_servidor_calcula_el_sugerido_que_se_guarda(app, fake_db):
    c = _login(app, fake_db)
    mandado = {}

    def fake_mandar(lineas, usuario):
        mandado["lineas"] = lineas
        return True, "enviado", "AF-x"

    clave = service.clave_renglon("FE96CAF", "")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo([_ped("PDCL-1", cantidad=2)])), \
         patch.object(views.formulas_memos, "estados", return_value={}), \
         patch.object(views.service, "por_pedido", return_value=([], False)), \
         patch.object(views.formulas_fabricar, "envios", return_value=([], True)), \
         patch.object(views, "_punto_reposicion", return_value={}), \
         patch.object(views.formulas_fabricar, "mandar", side_effect=fake_mandar):
        c.post("/pedidos/mandar-a-hacer", data={"cat": "Fleece", f"c_{clave}": "9"})
    (ln,) = mandado["lineas"]
    assert ln["cantidad"] == 9.0 and ln["sugerido"] == 6 and ln["sugerido_venta"] == 0
