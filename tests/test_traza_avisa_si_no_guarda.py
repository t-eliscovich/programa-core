"""Si la traza deja de guardar, se entera alguien.

TMT 2026-08-10, después de que la grabadora dejara de guardar diez minutos por
una columna que no existía: *"y también algo que avise si no está guardando"*.

El fail-soft está bien —una foto no puede tumbar la app— pero el único rastro
era una línea de log que no mira nadie: el error se escribió cien veces y la
pantalla siguió como si nada.

Dos piezas, a propósito distintas:

1. El `except` de `registrar()` deja un aviso en la campanita. Caza el caso
   "explotó al guardar".
2. `/admin/health/traza-fresca` NO pregunta si explotó: pregunta **si hay foto
   nueva**. Caza cualquier motivo —el hilo de fondo muerto, el lock trabado,
   el proceso caído— y no sólo la excepción que alguien pensó en atrapar.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import modules.avisos as av  # noqa: E402
from modules.informes import traza as t  # noqa: E402


def test_la_falla_deja_un_aviso_en_la_campanita():
    visto: dict = {}
    with patch.object(av, "avisar", lambda **kw: visto.update(kw) or True):
        t._avisar_que_no_guarda("column terminado_ukg does not exist")
    assert visto["titulo"] == "La traza dejó de guardar fotos"
    assert "terminado_ukg" in visto["detalle"]
    assert visto["nivel"] == "alerta"
    assert visto["url"] == "/informes/traza"


def test_el_aviso_es_uno_por_dia():
    """🚨 El hilo reintenta cada seis minutos: sin la clave por día el buzón
    se llenaría de cien avisos iguales."""
    from filters import today_ec
    visto: dict = {}
    with patch.object(av, "avisar", lambda **kw: visto.update(kw) or True):
        t._avisar_que_no_guarda("boom")
    assert visto["clave"] == f"traza:falla:{today_ec()}"


def test_avisar_nunca_rompe_al_que_avisa():
    with patch.object(av, "avisar", side_effect=RuntimeError("buzón caído")):
        t._avisar_que_no_guarda("boom")      # no levanta


def test_registrar_avisa_cuando_falla(monkeypatch):
    """La línea nueva del `except`: el aviso sale de la falla real —la misma
    forma que tuvo el 10/08, una columna que no existe."""
    llamado: list = []
    monkeypatch.setattr(t, "_avisar_que_no_guarda", lambda m: llamado.append(m))
    res = t.registrar(origen="test", bal={
        "diagnostico": {"componentes": {"utilidad": 1.0}},
        "stock_etapas": {"hilado": {"kg": 1.0, "ukg": 1.0}}})
    assert res["ok"] is False        # sin base, la foto no se guarda
    assert llamado, "la falla tiene que avisar, no sólo loguear"
    assert llamado[0] == res["motivo"]


def test_el_vigia_mira_si_hay_foto_nueva(app, fake_db):
    """No pregunta '¿explotó?' sino '¿hay foto nueva?'."""
    from modules.admin_dbase import health_audit_view as h

    rid = fake_db.add_role("Admin", ["usuarios.admin"])
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid

    original = h.db.fetch_one

    def _foto_de(minutos):
        # 🚨 Sólo se pisa la consulta de la traza: `fetch_one` también lo usa
        # el login, y pisarlo entero deja la sesión sin rol.
        def _fake(sql, params=None, *a, **k):
            if "traza_utilidad" in (sql or ""):
                return {"creado_en": "2026-08-10 15:43", "min": minutos}
            return original(sql, params, *a, **k)
        return _fake

    with patch.object(h.db, "fetch_one", side_effect=_foto_de(3.2)):
        fresca = c.get("/admin/health/traza-fresca").get_json()
    assert fresca["ok"] is True

    with patch.object(h.db, "fetch_one", side_effect=_foto_de(47.0)):
        vieja = c.get("/admin/health/traza-fresca").get_json()
    assert vieja["ok"] is False
    assert "47 minutos" in vieja["alerts"][0]["msg"]
    assert vieja["stats"]["tope_min"] == h._TRAZA_FRESCA_MIN
