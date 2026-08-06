"""Observación libre EDITABLE por el usuario sobre un cheque
(`scintela.cheque.nota_usuario`).

TMT 2026-08-06. Alex pidió una observación de texto libre en el cheque —
que se vea como `*` rojo en /cheques y se imprima en las dos hojas del
día (LISTADO DE CHEQUES INGRESADOS y RESUMEN DE COBRANZA DEL DÍA).

Va a COLUMNA PROPIA (`nota_usuario`), NO a la bitácora `observacion`
(append-only, con tags `[E]`/`[X]`/`[REBOTE]`/… del sistema — mezclarlas
disparaba el `*` para cualquier cheque con historia y ensuciaba las hojas).

Estos tests protegen los 3 lugares donde tiene que aparecer:
  · save por la pantalla de detalle (queries.editar + view POST),
  · asterisco visible sólo cuando hay contenido (lista.html),
  · texto renderizado en resumen_dia.html e ingresados_dia.html.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime

import bcrypt
import pytest

# ------------------------------------------------------------------ helpers


class _RecorderDB:
    """Stub que registra los executes — patrón de test_cheques_editar.py."""

    def __init__(self, *, cheque=None):
        self.cheque = cheque
        self.executes: list[tuple[str, tuple]] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.cheque where id_cheque" in s:
            return dict(self.cheque) if self.cheque else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()

    def apply_to(self, monkeypatch, db_mod):
        monkeypatch.setattr(db_mod, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db_mod, "fetch_all", self.fetch_all)
        monkeypatch.setattr(db_mod, "execute", self.execute)
        monkeypatch.setattr(db_mod, "execute_returning", self.execute_returning)
        monkeypatch.setattr(db_mod, "tx", self.tx)


@pytest.fixture(autouse=True)
def _no_periodo_guard(monkeypatch):
    import periodo_guard

    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda f: None)


@pytest.fixture(autouse=True)
def _no_bootstrap(monkeypatch):
    """El bootstrap del ALTER TABLE es fail-soft, pero acá lo neutralizamos
    para que los tests no dependan del orden — cada uno controla su fake."""
    from modules.cheques import nota_usuario as nu

    monkeypatch.setattr(nu, "_bootstrapped", True, raising=False)


# ----------------------------------------------- (a) SAVE desde vista detalle


def test_editar_guarda_nota_usuario(monkeypatch):
    """queries.editar(nota_usuario=...) escribe la columna nueva."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={
            "id_cheque": 42,
            "no_cheque": "12345",
            "stat": "Z",
            "fechad": date(2026, 5, 1),
        }
    )
    fake.apply_to(monkeypatch, db_mod)

    queries.editar(42, nota_usuario="cliente confirmó por whatsapp", usuario="alex")

    # UN solo UPDATE, con la columna y el texto correctos.
    ups = [(s, p) for s, p in fake.executes if "update scintela.cheque" in s.lower()]
    assert len(ups) == 1
    sql, params = ups[0]
    assert "nota_usuario=%s" in " ".join(sql.split()).lower()
    assert "cliente confirmó por whatsapp" in params


def test_editar_borra_nota_usuario_con_string_vacio(monkeypatch):
    """Mandar "" desde el form significa borrar (NULL en DB)."""
    import db as db_mod
    from modules.cheques import queries

    fake = _RecorderDB(
        cheque={"id_cheque": 7, "no_cheque": "9", "stat": "Z", "fechad": None}
    )
    fake.apply_to(monkeypatch, db_mod)

    queries.editar(7, nota_usuario="   ", usuario="alex")

    ups = [(s, p) for s, p in fake.executes if "update scintela.cheque" in s.lower()]
    assert len(ups) == 1
    sql, params = ups[0]
    assert "nota_usuario=%s" in " ".join(sql.split()).lower()
    # limpiar("   ") → None → guarda NULL.
    assert None in params


