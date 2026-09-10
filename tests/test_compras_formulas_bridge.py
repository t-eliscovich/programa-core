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
    # COLOURTEX (importación): entra como C2 con el 15% como todos — en
    # formulas se carga precio × 1,21 ÷ 1,15 (Andrés 10/09/2026)
    {"proveedor": "COLO", "factura": "26-27/125", "fecha": "2026-07-09",
     "kg": 9000, "importe_siva": 228863.50},
    # el código no entra en 3 letras: se traba
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
    colo = por_factura[("COLO", "26-27/125")]
    assert colo.estado == "pendiente"
    assert colo.proveedor_pc == "C2"
    assert colo.iva_pct == pytest.approx(0.15)
    assert colo.importe_con_iva == pytest.approx(228863.50 * 1.15, abs=0.01)
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
    assert est["pendientes"] == 3  # AVQ 0127 + EMP 7197 + COLO (C2, sin IVA)
    assert est["total_pendiente"] == pytest.approx(6200.17 + 5000.0 + round(228863.50 * 1.15, 2), abs=0.02)


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
    assert len(rep["creadas"]) == 3
    assert rep["errores"] == []
    assert rep["ya_cargadas"] == 2
    por_prov = {c["proveedor"]: c for c in rep["creadas"]}
    assert set(por_prov) == {"AQ", "ES", "C2"}
    # COLOURTEX entra como C2 con el 15% (el ×1,21 viene de formulas ÷ 1,15)
    kw_c2 = next(k for k in llamadas if k["codigo_prov"] == "C2")
    assert kw_c2["importe"] == pytest.approx(228863.50 * 1.15, abs=0.01)
    assert kw_c2["tipo"] == "Q"
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

    assert len(intentos) == 3
    assert len(rep["creadas"]) == 2
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
    assert len(rep["creadas"]) == 3
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
    assert rep["ajustadas"][0]["importe_previo"] == pytest.approx(3779.00)


# ── el aviso de corrección dice de cuánto era antes ─────────────────────────

def _aviso_ajuste(ajustadas):
    """Corre _avisar_novedades y devuelve los kwargs del aviso de ajuste."""
    with patch("modules.avisos.avisar") as av:
        fb._avisar_novedades([], [], ajustadas)
    return av.call_args_list[0].kwargs


def test_aviso_de_ajuste_muestra_el_importe_anterior_y_la_diferencia():
    kw = _aviso_ajuste([{"proveedor": "AQ", "factura": "174",
                         "importe_previo": 7608.97, "importe": 7708.97}])
    assert "antes $ 7.608,97" in kw["detalle"]
    assert "ahora $ 7.708,97" in kw["detalle"]
    assert "(+$ 100,00)" in kw["detalle"]
    assert "(+$ 100,00)" in kw["titulo"]


def test_aviso_de_ajuste_cuando_la_factura_bajo():
    kw = _aviso_ajuste([{"proveedor": "AQ", "factura": "174",
                         "importe_previo": 7708.97, "importe": 7608.97}])
    assert "(−$ 100,00)" in kw["detalle"]
    assert "+" not in kw["titulo"]


def test_aviso_de_ajuste_sin_previo_no_inventa_la_diferencia():
    """Si no sabemos de cuánto era, el aviso vuelve al texto de antes."""
    kw = _aviso_ajuste([{"proveedor": "AQ", "factura": "174",
                         "importe_previo": None, "importe": 7708.97}])
    assert kw["detalle"].endswith("174 · ahora $ 7.708,97")
    assert "antes" not in kw["detalle"]
    assert "$" not in kw["titulo"]


def test_aviso_de_ajuste_suma_varias_facturas():
    kw = _aviso_ajuste([
        {"proveedor": "AQ", "factura": "174",
         "importe_previo": 1000.00, "importe": 1100.00},
        {"proveedor": "AQ", "factura": "175",
         "importe_previo": 2000.00, "importe": 2400.00},
    ])
    assert "antes $ 3.000,00" in kw["detalle"]
    assert "ahora $ 3.500,00" in kw["detalle"]
    assert "(+$ 500,00)" in kw["detalle"]
    assert "2 compras" in kw["titulo"]


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
    # código de más de 3 letras: no hay cómo elegir el código de PC
    {"proveedor": "QQQQ", "factura": "77", "fecha": "2026-07-24",
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
    assert por["QQQQ"].estado == "sin_mapear"
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
                       "Proveedor de químicos con un código que no entra: QQQQ"]
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
                      return_value=[(2026, 10), (2026, 9), (2026, 5)]), \
         patch.object(fb, "estado_mes", side_effect=_estado), \
         patch("filters.today_ec", return_value=date(2026, 10, 5)):
        hist = fb.estado_historico()
    # 10 es el mes en curso (fuera) y 5 es anterior al piso (fuera)
    assert [m["mes_str"] for m in hist["meses"]] == ["2026-09"]
    assert hist["facturas"] == 1


