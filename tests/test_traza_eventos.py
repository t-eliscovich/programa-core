"""`mov_doble` le pone el nombre al detalle de la traza.

TMT 2026-08-07, mirando /historial: *"podés ver de traer algunas de acá… por
ejemplo anticipos e historial me gusta"*, *"cheque depositado en banco debería
ser un movimiento"*, *"cuando hay una devolución me gustaría que también
aparezca, ejemplo la factura de Puebla de hoy"*.

La división de trabajo: `mov_doble` pone el NOMBRE y el AGRUPAMIENTO, el diff
de saldos pone la PLATA. Los importes de `mov_doble` no siempre coinciden con
el Δ del componente —una retención, un redondeo, un cheque que cubre dos
facturas—, así que el que manda para los números sigue siendo el diff.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.informes import eventos as ev  # noqa: E402
from modules.informes import traza as t  # noqa: E402


def _ev(tipo, origen, oid, destino, did, importe=0.0, batch=None, concepto="",
        metadata=None):
    return {"id_mov_doble": abs(hash((tipo, oid, did))) % 10000,
            "batch_id": batch, "tipo": tipo, "metadata": metadata or {},
            "origen_table": origen, "origen_id": oid,
            "destino_table": destino, "destino_id": did,
            "importe": importe, "concepto": concepto, "usuario": "alex",
            "estado": "activo", "dia": "2026-08-07"}


def _con_label(filas):
    with patch.object(ev.db, "fetch_all", return_value=filas):
        return ev.de_la_ventana("2026-08-07 00:00+00", "2026-08-07 23:59+00")


# ── El índice documento → hecho ─────────────────────────────────────────────

def test_un_deposito_toca_el_cheque_y_por_eso_los_une():
    evs = _con_label([_ev("cheque_depositado", "cheque", 102023, "cheque", 102023)])
    idx = ev.indice(evs)
    from modules.historial.queries import TIPOS_LABEL
    assert idx["c102023"]["label"] == TIPOS_LABEL["cheque_depositado"]


def test_las_tablas_sin_fila_propia_en_la_foto_no_entran_al_indice():
    """`transacciones_bancarias` y `compra` no tienen documento en la foto: el
    diff las ve dentro del saldo del componente, no como fila."""
    evs = _con_label([_ev("banco_de_directo", "transacciones_bancarias", 5,
                          "transacciones_bancarias", 5)])
    assert ev.indice(evs) == {}


def test_sin_ventana_no_consulta_nada():
    assert ev.de_la_ventana(None, "2026-08-07") == []


def test_si_mov_doble_no_contesta_la_pantalla_sigue():
    """Sin eventos el detalle sale con nombres más pobres, pero sale."""
    with patch.object(ev.db, "fetch_all", side_effect=RuntimeError("timeout")):
        assert ev.de_la_ventana("a", "b") == []


# ── El resumen agrupado por hecho ───────────────────────────────────────────

def test_un_cheque_depositado_es_UN_renglon_y_no_dos():
    """🚨 TMT: *"cheque depositado en banco debería ser un movimiento"*. El
    cheque que se va de cartera y la plata que aparece en el banco son el mismo
    hecho; sueltos parecen dos, y encima netean cero sin decir por qué."""
    idx = ev.indice(_con_label([
        _ev("cheque_depositado", "cheque", 102023, "cheque", 102023)]))
    movs = [
        {"doc_id": "c102023", "componente": "cheques", "aporte": -1251.87,
         "regla": "Cheque depositado o dado de baja",
         "etiqueta": "Cheque 102023 · LEG", "familia": "traspaso"},
        {"doc_id": "b10", "componente": "bancos", "aporte": 1251.87,
         "regla": "Movimiento bancario", "etiqueta": "Banco Pichincha",
         "familia": "traspaso"},
    ]
    out = t.resumir(movs, 0.0, idx)
    # 07/08: el banco YA no queda aparte. Sin `mov_doble` que los una, la
    # señal es que los importes son exactamente opuestos y tocan componentes
    # distintos — `_unir_las_dos_patas`. Un renglón, con las dos columnas.
    textos = [g["texto"] for g in out]
    assert textos == ["CH → BC · LEG"], textos
    assert out[0]["por_col"] == {"cheques": -1251.87, "bancos": 1251.87}


def test_la_devolucion_aparece_con_nombre():
    """TMT: *"cuando hay una devolución me gustaría que también aparezca,
    ejemplo la factura de Puebla de hoy"* — 07/08, factura 11611, PUE."""
    idx = ev.indice(_con_label([
        _ev("factura_devolucion", "factura", 11611, "factura", 11611,
            importe=-907.97, concepto="DEVOLUCION Factura #11611 PUE")]))
    movs = [{"doc_id": "f11611", "componente": "facturas", "aporte": -907.97,
             "regla": "Abono a factura", "etiqueta": "Factura 11611 · PUE",
             "familia": "traspaso"}]
    g = t.resumir(movs, -907.97, idx)[0]
    # 🚨 Corto y con el cliente: "barely can read cheque cancelado por anticipo"
    assert g["texto"] == "FA PUE dev"
    assert "Factura: devolución" in g["titulo"]     # el largo, en el tooltip
    assert g["aporte"] == -907.97
    assert "tipo=factura_devolucion" in g["url"]


