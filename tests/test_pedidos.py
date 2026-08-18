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
    filas = [service._fila(_fila(codigo="A", ped_kg=100, inv_kg=0)),
             service._fila(_fila(codigo="B", ped_kg=900, inv_kg=0))]
    (tela,) = service.por_tela(filas, "Fleece")
    assert [f["codigo"] for f in tela["filas"]] == ["B", "A"]


def test_por_defecto_los_colores_cubiertos_TAMBIEN_se_listan():
    """⚠ Esconder los "ok" se probó el 17/08 y la dueña lo revirtió el mismo
    día: "tenés que mostrar todos los pedidos, no escondas los ok". La pantalla
    es el estado de TODO lo pedido, no una lista de tareas."""
    filas = [service._fila(_fila(codigo="FALTA", ped_kg=900, inv_kg=0)),
             service._fila(_fila(codigo="CUBIERTO", ped_kg=10, inv_kg=900))]
    (tela,) = service.por_tela(filas, "Fleece")
    assert [f["codigo"] for f in tela["filas"]] == ["FALTA", "CUBIERTO"]


def test_filtrar_es_una_accion_explicita_del_usuario():
    filas = [service._fila(_fila(codigo="FALTA", ped_kg=900, inv_kg=0)),
             service._fila(_fila(codigo="CUBIERTO", ped_kg=10, inv_kg=900))]
    (tela,) = service.por_tela(filas, "Fleece", solo_faltan=True)
    assert [f["codigo"] for f in tela["filas"]] == ["FALTA"]


def test_una_tela_entera_cubierta_igual_se_dibuja():
    filas = [service._fila(_fila(tela="Fleece 102", ped_kg=900, inv_kg=0)),
             service._fila(_fila(tela="Fleece 2.2", codigo="X", ped_kg=10,
                                 inv_kg=900))]
    assert sorted(t["tela"] for t in service.por_tela(filas, "Fleece")) == [
        "Fleece 102", "Fleece 2.2"]
    assert service.telas_sin_faltante(filas, "Fleece") == ["Fleece 2.2"]


def test_una_tela_con_faltante_no_entra_en_las_cubiertas():
    """Aunque tenga colores cubiertos: alcanza con que UNO falte."""
    filas = [service._fila(_fila(ped_kg=900, inv_kg=0)),
             service._fila(_fila(codigo="OTRO", ped_kg=10, inv_kg=900))]
    assert service.telas_sin_faltante(filas, "Fleece") == []


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
        r = c.get("/pedidos?corte=tela")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Jersey 3.2" in body
    assert "JE32AZS" in body
    assert "Fleece 96 Perchado" not in body      # es otra pestaña


def test_se_puede_elegir_la_pestana(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS_PANTALLA, True)):
        r = c.get("/pedidos?corte=tela&cat=Fleece")
    body = r.get_data(as_text=True)
    assert "FE96CAF" in body and "JE32AZS" not in body
    assert "19" in body                         # el faltante, en rollos
    assert "ok" in body                           # el NEG que sobra


def test_una_pestana_inventada_cae_a_la_primera_y_no_rompe(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS_PANTALLA, True)):
        r = c.get("/pedidos?corte=tela&cat=Terciopelo")
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


def test_el_color_sale_del_ultimo_token_y_no_de_restarle_el_nombre_de_la_tela():
    """La etiqueta de la subcategoría y la del producto NO siempre coinciden.

    `JE23RML` se llama "Jersey 1.2x2.3 RML" y su subcategoría es "Jersey 2.3":
    restarle el nombre de la tela no matcheaba nada y el color salía siendo el
    nombre entero. `JE30CIR` es "Jersey 3.0 CIR" contra "Jersey 3": matcheaba
    de más y salía ".0 CIR". Las dos se vieron en producción el 17/08.
    """
    sql = service._sql_pendientes()
    assert service.SQL_COLOR in sql
    assert "REPLACE(ISNULL(pr.nombre" not in sql


def test_una_familia_con_un_solo_color_no_dice_1_colores(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([_fila()], True)):
        body = " ".join(c.get("/pedidos?corte=tela").get_data(as_text=True).split())
    assert "1 color ·" in body      # el encabezado de la tela
    assert "en 1 color" in body     # la tarjeta del faltante
    assert "1 colores" not in body