def test_estado_historico_no_avisa_lo_anterior_al_piso():
    """Dueña 30/07: "lo anterior no importa, no vamos a mover ni mayo ni
    junio" — abril–junio quedan cerrados, no se listan ni se avisan."""
    def _estado(anio, mes):
        f = fb.FilaPuente("AVQ", "AQ", "6010", "6010", date(anio, mes, 24),
                          0, 7040.0, 0.15, 8096.0, "pendiente")
        return {"disponible": True, "filas": [f]}

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb, "meses_con_compras",
                      return_value=[(2026, 6), (2026, 5), (2026, 4)]), \
         patch.object(fb, "estado_mes", side_effect=_estado), \
         patch("filters.today_ec", return_value=date(2026, 7, 30)):
        hist = fb.estado_historico()
    assert hist["meses"] == []
    assert hist["facturas"] == 0


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


# ── proveedor nuevo: la compra se carga igual ───────────────────────────────
# TMT 2026-08-23 (dueña, mirando el aviso "Proveedor de químicos sin
# reconocer: NSQ"): *"se debería cargar la compra igual y pedir después cargar
# el proveedor, ponerle el nombre con el que viene"*.

_NSQ = [
    {"proveedor": "NSQ", "factura": "0032", "fecha": "2026-08-22",
     "kg": 1000, "importe_siva": 3470.0},
]


def _estado_nsq(maestro=None):
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_NSQ), \
         patch.object(fb.db, "fetch_all", return_value=[]), \
         patch.object(fb, "proveedores_pc", return_value=maestro or {}):
        return fb.estado_mes(2026, 8)


def test_resolver_prov_los_cuatro_caminos():
    maestro = {"NQ": "ANDESCHEMIE"}
    assert fb.resolver_prov("SEY", maestro) == ("SY", "mapeado")
    assert fb.resolver_prov("NQ", maestro) == ("NQ", "mapeado")
    # no está en el mapping pero el mismo código existe en el maestro
    assert fb.resolver_prov("PQ", {"PQ": "PROQUIMSA"}) == ("PQ", "maestro")
    # no está en ningún lado y entra en 3 letras → se da de alta
    assert fb.resolver_prov("NSQ", maestro) == ("NSQ", "nuevo")
    # más de 3 letras: codigo_prov es varchar(3), elegirlo sería adivinar
    assert fb.resolver_prov("NUEVO", maestro) == (None, "sin_codigo")
    assert fb.resolver_prov("", maestro) == (None, "sin_codigo")


def test_proveedor_nuevo_ya_no_traba_la_compra():
    """Antes salía 'sin mapear' y el pasivo quedaba de menos hasta que alguien
    tocara el código."""
    est = _estado_nsq()
    fila = est["filas"][0]
    assert fila.estado == "pendiente"
    assert fila.proveedor_pc == "NSQ"
    assert fila.prov_origen == "nuevo"
    assert est["trabadas"] == 0
    assert est["proveedores_nuevos"] == ["NSQ"]
    # y con el IVA que le toca, no el 0% que mostraba la fila trabada
    assert fila.iva_pct == 0.15
    assert fila.importe_con_iva == pytest.approx(3990.50, abs=0.01)


def test_proveedor_nuevo_se_da_de_alta_con_el_nombre_con_el_que_viene():
    creadas, altas = [], []
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_NSQ), \
         patch.object(fb.db, "fetch_all", return_value=[]), \
         patch.object(fb, "proveedores_pc", return_value={}), \
         patch("modules.proveedores.queries.crear",
               side_effect=lambda **kw: altas.append(kw)), \
         patch("modules.compras.queries.crear",
               side_effect=lambda **kw: creadas.append(kw) or {"numero": 9}), \
         patch("modules.avisos.avisar", return_value=True):
        rep = fb.sincronizar_mes(2026, 8)
    assert altas == [{"codigo_prov": "NSQ", "nombre": "NSQ",
                      "usuario": "formulas-auto"}]
    assert len(creadas) == 1
    assert creadas[0]["codigo_prov"] == "NSQ"
    assert creadas[0]["tipo"] == "Q"
    assert creadas[0]["importe"] == pytest.approx(3990.50, abs=0.01)
    assert rep["proveedores"] == [{"codigo": "NSQ", "prov_formulas": "NSQ"}]