def test_view_actualizar_pasa_nota_usuario_a_queries(client, fake_db, monkeypatch):
    """POST /cheques/<id>/actualizar con nota_usuario → llega a queries.editar."""
    from modules.cheques import views as v

    capt: dict = {}

    def fake_por_id(_id):
        return {"id_cheque": 99, "no_cheque": "1", "stat": "Z", "importe": 100.0}

    def fake_editar(id_cheque, **kwargs):
        capt["id_cheque"] = id_cheque
        capt["kwargs"] = kwargs
        return {"id_cheque": id_cheque, "fechad_nueva": None, "fechad_shifted_lunes": False}

    monkeypatch.setattr(v.queries, "por_id", fake_por_id)
    monkeypatch.setattr(v.queries, "editar", fake_editar)
    monkeypatch.setattr(v.queries, "STATS_TERMINALES_EDIT", frozenset({"X", "T", "R", "3"}))

    rid = fake_db.add_role("Admin", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})

    r = client.post(
        "/cheques/99/actualizar",
        data={"nota_usuario": "postergado por vacaciones del cliente"},
    )
    assert r.status_code in (302, 303)
    assert capt["id_cheque"] == 99
    assert capt["kwargs"]["nota_usuario"] == "postergado por vacaciones del cliente"


# ----------------------------------------------- (b) ASTERISCO en /cheques


def _stub_listar(monkeypatch, filas):
    """Fuerza a /cheques a devolver `filas`, cortando en queries.buscar."""
    from modules.cheques import views as v

    # queries.buscar toma args posicionales + kwargs. Aceptamos ambos.
    monkeypatch.setattr(v.queries, "buscar", lambda *a, **kw: list(filas))
    # transiciones_map devuelve el dict de transiciones legales por stat.
    monkeypatch.setattr(v.queries, "transiciones_map", lambda: {})


def _login_admin(client, fake_db):
    rid = fake_db.add_role("Admin", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})


def _fila_base(**over):
    base = {
        "id_cheque": 1,
        "no_cheque": "111",
        "fecha": date(2026, 8, 6),
        "fechad": date(2026, 8, 6),
        "fechaing": None,
        "fechaout": None,
        "fecha_recibido": date(2026, 8, 6),
        "fecha_crea": datetime(2026, 8, 6, 10, 0),
        "dia_ingreso": date(2026, 8, 6),
        "fechad_original": None,
        "fecha_postergacion": None,
        "codigo_cli": "ABC",
        "cliente": "ABC SA",
        "vendedor": "",
        "importe": 100.0,
        "stat": "Z",
        "doc_banco": None,
        "nota_usuario": None,
        "no_banco": 12,
        "banco_nombre": "PICHINCHA",
        "banco": "PICHINCHA",
        "endoso_prov": None,
        "endoso_proveedor": None,
        "saldo_acumulado": 100.0,
    }
    base.update(over)
    return base


def test_asterisco_visible_solo_si_hay_nota(client, fake_db, monkeypatch):
    """/cheques: fila con nota_usuario → * rojo; sin nota → nada."""
    _login_admin(client, fake_db)
    filas = [
        _fila_base(id_cheque=1, no_cheque="AAA", nota_usuario="cliente confirmó"),
        _fila_base(id_cheque=2, no_cheque="BBB", nota_usuario=None),
    ]
    _stub_listar(monkeypatch, filas)

    r = client.get("/cheques?estado=todos")
    assert r.status_code == 200
    html = r.get_data(as_text=True)

    # el asterisco marcado con testid tiene que salir UNA VEZ (fila 1).
    assert html.count('data-testid="ch-nota-asterisco"') == 1
    # y el tooltip lleva el texto de la nota (parcial o entero).
    assert "cliente confirmó" in html
    # la fila 2 no dispara asterisco.
    assert 'AAA' in html and 'BBB' in html


# ---------------------------------------- (c) render en las 2 hojas impresas


