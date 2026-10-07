"""El ACABADO es parte del renglón y sale de la LÍNEA del pedido (06/10/2026).

Caso testigo: PDCL-32677 (GIC, 01/10, Fleece 96 Perchado MARINO). En Asinfo
las dos líneas (FE96MAR y FE96CRU) son ABI (`id_atributo_3 = 1`,
`id_valor_atributo_3 = 2`); los cortes Color y Tipo de tela de /pedidos lo
pintaban TUB porque leían el acabado de los LOTES en stock y se quedaban con
el MAX ('TUB' > 'ABI'). Alex lo reportó; Tamara: "no es lo mismo pedir abi
o tub" — el renglón pasa a ser tela + color + acabado.

Los fakes devuelven filas con la forma de la FUENTE (aca_min / aca_max, como
las escribe el SQL contra Asinfo).
"""
from unittest.mock import patch

import pytest

from modules._lib import formulas_memos
from modules.pedidos import service, vigia_acabado


@pytest.fixture(autouse=True)
def _sin_cache():
    service.reset_cache()
    yield
    service.reset_cache()


def _renglon(**kw):
    """Fila cruda de `_SQL_PENDIENTES` — el FE96MAR del PDCL-32677."""
    base = {
        "categoria": "Fleece", "tela": "Fleece 96 Perchado",
        "codigo": "FE96MAR", "color": "MAR", "aca_min": "ABI", "aca_max": "ABI",
        "ped_kg": 141.0, "ped_rollos": 6.0, "ped_un": 0.0, "un_por_kg": None,
        "n_pedidos": 1, "n_clientes": 1, "mas_viejo": "2026-10-01",
        "inv_kg": 0.0, "prod_kg": 983.0, "n_ordenes": 3,
    }
    base.update(kw)
    return base


def _pedido_32677(codigo="FE96MAR", **kw):
    """Fila cruda de `_SQL_POR_PEDIDO` / `_SQL_PEDIDOS_TODOS`."""
    base = {
        "numero": "PDCL-32677", "fecha": "2026-10-01",
        "cliente": "CHASI PEREZ GISSELA ELIZABETH", "codigo_cliente": "GIC",
        "agente_id": 751, "agente_nombre": "Proaño Patricio", "descripcion": "",
        "codigo": codigo, "color": codigo[-3:], "tela": "Fleece 96 Perchado",
        "cantidad": 6.0, "unidad": 51, "aca_min": "ABI", "aca_max": "ABI",
    }
    base.update(kw)
    return base


_VENDEDORES = {frozenset({"PATRICIO", "PROANO"}):
               {"codigo": "PPR", "nombre": "Patricio Proano"}}


def _login(app, fake_db, permisos=("facturas.ver", "pedidos.enviar_memo")):
    rid = fake_db.add_role("Tester", list(permisos))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _fake_pantalla(renglones, pedidos):
    def fake(_db, sql, **_kw):
        if "ORDER BY pr.codigo, v.fecha" in sql:
            return pedidos, True
        if "ORDER BY v.fecha DESC, v.numero" in sql:      # por_pedido
            return pedidos, True
        return renglones, True
    return fake


# ── la regla única ──────────────────────────────────────────────────────────

def test_una_sola_funcion_rotula_el_acabado():
    assert service.acabado_de_linea("abi", "ABI") == "ABI"
    assert service.acabado_de_linea("ABI", "TUB") == "ABI/TUB"
    assert service.acabado_de_linea(None, None) == ""


def test_todas_las_consultas_de_pedidos_usan_la_misma_regla_del_acabado():
    """/pedidos (renglones y desplegable), el detalle de un color, el corte
    Pedido (que alimenta /mi-cartera y el memo) y el sync de memos: todas
    leen el acabado con la MISMA expresión, de la línea del pedido."""
    regla = service._SQL_VALOR_ACABADO_LINEA
    for sql in (service._sql_pendientes(), service._SQL_PEDIDOS_TODOS,
                service._SQL_DETALLE_PEDIDOS, service._SQL_POR_PEDIDO,
                service._SQL_ACABADO_POR_NUMERO):
        assert regla in sql
        assert "detalle_pedido_cliente d" in sql


def test_los_renglones_se_parten_por_acabado_en_el_sql():
    sql = service._sql_pendientes()
    assert "GROUP BY v.id_producto, ISNULL(a.aca_min, ''), ISNULL(a.aca_max, '')" in sql
    # El stock que cubre un renglón es el de lotes del MISMO acabado.
    assert "ia.aca = ped.aca_min" in sql


