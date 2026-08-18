"""Inventario rotativo — service y pantalla.

Los fakes devuelven filas con la forma de la FUENTE (los nombres de columna que
escriben las dos SQL contra Asinfo), no la del helper: un fake con la forma
equivocada pasa en verde mientras producción no resuelve nada.
"""
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.inventario_rotativo import service, views


def _ficha(**kw):
    """Fila cruda de `_SQL_FICHA`, con los defaults de una tela normal."""
    base = {
        "id_producto": 1, "categoria": "Fleece", "tela": "Fleece 102",
        "codigo": "FE102JOS", "color": "JOS", "semanas_venta": 49,
        "kg52": 5200.0, "inv_kg": 0.0, "ped_kg": 0.0, "ped_un": 0.0,
        "prod_kg": 0.0, "n_ordenes": 0,
    }
    base.update(kw)
    return base


def _serie(id_producto=1, kilos=None, ultima=1389):
    """Filas crudas de `_SQL_SERIE`: sólo las semanas CON venta."""
    kilos = kilos if kilos is not None else [100.0] * 52
    return [
        {"id_producto": id_producto, "w": ultima - len(kilos) + 1 + i, "kg": kg}
        for i, kg in enumerate(kilos)
        if kg
    ]


# ── el percentil ────────────────────────────────────────────────────────────

def test_el_percentil_interpola_entre_los_dos_valores_que_lo_rodean():
    assert service.percentil([0.0, 10.0], 0.90) == pytest.approx(9.0)


def test_el_percentil_de_una_lista_vacia_es_cero_y_no_revienta():
    assert service.percentil([], 0.90) == 0.0


def test_las_semanas_sin_venta_entran_en_la_grilla_con_cero():
    """El percentil se calcula sobre las 52 semanas, no sobre las que vendieron.

    Si se calculara sólo sobre las semanas buenas saldría inflado y la pantalla
    mandaría a teñir de más. El caso extremo lo muestra sin ambigüedad: un
    producto que vendió 5.200 kg en UNA sola semana del año tiene un punto de
    reposición de cero —no vende todas las semanas—, no de 5.200.
    """
    f = service._fila(_ficha(), {1389: 5200.0}, 1389)
    assert f["punto_kg"] == 0.0


def test_el_promedio_semanal_divide_por_las_13_semanas_y_no_por_las_vendidas():
    """Vender 1.300 kg en dos semanas de trece es un promedio de 100, no de 650.

    Dividir por las semanas con venta daría el tamaño del pedido típico, que es
    otra cosa, y sobreestimaría lo que hay que tener en bodega.
    """
    f = service._fila(_ficha(), {1388: 650.0, 1389: 650.0}, 1389)
    assert f["sem_kg"] == pytest.approx(100.0)


def test_el_punto_de_reposicion_cubre_dos_semanas_y_no_una():
    """Es la demanda ACUMULADA en el ciclo de tintura, no la de una semana."""
    f = service._fila(_ficha(), {w: 100.0 for w in range(1338, 1390)}, 1389)
    assert f["punto_kg"] == pytest.approx(200.0)


def test_una_semana_de_devoluciones_no_cuenta_como_demanda_negativa():
    """El neto negativo se trunca para el percentil (no existe demanda de -50)
    pero SIGUE restando en el promedio: eso sí se vendió de menos."""
    semanas = {w: 100.0 for w in range(1338, 1390)}
    semanas[1389] = -50.0
    f = service._fila(_ficha(), semanas, 1389)
    assert f["punto_kg"] > 0
    assert f["sem_kg"] < 100.0


# ── unidades ────────────────────────────────────────────────────────────────

def test_las_telas_se_leen_en_rollos_enteros():
    assert service.unidad_de("Fleece") == "roll"
    assert service.a_unidad(235.0, "roll", "Fleece") == 10.0


def test_medio_rollo_nunca_se_muestra_como_cero():
    """Redondear 8 kg a 0 rollos diría que no falta nada cuando falta."""
    assert service.a_unidad(8.0, "roll", "Fleece") == 1.0
    assert service.a_unidad(0.0, "roll", "Fleece") == 0.0