def test_los_tres_anticipos_de_una_compra_son_un_renglon_con_el_numero():
    """TMT: *"anticipos e historial me gusta"* — el Historial los muestra como
    "3 anticipo(s) → compra N° 10130" con el link para verlos uno por uno."""
    idx = ev.indice(_con_label([
        _ev("bap_anticipo_a_compra", "dolares", 2970, "compra", 555,
            concepto="AI 21 · 3 anticipo(s) → compra N° 10130",
            metadata={"codigo_prov": "AI", "numero_compra": 10130,
                      "ids_anticipos": [2970, 2971, 2972], "n_anticipos": 3}),
    ]))
    movs = [{"doc_id": f"d{i}", "componente": "antic", "aporte": -1000.0,
             "regla": "Anticipo aplicado", "etiqueta": f"Anticipo AI · {i}",
             "familia": "traspaso"} for i in (2970, 2971, 2972)]
    out = t.resumir(movs, -3000.0, idx)
    # 🚨 Los tres caen en el MISMO hecho: el mov_doble registra uno
    # "representativo" y los otros dos viven en metadata.ids_anticipos. Sin
    # leer esa metadata, de tres anticipos uno matcheaba y dos caían aparte —
    # el mismo hecho salía en dos renglones.
    assert len(out) == 1
    # 🚨 TMT 2026-08-11: antes decía "AN AI → CP 10130 (3)" — el código del
    # proveedor SIN el número de la importación, y un número que es el de la
    # compra. *"no sé qué es CP y ese número, quiero ver el AI 16"*. El nombre
    # de la fábrica ya venía en el concepto; ahora se usa.
    assert out[0]["texto"] == "AI 21 · 3 anticipos pasaron a la compra"
    assert out[0]["aporte"] == -3000.0


def test_sin_eventos_el_resumen_sigue_agrupando_por_regla():
    """El detalle no depende de `mov_doble`: si no hay evento, cae en la regla
    de siempre."""
    movs = [{"doc_id": "f1", "componente": "facturas", "aporte": 500.0,
             "regla": "Venta facturada", "etiqueta": "Factura 1 · AAA",
             "familia": "utilidad"}]
    assert t.resumir(movs, 500.0, {})[0]["texto"] == "FA #1 AAA"


# ── 07/08: lo que la dueña pidió mirando la pantalla ────────────────────────

def test_la_factura_sale_con_su_numero_y_el_codigo_del_cliente():
    """🚨 TMT 2026-08-07: la factura es el hecho N° 1 de la traza (7.451) y
    salía como "FA KLC nueva": el cliente sí, el número no. Con cinco facturas
    en la misma ventana no hay forma de saber cuál es cuál.

    El importe NO se repite en el texto: ya está en la columna Fact.
    """
    idx = ev.indice(_con_label([
        _ev("factura_emitida", "factura", 9001, "factura", 9001,
            importe=1641.22, concepto="Factura #181301 KLC",
            metadata={"numf": "181301", "codigo_cli": "KLC"})]))
    movs = [{"doc_id": "f9001", "componente": "facturas", "aporte": 1641.22,
             "regla": "Venta facturada", "etiqueta": "Factura 181301 · KLC",
             "familia": "utilidad"}]
    g = t.resumir(movs, 1641.22, idx)[0]
    assert g["texto"] == "FA #181301 KLC"


def test_ocho_cheques_depositados_de_una_sola_ida_al_banco_son_un_renglon():
    """🚨 El depósito consolidado escribe UNA `mov_doble` por cheque y NO pasa
    `batch_id`: ocho cheques al banco salían como ocho renglones. Comparten
    `id_transaccion` — es el mismo depósito en la cuenta.

    El paréntesis cuenta CHEQUES (`n_grupo`), no documentos: en el grupo
    también está el renglón de la cuenta bancaria.
    """
    meta = {"id_transaccion": 7788, "no_banco": 10, "consolidado": True,
            "n_grupo": 8, "banco_nombre": "Pichincha"}
    filas = [_ev("cheque_depositado", "cheque", 5000 + i,
                 "transacciones_bancarias", 7788, importe=100.0,
                 concepto=f"Dep. cheque {i}", metadata=meta) for i in range(8)]
    idx = ev.indice(_con_label(filas))
    movs = [{"doc_id": f"c{5000 + i}", "componente": "cheques", "aporte": -100.0,
             "regla": "Cheque depositado o dado de baja",
             "etiqueta": f"Cheque {i} · CLI", "familia": "traspaso"}
            for i in range(8)]
    movs.append({"doc_id": "b10", "componente": "bancos", "aporte": 800.0,
                 "regla": "Movimiento bancario", "etiqueta": "Banco Pichincha",
                 "familia": "traspaso"})
    out = t.resumir(movs, 0.0, idx)
    # Un solo renglón para los nueve documentos, y aporta CERO: un depósito no
    # mueve la utilidad. 🚨 Pero se muestra igual — TMT: *"¿qué es más
    # importante acá, los 40k que movieron o hilado y terminado?"*. El lado de
    # cada componente va en SU columna: −800 en Cheq., +800 en Bancos.
    # 07/08, segunda vuelta: la regla genérica (dos hechos del mismo tipo se
    # juntan) llega antes que el caso especial del depósito y lo dice mejor —
    # la cuenta adelante y el cliente al final, como el resto de los renglones.
    assert [g["texto"] for g in out] == ["8 CH → BC · CLI"], out
    g = out[0]
    assert g["aporte"] == 0.0
    assert g["n"] == 9
    assert g["por_col"] == {"cheques": -800.0, "bancos": 800.0}
    assert g["bruto"] == 800.0


def test_el_traspaso_grande_va_primero_aunque_aporte_cero():
    """🚨 TMT 2026-08-07: *"siempre mostrar el cambio más grande primero"*.
    Ordenado por el aporte, el depósito de $41.729 valía 0 y quedaba último —
    o directamente afuera, por debajo del umbral de un peso.
    """
    meta = {"id_transaccion": 7788, "no_banco": 10, "n_grupo": 2}
    idx = ev.indice(_con_label([
        _ev("cheque_depositado", "cheque", 5000 + i, "transacciones_bancarias",
            7788, importe=20864.5, metadata=meta) for i in range(2)]))
    movs = [{"doc_id": f"c{5000 + i}", "componente": "cheques", "aporte": -20864.5,
             "regla": "Cheque depositado o dado de baja",
             "etiqueta": f"Cheque {i} · CLI", "familia": "traspaso"}
            for i in range(2)]
    movs += [
        {"doc_id": "b10", "componente": "bancos", "aporte": 41729.0,
         "regla": "Movimiento bancario", "etiqueta": "Banco PICHINCHA",
         "familia": "traspaso"},
        {"doc_id": "#vsto", "componente": "vsto", "aporte": -3304.0,
         "regla": "Stock", "etiqueta": "salió de hil. y tej. y term.",
         "familia": "utilidad"},
    ]
    out = t.resumir(movs, -3304.0, idx)
    assert out[0]["texto"] == "2 CH → BC · CLI"
    assert out[1]["texto"] == "salió de hil. y tej. y term."


