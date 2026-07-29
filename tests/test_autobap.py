"""Conversión automática anticipo → compra BAP (mig 0136) — TMT 2026-07-29.

Dueña: "ponelo en una campanita y hagámoslo automático" + "convierte solo,
entiendo que va a diferir del dBase".

Lo que se prueba son los GUARDS: el motor sólo reusa convertir_a_compra(), lo
que hay que garantizar es que no dispare cuando no tiene que disparar.
"""
from unittest.mock import patch

from modules.importaciones import autobap

_CFG = {"activo": True, "fecha_corte": "2026-07-01", "tope_importaciones": 5,
        "tope_usd": 60000.0, "disponible": True}

_IM_UNICA = {("AC", 29): [{"im_numero": "IM-0000625", "fecha": "2026-06-01",
                           "nota": "ACMT/EXP/2026-27/1 ( AC 29)"}]}
_IM_REPETIDA = {("AC", 58): [
    {"im_numero": "IM-0000622", "fecha": "2026-07-20",
     "nota": "ACMT/EXP/2026-27/8368 ( AC 58)"},
    {"im_numero": "IM-0000527", "fecha": "2026-02-11",
     "nota": "INV HY336-26-1 ( AC 58)"},
]}


def _anticipo(**kw):
    base = {"id_dolares": 1, "fecha": "2026-07-29", "cta": "AC",
            "concepto": "29 SALDO", "importe": 1000.0, "ref": 29,
            "anio_ref": None, "im_numero": "IM-0000625",
            "fecha_recepcion_im": "2026-07-29", "kg_im": 24498}
    base.update(kw)
    return base


def _correr_pendientes(filas, index, cfg=None):
    """pendientes() con Asinfo y la DB mockeados."""
    with patch("modules.dolares.queries.anticipos_vivos", return_value=filas), \
         patch("modules.importaciones.service._index_importaciones_por_codigo",
               return_value=index), \
         patch("modules.importaciones.service.adjuntar_recepcion_asinfo"), \
         patch("modules.dolares.anio.adjuntar_anio"):
        return autobap.pendientes(cfg or _CFG)


# ── GUARD 1: Asinfo mudo ────────────────────────────────────────────────────

def test_asinfo_mudo_no_convierte_nada():
    r = _correr_pendientes([_anticipo()], {})
    assert r["disponible"] is False
    assert r["grupos"] == []
    assert r["frenos"]


def test_asinfo_caido_no_convierte_nada():
    with patch("modules.importaciones.service._index_importaciones_por_codigo",
               side_effect=Exception("metabase 500")):
        r = autobap.pendientes(_CFG)
    assert r["disponible"] is False and r["grupos"] == []


# ── GUARD 3: sólo con recepción real ────────────────────────────────────────

def test_sin_recepcion_no_entra():
    r = _correr_pendientes([_anticipo(fecha_recepcion_im=None)], _IM_UNICA)
    assert r["grupos"] == []


def test_sin_importacion_matcheada_no_entra():
    r = _correr_pendientes([_anticipo(im_numero=None)], _IM_UNICA)
    assert r["grupos"] == []


# ── GUARD 4: fecha de corte ─────────────────────────────────────────────────

def test_recepcion_anterior_al_corte_no_se_toca():
    """Las 4 pendientes viejas (58, 76, 77, 18) quedan congeladas."""
    r = _correr_pendientes(
        [_anticipo(fecha_recepcion_im="2026-06-15")], _IM_UNICA)
    assert r["grupos"] == []


def test_recepcion_posterior_al_corte_si_entra():
    r = _correr_pendientes([_anticipo()], _IM_UNICA)
    assert r["n_importaciones"] == 1
    assert r["grupos"][0]["im_numero"] == "IM-0000625"
    assert r["total_usd"] == 1000.0


# ── GUARD 9: código repetido sin año escrito ────────────────────────────────

def test_codigo_repetido_sin_anio_no_se_convierte_solo():
    """El caso AC 58: dos importaciones con el mismo código. Sin el año puesto
    el programa NO adivina — avisa y espera."""
    a = _anticipo(concepto="58", ref=58, im_numero="IM-0000527",
                  anio_ref=None)
    r = _correr_pendientes([a], _IM_REPETIDA)
    assert r["grupos"] == []
    assert any("58" in f and "año" in f for f in r["frenos"])