def test_el_rib_va_en_kilos_y_los_cuellos_en_unidades():
    """Dueña 2026-08-18: "se mide todo en rollos salvo rib, cuellos y puños"."""
    assert service.unidad_de("Rib") == "kg"
    assert service.a_unidad(403.9, "kg", "Rib") == 403.9
    assert service.unidad_de("Cuellos") == "un"
    assert service.a_unidad(15.0, "un", "Cuellos") == 500.0


# ── el semáforo ─────────────────────────────────────────────────────────────

def test_el_semaforo_corta_en_el_tiempo_que_tarda_una_tintura():
    assert service.estado(1.9) == "rojo"
    assert service.estado(2.5) == "ambar"
    assert service.estado(6.0) == "ok"
    assert service.estado(20.0) == "sobra"


def test_lo_que_esta_en_produccion_cuenta_para_la_cobertura():
    """Si ya hay 300 kg tinturándose, no hay que volver a pedirlos."""
    semanas = {w: 100.0 for w in range(1338, 1390)}
    sin = service._fila(_ficha(inv_kg=100.0), semanas, 1389)
    con = service._fila(_ficha(inv_kg=100.0, prod_kg=300.0), semanas, 1389)
    assert sin["estado"] == "rojo"
    assert con["alcanza"] > sin["alcanza"]
    assert con["falta_kg"] < sin["falta_kg"]


def test_sin_venta_reciente_no_se_pinta_de_rojo():
    """Rota en el año pero se apagó hace 13 semanas: no es "falta", es "no sé".

    Marcarlo rojo mandaría a teñir algo que nadie está comprando.
    """
    viejas = {w: 100.0 for w in range(1338, 1377)}
    f = service._fila(_ficha(semanas_venta=40), viejas, 1389)
    assert f["alcanza"] == -1.0
    assert f["estado"] == "ok"


# ── agrupado ────────────────────────────────────────────────────────────────

def _filas_2():
    return [
        service._fila(_ficha(color="JOS", tela="Fleece 102"),
                      {w: 100.0 for w in range(1338, 1390)}, 1389),
        service._fila(_ficha(id_producto=2, color="JOS", tela="Pique Especial",
                             inv_kg=9000.0),
                      {w: 100.0 for w in range(1338, 1390)}, 1389),
    ]


def test_agrupar_por_color_junta_las_telas_de_ese_color():
    (b,) = service.agrupar(_filas_2(), "color")
    assert b["nombre"] == "JOS" and b["n"] == 2
    assert b["etiqueta"] == "tela"


def test_el_bloque_con_mas_faltante_va_primero():
    filas = _filas_2()
    bloques = service.agrupar(filas, "tela")
    assert bloques[0]["nombre"] == "Fleece 102"      # el que no tiene stock
    assert bloques[0]["falta_kg"] > bloques[1]["falta_kg"]


def test_los_subtotales_del_bloque_van_en_kilos():
    """Un color agrupa telas de distinta unidad: sumar rollos con unidades da
    un número que no significa nada."""
    filas = [
        service._fila(_ficha(color="BLA", tela="Fleece 102"),
                      {w: 100.0 for w in range(1338, 1390)}, 1389),
        service._fila(_ficha(id_producto=2, categoria="Cuellos", color="BLA",
                             tela="Cuellos T40"),
                      {w: 10.0 for w in range(1338, 1390)}, 1389),
    ]
    (b,) = service.agrupar(filas, "color")
    assert b["sem_kg"] == pytest.approx(110.0)


# ── la consulta ─────────────────────────────────────────────────────────────

def test_asinfo_caido_no_es_lo_mismo_que_no_falta_nada():
    service._cache.clear()
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        filas, disponible = service.rotativo(_ahora=lambda: 0.0)
    assert filas == [] and disponible is False


