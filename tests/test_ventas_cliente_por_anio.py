"""ventas_cliente_por_anio — resumen por AÑO CALENDARIO (kilos y dólares).

TMT 2026-09-14 — pedido de Tamara sobre `/informes/ventas/cliente/<cod>`:
*"cuando pongo ver 24 meses tarda una barbaridad. y quiero los kpis mas
chiquitos y hacer de los utimos 3 anos. 24, 25 y 26 total, promedio por mes,
para kg y $. algo simple y facil de digerir. abajo que se pueda ir viendo
los meses"*.

Verificado en vivo contra Asinfo (con Tamara autenticada en Metabase) que la
data histórica 2022-2026 existe y es sustancial — el rediseño no necesita
ningún caveat sobre "2024 no está".

Lo que estos tests protegen:

· Los últimos `n_anios` años CALENDARIO completos, terminando en el año en
  curso — el año en curso es PARCIAL y su promedio/mes divide por los meses
  ya transcurridos, no por 12.
· Misma fuente que `ventas_cliente_por_mes` (Asinfo, nunca `scintela.factura`)
  y mismo piso ancho de caché compartido con el warmup.
· `/informes/ventas/cliente/<cod>` la usa como vista POR DEFECTO; el CSV
  sigue bajando el corrido mes a mes (`ventas_cliente_por_mes`, con
  acumulado) en una ventana de 36 meses por defecto.
· Un código que no existe da 404, no una pantalla vacía.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from modules.informes import queries

HOY = date(2026, 9, 14)  # setiembre: el año en curso lleva 9 meses


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


def test_tres_anios_calendario_con_actual_parcial(monkeypatch):
    cap = _fakes(
        monkeypatch,
        [
            _doc("ABC", date(2024, 3, 5), 100, 500.0),
            _doc("ABC", date(2024, 12, 1), 50, 250.0),
            _doc("AB2", date(2025, 6, 10), 80, 400.0),       # alias del mismo cliente
            _doc("ABC", date(2025, 6, 15), -10, -50.0, "DEVOLUCION"),  # resta
            _doc("ABC", date(2026, 1, 10), 40, 200.0),
            _doc("ABC", date(2026, 9, 3), 60, 300.0),        # mes en curso
            _doc("XYZ", date(2026, 9, 3), 999, 9999.0),      # otro cliente: fuera
            _doc("ABC", date(2023, 12, 31), 999, 9999.0),    # antes de la ventana: fuera
        ],
    )
    with patch.object(queries, "today_ec", return_value=HOY):
        data = queries.ventas_cliente_por_anio("abc", 3)

    assert data["cliente"] == {"codigo_cli": "ABC", "nombre": "ABC TEXTIL"}
    assert cap["cliente_params"] == ("ABC",)
    # Piso ancho compartido con el warmup: 2024-01-01 → fin de septiembre 2026.
    assert cap["rango"] == (date(2024, 1, 1), date(2026, 9, 30))

    anios = {a["anio"]: a for a in data["anios"]}
    assert list(anios) == [2024, 2025, 2026]  # más viejo primero

    a24 = anios[2024]
    assert a24["meses_transcurridos"] == 12 and a24["es_parcial"] is False
    assert a24["total_kg"] == 150 and a24["total_importe"] == 750.0
    assert a24["promedio_kg_mes"] == pytest.approx(150 / 12)
    assert a24["promedio_importe_mes"] == pytest.approx(750.0 / 12)
    assert a24["meses_con_venta"] == 2
    assert len(a24["filas"]) == 12

    a25 = anios[2025]
    # Alias + devolución negativa en el mismo mes.
    assert a25["total_kg"] == 70 and a25["total_importe"] == 350.0
    assert len(a25["filas"]) == 12

    a26 = anios[2026]
    assert a26["meses_transcurridos"] == 9 and a26["es_parcial"] is True
    assert a26["total_kg"] == 100 and a26["total_importe"] == 500.0
    # Promedio del año en curso: divide por los meses YA transcurridos (9), no 12.
    assert a26["promedio_kg_mes"] == pytest.approx(100 / 9)
    assert a26["promedio_importe_mes"] == pytest.approx(500.0 / 9)
    assert len(a26["filas"]) == 9  # no incluye octubre-diciembre (todavía no pasaron)

    assert data["total_kg"] == 150 + 70 + 100
    assert data["total_importe"] == 750.0 + 350.0 + 500.0
    assert data["n_documentos"] == 6  # sin contar XYZ ni el doc de 2023
    assert data["fuente_caida"] is False


def test_asinfo_caido_avisa_y_anios_en_cero(monkeypatch):
    _fakes(monkeypatch, [])
    with patch.object(queries, "today_ec", return_value=HOY):
        data = queries.ventas_cliente_por_anio("ABC", 3)
    assert data["fuente_caida"] is True
    assert [a["anio"] for a in data["anios"]] == [2024, 2025, 2026]
    assert data["total_kg"] == 0 and data["total_importe"] == 0


def test_no_lee_scintela_factura(monkeypatch):
    """Misma regla que ventas_cliente_por_mes: la tabla propia no se toca."""
    import inspect

    fuente = inspect.getsource(queries.ventas_cliente_por_anio)
    cuerpo = fuente.split(queries.ventas_cliente_por_anio.__doc__, 1)[1]
    assert "scintela.factura" not in cuerpo
    assert "facturas_periodo" in fuente


def test_cliente_inexistente_devuelve_vacio(monkeypatch):
    _fakes(monkeypatch, [], cliente=False)
    with patch.object(queries, "today_ec", return_value=HOY):
        assert queries.ventas_cliente_por_anio("ZZZ") == {}
    assert queries.ventas_cliente_por_anio("") == {}


def test_n_anios_tope_y_default(monkeypatch):
    _fakes(monkeypatch, [])
    with patch.object(queries, "today_ec", return_value=HOY):
        assert len(queries.ventas_cliente_por_anio("ABC", n_anios=1)["anios"]) == 1
        assert len(queries.ventas_cliente_por_anio("ABC", n_anios=999)["anios"]) == 10
        assert len(queries.ventas_cliente_por_anio("ABC", n_anios=0)["anios"]) == 3
        assert len(queries.ventas_cliente_por_anio("ABC")["anios"]) == 3


# ── la pantalla ──────────────────────────────────────────────────────────────

def _login(app, fake_db, perms=("informes.ver",)):
    rid = fake_db.add_role("Tester", list(perms))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _data_anios():
    def _anio(anio, meses_transcurridos, es_parcial, kg0=100.0, imp0=550.0):
        filas = [
            {"mes_num": m, "mes_nombre": f"Mes{m}", "kg": kg0 * m,
             "importe": imp0 * m, "precio": imp0 / kg0}
            for m in range(1, meses_transcurridos + 1)
        ]
        total_kg = sum(f["kg"] for f in filas)
        total_importe = sum(f["importe"] for f in filas)
        return {
            "anio": anio, "meses_transcurridos": meses_transcurridos,
            "es_parcial": es_parcial, "total_kg": total_kg,
            "promedio_kg_mes": total_kg / meses_transcurridos,
            "total_importe": total_importe,
            "promedio_importe_mes": total_importe / meses_transcurridos,
            "precio_prom": imp0 / kg0, "meses_con_venta": meses_transcurridos,
            "filas": filas,
        }

    anios = [_anio(2024, 12, False), _anio(2025, 12, False), _anio(2026, 9, True)]
    return {
        "cliente": {"codigo_cli": "ABC", "nombre": "ABC TEXTIL"},
        "anios": anios,
        "total_kg": sum(a["total_kg"] for a in anios),
        "total_importe": sum(a["total_importe"] for a in anios),
        "n_documentos": 33,
        "fuente_caida": False,
    }


def _data_mes_a_mes(meses=36):
    filas = [
        {"anio": 2026, "mes_num": (m % 12) + 1, "kg": 100.0 * m,
         "importe": 550.0 * m, "precio": 5.5, "acum": 0.0}
        for m in range(1, meses + 1)
    ]
    return {
        "cliente": {"codigo_cli": "ABC", "nombre": "ABC TEXTIL"},
        "meses": meses, "desde": "01/2024", "hasta": "09/2026",
        "filas": filas, "total_kg": 100.0, "total_importe": 550.0,
        "precio_prom": 5.5, "meses_con_venta": meses,
        "n_documentos": meses, "fuente_caida": False,
    }


def test_pantalla_renderiza_anios_y_links(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_cliente_por_anio", return_value=_data_anios()) as m:
        r = c.get("/informes/ventas/cliente/abc")
    assert r.status_code == 200
    assert m.call_args[0] == ("ABC", 3)
    html = r.get_data(as_text=True)
    assert "ABC TEXTIL" in html
    assert "Ventas por año" in html
    assert "2024" in html and "2025" in html and "2026" in html
    assert "/mes" in html  # total y promedio por mes, en la misma línea
    # El año más reciente aparece PRIMERO (Tamara, 14/09): tanto en las
    # tarjetas como en el detalle plegado.
    assert html.index(">2026<") < html.index(">2025<") < html.index(">2024<")
    # Cada mes del detalle plegado linkea al ranking de ese mes; hay CSV.
    assert "/informes/ventas?anio=2026&amp;mes=3" in html
    assert "export=csv" in html


def test_pantalla_404_si_el_cliente_no_existe(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_cliente_por_anio", return_value={}):
        assert c.get("/informes/ventas/cliente/ZZZ").status_code == 404


def test_pantalla_404_sin_permiso(app, fake_db):
    c = _login(app, fake_db, perms=("cheques.ver",))
    with patch.object(queries, "ventas_cliente_por_anio", return_value=_data_anios()):
        assert c.get("/informes/ventas/cliente/ABC").status_code == 404


def test_csv_baja_el_corrido_mes_a_mes(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_cliente_por_mes", return_value=_data_mes_a_mes()) as m:
        r = c.get("/informes/ventas/cliente/ABC?export=csv")
    assert r.status_code == 200
    # Por defecto el CSV cubre 36 meses (los mismos 3 años que ve la pantalla).
    assert m.call_args[0] == ("ABC", 36)
    assert "ventas_ABC.csv" in r.headers.get("Content-Disposition", "")
    cuerpo = r.get_data(as_text=True)
    assert "Mes" in cuerpo


def test_csv_respeta_meses_explicito(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(queries, "ventas_cliente_por_mes", return_value=_data_mes_a_mes(24)) as m:
        r = c.get("/informes/ventas/cliente/ABC?export=csv&meses=24")
    assert r.status_code == 200
    assert m.call_args[0] == ("ABC", 24)


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
