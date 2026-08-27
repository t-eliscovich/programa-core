"""Corte por PEDIDO, dueño (agente comercial) y envío de MEMOS a la fábrica.

Los fakes de Asinfo devuelven filas con la forma de la FUENTE (las columnas
que escribe `_SQL_POR_PEDIDO`), no la del helper. El dueño se matchea por
NOMBRE contra scintela.vendedor — acá `mapa_vendedores` se pisa directo
porque el fake de db no conoce esa consulta.
"""
from unittest.mock import patch

import pytest

from modules._lib import formulas_memos
from modules.pedidos import service


@pytest.fixture(autouse=True)
def _sin_cache():
    service.reset_cache()
    yield
    service.reset_cache()


_VENDEDORES = {
    frozenset({"PATRICIO", "PROANO"}): {"codigo": "PPR", "nombre": "Patricio Proano"},
    frozenset({"ROBERTO", "MIRANDA"}): {"codigo": "RMY", "nombre": "Roberto Miranda"},
}


def _fila(**kw):
    """Fila cruda de `_SQL_POR_PEDIDO`."""
    base = {
        "numero": "PDCL-26438", "fecha": "2026-08-25",
        "cliente": "CONFECCIONES NOVA", "codigo_cliente": "CNV",
        "agente_id": 751, "agente_nombre": "Proaño Patricio",
        "descripcion": "", "codigo": "FE96HAB", "color": "HAB",
        "tela": "Fleece 96 Perchado", "cantidad": 18.0, "unidad": 51,
    }
    base.update(kw)
    return base


# ── el dueño ────────────────────────────────────────────────────────────────

def test_el_dueno_se_matchea_por_nombre_aunque_asinfo_lo_diga_al_reves():
    """Asinfo: "Proaño Patricio" (con ñ). Local: "Patricio Proano" (sin)."""
    d = service._dueno("Proaño Patricio", _VENDEDORES)
    assert d["codigo"] == "PPR"


def test_la_casa_no_es_un_vendedor():
    d = service._dueno("Cía. Ltda. Intela", _VENDEDORES)
    assert d["codigo"] == "" and d["es_casa"] is True
    assert d["nombre"] == "Intela"


def test_un_agente_historico_muestra_su_nombre_sin_inventar_codigo():
    d = service._dueno("Bedon Hector", _VENDEDORES)
    assert d["codigo"] == "" and d["nombre"] == "Bedon Hector"


def test_sin_agente_no_hay_dueno():
    assert service._dueno("", _VENDEDORES)["nombre"] == ""


# ── por_pedido ──────────────────────────────────────────────────────────────

_FILAS = [
    _fila(),
    _fila(codigo="C34BLA", color="BLA", tela="Cuellos T34",
          cantidad=212.0, unidad=1),
    _fila(numero="PDCL-26401", fecha="2026-08-20", cliente="TEXTILES DEL VALLE",
          codigo_cliente="TDV", agente_nombre="Miranda Roberto",
          codigo="PI28NEG", color="NEG", tela="Pique 2.8",
          cantidad=470.0, unidad=2, descripcion="urgente"),
]


def _por_pedido():
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES):
        return service.por_pedido()


def test_agrupa_las_lineas_por_pedido_y_los_nuevos_van_primero():
    pedidos, ok = _por_pedido()
    assert ok
    assert [p["numero"] for p in pedidos] == ["PDCL-26438", "PDCL-26401"]
    assert pedidos[0]["n_lineas"] == 2


def test_los_rollos_se_estiman_en_kilos_y_las_unidades_no_inventan_kilos():
    pedidos, _ = _por_pedido()
    p = pedidos[0]
    lineas = {ln["producto"]: ln for ln in p["lineas"]}
    assert lineas["FE96HAB"]["kg"] == round(18 * service.KG_POR_ROLLO)
    assert lineas["C34BLA"]["kg"] is None
    # Con una línea en unidades el total queda marcado parcial, no un cero
    # sumado en silencio.
    assert p["kg_completo"] is False
    assert p["total_kg"] == round(18 * service.KG_POR_ROLLO)


def test_el_dueno_del_pedido_es_el_agente_comercial():
    pedidos, _ = _por_pedido()
    assert pedidos[0]["dueno"]["codigo"] == "PPR"
    assert pedidos[1]["dueno"]["codigo"] == "RMY"


def test_si_asinfo_no_contesta_no_hay_pedidos_pero_se_sabe():
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        pedidos, ok = service.por_pedido()
    assert pedidos == [] and ok is False


# ── armar_memo ──────────────────────────────────────────────────────────────