def test_solo_entran_las_familias_de_tela_vendible():
    """TELA CRUDA, HILO y los insumos no se stockean contra promedio."""
    service._cache.clear()
    fichas = [_ficha(), _ficha(id_producto=2, categoria="HILO", tela="Hilado")]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=[(fichas, True), (_serie() + _serie(2), True)]):
        filas, _ = service.rotativo(_ahora=lambda: 0.0)
    assert [f["familia"] for f in filas] == ["Fleece"]


def test_la_sql_pide_solo_los_productos_que_rotan():
    assert f"HAVING COUNT(*) >= {service.SEMANAS_MINIMAS}" in service._sql_ficha()


def test_la_sql_de_produccion_deja_afuera_las_ordenes_padre():
    """Las órdenes viven en dos capas y `SUM` sobre la tabla cruda las cuenta
    dos veces. Misma trampa que /pedidos."""
    sql = service._sql_ficha()
    assert "h.p IS NULL" in sql
    assert "o.id_producto IS NOT NULL" in sql
    assert "o.estado_produccion = 2" in sql


def test_la_sql_usa_la_hora_de_ecuador_y_no_getdate_pelado():
    """En Asinfo `GETDATE()` devuelve UTC: pelado corre la ventana un día."""
    for sql in (service._sql_ficha(), service._sql_serie()):
        assert "DATEADD(hour, -5, GETDATE())" in sql


# ── la pantalla ─────────────────────────────────────────────────────────────

def _login(app, fake_db):
    rid = fake_db.add_role("Tester", ["stock.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _fake_asinfo(fichas=None, serie=None):
    fichas = fichas if fichas is not None else [_ficha()]
    serie = serie if serie is not None else _serie()

    def fake(_db, sql, **_kw):
        return (serie, True) if "SELECT v.id_producto, v.w" in sql else (fichas, True)

    return fake


def test_la_pantalla_abre_por_color(app, fake_db):
    """Dueña 2026-08-18: "que color sea lo primero"."""
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        body = c.get("/inventario-rotativo").get_data(as_text=True)
    assert "Inventario rotativo" in body
    assert "JOS" in body
    # el encabezado de la tabla dice Tela: las filas de un color SON telas
    assert "<th>Tela</th>" in body


def test_el_corte_por_tela_da_vuelta_el_agrupado(app, fake_db):
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        body = c.get("/inventario-rotativo?ver=tela").get_data(as_text=True)
    assert "<th>Color</th>" in body


def test_un_corte_que_no_existe_cae_en_color(app, fake_db):
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        body = c.get("/inventario-rotativo?ver=cualquiera").get_data(as_text=True)
    assert "<th>Tela</th>" in body


def test_con_asinfo_caido_la_pantalla_lo_dice_en_vez_de_mentir(app, fake_db):
    """"No pude preguntar" no es "no falta nada"."""
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        body = c.get("/inventario-rotativo").get_data(as_text=True)
    assert "No pude consultar Asinfo" in body


def test_sin_el_permiso_de_stock_la_pantalla_da_404(app, fake_db):
    rid = fake_db.add_role("Otro", ["facturas.ver"])
    uid = fake_db.add_user("otro", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    assert c.get("/inventario-rotativo").status_code == 404


def test_el_faltante_se_muestra_del_mismo_color_que_alcanza(app, fake_db):
    """Dueña 2026-08-18: "mantengamos naranja y naranja, rojo y rojo".

    Dos rojos distintos en la misma fila se leen como dos alarmas donde hay
    una sola.
    """
    service._cache.clear()
    c = _login(app, fake_db)
    # 250 kg de stock contra 100 kg/semana: 2,5 semanas → ámbar, y como el
    # punto son 200 no falta nada... le bajamos el stock para que falte.
    fichas = [_ficha(inv_kg=250.0)]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo(fichas, _serie(kilos=[100.0] * 52))):
        body = c.get("/inventario-rotativo").get_data(as_text=True)
    assert 'class="ambar"' in body
    assert 'class="rojo"' not in body.split("<table>")[1]


# ── imprimir y Excel ────────────────────────────────────────────────────────

def test_la_hoja_impresa_es_la_misma_pantalla(app, fake_db):
    """Dueña 2026-08-18: "que se pueda imprimir también así".

    Sale de la misma plantilla con `?imprimir=1`: si fueran dos, lo que se
    lleva a planta se iría separando de lo que se mira en la oficina.
    """
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        body = c.get("/inventario-rotativo?imprimir=1").get_data(as_text=True)
    assert 'class="iv hoja"' in body
    assert "JOS" in body                      # los datos siguen estando
    assert "Fuente: Asinfo" in body           # y el pie con la fecha


def test_la_hoja_esconde_los_filtros_y_fija_el_ancho_de_la_a4():
    """Sin ancho fijo la tabla sale del papel; con `@media print` sólo no se
    puede verificar la hoja sin abrir el diálogo de impresión."""
    tpl = (Path(service.__file__).parent
           / "templates" / "inventario_rotativo" / "lista.html").read_text(encoding="utf-8")
    assert ".iv.hoja{width:816px" in tpl
    assert ".iv.hoja .barra,.iv.hoja .tabs" in tpl


def test_el_excel_trae_una_fila_por_producto_con_encabezado(app, fake_db):
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        r = c.get("/inventario-rotativo/excel")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["Content-Type"]
    assert "inventario_rotativo_" in r.headers["Content-Disposition"]

    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(r.data)).active
    assert [c.value for c in ws[1]] == list(views.COLUMNAS)
    assert ws.max_row == 2
    assert ws["A2"].value == "JOS"


def test_el_excel_deja_vacia_la_cobertura_que_no_se_puede_calcular():
    """Sin venta reciente `alcanza` vale -1 adentro; en la planilla va vacío.

    Un -1 en una columna de semanas se ordena y se suma como si fuera un dato.
    """
    f = service._fila(_ficha(), {w: 100.0 for w in range(1338, 1377)}, 1389)
    assert views._valores(f)[views.COLUMNAS.index("Alcanza (sem)")] is None


def test_con_asinfo_caido_el_excel_no_baja_una_planilla_vacia(app, fake_db):
    """Bajar un archivo con el encabezado solo se lee como "no falta nada"."""
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      return_value=([], False)):
        r = c.get("/inventario-rotativo/excel")
    assert r.status_code == 302


