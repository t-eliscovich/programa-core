"""Flujo de Fondos — réplica opción 6 dBase (MENU.PRG PROCEDURE FLUJO).

TMT 2026-07-06 (dueña) "no está como el dBase": la pantalla pasó de agrupar
por semana a la tabla DIARIA del dBase. Acá se testea la función PURA
`construir_flujo_diario` (el loop L652-706 del PRG) + smoke de la vista.
"""
from datetime import date

from modules.informes.queries import _dow_dbase, construir_flujo_diario

LUNES = date(2026, 7, 6)  # DOW dBase = 2


def test_dow_dbase_domingo_es_1():
    assert _dow_dbase(date(2026, 7, 5)) == 1   # domingo
    assert _dow_dbase(date(2026, 7, 6)) == 2   # lunes
    assert _dow_dbase(date(2026, 7, 11)) == 7  # sábado


def test_fila_arranque_como_dbase():
    # MENU.PRG L657-659: FECHA=hoy, PICH=S1, INTER=S2, GASTOS=SALCA (la caja
    # va en la columna Gastos), SALDO=S1+S2+SALCA.
    filas = construir_flujo_diario(LUNES, 100.0, 50.0, 10.0, [])
    assert len(filas) == 1
    f = filas[0]
    assert f["tipo"] == "arranque"
    assert f["fecha"] == LUNES
    assert f["pichincha"] == 100.0
    assert f["inter"] == 50.0
    assert f["gastos"] == 10.0  # la caja
    assert f["saldo"] == 160.0


def test_cheque_suma_y_gasto_resta():
    items = [
        {"fecha": date(2026, 7, 8), "tipo": "cheque", "importe": 20.0},
        {"fecha": date(2026, 7, 9), "tipo": "gasto", "importe": 5.0},
    ]
    filas = construir_flujo_diario(LUNES, 100.0, 50.0, 10.0, items)
    dias = [f for f in filas if f["tipo"] == "dia"]
    assert [f["saldo"] for f in dias] == [180.0, 175.0]
    assert dias[0]["cheques"] == 20.0
    assert dias[1]["gastos"] == -5.0  # egreso en negativo, como el dBase


def test_mprima_separada_de_gastos():
    # MENU.PRG L681: CASE PROV$HIL → columna MAT.PR, no GASTS.
    items = [{"fecha": date(2026, 7, 8), "tipo": "mprima", "importe": 30.0}]
    filas = construir_flujo_diario(LUNES, 100.0, 50.0, 10.0, items)
    dia = [f for f in filas if f["tipo"] == "dia"][0]
    assert dia["mprima"] == -30.0
    assert dia["gastos"] == 0.0
    assert dia["saldo"] == 130.0


def test_corte_de_semana_acumula_ingresos_y_egresos():
    # dBase L697: al cambiar de semana (DOW no crece) inserta la fila con
    # ACUMI (Σ ingresos) y ACUME (Σ gastos+mprima), y resetea.
    items = [
        {"fecha": date(2026, 7, 8), "tipo": "cheque", "importe": 20.0},   # miér
        {"fecha": date(2026, 7, 9), "tipo": "gasto", "importe": 5.0},     # jue
        {"fecha": date(2026, 7, 14), "tipo": "mprima", "importe": 7.0},   # mar sig.
    ]
    filas = construir_flujo_diario(LUNES, 100.0, 50.0, 10.0, items)
    semanas = [f for f in filas if f["tipo"] == "semana"]
    assert len(semanas) == 2  # corte entre semanas + última parcial
    assert semanas[0]["ingresos"] == 20.0
    assert semanas[0]["egresos"] == 5.0
    assert semanas[1]["ingresos"] == 0.0
    assert semanas[1]["egresos"] == 7.0
    # el corte va ANTES de la fila del martes siguiente
    idx_corte = filas.index(semanas[0])
    assert filas[idx_corte + 1]["fecha"] == date(2026, 7, 14)


def test_facturas_no_suman_por_default_fa_cero():
    # MENU.PRG L701: FA=0 — la factura genera fila pero no mueve el saldo.
    items = [{"fecha": date(2026, 7, 8), "tipo": "factura", "importe": 99.0}]
    filas = construir_flujo_diario(LUNES, 100.0, 50.0, 10.0, items)
    dia = [f for f in filas if f["tipo"] == "dia"][0]
    assert dia["facturas"] == 0.0
    assert dia["saldo"] == 160.0  # sin cambio
    # con el toggle sí suman
    filas2 = construir_flujo_diario(
        LUNES, 100.0, 50.0, 10.0, items, incluir_facturas=True
    )
    dia2 = [f for f in filas2 if f["tipo"] == "dia"][0]
    assert dia2["facturas"] == 99.0
    assert dia2["saldo"] == 259.0


def test_vencidos_quedan_a_su_fecha_real_y_marcados():
    # El dBase lista los vencidos a su fecha original (antes de hoy).
    items = [{"fecha": date(2026, 6, 30), "tipo": "gasto", "importe": 5.0}]
    filas = construir_flujo_diario(LUNES, 100.0, 50.0, 10.0, items)
    dia = [f for f in filas if f["tipo"] == "dia"][0]
    assert dia["fecha"] == date(2026, 6, 30)
    assert dia["vencida"] is True
    assert dia["saldo"] == 155.0


def test_vencidos_a_hoy_para_el_grafico():
    items = [{"fecha": date(2026, 6, 30), "tipo": "gasto", "importe": 5.0}]
    filas = construir_flujo_diario(
        LUNES, 100.0, 50.0, 10.0, items, vencidos_a_hoy=True
    )
    dia = [f for f in filas if f["tipo"] == "dia"][0]
    assert dia["fecha"] == LUNES
    assert dia["vencida"] is False


def test_flujo_fondos_renderiza(app, fake_db):
    rid = fake_db.add_role("Accionista", ["informes.ver"])
    uid = fake_db.add_user("tamara", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    r = c.get("/informes/flujo-fondos")
    assert r.status_code == 200
    assert b"Flujo de Fondos" in r.data
    assert b"arranque" in r.data
