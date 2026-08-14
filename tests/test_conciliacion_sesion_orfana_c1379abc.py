"""El 500 de la conciliación que reportó Alex el 13/08/2026 (id `c1379abc`).

Qué pasó. Alex re-subió `mov-12-08-202634XXXX6004.xlsx`, cuyas 28 filas ya
estaban todas cargadas. `crear_sesion` las dedupeó todas (`nuevos == []`) y,
como no había sesión abierta, abrió una con `extracto_payload = []`. El GET
siguiente a `/conciliacion/banco-v2` reventaba con 500.

La sesión sin extracto NO era el problema — es un flujo previsto:
`estado_sesion` tiene una rama entera para ella (comentario de la dueña del
29/05: *"cuando se abre sesión sin extracto"*) y es la única puerta que
tiene el operador para arrancar a conciliar el backlog de pendientes cuando
el banco todavía no publicó movimientos nuevos.

La causa real, del traceback:

    _banco_v2_tab_manual.html, línea 88
    {% set _n_extracto = (_banco|length) - _n_pend %}
    jinja2.exceptions.UndefinedError:
        'dict object' has no attribute 'n_historicos_pendientes'

Las DOS ramas de `estado_sesion` armaban el dict por separado y la de "sin
extracto" devolvía una clave menos. El template ya tenía un guard escrito
para ese caso —

    buckets.n_historicos_pendientes if buckets.n_historicos_pendientes
        is not none else (_banco|length)

— pero `is not none` NO caza una clave ausente: Jinja devuelve `Undefined`,
que no es `None`, y la resta de la línea siguiente explota. Familia de
[[project_2026_08_11_estados_cheque_definiciones_paralelas]]: dos lugares
armando la misma forma, uno se quedó atrás.

Se arregla en los dos lados y cada uno tiene su test acá:
  · el modelo: las dos ramas devuelven las MISMAS claves.
  · la pantalla: renderiza de verdad con una sesión sin extracto.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest


def _hist(**kw):
    base = {"id": 1, "fecha": date(2026, 8, 11), "concepto": "DEPOSITO",
            "documento": "41508270", "monto": 590.27, "tipo": "C",
            "oficina": "AG. NORTE"}
    base.update(kw)
    return base


# ── El modelo: las dos ramas devuelven la MISMA forma ─────────────────

def test_estado_sesion_mismas_claves_con_y_sin_extracto(monkeypatch):
    """El invariante que faltaba: mismo contrato salga o no el extracto.

    Si mañana alguien agrega una clave a una rama y se olvida de la otra,
    la pantalla vuelve a reventar con Undefined. Este test lo caza antes.
    """
    from modules.conciliacion import sesion as _sesion

    monkeypatch.setattr(_sesion, "_cargar_historicos_pendientes",
                        lambda no_banco: [_hist()])
    monkeypatch.setattr(_sesion, "_cargar_programa_pendiente",
                        lambda no_banco: [])

    # (a) sesión SIN extracto
    monkeypatch.setattr(_sesion, "cargar_movs", lambda s: [])
    sin_extracto = _sesion.estado_sesion({"id": 64}, 10)

    # (b) sesión CON extracto: el matcher no nos importa acá, sólo la forma.
    mov = _sesion.MovBanco(
        fecha=date(2026, 8, 12), concepto="TRANSFERENCIA", documento="23202626",
        monto=1405.98, saldo=1875978.8, codigo="001045", tipo="C",
        oficina="AG. NORTE",
    )
    monkeypatch.setattr(_sesion, "cargar_movs", lambda s: [mov])
    con_extracto = _sesion.estado_sesion({"id": 65}, 10)

    faltantes = set(con_extracto) - set(sin_extracto)
    assert not faltantes, (
        "la rama 'sin extracto' se quedó sin estas claves y el template las "
        f"lee: {sorted(faltantes)}"
    )
    assert sin_extracto["n_historicos_pendientes"] == 1


# ── La pantalla: renderiza de verdad, sin extracto ────────────────────

@pytest.fixture
def app_logueada():
    from tests.test_routes_smoke import build_app
    app, deshacer = build_app()

    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "alex", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    try:
        yield app
    finally:
        deshacer()


def test_la_pantalla_renderea_con_sesion_sin_extracto(app_logueada, monkeypatch):
    """El test que habría cazado el 500: rama real + template real.

    A propósito NO se mockea `estado_sesion` — es justo el dict que arma esa
    función el que le faltaba la clave. Se mockean sus fuentes de datos.
    """
    from modules.conciliacion import banco_v2_view as v
    from modules.conciliacion import sesion as _s

    monkeypatch.setattr(v._sesion, "sesion_abierta", lambda nb, usuario=None: {
        "id": 64, "no_banco": 10, "abierta_en": datetime(2026, 8, 13, 18, 13),
        "cerrada_en": None, "extracto_nombre": "mov-12-08-202634XXXX6004.xlsx",
        "matches_hechos": 0, "saldo_banco_objetivo": None,
        "saldo_banco_detectado": 1875978.8,
    })
    # Sesión SIN extracto, con backlog de pendientes para conciliar.
    monkeypatch.setattr(_s, "cargar_movs", lambda s: [])
    monkeypatch.setattr(_s, "_cargar_historicos_pendientes",
                        lambda no_banco: [_hist(), _hist(id=2, monto=2300)])
    monkeypatch.setattr(_s, "_cargar_programa_pendiente", lambda no_banco: [])
    monkeypatch.setattr(v._sesion, "matches_de_sesion", lambda s: [])
    monkeypatch.setattr(v._sesion, "deshechos_de_sesion", lambda s: [])
    monkeypatch.setattr(v._bp, "calcular", lambda nb: {
        "saldo": 0.0, "saldo_si_concilio_todo": 0.0, "neto_pendientes": 0.0,
        "pendientes_banco_creditos": 0.0, "pendientes_banco_debitos": 0.0,
        "n_pendientes": 0,
    })
    monkeypatch.setattr(v._db, "fetch_one", lambda *a, **k: {"n": 0})
    monkeypatch.setattr(v._db, "fetch_all", lambda *a, **k: [])

    rv = app_logueada.test_client().get("/conciliacion/banco-v2")
    assert rv.status_code == 200, (
        "la pantalla revienta con una sesión sin extracto — es el 500 c1379abc"
    )
    body = rv.get_data(as_text=True)
    assert "pendientes" in body
    assert "2" in body, "el pill tiene que contar los 2 históricos pendientes"