def test_el_memo_es_la_foto_completa_del_pedido():
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES):
        m = service.armar_memo("PDCL-26401")
    assert m["cliente"] == {"codigo": "TDV", "nombre": "TEXTILES DEL VALLE"}
    assert m["vendedor"]["codigo"] == "RMY"
    assert m["descripcion"] == "urgente"
    assert m["lineas"][0]["color"] == "NEG"
    assert m["total_kg"] == 470


def test_un_pedido_que_no_esta_pendiente_no_arma_memo():
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES):
        assert service.armar_memo("PDCL-99999") is None


def test_la_etiqueta_del_dueno_lleva_el_codigo_cuando_lo_hay():
    assert service.etiqueta_dueno({"codigo": "PPR", "nombre": "Patricio Proano"}) \
        == "PPR · Patricio Proano"
    assert service.etiqueta_dueno({"codigo": "", "nombre": "Intela"}) == "Intela"


# ── producción por pedido ───────────────────────────────────────────────────

def test_la_produccion_junta_la_orden_de_formulas_con_su_oft_de_asinfo():
    ordenes = [{"pedido_numero": "PDCL-26401", "oft_numero": "OFT-000038020",
                "numero": "26-08-04", "kil": 280.0}]
    ofts = [{"numero": "OFT-000038020", "cantidad": 294.0, "fabricada": 120.0}]
    from modules._lib import formulas_db
    with patch.object(formulas_db, "fetch_all", return_value=ordenes), \
         patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(ofts, True)):
        prod = service.produccion_por_pedido(["PDCL-26401"])
    o = prod["PDCL-26401"][0]
    assert o["oft"] == "OFT-000038020"
    assert o["a_producir"] == 294 and o["producido"] == 120
    assert o["pct"] == 41


def test_una_orden_sin_oft_muestra_sus_kilos_sin_porcentaje():
    ordenes = [{"pedido_numero": "PDCL-26401", "oft_numero": None,
                "numero": "26-08-05", "kil": 150.0}]
    from modules._lib import formulas_db
    with patch.object(formulas_db, "fetch_all", return_value=ordenes), \
         patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], True)):
        prod = service.produccion_por_pedido(["PDCL-26401"])
    o = prod["PDCL-26401"][0]
    assert o["kil"] == 150 and o["pct"] is None


def test_sin_bridge_a_formulas_no_hay_produccion_y_no_rompe():
    from modules._lib import formulas_db
    with patch.object(formulas_db, "fetch_all", return_value=[]):
        assert service.produccion_por_pedido(["PDCL-1"]) == {}


def test_el_numero_de_oft_se_sanitiza_antes_de_ir_a_la_sql():
    assert service._oft_segura("oft-000038020") == "OFT-000038020"
    assert service._oft_segura("X'; DROP TABLE --") == "XDROPTABLE--"


def test_una_oft_sobreproducida_no_pasa_del_100_por_ciento():
    ordenes = [{"pedido_numero": "P", "oft_numero": "OFT-1", "numero": "N",
                "kil": 1.0}]
    ofts = [{"numero": "OFT-1", "cantidad": 100.0, "fabricada": 130.0}]
    from modules._lib import formulas_db
    with patch.object(formulas_db, "fetch_all", return_value=ordenes), \
         patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(ofts, True)):
        prod = service.produccion_por_pedido(["P"])
    assert prod["P"][0]["pct"] == 100


# ── formulas_memos (el bridge de escritura) ─────────────────────────────────

def test_sin_env_var_el_envio_degrada_sin_romper():
    assert formulas_memos.disponible() is False
    ok, motivo = formulas_memos.enviar("P", "C", "V", "yo", {})
    assert (ok, motivo) == (False, "sin_bridge")
    assert formulas_memos.estados(["P"]) == {}


# ── la pantalla de la oficina ───────────────────────────────────────────────

def _login(app, fake_db, permisos=("facturas.ver", "stock.ver",
                                   "pedidos.enviar_memo")):
    rid = fake_db.add_role("Tester", list(permisos))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


_FILA_PENDIENTES = {
    "categoria": "Fleece", "tela": "Fleece 96 Perchado", "codigo": "FE96HAB",
    "color": "HAB", "ped_kg": 423.0, "ped_rollos": 18.0, "ped_un": 0.0,
    "un_por_kg": None, "n_pedidos": 1, "n_clientes": 1,
    "mas_viejo": "2026-08-25", "inv_kg": 0.0, "prod_kg": 0.0, "n_ordenes": 0,
}


def _fake_asinfo(_db, sql):
    """Cada consulta con su forma: la del corte por pedido trae el agente,
    la de pendientes agrupa por producto. Un fake con una sola forma pasa en
    verde mientras la pantalla no resuelve nada."""
    if "id_agente_comercial" in sql:
        return ([dict(f) for f in _FILAS], True)
    if "saldo_producto" in sql:
        return ([dict(_FILA_PENDIENTES)], True)
    return ([], True)


