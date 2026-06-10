"""Smoke del Flujo de Fondos proyectado (gap #1 estudio dBase vs PC 2026-06-10)."""


def test_flujo_fondos_renderiza(app, fake_db):
    rid = fake_db.add_role("Accionista", ["informes.ver"])
    uid = fake_db.add_user("tamara", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    r = c.get("/informes/flujo-fondos")
    assert r.status_code == 200
    assert "Flujo de Fondos proyectado".encode() in r.data