def test_la_cuenta_bancaria_se_une_con_su_hecho():
    """🚨 TMT 2026-08-07: *"ahí tenemos un banco pichincha solo, no se está
    mostrando bien lo que queremos"*. La foto guarda los bancos por CUENTA, así
    que el renglón sabía el nombre del banco y nada más. El puente es el
    `no_banco` de la metadata de `mov_doble`.
    """
    idx = ev.indice(_con_label([
        _ev("nota_debito", "transacciones_bancarias", 900, "posdat", 44,
            importe=-370.0, concepto="COMISIONES E IMPUESTOS",
            metadata={"no_banco": 10, "documento": "ND"})]))
    movs = [{"doc_id": "b10", "componente": "bancos", "aporte": -370.0,
             "regla": "Movimiento bancario", "etiqueta": "Banco PICHINCHA",
             "familia": "traspaso"}]
    g = t.resumir(movs, -370.0, idx)[0]
    assert g["texto"] == "COMISIONES E IMPUESTOS"


def test_si_la_cuenta_tuvo_varios_hechos_no_se_le_atribuye_a_uno():
    """Elegir uno sería colgarle el Δ entero de la cuenta al que quedó último.
    El renglón dice cuántos fueron y no lleva link: `banco_varios` no es un
    tipo de `mov_doble` y filtrar por él daría una pantalla vacía.
    """
    idx = ev.indice(_con_label([
        _ev("banco_nd_directo", "transacciones_bancarias", 900,
            "transacciones_bancarias", 900, importe=-64.73, concepto="ND",
            metadata={"no_banco": 10}),
        _ev("banco_tr_directo", "transacciones_bancarias", 901,
            "transacciones_bancarias", 901, importe=7404.88, concepto="TR",
            metadata={"no_banco": 10}),
    ]))
    movs = [{"doc_id": "b10", "componente": "bancos", "aporte": 7340.15,
             "regla": "Movimiento bancario", "etiqueta": "Banco PICHINCHA",
             "familia": "traspaso"}]
    g = t.resumir(movs, 7340.15, idx)[0]
    # 07/08: los conceptos van en el RENGLÓN, no en el tooltip — "banco no sé
    # qué movimiento es todavía".
    assert g["texto"] == "BC · ND, TR"
    assert g["url"] is None
    assert "ND" in g["titulo"] and "TR" in g["titulo"]


def test_una_cuenta_sin_numero_de_banco_no_rompe_el_indice():
    """Metadata sin `no_banco` (o con basura): el renglón queda como estaba."""
    idx = ev.indice(_con_label([
        _ev("banco_mov_directo", "transacciones_bancarias", 900,
            "transacciones_bancarias", 900, metadata={"no_banco": "ahí"})]))
    assert idx == {}


def test_las_etapas_se_abrevian_tambien_en_las_fotos_ya_guardadas():
    """🚨 El texto del stock se escribe al SACAR la foto, no al mostrarla:
    abreviarlo sólo en `foto._texto_stock` dejaba las 200 fotos guardadas con
    "hilado → tejido y terminado", que son justo las que la dueña mira.
    """
    movs = [{"doc_id": "#vsto", "componente": "vsto", "aporte": -370.0,
             "regla": "Stock", "familia": "utilidad",
             "etiqueta": "salió de hilado y tejido y terminado"}]
    assert t.resumir(movs, -370.0, {})[0]["texto"] == "salió de hil. y tej. y term."


def test_un_cheque_aplicado_a_una_factura_no_dice_dos():
    """🚨 El paréntesis cuenta HECHOS, no documentos. Un cheque aplicado a una
    factura toca dos documentos y es UN hecho: salía "CH GSS → FA (2)", que se
    lee como dos cheques.
    """
    idx = ev.indice(_con_label([
        _ev("cheque_aplicado_a_factura", "cheque", 700, "factura", 800,
            importe=470.0)]))
    movs = [
        {"doc_id": "c700", "componente": "cheques", "aporte": 470.0,
         "regla": "Cheque recibido", "etiqueta": "Cheque 9 · GSS",
         "familia": "traspaso"},
        {"doc_id": "f800", "componente": "facturas", "aporte": -470.0,
         "regla": "Abono a factura", "etiqueta": "Factura 1 · GSS",
         "familia": "traspaso"},
    ]
    g = t.resumir(movs, 0.0, idx)[0]
    assert g["texto"] == "CH GSS → FA"
    assert g["n"] == 2                       # dos documentos…
    assert len(g["hechos"]) == 1             # …un solo hecho


def test_el_banco_cargado_a_mano_tambien_se_identifica():
    """🚨 TMT 2026-08-07, sobre "Banco PICHINCHA 759": *"fijate si
    identificás"*. Los movimientos cargados derecho por la pantalla de Bancos
    NO dejan `mov_doble`, así que `_cuentas()` no los alcanzaba y el renglón
    se quedaba con el nombre del banco. Se leen de `transacciones_bancarias`
    por `fecha_crea` — el momento en que se CARGÓ, no la fecha del movimiento.
    """
    filas = [{"no_banco": 10, "documento": "TR", "importe": 759.0,
              "concepto": "RECIBIDA DE JVL", "dia": "2026-08-07"}]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("2026-08-07 18:28+00", "2026-08-07 18:33+00")
    movs = [{"doc_id": "b10", "componente": "bancos", "aporte": 759.0,
             "regla": "Movimiento bancario", "etiqueta": "Banco PICHINCHA",
             "familia": "traspaso"}]
    g = t.resumir(movs, 759.0, ev.indice([], cuentas))[0]
    assert g["texto"] == "transferencia · RECIBIDA DE JVL"


