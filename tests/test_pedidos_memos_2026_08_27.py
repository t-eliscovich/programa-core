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


# ── etapas del pedido, POR LÍNEA (dueña 27/08) ──────────────────────────────
# Cada línea (producto) se matchea con las OFTs del pedido por el producto de
# la OFT. El resumen del pedido: terminado sólo si TODAS las líneas.

_MEMO = {"PDCL-1": {"estado": "pendiente", "en_proceso_por": None}}

_PEDIDO = {"numero": "PDCL-1",
           "lineas": [{"producto": "PI28NEG"}, {"producto": "FE96HAB"}]}


def _fila_oft(**kw):
    """Fila cruda de `_SQL_ETAPA_OFTS` (la forma de la fuente)."""
    base = {"parent_numero": "OFT-000038020", "producto": "PI28NEG",
            "estado_produccion": 2, "cantidad": 294.0, "fabricada": 0.0,
            "es_hija": False, "con_salida": 0}
    base.update(kw)
    return base


def _etapas(ordenes, ofts, ok_asinfo=True, memo=None):
    from modules._lib import formulas_db
    with patch.object(formulas_db, "fetch_all", return_value=ordenes), \
         patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(ofts, ok_asinfo)):
        return service.etapas_por_pedido([dict(_PEDIDO)], memo or _MEMO)


_ORDEN = [{"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038020"}]


def test_sin_memo_no_hay_etapa():
    assert service.etapas_por_pedido([{"numero": "PDCL-9", "lineas": []}], {}) == {}


def test_con_memo_y_sin_ordenes_todo_esta_enviado():
    r = _etapas([], [])["PDCL-1"]
    assert r["pedido"] == "enviado"
    assert r["lineas"] == {"PI28NEG": "enviado", "FE96HAB": "enviado"}


def test_la_orden_creada_avanza_SOLO_la_linea_de_su_producto():
    """La OFT de la orden es del NEG: el NEG pasa a en tintura APENAS la
    fábrica crea la orden (caso PDCL-30833 del 31/08: Asinfo asigna la
    salida de material DESPUÉS de arrancar — no se la espera). El HAB, sin
    orden, sigue enviado; el resumen dice en tintura."""
    ofts = [_fila_oft(),
            _fila_oft(es_hija=True, cantidad=100.0)]
    r = _etapas(_ORDEN, ofts)["PDCL-1"]
    assert r["lineas"] == {"PI28NEG": "en_tintura", "FE96HAB": "enviado"}
    assert r["pedido"] == "en_tintura"


def test_la_linea_termina_cuando_sus_hojas_estan_finalizadas():
    ofts = [_fila_oft(estado_produccion=2, producto=""),
            _fila_oft(es_hija=True, con_salida=1, estado_produccion=5)]
    r = _etapas(_ORDEN, ofts)["PDCL-1"]
    assert r["lineas"]["PI28NEG"] == "terminado"
    # La otra línea sigue enviada → el pedido NO está terminado.
    assert r["pedido"] == "en_tintura"


def test_el_pedido_termina_cuando_TODAS_sus_lineas_terminaron():
    ofts = [_fila_oft(es_hija=True, con_salida=1, estado_produccion=5),
            _fila_oft(es_hija=True, con_salida=1, estado_produccion=5,
                      producto="FE96HAB")]
    r = _etapas(_ORDEN, ofts)["PDCL-1"]
    assert r["lineas"] == {"PI28NEG": "terminado", "FE96HAB": "terminado"}
    assert r["pedido"] == "terminado"


def test_dos_ofts_del_mismo_producto_terminan_cuando_terminan_las_dos():
    ordenes = [{"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038020"},
               {"pedido_numero": "PDCL-1", "oft_numero": "OFT-000038021"}]
    ofts = [_fila_oft(es_hija=True, con_salida=1, estado_produccion=5),
            _fila_oft(parent_numero="OFT-000038021", es_hija=True,
                      con_salida=1, estado_produccion=2)]
    r = _etapas(ordenes, ofts)["PDCL-1"]
    assert r["lineas"]["PI28NEG"] == "en_tintura"


def test_al_100_del_plan_tambien_es_terminado_aunque_no_este_cerrada():
    ofts = [_fila_oft(es_hija=True, con_salida=1, fabricada=294.0)]
    r = _etapas(_ORDEN, ofts)["PDCL-1"]
    assert r["lineas"]["PI28NEG"] == "terminado"