def test_pdcl_32677_sale_abi_aunque_el_stock_tenga_lotes_tub(app, fake_db):
    """El caso de Alex: FE96MAR tiene lotes TUB y ABI en bodega; el pedido
    es ABI. La pantalla NO mira el acabado del stock."""
    c = _login(app, fake_db)
    fake = _fake_pantalla([_renglon()], [_pedido_32677()])
    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        for corte in ("color", "tela"):
            body = c.get(f"/pedidos?corte={corte}").get_data(as_text=True)
            boton = body[body.index('data-fila="d-FE96MAR_ABI"'):]
            boton = boton[:boton.index("</button>")]
            assert 'class="aca abi"' in boton
            assert 'class="aca tub"' not in boton
            # La pastilla TUB sólo queda en la leyenda.
            assert body.count('class="aca tub"') <= 1
            assert "PDCL-32677" in body          # el desplegable del renglón ABI
    # El acabado POR PRODUCTO (MAX de los lotes) ya no existe (07/10).
    assert not hasattr(service, "acabados_por_producto")


def test_el_mismo_color_pedido_abi_y_tub_son_dos_renglones(app, fake_db):
    c = _login(app, fake_db)
    renglones = [_renglon(), _renglon(aca_min="TUB", aca_max="TUB", ped_kg=282.0,
                                      ped_rollos=12.0, n_pedidos=4)]
    pedidos = [_pedido_32677(),
               _pedido_32677(numero="PDCL-32001", codigo_cliente="KAM",
                             aca_min="TUB", aca_max="TUB")]
    fake = _fake_pantalla(renglones, pedidos)
    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    assert 'data-fila="d-FE96MAR_ABI"' in body and 'data-fila="d-FE96MAR_TUB"' in body
    abi = body[body.index('data-grupo="d-FE96MAR_ABI"'):]
    abi = abi[:abi.index("</tr>")]
    assert "PDCL-32677" in abi and "PDCL-32001" not in abi
    tub = body[body.index('data-grupo="d-FE96MAR_TUB"'):]
    tub = tub[:tub.index("</tr>")]
    assert "PDCL-32001" in tub and "PDCL-32677" not in tub


def test_lo_que_esta_en_produccion_se_reparte_sin_contarse_dos_veces():
    """La orden de tintura no dice el acabado: se reparte por lo que falta."""
    filas = [service._fila(_renglon(ped_kg=100.0, inv_kg=0.0, prod_kg=90.0)),
             service._fila(_renglon(aca_min="TUB", aca_max="TUB", ped_kg=300.0,
                                    inv_kg=100.0, prod_kg=90.0))]
    service.repartir_produccion(filas)
    abi, tub = filas
    assert abi["produccion_kg"] + tub["produccion_kg"] == pytest.approx(90.0)
    assert abi["produccion_kg"] == pytest.approx(30.0)   # le faltan 100 de 300
    assert abi["faltan_kg"] == pytest.approx(70.0)
    assert abi["prod_repartida"] and tub["prod_repartida"]


def test_un_producto_con_un_solo_acabado_no_reparte_nada():
    filas = [service._fila(_renglon(prod_kg=90.0))]
    service.repartir_produccion(filas)
    assert filas[0]["produccion_kg"] == 90.0 and filas[0]["prod_repartida"] is False


def test_el_detalle_del_color_muestra_solo_los_pedidos_de_ese_acabado():
    pedidos = [_pedido_32677(), _pedido_32677(numero="PDCL-32001", aca_min="TUB",
                                              aca_max="TUB")]

    def fake(_db, sql, **_kw):
        if "orden_fabricacion" in sql and "saldos_comprometidos" not in sql:
            return [], True
        if "WHERE pr.codigo = 'FE96MAR'" in sql:
            return pedidos, True
        return [_renglon(), _renglon(aca_min="TUB", aca_max="TUB")], True

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        ficha, peds, _o, _ok = service.detalle_color("FE96MAR", "ABI")
    assert ficha["acabado"] == "ABI"
    assert [p["numero"] for p in peds] == ["PDCL-32677"]


def test_el_corte_pedido_mi_cartera_y_el_memo_dicen_abi(app, fake_db):
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([_pedido_32677(), _pedido_32677("FE96CRU")], True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES):
        memo = service.armar_memo("PDCL-32677")
        pedidos, _ = service.por_pedido()
    assert [ln["acabado"] for ln in memo["lineas"]] == ["ABI", "ABI"]
    assert [ln["acabado"] for ln in pedidos[0]["lineas"]] == ["ABI", "ABI"]

    rid = fake_db.add_role("Vendedor", ["micartera.ver"])
    uid = fake_db.add_user("patricio", b"$2b$12$fakehash", rid, vend="PPR")
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([_pedido_32677()], True)), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "estados", return_value={}), \
         patch.object(service, "etapas_por_pedido", return_value={}):
        body = c.get("/mi-cartera/pedidos").get_data(as_text=True)
    assert "PDCL-32677" in body
    assert 'class="aca abi"' in body and 'class="aca tub"' not in body