def test_dos_facturas_del_mismo_proveedor_nuevo_lo_dan_de_alta_una_sola_vez():
    dos = _NSQ + [{"proveedor": "NSQ", "factura": "0033", "fecha": "2026-08-23",
                   "kg": 500, "importe_siva": 1735.0}]
    altas, creadas = [], []
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=dos), \
         patch.object(fb.db, "fetch_all", return_value=[]), \
         patch.object(fb, "proveedores_pc", return_value={}), \
         patch("modules.proveedores.queries.crear",
               side_effect=lambda **kw: altas.append(kw)), \
         patch("modules.compras.queries.crear",
               side_effect=lambda **kw: creadas.append(kw)), \
         patch("modules.avisos.avisar", return_value=True):
        fb.sincronizar_mes(2026, 8)
    assert len(altas) == 1
    assert len(creadas) == 2


def test_si_el_alta_del_proveedor_falla_la_compra_no_se_carga():
    """Cargarla con un código que no existe dejaría una compra huérfana."""
    creadas = []
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_NSQ), \
         patch.object(fb.db, "fetch_all", return_value=[]), \
         patch.object(fb, "proveedores_pc", return_value={}), \
         patch("modules.proveedores.queries.crear",
               side_effect=RuntimeError("la base dijo que no")), \
         patch("modules.compras.queries.crear",
               side_effect=lambda **kw: creadas.append(kw)), \
         patch("modules.avisos.avisar", return_value=True):
        rep = fb.sincronizar_mes(2026, 8)
    assert creadas == []
    assert rep["proveedores"] == []
    assert "no se pudo dar de alta el proveedor" in rep["errores"][0]["error"]


def test_el_codigo_que_ya_existe_en_el_maestro_se_usa():
    """Dueña 2026-08-23: si el código ya está, es ése — y el aviso dice cuál."""
    est = _estado_nsq(maestro={"NSQ": "NUEVA SOLUCION QUIMICA"})
    fila = est["filas"][0]
    assert fila.proveedor_pc == "NSQ"
    assert fila.prov_origen == "maestro"
    assert fila.prov_nombre_pc == "NUEVA SOLUCION QUIMICA"
    assert est["proveedores_nuevos"] == []


def test_aviso_del_proveedor_nuevo_manda_a_ponerle_el_nombre():
    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        n = fb.avisar_proveedores(
            [{"codigo": "NSQ", "prov_formulas": "NSQ"}],
            [{"proveedor": "NSQ", "factura": "32", "importe": 3990.50,
              "prov_origen": "nuevo", "prov_formulas": "NSQ"}])
    assert n == 1
    assert puestos[0]["titulo"] == "Proveedor de químicos nuevo: NSQ"
    assert puestos[0]["url"] == "/proveedores/NSQ/editar"
    assert "3.990,50" in puestos[0]["detalle"]
    assert "nombre" in puestos[0]["detalle"]


def test_aviso_del_proveedor_que_ya_existia_dice_bajo_cual_quedo():
    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        n = fb.avisar_proveedores(
            [],
            [{"proveedor": "PQ", "factura": "5", "importe": 115.0,
              "prov_origen": "maestro", "prov_formulas": "PQ",
              "prov_nombre_pc": "PROQUIMSA"}])
    assert n == 1
    assert puestos[0]["titulo"] == "Químicos: PQ se cargó como PQ"
    assert "PROQUIMSA" in puestos[0]["detalle"]


def test_avisar_proveedores_sin_nada_no_avisa():
    with patch("modules.avisos.avisar") as av:
        assert fb.avisar_proveedores([], []) == 0
    av.assert_not_called()


def test_avisar_proveedores_nunca_levanta():
    with patch("modules.avisos.avisar", side_effect=RuntimeError("boom")):
        assert fb.avisar_proveedores([{"codigo": "NSQ"}], []) == 0


