"""Pedidos pendientes (Asinfo) — service y pantalla.

Los fakes devuelven filas con la forma de la FUENTE (los nombres de columna que
escribe el SQL contra Asinfo), no la del helper: un fake con la forma equivocada
pasa en verde mientras producción no resuelve nada.
"""
from datetime import date
from unittest.mock import patch

import pytest

from modules.pedidos import service


def _fila(**kw):
    """Fila cruda de `_SQL_PENDIENTES`, con los defaults de un color de tela."""
    base = {
        "categoria": "Fleece", "tela": "Fleece 96 Perchado",
        "codigo": "FE96CAF", "color": "CAF",
        "ped_kg": 447.0, "ped_rollos": 19.0, "ped_un": 0.0, "un_por_kg": None,
        "n_pedidos": 3, "n_clientes": 2, "mas_viejo": "2026-08-05",
        "inv_kg": 0.0, "prod_kg": 0.0, "n_ordenes": 0,
    }
    base.update(kw)
    return base


# ── conversión de unidades ──────────────────────────────────────────────────

def test_los_rollos_ya_vienen_en_kilos_desde_el_sql():
    f = service._fila(_fila())
    assert f["pedido_kg"] == 447.0
    assert f["pedido_rollos"] == 19.0
    assert f["en_unidades"] is False


def test_las_unidades_se_convierten_a_kilos_con_el_factor_del_producto():
    """Cuellos y puños se piden por unidad. 500 cuellos a 33,33 un/kg = 15 kg.

    Sin esta división, "18.077" se lee como kilos y son unidades — el error que
    inflaba Cuellos a 17.926 kg cuando son 542.
    """
    f = service._fila(_fila(
        categoria="Cuellos", tela="Cuello 40", codigo="C40BCR",
        ped_kg=0.0, ped_rollos=0.0, ped_un=500.0, un_por_kg=33.33333333))
    assert f["pedido_kg"] == 15.0
    assert f["pedido_un"] == 500.0
    assert f["en_unidades"] is True


def test_una_unidad_sin_factor_de_conversion_no_inventa_kilos():
    """Si falta el factor, los kilos NO se completan con las unidades crudas.

    Preferimos un pedido_kg en 0 (que se ve raro y se pregunta) antes que un
    número plausible y equivocado.
    """
    f = service._fila(_fila(
        categoria="Puños", ped_kg=0.0, ped_un=400.0, un_por_kg=None))
    assert f["pedido_kg"] == 0.0
    assert f["pedido_un"] == 400.0


def test_el_faltante_resta_bodega_y_produccion():
    f = service._fila(_fila(ped_kg=987.0, inv_kg=20.0, prod_kg=0.0))
    assert f["faltan_kg"] == 967.0


def test_lo_que_sobra_da_faltante_negativo():
    f = service._fila(_fila(ped_kg=235.0, inv_kg=8728.0, prod_kg=3141.0))
    assert f["faltan_kg"] == pytest.approx(-11634.0)


def test_una_fila_rota_no_levanta():
    f = service._fila({"categoria": "Fleece", "codigo": "X", "ped_kg": "no"})
    assert f["pedido_kg"] == 0.0
    assert f["faltan_kg"] == 0.0


# ── la SQL ──────────────────────────────────────────────────────────────────

def test_la_sql_pregunta_la_hora_de_ecuador_y_no_la_del_server():
    """En Asinfo `GETDATE()` es UTC y las fechas se graban en hora local.

    Usarlo pelado corre la antigüedad un día entero a partir de las 19:00 EC.
    """
    sql = service._sql_pendientes()
    assert "DATEADD(hour, -5, GETDATE())" in sql
    assert "DATEDIFF(day, v.fecha, GETDATE())" not in sql
    assert "DATEDIFF(day, o.fecha, GETDATE())" not in sql


def test_la_sql_descarta_los_padres_y_se_queda_con_las_hojas():
    """Las órdenes viven en padre + hijas: sumar las dos capas cuenta doble."""
    sql = service._sql_pendientes()
    assert "id_orden_fabricacion_padre" in sql
    assert "h.p IS NULL" in sql


