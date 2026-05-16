"""Tests for error_messages.humanize and the `humanizar` Jinja filter."""
from __future__ import annotations

import pytest

from error_messages import humanize

# ---------------------------------------------------------------------------
# humanize() — excepciones crudas
# ---------------------------------------------------------------------------

def test_humanize_valueerror_monto_con_literal():
    exc = ValueError("monto inválido: 'abc'")
    msg = humanize(exc)
    assert "'abc'" in msg
    assert "1234.56" in msg  # muestra el formato esperado
    assert "1.234,56" in msg


def test_humanize_valueerror_monto_sin_comilla():
    exc = ValueError("monto invalido: foo")
    msg = humanize(exc)
    assert "'foo'" in msg or "foo" in msg
    assert "1234.56" in msg


def test_humanize_valueerror_fecha():
    exc = ValueError("fecha inválida")
    msg = humanize(exc)
    assert "DD/MM/AAAA" in msg
    assert "17/04/2026" in msg


def test_humanize_valueerror_fecha_sin_tilde():
    exc = ValueError("fecha invalida para el periodo")
    msg = humanize(exc)
    assert "DD/MM/AAAA" in msg


def test_humanize_valueerror_generico_respeta_mensaje():
    exc = ValueError("Motivo requerido")
    assert humanize(exc) == "Motivo requerido"


def test_humanize_valueerror_vacio():
    exc = ValueError("")
    assert humanize(exc) == "Dato inválido."


def test_humanize_unique_violation_por_pgcode():
    class Fake(Exception):
        pgcode = "23505"

    msg = humanize(Fake("duplicate key value"))
    assert "Ya existe" in msg


def test_humanize_unique_violation_por_classname():
    class UniqueViolation(Exception):
        pass

    msg = humanize(UniqueViolation("alguna cosa"))
    assert "Ya existe" in msg


def test_humanize_fk_violation_por_pgcode():
    class Fake(Exception):
        pgcode = "23503"

    msg = humanize(Fake("fk"))
    assert "Referencia inválida" in msg


def test_humanize_fk_violation_por_classname():
    class ForeignKeyViolation(Exception):
        pass

    assert "Referencia inválida" in humanize(ForeignKeyViolation("fk"))


def test_humanize_check_violation():
    class Fake(Exception):
        pgcode = "23514"

    assert "rango" in humanize(Fake("check")) or "permitido" in humanize(Fake("check"))


def test_humanize_not_null_violation():
    class Fake(Exception):
        pgcode = "23502"

    assert "obligatorio" in humanize(Fake("null"))


def test_humanize_permission_error():
    exc = PermissionError("denied")
    assert "permiso" in humanize(exc).lower()


def test_humanize_exception_generica_devuelve_neutro():
    exc = RuntimeError("internal boom with path /usr/local/lib")
    msg = humanize(exc)
    assert "/usr/local" not in msg  # no leakea detalle técnico
    assert "soporte" in msg.lower()


def test_humanize_none_devuelve_vacio():
    assert humanize(None) == ""


# ---------------------------------------------------------------------------
# Jinja filter `humanizar`
# ---------------------------------------------------------------------------

def test_filter_humanizar_con_exc(app):
    """El filter registrado debe convertir una excepción en texto en español."""
    exc = ValueError("monto inválido: 'xx'")
    with app.app_context():
        fn = app.jinja_env.filters["humanizar"]
        out = fn(exc)
    assert "'xx'" in out


def test_filter_humanizar_con_string_pasa_tal_cual(app):
    with app.app_context():
        fn = app.jinja_env.filters["humanizar"]
        assert fn("ya es un texto") == "ya es un texto"


def test_filter_humanizar_con_none(app):
    with app.app_context():
        fn = app.jinja_env.filters["humanizar"]
        assert fn(None) == ""


# ---------------------------------------------------------------------------
# Error handlers globales 404 / 500
# ---------------------------------------------------------------------------

def test_404_handler_renderiza_template(client):
    r = client.get("/ruta-que-no-existe-123")
    assert r.status_code == 404
    assert b"404" in r.data
    # mensaje amigable
    assert b"No encontramos" in r.data or b"No encontramos" in r.data


def test_500_handler_muestra_request_id(app, client, fake_db):
    """Un 500 del view muestra el template amigable con request_id."""
    # agregar un endpoint que explota
    @app.route("/_boom_test_")
    def _boom():
        raise RuntimeError("explotó a propósito")

    # Flask en testing propaga por default; apagamos propagación para ver el handler.
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.config["TESTING"] = False
    r = client.get("/_boom_test_")
    assert r.status_code == 500
    assert b"500" in r.data
    # El request_id corto aparece en la página (8 chars).
    assert b"ID de error" in r.data
