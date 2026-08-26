"""Las provisiones pasan a repartirse entre los días del mes (desde 01/09/2026).

Pedido de Tamara, 25/08/2026: *"hagamos un total mensual de lo que se viene
pasando, y luego dividimos por los días"*, contando sábados y domingos.

Antes: `scintela.provisiones.importe` era la cuota DIARIA y sólo corría de
lunes a viernes — así, lo que se devengaba en el mes dependía de cuántos días
hábiles cayeran (21 en agosto 2026, 23 en julio). Ahora `importe` es el monto
MENSUAL (migración 0223) y cada día suma el mensual ÷ los días de ese mes.

Lo que estos tests fijan es que **el mes cierra en el mensual, siempre**.
"""
from datetime import date, timedelta

import pytest

from modules.posdat import queries as pq

MENSUAL_AEC = 195750.0      # 9.000 por día hábil, como venía
CORTE = date(2026, 9, 1)


def _persist(monkeypatch, *, importe, baseline, mensual, hoy):
    """Corre el motor de devengo y devuelve el importe nuevo (o None)."""
    import db as _db
    updates = []

    def fake_fetch_all(sql, params=None, **kw):
        if "FROM scintela.posdat" in sql:
            return [{"id_posdat": 1, "prov": "YY", "concepto": "A,E,C AG,EN,CMB",
                     "importe": importe, "baseline_date": baseline}]
        if "FROM scintela.provisiones" in sql:
            return [{"id_provisiones": 1, "concepto": "A,E,C",
                     "importe": mensual, "periodo_aplica": None}]
        return []

    monkeypatch.setattr(_db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(_db, "fetch_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(_db, "execute",
                        lambda sql, params=None, **kw: updates.append(params) or 1)
    pq.persistir_acumulacion_yy(hoy=hoy)
    return updates[0][0] if updates else None


def test_el_sabado_y_el_domingo_ahora_suman(monkeypatch):
    """Sá 05 y Do 06/09/2026 — antes del cambio no sumaban nada."""
    nuevo = _persist(monkeypatch, importe=0.0, baseline=date(2026, 9, 4),
                     mensual=MENSUAL_AEC, hoy=date(2026, 9, 6))
    assert nuevo == pytest.approx(round(MENSUAL_AEC / 30 * 2, 2))


def test_antes_del_corte_el_fin_de_semana_sigue_sin_sumar(monkeypatch):
    """Agosto no se mueve: el cambio arranca el 1 de septiembre."""
    nuevo = _persist(monkeypatch, importe=0.0, baseline=date(2026, 8, 28),
                     mensual=MENSUAL_AEC, hoy=date(2026, 8, 30))
    assert nuevo is None


def test_antes_del_corte_el_dia_habil_suma_lo_mismo_de_siempre(monkeypatch):
    """9.000 por día hábil, igual que venía — ni un centavo de diferencia."""
    nuevo = _persist(monkeypatch, importe=0.0, baseline=date(2026, 8, 24),
                     mensual=MENSUAL_AEC, hoy=date(2026, 8, 25))
    assert nuevo == pytest.approx(9000.0)


@pytest.mark.parametrize("primero, ultimo", [
    (date(2026, 9, 1), date(2026, 9, 30)),      # mes de 30
    (date(2026, 10, 1), date(2026, 10, 31)),    # mes de 31
    (date(2027, 2, 1), date(2027, 2, 28)),      # febrero corto
    (date(2028, 2, 1), date(2028, 2, 29)),      # febrero bisiesto
])
def test_el_mes_completo_devenga_el_mensual(monkeypatch, primero, ultimo):
    """El punto de todo el cambio: el gasto del mes es el mismo siempre."""
    nuevo = _persist(monkeypatch, importe=0.0, baseline=primero - timedelta(days=1),
                     mensual=MENSUAL_AEC, hoy=ultimo)
    assert nuevo == pytest.approx(MENSUAL_AEC, abs=0.01)


def test_un_mes_de_31_ya_no_gasta_mas_que_uno_de_30(monkeypatch):
    """Lo que estaba mal: julio (23 hábiles) gastaba 10% más que agosto (21)."""
    sept = _persist(monkeypatch, importe=0.0, baseline=date(2026, 8, 31),
                    mensual=MENSUAL_AEC, hoy=date(2026, 9, 30))
    octu = _persist(monkeypatch, importe=0.0, baseline=date(2026, 9, 30),
                    mensual=MENSUAL_AEC, hoy=date(2026, 10, 31))
    assert sept == pytest.approx(octu, abs=0.01)


def test_la_cuota_diaria_se_calcula_no_se_guarda():
    """La pantalla muestra la diaria del mes en curso, sin guardarla."""
    filas = [{"prov": "YY", "concepto": "A,E,C AG,EN,CMB"}]
    import db as _db
    orig = _db.fetch_all
    try:
        _db.fetch_all = lambda *a, **k: [
            {"id_provisiones": 1, "concepto": "A,E,C", "importe": MENSUAL_AEC,
             "periodo_aplica": None}]
        pq._resolver_cuotas(filas)
    finally:
        _db.fetch_all = orig
    assert filas[0]["cuota_mensual"] == MENSUAL_AEC
    assert filas[0]["cuota_diaria"] > 0
    # la diaria tiene que salir del mensual, nunca al revés
    assert filas[0]["cuota_diaria"] < filas[0]["cuota_mensual"]


def test_el_devengo_lee_la_cuota_mensual_no_la_diaria():
    """Guard: quien arme una fila a mano tiene que poner `cuota_mensual`.

    /admin/debug-yy armaba la fila con `cuota_diaria` y el devengo daba cero:
    la pantalla mostraba que la deuda no se movía, que es mentira.
    """
    con_mensual = {"prov": "YY", "importe": 0.0, "cuota_mensual": MENSUAL_AEC,
                   "baseline_date": date(2026, 9, 1)}
    con_diaria = {"prov": "YY", "importe": 0.0, "cuota_diaria": 9000.0,
                  "baseline_date": date(2026, 9, 1)}
    pq._aplicar_display_time_yy([con_mensual], hoy=date(2026, 9, 3))
    pq._aplicar_display_time_yy([con_diaria], hoy=date(2026, 9, 3))
    assert con_mensual["importe"] > 0
    assert con_diaria["importe"] == 0.0, (
        "una fila armada a mano con cuota_diaria no devenga — hay que pasarle "
        "cuota_mensual (ver modules/admin_dbase/debug_yy_view.py)")