# ── deshacer: la factura desapareció o le cambiaron el N° (09/09/2026) ──────
#
# El caso real: Jonathan cargó una importación de colorantes en formulas como
# AVQ "007-2026" (273.565,79 c/IVA); el puente la cargó. Después le cambió el
# número a "26-27/414" → el puente cargó OTRA. Después borró los renglones →
# las dos quedaron vivas con su pasivo y sin stock. Dueña: "no puede quedar el
# pasivo vivo si se reversó".

_G_IMPORTACION = [
    {"proveedor": "AVQ", "factura": "26-27/414", "fecha": "2026-09-09",
     "kg": 22850, "importe_siva": 237883.30},   # c/IVA = 273.565,79
]
_G_OTRA = [
    {"proveedor": "SEY", "factura": "3053", "fecha": "2026-09-09",
     "kg": 1000, "importe_siva": 3200.0},
]


def _pc_puente(**kw):
    base = {"id_compra": 678, "numero": 10323, "fecha": date(2026, 9, 9),
            "codigo_prov": "AQ", "importe": 273565.79,
            "concepto": "007-2026      9", "usuario_crea": "formulas-auto",
            "usuario_modifica": None, "id_transaccion": None,
            "cuenta_pagada": None}
    base.update(kw)
    return base


def _correr_deshacer(grupos, pc, responde=True, anular=None):
    editadas, creadas, anuladas = [], [], []

    def _anular(id_compra, **kw):
        anuladas.append((id_compra, kw))
        if anular:
            anular(id_compra, kw)
        return 1

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=grupos), \
         patch.object(fb.formulas_db, "fetch_one",
                      return_value={"n": 1} if responde else None), \
         patch.object(fb.db, "fetch_all", return_value=pc), \
         patch("modules.compras.queries.editar",
               side_effect=lambda i, **kw: editadas.append((i, kw)) or {}), \
         patch("modules.compras.queries.crear",
               side_effect=lambda **kw: creadas.append(kw) or {"numero": 1}), \
         patch("modules.compras.queries.anular", side_effect=_anular), \
         patch("modules.avisos.avisar", return_value=True):
        rep = fb.sincronizar_mes(2026, 9)
    return rep, editadas, creadas, anuladas


def test_estado_mes_lista_la_compra_del_puente_que_ya_no_esta_en_formulas():
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_G_OTRA), \
         patch.object(fb.db, "fetch_all", return_value=[_pc_puente()]):
        est = fb.estado_mes(2026, 9)
    assert len(est["huerfanas"]) == 1
    h = est["huerfanas"][0]
    assert h["id_compra"] == 678 and h["numero"] == 10323
    assert h["proveedor"] == "AQ" and h["factura"] == "007-2026"
    assert h["fecha"] == date(2026, 9, 9)
    assert est["total_huerfano"] == pytest.approx(273565.79)


def test_sincronizar_anula_la_compra_cuya_factura_desaparecio():
    rep, editadas, creadas, anuladas = _correr_deshacer(_G_OTRA, [_pc_puente()])
    assert editadas == []
    assert [c["codigo_prov"] for c in creadas] == ["SY"]   # la 3053 sí se carga
    assert len(anuladas) == 1
    id_compra, kw = anuladas[0]
    assert id_compra == 678
    assert kw["usuario"] == "formulas-auto"
    assert "007-2026" in kw["motivo"] and "tintorería" in kw["motivo"]
    assert [a["id_compra"] for a in rep["anuladas"]] == [678]
    assert rep["errores"] == []


@pytest.mark.parametrize("cambio", [
    {"cuenta_pagada": "B"},                 # pagada
    {"id_transaccion": 55},                 # pagada al instante
    {"usuario_modifica": "andres"},         # alguien la editó a mano
    {"usuario_crea": "andres"},             # no la creó el puente
    {"fecha": date(2026, 8, 30)},           # de otro mes
])
def test_no_se_anula_lo_que_no_es_del_puente_intacto_de_este_mes(cambio):
    rep, _, _, anuladas = _correr_deshacer(_G_OTRA, [_pc_puente(**cambio)])
    assert anuladas == []
    assert rep["anuladas"] == []


def test_la_que_formulas_si_tiene_no_es_huerfana():
    pc = [_pc_puente(concepto="26-27/414     9")]
    rep, _, creadas, anuladas = _correr_deshacer(_G_IMPORTACION, pc)
    assert anuladas == [] and creadas == []
    assert rep["ya_cargadas"] == 1


