"""Ajuste al cierre del mes anterior + PDF corregido + aviso de salto de la
utilidad (Tamara 02/10/2026).

A las 13:50 Asinfo corrigió el saldo de las bodegas (−51.775 kg de hilo,
−7.139 kg de tela cruda): la utilidad de octubre bajó 191.504 de golpe y la
campanita no dijo nada. Esos kilos ya no estaban al 30/09, así que se
absorben en septiembre (`ajuste_cierre`), el PDF del cierre se rearma como
segundo PDF, y un salto grande entre dos fotos de la traza avisa.
"""
from __future__ import annotations

import datetime as dt
import inspect
import io

import pytest

from modules.informes import ajuste_cierre, cierres_paquete, traza


def _login(app, fake_db, perms):
    rid = fake_db.add_role("Tester", perms)
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


# ── El ajuste toca la foto de cierre ENTERA, no sólo el patrimonio ──────────

def test_el_ajuste_mueve_stock_patrimonio_y_utilidad_juntos():
    """El ajuste del 03/09 (retirado) bajaba sólo el patrimonio y rompía
    `historia_balance_cierra` (activo = deuda + patrimonio)."""
    src = inspect.getsource(ajuste_cierre._sumar)
    for col in ("ustock", "stock", "patrimonio", "usuti"):
        assert f"{col} = COALESCE({col}, 0) +" in src


def test_calcular_valua_los_kilos_al_ukg_del_cierre(monkeypatch):
    monkeypatch.setattr(ajuste_cierre, "cierre_vigente", lambda: {
        "id_historia": 749, "fecha": dt.date(2026, 9, 30), "kvent": 345817.49,
        "ustock": 9887482.02, "stock": 2841381.7, "patrimonio": 22287799.68,
        "usuti": 804955.02})
    monkeypatch.setattr(ajuste_cierre, "tarifas_del_cierre", lambda f: {
        "hilado": 3.1822, "tejido": 3.682226859916865, "terminado": 5.38})
    c = ajuste_cierre.calcular([{"etapa": "hilado", "kg": -51775},
                                {"etapa": "tejido", "kg": -7139},
                                {"etapa": "otra", "kg": 5}])
    assert [ln["etapa"] for ln in c["lineas"]] == ["hilado", "tejido"]
    assert c["importe"] == pytest.approx(-191045.8, abs=1)
    assert c["despues"]["usuti"] == pytest.approx(804955.02 - 191045.8, abs=1)
    assert c["despues"]["ustock"] - c["antes"]["ustock"] == pytest.approx(c["importe"])
    assert c["despues"]["patrimonio"] - c["antes"]["patrimonio"] == pytest.approx(c["importe"])
    assert c["kg"] == -58914
    assert round(c["ukg_despues"], 3) == 1.775


def test_aplicar_exige_motivo(monkeypatch):
    with pytest.raises(ValueError, match="motivo"):
        ajuste_cierre.aplicar(lineas=[{"etapa": "hilado", "kg": -1}], motivo="  ")


def test_aplicar_solo_el_cierre_del_mes_anterior(monkeypatch):
    monkeypatch.setattr(ajuste_cierre, "calcular", lambda lineas: {
        "lineas": [{"etapa": "hilado", "kg": -1, "ukg": 3, "importe": -3}],
        "importe": -3, "kg": -1, "id_historia": 1, "fecha": dt.date(2026, 8, 31)})
    monkeypatch.setattr(ajuste_cierre, "today_ec", lambda: dt.date(2026, 10, 2))
    with pytest.raises(ValueError, match="sólo se ajusta"):
        ajuste_cierre.aplicar(lineas=[], motivo="x")


def test_regrabar_la_foto_vuelve_a_aplicar_los_ajustes():
    src = inspect.getsource(__import__("modules.informes.queries",
                                       fromlist=["x"]).crear_snapshot_historia)
    assert "_aj.reaplicar(conn" in src
    assert "if borradas:" in src


def test_movimientos_de_ajustes_nombran_el_salto_del_patant(monkeypatch):
    import db
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [
        {"id_ajuste": 7, "anio": 2026, "mes": 9, "motivo": "Asinfo corrigió",
         "importe": -191045.0, "creado_ahora": True, "deshecho_ahora": False}])
    movs = ajuste_cierre.movimientos_de_ajustes("d", "h")
    assert len(movs) == 1
    m = movs[0]
    assert m["componente"] == "patant" and m["regla"] == "ajuste_cierre"
    assert m["aporte"] == 191045.0           # el mes en curso recupera
    assert "septiembre" in m["etiqueta"]


def test_la_traza_agrega_los_ajustes_y_avisa_el_salto():
    src = inspect.getsource(traza.registrar)
    assert "movimientos_de_ajustes" in src
    assert "avisar_salto(idt, fila, previa, movs)" in src


# ── La campanita ────────────────────────────────────────────────────────────

def _t(h, mi, d=2):
    return dt.datetime(2026, 10, d, h, mi, tzinfo=dt.UTC)


def test_salto_grande_de_utilidad_avisa_con_los_kilos():
    previa = {"utilidad": 38581, "creado_en": _t(18, 45), "hilado_kg": 2210740,
              "tejido_kg": 299137, "terminado_kg": 306004}
    fila = {"utilidad": -152924, "creado_en": _t(18, 50), "hilado_kg": 2158964,
            "tejido_kg": 291998, "terminado_kg": 305919}
    t = traza.texto_del_salto(fila, previa, [])
    assert t["nivel"] == "alerta"
    assert "bajó 191.505" in t["titulo"]
    assert "hilo −51.776 kg" in t["detalle"]
    assert "tela cruda −7.139 kg" in t["detalle"]
    assert "terminado" not in t["detalle"]       # −85 kg es un despacho normal