def test_la_x_de_la_fabrica_NO_pisa_las_etapas():
    """Dueña 31/08: Jonathan pone la X sólo para que el memo no le siga
    apareciendo en formulas — es limpieza de su lista, no un dato de
    producción. El pedido sigue mostrando lo que dice la producción real."""
    memo = {"PDCL-1": {"estado": "terminado", "en_proceso_por": "jonathan"}}
    assert _etapas([], [], memo=memo)["PDCL-1"]["pedido"] == "enviado"


def test_con_asinfo_caido_la_etapa_no_inventa_avance():
    r = _etapas(_ORDEN, [], ok_asinfo=False)["PDCL-1"]
    assert r["pedido"] == "enviado"


# ── formulas_memos (el bridge de escritura) ─────────────────────────────────

def test_sin_env_var_el_envio_degrada_sin_romper():
    assert formulas_memos.disponible() is False
    ok, motivo = formulas_memos.enviar("P", "C", "V", "yo", {})
    assert (ok, motivo) == (False, "sin_bridge")
    assert formulas_memos.estados(["P"]) == {}


@pytest.fixture
def _pool_formulas_memos():
    """Reactiva/limpia formulas_memos._pool para los tests que lo wirean con
    un fake — mismo mecanismo que `_wire_fake_pool` de test_formulas_db.py,
    replicado acá (el módulo no expone un helper propio)."""
    original = formulas_memos._pool
    yield
    formulas_memos._pool = original


def _wire_fake_pool_memos(fila_returned):
    """Fake pool mínimo: `cur.fetchone()` devuelve `fila_returned` (una tupla
    tipo (id,) si el UPDATE/INSERT pegó, o None si el WHERE no matcheó)."""
    from unittest.mock import MagicMock

    fake_cur = MagicMock()
    fake_cur.fetchone.return_value = fila_returned

    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    fake_conn.cursor.return_value.__exit__.return_value = False

    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn
    formulas_memos._pool = fake_pool
    return fake_cur


def test_cancelar_un_memo_pendiente_pega(_pool_formulas_memos):
    cur = _wire_fake_pool_memos((5,))
    ok, motivo = formulas_memos.cancelar("PDCL-1", "jonathan")
    assert (ok, motivo) == (True, "cancelado")
    args = cur.execute.call_args.args
    assert "estado = 'pendiente'" in args[0]
    assert args[1] == ("jonathan", "PDCL-1")


def test_cancelar_un_memo_en_proceso_no_hace_nada(_pool_formulas_memos):
    """El UPDATE trae `WHERE ... AND estado = 'pendiente'`: si la fábrica ya
    lo tomó (en_proceso/terminado) el WHERE no matchea, 0 filas."""
    _wire_fake_pool_memos(None)
    ok, motivo = formulas_memos.cancelar("PDCL-1", "jonathan")
    assert (ok, motivo) == (False, "no_pendiente")


def test_enviar_reactiva_un_memo_cancelado(_pool_formulas_memos):
    """El ON CONFLICT ... DO UPDATE ... WHERE memos.estado = 'cancelado'
    matchea: la fila se reactiva y `enviar()` avisa "enviado", como si fuera
    nueva."""
    _wire_fake_pool_memos((9,))
    ok, motivo = formulas_memos.enviar("PDCL-1", "Cliente", "PPR", "test", {})
    assert (ok, motivo) == (True, "enviado")


def test_enviar_sigue_frenando_si_el_conflicto_es_con_un_memo_vivo(_pool_formulas_memos):
    """Si el conflicto es con un memo pendiente/en_proceso/terminado, el
    WHERE `estado = 'cancelado'` no matchea: 0 filas, "ya_enviado" — el
    comportamiento de siempre no cambió."""
    cur = _wire_fake_pool_memos(None)
    ok, motivo = formulas_memos.enviar("PDCL-1", "Cliente", "PPR", "test", {})
    assert (ok, motivo) == (False, "ya_enviado")
    assert "ON CONFLICT" in cur.execute.call_args.args[0]


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
        "PDCL-26438": {"pedido": "enviado", "lineas": {}},
        "PDCL-26401": {"pedido": "en_tintura",
                       "lineas": {"PI28NEG": "en_tintura"}},
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