def test_si_la_cuenta_tuvo_varias_cargas_dice_cuantas():
    filas = [{"no_banco": 10, "documento": "TR", "importe": 759.0,
              "concepto": "UNO", "dia": "2026-08-07"},
             {"no_banco": 10, "documento": "ND", "importe": -289.0,
              "concepto": "DOS", "dia": "2026-08-07"}]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    assert cuentas["b10"]["texto"] == "BC · UNO, DOS"
    assert cuentas["b10"]["concepto"] == "UNO · DOS"


def test_sin_ventana_no_se_leen_transacciones():
    assert ev.transacciones(None, "b") == {}


def test_si_transacciones_falla_el_detalle_sigue():
    with patch.object(ev.db, "fetch_all", side_effect=RuntimeError("boom")):
        assert ev.transacciones("a", "b") == {}


def test_el_hecho_con_nombre_le_gana_a_la_carga_del_banco():
    """`mov_doble` además UNE el banco con el otro lado; la transacción sola
    no sabe con qué se empareja. Si los dos conocen la cuenta, manda el hecho.
    """
    filas = [{"no_banco": 10, "documento": "ND", "importe": -370.0,
              "concepto": "TIPEADO A MANO", "dia": "2026-08-07"}]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    evs = _con_label([_ev("nota_debito", "transacciones_bancarias", 900,
                          "posdat", 44, importe=-370.0,
                          concepto="COMISIONES E IMPUESTOS",
                          metadata={"no_banco": 10})])
    assert ev.indice(evs, cuentas)["b10"]["tipo"] == "nota_debito"


def test_las_retenciones_revertidas_van_en_un_renglon():
    """🚨 TMT 2026-08-07, viendo ocho "RT TNZ ✗ FA" seguidos: *"esto también
    juntalo, retenciones es un solo movimiento"*. La DESaplicada no lleva
    `batch_id` —la aplicada sí, por eso ésa ya salía junta— así que caía de a
    una. Cuenta FACTURAS y no nombra al cliente: son todas del mismo.
    """
    filas = [_ev("retencion_asinfo_desaplicada", "factura", 8000 + i,
                 "factura", 8000 + i, importe=v)
             for i, v in enumerate([174.0, 140.0, 129.0, 78.0, 68.0, 38.0])]
    idx = ev.indice(_con_label(filas))
    movs = [{"doc_id": f"f{8000 + i}", "componente": "facturas", "aporte": v,
             "regla": "Factura corregida en más",
             "etiqueta": f"Factura {i} · TNZ", "familia": "traspaso"}
            for i, v in enumerate([174.0, 140.0, 129.0, 78.0, 68.0, 38.0])]
    g = t.resumir(movs, 627.0, idx)[0]
    assert g["texto"] == "retenciones revertidas · 6 facturas"
    assert g["aporte"] == 627.0


def test_el_deposito_cargado_en_el_banco_dice_que_es_un_deposito():
    """🚨 El concepto tipeado puede ser cualquier cosa: el depósito de un
    cheque se carga como "1 ch.KRH", que no se entiende sin saber que es un
    depósito. El código de documento va adelante, en castellano.
    """
    filas = [{"no_banco": 10, "documento": "DE", "importe": 1620.86,
              "concepto": "1 ch.KRH", "dia": "2026-08-07"}]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    assert cuentas["b10"]["texto"] == "depósito · 1 ch.KRH"


def test_el_documento_sin_hecho_se_escribe_como_los_demas():
    """"Factura 174039 · MLZ" al lado de "FA #181305 JVL" son dos formas de
    escribir lo mismo, y la larga se come el ancho de la columna."""
    movs = [{"doc_id": "f174039", "componente": "facturas", "aporte": -321.0,
             "regla": "Abono a factura", "etiqueta": "Factura 174039 · MLZ",
             "familia": "traspaso"}]
    assert t.resumir(movs, -321.0, {})[0]["texto"] == "FA #174039 MLZ"


def test_no_repite_la_palabra_si_el_concepto_ya_la_dice():
    filas = [{"no_banco": 10, "documento": "TR", "importe": 100.0,
              "concepto": "TRANSFERENCIA RECIBIDA JVL", "dia": "2026-08-07"}]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    assert cuentas["b10"]["texto"] == "TRANSFERENCIA RECIBIDA JVL"


def test_las_devoluciones_van_juntas_pero_aparte_de_las_ventas():
    """Segunda pasada 07/08: cuatro devoluciones sueltas en la misma ventana
    que trece facturas nuevas. Se juntan entre ellas, NO con las ventas: el
    signo es al revés y una devolución grande quedaría escondida en el neto.
    """
    devs = [("11614", "PUE", -900.0), ("11615", "CLR", -300.0),
            ("11616", "ERA", -200.0)]
    filas = [_ev("factura_devolucion", "factura", 9000 + i, "factura", 9000 + i,
                 importe=v, metadata={"numf": n, "codigo_cli": c})
             for i, (n, c, v) in enumerate(devs)]
    filas.append(_ev("factura_emitida", "factura", 9500, "factura", 9500,
                     importe=5000.0, metadata={"numf": "181310", "codigo_cli": "RRV"}))
    idx = ev.indice(_con_label(filas))
    movs = [{"doc_id": f"f{9000 + i}", "componente": "facturas", "aporte": v,
             "regla": "Factura corregida", "etiqueta": f"Factura {n} · {c}",
             "familia": "utilidad"} for i, (n, c, v) in enumerate(devs)]
    movs.append({"doc_id": "f9500", "componente": "facturas", "aporte": 5000.0,
                 "regla": "Venta facturada", "etiqueta": "Factura 181310 · RRV",
                 "familia": "utilidad"})
    textos = [g["texto"] for g in t.resumir(movs, 3600.0, idx)]
    assert textos == ["FA #181310 RRV", "3 FA dev · PUE, CLR, ERA"], textos


