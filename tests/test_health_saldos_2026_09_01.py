"""La alarma que vigila que Al arrancar - Vendido = Queda siga cerrando.

Duena 31/08/2026, despues de corregir la resta a mano: *"pone alertas despues
para que no nos vuelva a pasar"*. `resumen()` tiene un PISO que absorbe en
silencio tanto una tela nueva como la SEGUNDA que sigue entrando -- asi la
resta nunca se ROMPE a la vista -- pero ese mismo piso puede absorber tambien
kilos que ninguna venta explica de verdad (`kg_movido`). Por construccion
`kg_movido` nunca da negativo, asi que la alarma no es "cerro?" sino "cuanto
quedo sin explicar?" -- lo mismo que ya muestra la pantalla bajo Queda cuando
`kg_movido >= 1`, aca vigilado aunque nadie la mire ese dia.
"""
import pytest

from modules.admin_dbase import health_audit_view as hv


def _correr(resumen):
    return hv.saldos_alertas(resumen)


BIEN = {"kg_movido": 0.0}


def test_cuando_la_resta_cierra_no_avisa_nada():
    r = _correr(dict(BIEN))
    assert r["ok"] and r["alerts"] == []


def test_un_residuo_chico_de_redondeo_no_avisa():
    # Menos de 1 kg es centavos de redondeo entre floats, no un ajuste real.
    r = _correr({"kg_movido": 0.4})
    assert r["ok"] and r["alerts"] == []


@pytest.mark.parametrize("kg_movido", [1.0, 512.0, 1744.0])
def test_kg_sin_explicar_enciende_la_alarma(kg_movido):
    r = _correr({"kg_movido": kg_movido})
    assert not r["ok"], f"kg_movido={kg_movido} paso sin avisar"
    assert [a["category"] for a in r["alerts"]] == ["saldos_kg_sin_explicar"]
    assert f"{kg_movido:,.2f}" in r["alerts"][0]["msg"]


def test_la_alarma_entra_al_health_del_cron():
    """Si no esta en /admin/health/all, no la mira nadie: el cron diario es el
    unico que corre esto todos los dias."""
    import inspect
    fuente = inspect.getsource(hv.health_all)
    assert "saldos_coherente()" in fuente
    assert '"saldos": data19' in fuente
    assert 'data19["ok"]' in fuente, "no entra al ok general: no enciende el panel"


def test_saldos_coherente_recalcula_exactamente_lo_que_ve_la_pantalla():
    """El chequeo tiene que mirar los MISMOS datos que `/analisis/parado`:
    misma `items()`, `con_puntos()`, `kg_al_marcar_vivo()` y `largada` -- si
    usara otra fuente podria decir 'ok' con la pantalla rota, o al reves."""
    import inspect
    fuente = inspect.getsource(hv.saldos_coherente)
    assert "_saldos_q.items()" in fuente
    assert "_saldos_q.con_puntos(" in fuente
    assert "_saldos_q.kg_al_marcar_vivo(" in fuente
    assert '_saldos_q.config("largada"' in fuente
