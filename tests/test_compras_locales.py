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


def test_sin_tarifa_no_hay_importe_sugerido():
    (f,) = _con_cruce(tarifas=[])
    assert f["tarifa"] is None
    assert f["importe_sugerido"] is None


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
        ({"tarifa": None, "importe_sugerido": None}, "falta tarifa"),
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
