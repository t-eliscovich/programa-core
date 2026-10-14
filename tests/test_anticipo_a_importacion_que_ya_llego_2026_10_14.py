"""Anticipo a una importación que YA LLEGÓ — Tamara 14/10/2026.

Traza 14:09, −74.601: Andrés cargó $ 71.021,05 como "ANTICIPO AC 49/26"
cuando quería la AI 49. La AC 49 había llegado el 09/10 y tenía su compra,
así que el anticipo no quedó como anticipo (el sintético lo tapa y autobap lo
pasa a compra): salió del banco y la utilidad bajó, sin un cartel.
*"Tiene que haber un cartel más informativo — ojo, esta compra ya llegó"*.

Lo que fijan estos tests:
  1. el picker sabe cuál llegó (`recibida`, `fecha_recepcion`);
  2. `ya_llego` encuentra la AC 49/26 y respeta el año;
  3. `sin_llegar_con_numero` ofrece la AI 49 (otro proveedor, no llegó);
  4. el alta NO carga sin confirmar, y con `ya_llego_ok=1` sí (flete/CAE/saldo);
  5. la pantalla trae el aviso y el campo de confirmación;
  6. la traza ya no dice "no mueve plata".
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.importaciones import picker, service


def _filas():
    return [
        {"im_numero": "IM-0000601", "codigo": "AC 49", "prov": "AC", "numero": 49,
         "nota": "ACMT (AC 49)", "fecha": "2026-08-01", "anio": 2026,
         "recibida": True, "fecha_recepcion": "2026-10-09",
         "compra": {"n": 1, "importe_total": 84746.91}},
        {"im_numero": "IM-0000602", "codigo": "AC 65", "prov": "AC", "numero": 65,
         "nota": "ACMT (AC 65)", "fecha": "2026-09-01", "anio": 2026,
         "recibida": False, "fecha_recepcion": None,
         "compra": {"n": 0, "importe_total": 0.0}},
        {"im_numero": "IM-0000603", "codigo": "AI 49", "prov": "AI", "numero": 49,
         "nota": "AI (AI 49)", "fecha": "2026-09-10", "anio": 2026,
         "recibida": False, "fecha_recepcion": None,
         "compra": {"n": 0, "importe_total": 0.0}},
        # Misma AC 49 pero de la campaña 2025, sin llegar: no debe confundir.
        {"im_numero": "IM-0000300", "codigo": "AC 49", "prov": "AC", "numero": 49,
         "nota": "ACMT (AC 49)", "fecha": "2025-08-01", "anio": 2025,
         "recibida": False, "fecha_recepcion": None,
         "compra": {"n": 0, "importe_total": 0.0}},
    ]


@pytest.fixture
def asinfo():
    with patch.object(service, "importaciones_con_cruce", return_value=_filas()), \
            patch.object(service, "_anio_de", lambda r: {"anio": r.get("anio")}):
        yield


def _login(app, fake_db, perms):
    rid = fake_db.add_role("Tester", perms)
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


# 1 · el picker dice cuál llegó -------------------------------------------------

def test_el_picker_dice_cual_llego(app, fake_db, asinfo):
    c = _login(app, fake_db, ["compras.ver"])
    ims = c.get("/importaciones/_api/abiertas/AC").get_json()["importaciones"]
    ac49 = next(i for i in ims if i["numero"] == 49)
    assert ac49["recibida"] is True
    assert ac49["fecha_recepcion"] == "2026-10-09"
    assert ac49["compras_usd"] == pytest.approx(84746.91)
    assert next(i for i in ims if i["numero"] == 65)["recibida"] is False


# 2 · ya_llego ----------------------------------------------------------------

def test_ya_llego_encuentra_la_ac_49(asinfo):
    f = picker.ya_llego("AC", "49/26 ")
    assert f and f["codigo"] == "AC 49" and f["fecha_recepcion"] == "2026-10-09"
    # Sin año: la campaña más alta del proveedor (2026), que llegó.
    assert picker.ya_llego("ac", "49 SALDO")


def test_ya_llego_respeta_el_anio_y_lo_que_no_llego(asinfo):
    assert picker.ya_llego("AC", "49/25") is None      # la de 2025 no llegó
    assert picker.ya_llego("AC", "65/26") is None      # no llegó
    assert picker.ya_llego("AI", "49/26") is None      # la que quería Andrés
    assert picker.ya_llego("AC", "pago sin número") is None


# 3 · "¿era la AI 49?" ----------------------------------------------------------

def test_sugiere_la_ai_49(app, fake_db, asinfo):
    c = _login(app, fake_db, ["compras.ver"])
    ims = c.get("/importaciones/_api/sin-llegar/49?excluir=AC").get_json()["importaciones"]
    assert [i["codigo"] for i in ims] == ["AI 49"]


# 4 · el alta frena sin confirmar -----------------------------------------------

def _patch_alta(monkeypatch):
    import contextlib

    import bank_helpers
    import db as dbmod
    import mov_doble as _md
    import periodo_guard
    ins: list = []

    @contextlib.contextmanager
    def _tx():
        yield object()

    monkeypatch.setattr(dbmod, "tx", _tx)
    monkeypatch.setattr(dbmod, "execute_returning",
                        lambda sql, params=None, conn=None: (ins.append(sql) or {"id_dolares": 1}))
    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario",
                        lambda *a, **k: {"id_transaccion": 99})
    monkeypatch.setattr(_md, "registrar", lambda **k: 1)
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **k: None)
    return ins


def test_no_carga_a_la_que_ya_llego_sin_confirmar(app, fake_db, monkeypatch, asinfo):
    c = _login(app, fake_db, ["facturas.crear"])
    ins = _patch_alta(monkeypatch)
    r = c.post("/dolares/nuevo-anticipo", data={
        "fecha": "2026-10-14", "cta": "AC", "concepto": "49/26", "importe": "71.021,05"})
    assert r.status_code == 302
    assert not [s for s in ins if "INSERT INTO scintela.dolares" in s]
    with c.session_transaction() as s:
        msgs = " ".join(m for _, m in s.get("_flashes", []))
    assert "AC 49 ya llegó el 09/10" in msgs


def test_confirmado_carga_igual(app, fake_db, monkeypatch, asinfo):
    """El flete, el CAE y el saldo llegan después de la recepción: se cargan."""
    c = _login(app, fake_db, ["facturas.crear"])
    ins = _patch_alta(monkeypatch)
    c.post("/dolares/nuevo-anticipo", data={
        "fecha": "2026-10-14", "cta": "AC", "concepto": "49/26 CAE",
        "importe": "2.700,00", "ya_llego_ok": "1"})
    assert [s for s in ins if "INSERT INTO scintela.dolares" in s]


def test_la_que_no_llego_carga_sin_preguntar(app, fake_db, monkeypatch, asinfo):
    c = _login(app, fake_db, ["facturas.crear"])
    ins = _patch_alta(monkeypatch)
    c.post("/dolares/nuevo-anticipo", data={
        "fecha": "2026-10-14", "cta": "AI", "concepto": "49/26", "importe": "71.021,05"})
    assert [s for s in ins if "INSERT INTO scintela.dolares" in s]


# 5 · la pantalla trae el aviso ---------------------------------------------------

def test_la_pantalla_trae_el_aviso(app, fake_db):
    c = _login(app, fake_db, ["facturas.crear", "facturas.ver"])
    html = c.get("/dolares").get_data(as_text=True)
    assert 'id="dol-llego"' in html
    assert 'name="ya_llego_ok"' in html
    assert "/importaciones/_api/sin-llegar/" in html
    assert "es flete, CAE o saldo de la" in html


# 6 · la traza no dice "no mueve plata" ------------------------------------------

def test_la_traza_no_dice_no_mueve_plata():
    import inspect

    from modules.informes import traza
    src = inspect.getsource(traza._unir_anticipo_de_mercaderia_ya_recibida)
    assert "baja la utilidad" in src
    assert 'ent["nota"] = "la mercadería ya estaba en stock' not in src