def test_salto_chico_o_cambio_de_mes_no_avisa():
    previa = {"utilidad": 805000, "creado_en": _t(4, 54, 1)}
    assert traza.texto_del_salto({"utilidad": 830000, "creado_en": _t(4, 59, 1)},
                                 previa, []) is None
    previa = {"utilidad": 805423, "creado_en": dt.datetime(2026, 10, 1, 4, 54, tzinfo=dt.UTC)}
    fila = {"utilidad": 415, "creado_en": dt.datetime(2026, 10, 1, 5, 0, tzinfo=dt.UTC)}
    assert traza.texto_del_salto(fila, previa, []) is None   # 00:00 EC: cierre


def test_salto_explicado_por_un_ajuste_al_cierre_es_informativo():
    previa = {"utilidad": -152000, "creado_en": _t(19, 30)}
    fila = {"utilidad": 39045, "creado_en": _t(19, 35)}
    movs = [{"regla": "ajuste_cierre", "aporte": 191045,
             "etiqueta": "Ajuste al cierre de septiembre · Asinfo"}]
    t = traza.texto_del_salto(fila, previa, movs)
    assert t["nivel"] == "ok"
    assert "sube 191.045" in t["titulo"]


def test_resolver_vuelve_no_leido_para_todos_y_sube():
    from modules.avisos import queries as avisos
    src = inspect.getsource(avisos.resolver)
    assert "DELETE FROM scintela.aviso_leido WHERE id_aviso" in src
    assert "creado_en = now()" in src


def test_cuando_asinfo_arregla_se_olvida_lo_calculado():
    from modules.asinfo import salidas_sin_saldo as s
    assert "olvidar_lo_calculado()" in inspect.getsource(s.avisar_arreglos)
    src = inspect.getsource(s.olvidar_lo_calculado)
    assert "kardex_bodegas.reset_cache()" in src
    assert "reset_flujo_produccion_cache()" in src


# ── Pantallas ───────────────────────────────────────────────────────────────

def test_kg_escritos_a_mano():
    from modules.informes.views import _kg_de
    assert _kg_de("−51.775") == -51775
    assert _kg_de("-51775.42") == -51775.42
    assert _kg_de("-51.775,4") == -51775.4
    assert _kg_de("") == 0


def test_ajuste_al_cierre_solo_admin(app, fake_db):
    c = _login(app, fake_db, ["informes.ver"])
    assert c.get("/informes/cierres/ajuste").status_code == 404
    assert c.post("/informes/cierres/ajuste/aplicar").status_code in (400, 404)
    assert c.post("/informes/cierres/2026/9/corregido").status_code in (400, 404)


def test_ajuste_al_cierre_muestra_la_vista_previa(app, fake_db, monkeypatch):
    monkeypatch.setattr(ajuste_cierre, "cierre_vigente", lambda: {
        "id_historia": 749, "fecha": dt.date(2026, 9, 30), "kvent": 345817.49,
        "ustock": 9887482.02, "stock": 2841381.7, "patrimonio": 22287799.68,
        "usuti": 804955.02})
    monkeypatch.setattr(ajuste_cierre, "tarifas_del_cierre", lambda f: {
        "hilado": 3.1822, "tejido": 3.6822, "terminado": 5.38, "kg": {}})
    monkeypatch.setattr(ajuste_cierre, "listar", lambda *a, **k: [])
    c = _login(app, fake_db, ["usuarios.admin"])
    r = c.get("/informes/cierres/ajuste?kg_hilado=-51.775&kg_tejido=-7139&motivo=Asinfo")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Ajuste al cierre de septiembre" in body
    assert "613.9" in body                       # utilidad que queda
    assert "Aplicar el ajuste" in body


# ── El segundo PDF ──────────────────────────────────────────────────────────

def test_guardar_y_obtener_por_version():
    src = inspect.getsource(cierres_paquete.guardar)
    assert "ON CONFLICT (anio, mes, version)" in src
    assert "AND version = %s" in inspect.getsource(cierres_paquete.obtener)


def _pdf(textos):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for t in textos:
        c.drawString(50, 700, t)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_partir_el_original_por_secciones():
    pdf = _pdf(["RESULTADOS", "VENTAS DEL MES", "CLI KG", "Cartera", "Deudas",
                "Gastos del mes", "MOVIMIENTOS DEL MES (INICIAL ASINFO)",
                "Activos fijos", "Anticipos USD"])
    p = cierres_paquete._partir_original(pdf)
    assert (p["ventas"], p["flujo"], p["activos"], p["n"]) == (1, 6, 7, 9)


def test_partir_el_original_sin_secciones_levanta():
    with pytest.raises(RuntimeError, match="secciones"):
        cierres_paquete._partir_original(_pdf(["algo", "otra cosa"]))


def test_la_descarga_pide_la_version(app, fake_db, monkeypatch):
    pedido = {}

    def _ob(a, m, v=1):
        pedido["v"] = v
        return b"%PDF-x"
    monkeypatch.setattr(cierres_paquete, "obtener", _ob)
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/informes/cierres/2026/9/pdf?version=2")
    assert r.status_code == 200 and pedido["v"] == 2
    assert "corregido" in r.headers["Content-Disposition"]