def test_tres_compras_de_una_importacion_en_un_renglon_sin_el_numero_de_una():
    """Tres "CP AQ → DE 1013x" seguidas eran tres renglones. Juntadas, el
    número de UNA de las compras al lado de "3 CP → DE" se leería como si
    fueran todas esa: no va.
    """
    filas = [_ev("compra_a_posdat", "posdat", 400 + i, "posdat", 400 + i,
                 importe=-v, metadata={"codigo_prov": "AQ",
                                       "numero_compra": 10131 + i})
             for i, v in enumerate([8546.0, 7275.0, 5025.0])]
    idx = ev.indice(_con_label(filas))
    movs = [{"doc_id": f"p{400 + i}", "componente": "totp", "aporte": -v,
             "regla": "Deuda nueva cargada", "etiqueta": f"Posdat AQ · {i}",
             "familia": "utilidad"} for i, v in enumerate([8546.0, 7275.0, 5025.0])]
    g = t.resumir(movs, -20846.0, idx)[0]
    assert g["texto"] == "3 CP → DE · AQ"
    assert g["aporte"] == -20846.0


def test_el_totalizar_se_lleva_las_facturas_que_toco():
    """🚨 El TOTALIZAR toca N facturas y `mov_doble` guarda origen = la primera
    y destino = la última: las del medio caían al camino sin nombre y salían
    como "11 facturas canceladas · MLZ" AL LADO del totalizar que las causó.
    La lista completa está en el snapshot `antes` (el que hace posible el ↺).
    """
    ids = [174039, 174040, 174041]
    idx = ev.indice(_con_label([
        _ev("totalizar_estado_cuenta", "factura", ids[0], "factura", ids[-1],
            importe=-1747.0, concepto="TOTALIZAR estado de cuenta MLZ",
            metadata={"codigo_cli": "MLZ", "n_facturas": 3,
                      "antes": [{"id": i} for i in ids]})]))
    movs = [{"doc_id": f"f{i}", "componente": "facturas", "aporte": -100.0,
             "regla": "Factura cancelada del todo",
             "etiqueta": f"Factura {i} · MLZ", "familia": "traspaso"}
            for i in ids]
    out = t.resumir(movs, -300.0, idx)
    assert len(out) == 1, [g["texto"] for g in out]
    assert out[0]["texto"] == "FA MLZ totalizar · 3 facturas"
    assert out[0]["aporte"] == -300.0


def test_el_reverso_del_totalizar_hereda_las_facturas_del_deshecho():
    """🚨 El totalizar guarda el snapshot `antes` con las N facturas; su
    REVERSO guarda sólo los contadores y un `id_mov_deshecho`. Sin resolverlo,
    el renglón decía "↩ FA MLZ totalizar · 29 facturas" y al lado seguían
    "11 facturas nuevas · MLZ" y "FA #174039 MLZ": las mismas facturas, dos
    veces, y con un nombre que además miente (ninguna se emitió).
    """
    rev = _ev("reverso_totalizar_estado_cuenta", "factura", 174039,
              "factura", 174041, importe=1747.0,
              metadata={"codigo_cli": "MLZ", "n_facturas": 3,
                        "id_mov_deshecho": 22176})
    original = {"id_mov_doble": 22176,
                "metadata": {"antes": [{"id": i} for i in
                                       (174039, 174040, 174041)]}}
    llamadas = []

    def _fake(sql, args=None, *a, **k):
        llamadas.append(sql)
        return [original] if "id_mov_doble = ANY" in sql else [rev]

    with patch.object(ev.db, "fetch_all", side_effect=_fake):
        evs = ev.de_la_ventana("2026-08-07 00:00+00", "2026-08-07 23:59+00")
    assert sorted(evs[0]["docs"]) == ["f174039", "f174040", "f174041"]


def test_sin_reverso_no_se_consulta_el_mov_deshecho():
    """Una consulta de más por ventana, en la pantalla que se abre todo el
    día, para un caso que pasa una vez por semana."""
    llamadas = []

    def _fake(sql, args=None, *a, **k):
        llamadas.append(sql)
        return [_ev("factura_emitida", "factura", 1, "factura", 1)]

    with patch.object(ev.db, "fetch_all", side_effect=_fake):
        ev.de_la_ventana("a", "b")
    assert len(llamadas) == 1


def test_aplicar_un_cheque_no_lo_saca_de_cartera():
    """🚨 TMT 2026-08-07, viendo "8 CH → FA · KRH" con −$7.000 en Cheq.:
    aplicar un cheque a una factura NO lo saca de cartera. Si Cheq. bajó es
    porque se depositó — y el depósito cargado a mano por la pantalla de
    Bancos no deja `mov_doble`, así que el único hecho pegado al documento era
    el "aplicado a factura" de cuando entró. No es un nombre pobre: es FALSO.
    Cuando el hecho contradice lo que el diff vio, gana el diff.
    """
    idx = ev.indice(_con_label([
        _ev("cheque_aplicado_a_factura", "cheque", 700, "factura", 800)]))
    movs = [{"doc_id": "c700", "componente": "cheques", "aporte": -7000.0,
             "tipo": "baja", "regla": "Cheque depositado o dado de baja",
             "etiqueta": "Cheque 9 · KRH", "familia": "traspaso"}]
    g = t.resumir(movs, -7000.0, idx)[0]
    # Sin el hecho falso queda el documento pelado — dice menos, no miente.
    # En la vida real el renglón se une con su otra pata (el banco) y termina
    # diciendo "CH → BC · KRH": ver el test de acá abajo.
    assert "→ FA" not in g["texto"]
    assert g["texto"] == "CH #9 KRH"


