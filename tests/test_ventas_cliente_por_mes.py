"""Ventas por mes de UN cliente (kilos y dólares) — /informes/ventas/cliente/<cod>.

TMT 2026-09-11 — pedido de Andrés por WhatsApp: *"me gustaría poder ver las
ventas por mes del último año de un cliente — kilos y dólares"*.

Lo que estos tests protegen:

· La ventana son SIEMPRE `meses` filas terminando en el mes en curso, con los
  meses sin venta en cero (que un cliente no compre en marzo se tiene que VER,
  no desaparecer de la grilla).
· La fuente es ASINFO (`facturas_periodo`), no `scintela.factura`: la tabla
  propia tiene la historia rota (faltan facturas ene–jun 2026 y hay filas del
  backfill duplicadas), y Tamara (11/09) pidió no correr arreglos de datos.
  El cliente se busca por todos sus códigos de Asinfo (aliases) y el rango
  pedido es el del warmup, para pegarle al caché.
· Si Asinfo no contesta, `fuente_caida` avisa: cero filas NO es cero ventas.
· Un código que no existe da 404, no una pantalla vacía.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from modules.informes import queries
from modules.iniciales.queries import MESES_ES

HOY = date(2026, 9, 11)


def _fakes(monkeypatch, docs, cliente=True, aliases=("ABC", "AB2")):
    """Parchea el maestro de clientes (db), facturas_periodo (Asinfo) y aliases."""
    import db
    from modules.asinfo import aliases as al
    from modules.asinfo import service as asvc

    capturado: dict = {}

    def fetch_one(sql, params=None, conn=None):
        capturado["cliente_params"] = params
        return {"codigo_cli": "ABC", "nombre": "ABC TEXTIL"} if cliente else None

    def facturas_periodo(desde, hasta, max_edad_secs=None):
        capturado["rango"] = (desde, hasta)
        return docs

    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(asvc, "facturas_periodo", facturas_periodo)
    monkeypatch.setattr(al, "to_asinfo", lambda c: list(aliases))
    return capturado


def _doc(cli, fecha, kg, usd, tipo="FACTURA"):
    return {"tipo": tipo, "fecha": fecha, "numero": "001-099-000000001",
            "cliente_codigo": cli, "kg": kg, "usd": usd}


def test_doce_filas_terminando_en_el_mes_en_curso_con_ceros(monkeypatch):
    cap = _fakes(
        monkeypatch,
        [
            _doc("ABC", date(2026, 9, 3), 60, 300.0),
            _doc("ABC", "2026-09-10", 40, 200.0),            # fecha como texto también
            _doc("AB2", date(2025, 10, 20), 50, 200.0),      # alias del mismo cliente
            _doc("ABC", date(2025, 10, 25), -5, -20.0, "DEVOLUCION"),  # resta
            _doc("XYZ", date(2026, 9, 3), 999, 9999.0),      # otro cliente: fuera
            _doc("ABC", date(2025, 9, 30), 999, 9999.0),     # antes de la ventana: fuera
        ],
    )
    with patch.object(queries, "today_ec", return_value=HOY):
        data = queries.ventas_cliente_por_mes("abc")

    assert data["cliente"] == {"codigo_cli": "ABC", "nombre": "ABC TEXTIL"}
    assert cap["cliente_params"] == ("ABC",)  # se normaliza a mayúsculas
    # El rango pedido a Asinfo es el del warmup: 2025-01-01 → fin del mes en curso.
    assert cap["rango"] == (date(2025, 1, 1), date(2026, 9, 30))
    filas = data["filas"]
    assert len(filas) == 12
    assert (filas[0]["anio"], filas[0]["mes_num"]) == (2025, 10)
    assert (filas[-1]["anio"], filas[-1]["mes_num"]) == (2026, 9)
    assert data["desde"] == "10/2025" and data["hasta"] == "09/2026"
    # Octubre: alias + devolución negativa.
    assert filas[0]["kg"] == 45 and filas[0]["importe"] == 180.0
    # Septiembre: dos facturas, una con la fecha como texto.
    assert filas[-1]["kg"] == 100 and filas[-1]["importe"] == 500.0
    # Los meses sin venta están, en cero.
    assert filas[1]["kg"] == 0 and filas[1]["importe"] == 0 and filas[1]["precio"] == 0
    assert data["meses_con_venta"] == 2
    assert data["n_documentos"] == 4
    assert data["fuente_caida"] is False
    # Totales, precio promedio y acumulado.
    assert data["total_kg"] == 145 and data["total_importe"] == 680.0
    assert data["precio_prom"] == pytest.approx(680.0 / 145)
    assert filas[-1]["acum"] == 680.0 and filas[0]["acum"] == 180.0
    assert filas[-1]["precio"] == pytest.approx(5.0)


def test_asinfo_caido_avisa_y_no_dice_cero(monkeypatch):
    _fakes(monkeypatch, [])
    with patch.object(queries, "today_ec", return_value=HOY):
        data = queries.ventas_cliente_por_mes("ABC")
    assert data["fuente_caida"] is True
    assert len(data["filas"]) == 12 and data["total_kg"] == 0


def test_no_lee_scintela_factura(monkeypatch):
    """La tabla propia tiene la historia rota: la pantalla NO la consulta."""
    import inspect

    fuente = inspect.getsource(queries.ventas_cliente_por_mes)
    cuerpo = fuente.split(queries.ventas_cliente_por_mes.__doc__, 1)[1]  # sin el docstring
    assert "scintela.factura" not in cuerpo
    assert "facturas_periodo" in fuente


def test_ventana_de_24_meses_y_tope(monkeypatch):
    cap = _fakes(monkeypatch, [])
    with patch.object(queries, "today_ec", return_value=HOY):
        assert len(queries.ventas_cliente_por_mes("ABC", meses=24)["filas"]) == 24
        # 24 meses arranca en 10/2024: el rango a Asinfo se estira hacia atrás.
        assert cap["rango"][0] == date(2024, 10, 1)
        assert len(queries.ventas_cliente_por_mes("ABC", meses=999)["filas"]) == 60
        assert len(queries.ventas_cliente_por_mes("ABC", meses=0)["filas"]) == 12


def test_cliente_inexistente_devuelve_vacio(monkeypatch):
    _fakes(monkeypatch, [], cliente=False)
    with patch.object(queries, "today_ec", return_value=HOY):
        assert queries.ventas_cliente_por_mes("ZZZ") == {}
    assert queries.ventas_cliente_por_mes("") == {}


# ── la pantalla ──────────────────────────────────────────────────────────────

def _login(app, fake_db, perms=("informes.ver",)):
    rid = fake_db.add_role("Tester", list(perms))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _data(meses=12):
    filas = [
        {"anio": 2026, "mes_num": m, "mes_nombre": MESES_ES[m - 1],
         "kg": 100.0 * m, "importe": 550.0 * m, "precio": 5.5, "acum": 0.0}
        for m in range(1, meses + 1)
    ]
    return {
        "cliente": {"codigo_cli": "ABC", "nombre": "ABC TEXTIL"},
        "meses": meses, "desde": "01/2026", "hasta": "12/2026",
        "filas": filas, "total_kg": 100.0, "total_importe": 550.0,
        "precio_prom": 5.5, "meses_con_venta": meses,
        "n_documentos": meses, "fuente_caida": False,
    }


def test_pantalla_renderiza_kilos_dolares_y_links(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_cliente_por_mes", return_value=_data()) as m:
        r = c.get("/informes/ventas/cliente/abc")
    assert r.status_code == 200
    assert m.call_args[0] == ("ABC", 12)
    html = r.get_data(as_text=True)
    assert "ABC TEXTIL" in html
    assert "Ventas por mes" in html
    assert "Kg" in html and "U$/kg" in html
    # Cada mes linkea al ranking de ese mes; hay CSV y ventana de 24 meses.
    assert "/informes/ventas?anio=2026&amp;mes=3" in html
    assert "meses=24" in html
    assert "export=csv" in html


def test_pantalla_404_si_el_cliente_no_existe(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_cliente_por_mes", return_value={}):
        assert c.get("/informes/ventas/cliente/ZZZ").status_code == 404


def test_pantalla_404_sin_permiso(app, fake_db):
    c = _login(app, fake_db, perms=("cheques.ver",))
    with patch.object(queries, "ventas_cliente_por_mes", return_value=_data()):
        assert c.get("/informes/ventas/cliente/ABC").status_code == 404


def test_csv_baja_la_grilla(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_cliente_por_mes", return_value=_data()):
        r = c.get("/informes/ventas/cliente/ABC?export=csv&meses=24")
    assert r.status_code == 200
    assert "ventas_ABC.csv" in r.headers.get("Content-Disposition", "")
    cuerpo = r.get_data(as_text=True)
    assert "Mes" in cuerpo and "01/2026" in cuerpo


def test_landing_redirige_al_codigo(app, fake_db):
    c = _login(app, fake_db)
    r = c.get("/informes/ventas/cliente?codigo=abc&meses=24")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/informes/ventas/cliente/ABC?meses=24")
    r = c.get("/informes/ventas/cliente")
    assert r.status_code == 302 and "estado-cuenta" in r.headers["Location"]


def test_los_links_de_entrada_existen():
    """El estado de cuenta y el ranking del mes llevan a la pantalla nueva."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "modules/informes/templates/informes"
    assert "informes.ventas_cliente" in (base / "estado_cuenta.html").read_text(encoding="utf-8")
    assert "informes.ventas_cliente" in (base / "ventas_mes.html").read_text(encoding="utf-8")