def test_la_pantalla_arranca_mostrando_TODOS_con_su_ok(app, fake_db):
    """El "ok" de un color cubierto es información, no ruido (dueña 17/08)."""
    c = _login(app, fake_db)
    filas = [_fila(codigo="FE96CAF"),
             _fila(codigo="FE96NEG", ped_kg=10.0, inv_kg=9000.0)]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(filas, True)):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    assert "FE96CAF" in body and "FE96NEG" in body
    assert ">ok<" in body.replace("\n", "").replace(" ", "")
    assert "Ver sólo lo que falta" in body


def test_con_falta_1_el_usuario_puede_filtrar(app, fake_db):
    c = _login(app, fake_db)
    filas = [_fila(codigo="FE96CAF"),
             _fila(codigo="FE96NEG", ped_kg=10.0, inv_kg=9000.0)]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(filas, True)):
        body = c.get("/pedidos?corte=tela&falta=1").get_data(as_text=True)
    assert "FE96CAF" in body and "FE96NEG" not in body
    assert "Ver todos los pedidos" in body


def test_bodega_y_tinturandose_son_dos_columnas_con_el_nombre_entero(app, fake_db):
    """Juntarlas en una sola "Tenemos" con la parte tinturándose abreviada
    ("717 tint. 466") le hizo preguntar a la dueña qué era el "tint" — y encima
    no se sabía si el 466 estaba adentro del 717 o aparte."""
    c = _login(app, fake_db)
    filas = [_fila(inv_kg=169.0, prod_kg=18.0, n_ordenes=1)]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(filas, True)):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    assert "En bodega" in body and "Tinturándose" in body
    assert "tint." not in body
    assert "Tenemos" not in body


# ── el conversor kg ↔ rollos / unidades ─────────────────────────────────────

def test_en_rollos_el_pedido_usa_el_numero_original_y_no_la_ida_y_vuelta():
    """⭐ Reconvertir arrastra el redondeo: 212 unidades de C34BLA volvían como
    213,3 porque sus 6,36 kg se habían guardado como 6,4."""
    f = service._fila(_fila(categoria="Cuellos", codigo="C34BLA", ped_kg=0,
                            ped_rollos=0, ped_un=212, un_por_kg=33.33333333))
    (out,) = service.en_unidad([f], "alt")
    assert out["pedido_d"] == 212
    assert out["u"] == "un"


def test_en_rollos_el_pedido_sale_del_rollo_original():
    (out,) = service.en_unidad([service._fila(_fila())], "alt")
    assert out["pedido_d"] == 19.0
    assert out["u"] == "roll"


def test_bodega_y_produccion_si_se_convierten_porque_solo_existen_en_kilos():
    f = service._fila(_fila(inv_kg=470.0, prod_kg=235.0))
    (out,) = service.en_unidad([f], "alt")
    assert out["inventario_d"] == 20.0        # 470 / 23,5
    assert out["produccion_d"] == 10.0


def test_los_rollos_son_enteros_y_un_positivo_nunca_es_cero():
    """Dueña 2026-08-18: no puede haber medios rollos. 12 kg no es "0 rollos"
    (sería mentira) ni "0,5": sube a 1. Medio cuello tampoco existe."""
    (tela,) = service.en_unidad([service._fila(_fila(inv_kg=12.0))], "alt")
    assert tela["inventario_d"] == 1 and tela["decimales"] == 0   # 12/23,5 sube a 1
    (cuello,) = service.en_unidad(
        [service._fila(_fila(categoria="Cuellos", ped_un=150,
                             un_por_kg=33.33333333, inv_kg=0.7))], "alt")
    assert cuello["inventario_d"] == 23 and cuello["decimales"] == 0


def test_en_kg_no_convierte_nada():
    (out,) = service.en_unidad([service._fila(_fila(inv_kg=470.0))], "kg")
    assert (out["pedido_d"], out["inventario_d"], out["u"]) == (447, 470.0, "kg")


def test_un_cuello_sin_factor_cargado_se_muestra_en_kilos_y_no_inventa():
    f = service._fila(_fila(categoria="Cuellos", ped_kg=0, ped_un=150,
                            un_por_kg=None, inv_kg=10.0))
    (out,) = service.en_unidad([f], "alt")
    assert out["u"] == "kg"
    assert out["inventario_d"] == 10