def test_las_dos_patas_de_un_deposito_sin_mov_doble_son_un_renglon():
    """*"esto también podría ser un renglón, ¿te das cuenta?"* — el depósito
    de KRH: banco +7.000 y cheques −7.000, mismo importe, distinta columna."""
    filas = [{"no_banco": 10, "documento": "DE", "importe": 7000.0,
              "concepto": "1 ch.KRH", "dia": "2026-08-07"}]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    movs = [
        {"doc_id": "b10", "componente": "bancos", "aporte": 7000.0,
         "tipo": "cambio", "regla": "Movimiento bancario",
         "etiqueta": "Banco PICHINCHA", "familia": "traspaso"},
        {"doc_id": "c700", "componente": "cheques", "aporte": -7000.0,
         "tipo": "baja", "regla": "Cheque depositado o dado de baja",
         "etiqueta": "Cheque 9 · KRH", "familia": "traspaso"},
    ]
    out = t.resumir(movs, 0.0, ev.indice([], cuentas))
    assert [g["texto"] for g in out] == ["CH → BC · KRH"], out
    assert out[0]["por_col"] == {"bancos": 7000.0, "cheques": -7000.0}
    assert out[0]["aporte"] == 0.0


def test_no_se_unen_dos_patas_si_el_par_es_ambiguo():
    """Unir de más sería inventar un hecho: con dos candidatos del mismo
    importe no hay forma de saber cuál va con cuál."""
    movs = [
        {"doc_id": "b10", "componente": "bancos", "aporte": 500.0,
         "tipo": "cambio", "regla": "Movimiento bancario",
         "etiqueta": "Banco PICHINCHA", "familia": "traspaso"},
        {"doc_id": "c1", "componente": "cheques", "aporte": -500.0,
         "tipo": "baja", "regla": "Cheque depositado o dado de baja",
         "etiqueta": "Cheque 1 · AAA", "familia": "traspaso"},
        {"doc_id": "k1", "componente": "caja", "aporte": -500.0,
         "tipo": "baja", "regla": "Gasto de caja",
         "etiqueta": "Caja S · X", "familia": "traspaso"},
    ]
    assert len(t.resumir(movs, -500.0, {})) == 3


def test_una_cuenta_con_muchos_movimientos_corta_con_mas_n():
    """El renglón se estira hasta `ANCHO_RENGLON` y corta con "+N": la columna
    del concepto dobla, pero un renglón de cuatro líneas ya es un párrafo."""
    filas = [{"no_banco": 10, "documento": "ND", "importe": -1.0,
              "concepto": c, "dia": "2026-08-07"}
             for c in ("GS ALMAGRO SEGURIDAD", "GS LICENCIA ANUAL",
                       "KK ARAUJO MONTACARGA", "CC MEDIO AMBIENTE")]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    txt = cuentas["b10"]["texto"]
    assert txt.startswith("BC · ") and txt.endswith("+1"), txt
    assert len(txt) <= ev.ANCHO_RENGLON + 4, txt


def test_el_grupo_mas_grande_de_la_cuenta_va_primero():
    """Ocho anticipos y tres gastos sueltos: el bloque de ocho encabeza. Es la
    misma regla que en el resto de la pantalla."""
    conceptos = [f"Anticipo USD AI $ {v} (ND Pichincha)" for v in
                 (314.96, 251.75, 304.20, 332.41, 306.70, 312.28)]
    conceptos += ["CC MERA ARREGLO GENERADOR", "CC ING MORA"]
    filas = [{"no_banco": 10, "documento": "ND", "importe": -1.0,
              "concepto": c, "dia": "2026-08-07"} for c in conceptos]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    assert cuentas["b10"]["texto"].startswith("BC · 6 × Anticipo USD"), \
        cuentas["b10"]["texto"]


def test_una_cuenta_sin_conceptos_cae_al_numero():
    filas = [{"no_banco": 10, "documento": "", "importe": 1.0,
              "concepto": "", "dia": "2026-08-07"} for _ in range(2)]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    assert cuentas["b10"]["texto"] == "BC · 2 movimientos"


def test_las_retenciones_de_varias_corridas_del_cron_son_un_renglon():
    """🚨 TMT 2026-08-07: *"se nos separaron de vuelta"* — cuatro renglones
    "retenciones · 17 / · 4 / · 2 / retenciones" en la misma ventana. La
    aplicada lleva `batch_id` (uno por corrida del cron) y se juntaba POR
    BATCH: tres corridas en cinco minutos = tres renglones que dicen lo mismo.
    """
    filas = []
    for batch, ids in (("aa", (1, 2, 3)), ("bb", (4, 5))):
        for i in ids:
            filas.append(_ev("retencion_asinfo_aplicada", "factura", 8000 + i,
                             "factura", 8000 + i, importe=-10.0, batch=batch))
    idx = ev.indice(_con_label(filas))
    movs = [{"doc_id": f"f{8000 + i}", "componente": "facturas",
             "aporte": -10.0, "regla": "Abono a factura",
             "etiqueta": f"Factura {i} · TNZ", "familia": "traspaso"}
            for i in range(1, 6)]
    out = t.resumir(movs, -50.0, idx)
    assert [g["texto"] for g in out] == ["retenciones · 5 facturas"], out