# ── lo pedido ───────────────────────────────────────────────────────────────

def test_lo_pedido_no_se_resta_de_la_cobertura():
    """Dueña 2026-08-18: "no suma a ningún lado, pero un número informativo" —
    "todavía no se va".

    La mercadería sigue en bodega hasta que se despacha. Restarla acá
    adelantaría una salida que puede no ocurrir esta semana, y pondría en rojo
    algo que hoy está.
    """
    semanas = {w: 100.0 for w in range(1338, 1390)}
    sin = service._fila(_ficha(inv_kg=600.0), semanas, 1389)
    con = service._fila(_ficha(inv_kg=600.0, ped_kg=500.0), semanas, 1389)
    assert con["alcanza"] == sin["alcanza"]
    assert con["falta_kg"] == sin["falta_kg"]
    assert con["pedido_kg"] == 500.0


def test_los_cuellos_se_piden_por_unidad_y_el_pedido_se_pasa_a_kilos():
    """La columna tiene que ser comparable con el stock, que viene en kilos."""
    f = service._fila(
        _ficha(categoria="Cuellos", tela="Cuellos T40", ped_un=500.0),
        {w: 10.0 for w in range(1338, 1390)}, 1389)
    assert f["pedido_kg"] == pytest.approx(15.0, abs=0.1)


def test_la_columna_pedido_lleva_asterisco_y_su_nota(app, fake_db):
    """Sin la nota, una columna en el medio de la tabla se lee como que suma."""
    service._cache.clear()
    c = _login(app, fake_db)
    fichas = [_ficha(inv_kg=600.0, ped_kg=235.0)]
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo(fichas)):
        body = c.get("/inventario-rotativo").get_data(as_text=True)
    assert 'Pedido<span class="ast">*</span>' in body
    assert "no se resta de lo que hay" in body


