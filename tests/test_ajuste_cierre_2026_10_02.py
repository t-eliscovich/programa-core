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
    """Un PDF mínimo escrito a mano (sin reportlab, que el CI no tiene). Cada
    página es un texto, o una lista de (x, y, texto) en celdas separadas como
    las pone Chromium (BT … Tm … Tj ET por celda)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for t in textos:
        celdas = [(50, 700, t)] if isinstance(t, str) else t
        flujo = " ".join(f"BT /F1 9 Tf 1 0 0 1 {x} {y} Tm ({c}) Tj ET"
                         for x, y, c in celdas).encode("latin-1")
        objs.append(f"<< /Length {len(flujo)} >>\nstream\n".encode("latin-1")
                    + flujo + b"\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        body = o if isinstance(o, bytes) else o.encode("latin-1")
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return out


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


_HOJA = [(38, 730, "Ventas"), (220, 730, "345.817"), (338, 730, "8,531"),
         (440, 730, "2.950.184"),
         (38, 560, "Utilidad Real"), (336, 560, "2,328"), (445, 560, "804.955"),
         (38, 486, "Utilidades 2026"), (440, 486, "5.202.316"),
         (38, 340, "Hilado"), (262, 340, "2.231.232"), (317, 340, "3,182"),
         (464, 340, "7.100.285"),
         (38, 323, "Tejido"), (269, 323, "292.217"), (317, 323, "3,682"),
         (464, 323, "1.076.010"),
         (38, 307, "Terminado"), (269, 307, "317.933"), (317, 307, "5,382"),
         (464, 307, "1.711.187"),
         (38, 290, "Stock MP+Prod."), (262, 290, "2.841.382"), (317, 290, "3,480"),
         (537, 290, "9.887.482"),
         (38, 210, "Total activo"), (529, 210, "26.658.002"),
         (38, 175, "Patrimonio neto"), (521, 175, "22.287.800")]

_AJ = [{"importe": -191045.82, "motivo": "Asinfo corrigió el saldo",
        "lineas": [{"etapa": "hilado", "kg": -51775, "importe": -164758.4},
                   {"etapa": "tejido", "kg": -7139, "importe": -26287.42}]}]


def test_la_hoja_es_la_original_con_los_numeros_del_ajuste():
    """Tamara: *"¿por qué hacés otro formato?"* — es la misma página, sólo
    cambian los números que mueve el ajuste."""
    from pypdf import PdfReader
    page = PdfReader(io.BytesIO(_pdf([_HOJA]))).pages[0]
    tx = cierres_paquete._textos_con_posicion(page)
    assert {t["t"]: (round(t["x"]), round(t["y"])) for t in tx}["7.100.285"] == (464, 340)
    cambios = {c["t"]: c["nuevo"] for c in
               cierres_paquete.cambios_hoja_resultados(tx, _AJ, 2026)}
    assert cambios == {
        "804.955": "613.909", "2,328": "1,775", "5.202.316": "5.011.270",
        "2.231.232": "2.179.457", "7.100.285": "6.935.527",
        "292.217": "285.078", "1.076.010": "1.049.723",
        "2.841.382": "2.782.468", "9.887.482": "9.696.436", "3,480": "3,485",
        "26.658.002": "26.466.956", "22.287.800": "22.096.754"}
    # Terminado no se tocó: no estaba en el ajuste.
    assert "1.711.187" not in cambios and "317.933" not in cambios


def test_la_hoja_corregida_se_estampa_sobre_la_original(monkeypatch):
    from pypdf import PdfReader
    from modules.informes import ajuste_cierre as aj
    original = _pdf([_HOJA, "VENTAS DEL MES"])
    monkeypatch.setattr(cierres_paquete, "obtener", lambda a, m, v=1: original)
    monkeypatch.setattr(aj, "vivos_del_mes", lambda a, m: _AJ)
    monkeypatch.setattr(cierres_paquete, "today_ec", lambda: dt.date(2026, 10, 2))
    pdf, cambios = cierres_paquete.hoja_resultados_corregida(2026, 9)
    r = PdfReader(io.BytesIO(pdf))
    assert len(r.pages) == 1
    texto = r.pages[0].extract_text()
    assert "613.909" in texto and "22.096.754" in texto
    assert "Corregido el 02/10/2026" in texto
    assert len(cambios) == 12


def test_la_hoja_sin_la_fila_levanta():
    from pypdf import PdfReader
    page = PdfReader(io.BytesIO(_pdf([[(38, 700, "Ventas"), (220, 700, "1")]]))).pages[0]
    with pytest.raises(RuntimeError, match="Utilidad Real"):
        cierres_paquete.cambios_hoja_resultados(
            cierres_paquete._textos_con_posicion(page), _AJ, 2026)
