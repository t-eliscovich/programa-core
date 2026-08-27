"""`sesion.tabla_existe()` no puede quedarse con un error.

TMT 2026-08-26. La función pregunta UNA vez si corrió la migración 0060 y se
guarda la respuesta para todo el proceso. Cachear la respuesta buena está bien
—la migración no aparece sola, y cuando aparece es con un deploy, que reinicia—.

Cachear el ERROR no: si la app arrancaba con la base un segundo inaccesible,
quedaba en «no existe» y **toda la conciliación v2 redirigía al hub hasta que
alguien reiniciara el proceso**, sin un aviso en ningún lado. Es la familia
«algo que no anda y no avisa» que la dueña viene persiguiendo desde el 13/08.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.conciliacion import sesion as _sesion  # noqa: E402


def _limpio():
    """La fixture autouse del conftest deja el caché en True: acá arrancamos
    de cero a propósito."""
    _sesion.olvidar_si_existe_la_tabla()


def test_si_la_base_no_contesta_se_vuelve_a_preguntar(monkeypatch):
    """El caso que rompía. Primera llamada revienta → False, pero SIN cachear:
    la segunda pregunta de nuevo y, si la base ya volvió, dice la verdad."""
    _limpio()
    llamadas = []

    def _revienta(*a, **k):
        llamadas.append(1)
        raise RuntimeError("connection refused")

    monkeypatch.setattr(_sesion.db, "fetch_one", _revienta)
    assert _sesion.tabla_existe() is False
    assert not hasattr(_sesion.tabla_existe, "_cache"), (
        "se cacheó el error: la pantalla queda muerta hasta el próximo reinicio")

    # la base vuelve
    monkeypatch.setattr(_sesion.db, "fetch_one", lambda *a, **k: {"?column?": 1})
    assert _sesion.tabla_existe() is True
    assert len(llamadas) == 1, "la segunda vez tiene que volver a preguntar"


def test_la_respuesta_buena_se_cachea(monkeypatch):
    """Lo que no cambia: una respuesta de verdad se pregunta UNA sola vez."""
    _limpio()
    n = []
    monkeypatch.setattr(_sesion.db, "fetch_one",
                        lambda *a, **k: (n.append(1), {"?column?": 1})[1])
    assert _sesion.tabla_existe() is True
    assert _sesion.tabla_existe() is True
    assert len(n) == 1, "preguntó dos veces algo que no cambia"


def test_el_no_esta_tambien_se_cachea(monkeypatch):
    """«No corrió la migración» es una respuesta, no un error: se cachea igual.
    La migración no aparece sola — y cuando aparece, es con un deploy."""
    _limpio()
    n = []
    monkeypatch.setattr(_sesion.db, "fetch_one",
                        lambda *a, **k: (n.append(1), None)[1])
    assert _sesion.tabla_existe() is False
    assert _sesion.tabla_existe() is False
    assert len(n) == 1