def test_formulas_caida_no_anula_nada():
    """fetch_all devuelve [] tanto si el mes está vacío como si la base no
    contesta. Con la base caída NO se puede dar por desaparecida ninguna
    factura: anularía el pasivo del mes entero."""
    rep, _, _, anuladas = _correr_deshacer([], [_pc_puente()], responde=False)
    assert anuladas == [] and rep["anuladas"] == []


def test_mes_vacio_con_formulas_respondiendo_si_anula():
    """Si formulas contesta y el mes está vacío, las facturas de verdad se
    borraron: se anulan."""
    rep, _, _, anuladas = _correr_deshacer([], [_pc_puente()], responde=True)
    assert [i for i, _ in anuladas] == [678]


def test_cambio_de_numero_renombra_en_vez_de_cargar_otra():
    """'007-2026' → '26-27/414' en formulas: misma fecha, mismo proveedor,
    mismo importe. Se edita el concepto de la que está; ni se crea otra ni se
    anula la vieja."""
    rep, editadas, creadas, anuladas = _correr_deshacer(_G_IMPORTACION,
                                                        [_pc_puente()])
    assert creadas == [] and anuladas == []
    assert len(editadas) == 1
    id_compra, kw = editadas[0]
    assert id_compra == 678
    assert kw["concepto"].startswith("26-27/414")
    assert kw["concepto"].endswith(" 9")
    assert "007-2026" in kw["observacion"]
    assert rep["renombradas"][0]["factura_previa"] == "007-2026"
    assert rep["renombradas"][0]["factura"] == "26-27/414"


def test_estado_mes_marca_la_renombrada_como_cargada():
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_G_IMPORTACION), \
         patch.object(fb.db, "fetch_all", return_value=[_pc_puente()]):
        est = fb.estado_mes(2026, 9)
    f = est["filas"][0]
    assert f.estado == "cargada" and f.renombrar
    assert f.factura_previa == "007-2026" and f.id_compra_pc == 678
    assert est["pendientes"] == 0 and est["renombradas"] == 1
    assert est["huerfanas"] == []


def test_distinto_importe_no_es_renombre():
    """Mismo día y proveedor pero otro importe: son dos facturas distintas —
    la nueva se carga y la vieja, que ya no está, se anula."""
    pc = [_pc_puente(importe=1000.0)]
    rep, editadas, creadas, anuladas = _correr_deshacer(_G_IMPORTACION, pc)
    assert editadas == []
    assert len(creadas) == 1 and creadas[0]["codigo_prov"] == "AQ"
    assert [i for i, _ in anuladas] == [678]


def test_si_anular_falla_queda_en_errores_y_sigue():
    pc = [_pc_puente(), _pc_puente(id_compra=680, numero=10325,
                                   concepto="26-27/414     9")]

    def _boom(id_compra, kw):
        if id_compra == 678:
            raise ValueError("la posdat hermana ya fue pagada con cheque")

    rep, _, _, anuladas = _correr_deshacer(_G_OTRA, pc, anular=_boom)
    assert [i for i, _ in anuladas] == [678, 680]   # se intentaron las dos
    assert [a["id_compra"] for a in rep["anuladas"]] == [680]
    assert len(rep["errores"]) == 1
    assert "cheque" in rep["errores"][0]["error"]
    assert rep["errores"][0]["factura"] == "007-2026"


def test_tope_de_anulaciones_por_corrida(monkeypatch):
    monkeypatch.setattr(fb, "_MAX_ANULAR_POR_CORRIDA", 2)
    pc = [_pc_puente(id_compra=i, numero=100 + i, concepto=f"{i}  9")
          for i in range(1, 5)]
    rep, _, _, anuladas = _correr_deshacer(_G_OTRA, pc)
    assert len(anuladas) == 2
    assert len(rep["anuladas"]) == 2 and len(rep["sin_anular"]) == 2


def test_aviso_de_anuladas_dice_cuales_y_cuanto():
    puestos = []
    with patch("modules.avisos.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        fb._avisar_deshechas(
            [{"proveedor": "AQ", "factura": "26-27/414",
              "factura_previa": "007-2026", "importe": 273565.79,
              "id_compra": 678}],
            [{"id_compra": 680, "proveedor": "AQ", "factura": "26-27/414",
              "importe": 273565.79, "fecha": date(2026, 9, 9), "numero": 10325}],
            [])
    assert len(puestos) == 2
    ren, anu = puestos
    assert "N° de factura cambiado" in ren["titulo"]
    assert "007-2026 → 26-27/414" in ren["detalle"]
    assert anu["nivel"] == "alerta"
    assert anu["titulo"] == "Químicos · se anuló 1 compra por $ 273.565,79"
    assert "AQ 26-27/414" in anu["detalle"] and "pasivo" in anu["detalle"]
    assert anu["url"] == "/compras"