def test_el_subtotal_de_una_tela_tambien_se_convierte():
    assert service.total_en_unidad(470.0, "alt", False) == (20, 0, "roll")
    assert service.total_en_unidad(470.0, "kg", False) == (470, 0, "kg")
    assert service.total_en_unidad(15.0, "alt", True, 33.33333333) == (500, 0, "un")
    assert service.total_en_unidad(15.0, "alt", True, 0) == (15, 0, "kg")


def test_no_queda_un_solo_kilo_en_la_pantalla(app, fake_db):
    """⚠ Dueña 17/08: "todo rollos, no pongamos kg". Hubo un toggle kg/rollos
    unas horas y lo sacó. Los kilos se siguen calculando —son la única forma de
    restar bodega y producción contra el pedido— pero no se muestran."""
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([_fila()], True)):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    tabla = body[body.index('class="pd"'):]
    assert "en rollos" in tabla          # la unidad se dice UNA vez, en la tela
    assert " kg" not in tabla.replace("kg cada uno", "").replace("en kilos", "")
    assert "19" in tabla


def test_en_cuellos_todo_se_lee_en_unidades(app, fake_db):
    c = _login(app, fake_db)
    filas = [_fila(categoria="Cuellos", tela="Cuellos T34", codigo="C34ARV",
                   ped_kg=4.5, ped_rollos=0, ped_un=150, un_por_kg=33.33333333)]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(filas, True)):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    assert "en unidades" in body and "en rollos" not in body
    assert "150" in body


def test_la_flechita_despliega_el_pedido_y_el_cliente_sin_cambiar_de_pagina(app, fake_db):
    """Dueña 17/08: "en vez de ir a otra página, una flechita para abajo para
    que muestre el número de pedido y cliente"."""
    c = _login(app, fake_db)
    peds = [{"codigo": "FE96CAF", "numero": "PDCL-29712",
             "cliente": "MALDONADO ANA KARINA", "codigo_cliente": "KAM",
             "fecha": "2026-08-05", "cantidad": 9, "unidad": 51}]

    def fake(_db, sql, **_kw):
        if "ORDER BY pr.codigo, v.fecha" in sql:
            return peds, True
        return [_fila()], True

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)

    assert 'data-fila="d-FE96CAF"' in body
    assert 'data-grupo="d-FE96CAF"' in body
    assert "PDCL-29712" in body and ">KAM<" in body
    # el nombre completo no se imprime, pero queda al pasar el mouse
    assert 'title="MALDONADO ANA KARINA"' in body


def test_un_color_sin_pedidos_en_el_desplegable_lo_dice(app, fake_db):
    c = _login(app, fake_db)

    def fake(_db, sql, **_kw):
        if "ORDER BY pr.codigo, v.fecha" in sql:
            return [], True
        return [_fila()], True

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)
    assert "no tiene pedidos cargados" in body


def test_si_asinfo_no_contesta_los_desplegables_quedan_vacios_sin_romper():
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        assert service.pedidos_por_color() == {}


def test_los_pedidos_se_agrupan_por_codigo_de_producto():
    filas = [{"codigo": "FE96CAF", "numero": "A", "cliente": "X",
              "codigo_cliente": "K", "fecha": "2026-08-05", "cantidad": 9,
              "unidad": 51},
             {"codigo": "FE96CAF", "numero": "B", "cliente": "Y",
              "codigo_cliente": "L", "fecha": "2026-08-14", "cantidad": 9,
              "unidad": 51},
             {"codigo": "FE96NEG", "numero": "C", "cliente": "Z",
              "codigo_cliente": "M", "fecha": "2026-08-12", "cantidad": 1,
              "unidad": 51}]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(filas, True)):
        out = service.pedidos_por_color()
    assert [p["numero"] for p in out["FE96CAF"]] == ["A", "B"]
    assert len(out["FE96NEG"]) == 1