# ── el vigía del health ─────────────────────────────────────────────────────

_VERDAD = [{"numero": "PDCL-32677", "codigo": "FE96MAR", "categoria": "Fleece",
            "acabado": "ABI"}]


def _vigia(renglones, pedidos, memos=(), verdad=_VERDAD):
    def fake(_db, sql, **_kw):
        if "va.id_atributo = 1\n   AND va.id_valor_atributo IN" in sql and "v.numero, pr.codigo" in sql:
            return verdad, True
        if "ORDER BY v.fecha DESC, v.numero" in sql:
            return pedidos, True
        return renglones, True
    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake), \
         patch.object(service, "mapa_vendedores", return_value=_VENDEDORES), \
         patch.object(formulas_memos, "vivos", return_value=list(memos)):
        return vigia_acabado.health()


def test_el_vigia_esta_en_verde_cuando_todas_las_pantallas_coinciden():
    r = _vigia([_renglon()], [_pedido_32677()])
    assert r["ok"] and r["alerts"] == []
    assert r["stats"]["abi"] == 1


def test_el_vigia_canta_el_renglon_tub_del_pdcl_32677():
    """Exactamente lo que vio Alex: el renglón sale TUB y el pedido es ABI."""
    r = _vigia([_renglon(aca_min="TUB", aca_max="TUB")], [_pedido_32677()])
    assert r["ok"] is False
    (a,) = r["alerts"]
    assert a["category"] == "acabado_pedidos_renglon"
    assert "FE96MAR" in a["msg"] and "PDCL-32677" in a["msg"] and "TUB" in a["msg"]


def test_el_vigia_canta_la_linea_del_corte_pedido_y_mi_cartera():
    r = _vigia([_renglon()], [_pedido_32677(aca_min="TUB", aca_max="TUB")])
    assert [a["category"] for a in r["alerts"]] == ["acabado_pedidos_linea"]
    assert r["ok"] is False


def test_el_vigia_canta_el_memo_ya_mandado_con_otro_acabado():
    memo = {"numero": "PDCL-32677", "detalle": {"lineas": [
        {"producto": "FE96MAR", "acabado": "TUB"}]}}
    r = _vigia([_renglon()], [_pedido_32677()], memos=[memo])
    assert [a["category"] for a in r["alerts"]] == ["acabado_pedidos_memo"]


def test_el_vigia_lee_la_verdad_por_otro_camino_que_las_pantallas():
    """Si alguien rompe la regla única, los dos caminos dejan de coincidir."""
    assert service._SQL_VALOR_ACABADO_LINEA not in vigia_acabado._SQL_VERDAD
    assert "va.id_atributo = 1" in vigia_acabado._SQL_VERDAD


def test_sin_asinfo_el_vigia_no_se_pone_rojo():
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        r = vigia_acabado.health()
    assert r["ok"] and "sin_datos" in r["stats"]


def test_el_vigia_esta_en_el_health_all():
    import inspect

    from modules.admin_dbase import health_audit_view as h
    assert '"acabado_pedidos": data30' in inspect.getsource(h.health_all)


# ── el vigía también mira las ventas de Saldos (07/10/2026) ─────────────────

def test_el_vigia_canta_una_venta_de_saldos_con_el_acabado_equivocado():
    verdad = [{"numero": "001-099-000185071", "subcategoria": "Fleece 96 Perchado",
               "color": "CRU", "acabado": "ABI"}]
    guardadas = [{"numero": "001-099-000185071", "subcategoria": "Fleece 96 Perchado",
                  "color": "CRU", "acabado": "TUB"}]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(verdad, True)), \
         patch("db.fetch_all", return_value=guardadas):
        problemas, n, ok = vigia_acabado.ventas_saldos()
    assert ok and n == 1
    assert problemas == ["001-099-000185071 Fleece 96 Perchado CRU: dice TUB y Asinfo ABI"]


def test_el_vigia_de_saldos_en_verde_cuando_coinciden():
    verdad = [{"numero": "N1", "subcategoria": "Jersey 3", "color": "BLA", "acabado": "TUB"}]
    guardadas = [{"numero": "N1", "subcategoria": "Jersey 3", "color": "BLA", "acabado": "TUB"}]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(verdad, True)), \
         patch("db.fetch_all", return_value=guardadas):
        assert vigia_acabado.ventas_saldos() == ([], 1, True)


def test_la_verdad_de_saldos_tambien_va_por_otro_camino():
    from modules.analisis import asinfo_parado
    assert "va.id_atributo = 1 AND va.id_valor_atributo IN" in vigia_acabado._SQL_VENTAS
    assert service.sql_valor_acabado("dfc") not in vigia_acabado._SQL_VENTAS
    assert service.sql_valor_acabado("dfc") in asinfo_parado._sql_vendido("2026-08-25")