def test_aviso_de_deshechas_sin_nada_no_avisa_y_nunca_levanta():
    with patch("modules.avisos.avisar") as av:
        fb._avisar_deshechas([], [], [])
    av.assert_not_called()
    with patch("modules.avisos.avisar", side_effect=RuntimeError("boom")):
        fb._avisar_deshechas([], [{"id_compra": 1, "proveedor": "AQ",
                                   "factura": "1", "importe": 1.0}], [])


def test_formulas_responde_ante_la_duda_dice_que_no():
    with patch.object(fb.formulas_db, "fetch_one", side_effect=RuntimeError("x")):
        assert fb._formulas_responde() is False
    with patch.object(fb.formulas_db, "fetch_one", return_value={"n": 0}):
        assert fb._formulas_responde() is True


def test_si_renombrar_falla_queda_en_errores_y_no_se_carga_otra():
    editadas, creadas = [], []
    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=_G_IMPORTACION), \
         patch.object(fb.db, "fetch_all", return_value=[_pc_puente()]), \
         patch("modules.compras.queries.editar",
               side_effect=ValueError("Compra ya pagada")), \
         patch("modules.compras.queries.crear",
               side_effect=lambda **kw: creadas.append(kw) or {"numero": 1}), \
         patch("modules.compras.queries.anular") as anular, \
         patch("modules.avisos.avisar", return_value=True):
        rep = fb.sincronizar_mes(2026, 9)
    assert creadas == [] and editadas == []
    anular.assert_not_called()
    assert rep["renombradas"] == []
    assert len(rep["errores"]) == 1
    assert "cambiar el N°" in rep["errores"][0]["error"]


# ── el vínculo por id (mig 0247, dueña 10/09: "tienen que tener id único") ──

_G_POR_ID = [
    {"proveedor": "AVQ", "factura": "26-27/414", "fecha": "2026-09-09",
     "kg": 22850, "importe_siva": 237883.30, "ids": [901, 902, 903]},
]


def _correr_con_vinculos(grupos, pc, vinculos, responde=True):
    editadas, creadas, anuladas, vinculados = [], [], [], []

    def _crear(**kw):
        creadas.append(kw)
        return {"id_compra": 999, "numero": 1}

    with patch.object(fb.formulas_db, "disponible", return_value=True), \
         patch.object(fb.formulas_db, "fetch_all", return_value=grupos), \
         patch.object(fb.formulas_db, "fetch_one",
                      return_value={"n": 1} if responde else None), \
         patch.object(fb.db, "fetch_all", return_value=pc), \
         patch.object(fb, "_vinculos", return_value=vinculos), \
         patch.object(fb, "vincular",
                      side_effect=lambda i, ids: vinculados.append((i, tuple(ids))) or len(ids)), \
         patch("modules.compras.queries.editar",
               side_effect=lambda i, **kw: editadas.append((i, kw)) or {}), \
         patch("modules.compras.queries.crear", side_effect=_crear), \
         patch("modules.compras.queries.anular",
               side_effect=lambda i, **kw: anuladas.append(i) or 1), \
         patch("modules.avisos.avisar", return_value=True):
        rep = fb.sincronizar_mes(2026, 9)
    return rep, editadas, creadas, anuladas, vinculados


def test_al_crear_se_guardan_los_ids_de_formulas():
    rep, _, creadas, _, vinculados = _correr_con_vinculos(_G_POR_ID, [], {})
    assert len(creadas) == 1
    assert vinculados == [(999, (901, 902, 903))]


def test_por_id_reconoce_la_compra_aunque_cambien_numero_y_fecha():
    """La compra 678 quedó cargada como '007-2026' del 9/9. En formulas le
    cambiaron el número a '26-27/414' y la fecha al 10/9: por los ids sigue
    siendo la misma → se renombra el concepto, no se carga otra."""
    g = [dict(_G_POR_ID[0], fecha="2026-09-10")]
    pc = [_pc_puente()]
    vinc = {901: 678, 902: 678, 903: 678}
    rep, editadas, creadas, anuladas, _ = _correr_con_vinculos(g, pc, vinc)
    assert creadas == [] and anuladas == []
    assert len(editadas) == 1
    assert editadas[0][0] == 678
    assert editadas[0][1]["concepto"].startswith("26-27/414")
    assert rep["renombradas"][0]["factura_previa"] == "007-2026"