def test_la_sql_solo_cuenta_produccion_lanzada_y_reciente():
    """Estado 0 son órdenes abandonadas (660 días promedio, 0 kg fabricados).

    Si entran, la pantalla nunca muestra un faltante.
    """
    sql = service._sql_pendientes()
    assert "o.estado_produccion = 2" in sql
    assert f"<= {service.DIAS_PRODUCCION_VIVA}" in sql


# ── pendientes() y el bridge caído ──────────────────────────────────────────

def test_pendientes_filtra_las_familias_que_no_son_tela():
    filas = [_fila(), _fila(categoria="HILO", codigo="HIL1"),
             _fila(categoria="TELA CRUDA", codigo="TC-1")]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(filas, True)):
        out, ok = service.pendientes()
    assert ok is True
    assert [f["codigo"] for f in out] == ["FE96CAF"]


def test_si_asinfo_no_contesta_devuelve_disponible_false_y_no_lista_vacia_muda():
    """`[]` con ok=False es "no pude preguntar", no "no falta nada".

    Es la distinción que rompió el balance el 29/07 al cachear un fracaso.
    """
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        out, ok = service.pendientes()
    assert out == []
    assert ok is False


# ── agrupaciones ────────────────────────────────────────────────────────────

def test_el_resumen_por_familia_solo_suma_los_faltantes_positivos():
    """Un color que sobra no compensa a uno que falta: son telas distintas."""
    filas = [service._fila(_fila(ped_kg=100, inv_kg=0, prod_kg=0)),
             service._fila(_fila(codigo="FE96NEG", ped_kg=10, inv_kg=9000))]
    (cat,) = service.por_categoria(filas)
    assert cat["faltan_kg"] == 100.0
    assert cat["colores_faltan"] == 1
    assert cat["colores"] == 2


def test_las_familias_se_ordenan_por_lo_que_falta():
    filas = [service._fila(_fila(categoria="Rib", ped_kg=10, inv_kg=0)),
             service._fila(_fila(categoria="Jersey", ped_kg=500, inv_kg=0))]
    assert [c["categoria"] for c in service.por_categoria(filas)] == ["Jersey", "Rib"]


def test_adentro_de_la_tela_lo_que_falta_va_arriba():
    filas = [service._fila(_fila(codigo="A", ped_kg=10, inv_kg=900)),
             service._fila(_fila(codigo="B", ped_kg=900, inv_kg=0))]
    (tela,) = service.por_tela(filas, "Fleece")
    assert [f["codigo"] for f in tela["filas"]] == ["B", "A"]


def test_por_tela_ignora_las_otras_familias():
    filas = [service._fila(_fila()),
             service._fila(_fila(categoria="Rib", tela="Rib 1x1", codigo="RI1"))]
    assert [t["tela"] for t in service.por_tela(filas, "Rib")] == ["Rib 1x1"]


# ── fechas ──────────────────────────────────────────────────────────────────

def test_la_fecha_se_muestra_como_la_lee_la_duena():
    assert service._fecha_corta("2026-08-05") == "5 ago"
    assert service._fecha_corta("") == ""
    assert service._fecha_corta("mañana") == ""


def test_los_dias_de_espera_nunca_son_negativos():
    assert service._dias_desde("2026-08-05", hoy=date(2026, 8, 17)) == 12
    assert service._dias_desde("2026-08-20", hoy=date(2026, 8, 17)) == 0
    assert service._dias_desde("", hoy=date(2026, 8, 17)) == 0


# ── detalle de un color ─────────────────────────────────────────────────────

def test_el_codigo_de_la_url_se_sanitiza_antes_de_entrar_a_la_sql():
    assert service.codigo_seguro(" fe96caf ") == "FE96CAF"
    assert service.codigo_seguro("TC-JE-3.8-AB") == "TC-JE-3.8-AB"
    assert service.codigo_seguro("X'; DROP TABLE producto;--") == "XDROPTABLEPRODUCTO--"
    assert service.codigo_seguro("") == ""
    assert service.codigo_seguro(None) == ""


