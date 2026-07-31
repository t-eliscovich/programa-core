"""El watchdog comparaba peras con manzanas (TMT 2026-07-31).

`/admin/health/utilidad-watchdog` restaba:

    componentes.patr      → patrimonio BRUTO (URET vive dentro del activo)
  − historia.patrimonio   → patrimonio NETO  (crear_snapshot_diario guarda
                            patr − uret, igual que el PRG línea 1347)

Resultado: el delta venía inflado por TODOS los retiros del mes. El 31/07,
con 211.393 de dividendos, el watchdog decía "+153.772" mientras el
patrimonio real había BAJADO 57.800 — y la alerta de $200k iba a saltar sola
todos los fines de mes.

Ahora las dos patas son netas.
"""
from unittest.mock import patch

import pytest

from modules.admin_dbase import health_audit_view as hv


SNAP = {
    "fecha": "2026-07-30",
    "fecha_crea": "2026-07-30 23:05:00",
    "patrimonio": 21_185_000.0,   # NETO de URET
    "banco": 0.0,
    "cart": 7_702_200.0,
    "ustock": 8_139_000.0,
    "deuda": 0.0,
    "anticipos": 0.0,
}

# LIVE del 31/07: patrimonio neto 21.127.200 (bajó 57.800) y 211.393 de
# retiros del mes adentro del activo.
URET = 211_393.0
BALANCE = {
    "diagnostico": {
        "componentes": {
            "patr": 21_127_200.0 + URET,   # BRUTO
            "uret": URET,
            "cart": 7_684_000.0,
            "vsto": 8_368_000.0,
            "utilidad": 552_700.0,
        }
    }
}


def _watchdog(snap=SNAP, balance=BALANCE):
    import modules.informes.queries as iq
    with patch.object(hv.db, "fetch_one", return_value=snap), \
         patch.object(iq, "informe_balance", return_value=balance):
        # La vista está decorada con @requiere_login/@requiere_permiso; acá
        # nos interesa la aritmética, así que llamamos la función desnuda.
        vista = hv.utilidad_watchdog
        while hasattr(vista, "__wrapped__"):
            vista = vista.__wrapped__
        return vista()


@pytest.fixture
def _app_ctx(app):
    with app.test_request_context("/"):
        yield


def _stats(resp):
    return resp.get_json()["stats"]


def test_el_delta_no_arrastra_los_dividendos_del_mes(_app_ctx):
    resp = _watchdog()
    st = _stats(resp)
    # Lo real: −57.800. Antes daba +153.593 (= −57.800 + 211.393).
    assert round(st["delta_patrimonio"]) == -57_800
    assert st["live_uret"] == URET


def test_no_dispara_la_alerta_de_200k_por_los_retiros(_app_ctx):
    resp = _watchdog()
    cats = [a["category"] for a in resp.get_json()["alerts"]]
    assert "delta_patrimonio_alto" not in cats


def test_un_salto_de_verdad_sigue_alertando(_app_ctx):
    roto = {"diagnostico": {"componentes": dict(
        BALANCE["diagnostico"]["componentes"],
        patr=21_185_000.0 + 900_000.0 + URET,
    )}}
    resp = _watchdog(balance=roto)
    cats = [a["category"] for a in resp.get_json()["alerts"]]
    assert "delta_patrimonio_alto" in cats


def test_sin_uret_en_los_componentes_no_rompe(_app_ctx):
    """Meses sin retiros: `uret` puede venir en 0 o ausente."""
    sin = {"diagnostico": {"componentes": {
        "patr": 21_185_000.0, "cart": 0.0, "vsto": 0.0, "utilidad": 0.0,
    }}}
    st = _stats(_watchdog(balance=sin))
    assert st["delta_patrimonio"] == 0.0
    assert st["live_uret"] == 0.0


def test_el_snapshot_diario_guarda_el_patrimonio_NETO():
    """La otra pata de la comparación — si esto cambia, el fix se rompe."""
    import inspect
    from modules.informes import queries as iq
    src = inspect.getsource(iq.crear_snapshot_diario)
    assert '- float(bal.get("uret")' in src or "- float(bal.get('uret')" in src