def test_dos_hechos_del_mismo_tipo_se_juntan_sin_estar_en_ninguna_lista():
    """🚨 TMT 2026-08-07, después de mapear las 200 ventanas: la lista de tipos
    que se juntaban se escribía A MANO y en cada pasada aparecía uno nuevo
    haciendo lo mismo (anticipos, cheques a caja, cheques a proveedor). La
    regla está dada vuelta: dos hechos del MISMO tipo se juntan siempre y el
    rótulo sale de `corto()`, que ya sabe nombrar los 50 tipos.
    """
    idx = ev.indice(_con_label([
        _ev("cheque_efectivo_to_caja", "cheque", 700 + i, "caja", 800 + i,
            importe=v) for i, v in enumerate([380.0, 180.0])]))
    movs = [{"doc_id": f"k{800 + i}", "componente": "caja", "aporte": v,
             "regla": "Ingreso de caja", "etiqueta": f"Caja E · {c}",
             "familia": "utilidad"}
            for i, (v, c) in enumerate([(380.0, "AAA"), (180.0, "BBB")])]
    g = t.resumir(movs, 560.0, idx)[0]
    # Sin clientes: en caja el último segmento es el CONCEPTO, no un código —
    # `_quien` los descarta a propósito ("4 gastos de caja · LUZ, AGUA, TAXI").
    assert g["texto"] == "2 CH → CJ"
    assert g["aporte"] == 560.0


def test_las_cuentas_bancarias_NO_se_juntan_entre_si():
    """La excepción: el renglón de una cuenta ya viene armado y su grupo ES la
    cuenta. Juntar por tipo mezclaría Pichincha con Internacional."""
    filas = [{"no_banco": 10, "documento": "TR", "importe": 100.0,
              "concepto": "UNO", "dia": "2026-08-07"},
             {"no_banco": 20, "documento": "TR", "importe": 200.0,
              "concepto": "DOS", "dia": "2026-08-07"}]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    movs = [{"doc_id": "b10", "componente": "bancos", "aporte": 100.0,
             "regla": "Movimiento bancario", "etiqueta": "Banco PICHINCHA",
             "familia": "traspaso"},
            {"doc_id": "b20", "componente": "bancos", "aporte": 200.0,
             "regla": "Movimiento bancario", "etiqueta": "Banco INTERNACI",
             "familia": "traspaso"}]
    textos = sorted(g["texto"] for g in t.resumir(movs, 300.0, ev.indice([], cuentas)))
    assert textos == ["transferencia · DOS", "transferencia · UNO"], textos


def test_las_dos_patas_se_unen_aunque_un_lado_toque_dos_componentes():
    """⭐ La condición era `len(por_col) == 1` y con eso la cobranza que se
    deposita —que toca cheques Y facturas— nunca entraba, justo el caso para el
    que esto se escribió. Lo que importa es que los dos lados no COMPARTAN
    componente."""
    idx = ev.indice(_con_label([
        _ev("cheque_aplicado_a_factura", "cheque", 700 + i, "factura", 800 + i,
            importe=100.0) for i in range(2)]))
    movs = [
        {"doc_id": "b10", "componente": "bancos", "aporte": 2198.0,
         "regla": "Movimiento bancario", "etiqueta": "Banco PICHINCHA",
         "familia": "traspaso"},
        {"doc_id": "c700", "componente": "cheques", "aporte": 500.0,
         "regla": "Cheque recibido", "etiqueta": "Cheque 1 · EEG",
         "familia": "traspaso"},
        {"doc_id": "f801", "componente": "facturas", "aporte": -2698.0,
         "regla": "Abono a factura", "etiqueta": "Factura 1 · EEG",
         "familia": "traspaso"},
    ]
    out = t.resumir(movs, 0.0, idx)
    assert len(out) == 1, [g["texto"] for g in out]
    assert out[0]["texto"].endswith("→ BC · EEG")
    assert out[0]["por_col"] == {"bancos": 2198.0, "cheques": 500.0,
                                 "facturas": -2698.0}


# ── Lo que no explicaba (mapa del 07/08) ────────────────────────────────────

def test_once_anticipos_iguales_se_dicen_una_vez():
    """🚨 "BC · Anticipo USD AI $ 314.96 (ND Pichincha), Anticipo USD AI
    $ 251.75 (ND Pichincha), … +8": el mismo concepto tres veces, con el
    importe adentro, y el renglón ilegible."""
    filas = [{"no_banco": 10, "documento": "ND", "importe": -v,
              "concepto": f"Anticipo USD AI $ {v} (ND Pichincha)",
              "dia": "2026-08-07"} for v in (314.96, 251.75, 304.20)]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    assert cuentas["b10"]["texto"] == "BC · 3 × Anticipo USD AI"


def test_conceptos_distintos_no_se_resumen_por_una_coincidencia():
    """Con menos de 8 caracteres el "prefijo común" es casualidad ("GS ") y
    resumir por ahí junta cosas que no tienen nada que ver."""
    filas = [{"no_banco": 10, "documento": "ND", "importe": -1.0,
              "concepto": c, "dia": "2026-08-07"}
             for c in ("GS ALMAGRO", "GS LICENCIA")]
    with patch.object(ev.db, "fetch_all", return_value=filas):
        cuentas = ev.transacciones("a", "b")
    assert cuentas["b10"]["texto"] == "BC · GS ALMAGRO, GS LICENCIA"


def test_los_quimicos_dicen_que_paso_y_no_de_donde_salen():
    """🚨 Cuarto renglón más frecuente del día (19 de 200 ventanas) y decía de
    qué SISTEMA sale el dato. Del lado de PC es un solo número —no hay detalle
    por colorante— pero para qué lado se movió sí se sabe."""
    base = {"doc_id": "#quimicos", "componente": "vqx",
            "regla": "Stock de químicos", "familia": "utilidad",
            "etiqueta": "Stock de químicos (formulas_app)"}
    sale = t.resumir([dict(base, aporte=-143.0)], -143.0, {})[0]
    entra = t.resumir([dict(base, aporte=101.0)], 101.0, {})[0]
    # 🚨 TMT 2026-08-11: *"esto debería ser consumo en químicos, x órdenes
    # terminadas"*. No es interpretación: el stock que PC lee de formulas_app
    # es compras + ajustes − consumo de las órdenes con `fecha_terminado`, así
    # que la BAJA es exactamente eso.
    assert sale["texto"] == "consumo de químicos por órdenes terminadas"
    # La suba se queda genérica a propósito: puede ser compra o ajuste, y
    # decir "compra" cuando fue un ajuste sería inventar.
    assert entra["texto"] == "entró a químicos"


