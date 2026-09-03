"""`/admin/health/arranque-de-mes` — las cinco fallas del 01/09/2026, vigiladas.

El cambio de mes del 01/09/2026 rompió cinco cosas y ninguna la detectó una
alarma; se supo porque Andrés reclamó por WhatsApp. Tamara: *"tener el cierre
de mes aceitado, que nunca más nos vuelva a pasar"*. Cada test de acá es uno
de esos dry runs: los datos que había ese día, y la alarma que TENDRÍA que
haber sonado.

  1. `gastos_proyectado_mes` sin fila del período → ~815.000 de gastos fijos
     desaparecían de la proyección.
  2. `venta_proyectada_mes` sin fila → la meta volvía al kprog viejo.
  3. Precio y colorantes de la Proyección en 0 → sale del `pre` de Iniciales.
  4. `cerrar_mes_auto` no copiaba `pre`, `dificil`, `pretej`, `pretin`,
     `preadm` → septiembre nació con 12 columnas en vez de 17.
  5. `procesa_provisiones` (mes completo) corrió encima del devengo diario →
     724.275 de más en las 12 provisiones YY/RT.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from modules.admin_dbase import health_audit_view as hv

HOY = date(2026, 9, 1)

# Agosto 2026 tal como quedó en /informes/iniciales (17 columnas con dato).
AGOSTO = {
    "hilado": 120_000.0, "tejido": 30_000.0, "terminado": 80_000.0, "vq": 280_000.0,
    "um": 3.04, "uk": 4.1, "uq": 1.2, "uf": 6.3, "pre": 8.55,
    "kprog": 320_000.0, "gprog": 800_000.0, "numnot": 1234.0, "dificil": 5_000.0,
    "pretej": 165_000.0, "pretin": 360_000.0, "preadm": 290_000.0, "pretot": 815_000.0,
}
# Lo que copia `cerrar_mes_auto` (12) y lo que copia el rollover (17).
SEPTIEMBRE_A_MEDIAS = {c: (None if c in ("pre", "dificil", "pretej", "pretin", "preadm") else v)
                       for c, v in AGOSTO.items()}
SEPTIEMBRE_ENTERO = dict(AGOSTO)

PROVISIONES_SANAS = [
    {"id_posdat": 1, "prov": "YY", "concepto": "A,E,C aportes", "cuota_mensual": 195_750.0,
     "delta_ventana": 195_750.0 / 30},
    {"id_posdat": 2, "prov": "RT", "concepto": "RETENCIONES", "cuota_mensual": 182_700.0,
     "delta_ventana": 182_700.0 / 30},
]


def _datos(**cambios) -> dict:
    """El arranque de mes SANO; cada escenario pisa lo que rompe."""
    base = {
        "hoy": HOY,
        "errores": {},
        "iniciales_hoy": dict(SEPTIEMBRE_ENTERO),
        "iniciales_prev": dict(AGOSTO),
        "gastos_proy": {"tej": 165_000.0, "tin": 360_000.0, "adm": 290_000.0,
                        "heredado": False, "periodo_origen": "2026-09"},
        "venta_proy": {"kg": 320_000.0, "heredado": False, "periodo_origen": "2026-09"},
        "gastos_manual_prev": {"tej": 164_552.0, "tin": 363_948.0, "adm": 294_607.0},
        "patant": {"fecha": date(2026, 8, 31), "patrimonio": 9_500_000.0},
        "provisiones": [dict(p) for p in PROVISIONES_SANAS],
    }
    base.update(cambios)
    return base


def _cats(r):
    return [a["category"] for a in r["alerts"]]


def test_el_mes_que_arranca_entero_no_dice_nada():
    r = hv.arranque_de_mes_alertas(_datos())
    assert r["ok"] is True and r["alerts"] == []
    assert r["stats"]["mes"] == "2026-09"


# --- 1 y 2: las tablas por período --------------------------------------------

def test_falla_1_gastos_proyectados_sin_fila_ni_herencia():
    """01/09 06:00: la fila '2026-09' no existe y no había de dónde heredar."""
    r = hv.arranque_de_mes_alertas(_datos(gastos_proy={
        "tej": 0.0, "tin": 0.0, "adm": 0.0, "heredado": False, "periodo_origen": None}))
    assert r["ok"] is False
    assert _cats(r) == ["gastos_proyectado_sin_fila"]
    assert "/informes/gastos" in r["alerts"][0]["msg"]


def test_la_herencia_de_gastos_es_un_arranque_sano():
    """Con el arreglo del 02/09 el día 1 hereda agosto: eso NO es alarma, pero
    el chequeo lo dice para que se sepa de dónde salió el número."""
    r = hv.arranque_de_mes_alertas(_datos(gastos_proy={
        "tej": 165_000.0, "tin": 360_000.0, "adm": 290_000.0,
        "heredado": True, "periodo_origen": "2026-08"}))
    assert r["ok"] is True
    assert r["stats"]["gastos_proyectado"] == {
        "total": 815_000.0, "heredado": True, "periodo_origen": "2026-08"}


def test_falla_2_venta_proyectada_sin_fila_ni_herencia():
    r = hv.arranque_de_mes_alertas(_datos(venta_proy={
        "kg": None, "heredado": False, "periodo_origen": None}))
    assert r["ok"] is False
    assert _cats(r) == ["venta_proyectada_sin_fila"]
    assert r["alerts"][0]["severity"] == "medium"


def test_la_herencia_de_venta_es_un_arranque_sano():
    r = hv.arranque_de_mes_alertas(_datos(venta_proy={
        "kg": 320_000.0, "heredado": True, "periodo_origen": "2026-08"}))
    assert r["ok"] is True
    assert r["stats"]["venta_proyectada"]["heredado"] is True


def test_gastos_mes_anterior_sin_congelar_recien_reclama_desde_el_dia_2():
    """Se congela solo al primer /informes/gastos del mes: el día 1 a las 06:00
    todavía no está y no es una falla."""
    r1 = hv.arranque_de_mes_alertas(_datos(gastos_manual_prev=None), hoy=date(2026, 9, 1))
    assert r1["ok"] is True
    assert r1["stats"]["gastos_mes_anterior"] == {"periodo": "2026-08", "congelado": False}
    r2 = hv.arranque_de_mes_alertas(_datos(gastos_manual_prev=None), hoy=date(2026, 9, 2))
    assert r2["ok"] is False
    assert _cats(r2) == ["gastos_mes_anterior_sin_congelar"]
    assert "agosto 2026" in r2["alerts"][0]["msg"]


# --- 3 y 4: la fila de iniciales ----------------------------------------------

def test_falla_4_septiembre_nacio_con_12_columnas_en_vez_de_17():
    """Lo que dejó `cerrar_mes_auto` el 01/09: sin pre/dificil/pretej/pretin/preadm.
    Es también la falla 3: sin `pre`, la Proyección sale en 0 el día 1."""
    r = hv.arranque_de_mes_alertas(_datos(iniciales_hoy=dict(SEPTIEMBRE_A_MEDIAS)))
    assert r["ok"] is False
    assert _cats(r) == ["iniciales_columna_perdida"]
    assert r["stats"]["iniciales"]["columnas_perdidas"] == [
        "pre", "dificil", "pretej", "pretin", "preadm"]
    msg = r["alerts"][0]["msg"]
    assert "pre, dificil, pretej, pretin, preadm" in msg
    assert "/iniciales" in msg


def test_una_columna_en_cero_en_los_dos_meses_no_es_perdida():
    """Si agosto tampoco tenía `dificil`, septiembre sin `dificil` es lo normal."""
    prev = dict(AGOSTO, dificil=0.0)
    hoy = dict(SEPTIEMBRE_ENTERO, dificil=None)
    r = hv.arranque_de_mes_alertas(_datos(iniciales_hoy=hoy, iniciales_prev=prev))
    assert r["ok"] is True
    assert r["stats"]["iniciales"]["columnas_perdidas"] == []


def test_sin_fila_de_iniciales_del_mes_avisa_fuerte():
    """El 01/07/2026: sin fila de julio, stock −2M y utilidad −1,69M fantasma."""
    r = hv.arranque_de_mes_alertas(_datos(iniciales_hoy=None))
    assert r["ok"] is False
    assert _cats(r) == ["iniciales_faltante"]
    assert r["alerts"][0]["severity"] == "high"
    assert "septiembre 2026" in r["alerts"][0]["msg"]


def test_sin_fila_del_mes_anterior_no_puede_comparar_columnas():
    r = hv.arranque_de_mes_alertas(_datos(iniciales_prev=None))
    assert r["ok"] is True
    assert "columnas_perdidas" not in r["stats"]["iniciales"]
    assert r["stats"]["iniciales"]["mes_anterior_existe"] is False


def test_las_17_columnas_son_las_que_copia_el_rollover():
    """La lista del chequeo no puede separarse de la del código que crea la
    fila — si alguien suma una columna al rollover, tiene que sumarla acá."""
    from tests.test_iniciales_dos_creadores_misma_fila import _cols_rollover
    assert set(hv.INICIALES_COLUMNAS_QUE_VIAJAN) == _cols_rollover()
    assert len(hv.INICIALES_COLUMNAS_QUE_VIAJAN) == 17


# --- El PATANT -----------------------------------------------------------------

def test_patant_del_ultimo_dia_del_mes_anterior_esta_ok():
    r = hv.arranque_de_mes_alertas(_datos())
    assert r["stats"]["patant"] == {
        "fecha": "2026-08-31", "esperada": "2026-08-31", "patrimonio": 9_500_000.0}


def test_patant_de_otro_dia_avisa():
    """Agosto 2026: la foto del 31 no existía y el PATANT salió del 30."""
    r = hv.arranque_de_mes_alertas(_datos(patant={
        "fecha": date(2026, 8, 30), "patrimonio": 9_400_000.0}))
    assert r["ok"] is False
    assert _cats(r) == ["patant_de_otro_dia"]
    assert "2026-08-30" in r["alerts"][0]["msg"]
    assert "/admin/regenerar-snapshot/" in r["alerts"][0]["msg"]


def test_patant_acepta_la_fecha_como_datetime():
    from datetime import datetime
    r = hv.arranque_de_mes_alertas(_datos(patant={
        "fecha": datetime(2026, 8, 31, 23, 59), "patrimonio": 9_500_000.0}))
    assert r["ok"] is True


def test_sin_ninguna_foto_de_cierre_avisa():
    r = hv.arranque_de_mes_alertas(_datos(patant=None))
    assert r["ok"] is False
    assert _cats(r) == ["patant_faltante"]
    assert r["stats"]["patant"]["fecha"] is None


def test_patant_en_cero_avisa():
    r = hv.arranque_de_mes_alertas(_datos(patant={"fecha": date(2026, 8, 31), "patrimonio": 0}))
    assert r["ok"] is False
    assert _cats(r) == ["patant_en_cero"]


def test_en_enero_el_mes_anterior_es_diciembre_del_ano_pasado():
    r = hv.arranque_de_mes_alertas(_datos(patant={
        "fecha": date(2026, 12, 31), "patrimonio": 1.0}), hoy=date(2027, 1, 1))
    assert r["stats"]["patant"]["esperada"] == "2026-12-31"
    assert r["stats"]["gastos_mes_anterior"]["periodo"] == "2026-12"
    assert "patant_de_otro_dia" not in _cats(r)


# --- 5: el doble cobro de provisiones -----------------------------------------

def test_falla_5_el_doble_cobro_del_01_09():
    """Cada provisión subió el mes COMPLETO más la cuota del día."""
    dobles = [dict(p, delta_ventana=p["cuota_mensual"] + p["cuota_mensual"] / 30)
              for p in PROVISIONES_SANAS]
    r = hv.arranque_de_mes_alertas(_datos(provisiones=dobles))
    assert r["ok"] is False
    assert _cats(r) == ["provisiones_doble_carga"]
    msg = r["alerts"][0]["msg"]
    assert msg.startswith("2 provisiones subieron 391.065,00 en 48 h")
    assert "YY A,E,C aportes" in msg and "RT RETENCIONES" in msg
    assert "/posdat?tab=yy" in msg
    assert len(r["stats"]["provisiones"]["saltos"]) == 2


def test_la_cuota_diaria_de_un_dia_normal_no_avisa():
    r = hv.arranque_de_mes_alertas(_datos())
    assert r["stats"]["provisiones"] == {"n": 2, "saltos": []}


def test_dos_dias_de_cuota_en_la_ventana_de_48_h_tampoco():
    provs = [dict(p, delta_ventana=2 * p["cuota_mensual"] / 30) for p in PROVISIONES_SANAS]
    r = hv.arranque_de_mes_alertas(_datos(provisiones=provs))
    assert r["ok"] is True


def test_una_provision_que_bajo_no_es_doble_cobro():
    """Un pago de RT o la corrección de /admin/correccion-provisiones-doble/."""
    provs = [dict(p, delta_ventana=-p["cuota_mensual"]) for p in PROVISIONES_SANAS]
    r = hv.arranque_de_mes_alertas(_datos(provisiones=provs))
    assert r["ok"] is True


def test_una_provision_sin_cuota_conocida_no_se_juzga():
    provs = [{"id_posdat": 9, "prov": "YY", "concepto": "rara", "cuota_mensual": 0.0,
              "delta_ventana": 999_999.0}]
    r = hv.arranque_de_mes_alertas(_datos(provisiones=provs))
    assert r["ok"] is True


def test_con_mas_de_cuatro_saltos_el_mensaje_no_se_hace_eterno():
    provs = [{"id_posdat": i, "prov": "YY", "concepto": f"P{i}", "cuota_mensual": 100.0,
              "delta_ventana": 100.0} for i in range(12)]
    r = hv.arranque_de_mes_alertas(_datos(provisiones=provs))
    assert "y 8 más" in r["alerts"][0]["msg"]
    assert r["alerts"][0]["msg"].startswith("12 provisiones subieron 1.200,00")


# --- Todo junto: el 01/09/2026 tal como fue ------------------------------------

def test_el_01_09_2026_hubiera_encendido_cuatro_luces():
    """Las cinco fallas juntas (la 3 y la 4 son la misma fila de Iniciales)."""
    dobles = [dict(p, delta_ventana=p["cuota_mensual"] + p["cuota_mensual"] / 30)
              for p in PROVISIONES_SANAS]
    r = hv.arranque_de_mes_alertas(_datos(
        iniciales_hoy=dict(SEPTIEMBRE_A_MEDIAS),
        gastos_proy={"tej": 0.0, "tin": 0.0, "adm": 0.0, "heredado": False,
                     "periodo_origen": None},
        venta_proy={"kg": None, "heredado": False, "periodo_origen": None},
        provisiones=dobles,
    ))
    assert r["ok"] is False
    assert _cats(r) == [
        "iniciales_columna_perdida", "gastos_proyectado_sin_fila",
        "venta_proyectada_sin_fila", "provisiones_doble_carga",
    ]


def test_un_error_de_lectura_queda_a_la_vista_y_no_tumba_el_chequeo():
    r = hv.arranque_de_mes_alertas(_datos(
        provisiones=None, errores={"provisiones": "relation dia_movimiento no existe"}))
    assert r["stats"]["errores"] == {"provisiones": "relation dia_movimiento no existe"}
    assert r["stats"]["provisiones"] == {"n": 0, "saltos": []}


# --- La vista y el health del cron ---------------------------------------------

@pytest.fixture
def _app_ctx(app):
    with app.test_request_context("/"):
        yield


def _vista():
    vista = hv.arranque_de_mes
    while hasattr(vista, "__wrapped__"):
        vista = vista.__wrapped__
    return vista


def test_la_vista_devuelve_el_juicio_y_avisa_en_la_campanita(_app_ctx):
    avisos = []
    with patch.object(hv, "arranque_de_mes_datos", lambda: _datos(iniciales_hoy=None)), \
         patch("modules.avisos.avisar", lambda **kw: avisos.append(kw) or True):
        d = _vista()().get_json()
    assert d["ok"] is False and _cats(d) == ["iniciales_faltante"]
    assert len(avisos) == 1
    assert avisos[0]["clave"] == "health:arranque-de-mes:2026-09:iniciales_faltante"
    assert avisos[0]["url"] == "/admin/health/arranque-de-mes"
    assert avisos[0]["nivel"] == "alerta"
    assert avisos[0]["titulo"].startswith("No hay fila de Iniciales para septiembre 2026")


def test_con_varias_alarmas_el_titulo_las_cuenta(_app_ctx):
    avisos = []
    with patch.object(hv, "arranque_de_mes_datos",
                      lambda: _datos(iniciales_hoy=None, patant=None)), \
         patch("modules.avisos.avisar", lambda **kw: avisos.append(kw) or True):
        _vista()()
    assert avisos[0]["titulo"] == "El arranque de mes tiene 2 cosas mal"
    assert avisos[0]["cantidad"] == 2
    assert avisos[0]["clave"].endswith(":iniciales_faltante|patant_faltante")


def test_sano_no_deja_aviso(_app_ctx):
    avisos = []
    with patch.object(hv, "arranque_de_mes_datos", lambda: _datos()), \
         patch("modules.avisos.avisar", lambda **kw: avisos.append(kw) or True):
        d = _vista()().get_json()
    assert d["ok"] is True and avisos == []


def test_si_la_campanita_falla_el_health_igual_contesta(_app_ctx):
    def _boom(**kw):
        raise RuntimeError("sin tabla aviso")
    with patch.object(hv, "arranque_de_mes_datos", lambda: _datos(patant=None)), \
         patch("modules.avisos.avisar", _boom):
        d = _vista()().get_json()
    assert d["ok"] is False


def test_entra_al_health_all(_app_ctx):
    """Una alarma que no está en /all no la mira nadie."""
    import inspect
    src = inspect.getsource(hv.health_all)
    assert "arranque_de_mes()" in src
    assert '"arranque_de_mes": data24' in src
    assert 'and data24["ok"]' in src


def test_arranque_de_mes_datos_lee_cada_bloque_por_separado(fake_db):
    """Si una lectura falla, las otras igual llegan y el error queda dicho."""
    from modules.informes import queries as _iq

    def _fetch_one(sql, params=None, **kw):
        if "scintela.iniciales" in sql and params == (9, 2026):
            return dict(SEPTIEMBRE_ENTERO)
        if "scintela.iniciales" in sql:
            return dict(AGOSTO)
        return None

    def _boom(*a, **k):
        raise RuntimeError("la traza no está")

    with patch.object(hv.db, "fetch_one", _fetch_one), \
         patch.object(_iq, "gastos_proyectado_mes_get", lambda per: {"tej": 1.0, "periodo_origen": per}), \
         patch.object(_iq, "venta_proyectada_mes_vigente", lambda per: {"kg": 5.0}), \
         patch.object(_iq, "gastos_mes_manual_get", lambda per: {"tej": 2.0, "periodo": per}), \
         patch.object(_iq, "historia_ultimo_mes", lambda: {"fecha": date(2026, 8, 31), "patrimonio": 1.0}), \
         patch.object(hv, "_provisiones_con_salto", _boom):
        d = hv.arranque_de_mes_datos(HOY)
    assert d["iniciales_hoy"]["pre"] == 8.55 and d["iniciales_prev"]["pre"] == 8.55
    assert d["gastos_proy"]["periodo_origen"] == "2026-09"
    assert d["gastos_manual_prev"]["periodo"] == "2026-08"
    assert d["provisiones"] is None
    assert d["errores"] == {"provisiones": "la traza no está"}


def test_provisiones_con_salto_cruza_la_traza_con_las_cuotas(fake_db):
    from modules.posdat import queries as _pq

    def _fetch_all(sql, params=None, **kw):
        if "scintela.posdat" in sql:
            return [{"id_posdat": 7, "prov": "YY", "concepto": "SUELDOS", "importe": 1.0,
                     "baseline_date": date(2026, 8, 31)}]
        assert "dia_movimiento" in sql and "traza_utilidad" in sql
        assert params == (48,)
        return [{"doc_id": "p7", "delta": 134_850.0}, {"doc_id": "p99", "delta": 5.0}]

    def _cuotas(rows):
        for r in rows:
            r["cuota_mensual"] = 130_500.0

    with patch.object(hv.db, "fetch_all", _fetch_all), \
         patch.object(_pq, "_resolver_cuotas", _cuotas):
        provs = hv._provisiones_con_salto(HOY)
    assert provs == [{"id_posdat": 7, "prov": "YY", "concepto": "SUELDOS",
                      "cuota_mensual": 130_500.0, "delta_ventana": 134_850.0}]
    r = hv.arranque_de_mes_alertas(_datos(provisiones=provs))
    assert _cats(r) == ["provisiones_doble_carga"]


def test_provisiones_con_salto_sin_filas_no_consulta_la_traza(fake_db):
    llamadas = []

    def _fetch_all(sql, *a, **k):
        llamadas.append(sql)
        return []

    with patch.object(hv.db, "fetch_all", _fetch_all):
        assert hv._provisiones_con_salto(HOY) == []
    assert len(llamadas) == 1


def test_una_sola_provision_habla_en_singular():
    provs = [dict(PROVISIONES_SANAS[0], delta_ventana=195_750.0)]
    r = hv.arranque_de_mes_alertas(_datos(provisiones=provs))
    assert r["alerts"][0]["msg"].startswith("1 provisión subió 195.750,00 en 48 h (YY A,E,C aportes)")