# ── cancelar-memo (2026-09-01) ──────────────────────────────────────────────

def test_cancelar_memo_sin_permiso_es_404(app, fake_db):
    c = _login(app, fake_db, permisos=("facturas.ver",))
    with patch.object(formulas_memos, "cancelar") as canc:
        r = c.post("/pedidos/cancelar-memo", data={"numero": "PDCL-26438"})
    assert r.status_code == 404
    canc.assert_not_called()


def test_cancelar_memo_pendiente_confirma_y_puede_reenviarse(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(formulas_memos, "cancelar",
                      return_value=(True, "cancelado")) as canc:
        r = c.post("/pedidos/cancelar-memo", data={"numero": "PDCL-26438"},
                   follow_redirects=True)
    assert "cancelado" in r.get_data(as_text=True)
    assert "Ya lo podés mandar de nuevo" in r.get_data(as_text=True)
    canc.assert_called_once_with("PDCL-26438", "test")


def test_cancelar_memo_no_pendiente_avisa_y_no_rompe(app, fake_db):
    """Cubre tanto el pedido inexistente como el que la fábrica ya tomó: el
    UPDATE con WHERE estado='pendiente' no matchea en ninguno de los dos
    casos, así que `cancelar()` devuelve el mismo "no_pendiente"."""
    c = _login(app, fake_db)
    with patch.object(formulas_memos, "cancelar",
                      return_value=(False, "no_pendiente")):
        r = c.post("/pedidos/cancelar-memo", data={"numero": "PDCL-99999"},
                   follow_redirects=True)
    assert "ya no está pendiente" in r.get_data(as_text=True)


def test_cancelar_memo_sin_numero_no_llama_al_bridge(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(formulas_memos, "cancelar") as canc:
        r = c.post("/pedidos/cancelar-memo", data={}, follow_redirects=True)
    assert "Falta el número de pedido" in r.get_data(as_text=True)
    canc.assert_not_called()


def test_pedido_con_memo_cancelado_no_cuenta_como_tiene_memo(app, fake_db):
    """El filtrado vive en la VIEW: `estados()` puede traer un memo
    'cancelado', pero el dict que llega a `etapas_por_pedido` ya no lo
    incluye — la pantalla vuelve a ofrecer "Enviar memo" para ese pedido."""
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "estados",
                      return_value={"PDCL-26438": {"estado": "cancelado",
                                                    "en_proceso_por": None}}), \
         patch.object(service, "etapas_por_pedido", return_value={}) as etapas_mock:
        r = c.get("/pedidos?corte=pedido")
    activos_pasado = etapas_mock.call_args.args[1]
    assert "PDCL-26438" not in activos_pasado
    body = r.get_data(as_text=True)
    assert 'class="btnmemo"' in body


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


def test_el_vendedor_no_puede_cancelar_un_pedido_ajeno(app, fake_db):
    """PPR postea el numero del pedido de RMY: 404, como si no existiera —
    calcado del cerco de `pedidos_enviar_memo`."""
    c = _login_vendedor(app, fake_db, vend="PPR")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "cancelar") as canc:
        r = c.post("/mi-cartera/pedidos/cancelar-memo",
                   data={"numero": "PDCL-26401"})
    assert r.status_code == 404
    canc.assert_not_called()


def test_el_vendedor_cancela_lo_suyo_y_queda_registrado_su_usuario(app, fake_db):
    c = _login_vendedor(app, fake_db, vend="PPR")
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS, True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "cancelar",
                      return_value=(True, "cancelado")) as canc:
        r = c.post("/mi-cartera/pedidos/cancelar-memo",
                   data={"numero": "PDCL-26438"})
    assert r.status_code == 302
    canc.assert_called_once_with("PDCL-26438", "patricio")


def test_un_pedido_terminado_muestra_terminado(app, fake_db):
    """Etapa final: todas las OFT cerradas (o la X de la fábrica)."""
    r = _get_corte_pedido(_login(app, fake_db),
                          etapas={"PDCL-26401": {"pedido": "terminado",
                                                 "lineas": {"PI28NEG": "terminado"}}})
    body = r.get_data(as_text=True)
    assert "Terminado" in body
    assert 'class="btnmemo"' in body   # el otro pedido sigue con su botón