def test_codigo_repetido_con_anio_si_se_convierte():
    a = _anticipo(concepto="58", ref=58, im_numero="IM-0000622", anio_ref=2026)
    r = _correr_pendientes([a], _IM_REPETIDA)
    assert r["n_importaciones"] == 1
    assert r["grupos"][0]["anio"] == 2026


def test_codigo_unico_sin_anio_si_se_convierte():
    """Si hay una sola importación con ese código no hay nada que desambiguar."""
    r = _correr_pendientes([_anticipo(anio_ref=None)], _IM_UNICA)
    assert r["n_importaciones"] == 1


# ── Agrupación ──────────────────────────────────────────────────────────────

def test_agrupa_por_importacion_en_una_sola_compra():
    filas = [
        _anticipo(id_dolares=1, concepto="29", importe=16530.05),
        _anticipo(id_dolares=2, concepto="29 SALDO", importe=56552.80),
        _anticipo(id_dolares=3, concepto="29 MAPFRE", importe=277.10),
    ]
    r = _correr_pendientes(filas, _IM_UNICA)
    assert r["n_importaciones"] == 1
    g = r["grupos"][0]
    assert g["n"] == 3
    assert sorted(g["ids"]) == [1, 2, 3]
    assert g["importe"] == 73359.95


# ── GUARD 5: topes ──────────────────────────────────────────────────────────

def test_tope_de_plata_frena_todo_y_avisa():
    caro = _anticipo(importe=99999.0)
    with patch.object(autobap, "config", return_value=_CFG), \
         patch.object(autobap, "pendientes", return_value={
             "grupos": [{"im_numero": "IM-1", "codigo_prov": "AC", "ref": 1,
                         "ids": [1], "n": 1, "importe": 99999.0,
                         "fecha_recepcion": "2026-07-29", "kg": 1, "anio": 2026}],
             "total_usd": 99999.0, "n_importaciones": 1, "frenos": [],
             "disponible": True}), \
         patch.object(autobap, "avisar") as av, \
         patch("modules.dolares.queries.convertir_a_compra") as conv:
        r = autobap.correr()
    assert r["convertidas"] == 0
    conv.assert_not_called()
    assert av.call_args.kwargs["tipo"] == "freno"
    assert caro["importe"] == 99999.0  # el fixture no se tocó


def test_tope_se_puede_forzar_desde_la_pantalla():
    with patch.object(autobap, "config", return_value=_CFG), \
         patch.object(autobap, "pendientes", return_value={
             "grupos": [{"im_numero": "IM-1", "codigo_prov": "AC", "ref": 1,
                         "ids": [1], "n": 1, "importe": 99999.0,
                         "fecha_recepcion": "2026-07-29", "kg": 1, "anio": 2026}],
             "total_usd": 99999.0, "n_importaciones": 1, "frenos": [],
             "disponible": True}), \
         patch.object(autobap, "avisar"), \
         patch("periodo_guard.asegurar_fecha_abierta"), \
         patch("modules.dolares.queries.convertir_a_compra",
               return_value={"importe_total": 99999.0, "comprobante": "BAP9",
                             "id_compra": 5, "numero_compra": 9}) as conv:
        r = autobap.correr(forzar_topes=True)
    assert r["convertidas"] == 1
    conv.assert_called_once()


# ── GUARD 7: el switch ──────────────────────────────────────────────────────

def test_apagado_el_ciclo_no_hace_nada():
    autobap._ultimo_ts = -1e9  # monotonic() puede ser chico en el contenedor
    with patch.object(autobap, "config",
                      return_value={**_CFG, "activo": False}), \
         patch.object(autobap, "correr") as c:
        r = autobap.correr_si_toca()
    c.assert_not_called()
    assert r["convertidas"] == 0


def test_el_freno_de_30_minutos_no_encima_corridas():
    autobap._ultimo_ts = -1e9  # monotonic() puede ser chico en el contenedor
    with patch.object(autobap, "config", return_value=_CFG), \
         patch.object(autobap, "correr",
                      return_value={"convertidas": 1, "importe": 1.0,
                                    "frenos": [], "grupos": []}) as c:
        autobap.correr_si_toca()
        autobap.correr_si_toca()   # inmediata: no debería correr de nuevo
    assert c.call_count == 1
    autobap._ultimo_ts = -1e9  # monotonic() puede ser chico en el contenedor


# ── Un error en una importación no frena al resto ───────────────────────────