def test_el_pedido_del_dia_usa_el_mismo_corte_de_90_dias_que_pedidos():
    """Si las dos pantallas cortan distinto, muestran números distintos del
    mismo pedido y la dueña no sabe a cuál creerle."""
    assert f"<= {service.DIAS_PEDIDO_MAX}" in service._sql_ficha()
    assert service.DIAS_PEDIDO_MAX == 90


def test_la_unidad_va_en_una_columna_y_no_pegada_a_cada_numero(app, fake_db):
    """Dueña 2026-08-18: "poner una columna de unidades y ya, como hace Asinfo,
    no repetirlo tantas veces".

    Seis sufijos por fila (roll roll roll roll) tapan los números, que son lo
    que hay que leer.
    """
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        body = c.get("/inventario-rotativo").get_data(as_text=True)
    tabla = body.split("<table>")[1].split("</table>")[0]
    assert '<td class="un">roll</td>' in tabla
    assert tabla.count("roll") == 1         # una sola vez por fila, no seis
    assert 'class="u"' not in tabla         # el sufijo por celda ya no existe


def test_el_link_del_menu_va_debajo_de_pedidos_pendientes(app, fake_db):
    """Dueña 2026-08-18: "debajo de pedidos ponelo esto".

    Y gateado por `stock.ver`, el permiso de SU pantalla: con el de la sección
    (`facturas.ver`) le aparecería el link a quien después se come un 404.
    """
    base = (Path(app.root_path) / "templates" / "base.html").read_text(encoding="utf-8")
    i_ped = base.index("pedidos.lista")
    i_inv = base.index("inventario_rotativo.lista")
    i_compras = base.index("compras.lista")
    assert i_ped < i_inv < i_compras
    linea = base[base.rindex("{%", 0, i_inv):i_inv]
    assert "stock.ver" in linea


# ── acabado ─────────────────────────────────────────────────────────────────

def test_el_acabado_sale_de_pedidos_y_no_de_una_sql_propia(app, fake_db):
    """Dueña 2026-08-18: "pone columna de acabado".

    Se importa `pedidos.service.acabados_por_producto` en vez de copiar la SQL:
    dos consultas paralelas se desincronizan sin que nadie se entere hasta que
    las dos pantallas dicen acabados distintos del mismo producto.
    """
    service._cache.clear()
    c = _login(app, fake_db)
    with patch("modules.pedidos.service.acabados_por_producto",
               return_value={"FE102JOS": "TUB"}), \
         patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        body = c.get("/inventario-rotativo").get_data(as_text=True)
    assert "<th style=\"text-align:left\">Acab.</th>" in body
    assert '<td class="ac">TUB</td>' in body


def test_sin_acabado_la_celda_queda_vacia_y_la_pantalla_no_se_cae(app, fake_db):
    """No se inventa un acabado: un producto sin lote con atributo no tiene."""
    service._cache.clear()
    c = _login(app, fake_db)
    with patch("modules.pedidos.service.acabados_por_producto",
               side_effect=RuntimeError("Asinfo se cayó")), \
         patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        body = c.get("/inventario-rotativo").get_data(as_text=True)
    assert '<td class="ac"></td>' in body
    assert "JOS" in body


# ── subtotales del bloque ───────────────────────────────────────────────────

def test_el_subtotal_va_en_rollos_cuando_todo_el_bloque_son_rollos():
    """Dueña 2026-08-18: "poneme todo en rollos salvo los cuellos, rib y puños".

    El encabezado del color era el único lugar que seguía hablando en kilos.
    """
    filas = [service._fila(_ficha(color="BLA", tela="Fleece 102"),
                           {w: 235.0 for w in range(1338, 1390)}, 1389)]
    (b,) = service.agrupar(filas, "color")
    assert (b["unidad"], b["sem"]) == ("roll", 10.0)   # 235 kg = 10 rollos