def test_el_desplegable_usa_las_columnas_de_la_tabla_de_arriba(app, fake_db):
    """Dueña 17/08: "que esté encolumnado bien, fecha debajo de desde, pedido
    rollo debajo de pedido. El diseño es importante". Por eso el detalle son
    filas de LA MISMA tabla y no una tabla anidada con su propio ancho."""
    c = _login(app, fake_db)
    peds = [{"codigo": "FE96CAF", "numero": "PDCL-29712",
             "cliente": "MALDONADO MALDONADO ANA KARINA", "codigo_cliente": "KAM",
             "fecha": "2026-08-05", "cantidad": 9, "unidad": 51}]

    def fake(_db, sql, **_kw):
        return (peds, True) if "ORDER BY pr.codigo, v.fecha" in sql else ([_fila()], True)

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        body = c.get("/pedidos?corte=tela").get_data(as_text=True)

    assert "<table class=\"sub\"" not in body        # nada de tablas anidadas
    fila = body[body.index('data-grupo="d-FE96CAF"'):]
    fila = fila[:fila.index("</tr>")]
    assert fila.count("<td") == 4                   # 3 columnas + un colspan de 3
    assert 'colspan="3"' in fila
    # un solo renglón: va el CÓDIGO del cliente, no el nombre fiscal entero
    assert ">KAM<" in fila
    assert "MALDONADO MALDONADO ANA KARINA<" not in fila
    assert 'title="MALDONADO MALDONADO ANA KARINA"' in fila


def test_el_link_dice_a_donde_lleva(app, fake_db):
    """"Ver la ficha completa" no decía nada. Ahora nombra lo que hay del otro
    lado, y sólo aparece si de verdad hay órdenes que mirar."""
    c = _login(app, fake_db)

    def fake(_db, sql, **_kw):
        return ([], True) if "ORDER BY pr.codigo, v.fecha" in sql else (filas, True)

    filas = [_fila(prod_kg=490.0, n_ordenes=2)]
    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        con = c.get("/pedidos?corte=tela").get_data(as_text=True)
    filas = [_fila(prod_kg=0.0, n_ordenes=0)]
    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        sin = c.get("/pedidos?corte=tela").get_data(as_text=True)

    assert "Ver las 2 órdenes de tinturado" in " ".join(con.split())
    assert "ficha completa" not in con
    assert "tinturado de este color" not in sin


# ── cortes nuevos: color, cliente, categoría (2026-08-18) ────────────────────

def test_la_pantalla_abre_por_defecto_en_el_corte_color(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS_PANTALLA, True)):
        body = c.get("/pedidos").get_data(as_text=True)
    assert 'class="ver"' in body               # el selector de cortes
    assert 'class="ccode"' in body             # la fila-cabecera de un color
    assert ">CAF<" in body or "CAF</span>" in body


def test_por_color_agrupa_por_codigo_y_ordena_por_lo_que_mas_falta():
    filas = [service._fila(_fila(color="CAF")),                         # falta 447
             service._fila(_fila(codigo="FE96NEG", color="NEG",
                                 ped_kg=10.0, ped_rollos=1.0, inv_kg=9000.0))]  # ok
    gs = service.por_color(filas)
    assert [g["codigo"] for g in gs][0] == "CAF"       # lo que falta, arriba
    assert gs[0]["u"] == "roll"
    assert gs[0]["pedido"] == 19                        # rollos ENTEROS


def test_el_corte_color_pasa_el_pedido_de_rib_de_kilos_a_rollos():
    """Rib se pide en kilos (no en rollos). Igual se muestra en rollos, o
    mostraría 0."""
    (g,) = service.por_color([service._fila(_fila(
        categoria="Rib", tela="Rib Acanalado", codigo="RIBACE", color="ACE",
        ped_kg=70.5, ped_rollos=0.0))])
    assert g["u"] == "roll" and g["pedido"] == 3       # 70,5 / 23,5


def test_por_cliente_suma_por_cliente_y_ordena_de_mayor_a_menor():
    rows = [{"codigo": "FE96CAF", "numero": "P1", "cliente": "Empresa A",
             "codigo_cliente": "AAM", "fecha": "2026-08-01", "cantidad": 10, "unidad": 51},
            {"codigo": "JE32AZS", "numero": "P2", "cliente": "Empresa B",
             "codigo_cliente": "ATR", "fecha": "2026-08-02", "cantidad": 5, "unidad": 51},
            {"codigo": "FE96NEG", "numero": "P3", "cliente": "Empresa A",
             "codigo_cliente": "AAM", "fecha": "2026-08-03", "cantidad": 8, "unidad": 51}]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(rows, True)):
        out = service.por_cliente()
    assert out[0]["cliente"] == "AAM" and out[0]["pedido"] == 18.0
    assert out[1]["cliente"] == "ATR" and out[1]["pedido"] == 5.0


