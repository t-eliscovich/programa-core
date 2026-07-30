"""Tests del puente formulas_app → compras (modules/compras/formulas_bridge).

Sin DB real: se mockean `formulas_db` (lado formulas) y `db.fetch_all`
(lado PC). `queries.crear` se mockea para verificar los argumentos con los
que el puente daría de alta (y que genera el pasivo vía ese camino único).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from modules.compras import formulas_bridge as fb

# ── Normalización ───────────────────────────────────────────────────────────

def test_normalizar_factura_quita_ceros():
    assert fb.normalizar_factura("AQ", "0085") == "85"
    assert fb.normalizar_factura("PO", "0272") == "272"


def test_normalizar_factura_sey_completa_millar():
    # formulas trunca el prefijo: '2444' es la 22444 real
    assert fb.normalizar_factura("SY", "2444") == "22444"
    # con 5 dígitos ya viene completa
    assert fb.normalizar_factura("SY", "22380") == "22380"
    # con 3 dígitos no se puede saber → queda como está
    assert fb.normalizar_factura("SY", "859") == "859"


def test_normalizar_factura_qi_completa_prefijo():
    """QI (Q.S.I.) numera en 6 dígitos y el campo de formulas tiene 4:
    la 104904 del 28/07/2026 llegó como '4904'."""
    assert fb.normalizar_factura("QI", "4904") == "104904"
    assert fb.normalizar_factura("QI", "104250") == "104250"
    assert fb.normalizar_factura("QI", "265") == "265"


def test_prov_map_qsi_es_qi():
    assert fb.PROV_MAP["QSI"] == "QI"


def test_normalizar_factura_prov_sin_prefijo_no_toca():
    # un proveedor que numera corto NO lleva prefijo aunque tenga 4 dígitos
    assert fb.normalizar_factura("ES", "7197") == "7197"
    assert fb.normalizar_factura("NQ", "8426") == "8426"


def test_normalizar_factura_no_digito_queda():
    assert fb.normalizar_factura("AQ", "F26-27/125") == "F26-27/125"


def test_concepto_pc_formato_dbase():
    c = fb.concepto_pc("85", date(2026, 7, 1))
    assert len(c) == 15
    assert c.startswith("85 ")
    assert c.endswith(" 1")
    c2 = fb.concepto_pc("22521", date(2026, 7, 14))
    assert len(c2) == 15
    assert c2.startswith("22521")
    assert c2.endswith("14")


def test_concepto_pc_sin_fecha():
    assert fb.concepto_pc("85", None) == "85"


# ── estado_mes ──────────────────────────────────────────────────────────────

GRUPOS = [
    # cargada por número exacto (dBase la trae como '85            1')
    {"proveedor": "AVQ", "factura": "0085", "fecha": "2026-07-01",
     "kg": 990, "importe_siva": 3430.90},
    # cargada por sufijo + importe en rango (SY manual con IVA mixto)
    {"proveedor": "SEY", "factura": "1945", "fecha": "2026-07-01",
     "kg": 1300, "importe_siva": 910.0},
    # pendiente
    {"proveedor": "AVQ", "factura": "0127", "fecha": "2026-07-14",
     "kg": 1015, "importe_siva": 5391.45},
    # excluida (importación)
    {"proveedor": "COLO", "factura": "26-27/125", "fecha": "2026-07-09",
     "kg": 9000, "importe_siva": 228863.50},
    # sin mapear
    {"proveedor": "NUEVO", "factura": "1", "fecha": "2026-07-02",
     "kg": 10, "importe_siva": 100.0},
    # EMP → ES con IVA 0%
    {"proveedor": "EMP", "factura": "7197", "fecha": "2026-07-07",
     "kg": 25000, "importe_siva": 5000.0},
]

COMPRAS_PC = [
    {"id_compra": 1, "codigo_prov": "AQ", "importe": 3945.54,
     "concepto": "85            1", "usuario_crea": "formulas-auto",
     "usuario_modifica": None, "id_transaccion": None},
    # SY cargada a mano con el número completo y total real (entre s/IVA y c/IVA)
    {"id_compra": 2, "codigo_prov": "SY", "importe": 1014.50,
     "concepto": "21945         1", "usuario_crea": "andres",
     "usuario_modifica": None, "id_transaccion": None},
]


def _estado_mock():
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=GRUPOS), \
         patch.object(fb.db, "fetch_all", return_value=COMPRAS_PC):
        return fb.estado_mes(2026, 7)


def test_estado_mes_estados():
    est = _estado_mock()
    assert est["disponible"] is True
    por_factura = {(f.proveedor_formulas, f.factura_formulas): f
                   for f in est["filas"]}
    assert por_factura[("AVQ", "0085")].estado == "cargada"
    assert por_factura[("SEY", "1945")].estado == "cargada"
    assert por_factura[("AVQ", "0127")].estado == "pendiente"
    assert por_factura[("COLO", "26-27/125")].estado == "excluida"
    assert por_factura[("NUEVO", "1")].estado == "sin_mapear"
    assert por_factura[("EMP", "7197")].estado == "pendiente"


def test_estado_mes_iva_por_proveedor():
    est = _estado_mock()
    por = {(f.proveedor_formulas, f.factura_formulas): f for f in est["filas"]}
    # químicos: 15%
    assert por[("AVQ", "0127")].importe_con_iva == pytest.approx(6200.17, abs=0.01)
    # sal (ES): 0%
    fila_sal = por[("EMP", "7197")]
    assert fila_sal.iva_pct == 0.0
    assert fila_sal.importe_con_iva == pytest.approx(5000.0, abs=0.01)


def test_estado_mes_pendientes_y_total():
    est = _estado_mock()
    assert est["pendientes"] == 2  # AVQ 0127 + EMP 7197
    assert est["total_pendiente"] == pytest.approx(6200.17 + 5000.0, abs=0.02)


def test_estado_mes_no_matchea_por_importe_solo():
    """Regresión 2026-07-17: compras recurrentes repiten el monto exacto con
    facturas distintas (SOFTER FRESH 3.680, sal 5.000). Un match por importe
    solo marcaba 'cargada' una factura que NO estaba → pasivo faltante."""
    grupos = [
        # formulas 2521 (=22521), mismo monto que la 22385 ya cargada
        {"proveedor": "SEY", "factura": "2521", "fecha": "2026-07-14",
         "kg": 1000, "importe_siva": 3200.0},
        # sal: mismo monto 5000 que la 7052 ya cargada, factura distinta
        {"proveedor": "EMP", "factura": "7197", "fecha": "2026-07-07",
         "kg": 25000, "importe_siva": 5000.0},
    ]
    pc = [
        {"id_compra": 1, "codigo_prov": "SY", "importe": 3680.0,
         "concepto": "22385        26"},
        {"id_compra": 2, "codigo_prov": "ES", "importe": 5000.0,
         "concepto": "7052         25"},
    ]
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=grupos), \
         patch.object(fb.db, "fetch_all", return_value=pc):
        est = fb.estado_mes(2026, 7)
    assert all(f.estado == "pendiente" for f in est["filas"])


def test_estado_mes_sin_bridge():
    with patch.object(fb.formulas_db, "disponible", return_value=False):
        est = fb.estado_mes(2026, 7)
    assert est["disponible"] is False
    assert est["filas"] == []


# ── sincronizar_mes ─────────────────────────────────────────────────────────

def test_sincronizar_crea_solo_pendientes():
    llamadas = []

    def _crear(**kw):
        llamadas.append(kw)
        return {"id_compra": 99, "numero": 500 + len(llamadas)}

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=GRUPOS), \
         patch.object(fb.db, "fetch_all", return_value=COMPRAS_PC), \
         patch("modules.compras.queries.crear", side_effect=_crear):
        rep = fb.sincronizar_mes(2026, 7, usuario="formulas-test")

    assert rep["disponible"] is True
    assert len(rep["creadas"]) == 2
    assert rep["errores"] == []
    assert rep["ya_cargadas"] == 2
    por_prov = {c["proveedor"]: c for c in rep["creadas"]}
    assert set(por_prov) == {"AQ", "ES"}
    # argumentos del alta: tipo Q, clave F, concepto formato dBase, importe c/IVA
    kw_aq = next(k for k in llamadas if k["codigo_prov"] == "AQ")
    assert kw_aq["tipo"] == "Q"
    assert kw_aq["clave"] == "F"
    assert kw_aq["pagada"] is False
    assert kw_aq["fecha"] == date(2026, 7, 14)
    assert kw_aq["importe"] == pytest.approx(6200.17, abs=0.01)
    assert kw_aq["concepto"].startswith("127")
    assert kw_aq["concepto"].endswith("14")
    assert kw_aq["usuario"] == "formulas-test"
    kw_es = next(k for k in llamadas if k["codigo_prov"] == "ES")
    assert kw_es["importe"] == pytest.approx(5000.0, abs=0.01)


def test_sincronizar_un_error_no_frena_al_resto():
    intentos = []

    def _crear(**kw):
        intentos.append(kw)
        if kw["codigo_prov"] == "AQ":
            raise ValueError("proveedor bloqueado")
        return {"id_compra": 1, "numero": 1}

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=GRUPOS), \
         patch.object(fb.db, "fetch_all", return_value=COMPRAS_PC), \
         patch("modules.compras.queries.crear", side_effect=_crear):
        rep = fb.sincronizar_mes(2026, 7)

    assert len(intentos) == 2
    assert len(rep["creadas"]) == 1
    assert len(rep["errores"]) == 1
    assert rep["errores"][0]["proveedor"] == "AQ"


def test_sincronizar_sin_bridge_no_crea():
    with patch.object(fb.formulas_db, "disponible", return_value=False), \
         patch("modules.compras.queries.crear") as crear_mock:
        rep = fb.sincronizar_mes(2026, 7)
    assert rep["disponible"] is False
    crear_mock.assert_not_called()


def test_sincronizar_carga_tambien_las_de_hoy():
    """Dueña 2026-07-30: "dejá de poner topes que entorpecen". La factura del
    día en curso se carga igual; si después crece, se corrige (ver los tests
    de auto-corrección)."""
    llamadas = []

    def _crear(**kw):
        llamadas.append(kw)
        return {"id_compra": 1, "numero": 1}

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=GRUPOS), \
         patch.object(fb.db, "fetch_all", return_value=COMPRAS_PC), \
         patch("filters.today_ec", return_value=date(2026, 7, 14)), \
         patch("modules.compras.queries.crear", side_effect=_crear):
        rep = fb.sincronizar_mes(2026, 7)

    # AVQ 0127 es del 14/07 (= hoy simulado) → SÍ se carga
    assert any(k["codigo_prov"] == "AQ" and k["fecha"] == date(2026, 7, 14)
               for k in llamadas)
    assert len(rep["creadas"]) == 2
    assert "dejadas_para_manana" not in rep


# ── auto-corrección de importe ──────────────────────────────────────────────

_GRUPO_CRECIO = [
    {"proveedor": "AVQ", "factura": "0133", "fecha": "2026-07-17",
     "kg": 2370, "importe_siva": 8634.50},   # c/IVA = 9.929,67
]


def _pc(**kw):
    base = {"id_compra": 7, "codigo_prov": "AQ", "importe": 3779.00,
            "concepto": "133          17", "usuario_crea": "formulas-auto",
            "usuario_modifica": None, "id_transaccion": None}
    base.update(kw)
    return [base]


def _correr(grupos, pc):
    editadas, creadas = [], []

    def _editar(id_compra, **kw):
        editadas.append((id_compra, kw))
        return {"importe_previo": pc[0]["importe"],
                "importe_nuevo": kw.get("importe")}

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=grupos), \
         patch.object(fb.db, "fetch_all", return_value=pc), \
         patch("modules.compras.queries.editar", side_effect=_editar), \
         patch("modules.compras.queries.crear",
               side_effect=lambda **kw: creadas.append(kw) or {"numero": 1}):
        rep = fb.sincronizar_mes(2026, 7)
    return rep, editadas, creadas


def test_estado_mes_marca_ajustable_cuando_formulas_crecio():
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_GRUPO_CRECIO), \
         patch.object(fb.db, "fetch_all", return_value=_pc()):
        est = fb.estado_mes(2026, 7)
    fila = est["filas"][0]
    assert fila.estado == "cargada"
    assert fila.ajustable is True
    assert fila.id_compra_pc == 7
    assert fila.importe_pc == pytest.approx(3779.00)
    assert est["ajustables"] == 1


def test_sincronizar_corrige_el_importe_y_no_duplica():
    rep, editadas, creadas = _correr(_GRUPO_CRECIO, _pc())
    assert creadas == []                       # NO se crea una segunda compra
    assert len(editadas) == 1
    id_compra, kw = editadas[0]
    assert id_compra == 7
    assert kw["importe"] == pytest.approx(9929.67, abs=0.01)
    assert "formulas" in kw["observacion"]
    assert len(rep["ajustadas"]) == 1
    assert rep["ajustadas"][0]["factura"] == "133"


def test_no_corrige_si_alguien_la_edito_a_mano():
    """La corrección manual de una factura con IVA mixto no se pisa."""
    _, editadas, _ = _correr(_GRUPO_CRECIO, _pc(usuario_modifica="tamara"))
    assert editadas == []


def test_no_corrige_si_no_la_creo_el_puente():
    _, editadas, _ = _correr(_GRUPO_CRECIO, _pc(usuario_crea="dbf-import"))
    assert editadas == []


def test_no_corrige_si_ya_esta_pagada():
    _, editadas, _ = _correr(_GRUPO_CRECIO, _pc(id_transaccion=42))
    assert editadas == []


def test_no_corrige_si_el_importe_coincide():
    _, editadas, _ = _correr(_GRUPO_CRECIO, _pc(importe=9929.67))
    assert editadas == []


def test_editar_que_falla_no_frena_la_corrida():
    def _editar(id_compra, **kw):
        raise ValueError("compra bloqueada")

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_GRUPO_CRECIO), \
         patch.object(fb.db, "fetch_all", return_value=_pc()), \
         patch("modules.compras.queries.editar", side_effect=_editar):
        rep = fb.sincronizar_mes(2026, 7)
    assert rep["disponible"] is True
    assert rep["ajustadas"] == []


# ── hilo de fondo ───────────────────────────────────────────────────────────

def test_correr_si_toca_respeta_el_freno(monkeypatch):
    monkeypatch.setattr(fb, "_auto_ultimo_ts", 0.0)
    with patch.object(fb, "sincronizar_mes_actual",
                      return_value={"creadas": [], "ajustadas": []}) as sm:
        primera = fb.correr_si_toca()
        segunda = fb.correr_si_toca()
    assert primera["corrio"] is True
    assert segunda["corrio"] is False      # el freno de 30 min la frenó
    assert sm.call_count == 1


def test_correr_si_toca_apagado(monkeypatch):
    monkeypatch.setattr(fb, "_auto_ultimo_ts", 0.0)
    monkeypatch.setenv("FORMULAS_COMPRAS_AUTOSYNC", "0")
    with patch.object(fb, "sincronizar_mes_actual") as sm:
        assert fb.correr_si_toca()["corrio"] is False
    sm.assert_not_called()


def test_correr_si_toca_nunca_levanta(monkeypatch):
    monkeypatch.setattr(fb, "_auto_ultimo_ts", 0.0)
    with patch.object(fb, "sincronizar_mes_actual",
                      side_effect=RuntimeError("boom")):
        res = fb.correr_si_toca()
    assert res["corrio"] is True and res["creadas"] == 0


# ── autosync / hook del cron ────────────────────────────────────────────────

def test_autosync_habilitado_default_on(monkeypatch):
    monkeypatch.delenv("FORMULAS_COMPRAS_AUTOSYNC", raising=False)
    assert fb.autosync_habilitado() is True


@pytest.mark.parametrize("valor", ["0", "false", "OFF", "no"])
def test_autosync_apagable(monkeypatch, valor):
    monkeypatch.setenv("FORMULAS_COMPRAS_AUTOSYNC", valor)
    assert fb.autosync_habilitado() is False


def test_sincronizar_mes_actual_apagado(monkeypatch):
    monkeypatch.setenv("FORMULAS_COMPRAS_AUTOSYNC", "0")
    rep = fb.sincronizar_mes_actual()
    assert rep.get("apagado") is True
    assert rep["creadas"] == []


def test_sincronizar_mes_actual_nunca_levanta(monkeypatch):
    monkeypatch.delenv("FORMULAS_COMPRAS_AUTOSYNC", raising=False)
    with patch.object(fb, "sincronizar_mes", side_effect=RuntimeError("boom")):
        rep = fb.sincronizar_mes_actual()
    assert rep["disponible"] is False
    assert rep["errores"]


def test_contar_pendientes_fail_soft():
    with patch.object(fb, "estado_mes", side_effect=RuntimeError("boom")):
        assert fb.contar_pendientes_mes_actual() == 0


def test_contar_pendientes_ok():
    with patch.object(fb, "estado_mes", return_value={"pendientes": 3}):
        assert fb.contar_pendientes_mes_actual(hoy=date(2026, 7, 17)) == 3


# ── trabadas: sin número de factura + aviso ─────────────────────────────────

_SIN_NUM = [
    {"proveedor": "SEY", "factura": "", "fecha": "2026-07-23",
     "kg": 500, "importe_siva": 1550.0},
    {"proveedor": "QQQ", "factura": "77", "fecha": "2026-07-24",
     "kg": 10, "importe_siva": 100.0},
]


def _estado_sin_num():
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_SIN_NUM), \
         patch.object(fb.db, "fetch_all", return_value=[]):
        return fb.estado_mes(2026, 7)


def test_factura_sin_numero_no_se_carga():
    """Sin N° el puente no la puede reconocer después: la cargaría de nuevo en
    CADA corrida (ahora cada 30 min). Se frena."""
    est = _estado_sin_num()
    por = {f.proveedor_formulas: f for f in est["filas"]}
    assert por["SEY"].estado == "sin_numero"
    assert por["QQQ"].estado == "sin_mapear"
    assert est["trabadas"] == 2
    assert est["pendientes"] == 0

    creadas = []
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_SIN_NUM), \
         patch.object(fb.db, "fetch_all", return_value=[]), \
         patch("modules.compras.queries.crear",
               side_effect=lambda **kw: creadas.append(kw)):
        rep = fb.sincronizar_mes(2026, 7)
    assert creadas == []
    assert rep["creadas"] == []


def test_avisar_trabadas_un_aviso_por_proveedor():
    est = _estado_sin_num()
    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        n = fb.avisar_trabadas(est)
    assert n == 2
    titulos = sorted(a["titulo"] for a in puestos)
    assert titulos == ["Compra de químicos sin N° de factura: SEY",
                       "Proveedor de químicos sin reconocer: QQQ"]
    assert all(a["url"] == "/compras/desde-formulas" for a in puestos)
    assert all(a["nivel"] == "alerta" for a in puestos)
    # la clave lleva el conteo → si aparece otra factura, vuelve a avisar
    assert all(a["clave"].endswith(":1") for a in puestos)


def test_avisar_trabadas_sin_nada_no_avisa():
    with patch("modules.avisos.avisar") as av:
        assert fb.avisar_trabadas({"filas": []}) == 0
    av.assert_not_called()


def test_avisar_trabadas_nunca_levanta():
    with patch("modules.avisos.avisar", side_effect=RuntimeError("boom")):
        assert fb.avisar_trabadas(_estado_sin_num()) == 0


# ── ventana de match ±1 mes ─────────────────────────────────────────────────

def test_ventana_match_abarca_mes_anterior_y_siguiente():
    """La SEY 21859 está en formulas el 28/04 y en PC el 01/05: con la ventana
    del mes exacto el puente la daba por pendiente y la habría DUPLICADO."""
    capt = {}

    def _fetch(sql, params=None):
        capt["params"] = params
        return []

    with patch.object(fb.db, "fetch_all", side_effect=_fetch):
        fb._compras_pc_mes(2026, 4)
    assert capt["params"] == (date(2025, 10, 1), date(2026, 11, 1))


def test_ventana_match_cruza_el_anio():
    capt = {}
    with patch.object(fb.db, "fetch_all",
                      side_effect=lambda sql, params=None: capt.update(p=params) or []):
        fb._compras_pc_mes(2026, 12)
    assert capt["p"] == (date(2026, 6, 1), date(2027, 7, 1))


def test_no_da_pendiente_la_tipeada_dos_meses_despues():
    """SEY 21945: formulas 08/05, Andrés la tipeó en PC el 16/07. Con la
    ventana angosta salía "sin cargar" y el botón la habría duplicado."""
    grupos = [{"proveedor": "SEY", "factura": "1945", "fecha": "2026-05-08",
               "kg": 1300, "importe_siva": 910.0}]
    pc = [{"id_compra": 9, "codigo_prov": "SY", "importe": 1014.50,
           "concepto": "21945", "usuario_crea": "andres",
           "usuario_modifica": None, "id_transaccion": 77}]
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=grupos), \
         patch.object(fb.db, "fetch_all", return_value=pc):
        est = fb.estado_mes(2026, 5)
    assert est["filas"][0].estado == "cargada"
    assert est["pendientes"] == 0
    # importe distinto, pero NO la creó el puente ⇒ no se toca
    assert est["filas"][0].ajustable is False


def test_no_da_pendiente_la_que_esta_en_el_mes_siguiente():
    grupos = [{"proveedor": "SEY", "factura": "859", "fecha": "2026-04-28",
               "kg": 1000, "importe_siva": 3200.0}]
    pc = [{"id_compra": 5, "codigo_prov": "SY", "importe": 3680.0,
           "concepto": "21859        28", "usuario_crea": "dbf-import",
           "usuario_modifica": None, "id_transaccion": None}]
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=grupos), \
         patch.object(fb.db, "fetch_all", return_value=pc):
        est = fb.estado_mes(2026, 4)
    # '859' → sufijo de '21859' + importe en rango ⇒ ya está cargada
    assert est["filas"][0].estado == "cargada"
    assert est["pendientes"] == 0


# ── barrido histórico ───────────────────────────────────────────────────────

def test_meses_con_compras_parsea():
    with patch.object(fb.formulas_db, "fetch_all",
                      return_value=[{"mes": "2026-07"}, {"mes": "2026-04"},
                                    {"mes": None}]):
        assert fb.meses_con_compras() == [(2026, 7), (2026, 4)]


def test_estado_historico_excluye_el_mes_en_curso():
    def _estado(anio, mes):
        f = fb.FilaPuente("AVQ", "AQ", "6010", "6010", date(anio, mes, 24),
                          0, 7040.0, 0.15, 8096.0, "pendiente")
        return {"disponible": True, "filas": [f], "pendientes": 1,
                "total_pendiente": 8096.0, "ajustables": 0, "trabadas": 0,
                "total_trabado": 0.0}

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb, "meses_con_compras",
                      return_value=[(2026, 7), (2026, 5), (2026, 4)]), \
         patch.object(fb, "estado_mes", side_effect=_estado), \
         patch("filters.today_ec", return_value=date(2026, 7, 30)):
        hist = fb.estado_historico()
    assert [m["mes_str"] for m in hist["meses"]] == ["2026-05", "2026-04"]
    assert hist["facturas"] == 2
    assert hist["importe"] == pytest.approx(16192.0, abs=0.01)


def test_estado_historico_sin_bridge():
    with patch.object(fb.formulas_db, "disponible", return_value=False):
        assert fb.estado_historico()["disponible"] is False


def test_avisar_historico():
    hist = {"facturas": 9, "importe": 42126.74,
            "meses": [{"mes_str": "2026-04"}, {"mes_str": "2026-06"}]}
    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        assert fb.avisar_historico(hist) == 1
    assert "9 compras de químicos de meses anteriores" in puestos[0]["titulo"]
    assert puestos[0]["url"] == "/compras/desde-formulas/historico"
    assert puestos[0]["clave"].endswith(":9")


def test_avisar_historico_sin_nada():
    with patch("modules.avisos.avisar") as av:
        assert fb.avisar_historico({"facturas": 0, "meses": []}) == 0
    av.assert_not_called()


def test_avisar_historico_nunca_levanta():
    with patch("modules.avisos.avisar", side_effect=RuntimeError("boom")):
        assert fb.avisar_historico({"facturas": 1, "importe": 1.0,
                                    "meses": [{"mes_str": "2026-04"}]}) == 0
