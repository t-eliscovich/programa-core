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


# ── etapas del pedido (dueña 27/08: sin porcentajes) ───────────────────────
# enviado → en_tintura (la OFT tiene orden de salida de material) →
# terminado (todas las OFT Finalizadas, o la X del memo).

_MEMO = {"PDCL-1": {"estado": "pendiente", "en_proceso_por": None}}


def _fila_oft(**kw):
    """Fila cruda de `_SQL_ETAPA_OFTS` (la forma de la fuente)."""
    base = {"parent_numero": "OFT-000038020", "estado_produccion": 2,
            "cantidad": 294.0, "fabricada": 0.0, "es_hija": False,
            "con_salida": 0}
    base.update(kw)
    return base


def _etapas(ordenes, ofts, ok_asinfo=True, memo=None):
    from modules._lib import formulas_db
    with patch.object(formulas_db, "fetch_all", return_value=ordenes), \
         patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(ofts, ok_asinfo)):
        return service.etapas_por_pedido(["PDCL-1"], memo or _MEMO)


def test_sin_memo_no_hay_etapa():
    assert service.etapas_por_pedido(["PDCL-9"], {}) == {}


def test_con_memo_y_sin_ordenes_el_pedido_esta_enviado():
    assert _etapas([], []) == {"PDCL-1": "enviado"}


def test_con_orden_pero_sin_salida_de_material_sigue_enviado():
    """La OFT existe pero la tela no salió de bodega: planificado no es
    tinturándose."""
    ordenes = [{"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038020"}]
    assert _etapas(ordenes, [_fila_oft()]) == {"PDCL-1": "enviado"}


def test_la_salida_de_material_pone_al_pedido_en_tintura():
    """La salida se cuelga de una HIJA de la OFT — igual cuenta."""
    ordenes = [{"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038020"}]
    ofts = [_fila_oft(),
            _fila_oft(es_hija=True, con_salida=1, cantidad=100.0)]
    assert _etapas(ordenes, ofts) == {"PDCL-1": "en_tintura"}


def test_todas_las_oft_finalizadas_es_terminado():
    """Finalizada = estado 5. Las hojas mandan: el padre puede quedar en 2
    aunque sus hijas hayan cerrado."""
    ordenes = [{"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038020"}]
    ofts = [_fila_oft(estado_produccion=2),
            _fila_oft(es_hija=True, con_salida=1, estado_produccion=5)]
    assert _etapas(ordenes, ofts) == {"PDCL-1": "terminado"}


def test_una_oft_terminada_y_otra_a_medias_es_en_tintura():
    ordenes = [{"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038020"},
               {"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038021"}]
    ofts = [_fila_oft(estado_produccion=5, con_salida=1),
            _fila_oft(parent_numero="OFT-000038021", con_salida=1)]
    assert _etapas(ordenes, ofts) == {"PDCL-1": "en_tintura"}


def test_al_100_del_plan_tambien_es_terminado_aunque_no_este_cerrada():
    ordenes = [{"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038020"}]
    ofts = [_fila_oft(fabricada=294.0, con_salida=1)]
    assert _etapas(ordenes, ofts) == {"PDCL-1": "terminado"}


def test_la_x_de_la_fabrica_manda_sobre_todo():
    memo = {"PDCL-1": {"estado": "terminado", "en_proceso_por": "jonathan"}}
    assert _etapas([], [], memo=memo) == {"PDCL-1": "terminado"}


def test_con_asinfo_caido_la_etapa_no_inventa_avance():
    """Fail-soft: sin respuesta de Asinfo el pedido queda en enviado —
    nunca un avance que no se pudo probar."""
    ordenes = [{"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038020"}]
    assert _etapas(ordenes, [], ok_asinfo=False) == {"PDCL-1": "enviado"}


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


def _get_corte_pedido(c, etapas=None):
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "estados", return_value={}), \
         patch.object(service, "etapas_por_pedido",
                      return_value=etapas or {}):
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


def test_un_pedido_ya_enviado_muestra_su_etapa_y_no_el_boton(app, fake_db):
    c = _get_corte_pedido(_login(app, fake_db), etapas={
        "PDCL-26438": "enviado",
        "PDCL-26401": "en_tintura",
    })
    body = c.get_data(as_text=True)
    # La tira de pasos: los dos pedidos la llevan, con su paso actual.
    assert "En tintura" in body and body.count('class="steps"') == 2
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
         patch.object(service, "etapas_por_pedido", return_value={}):
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


def test_un_pedido_terminado_muestra_terminado(app, fake_db):
    """Etapa final: todas las OFT cerradas (o la X de la fábrica)."""
    r = _get_corte_pedido(_login(app, fake_db),
                          etapas={"PDCL-26401": "terminado"})
    body = r.get_data(as_text=True)
    assert "Terminado" in body
    assert 'class="btnmemo"' in body   # el otro pedido sigue con su botón
