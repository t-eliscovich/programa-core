"""Las doce frases del catálogo, con datos REALES de producción (07/08/2026).

Dueña: *"la frase nombra el hecho"* y *"todos los acorté, está muy wordy"*. Los
tests fijan las dos cosas: qué dice cada frase y que no diga de más.
"""
from __future__ import annotations

from unittest.mock import patch

from modules.informes import catalogo as cat


def _h(tipo, importe, meta, concepto="", origen=("cheque", 1), destino=("cheque", 1)):
    return {"tipo": tipo, "importe": importe, "concepto": concepto,
            "origen_table": origen[0], "origen_id": origen[1],
            "destino_table": destino[0], "destino_id": destino[1],
            "metadata": meta}


def _una(h):
    """Las frases DISTINTAS del hecho.

    Un mismo hecho se indexa bajo el doc_id de origen Y el de destino —así lo
    encuentra cualquiera de los dos renglones de la foto—, por eso hay que
    deduplicar. `aplicar()` hace lo mismo con `vistos`.
    """
    with patch.object(cat.db, "fetch_all", return_value=[h]):
        return sorted(set(cat.hechos("2026-08-07 00:00", "2026-08-07 23:59").values()))


def test_cheque_que_totaliza_no_dice_saldo():
    h = _h("cheque_aplicado_a_factura", 2006.02,
           {"numf": 181187, "id_cheque": 102021, "saldo_factura_post": 0.0},
           origen=("cheque", 102021), destino=("factura", 280650))
    assert _una(h) == ["Cheque 102021 totalizó F#181187 de $ 2.006,02"]


def test_cheque_que_abona_si_dice_saldo():
    h = _h("cheque_aplicado_a_factura", 500.0,
           {"numf": 181187, "id_cheque": 102021, "saldo_factura_post": 1506.02},
           origen=("cheque", 102021), destino=("factura", 280650))
    assert _una(h) == ["Cheque 102021 abonó F#181187 de $ 500,00, saldo $ 1.506,02"]


def test_el_cheque_que_no_es_anticipo_no_emite_renglon():
    """Entra SIEMPRE aplicado a una factura: habla la aplicación, no el alta."""
    h = _h("cheque_creado", 2012.12, {"codigo_cli": "SEF", "es_anticipo": False})
    assert _una(h) == []


def test_el_anticipo_si_emite():
    h = _h("cheque_creado", 1251.87, {"codigo_cli": "LEG", "es_anticipo": True})
    assert _una(h) == ["Cheque de LEG por $ 1.251,87 (anticipo)"]


def test_los_depositos_van_agrupados():
    """22 cheques depositados juntos son UN renglón, no 22."""
    filas = [_h("cheque_depositado", 100.0,
                {"n_grupo": 22, "no_banco": 10, "banco_nombre": "PICHINCHA"},
                origen=("cheque", 900 + i)) for i in range(3)]
    with patch.object(cat.db, "fetch_all", return_value=filas):
        out = set(cat.hechos("a", "b").values())
    assert out == {"Se depositaron 3 cheques por $ 300,00 en PICHINCHA"}


def test_un_deposito_solo_no_dice_el_grupo():
    h = _h("cheque_depositado", 172.33,
           {"n_grupo": 5, "no_banco": 10, "banco_nombre": "PICHINCHA"},
           origen=("cheque", 101875))
    assert _una(h) == ["Se depositó 1 cheque por $ 172,33 en PICHINCHA"]


def test_anticipo_que_cancela_un_cheque():
    h = _h("cheque_cancelado_por_anticipo", 1251.87,
           {"codigo_cli": "LEG", "id_cheque": 99979}, origen=("cheque", 99979))
    assert _una(h) == ["Anticipo de LEG por $ 1.251,87 canceló el cheque 99979"]


def test_factura_compra_devolucion_y_retencion():
    casos = [
        (_h("factura_emitida", 413.57, {"numf": 181292, "codigo_cli": "JIM"},
            origen=("factura", 1), destino=("factura", 1)),
         "F#181292 de JIM por $ 413,57"),
        (_h("compra_a_posdat", 7274.9,
            {"numero_compra": 10133, "codigo_prov": "AQ"},
            origen=("compra", 1), destino=("posdat", 77)),
         "Compra 10133 a AQ por $ 7.274,90"),
        (_h("factura_devolucion", 3.91, {"numf": 11617, "codigo_cli": "CLR"},
            origen=("factura", 5), destino=("factura", 5)),
         "Devolución F#11617 de CLR por $ 3,91"),
        (_h("retencion_asinfo_aplicada", 45.5, {"numf": 181200},
            origen=("factura", 7), destino=("factura", 7)),
         "Retención de $ 45,50 sobre F#181200"),
    ]
    for h, esperado in casos:
        assert _una(h) == [esperado]


def test_ninguna_frase_interpreta():
    """Nada de "la utilidad no cambia": el Δ ya está en la columna de al lado."""
    prohibidas = ("utilidad", "no cambia", "cambió de lugar", "baja la utilidad")
    filas = [
        _h("cheque_aplicado_a_factura", 10, {"numf": 1, "id_cheque": 2, "saldo_factura_post": 0.0}),
        _h("cheque_depositado", 10, {"n_grupo": 1, "no_banco": 10, "banco_nombre": "PICHINCHA"}),
        _h("compra_a_posdat", 10, {"numero_compra": 1, "codigo_prov": "AQ"},
           origen=("compra", 1), destino=("posdat", 2)),
        _h("cheque_efectivo_to_caja", 200, {"codigo_cli": "LIO"},
           origen=("cheque", 3), destino=("caja", 872)),
    ]
    with patch.object(cat.db, "fetch_all", return_value=filas):
        for txt in cat.hechos("a", "b").values():
            for mala in prohibidas:
                assert mala not in txt.lower(), f"{txt!r} interpreta de más"


def test_aplicar_pisa_la_etiqueta_pero_guarda_la_firma():
    movs = [{"doc_id": "c102021", "etiqueta": "Cheque · SEF",
             "regla": "Cheque depositado o dado de baja"}]
    h = _h("cheque_aplicado_a_factura", 2006.02,
           {"numf": 181187, "id_cheque": 102021, "saldo_factura_post": 0.0},
           origen=("cheque", 102021), destino=("factura", 280650))
    with patch.object(cat.db, "fetch_all", return_value=[h]):
        out = cat.aplicar(movs, "a", "b")
    assert out[0]["etiqueta"] == "Cheque 102021 totalizó F#181187 de $ 2.006,02"
    assert out[0]["etiqueta_firma"] == "Cheque · SEF"
    assert out[0]["del_catalogo"] is True


def test_si_la_base_falla_la_traza_sigue():
    movs = [{"doc_id": "c1", "etiqueta": "lo de siempre"}]
    with patch.object(cat.db, "fetch_all", side_effect=RuntimeError("boom")):
        out = cat.aplicar(movs, "a", "b")
    assert out[0]["etiqueta"] == "lo de siempre"


def test_los_doce_tipos_del_95_por_ciento_tienen_frase():
    for tipo in ("factura_emitida", "cheque_aplicado_a_factura", "cheque_creado",
                 "cheque_depositado", "retencion_asinfo_aplicada", "factura_devolucion",
                 "compra_a_posdat", "cheque_efectivo_to_caja", "caja_s_to_gasto",
                 "compra_backfill", "gasto_a_posdat", "cheque_emitido_gasto"):
        assert tipo in cat.FRASES, f"falta la frase de {tipo}"
