"""/admin/copia-de-datos — la base entera en un zip, para guardar AFUERA de AWS.

TMT 2026-09-03. Lo que tiene que quedar garantizado:
  - la copia NO escribe (transacción READ ONLY + rollback, como la consola SQL);
  - sin sesión y sin clave no se baja nada; con la clave, se baja sin sesión;
  - la clave nace sola, se persiste y no cambia entre lecturas;
  - el zip trae un CSV por tabla, migraciones.txt y LEEME.txt.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.admin_dbase import copia_datos_view as cd  # noqa: E402


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        if "pg_tables" in sql:
            self._rows = [("scintela", "cheque"), ("seguridad", "usuario")]
        elif "migraciones_aplicadas" in sql:
            self._rows = [("0001",), ("0241",)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def copy_expert(self, sql, f):
        self.conn.sql.append(sql)
        f.write(b"id,nombre\n1,uno\n")


class _Conn:
    def __init__(self):
        self.sql = []
        self.rollbacks = 0
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


def _conn_falsa(monkeypatch):
    conn = _Conn()

    @contextlib.contextmanager
    def _get_conn():
        yield conn

    monkeypatch.setattr(cd.db, "get_conn", _get_conn)
    return conn


def _login(app, fake_db, perms):
    rid = fake_db.add_role("Tester", perms)
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


# --- solo lectura ------------------------------------------------------------

def test_la_copia_es_READ_ONLY_y_termina_en_rollback(monkeypatch):
    src = inspect.getsource(cd.armar_copia)
    assert "SET TRANSACTION READ ONLY" in src
    assert "conn.rollback()" in src and "conn.commit()" not in src
    conn = _conn_falsa(monkeypatch)
    cd.armar_copia(io.BytesIO())
    assert conn.sql[0] == "SET TRANSACTION READ ONLY"
    assert conn.rollbacks == 1 and conn.commits == 0


def test_el_zip_trae_csv_por_tabla_migraciones_y_leeme(monkeypatch):
    _conn_falsa(monkeypatch)
    buf = io.BytesIO()
    info = cd.armar_copia(buf)
    assert info == {"tablas": 2, "migracion": "0241"}
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        nombres = sorted(z.namelist())
        assert nombres == ["LEEME.txt", "migraciones.txt",
                           "scintela.cheque.csv", "seguridad.usuario.csv"]
        assert z.read("scintela.cheque.csv") == b"id,nombre\n1,uno\n"
        assert z.read("migraciones.txt") == b"0001\n0241\n"
        assert b"\\copy" in z.read("LEEME.txt")


# --- la clave ---------------------------------------------------------------

def test_la_clave_nace_sola_se_persiste_y_no_cambia(tmp_path, monkeypatch):
    ruta = tmp_path / ".clave_copia"
    monkeypatch.setenv("CLAVE_COPIA_FILE", str(ruta))
    a = cd.clave_de_descarga()
    assert len(a) >= 32 and ruta.read_text().strip() == a
    assert cd.clave_de_descarga() == a, "la clave rotó entre dos lecturas"


def test_una_clave_corta_en_el_archivo_no_vale(tmp_path, monkeypatch):
    ruta = tmp_path / ".clave_copia"
    ruta.write_text("corta")
    monkeypatch.setenv("CLAVE_COPIA_FILE", str(ruta))
    assert len(cd.clave_de_descarga()) >= 32


def test_si_no_se_puede_guardar_igual_hay_clave(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAVE_COPIA_FILE", str(tmp_path / "no-existe" / "x"))
    assert len(cd.clave_de_descarga()) >= 32


# --- las rutas --------------------------------------------------------------

def test_descargar_sin_clave_o_con_clave_mala_403(app, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAVE_COPIA_FILE", str(tmp_path / ".clave_copia"))
    c = app.test_client()
    assert c.get("/admin/copia-de-datos/descargar").status_code == 403
    assert c.get("/admin/copia-de-datos/descargar?clave=x").status_code == 403


def test_descargar_con_la_clave_baja_el_zip_sin_sesion(app, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAVE_COPIA_FILE", str(tmp_path / ".clave_copia"))
    _conn_falsa(monkeypatch)
    clave = cd.clave_de_descarga()
    c = app.test_client()
    r = c.get("/admin/copia-de-datos/descargar", headers={"X-Clave-Copia": clave})
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    assert "programa-core-datos-" in r.headers["Content-Disposition"]
    assert zipfile.ZipFile(io.BytesIO(r.data)).namelist()


def test_la_pantalla_pide_admin_y_muestra_la_clave(app, fake_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAVE_COPIA_FILE", str(tmp_path / ".clave_copia"))
    _conn_falsa(monkeypatch)
    assert app.test_client().get("/admin/copia-de-datos/").status_code in (302, 404)
    c = _login(app, fake_db, ["informes.ver"])
    assert c.get("/admin/copia-de-datos/").status_code == 404
    c = _login(app, fake_db, ["usuarios.admin"])
    r = c.get("/admin/copia-de-datos/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Descargar copia" in html and cd.clave_de_descarga() in html
    assert "2 tablas" in html


def test_el_boton_baja_el_zip_con_sesion_de_admin(app, fake_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAVE_COPIA_FILE", str(tmp_path / ".clave_copia"))
    _conn_falsa(monkeypatch)
    c = _login(app, fake_db, ["usuarios.admin"])
    r = c.post("/admin/copia-de-datos/bajar")
    assert r.status_code == 200 and r.mimetype == "application/zip"
