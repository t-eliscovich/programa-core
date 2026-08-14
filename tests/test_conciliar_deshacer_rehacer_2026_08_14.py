"""Confirmar, deshacer y rehacer una conciliación.

TMT 2026-08-14. `modules/conciliacion/views.py` estaba al **6,94%** y ahí
viven las acciones que mueven plata de verdad: confirmar los matches,
deshacerlos y rehacerlos. Ninguna tenía test.

Además cierran un bug que salió escribiéndolos: el contador `matches_hechos`
se actualizaba filtrando `AND usuario = ...`, de cuando la sesión era una por
banco Y POR USUARIO. Desde la migración 0189 hay UNA sola por banco, así que
si Alex deshacía sobre la sesión que abrió otro, el UPDATE no encontraba fila
y el contador quedaba inflado sin que nadie se enterara — el síntoma que la
dueña reportó el 03/06 ("decía 14 con 0 matches reales") y que en su momento
se tapó haciendo que la pantalla lo recalcule sola.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)


@pytest.fixture
def cliente(app):
    @app.before_request
    def _login():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "alex", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}
    return app.test_client()


def _avisos(cliente) -> str:
    with cliente.session_transaction() as s:
        return " ".join(m for _c, m in s.get("_flashes", [])).lower()


# ── confirmar ─────────────────────────────────────────────────────────

def _payload(doc="41508270", monto="590.27"):
    return json.dumps([{
        "real": {"fecha": "2026-08-12", "concepto": "DEPOSITO",
                 "documento": doc, "monto": monto, "codigo": "001045",
                 "tipo": "C", "oficina": "AG. NORTE"},
        "bancsis": {"id_transaccion": 5001},
    }])


def test_confirmar_persiste_el_match(cliente, monkeypatch):
    from modules.conciliacion import views as v

    vistos = []
    monkeypatch.setattr(v, "confirmar_match",
                        lambda **kw: vistos.append(kw) or 1)

    rv = cliente.post("/conciliacion/hub/confirmar-matches",
                      data={"matches_json": _payload()})
    assert rv.status_code == 302
    assert len(vistos) == 1
    assert vistos[0]["id_transaccion"] == 5001
    assert vistos[0]["real"].documento == "41508270"
    assert str(vistos[0]["real"].monto) == "590.27", "el importe va exacto"
    assert "confirmados 1" in _avisos(cliente)


def test_si_uno_falla_los_otros_se_guardan_igual(cliente, monkeypatch):
    """Un match roto no puede llevarse puestos a los demás — pero el
    operador tiene que enterarse de que hubo uno que no entró."""
    from modules.conciliacion import views as v

    ids = []

    def _confirmar(**kw):
        if kw["id_transaccion"] == 2:
            raise RuntimeError("id_transaccion no existe")
        ids.append(kw["id_transaccion"])

    monkeypatch.setattr(v, "confirmar_match", _confirmar)
    payload = json.loads(_payload())
    payload = [dict(payload[0], bancsis={"id_transaccion": i}) for i in (1, 2, 3)]

    cliente.post("/conciliacion/hub/confirmar-matches",
                 data={"matches_json": json.dumps(payload)})
    assert ids == [1, 3]
    avisos = _avisos(cliente)
    assert "confirmados 2" in avisos
    assert "1 no se pudieron persistir" in avisos, "el fallado tiene que avisar"


def test_json_roto_no_rompe_la_pantalla(cliente, monkeypatch):
    from modules.conciliacion import views as v

    def _no_llamar(**kw):  # pragma: no cover
        raise AssertionError("no debe confirmar nada con json roto")
    monkeypatch.setattr(v, "confirmar_match", _no_llamar)

    rv = cliente.post("/conciliacion/hub/confirmar-matches",
                      data={"matches_json": "{esto no es json"})
    assert rv.status_code == 302


# ── deshacer ──────────────────────────────────────────────────────────

def test_deshacer_libera_el_match(cliente, monkeypatch):
    from modules.conciliacion import views as v

    monkeypatch.setattr(v, "romper_match_grupo", lambda **kw: (1, None))

    rv = cliente.post("/conciliacion/banco/deshacer", data={"match_id": "77"})
    assert rv.status_code == 302
    assert "#77 deshecho" in _avisos(cliente)


def test_deshacer_grupal_lo_dice(cliente, monkeypatch):
    """Si el match era parte de un lote, se liberan todos y el aviso tiene
    que decir cuántos: si no, el operador cree que soltó uno solo."""
    from modules.conciliacion import views as v

    monkeypatch.setattr(v, "romper_match_grupo", lambda **kw: (4, "b-1"))

    cliente.post("/conciliacion/banco/deshacer", data={"match_id": "77"})
    avisos = _avisos(cliente)
    assert "4 matches del grupo" in avisos


def test_deshacer_con_id_invalido_avisa_y_no_toca_nada(cliente, monkeypatch):
    from modules.conciliacion import views as v

    def _no_llamar(**kw):  # pragma: no cover
        raise AssertionError("no debe romper nada con un id inválido")
    monkeypatch.setattr(v, "romper_match_grupo", _no_llamar)

    rv = cliente.post("/conciliacion/banco/deshacer", data={"match_id": "no"})
    assert rv.status_code == 302
    assert "inválido" in _avisos(cliente)


def test_deshacer_lo_que_ya_estaba_deshecho_no_miente(cliente, monkeypatch):
    from modules.conciliacion import views as v

    monkeypatch.setattr(v, "romper_match_grupo", lambda **kw: (0, None))

    cliente.post("/conciliacion/banco/deshacer", data={"match_id": "77"})
    assert "no encontré" in _avisos(cliente)


def test_el_contador_no_filtra_por_usuario(cliente, monkeypatch):
    """El bug: la sesión es UNA por banco desde la mig 0189. Si el UPDATE
    sigue filtrando por usuario, deshacer sobre la sesión que abrió otro no
    descuenta nada y el contador queda inflado en silencio."""
    import db as _db
    from modules.conciliacion import views as v

    monkeypatch.setattr(v, "romper_match_grupo", lambda **kw: (1, None))
    sqls = []
    monkeypatch.setattr(_db, "execute",
                        lambda sql, params=None, **k: sqls.append((sql, params)))

    cliente.post("/conciliacion/banco/deshacer", data={"match_id": "77"})

    contador = [s for s, _p in sqls if "matches_hechos" in s]
    assert contador, "no corrió el UPDATE del contador"
    assert "usuario" not in contador[0], (
        "el contador volvió a filtrar por usuario: no va a descontar sobre "
        "la sesión que abrió otro"
    )


def test_rehacer_es_el_espejo_y_tampoco_filtra(cliente, monkeypatch):
    import db as _db
    from modules.conciliacion import views as v

    monkeypatch.setattr(v, "rehacer_match_grupo", lambda **kw: (2, "b-9"))
    sqls = []
    monkeypatch.setattr(_db, "execute",
                        lambda sql, params=None, **k: sqls.append((sql, params)))

    rv = cliente.post("/conciliacion/banco/rehacer", data={"match_id": "77"})
    assert rv.status_code == 302
    contador = [s for s, _p in sqls if "matches_hechos" in s]
    assert contador, "no corrió el UPDATE del contador"
    assert "usuario" not in contador[0]