def _get_corte_pedido(c, estados=None):
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "estados", return_value=estados or {}), \
         patch.object(service, "produccion_por_pedido", return_value={}):
        return c.get("/pedidos?corte=pedido")


def test_el_corte_pedido_muestra_el_dueno_y_el_boton(app, fake_db):
    c = _login(app, fake_db)
    r = _get_corte_pedido(c)
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "PDCL-26438" in body
    assert "PPR" in body and "RMY" in body
    # El botón, no la nota de arriba (que también dice "Enviar memo").
    assert 'class="btnmemo"' in body


def test_un_pedido_ya_enviado_muestra_su_estado_y_no_el_boton(app, fake_db):
    c = _login(app, fake_db)
    r = _get_corte_pedido(c, estados={
        "PDCL-26438": {"estado": "pendiente", "en_proceso_por": None},
        "PDCL-26401": {"estado": "en_proceso", "en_proceso_por": "jonathan"},
    })
    body = r.get_data(as_text=True)
    assert "ENVIADO" in body and "EN PROCESO" in body
    assert 'class="btnmemo"' not in body


def test_sin_el_permiso_el_boton_no_aparece(app, fake_db):
    c = _login(app, fake_db, permisos=("facturas.ver", "stock.ver"))
    body = _get_corte_pedido(c).get_data(as_text=True)
    assert 'class="btnmemo"' not in body


def test_enviar_memo_manda_la_foto_y_confirma(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "enviar",
                      return_value=(True, "enviado")) as env:
        r = c.post("/pedidos/enviar-memo", data={"numero": "PDCL-26401"},
                   follow_redirects=False)
    assert r.status_code == 302
    kw = env.call_args.kwargs
    assert kw["numero"] == "PDCL-26401"
    assert kw["vendedor"] == "RMY · Roberto Miranda"
    assert kw["enviado_por"] == "test"
    assert kw["detalle"]["cliente"]["nombre"] == "TEXTILES DEL VALLE"


def test_enviar_dos_veces_avisa_y_no_duplica(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "enviar",
                      return_value=(False, "ya_enviado")):
        r = c.post("/pedidos/enviar-memo", data={"numero": "PDCL-26401"},
                   follow_redirects=True)
    assert "ya tenía memo enviado" in r.get_data(as_text=True)


def test_enviar_sin_permiso_es_404(app, fake_db):
    c = _login(app, fake_db, permisos=("facturas.ver",))
    with patch.object(formulas_memos, "enviar") as env:
        r = c.post("/pedidos/enviar-memo", data={"numero": "PDCL-26401"})
    assert r.status_code == 404
    env.assert_not_called()


def test_enviar_un_pedido_que_no_existe_no_manda_nada(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "enviar") as env:
        r = c.post("/pedidos/enviar-memo", data={"numero": "PDCL-00000"},
                   follow_redirects=True)
    assert "No encontré ese pedido" in r.get_data(as_text=True)
    env.assert_not_called()


# ── el portal del vendedor ──────────────────────────────────────────────────

def _login_vendedor(app, fake_db, vend="PPR"):
    rid = fake_db.add_role("Vendedor", ["micartera.ver"])
    uid = fake_db.add_user("patricio", b"$2b$12$fakehash", rid, vend=vend)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_el_vendedor_ve_solo_sus_pedidos(app, fake_db):
    c = _login_vendedor(app, fake_db, vend="PPR")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "estados", return_value={}), \
         patch.object(service, "produccion_por_pedido", return_value={}):
        r = c.get("/mi-cartera/pedidos")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "PDCL-26438" in body          # el de PPR
    assert "PDCL-26401" not in body      # el de RMY no aparece


def test_el_vendedor_no_puede_mandar_un_pedido_ajeno(app, fake_db):
    """PPR postea el numero del pedido de RMY: 404, como si no existiera."""
    c = _login_vendedor(app, fake_db, vend="PPR")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "enviar") as env:
        r = c.post("/mi-cartera/pedidos/enviar-memo",
                   data={"numero": "PDCL-26401"})
    assert r.status_code == 404
    env.assert_not_called()


def test_el_vendedor_manda_lo_suyo_y_queda_registrado_su_usuario(app, fake_db):
    c = _login_vendedor(app, fake_db, vend="PPR")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "enviar",
                      return_value=(True, "enviado")) as env:
        r = c.post("/mi-cartera/pedidos/enviar-memo",
                   data={"numero": "PDCL-26438"})
    assert r.status_code == 302
    assert env.call_args.kwargs["enviado_por"] == "patricio"