def test_ingresados_dia_imprime_nota_usuario(monkeypatch):
    """La hoja LISTADO DE CHEQUES INGRESADOS muestra la observación."""
    from modules.cheques import queries

    fila = {
        "id_cheque": 1, "no_cheque": "1",
        "fechad": date(2026, 8, 6), "fecha": date(2026, 8, 6),
        "importe": 500.0, "stat": "Z", "no_banco": 12,
        "codigo_cli": "ABC", "banco_emisor": "PICHINCHA",
        "cliente": "ABC SA",
        "nota_usuario": "cliente pidió esperar 2 días",
    }

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque c" in s and "order by c.importe desc" in s:
            return [fila]
        return []

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)
    r = queries.cheques_ingresados_dia(date(2026, 8, 6))
    assert r["filas"][0]["nota_usuario"] == "cliente pidió esperar 2 días"

    # Render por Jinja (evitamos flask app entera acá — probamos el template).
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("modules/cheques/templates"))
    env.filters["fecha_es"] = lambda d: d.strftime("%d/%m/%Y") if d else ""
    env.filters["money_es"] = lambda x: f"{x:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")
    env.filters["cleanstr"] = lambda x: (x or "").replace("<", "&lt;").replace(">", "&gt;")
    env.filters["hora_ec"] = lambda x, f: ""
    env.globals["url_for"] = lambda *a, **kw: "/"
    env.globals["tiene_permiso"] = lambda *a, **kw: True
    env.globals["csrf_token"] = lambda: "tok"
    env.globals["request"] = type("R", (), {"args": {}, "full_path": "/"})()
    # base.html es complejo → probamos solo el fragmento del content
    with open("modules/cheques/templates/cheques/ingresados_dia.html") as _fh:
        tpl_src = _fh.read()
    # Cortamos {% extends %} y {% block %} para renderizar el cuerpo.
    body = tpl_src.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
    tpl = env.from_string(body)
    html = tpl.render(
        listado=r, fecha=date(2026, 8, 6), error=None,
    )
    assert "cliente pidió esperar 2 días" in html
    assert 'data-testid="ing-obs"' in html


def test_resumen_dia_imprime_nota_usuario(monkeypatch):
    """La hoja RESUMEN DE COBRANZA DEL DÍA muestra la observación en la
    celda de facturas."""
    from modules.cheques import queries

    fila = {
        "id_cheque": 1, "no_cheque": "999", "importe": 800.0,
        "fecha": date(2026, 8, 6), "fechad": date(2026, 8, 6),
        "no_banco": 12, "stat": "Z", "doc_banco": None,
        "concepto": None,
        "nota_usuario": "postergado — cliente en Guayaquil hasta el lunes",
        "fecha_crea": datetime(2026, 8, 6, 10, 0),
        "usuario_crea": "alex", "clave": "ALX",
        "fecha_recibido": date(2026, 8, 6), "fechaing": date(2026, 8, 6),
        "banco_emisor": "PICHINCHA", "codigo_cli": "ABC",
        "cliente": "ABC SA",
    }

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque c" in s and "left join scintela.cliente cl" in s:
            return [fila]
        if "from scintela.chequesxfact cxf" in s:
            return []
        return []

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)
    r = queries.resumen_cobranza_dia(date(2026, 8, 6))
    assert r["ingresos"][0]["nota_usuario"].startswith("postergado")

    # Render de la sección (mismo truco que en el test de ingresados)
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("modules/cheques/templates"))
    env.filters["fecha_es"] = lambda d: d.strftime("%d/%m/%Y") if d else ""
    env.filters["money_es"] = lambda x: f"{x:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".") if x is not None else ""
    env.filters["cleanstr"] = lambda x: (x or "").replace("<", "&lt;").replace(">", "&gt;")
    env.filters["hora_ec"] = lambda x, f: ""
    env.filters["truncate"] = lambda x, n, *a, **kw: (x or "")[:n]
    env.globals["url_for"] = lambda *a, **kw: "/"
    env.globals["tiene_permiso"] = lambda *a, **kw: True
    env.globals["csrf_token"] = lambda: "tok"
    with open("modules/cheques/templates/cheques/resumen_dia.html") as _fh:
        tpl_src = _fh.read()
    body = tpl_src.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
    tpl = env.from_string(body)
    html = tpl.render(resumen=r, fecha=date(2026, 8, 6), error=None)
    assert "postergado — cliente en Guayaquil hasta el lunes" in html
    assert 'data-testid="res-obs"' in html