def test_un_codigo_que_queda_vacio_no_le_pregunta_nada_a_asinfo():
    with patch.object(service.metabase_client, "fetch_dataset_estado") as fake:
        ficha, peds, ords, ok = service.detalle_color("';")
    assert (ficha, peds, ords, ok) == (None, [], [], True)
    fake.assert_not_called()


def test_el_detalle_trae_pedidos_y_ordenes_con_el_codigo_de_cliente():
    pedidos = [{"numero": "PDCL-29712", "cliente": "MALDONADO ANA KARINA",
                "codigo_cliente": "KAM", "fecha": "2026-08-05T00:00:00Z",
                "cantidad": 9, "unidad": 51, "estado": 5}]
    ordenes = [{"numero": "OFT-000040497.1", "fecha": "2026-08-10T00:00:00Z",
                "cantidad": 539, "fabricada": 474.65, "dias": 7}]

    def fake(_db, sql, **_kw):
        if "orden_fabricacion" in sql and "saldos_comprometidos" not in sql:
            return ordenes, True
        if "v_saldos_comprometidos_detallado v\n  JOIN producto" in sql:
            return pedidos, True
        return [_fila()], True

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        ficha, peds, ords, ok = service.detalle_color("fe96caf")

    assert ok is True
    assert ficha["codigo"] == "FE96CAF"
    assert peds[0]["codigo_cliente"] == "KAM"
    assert peds[0]["es_rollo"] is True
    assert peds[0]["kg"] == 211.5          # 9 rollos × 23,5
    assert peds[0]["fecha_es"] == "5 ago"
    assert ords[0]["avance"] == 88          # 474,65 de 539


def test_un_pedido_en_kilos_no_se_multiplica_por_el_rollo():
    p = service._fila_pedido({"numero": "P", "cliente": "C", "fecha": "2026-08-05",
                              "cantidad": 100, "unidad": service.UNIDAD_KG})
    assert p["kg"] == 100.0
    assert p["es_rollo"] is False


def test_un_pedido_en_unidades_no_declara_kilos():
    p = service._fila_pedido({"numero": "P", "cliente": "C", "fecha": "2026-08-05",
                              "cantidad": 500, "unidad": service.UNIDAD_UN})
    assert p["kg"] is None
    assert p["es_unidad"] is True


def test_una_orden_sin_kilos_fabricados_no_divide_por_cero():
    o = service._fila_orden({"numero": "OFT-1", "fecha": "2026-08-12",
                             "cantidad": 0, "fabricada": 0, "dias": 5})
    assert o["avance"] == 0


# ── repetidos ───────────────────────────────────────────────────────────────

def test_marca_al_cliente_que_pidio_dos_veces_el_mismo_color():
    """KAM pidió FE96CAF el 5 y el 14 de agosto sin recibir el primero."""
    peds = [{"codigo_cliente": "KAM", "cliente": "Maldonado"},
            {"codigo_cliente": "KAM", "cliente": "Maldonado"},
            {"codigo_cliente": "AL1", "cliente": "Almacenes Lira"}]
    assert service.repetidos(peds) == ["KAM"]


def test_sin_codigo_de_cliente_se_agrupa_por_nombre():
    peds = [{"codigo_cliente": "", "cliente": "Naula Guashpa"},
            {"codigo_cliente": "", "cliente": "Naula Guashpa"}]
    assert service.repetidos(peds) == ["Naula Guashpa"]


def test_un_cliente_por_pedido_no_es_repetido():
    peds = [{"codigo_cliente": "KAM", "cliente": "M"},
            {"codigo_cliente": "AL1", "cliente": "A"}]
    assert service.repetidos(peds) == []


# ── la pantalla ─────────────────────────────────────────────────────────────