def test_el_corte_cliente_avisa_que_mezcla_unidades(app, fake_db):
    c = _login(app, fake_db)
    rows = [{"codigo": "FE96CAF", "numero": "P1", "cliente": "Empresa A",
             "codigo_cliente": "AAM", "fecha": "2026-08-01", "cantidad": 10, "unidad": 51}]

    def fake(_db, sql, **_kw):
        return (rows, True) if "ORDER BY pr.codigo, v.fecha" in sql else (_FILAS_PANTALLA, True)

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        body = c.get("/pedidos?corte=cliente").get_data(as_text=True)
    assert "mezcla unidades" in body
    assert ">AAM<" in body


def test_el_corte_categoria_muestra_cada_familia_en_su_unidad(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=(_FILAS_PANTALLA, True)):
        body = c.get("/pedidos?corte=categoria").get_data(as_text=True)
    assert "Fleece" in body and "Jersey" in body
    assert "No se suman entre sí" in body


def test_nombres_color_es_fail_soft_sin_catalogo():
    """Sin catálogo (o si la consulta falla) el color va por su código pelado,
    la pantalla no se cae."""
    # sin app/DB real la lectura falla y devuelve {} — nunca levanta.
    assert service.nombres_color() == {}


def test_el_corte_color_trae_el_desde_del_pedido_mas_viejo():
    """Como el corte por tela, el color muestra "Desde": el pedido que más
    espera entre sus telas."""
    filas = [service._fila(_fila(color="CAF", codigo="FE96CAF", mas_viejo="2026-08-14")),
             service._fila(_fila(color="CAF", codigo="FE96X", tela="Otra", mas_viejo="2026-08-01"))]
    (g,) = service.por_color(filas)
    assert g["mas_viejo_es"] == "1 ago"      # el más viejo, no el más nuevo
    assert g["dias_espera"] >= 1


def test_el_desplegable_del_corte_color_muestra_cliente_y_fecha(app, fake_db):
    """En el corte Color, al abrir una tela se ve QUIÉN pidió y CUÁNDO, no sólo
    el código y la cantidad."""
    c = _login(app, fake_db)
    peds = [{"codigo": "FE96CAF", "numero": "PDCL-29712", "cliente": "MALDONADO ANA",
             "codigo_cliente": "KAM", "fecha": "2026-08-05", "cantidad": 9, "unidad": 51}]

    def fake(_db, sql, **_kw):
        return (peds, True) if "ORDER BY pr.codigo, v.fecha" in sql else ([_fila()], True)

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        body = c.get("/pedidos").get_data(as_text=True)
    assert ">KAM<" in body
    assert "5 ago" in body               # la fecha del pedido
    assert "PDCL-29712" in body          # y el número


def test_marcar_acabado_pega_el_acabado_por_codigo_de_producto():
    filas = [service._fila(_fila(codigo="FE96CAF"))]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([{"codigo": "FE96CAF", "acabado": "TUB"}], True)):
        service.marcar_acabado(filas)
    assert filas[0]["acabado"] == "TUB"


def test_si_asinfo_no_da_el_acabado_la_pantalla_sigue_sin_punto():
    filas = [service._fila(_fila(codigo="FE96CAF"))]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        service.marcar_acabado(filas)
    assert filas[0]["acabado"] == ""


def test_el_corte_color_muestra_el_punto_de_acabado_tub_abi(app, fake_db):
    c = _login(app, fake_db)

    def fake(_db, sql, **_kw):
        if "saldo_producto_lote" in sql and "id_atributo = 1" in sql:
            return ([{"codigo": "FE96CAF", "acabado": "TUB"}], True)
        if "ORDER BY pr.codigo, v.fecha" in sql:
            return ([], True)
        return ([_fila()], True)

    with patch.object(service.metabase_client, "fetch_dataset_estado", side_effect=fake):
        body = c.get("/pedidos").get_data(as_text=True)
    assert 'class="dot tub"' in body       # el punto tubular en la fila
    assert "tubular" in body and "abierto" in body   # la leyenda
