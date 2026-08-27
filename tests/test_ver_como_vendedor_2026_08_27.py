"""INT puede "Ver como" los VENDEDORES — y a nadie más.

Dueña 2026-08-27: *"¿Podemos autorizar a INT a ver como los vendedores?"*.

El permiso es `vendedores.ver_como` (mig 0236, rol INT). Lo que NO cambia:
verse como un usuario que no es vendedor sigue siendo sólo de Accionistas —
un vendedor tiene MENOS permisos que INT, así que verse como él no escala;
verse como un Accionista sí escalaría, y el gate chequea el DESTINO, no sólo
el origen.
"""
from __future__ import annotations

import bcrypt

_PW = "Clave2026segura1"


def _hash(pw: str) -> bytes:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4))


def _armar(fake_db):
    rid_acc = fake_db.add_role("Accionista", ["*"])
    rid_int = fake_db.add_role("INT", ["cheques.ver", "vendedores.ver_como"])
    rid_vend = fake_db.add_role("Vendedor", ["micartera.ver"])
    rid_conta = fake_db.add_role("Contabilidad", ["cheques.ver"])
    h = _hash(_PW)
    return {
        "jefa": fake_db.add_user("jefa", h, rid_acc),
        "alex": fake_db.add_user("alex", h, rid_int),
        "roberto": fake_db.add_user("roberto", h, rid_vend, vend="RMY"),
        "conta": fake_db.add_user("conta", h, rid_conta),
    }


def _login(client, username: str) -> None:
    client.post("/login", data={"username": username, "password": _PW})


def _sesion(client) -> dict:
    with client.session_transaction() as s:
        return {"user_id": s.get("user_id"), "desde": s.get("impersonating_from_id")}


def test_int_puede_ver_como_un_vendedor(app, client, fake_db):
    ids = _armar(fake_db)
    _login(client, "alex")

    r = client.post(f"/impersonate/{ids['roberto']}")

    assert r.status_code == 302
    s = _sesion(client)
    assert s["user_id"] == ids["roberto"], "la sesión tiene que pasar al vendedor"
    assert s["desde"] == ids["alex"], "y guardar de quién volver"


def test_int_no_puede_verse_como_un_accionista(app, client, fake_db):
    """⭐ El límite es el DESTINO: un Accionista impersonado son permisos
    wildcard — escalada directa. El gate rechaza a cualquier destino sin
    `vend`, aunque el origen tenga el permiso."""
    ids = _armar(fake_db)
    _login(client, "alex")

    r = client.post(f"/impersonate/{ids['jefa']}")

    assert r.status_code == 302
    s = _sesion(client)
    assert s["user_id"] == ids["alex"], "la sesión NO tiene que moverse"
    assert s["desde"] is None


def test_sin_el_permiso_no_hay_ver_como_ni_a_un_vendedor(app, client, fake_db):
    ids = _armar(fake_db)
    _login(client, "conta")

    client.post(f"/impersonate/{ids['roberto']}")

    s = _sesion(client)
    assert s["user_id"] == ids["conta"]
    assert s["desde"] is None


def test_accionista_sigue_viendo_como_cualquiera(app, client, fake_db):
    """Regresión: el camino viejo (Accionista → cualquier usuario, tenga o
    no `vend`) no se achica con el permiso nuevo."""
    ids = _armar(fake_db)
    _login(client, "jefa")

    client.post(f"/impersonate/{ids['alex']}")

    s = _sesion(client)
    assert s["user_id"] == ids["alex"]
    assert s["desde"] == ids["jefa"]


def test_la_confirmacion_de_int_cancela_hacia_su_pantalla(app, client, fake_db):
    """El Cancelar de la confirmación no puede llevar a /usuarios: INT no
    tiene `usuarios.admin` y eso sería un 404 sin síntoma en el código."""
    ids = _armar(fake_db)
    _login(client, "alex")

    r = client.get(f"/impersonate/{ids['roberto']}")

    assert r.status_code == 200
    html = r.data.decode()
    assert "/usuarios/vendedores" in html
    assert f'action="/impersonate/{ids["roberto"]}"' in html and "csrf_token" in html


def test_pantalla_vendedores_lista_y_linkea(app, client, fake_db, monkeypatch):
    ids = _armar(fake_db)
    _login(client, "alex")

    from modules.usuarios import queries as uq

    monkeypatch.setattr(
        uq,
        "listar_vendedores",
        lambda: [
            {"id_usuario": ids["roberto"], "username": "roberto",
             "vend": "RMY", "vendedor": "Roberto Miranda"},
        ],
    )

    r = client.get("/usuarios/vendedores")

    assert r.status_code == 200
    html = r.data.decode()
    assert "RMY" in html and "Roberto Miranda" in html
    assert f"/impersonate/{ids['roberto']}" in html


def test_pantalla_vendedores_sin_permiso_da_404(app, client, fake_db):
    _armar(fake_db)
    _login(client, "conta")

    assert client.get("/usuarios/vendedores").status_code == 404
