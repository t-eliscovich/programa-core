"""Tests para el módulo `modules.recientes`.

Helpers cubiertos:
    - registrar(tipo, id_ref, etiqueta)  — UPSERT + trim
    - listar_recientes(tipo, limite)     — lectura filtrada
    - best-effort: si la DB falla, no levantan

El test no necesita Postgres — monkeypatcheamos `db.execute` / `db.fetch_all`
en tiempo de test.
"""
from __future__ import annotations

import pytest
from flask import g

import db
from modules.recientes import queries as rec

# ---------------------------------------------------------------------------
# Recorder de DB — captura ejecuciones para poder asertar SQL + params
# ---------------------------------------------------------------------------

class _Rec:
    def __init__(self):
        self.executes: list[tuple[str, tuple]] = []
        self._fetch_all_return: list[dict] = []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((" ".join(sql.split()), params))
        return 1

    def fetch_all(self, sql, params=None, conn=None):
        # Guardamos la query para assertions y devolvemos lo configurado.
        self.executes.append(("QUERY " + " ".join(sql.split()), params))
        return list(self._fetch_all_return)


@pytest.fixture
def rec_db(monkeypatch):
    r = _Rec()
    monkeypatch.setattr(db, "execute", r.execute)
    monkeypatch.setattr(db, "fetch_all", r.fetch_all)
    return r


# ---------------------------------------------------------------------------
# registrar()
# ---------------------------------------------------------------------------

def test_registrar_upsert_con_usuario(app, rec_db):
    with app.test_request_context("/"):
        g.user = {"id_usuario": 42, "username": "x", "nombre_rol": "Dueño"}
        rec.registrar("cliente", "JTX", "JTX — Jiménez")
    # Al menos un INSERT ... ON CONFLICT DO UPDATE se emitió.
    upsert = [e for e in rec_db.executes if "ON CONFLICT" in e[0]]
    assert len(upsert) >= 1
    sql, params = upsert[0]
    assert "seguridad.usuario_recientes" in sql
    assert params[0] == 42  # id_usuario
    assert params[1] == "cliente"
    assert params[2] == "JTX"
    assert params[3] == "JTX — Jiménez"


def test_registrar_sin_usuario_no_hace_nada(app, rec_db):
    with app.test_request_context("/"):
        g.user = None
        rec.registrar("cliente", "JTX", "J")
    assert rec_db.executes == []


def test_registrar_tipo_invalido_es_noop(app, rec_db):
    with app.test_request_context("/"):
        g.user = {"id_usuario": 1}
        rec.registrar("tipo_raro", "ABC", "x")
    assert rec_db.executes == []


def test_registrar_id_ref_vacio_es_noop(app, rec_db):
    with app.test_request_context("/"):
        g.user = {"id_usuario": 1}
        rec.registrar("cliente", "", "x")
        rec.registrar("cliente", None, "x")
    assert rec_db.executes == []


def test_registrar_es_best_effort(app, monkeypatch):
    """Si db.execute explota, registrar() no debe propagar."""
    def boom(*a, **kw):
        raise RuntimeError("DB offline")
    monkeypatch.setattr(db, "execute", boom)
    with app.test_request_context("/"):
        g.user = {"id_usuario": 1}
        # No debería levantar.
        rec.registrar("cliente", "JTX", "J")


def test_registrar_emite_trim_despues_del_insert(app, rec_db):
    with app.test_request_context("/"):
        g.user = {"id_usuario": 3}
        rec.registrar("factura", 100, "Factura 100")
    # Debería haber dos executes: upsert + trim delete.
    assert len(rec_db.executes) == 2
    assert "ON CONFLICT" in rec_db.executes[0][0]
    assert "DELETE" in rec_db.executes[1][0]


# ---------------------------------------------------------------------------
# listar_recientes()
# ---------------------------------------------------------------------------

def test_listar_recientes_respeta_limite(app, rec_db):
    rec_db._fetch_all_return = [
        {"tipo": "cliente", "id_ref": "JTX", "etiqueta": "J", "tocado_en": None}
    ]
    with app.test_request_context("/"):
        g.user = {"id_usuario": 1}
        res = rec.listar_recientes(limite=3)
    # Una sola query
    queries = [e for e in rec_db.executes if e[0].startswith("QUERY")]
    assert len(queries) == 1
    sql, params = queries[0]
    # LIMIT va como último param
    assert params[-1] == 3
    assert res == rec_db._fetch_all_return


def test_listar_recientes_filtra_por_tipo(app, rec_db):
    rec_db._fetch_all_return = []
    with app.test_request_context("/"):
        g.user = {"id_usuario": 1}
        rec.listar_recientes(tipo="factura", limite=5)
    queries = [e for e in rec_db.executes if e[0].startswith("QUERY")]
    assert len(queries) == 1
    sql, params = queries[0]
    assert "tipo = %s" in sql
    assert "factura" in params


def test_listar_sin_usuario_devuelve_lista_vacia(app, rec_db):
    with app.test_request_context("/"):
        g.user = None
        res = rec.listar_recientes()
    assert res == []


def test_listar_best_effort_si_db_falla(app, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("x")
    monkeypatch.setattr(db, "fetch_all", boom)
    with app.test_request_context("/"):
        g.user = {"id_usuario": 1}
        assert rec.listar_recientes() == []


def test_listar_clampa_limite_superior(app, rec_db):
    rec_db._fetch_all_return = []
    with app.test_request_context("/"):
        g.user = {"id_usuario": 1}
        rec.listar_recientes(limite=999)
    sql, params = rec_db.executes[0]
    # limite máximo 50
    assert params[-1] == 50
