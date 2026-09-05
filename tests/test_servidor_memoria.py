"""La memoria del servidor — `modules/_lib/servidor.py`, /admin/pantallas y el health.

Andrés 2026-09-05 (WhatsApp): *"está super lento el sistema, qué pasará? se
queda pensando"*. Igual que el 31/08: no era una pantalla, era la máquina sin
memoria (Metabase con 1,6 GB). Para verlo hubo que entrar por CloudShell; ahora
lo muestra el propio programa y lo vigila el health.
"""
from __future__ import annotations

from modules._lib import servidor


def _con_memoria(monkeypatch, disponible_mb, procesos=()):
    monkeypatch.setattr(servidor, "memoria", lambda: {
        "total_mb": 4036, "disponible_mb": disponible_mb, "usado_pct": 90.0})
    monkeypatch.setattr(servidor, "procesos", lambda n=8: list(procesos))
    monkeypatch.setattr(servidor, "cpu", lambda: {"cpu_pct": 12.0, "nucleos": 2})
    monkeypatch.setattr(servidor, "este_proceso", lambda: {"pid": 1, "memoria_mb": 300})


def test_lee_la_memoria_de_verdad():
    est = servidor.estado()
    assert est["total_mb"] > 0
    assert 0 <= est["disponible_mb"] <= est["total_mb"]
    assert est["programa"]["memoria_mb"] > 0
    # Ordenados por memoria privada, de mayor a menor.
    mbs = [p["memoria_mb"] for p in est["procesos"]]
    assert mbs == sorted(mbs, reverse=True)


def test_el_health_avisa_cuando_falta_memoria_y_dice_quien(monkeypatch):
    _con_memoria(monkeypatch, 63, [
        {"pid": 9, "nombre": "java.exe", "memoria_mb": 1600, "cpu_pct": 3.0},
        {"pid": 8, "nombre": "python.exe", "memoria_mb": 400, "cpu_pct": 1.0},
    ])
    h = servidor.health()
    assert h["ok"] is False
    assert h["alerts"][0]["tipo"] == "servidor_sin_memoria"
    assert "63 MB libres" in h["alerts"][0]["detalle"]
    assert "java.exe 1600 MB" in h["alerts"][0]["detalle"]


def test_el_health_calla_cuando_hay_memoria(monkeypatch):
    _con_memoria(monkeypatch, 1989)
    h = servidor.health()
    assert h["ok"] is True and h["alerts"] == []
    assert h["stats"]["disponible_mb"] == 1989


def test_sin_psutil_no_rompe_nada(monkeypatch):
    import builtins

    real = builtins.__import__

    def sin_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", sin_psutil)
    assert servidor.memoria() == {}
    assert servidor.procesos() == []
    assert servidor.health()["ok"] is True


def _login(app, fake_db, perms=("admin_dbase.ver",)):
    rid = fake_db.add_role("Tester", list(perms))
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_la_pantalla_muestra_el_servidor_arriba(app, fake_db, monkeypatch):
    _con_memoria(monkeypatch, 63, [
        {"pid": 9, "nombre": "java.exe", "memoria_mb": 1600, "cpu_pct": 3.0}])
    html = _login(app, fake_db).get("/admin/pantallas").get_data(as_text=True)
    assert "El servidor" in html
    assert "63 MB" in html
    assert "java.exe" in html
    assert "paginando a disco" in html
    assert html.index("El servidor") < html.index("Todavía no hay nada medido")


def test_la_pantalla_con_memoria_no_alarma(app, fake_db, monkeypatch):
    _con_memoria(monkeypatch, 1989)
    html = _login(app, fake_db).get("/admin/pantallas").get_data(as_text=True)
    assert "1989 MB" in html
    assert "paginando a disco" not in html
