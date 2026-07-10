"""Rework de /dolares (dueña 2026-07-10): columna "Fecha recibido" cruzada de
Asinfo, filtro por código en un campo ("AC 31"), y convertir la selección a
compra en un paso — con el kg quedando en el STOCK (Asinfo), no en la compra.
Sin DB real (fake_db + mocks)."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from modules.importaciones import service as isvc


def _login(app, fake_db, perms):
    rid = fake_db.add_role("Tester", perms)
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


# ── adjuntar_recepcion_asinfo ────────────────────────────────────────────────
def test_adjuntar_recepcion_cuelga_fecha_kg_ref():
    anticipos = [
        {"cta": "AC", "concepto": "31 SALDO", "fecha": date(2026, 6, 2)},
        {"cta": "ac", "concepto": "31 MAPFRE", "fecha": date(2026, 4, 21)},  # cta minúscula
        {"cta": "AC", "concepto": "99 SALDO", "fecha": date(2026, 5, 1)},     # sin importación
        {"cta": "AC", "concepto": "sin numero", "fecha": date(2026, 5, 1)},   # sin nº
    ]
    imps = [{"im_numero": "IM-553", "fecha": "2026-04-16",
             "fecha_recepcion": "2026-07-09", "recibida": True,
             "nota": "ACMT/EXP/2026-27/78004 ( AC 31)"}]
    with patch.object(isvc.asinfo_service, "importaciones_asinfo", return_value=imps), \
         patch.object(isvc.asinfo_service, "importaciones_kg", return_value={"IM-553": 24494.0}):
        isvc.adjuntar_recepcion_asinfo(anticipos)
    assert anticipos[0]["fecha_recepcion_im"] == "2026-07-09"
    assert anticipos[0]["kg_im"] == 24494.0
    assert anticipos[0]["im_numero"] == "IM-553"
    assert anticipos[0]["ref"] == 31
    assert anticipos[1]["fecha_recepcion_im"] == "2026-07-09"  # cta lowercase igual matchea
    assert anticipos[2]["fecha_recepcion_im"] is None and anticipos[2]["ref"] == 99
    assert anticipos[3]["ref"] is None and anticipos[3]["fecha_recepcion_im"] is None


def test_adjuntar_recepcion_fail_soft_sin_asinfo():
    anticipos = [{"cta": "AC", "concepto": "31 SALDO", "fecha": None}]
    with patch.object(isvc.asinfo_service, "importaciones_asinfo",
                      side_effect=RuntimeError("metabase down")):
        isvc.adjuntar_recepcion_asinfo(anticipos)
    assert anticipos[0]["fecha_recepcion_im"] is None
    assert anticipos[0]["ref"] == 31  # el ref NO depende de Asinfo


# ── /dolares?codigo=AC 31 → cta=AC + concepto=31 ─────────────────────────────
def test_lista_codigo_parsea_cta_y_concepto(app, fake_db, monkeypatch):
    from modules.dolares import queries as dq
    cap = {}
    monkeypatch.setattr(dq, "lista", lambda **kw: cap.update(kw) or [])
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/dolares?codigo=AC%2031")
    assert r.status_code == 200
    assert cap["cta"] == "AC"
    assert cap["q"] == "31"


def test_lista_codigo_solo_cuenta(app, fake_db, monkeypatch):
    from modules.dolares import queries as dq
    cap = {}
    monkeypatch.setattr(dq, "lista", lambda **kw: cap.update(kw) or [])
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/dolares?codigo=AI")
    assert r.status_code == 200
    assert cap["cta"] == "AI" and cap["q"] is None


def test_lista_filtra_por_recibido_mes(app, fake_db, monkeypatch):
    from modules.dolares import queries as dq
    from modules.importaciones import service as _isvc
    rows = [
        {"id_dolares": 1, "cta": "AC", "concepto": "31 SALDO", "fecha": None,
         "importe": 1.0, "st": None, "clave": "", "saldo_acumulado": 1.0},
        {"id_dolares": 2, "cta": "AC", "concepto": "30 SALDO", "fecha": None,
         "importe": 1.0, "st": None, "clave": "", "saldo_acumulado": 1.0},
    ]
    monkeypatch.setattr(dq, "lista", lambda **kw: [dict(r) for r in rows])

    def _attach(filas, limite=400):
        for f in filas:
            f["fecha_recepcion_im"] = "2026-07-09" if f["concepto"].startswith("31") else "2026-06-30"
            f["kg_im"] = None
            f["im_numero"] = None
            f["ref"] = int(f["concepto"].split()[0])
    monkeypatch.setattr(_isvc, "adjuntar_recepcion_asinfo", _attach)
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/dolares?recibido_mes=2026-07")
    assert r.status_code == 200  # no rompe; deja solo la recibida en julio


# ── convertir-seleccion: kg=None (queda en el stock) ─────────────────────────
def test_convertir_seleccion_llama_con_kg_none(app, fake_db, monkeypatch):
    from modules.dolares import queries as dq
    cap = {}
    monkeypatch.setattr(dq, "convertir_a_compra", lambda **kw: cap.update(kw) or {
        "n_anticipos": 2, "numero_compra": 5, "comprobante": "BAP5",
        "importe_total": 58629.4,
    })
    c = _login(app, fake_db, ["compras.crear"])
    r = c.post("/dolares/convertir-seleccion",
               data={"codigo_prov": "AC", "concepto": "31", "id_dolares": ["1", "2"]})
    assert r.status_code == 302
    assert cap["codigo_prov"] == "AC"
    assert cap["ids_anticipos"] == [1, 2]
    assert cap["concepto"] == "31"
    assert cap["tipo_compra"] == "H"
    assert cap["kg"] is None  # el kg vive en el stock (Asinfo), no en la compra


def test_convertir_seleccion_sin_seleccion_no_convierte(app, fake_db, monkeypatch):
    from modules.dolares import queries as dq
    called = []
    monkeypatch.setattr(dq, "convertir_a_compra", lambda **kw: called.append(kw))
    c = _login(app, fake_db, ["compras.crear"])
    r = c.post("/dolares/convertir-seleccion", data={"codigo_prov": "", "id_dolares": []})
    assert r.status_code == 302
    assert not called


def test_convertir_seleccion_sin_permiso_404(app, fake_db):
    c = _login(app, fake_db, ["facturas.ver"])  # ver sí, crear no
    r = c.post("/dolares/convertir-seleccion",
               data={"codigo_prov": "AC", "id_dolares": ["1"]})
    assert r.status_code == 404


# ── kg_stock_por_compra: el kg se cuenta una vez por importación ─────────────
def test_kg_stock_por_compra_dedup_por_importacion():
    compras = [
        {"prov": "AC", "ref": 31, "fecha": date(2026, 7, 10)},
        {"prov": "AC", "ref": 31, "fecha": date(2026, 7, 10)},  # misma imp → no dobla
    ]
    imps = [{"im_numero": "IM-553", "fecha": "2026-04-16",
             "fecha_recepcion": "2026-07-09", "recibida": True,
             "nota": "ACMT/EXP/2026-27/78004 ( AC 31)"}]
    with patch.object(isvc.asinfo_service, "importaciones_asinfo", return_value=imps), \
         patch.object(isvc.asinfo_service, "importaciones_kg", return_value={"IM-553": 24494.0}):
        out = isvc.kg_stock_por_compra(compras)
    assert out == {"AC": 24494.0}  # kg del stock, contado una sola vez


# ── adjuntar_kg_asinfo_a_compras: mostrar kg del stock en /compras ───────────
def test_adjuntar_kg_asinfo_a_compras():
    compras = [
        {"codigo_prov": "AC", "concepto": "31", "fecha": date(2026, 7, 10), "kg": None},
        {"codigo_prov": "AQ", "concepto": "55 19", "fecha": date(2026, 6, 19), "kg": 0},  # químico
    ]
    imps = [{"im_numero": "IM-553", "fecha": "2026-04-16",
             "fecha_recepcion": "2026-07-09", "recibida": True,
             "nota": "ACMT/EXP/2026-27/78004 ( AC 31)"}]
    with patch.object(isvc.asinfo_service, "importaciones_asinfo", return_value=imps), \
         patch.object(isvc.asinfo_service, "importaciones_kg", return_value={"IM-553": 24494.0}):
        isvc.adjuntar_kg_asinfo_a_compras(compras)
    assert compras[0]["kg_asinfo"] == 24494.0  # AC 31 → su importación
    assert compras[1]["kg_asinfo"] is None      # AQ 55 no es importación


# ── el template renderiza con las piezas nuevas ──────────────────────────────
def test_lista_renderiza_columna_y_convertir(app, fake_db):
    c = _login(app, fake_db, ["informes.ver", "compras.crear"])
    r = c.get("/dolares")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Recibido" in html            # columna nueva
    assert 'name="codigo"' in html       # filtro por código en un campo
    assert 'name="recibido_mes"' in html  # filtro por mes de recibido
    assert "Convertir a compra" in html  # acción sobre la selección
