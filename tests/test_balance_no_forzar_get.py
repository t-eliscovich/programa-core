"""Regresión bug #4 — un GET a /informes/balance NO debe poder forzar
provisiones diarias.

El viejo `?forzar_provisiones=1` era un footgun: un GET (refresh, prefetch del
navegador, favorito) disparaba una aplicación de provisiones sobre estado
financiero. El bypass GET se removió (TMT 2026-07-11); el forzado manual sólo
queda disponible para scripts vía `correr_provisiones_diarias(forzar=True)`.

Este test fija esa decisión: aunque venga el query param, la vista llama al
corredor SIN forzar.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from modules.informes import queries as informes_queries


def _login(app, fake_db, permisos=("informes.ver",)):
    rid = fake_db.add_role("Informes", list(permisos))
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_get_con_forzar_provisiones_no_fuerza(app, fake_db):
    c = _login(app, fake_db)
    spy = MagicMock(return_value={"aplicado": False, "dias_aplicados": 0})
    with patch.object(informes_queries, "correr_provisiones_diarias", spy), \
         patch.object(informes_queries, "informe_balance", return_value={}), \
         patch("modules.posdat.queries.persistir_acumulacion_yy", lambda: None), \
         patch(
             "modules.iniciales.views.auto_cerrar_mes_si_corresponde",
             lambda: None,
         ):
        r = c.get("/informes/balance?forzar_provisiones=1")

    assert r.status_code == 200, r.data[:400]
    # TMT 2026-07-24: correr_provisiones_diarias YA NO se auto-corre en el
    # balance — persistir_acumulacion_yy (baseline por fila) es el ÚNICO motor
    # de devengo. Un GET (con o sin ?forzar_provisiones) NO debe disparar el
    # corredor viejo del marcador global (que pisaba las ediciones manuales).
    assert not spy.called, (
        "correr_provisiones_diarias se llamó desde el balance — se retiró; "
        "el devengo lo hace sólo persistir_acumulacion_yy"
    )
