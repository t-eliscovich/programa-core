"""La alarma que vigila la contabilización de la competencia de saldos.

Dueña 25/08/2026: *"que la contabilización a futuro no se dañe"*. La cuenta se
rehace entera en cada refresco desde Asinfo y nadie la mira renglón por
renglón: sin alarma, un cambio de datos allá se convierte en puntos de más acá
y se nota recién cuando alguien mira la pantalla y dice "esto está mal".
"""
import pytest

from modules.admin_dbase import health_audit_view as hv


def _correr(fila):
    """Las cinco reglas sobre una fila de números, sin base ni request."""
    return hv.competencia_alertas(fila)


BIEN = {"kg_venta": 314.85, "kg_foto": 314.85, "n_ajenos": 0, "kg_ajenos": 0,
        "n_pasados": 0, "n_sin_puntaje": 0, "n_sin_motivo": 0, "n_sin_tope": 0}


def test_cuando_todo_cierra_no_avisa_nada():
    r = _correr(dict(BIEN))
    assert r["ok"] and r["alerts"] == []


@pytest.mark.parametrize("campo, valor, categoria", [
    # El caso real del 25/08: el encabezado decía 381 y el ranking 230.
    ("kg_foto", 381.0, "competencia_descuadrada"),
    # 152 kg firmados por Bedon Hector, que no compite.
    ("n_ajenos", 3, "competencia_vendedor_ajeno"),
    # Jersey 3 BLA: 554 puntos de tela tejida en julio.
    ("n_pasados", 1, "competencia_sin_tope"),
    # Una tela sin puntaje congelado vale 1 punto por kilo en silencio.
    ("n_sin_puntaje", 2, "competencia_sin_puntaje"),
    # Sin motivo cuenta también la primera; sin kilos al marcar no hay tope.
    ("n_sin_motivo", 9, "competencia_cohorte_incompleta"),
    ("n_sin_tope", 4, "competencia_cohorte_incompleta"),
])
def test_cada_agujero_enciende_su_alarma(campo, valor, categoria):
    fila = dict(BIEN)
    fila[campo] = valor
    r = _correr(fila)
    assert not r["ok"], f"{campo}={valor} pasó sin avisar"
    assert [a["category"] for a in r["alerts"]] == [categoria]


def test_la_alarma_entra_al_health_del_cron():
    """Si no está en /admin/health/all, no la mira nadie: el cron diario es el
    único que corre esto todos los días."""
    import inspect
    fuente = inspect.getsource(hv.health_all)
    assert "competencia_coherente()" in fuente
    assert '"competencia": data16' in fuente
    assert 'data16["ok"]' in fuente, "no entra al ok general: no enciende el panel"
