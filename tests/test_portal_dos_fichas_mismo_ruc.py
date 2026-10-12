"""Un RUC en dos fichas, y una de ellas VACÍA.

🐞 12/10/2026 — PGQ (Guaranga Quishpe María Patricia, 45 mil de deuda) y PG1
(una ficha vieja con el mismo RUC, sin una factura ni un cheque). El portal
ordenaba las cuentas por código y "PG1" le gana a "PGQ" por un 1 contra una Q:
la clave quedó guardada en PG1 y el cliente entraba a una cuenta vacía. Desde
"Perfil" escribió: "NO EXISTEN VALORES".

Lo que protegen estos tests:

- la ficha vacía no se muestra si hay otra con movimiento, y se arranca en la
  que más debe;
- la clave sigue sirviendo aunque viva en la ficha vacía (no se le pide clave
  ni datos de nuevo);
- cambiar de cuenta no le pide clave otra vez (AJO/AJ2 tenía el mismo agujero);
- una sesión abierta ANTES del arreglo se acomoda sola en el próximo click.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.portal import acceso, datos  # noqa: E402
from modules.portal import views as pv  # noqa: E402

TPL = ROOT / "modules" / "portal" / "templates" / "portal"

PGQ = {"codigo_cli": "PGQ", "nombre": "GUARANGA QUISHPE MARIA PATRICIA"}
PG1 = {"codigo_cli": "PG1", "nombre": "PG1"}


def _ec(**por_cod):
    """Un `estado_cuenta_lote` falso: `{cod: (saldo, tiene_movimiento)}`."""
    def lote(cods):
        salida = {}
        for c in cods:
            saldo, algo = por_cod.get(c, (0, False))
            salida[c] = {"facturas": [{"saldo": saldo}] if algo else [], "cheques": [],
                         "anticipos": [], "totales": {"saldo_neto": saldo}}
        return salida
    return lote


def _app_portal():
    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False
    return app, deshacer


def _fichas(monkeypatch, clave_en="PG1", datos_en=("PG1",)):
    from modules.informes import queries as iq
    monkeypatch.setattr(iq, "estado_cuenta_lote", _ec(PGQ=(45451.36, True), PG1=(0, False),
                                                      AJO=(100, True), AJ2=(900, True)))
    fic = lambda cod: {"codigo_cli": cod, "nombre": cod, "ruc": "0604978643001", "vend": ""}  # noqa: E731
    monkeypatch.setattr(acceso, "cliente", fic)
    monkeypatch.setattr(acceso, "ficha", fic)
    monkeypatch.setattr(acceso, "acceso", lambda cod: {
        "activo": True, "mail": "", "clave_hash": "x" if cod == clave_en else None})
    monkeypatch.setattr(datos, "completo", lambda cod: cod in datos_en)
    monkeypatch.setattr(datos, "leer", lambda cod: {"celular": "0991234567"} if cod in datos_en else None)


# ---------------------------------------------------------------------------
# Qué cuentas se le muestran
# ---------------------------------------------------------------------------


def test_la_ficha_vacia_no_se_muestra_y_arranca_en_la_que_debe(monkeypatch):
    _fichas(monkeypatch)
    assert [c["codigo_cli"] for c in pv.cuentas_a_mostrar([PG1, PGQ])] == ["PGQ"]


def test_con_dos_que_tienen_algo_quedan_las_dos_la_que_mas_debe_primero(monkeypatch):
    _fichas(monkeypatch)
    cuentas = [{"codigo_cli": "AJO", "nombre": "P"}, {"codigo_cli": "AJ2", "nombre": "P"}]
    assert [c["codigo_cli"] for c in pv.cuentas_a_mostrar(cuentas)] == ["AJ2", "AJO"]


def test_si_estan_todas_vacias_quedan_todas(monkeypatch):
    from modules.informes import queries as iq
    monkeypatch.setattr(iq, "estado_cuenta_lote", _ec())
    assert len(pv.cuentas_a_mostrar([PG1, PGQ])) == 2


def test_si_la_base_no_contesta_no_se_cierra_la_puerta(monkeypatch):
    from modules.informes import queries as iq

    def roto(cods):
        raise RuntimeError("sin base")
    monkeypatch.setattr(iq, "estado_cuenta_lote", roto)
    assert len(pv.cuentas_a_mostrar([PG1, PGQ])) == 2


# ---------------------------------------------------------------------------
# El recorrido de verdad
# ---------------------------------------------------------------------------


def test_entra_con_su_clave_y_cae_en_la_cuenta_con_deuda(monkeypatch):
    """El caso del 12/10: la clave vive en PG1, la deuda en PGQ."""
    app, deshacer = _app_portal()
    try:
        _fichas(monkeypatch)
        monkeypatch.setattr(acceso, "entrar", lambda *a, **k: {
            "ok": True, "mensaje": "", "codigo_cli": "PG1", "cuentas": [PG1, PGQ],
            "primera_vez": False})
        c = app.test_client()
        r = c.post("/ingresar", data={"identificador": "0604978643001", "clave": "x"})
        # Una sola cuenta con algo: entra directo, sin pantalla de elegir.
        assert r.status_code == 302 and not r.headers["Location"].endswith("/mis-cuentas")
        with c.session_transaction() as s:
            assert s[pv.LLAVE] == "PGQ" and s[pv.ACCESO] == "PG1"
            assert [x["codigo_cli"] for x in s[pv.CUENTAS]] == ["PGQ"]
        # Y adentro NO le pide clave de nuevo (la clave vive en PG1) ni sus
        # datos (los cargó mirando PG1).
        r = c.get("/")
        assert r.headers["Location"].endswith("/estado-de-cuenta")
        r = c.get("/mis-datos")
        assert r.status_code == 200
    finally:
        deshacer()


def test_la_sesion_vieja_se_acomoda_sola(monkeypatch):
    """Ella ya estaba adentro, parada en PG1. En el próximo click pasa a PGQ."""
    app, deshacer = _app_portal()
    try:
        _fichas(monkeypatch)
        c = app.test_client()
        with c.session_transaction() as s:
            s[pv.LLAVE] = "PG1"
            s[pv.CUENTAS] = [PG1, PGQ]
        assert c.get("/mis-datos").status_code == 200
        with c.session_transaction() as s:
            assert s[pv.LLAVE] == "PGQ" and s[pv.ACCESO] == "PG1"
    finally:
        deshacer()


def test_cambiar_de_cuenta_no_pide_clave_otra_vez(monkeypatch):
    """AJO/AJ2: la clave vive en AJ2. Antes, pasar a AJO lo mandaba a
    "Elija una clave" (y a cargar sus datos de nuevo)."""
    app, deshacer = _app_portal()
    try:
        _fichas(monkeypatch, clave_en="AJ2", datos_en=("AJ2",))
        cuentas = [{"codigo_cli": "AJ2", "nombre": "P"}, {"codigo_cli": "AJO", "nombre": "P"}]
        c = app.test_client()
        with c.session_transaction() as s:
            s[pv.LLAVE] = "AJ2"
            s[pv.ACCESO] = "AJ2"
            s[pv.CUENTAS] = cuentas
        r = c.post("/mis-cuentas", data={"codigo_cli": "AJO"})
        assert r.status_code == 302
        r = c.get("/")
        assert r.headers["Location"].endswith("/estado-de-cuenta"), r.headers["Location"]
        r = c.get("/mis-datos")
        assert r.status_code == 200
    finally:
        deshacer()


def test_elegir_cuenta_muestra_el_saldo_de_cada_una(monkeypatch):
    app, deshacer = _app_portal()
    try:
        _fichas(monkeypatch, clave_en="AJ2", datos_en=("AJ2",))
        c = app.test_client()
        with c.session_transaction() as s:
            s[pv.LLAVE] = "AJ2"
            s[pv.ACCESO] = "AJ2"
            s[pv.CUENTAS] = [{"codigo_cli": "AJ2", "nombre": "P"}, {"codigo_cli": "AJO", "nombre": "P"}]
        html = c.get("/mis-cuentas").get_data(as_text=True)
        assert "Saldo $ 900,00" in html and "Saldo $ 100,00" in html
    finally:
        deshacer()


def test_la_clave_se_guarda_donde_vive_su_acceso(monkeypatch):
    app, deshacer = _app_portal()
    try:
        _fichas(monkeypatch)
        guardadas = []
        monkeypatch.setattr(acceso, "guardar_clave",
                            lambda cod, clave: guardadas.append(cod) or (True, "ok"))
        monkeypatch.setattr(acceso, "guardar_mail", lambda *a, **k: None)
        c = app.test_client()
        with c.session_transaction() as s:
            s[pv.LLAVE] = "PGQ"
            s[pv.ACCESO] = "PG1"
            s[pv.CUENTAS] = [PGQ]
        c.post("/elegir-clave", data={"clave": "unaclave123", "repetir": "unaclave123"})
        assert guardadas == ["PG1"]
    finally:
        deshacer()


# ---------------------------------------------------------------------------
# Textos: los clientes son de Ecuador, se les habla de usted
# ---------------------------------------------------------------------------


def test_al_cliente_no_se_le_habla_de_vos():
    import re
    vos = re.compile(r"\b(Probaste|Esperá|[Ll]lamanos|entrá|Elegí|Probá|Tu acceso|tu clave)\b")
    textos = [p.read_text(encoding="utf-8") for p in TPL.glob("*.html")]
    for py in ("acceso.py", "views.py"):
        fuente = (ROOT / "modules" / "portal" / py).read_text(encoding="utf-8")
        # Sólo lo que se le muestra: los textos entre comillas, no los comentarios.
        textos += re.findall(r'"([^"\n]*)"', fuente)
    for t in textos:
        t = re.sub(r"\{#.*?#\}", "", t, flags=re.S)
        assert not vos.search(t), vos.search(t).group(0)
