"""Compras LOCALES de hilo — tarifario, cruce y carga automática.

Nada de red ni de DB real: se mockean `asinfo_service.compras_locales_asinfo`,
las tarifas y el cruce contra `scintela.compra`.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from modules.asinfo import service as asinfo_service
from modules.compras_locales import queries as q
from modules.compras_locales import service as svc

# ---------------------------------------------------------------------------
# numero_de_factura — el nº que el dBase escribe en el concepto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("001-002-000037649", 37649),   # HY, caso real
        ("001-002-000002049", 2049),    # EP, caso real
        ("37649", 37649),               # sin guiones
        ("", None),
        ("   ", None),
        (None, None),
        ("001-002-XXX", 1002),          # sin dígitos en el último tramo
    ],
)
def test_numero_de_factura(entrada, esperado):
    assert asinfo_service.numero_de_factura(entrada) == esperado


# ---------------------------------------------------------------------------
# resolver — el patrón más específico gana
# ---------------------------------------------------------------------------
TARIFAS = [
    {"cod_prov": "HY", "patron": None, "tarifa": 3.00},
    {"cod_prov": "HY", "patron": "22/1-65:35CAR-10%-HY", "tarifa": 3.5249},
    {"cod_prov": "EP", "patron": "14/1-65:35-OP-PERAL", "tarifa": 2.95},
]


def test_resolver_patron_especifico_gana():
    assert q.resolver(TARIFAS, "HY", "22/1-65:35CAR-10%-HY") == 3.5249


def test_resolver_cae_al_default_del_proveedor():
    assert q.resolver(TARIFAS, "HY", "99/1-OTRO") == 3.00


def test_resolver_sin_default_devuelve_none():
    # EP no tiene fila con patron NULL → un producto desconocido no resuelve.
    assert q.resolver(TARIFAS, "EP", "16/1-65:35-OPEN-EP") is None


def test_una_tarifa_por_proveedor_aplica_a_cualquier_producto():
    # Forma REAL del tarifario desde la mig 0144: una fila por proveedor
    # (patron NULL) con el promedio ponderado. Cubre todos sus productos.
    solo_default = [{"cod_prov": "HY", "patron": None, "tarifa": 3.0708}]
    assert q.resolver(solo_default, "HY", "75D/72F-POL-HENGYI") == 3.0708
    assert q.resolver(solo_default, "HY", "14/1-SPUN-HY") == 3.0708
    assert q.resolver(solo_default, "HY", None) == 3.0708


def test_factura_con_varios_productos_ya_no_frena_la_carga():
    # Con una tarifa por proveedor, cuántos productos trae la factura da igual.
    res = _plan([_fila(n_productos=3)])
    assert res["creadas"] == 1


def test_resolver_proveedor_desconocido_o_vacio():
    assert q.resolver(TARIFAS, "ZZ", "lo que sea") is None
    assert q.resolver(TARIFAS, "", "lo que sea") is None


# ---------------------------------------------------------------------------
# _estado_pago — el marcador de la columna Compras US$
# ---------------------------------------------------------------------------
def _compra(importe, *, saldo=0.0, tiene_posdat=True, legacy=False):
    return {"id_compra": 1, "importe": importe, "saldo_pasivo": saldo,
            "tiene_posdat": tiene_posdat, "pagada_legacy": legacy}


def test_estado_pago_saldada():
    est = svc._estado_pago([_compra(1664.64, saldo=0.0)])
    assert est["pagada"] is True
    assert est["saldo"] == 0.0
    assert est["parcial"] is False


def test_estado_pago_impaga_entera_no_es_parcial():
    # El importe YA es el pasivo → la pantalla no repite el número.
    est = svc._estado_pago([_compra(1664.64, saldo=1664.64)])
    assert est["pagada"] is False
    assert est["saldo"] == 1664.64
    assert est["parcial"] is False


def test_estado_pago_parcial_muestra_el_saldo():
    est = svc._estado_pago([_compra(73359.95, saldo=20000.0)])
    assert est["pagada"] is False
    assert est["parcial"] is True
    assert est["saldo"] == 20000.0


def test_estado_pago_legacy_del_dbase_sin_posdat():
    # Compra histórica del DBF con banco cargado = pagada.
    est = svc._estado_pago([_compra(100.0, tiene_posdat=False, legacy=True)])
    assert est["pagada"] is True
    # Sin banco y sin posdat = deuda viva.
    est2 = svc._estado_pago([_compra(100.0, tiene_posdat=False, legacy=False)])
    assert est2["pagada"] is False
    assert est2["saldo"] == 100.0


# ---------------------------------------------------------------------------
# compras_locales_con_cruce
# ---------------------------------------------------------------------------
CRUDA = {
    "fp_numero": "CMTR-06994", "fecha": "2026-07-21",
    "fecha_recepcion": "2026-07-20", "recibida": True, "bod": "BOD-000002314",
    "numero_factura": "001-002-000037649", "fact_num": 37649,
    "total_asinfo": 1447.51, "proveedor": "HILTEXPOY S.A.",
    "ruc": "1791436210001", "producto": "14/1-65:35-CAR-HY", "n_productos": 1,
    "kg": 499.14, "nota": "HY14 CAR L-HGA514 20/F F-37649",
}
POR_RUC = {"1791436210001": "HY", "1890153654001": "EP"}
TARIFA_HY = [{"cod_prov": "HY", "patron": "14/1-65:35-CAR-HY", "tarifa": 2.90}]


def _con_cruce(crudas=None, cruce=None, tarifas=None):
    with patch.object(asinfo_service, "compras_locales_asinfo",
                      return_value=crudas if crudas is not None else [CRUDA]), \
         patch.object(q, "proveedores_por_ruc", return_value=POR_RUC), \
         patch.object(q, "listar_tarifas",
                      return_value=tarifas if tarifas is not None else TARIFA_HY), \
         patch.object(svc, "_buscar_compras", return_value=cruce or {}):
        return svc.compras_locales_con_cruce()


def test_cruce_mapea_proveedor_por_ruc_y_arma_el_codigo():
    (f,) = _con_cruce()
    assert f["origen"] == "compra"
    assert f["prov"] == "HY"
    assert f["codigo"] == "HY 37649"
    assert f["im_numero"] == "CMTR-06994"


def test_importe_sugerido_es_kg_por_tarifa_con_iva():
    # Caso REAL verificado contra /compras: 499,14 × 2,90 × 1,15 = 1.664,64.
    (f,) = _con_cruce()
    assert f["tarifa"] == 2.90
    assert f["importe_sugerido"] == 1664.64


def test_sin_tarifa_ni_factura_no_hay_importe_sugerido():
    (f,) = _con_cruce(crudas=[{**CRUDA, "total_asinfo": 0}], tarifas=[])
    assert f["tarifa"] is None
    assert f["importe_sugerido"] is None
    assert f["importe_de"] is None


def test_con_tarifa_manda_la_tarifa_aunque_asinfo_tenga_la_factura():
    (f,) = _con_cruce(crudas=[{**CRUDA, "iva_asinfo": 217.13, "kg_factura": 499.14}])
    assert f["importe_de"] == "tarifa"
    assert f["importe_sugerido"] == 1664.64


# Tamara 2026-09-17: "la factura está en Asinfo — buscarla y matchear todo de
# una". El caso real: QC (Química Comercial) 1.012,32 kg de lycra trabados 23
# días por "falta la tarifa", con la factura CMTR-07002 cargada en Asinfo
# (6.580,08 + 987,01 de IVA).
QC = {**CRUDA, "fp_numero": "CMTR-07002", "bod": "BOD-000002356",
      "numero_factura": "001-003-000001773", "fact_num": 1773,
      "total_asinfo": 6580.08, "iva_asinfo": 987.01, "kg_factura": 1012.32,
      "proveedor": "QUIMICA COMERCIAL", "ruc": "1790451682001",
      "producto": "40-LYC-QC", "kg": 1012.32, "fecha_recepcion": "2026-08-25"}


def test_sin_tarifa_vale_la_factura_de_asinfo_con_iva():
    with patch.dict(POR_RUC, {"1790451682001": "QC"}):
        (f,) = _con_cruce(crudas=[QC], tarifas=TARIFA_HY)
    assert f["prov"] == "QC"
    assert f["tarifa"] is None
    assert f["importe_de"] == "asinfo"
    assert f["importe_sugerido"] == 7567.09      # 6.580,08 + 987,01, al centavo


def test_entrega_parcial_prorratea_la_factura_por_kilos():
    # La factura es de 1.000 kg y este BOD trajo 250: carga un cuarto.
    assert svc._importe_asinfo(250.0, 1000.0, 150.0, 1000.0) == 287.5
    # Entrega completa (o sin kg de factura conocidos): la factura entera.
    assert svc._importe_asinfo(1000.0, 1000.0, 150.0, 1000.0) == 1150.0
    assert svc._importe_asinfo(1000.0, 1000.0, 150.0, 0) == 1150.0
    # Sin factura en Asinfo no hay plata.
    assert svc._importe_asinfo(1000.0, 0, 0, 0) is None


def test_el_motor_carga_la_de_asinfo_sin_tarifa():
    res = _plan([_fila(tarifa=None, importe_sugerido=7567.09, importe_de="asinfo")])
    assert res["creadas"] == 1
    assert res["detalle"][0]["importe"] == 7567.09
    assert res["detalle"][0]["importe_de"] == "asinfo"


def test_cruce_con_compra_marca_fuente_y_estado():
    cruce = {("HY", 37649): [_compra(1664.64, saldo=1664.64)]}
    (f,) = _con_cruce(cruce=cruce)
    assert f["fuente"] == "compra"
    assert f["importe_programa"] == 1664.64
    assert f["pagada"] is False
    assert f["compra"]["n"] == 1


def test_asinfo_mudo_devuelve_vacio():
    assert _con_cruce(crudas=[]) == []


def test_asinfo_que_explota_no_rompe_la_pantalla():
    with patch.object(asinfo_service, "compras_locales_asinfo",
                      side_effect=RuntimeError("metabase caído")):
        assert svc.compras_locales_con_cruce() == []


# ---------------------------------------------------------------------------
# cargar_pendientes — las guardas
# ---------------------------------------------------------------------------
def _fila(**kw):
    base = {
        "origen": "compra", "im_numero": "CMTR-06994", "prov": "HY",
        "proveedor": "HILTEXPOY S.A.", "fact_num": 37711,
        "producto": "14/1-65:35-CAR-HY", "n_productos": 1,
        "fecha_recepcion": "2026-07-27", "recibida": True, "kg": 5002.5,
        "tarifa": 2.90, "importe_sugerido": 16683.34, "compra": None,
    }
    base.update(kw)
    return base


def _plan(filas, **kw):
    with patch.object(svc, "compras_locales_con_cruce", return_value=filas):
        return svc.cargar_pendientes(dry_run=True, **kw)


def test_carga_dry_run_no_escribe_y_planifica():
    res = _plan([_fila()])
    assert res["creadas"] == 1
    assert res["importe"] == 16683.34
    assert res["detalle"][0]["ok"] is True


@pytest.mark.parametrize(
    ("cambio", "motivo"),
    [
        ({"compra": {"n": 1}}, "ya tiene compra"),
        ({"recibida": False}, "todavía no recibida"),
        ({"kg": 0}, "todavía no recibida"),
        ({"fecha_recepcion": "2026-06-30"}, "antes del"),
        ({"prov": None}, "sin código en el programa"),
        ({"fact_num": None}, "factura sin número"),
        ({"tarifa": None, "importe_sugerido": None}, "sin tarifa"),
    ],
)
def test_guardas_saltean_con_motivo(cambio, motivo):
    res = _plan([_fila(**cambio)])
    assert res["creadas"] == 0
    assert res["salteadas"] == 1
    assert motivo in res["detalle"][0]["motivo"]


def test_tope_de_cantidad_frena_la_corrida():
    filas = [_fila(fact_num=n, im_numero=f"CMTR-{n}") for n in range(1, 26)]
    res = _plan(filas)
    assert res["creadas"] == svc.TOPE_COMPRAS
    assert any("tope" in d["motivo"] for d in res["detalle"] if not d["ok"])


def test_tope_de_importe_frena_la_corrida():
    filas = [_fila(fact_num=n, im_numero=f"CMTR-{n}",
                   importe_sugerido=200_000.0) for n in range(1, 6)]
    res = _plan(filas)
    assert res["creadas"] == 2  # 400k entra, el tercero pasaría los 500k
    assert res["importe"] == 400_000.0


def test_asinfo_mudo_no_carga_nada():
    assert _plan([])["creadas"] == 0


def test_antes_del_corte():
    assert svc._antes_del_corte("2026-06-30") is True
    assert svc._antes_del_corte("2026-07-01") is False   # corte INCLUSIVO
    assert svc._antes_del_corte(None) is True
    assert svc._antes_del_corte("no es fecha") is True


def test_corte_es_el_primero_de_julio():
    assert date(2026, 7, 1) == svc.CORTE


# ---------------------------------------------------------------------------
# Carga real (con crear() mockeado) + aviso
# ---------------------------------------------------------------------------
def test_carga_real_crea_la_compra_impaga_con_la_fecha_de_recepcion():
    creadas = []

    def _fake_crear(**kw):
        creadas.append(kw)
        return {"numero": 999}

    with patch.object(svc, "compras_locales_con_cruce", return_value=[_fila()]), \
         patch("modules.compras.queries.crear", side_effect=_fake_crear), \
         patch.object(asinfo_service, "reset_locales_cache"), \
         patch("modules.avisos.avisar", return_value=True) as av:
        res = svc.cargar_pendientes(usuario="test")

    assert res["creadas"] == 1
    (kw,) = creadas
    assert kw["tipo"] == "H"
    assert kw["codigo_prov"] == "HY"
    # El pasivo nace al RECIBIR, no con la fecha de la factura (dueña 30/07).
    assert kw["fecha"] == date(2026, 7, 27)
    # El concepto es el nº de factura pelado: la misma clave con la que la
    # pantalla vuelve a encontrar la compra (y la que escribe el FoxPro).
    assert kw["concepto"] == "37711"
    assert kw["kg"] == 5002.5
    # No se pasa `pagada` → crear() la deja impaga y arma el posdat = el pasivo.
    assert "pagada" not in kw or kw["pagada"] is False
    assert av.called


def test_una_factura_que_falla_no_corta_el_lote():
    def _crear(**kw):
        if kw["concepto"] == "1":
            raise ValueError("período cerrado")
        return {"numero": 1}

    filas = [_fila(fact_num=1, im_numero="CMTR-1"),
             _fila(fact_num=2, im_numero="CMTR-2")]
    with patch.object(svc, "compras_locales_con_cruce", return_value=filas), \
         patch("modules.compras.queries.crear", side_effect=_crear), \
         patch.object(asinfo_service, "reset_locales_cache"), \
         patch("modules.avisos.avisar", return_value=True):
        res = svc.cargar_pendientes(usuario="test")

    assert res["creadas"] == 1
    assert res["salteadas"] == 1
    assert "período cerrado" in res["detalle"][0]["motivo"]


def test_switch_de_ambiente_apaga_el_automatico(monkeypatch):
    monkeypatch.setenv("HILO_LOCAL_AUTO", "0")
    assert svc.correr_si_toca() == {"corrio": False, "creadas": 0, "importe": 0.0}


def test_freno_de_30_minutos(monkeypatch):
    monkeypatch.setenv("HILO_LOCAL_AUTO", "1")
    monkeypatch.setattr(svc, "_auto_ultimo_ts", 0.0)
    with patch.object(svc, "cargar_pendientes",
                      return_value={"creadas": 0, "importe": 0.0}) as cp:
        assert svc.correr_si_toca()["corrio"] is True
        assert svc.correr_si_toca()["corrio"] is False   # frenada
    assert cp.call_count == 1


def test_correr_si_toca_nunca_levanta(monkeypatch):
    monkeypatch.setenv("HILO_LOCAL_AUTO", "1")
    monkeypatch.setattr(svc, "_auto_ultimo_ts", 0.0)
    with patch.object(svc, "cargar_pendientes", side_effect=RuntimeError("boom")):
        assert svc.correr_si_toca()["creadas"] == 0


# ---------------------------------------------------------------------------
# Buscador de /importaciones — varias palabras
# ---------------------------------------------------------------------------
def _buscar(q, rows):
    """Réplica del filtro de la vista (AND entre términos, OR entre campos)."""
    terminos = q.upper().split()

    def _m(r):
        campos = " ".join((
            r.get("proveedor") or "", r.get("nota") or "", r.get("codigo") or "",
            r.get("im_numero") or "", r.get("prov") or "",
            r.get("numero_factura") or "",
        )).upper()
        return all(t in campos for t in terminos)

    return [r for r in rows if _m(r)]


FILAS_BUSCADOR = [
    {"im_numero": "CMTR-06995", "prov": "HY", "codigo": "HY 37711",
     "proveedor": "HILTEXPOY S.A.", "nota": "HY HG75/72 F-37711",
     "numero_factura": "001-002-000037711"},
    {"im_numero": "IM-0000625", "prov": "AC", "codigo": "AC 29",
     "proveedor": "ARIESCOPE", "nota": "ACMT/EXP/2026-27/8140 ( AC 29)",
     "numero_factura": ""},
]


def test_buscador_dos_palabras_cruza_campos_distintos():
    # "CMTR" vive en el número y "HY" en el código: ningún campo suelto tiene
    # la cadena entera. Antes esto devolvía cero filas.
    r = _buscar("CMTR HY", FILAS_BUSCADOR)
    assert len(r) == 1
    assert r[0]["im_numero"] == "CMTR-06995"


def test_buscador_una_palabra_se_comporta_igual_que_antes():
    assert len(_buscar("AC", FILAS_BUSCADOR)) == 1
    assert len(_buscar("CMTR", FILAS_BUSCADOR)) == 1


def test_buscador_es_and_entre_terminos():
    assert _buscar("CMTR AC", FILAS_BUSCADOR) == []


# ---------------------------------------------------------------------------
# Estado de pago de las HISTÓRICAS del dBase (bug 30/07)
# ---------------------------------------------------------------------------
def test_historica_sin_posdat_abierto_figura_pagada():
    """El caso real HY 36880/36925.

    Se pagaron el 16/07 con un ND de Pichincha de 60.790,64, pero
    `compra.no_banco` seguía en 0 porque el sync del dBase está parado desde el
    10/07. La verdad la tiene scintela.posdat: si no hay posdatado abierto que
    represente la compra, está pagada — y eso es lo que ahora resuelve el SQL
    en la columna `pagada_legacy`.
    """
    est = svc._estado_pago([
        _compra(41970.55, tiene_posdat=False, legacy=True),
        _compra(20728.31, tiene_posdat=False, legacy=True),
    ])
    assert est["pagada"] is True
    assert est["saldo"] == 0.0


def test_historica_con_posdat_abierto_sigue_debiendo():
    # HY 37216 / 37231: sí tienen posdatado vivo → pagada_legacy False.
    est = svc._estado_pago([_compra(21297.47, tiene_posdat=False, legacy=False)])
    assert est["pagada"] is False
    assert est["saldo"] == 21297.47


def test_fila_anterior_al_corte_se_marca_como_historica():
    # La pantalla no la muestra como "sin cargar": el motor no la va a tocar y
    # scintela.compra tampoco la tiene (COMPRAS.DBF es rotativo ~3 meses).
    vieja = {**CRUDA, "fecha_recepcion": "2026-04-21", "fact_num": 36736}
    (f,) = _con_cruce(crudas=[vieja])
    assert f["pre_corte"] is True
    (g,) = _con_cruce()          # 2026-07-20, posterior al corte
    assert g["pre_corte"] is False


# ---------------------------------------------------------------------------
# El disparador es la RECEPCIÓN, no la factura (TMT 2026-09-16)
#
# Medido en /admin/debug-hilo-local: entre el BOD y su factura pasan ~21 h de
# mediana y hasta 5 días, y en esa ventana el hilo ya está en el stock del
# programa sin su pasivo. Estos tests fijan las tres piezas que lo evitan.
# ---------------------------------------------------------------------------
def test_el_bod_queda_estampado_en_el_comprobante():
    """Sin esto, la corrida siguiente no reconoce la entrega y la duplica."""
    from modules.compras import queries as compras_queries

    creadas = []

    def _crear(**kw):
        creadas.append(kw)
        return {"numero": 10365}

    with patch.object(svc, "compras_locales_con_cruce",
                      return_value=[_fila(bod="BOD-000002374")]), \
         patch.object(compras_queries, "crear", _crear), \
         patch.object(svc, "_avisar_carga", return_value=0), \
         patch.object(asinfo_service, "reset_locales_cache", lambda: None):
        res = svc.cargar_pendientes()

    assert res["creadas"] == 1
    assert creadas[0]["comprobante"] == "BOD-000002374"
    # y el concepto sigue siendo el nº de factura: la llave vieja no cambia
    assert creadas[0]["concepto"] == "37711"


def test_dos_entregas_de_la_misma_factura_son_dos_pasivos():
    """El caso que el cruce por nº de factura se comía.

    Asinfo parte una factura en N recepciones. Cruzando por (proveedor, nº de
    factura), la segunda entrega se ve como «ya tiene compra» y su plata no
    entra nunca. Cruzando por BOD, cada entrega es su propia fila.
    """
    cruda_a = {**CRUDA, "bod": "BOD-000002374", "kg": 300.0}
    cruda_b = {**CRUDA, "bod": "BOD-000002375", "kg": 199.14}
    # En PC ya está cargada SÓLO la primera entrega.
    ya_cargada = {"BOD-000002374": [_compra(1000.0, saldo=1000.0)]}
    with patch.object(asinfo_service, "compras_locales_asinfo",
                      return_value=[cruda_a, cruda_b]), \
         patch.object(q, "proveedores_por_ruc", return_value=POR_RUC), \
         patch.object(q, "listar_tarifas", return_value=TARIFA_HY), \
         patch.object(svc, "_buscar_por_bod", return_value=ya_cargada), \
         patch.object(svc, "_buscar_compras",
                      return_value={("HY", 37649): [_compra(1000.0)]}):
        filas = svc.compras_locales_con_cruce()

    por_bod = {f["bod"]: f for f in filas}
    assert por_bod["BOD-000002374"]["cruce_por"] == "bod"
    # La segunda NO cruza por BOD; sin la llave nueva habría cruzado por
    # factura y se habría dado por cargada.
    assert por_bod["BOD-000002375"]["cruce_por"] == "factura"


def test_recepcion_anulada_en_asinfo_no_se_carga():
    res = _plan([_fila(bod="BOD-000002374", anulada=True)])
    assert res["creadas"] == 0
    assert "anulada en Asinfo" in res["detalle"][0]["motivo"]


def test_entrega_que_crece_ajusta_el_importe():
    """Tamara 2026-09-16: «con cada entrega, y se ajusta»."""
    fila = _fila(
        bod="BOD-000002374", kg=5002.5, importe_sugerido=16683.34,
        cruce_por="bod",
        compra={"n": 1, "items": [{"id_compra": 720, "importe": 9000.0}]},
    )
    res = _plan([fila])
    assert res["creadas"] == 0
    assert res["ajustadas"] == 1
    d = res["detalle"][0]
    assert d["ok"] is True and d["ajuste"] is True
    assert d["importe_previo"] == 9000.0
    assert d["importe"] == 16683.34


def test_no_se_ajusta_lo_que_cruzo_por_numero_de_factura():
    """Una compra tipeada a mano (o que cubre varias entregas) no se toca."""
    fila = _fila(
        bod="BOD-000002374", importe_sugerido=16683.34, cruce_por="factura",
        compra={"n": 1, "items": [{"id_compra": 720, "importe": 9000.0}]},
    )
    res = _plan([fila])
    assert res["ajustadas"] == 0
    assert res["detalle"][0]["motivo"] == "ya tiene compra"


def test_no_se_ajusta_cuando_el_importe_no_cambio():
    fila = _fila(
        bod="BOD-000002374", importe_sugerido=16683.34, cruce_por="bod",
        compra={"n": 1, "items": [{"id_compra": 720, "importe": 16683.34}]},
    )
    res = _plan([fila])
    assert res["ajustadas"] == 0


def test_no_se_ajusta_cuando_el_bod_tiene_varias_compras():
    """Repartir entre dos no es nuestro: eso va a la alarma de duplicados."""
    fila = _fila(
        bod="BOD-000002374", importe_sugerido=16683.34, cruce_por="bod",
        compra={"n": 2, "items": [{"id_compra": 720, "importe": 9000.0},
                                  {"id_compra": 721, "importe": 7000.0}]},
    )
    res = _plan([fila])
    assert res["ajustadas"] == 0


# ---------------------------------------------------------------------------
# La alarma: kilos en bodega sin su deuda (Tamara 2026-09-16)
# ---------------------------------------------------------------------------
def _recibida(**kw):
    """Una fila de /importaciones ya cruzada, lista para `sin_pasivo`."""
    base = {
        "bod": "BOD-000002356", "prov": "QC", "proveedor": "QUIMICA COMERCIAL",
        "producto": "QCLY40", "fact_num": 1773, "kg": 1012.32,
        "fecha_recepcion": "2026-08-25", "recibida": True, "anulada": False,
        "pre_corte": False, "tarifa": None, "importe_sugerido": None,
        "compra": None, "pagada": False,
    }
    base.update(kw)
    return base


def test_sin_tarifa_queda_trabada_y_la_alarma_la_nombra():
    """El caso real del 16/09: QC 1.012 kg de tres semanas, sin pasivo."""
    (f,) = svc.sin_pasivo([_recibida()])
    assert f["bod"] == "BOD-000002356"
    assert f["kg"] == 1012.32
    assert "QC no tiene tarifa" in f["motivo"]
    assert "no está en Asinfo" in f["motivo"]


def test_con_la_factura_en_asinfo_no_esta_trabada():
    # Misma QC, pero ya con la plata de la factura: el motor la carga; no es
    # una alarma (y si el motor no corrió, el motivo lo dice).
    (f,) = svc.sin_pasivo([_recibida(importe_sugerido=7567.09, importe_de="asinfo")])
    assert "carga automática" in f["motivo"]
    assert f["dias"] >= 1


def test_ruc_que_no_mapea_dice_que_el_problema_es_el_proveedor():
    (f,) = svc.sin_pasivo([_recibida(prov=None, proveedor="COMERCIALIZADORA")])
    assert "RUC de Asinfo no coincide" in f["motivo"]


@pytest.mark.parametrize("cambio", [
    {"compra": {"n": 1}},          # ya tiene su deuda
    {"recibida": False},           # todavía no llegó
    {"anulada": True},             # se anuló en Asinfo
    {"kg": 0},                     # sin kilos
    {"pre_corte": True},           # anterior al corte: el motor no la mira
])
def test_la_alarma_no_grita_por_lo_que_no_es_un_problema(cambio):
    assert svc.sin_pasivo([_recibida(**cambio)]) == []


def test_la_alarma_espera_las_horas_antes_de_gritar():
    """Un ⚠ por la corrida que todavía no pasó entrena a ignorar el panel."""
    from filters import today_ec
    hoy = today_ec().isoformat()
    assert svc.sin_pasivo([_recibida(fecha_recepcion=hoy)]) == []


def test_health_marca_las_trabadas_con_los_kilos():
    with patch.object(svc, "compras_locales_con_cruce",
                      return_value=[_recibida()]), \
         patch.object(svc, "duplicadas", return_value=[]):
        h = svc.health()
    assert h["ok"] is False
    (a,) = h["alerts"]
    assert a["category"] == "hilo_local_sin_pasivo"
    # números como se leen acá: punto de miles, coma de decimales
    assert "1.012,32 kg" in a["msg"]
    # "hace N días" se mide contra hoy: el 16/09 (cuando se escribió el test)
    # eran 22 — fijarlos rompía la suite al día siguiente.
    from filters import today_ec
    dias = (today_ec() - date(2026, 8, 25)).days
    assert f"hace {dias} días" in a["msg"]
    assert len(h["stats"]["sin_pasivo"]) == 1


def test_health_con_asinfo_mudo_no_se_pone_rojo():
    """No poder mirar no es lo mismo que estar mal."""
    with patch.object(svc, "compras_locales_con_cruce", return_value=[]):
        h = svc.health()
    assert h["ok"] is True
    assert h["stats"]["sin_datos"]


def test_health_avisa_la_entrega_anulada_que_sigue_debiendo():
    fila = _recibida(anulada=True, compra={"n": 1, "ids": [720]},
                     importe_programa=3078.0, pagada=False)
    with patch.object(svc, "compras_locales_con_cruce", return_value=[fila]), \
         patch.object(svc, "duplicadas", return_value=[]):
        h = svc.health()
    cats = {a["category"] for a in h["alerts"]}
    assert "hilo_local_anulada_con_deuda" in cats


def test_una_anulada_ya_pagada_no_es_alarma():
    fila = _recibida(anulada=True, compra={"n": 1, "ids": [720]}, pagada=True)
    assert svc.anuladas_con_compra([fila]) == []