def _login(app, fake_db):
    rid = fake_db.add_role("Tester", ["facturas.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


_FILAS_PANTALLA = [
    _fila(),                                                   # falta 447
    _fila(codigo="FE96NEG", color="NEG", ped_kg=235.0, ped_rollos=10.0,
          inv_kg=8728.0, prod_kg=3141.0, n_ordenes=13, mas_viejo="2026-08-12"),
    _fila(categoria="Jersey", tela="Jersey 3.2", codigo="JE32AZS", color="AZS",
          ped_kg=1000.0, ped_rollos=42.0, inv_kg=0.0, mas_viejo="2026-08-14"),
]


def test_la_pantalla_abre_en_la_familia_que_mas_falta(app, fake_db):
    """Jersey falta 1.000 y Fleece 447: la pestaña activa es Jersey."""
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS_PANTALLA, True)):
        r = c.get("/pedidos")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Jersey 3.2" in body
    assert "JE32AZS" in body
    assert "Fleece 96 Perchado" not in body      # es otra pestaña


def test_se_puede_elegir_la_pestana(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS_PANTALLA, True)):
        r = c.get("/pedidos?cat=Fleece")
    body = r.get_data(as_text=True)
    assert "FE96CAF" in body and "JE32AZS" not in body
    assert "447" in body                          # el faltante
    assert "ok" in body                           # el NEG que sobra


def test_una_pestana_inventada_cae_a_la_primera_y_no_rompe(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS_PANTALLA, True)):
        r = c.get("/pedidos?cat=Terciopelo")
    assert r.status_code == 200
    assert "Jersey 3.2" in r.get_data(as_text=True)


def test_con_asinfo_caido_la_pantalla_abre_y_lo_dice(app, fake_db):
    """200 con cartel, nunca 500 ni un 'no falta nada' silencioso."""
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        r = c.get("/pedidos")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "No se pudo consultar Asinfo" in body
    assert "es que no pudo preguntar" in body


def test_sin_pedidos_pendientes_lo_dice_sin_confundirlo_con_un_error(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], True)):
        r = c.get("/pedidos")
    assert r.status_code == 200
    assert "No hay pedidos pendientes" in r.get_data(as_text=True)


def test_sin_permiso_la_pantalla_no_existe(app, fake_db):
    rid = fake_db.add_role("Pelado", ["stock.ver"])
    uid = fake_db.add_user("pelado", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    assert c.get("/pedidos").status_code == 404


def test_el_detalle_de_un_color_muestra_al_cliente_y_las_ordenes(app, fake_db):
    c = _login(app, fake_db)
    pedidos = [{"numero": "PDCL-29712", "cliente": "MALDONADO ANA KARINA",
                "codigo_cliente": "KAM", "fecha": "2026-08-05",
                "cantidad": 9, "unidad": 51},
               {"numero": "PDCL-30117", "cliente": "MALDONADO ANA KARINA",
                "codigo_cliente": "KAM", "fecha": "2026-08-14",
                "cantidad": 9, "unidad": 51}]

    def fake(_db, sql, **_kw):
        if "orden_fabricacion" in sql and "saldos_comprometidos" not in sql:
            return [], True
        if "JOIN producto pr ON pr.id_producto = v.id_producto" in sql:
            return pedidos, True
        return _FILAS_PANTALLA, True

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        r = c.get("/pedidos/color/FE96CAF")

    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "MALDONADO ANA KARINA" in body and "KAM" in body
    assert "PDCL-29712" in body
    assert "pidió este color más de una vez" in body     # el aviso de repetido
    assert "Ninguna orden de fabricación lanzada" in body


def test_un_color_sin_pedidos_no_da_404(app, fake_db):
    """Se puede llegar por URL a cualquier código: la ficha se muestra vacía."""
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], True)):
        r = c.get("/pedidos/color/NOEXISTE")
    assert r.status_code == 200
    assert "no tiene pedidos pendientes" in r.get_data(as_text=True)


def test_el_link_del_menu_apunta_a_una_pantalla_que_existe(app, fake_db):
    """Los links del sidebar son strings: un endpoint mal escrito revienta el
    render de CUALQUIER pantalla, no sólo la de pedidos."""
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], True)):
        r = c.get("/pedidos")
    assert 'href="/pedidos"' in r.get_data(as_text=True)