def test_un_bloque_mixto_usa_la_unidad_que_pesa_mas():
    """Dueña 2026-08-18: "o en rollo o en kg, no me conviertas ambos".

    Un color que junta Fleece con Rib se lee en la unidad que manda por peso:
    dos términos en un encabezado son dos números donde alcanza uno.
    """
    filas = [
        service._fila(_ficha(color="BLA", tela="Fleece 102"),
                      {w: 235.0 for w in range(1338, 1390)}, 1389),
        service._fila(_ficha(id_producto=2, categoria="Rib", color="BLA",
                             tela="Rib Normal"),
                      {w: 100.0 for w in range(1338, 1390)}, 1389),
    ]
    (b,) = service.agrupar(filas, "color")
    # 235 kg de Fleece contra 100 de Rib: mandan los rollos
    assert (b["unidad"], b["sem"]) == ("roll", 14.0)


def test_cuellos_y_punos_comparten_el_termino_en_unidades():
    """Los dos se leen "un", así que van en el mismo término — ya convertidos
    fila por fila con el factor de su familia (33,33 y 50 un/kg)."""
    filas = [
        service._fila(_ficha(categoria="Cuellos", color="BLA", tela="Cuellos T40"),
                      {w: 10.0 for w in range(1338, 1390)}, 1389),
        service._fila(_ficha(id_producto=2, categoria="Puños", color="BLA",
                             tela="Puños"),
                      {w: 10.0 for w in range(1338, 1390)}, 1389),
    ]
    (b,) = service.agrupar(filas, "color")
    assert b["unidad"] == "un"


# ── la hoja respeta el filtro ───────────────────────────────────────────────

def _dos_filas():
    """Un rojo (sin stock) y un tranquilo (con stock de sobra)."""
    return [_ficha(color="JOS"),
            _ficha(id_producto=2, color="NEG", codigo="FE102NEG", inv_kg=9000.0)]


def test_la_hoja_imprime_solo_lo_filtrado(app, fake_db):
    """Dueña 2026-08-18. Pedir "las que hay que teñir" imprimía las 289 —
    trece páginas para tirar."""
    service._cache.clear()
    c = _login(app, fake_db)
    serie = _serie(1) + _serie(2)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo(_dos_filas(), serie)):
        todo = c.get("/inventario-rotativo?imprimir=1").get_data(as_text=True)
        rojo = c.get("/inventario-rotativo?imprimir=1&est=rojo").get_data(as_text=True)
    assert ">JOS<" in todo and ">NEG<" in todo
    assert ">JOS<" in rojo and ">NEG<" not in rojo


def test_la_hoja_filtrada_dice_en_el_pie_qué_filtro_tiene(app, fake_db):
    """Una hoja de 62 filas de 289 tiene que decir por qué, o el que la lee
    piensa que eso es todo el inventario."""
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        body = c.get("/inventario-rotativo?imprimir=1&est=rojo&fam=Fleece").get_data(as_text=True)
    assert "sólo lo que hay que teñir" in body
    assert "Fleece" in body


def test_el_filtro_de_la_pantalla_no_recorta_lo_que_se_ve(app, fake_db):
    """Sin `imprimir=1` filtra el JS: si el servidor recortara, al apretar
    "Todo" no habría forma de volver a ver el resto."""
    service._cache.clear()
    c = _login(app, fake_db)
    serie = _serie(1) + _serie(2)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo(_dos_filas(), serie)):
        body = c.get("/inventario-rotativo?est=rojo").get_data(as_text=True)
    assert ">JOS<" in body and ">NEG<" in body


def test_un_filtro_que_no_existe_no_vacia_la_hoja(app, fake_db):
    """`?est=cualquiera` imprime todo, no una hoja en blanco."""
    service._cache.clear()
    c = _login(app, fake_db)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo()):
        body = c.get("/inventario-rotativo?imprimir=1&est=cualquiera").get_data(as_text=True)
    assert ">JOS<" in body


def test_el_buscador_de_la_hoja_mira_color_y_tela(app, fake_db):
    service._cache.clear()
    c = _login(app, fake_db)
    serie = _serie(1) + _serie(2)
    with patch.object(service.metabase_client, "fetch_dataset_estado",
                      side_effect=_fake_asinfo(_dos_filas(), serie)):
        body = c.get("/inventario-rotativo?imprimir=1&q=neg").get_data(as_text=True)
    assert ">NEG<" in body and ">JOS<" not in body