def test_la_cuenta_con_varios_hechos_usa_el_concepto_que_se_escribio():
    """🚨 TMT 2026-08-07: *"acá me falta el número de AC 15 o AI 15"*.

    El `mov_doble` de un anticipo dice "Anticipo USD AC $ 1256.29 (ND
    Pichincha)" — el IMPORTE en el lugar del número, y el importe ya está en la
    columna. La FILA BANCARIA sí guarda lo que la persona escribió
    ("ANTICIPO AC 15"), así que cuando la cuenta tuvo varios hechos distintos
    —que es cuando el renglón se arma con conceptos— mandan los de la tabla de
    transacciones.
    """
    tx = [{"no_banco": 10, "documento": "ND", "importe": -v,
           "concepto": "ANTICIPO AC 15", "dia": "2026-08-07"}
          for v in (1256.29, 1407.14, 124.20)]
    with patch.object(ev.db, "fetch_all", return_value=tx):
        cuentas = ev.transacciones("a", "b")
    evs = _con_label([
        _ev("dolares_anticipo", "dolares", 900 + i, "transacciones_bancarias",
            700 + i, importe=-v,
            concepto=f"Anticipo USD AC $ {v} (ND Pichincha)",
            metadata={"no_banco": 10})
        for i, v in enumerate((1256.29, 1407.14, 124.20))])
    idx = ev.indice(evs, cuentas)
    assert idx["b10"]["tipo"] == "banco_varios"
    assert "15" in idx["b10"]["texto"], idx["b10"]["texto"]
    assert "$" not in idx["b10"]["texto"], idx["b10"]["texto"]


def test_con_UN_solo_hecho_manda_el_nombre_del_hecho_y_no_el_concepto():
    """"CH → BC" dice más que "1 ch.KRH", que es lo que se tipeó en el banco."""
    tx = [{"no_banco": 10, "documento": "DE", "importe": 7000.0,
           "concepto": "1 ch.KRH", "dia": "2026-08-07"}]
    with patch.object(ev.db, "fetch_all", return_value=tx):
        cuentas = ev.transacciones("a", "b")
    evs = _con_label([
        _ev("cheque_depositado", "cheque", 5000, "transacciones_bancarias",
            7788, importe=7000.0, metadata={"no_banco": 10,
                                            "id_transaccion": 7788})])
    assert ev.indice(evs, cuentas)["b10"]["tipo"] == "cheque_depositado"


# ── Químicos: el neto arriba, lo que se cargó abajo ─────────────────────────
#
# 🚨 TMT 2026-08-11, tras el deep dive de las 9 subas grabadas desde el 31/07:
# 7 son compras, 1 son ajustes, 1 es el reloj. Y al revés: el 04/08 14:27 se
# cargó una compra a QSI de 60 u. y la ventana BAJÓ −135,17, porque el consumo
# de las órdenes que cerraron pesó más. El signo es el neto, no la causa.

_QX = {"doc_id": "#quimicos", "componente": "vqx",
       "regla": "Stock de químicos", "familia": "utilidad"}


def _qx(etiqueta, aporte):
    return t.resumir([dict(_QX, etiqueta=etiqueta, aporte=aporte)], aporte, {})[0]


def test_la_compra_de_quimicos_se_nombra_aunque_la_ventana_baje():
    """El caso del 04/08 14:27: compra a QSI y la columna igual bajó."""
    g = _qx(t._TXT_QUIMICOS + " · se cargó una compra a QSI (60 u.)", -135.17)
    assert g["texto"] == "consumo de químicos por órdenes terminadas"
    assert g["nota"] == "se cargó una compra a QSI (60 u.)"


def test_la_suba_con_compra_lo_dice():
    g = _qx(t._TXT_QUIMICOS + " · se cargó 11 compras a AVQ (1.605 u.)", 7044.27)
    assert g["texto"] == "entró a químicos"
    assert g["nota"] == "se cargó 11 compras a AVQ (1.605 u.)"


def test_sin_causa_guardada_no_hay_nota():
    """Las fotos anteriores al 11/08 no la guardaron: sin nota, no inventada."""
    g = _qx(t._TXT_QUIMICOS, -530.0)
    assert g["texto"] == "consumo de químicos por órdenes terminadas"
    assert not g.get("nota")


def test_la_frase_de_lo_cargado_se_arma_sin_base():
    from modules.informes.quimico_inv_formulas import _frase_de_lo_cargado as fr

    assert fr([]) == ""
    assert fr([{"k": "compra", "quien": "AVQ", "n": 1, "q": 200}]) == \
        "se cargó una compra a AVQ (200 u.)"
    # Dos proveedores en la misma ventana: se nombran los dos.
    assert fr([{"k": "compra", "quien": "AVQ", "n": 2, "q": 400},
               {"k": "compra", "quien": "SEY", "n": 1, "q": 1000}]) == \
        "se cargaron 3 compras a AVQ y SEY (1.400 u.)"
    # El 03/08: tres ajustes, ninguna compra.
    assert fr([{"k": "ajuste", "quien": "", "n": 3, "q": 29.55}]) == \
        "se cargaron 3 ajustes de inventario"
    # Un conteo físico también mueve el número, y no es ni compra ni ajuste.
    assert fr([{"k": "conteo", "quien": "", "n": 1, "q": 5}]) == \
        "se cargó conteo físico"
    # Compra Y ajuste en la misma ventana: se dicen las dos cosas.
    assert fr([{"k": "compra", "quien": "AVQ", "n": 1, "q": 200},
               {"k": "ajuste", "quien": "", "n": 2, "q": 30}]) == \
        "se cargaron una compra a AVQ (200 u.) y 2 ajustes de inventario"
