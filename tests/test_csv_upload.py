"""Tests del helper csv_upload + rutas cargar-csv en facturas/cheques/compras."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from csv_upload import (
    ResultadoUpload,
    parse_bool,
    parse_fecha,
    parse_int,
    parse_monto,
    plantilla_csv,
    procesar_csv,
)

# ---------------------------------------------------------------------------
# Parsers individuales
# ---------------------------------------------------------------------------

def test_parse_fecha_dd_mm_yyyy():
    assert parse_fecha("17/04/2026") == date(2026, 4, 17)


def test_parse_fecha_iso():
    assert parse_fecha("2026-04-17") == date(2026, 4, 17)


def test_parse_fecha_vacio_es_none():
    assert parse_fecha("") is None
    assert parse_fecha(None) is None
    assert parse_fecha("   ") is None


def test_parse_fecha_invalida_es_none():
    assert parse_fecha("basura") is None
    assert parse_fecha("31/02/2026") is None


def test_parse_monto_formatos_varios():
    assert parse_monto("1234.56") == Decimal("1234.56")
    assert parse_monto("1.234,56") == Decimal("1234.56")
    assert parse_monto("1234,56") == Decimal("1234.56")
    assert parse_monto("") is None
    assert parse_monto(None) is None


def test_parse_monto_basura_levanta():
    with pytest.raises(ValueError):
        parse_monto("abc")


def test_parse_int_happy():
    assert parse_int("123") == 123
    assert parse_int("  42  ") == 42


def test_parse_int_vacio_es_none():
    assert parse_int("") is None
    assert parse_int(None) is None


def test_parse_int_invalido_levanta():
    with pytest.raises(ValueError):
        parse_int("doce")


def test_parse_bool():
    for t in ("1", "true", "TRUE", "sí", "si", "S", "Y"):
        assert parse_bool(t) is True
    for f in ("0", "false", "No", "N", "", None):
        assert parse_bool(f) is False


# ---------------------------------------------------------------------------
# plantilla_csv
# ---------------------------------------------------------------------------

def test_plantilla_csv_tiene_headers():
    cols = [("fecha", "Fecha", True), ("importe", "Importe", True)]
    csv = plantilla_csv(cols)
    assert "Fecha" in csv
    assert "Importe" in csv


def test_plantilla_csv_formato_separador():
    cols = [("a", "A", True), ("b", "B", True)]
    csv = plantilla_csv(cols)
    lines = csv.strip().splitlines()
    # Primera línea es header, segunda una fila de ejemplo vacía
    assert lines[0].count(";") == 1  # separador ; entre dos cols
    assert "A" in lines[0] and "B" in lines[0]


def test_resultado_upload_total_suma_ok_y_error():
    res = ResultadoUpload(ok=2, error=3)
    assert res.total == 5


# ---------------------------------------------------------------------------
# procesar_csv — happy path + errores
# ---------------------------------------------------------------------------

_COLS = [
    ("fecha",      "Fecha",       True),
    ("codigo_cli", "Código",      True),
    ("importe",    "Importe",     True),
    ("numf",       "N°",          False),
]


def test_procesar_csv_happy():
    created = []
    def _crear(usuario, **kw):
        created.append(kw)
        return {"id": len(created)}

    csv = (b"Fecha;Codigo;Importe;N\xc2\xb0\n"
           b"17/04/2026;JTX;1.234,56;101\n"
           b"18/04/2026;TEX;500.00;\n")
    res = procesar_csv(csv, _COLS, _crear)
    assert res.ok == 2
    assert res.error == 0
    assert created[0]["codigo_cli"] == "JTX"
    assert created[0]["importe"] == Decimal("1234.56")
    assert created[0]["numf"] == 101
    assert created[1]["numf"] is None


def test_procesar_csv_requeridos_faltan_falla_por_fila():
    def _crear(usuario, **kw):
        return {"id": 1}
    csv = b"Fecha;Codigo;Importe;N\n17/04/2026;JTX;;101\n18/04/2026;TEX;500;\n"
    res = procesar_csv(csv, _COLS, _crear)
    assert res.ok == 1
    assert res.error == 1
    assert "Importe" in res.detalles[0]["mensaje"]


def test_procesar_csv_crear_levanta_se_cuenta_como_error():
    def _crear(usuario, **kw):
        if kw["codigo_cli"] == "BAD":
            raise ValueError("cliente no existe")
        return {"id": 1}
    csv = b"Fecha;Codigo;Importe;N\n17/04/2026;BAD;100;\n17/04/2026;OK;100;\n"
    res = procesar_csv(csv, _COLS, _crear)
    assert res.ok == 1
    assert res.error == 1


def test_procesar_csv_vacio():
    res = procesar_csv(b"", _COLS, lambda **kw: None)
    assert res.ok == 0
    assert res.error == 0


def test_procesar_csv_con_bom():
    """UTF-8 BOM no rompe."""
    def _crear(usuario, **kw):
        return None
    raw = b"\xef\xbb\xbfFecha;Codigo;Importe;N\n17/04/2026;JTX;100;\n"
    res = procesar_csv(raw, _COLS, _crear)
    assert res.ok == 1


def test_procesar_csv_fallback_cp1252():
    created = []

    def _crear(usuario, **kw):
        created.append(kw)

    raw = b"Fecha;C\xf3digo;Importe;N\n17/04/2026;JTX;100;\n"
    res = procesar_csv(raw, _COLS, _crear)
    assert res.ok == 1
    assert created[0]["codigo_cli"] == "JTX"


def test_procesar_csv_coma_separador():
    def _crear(usuario, **kw):
        return None
    raw = b"Fecha,Codigo,Importe,N\n17/04/2026,JTX,100,\n"
    res = procesar_csv(raw, _COLS, _crear)
    assert res.ok == 1


def test_procesar_csv_usa_converter_custom():
    created = []

    def _crear(usuario, **kw):
        created.append(kw)

    raw = b"Fecha;Codigo;Importe;N\n17/04/2026;jtx;100;\n"
    res = procesar_csv(raw, _COLS, _crear, converters={"codigo_cli": str.upper})
    assert res.ok == 1
    assert created[0]["codigo_cli"] == "JTX"


def test_procesar_csv_campo_bool_por_nombre():
    created = []

    def _crear(usuario, **kw):
        created.append(kw)

    cols = [("pagada", "Pagada", True)]
    res = procesar_csv(b"Pagada\nsi\nno\n", cols, _crear)
    assert res.ok == 2
    assert [row["pagada"] for row in created] == [True, False]


def test_procesar_csv_monto_invalido_linea_falla_resto_sigue():
    def _crear(usuario, **kw):
        return None
    raw = (b"Fecha;Codigo;Importe;N\n"
           b"17/04/2026;JTX;basura;\n"
           b"18/04/2026;OK;100;\n")
    res = procesar_csv(raw, _COLS, _crear)
    assert res.ok == 1
    assert res.error == 1
    assert "monto inválido" in res.detalles[0]["mensaje"].lower()


def test_procesar_csv_crear_levanta_error_generico_se_cuenta_como_error():
    def _crear(usuario, **kw):
        raise RuntimeError("db offline")

    raw = b"Fecha;Codigo;Importe;N\n17/04/2026;BAD;100;\n"
    res = procesar_csv(raw, _COLS, _crear)
    assert res.ok == 0
    assert res.error == 1
    assert "RuntimeError: db offline" in res.detalles[0]["mensaje"]


# ---------------------------------------------------------------------------
# Rutas HTTP — plantilla + página de carga
# ---------------------------------------------------------------------------

def _login_as(app, fake_db, permisos):
    rid = fake_db.add_role("Tester", permisos)
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_facturas_cargar_csv_get_renderiza(app, fake_db):
    c = _login_as(app, fake_db, ["facturas.crear"])
    r = c.get("/facturas/cargar-csv")
    assert r.status_code == 200
    assert b"Cargar facturas" in r.data or b"facturas" in r.data.lower()


def test_facturas_plantilla_csv_descarga(app, fake_db):
    c = _login_as(app, fake_db, ["facturas.crear"])
    r = c.get("/facturas/cargar-csv?plantilla=1")
    assert r.status_code == 200
    assert "text/csv" in r.headers["Content-Type"]
    assert "plantilla_facturas.csv" in r.headers["Content-Disposition"]
    assert b"Fecha" in r.data
    assert b"Importe" in r.data


def test_cheques_cargar_csv_get_renderiza(app, fake_db):
    c = _login_as(app, fake_db, ["cheques.crear"])
    r = c.get("/cheques/cargar-csv")
    assert r.status_code == 200


def test_cheques_plantilla_csv_incluye_no_cheque(app, fake_db):
    c = _login_as(app, fake_db, ["cheques.crear"])
    r = c.get("/cheques/cargar-csv?plantilla=1")
    assert r.status_code == 200
    assert b"N" in r.data  # N° cheque
    assert b"cheques" in r.headers["Content-Disposition"].encode()


def test_compras_cargar_csv_get_renderiza(app, fake_db):
    c = _login_as(app, fake_db, ["compras.crear"])
    r = c.get("/compras/cargar-csv")
    assert r.status_code == 200


def test_compras_plantilla_csv_incluye_codigo_prov(app, fake_db):
    c = _login_as(app, fake_db, ["compras.crear"])
    r = c.get("/compras/cargar-csv?plantilla=1")
    assert r.status_code == 200
    assert b"proveedor" in r.data.lower()


def test_cargar_csv_requiere_permiso(app, fake_db):
    """Sin permiso facturas.crear, la ruta devuelve 403."""
    c = _login_as(app, fake_db, ["facturas.ver"])  # read-only
    r = c.get("/facturas/cargar-csv")
    assert r.status_code == 403


def test_cargar_csv_sin_archivo_redirige_con_flash(app, fake_db):
    c = _login_as(app, fake_db, ["facturas.crear"])
    r = c.post("/facturas/cargar-csv", data={})
    # Sin archivo → flash + redirect a GET
    assert r.status_code in (302, 303)