def test_por_id_reconoce_el_cambio_de_producto_como_ajuste_de_importe():
    """Se confundieron de producto y editaron el renglón: el total cambió.
    Misma compra (por ids), importe corregido, nada duplicado."""
    g = [dict(_G_POR_ID[0], importe_siva=200000.0)]   # c/IVA 230.000
    pc = [_pc_puente(concepto="26-27/414     9")]
    vinc = {901: 678, 902: 678, 903: 678}
    rep, editadas, creadas, anuladas, _ = _correr_con_vinculos(g, pc, vinc)
    assert creadas == [] and anuladas == []
    assert len(editadas) == 1 and editadas[0][0] == 678
    assert editadas[0][1]["importe"] == pytest.approx(230000.0)
    assert rep["ajustadas"][0]["importe_previo"] == pytest.approx(273565.79)


def test_compra_vieja_sin_vinculo_se_reconoce_por_numero_y_se_vincula():
    pc = [_pc_puente(concepto="26-27/414     9")]
    rep, editadas, creadas, _, vinculados = _correr_con_vinculos(_G_POR_ID, pc, {})
    assert creadas == [] and editadas == []
    assert vinculados == [(678, (901, 902, 903))]
    assert rep["ya_cargadas"] == 1


def test_huerfana_por_id_es_exacta_sus_renglones_ya_no_estan():
    pc = [_pc_puente()]
    vinc = {901: 678, 902: 678}
    otra = [{"proveedor": "SEY", "factura": "3053", "fecha": "2026-09-09",
             "kg": 1000, "importe_siva": 3200.0, "ids": [950]}]
    rep, _, _, anuladas, _ = _correr_con_vinculos(otra, pc, vinc)
    assert anuladas == [678]


def test_si_algun_renglon_sigue_en_formulas_no_es_huerfana():
    """Movieron dos de los tres renglones a otra factura: la compra 678 no
    matchea (la nueva factura tiene más renglones nuevos), pero sus ids
    siguen vivos → no se anula sola; queda para mirar."""
    pc = [_pc_puente(), _pc_puente(id_compra=700, numero=10400,
                                   concepto="500           9")]
    g = [{"proveedor": "AVQ", "factura": "500", "fecha": "2026-09-09",
          "kg": 100, "importe_siva": 100.0, "ids": [901, 902, 960, 961, 962]}]
    vinc = {901: 678, 902: 678, 960: 700, 961: 700, 962: 700}
    rep, _, creadas, anuladas, _ = _correr_con_vinculos(g, pc, vinc)
    assert creadas == [] and anuladas == []


def test_match_por_vinculo_gana_la_compra_con_mas_renglones():
    por_id = {1: {"id_compra": 1}, 2: {"id_compra": 2}}
    hit = fb._match_por_vinculo((10, 11, 12), {10: 1, 11: 2, 12: 2}, por_id, "X")
    assert hit[1]["id_compra"] == 2 and hit[0].startswith("ids 2/3")
    assert fb._match_por_vinculo((), {}, por_id, "X") is None
    assert fb._match_por_vinculo((5,), {5: 9}, por_id, "X") is None  # 9 no está en el mes


def test_vinculos_lee_la_tabla_y_tolera_filas_ajenas():
    with patch.object(fb.db, "fetch_all",
                      return_value=[{"formulas_id": 7, "id_compra": 3},
                                    {"id_compra": 3, "importe": 1}]):
        assert fb._vinculos([{"id_compra": 3}]) == {7: 3}
    assert fb._vinculos([]) == {}


def test_vincular_inserta_cada_id_y_nunca_levanta():
    llamadas = []
    with patch.object(fb.db, "execute",
                      side_effect=lambda sql, p: llamadas.append(p) or 1):
        assert fb.vincular(678, (901, 902)) == 2
    assert llamadas == [(901, 678), (902, 678)]
    with patch.object(fb.db, "execute", side_effect=RuntimeError("boom")):
        assert fb.vincular(678, (901,)) == 0