def test_una_importacion_que_falla_no_frena_a_las_otras():
    grupos = [
        {"im_numero": "IM-1", "codigo_prov": "AC", "ref": 1, "ids": [1], "n": 1,
         "importe": 100.0, "fecha_recepcion": "2026-07-29", "kg": 1, "anio": 2026},
        {"im_numero": "IM-2", "codigo_prov": "AC", "ref": 2, "ids": [2], "n": 1,
         "importe": 200.0, "fecha_recepcion": "2026-07-29", "kg": 1, "anio": 2026},
    ]
    with patch.object(autobap, "config", return_value=_CFG), \
         patch.object(autobap, "pendientes", return_value={
             "grupos": grupos, "total_usd": 300.0, "n_importaciones": 2,
             "frenos": [], "disponible": True}), \
         patch.object(autobap, "avisar"), \
         patch("periodo_guard.asegurar_fecha_abierta"), \
         patch("modules.dolares.queries.convertir_a_compra",
               side_effect=[Exception("período cerrado"),
                            {"importe_total": 200.0, "comprobante": "BAP2",
                             "id_compra": 2, "numero_compra": 2}]):
        r = autobap.correr()
    assert r["convertidas"] == 1
    assert r["importe"] == 200.0
    assert any("IM-1" in f for f in r["frenos"])


# ── La compra lleva el número SOLO, no el año ───────────────────────────────

def test_el_concepto_de_la_compra_va_sin_anio():
    """_buscar_compras junta todos los dígitos: "58/26" sería 5826."""
    grupos = [{"im_numero": "IM-1", "codigo_prov": "AC", "ref": 58, "ids": [1],
               "n": 1, "importe": 100.0, "fecha_recepcion": "2026-07-29",
               "kg": 1, "anio": 2026}]
    with patch.object(autobap, "config", return_value=_CFG), \
         patch.object(autobap, "pendientes", return_value={
             "grupos": grupos, "total_usd": 100.0, "n_importaciones": 1,
             "frenos": [], "disponible": True}), \
         patch.object(autobap, "avisar"), \
         patch("periodo_guard.asegurar_fecha_abierta"), \
         patch("modules.dolares.queries.convertir_a_compra",
               return_value={"importe_total": 100.0, "comprobante": "BAP1",
                             "id_compra": 1, "numero_compra": 1}) as conv:
        autobap.correr()
    assert conv.call_args.kwargs["concepto"] == "58"
    assert conv.call_args.kwargs["usuario"] == "bap-auto"
    assert conv.call_args.kwargs["kg"] is None
    assert conv.call_args.kwargs["tipo_compra"] == "H"


# ── dry_run no escribe ──────────────────────────────────────────────────────

def test_dry_run_no_convierte():
    grupos = [{"im_numero": "IM-1", "codigo_prov": "AC", "ref": 1, "ids": [1],
               "n": 1, "importe": 100.0, "fecha_recepcion": "2026-07-29",
               "kg": 1, "anio": 2026}]
    with patch.object(autobap, "config", return_value=_CFG), \
         patch.object(autobap, "pendientes", return_value={
             "grupos": grupos, "total_usd": 100.0, "n_importaciones": 1,
             "frenos": [], "disponible": True}), \
         patch("modules.dolares.queries.convertir_a_compra") as conv:
        r = autobap.correr(dry_run=True)
    conv.assert_not_called()
    assert r["convertidas"] == 0


# ── Topes editables (mig 0137) ──────────────────────────────────────────────

def test_topes_default_ya_no_frenan_una_importacion_normal():
    """US$60.000 era menos que una sola importación (AC 94: $99.519,84)."""
    assert autobap._TOPE_USD_DEFAULT >= 300000
    assert autobap._TOPE_IM_DEFAULT >= 10


def test_set_topes_valida_rangos():
    for n, u in ((0, 100), (501, 100), (5, 0), (5, -1), (5, 99_000_000)):
        try:
            with patch.object(autobap.db, "execute"):
                autobap.set_topes(tope_importaciones=n, tope_usd=u)
            raise AssertionError(f"aceptó {n}/{u}")
        except ValueError:
            pass


def test_set_topes_guarda():
    with patch.object(autobap.db, "execute") as ex, \
         patch.object(autobap, "config", return_value=_CFG):
        autobap.set_topes(tope_importaciones=10, tope_usd=500000,
                          usuario="tamara")
    assert ex.call_args[0][1][:2] == (10, 500000.0)
