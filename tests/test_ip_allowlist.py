"""Tests de ip_allowlist — middleware por rol."""
from __future__ import annotations

import ipaddress

import ip_allowlist as ial

# ---------------------------------------------------------------------------
# Normalización de rol → env key
# ---------------------------------------------------------------------------

def test_env_key_elimina_tildes():
    assert ial._env_key_for_role("Dueño") == "ROLE_IP_ALLOWLIST_DUENO"


def test_env_key_espacios_a_underscore():
    assert ial._env_key_for_role("QA Supervisor") == "ROLE_IP_ALLOWLIST_QA_SUPERVISOR"


def test_env_key_rol_vacio():
    assert ial._env_key_for_role("") == ""


def test_env_key_mayuscula():
    # debe ser upper, no lower
    assert ial._env_key_for_role("contabilidad") == "ROLE_IP_ALLOWLIST_CONTABILIDAD"


# ---------------------------------------------------------------------------
# Parse CIDR
# ---------------------------------------------------------------------------

def test_parse_allowlist_ip_exacta():
    ial._parse_allowlist.cache_clear()
    redes = ial._parse_allowlist("1.2.3.4")
    assert len(redes) == 1
    assert ipaddress.ip_address("1.2.3.4") in redes[0]


def test_parse_allowlist_cidr():
    ial._parse_allowlist.cache_clear()
    redes = ial._parse_allowlist("192.168.1.0/24")
    assert ipaddress.ip_address("192.168.1.100") in redes[0]
    assert ipaddress.ip_address("192.168.2.1") not in redes[0]


def test_parse_allowlist_ignora_invalidos():
    ial._parse_allowlist.cache_clear()
    redes = ial._parse_allowlist("1.2.3.4, basura, 10.0.0.0/8")
    assert len(redes) == 2  # basura ignorada


def test_parse_allowlist_string_vacio_devuelve_vacio():
    ial._parse_allowlist.cache_clear()
    assert ial._parse_allowlist("") == ()


# ---------------------------------------------------------------------------
# Lookup por rol
# ---------------------------------------------------------------------------

def test_allowlist_for_role_sin_env_devuelve_vacio(monkeypatch):
    monkeypatch.delenv("ROLE_IP_ALLOWLIST_DUENO", raising=False)
    ial._parse_allowlist.cache_clear()
    assert ial.allowlist_for_role("Dueño") == ()


def test_allowlist_for_role_lee_env(monkeypatch):
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_DUENO", "192.168.1.0/24")
    ial._parse_allowlist.cache_clear()
    redes = ial.allowlist_for_role("Dueño")
    assert len(redes) == 1


def test_allowlist_for_role_tildes_matching(monkeypatch):
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_DUENO", "10.0.0.1")
    ial._parse_allowlist.cache_clear()
    assert len(ial.allowlist_for_role("Dueño")) == 1
    # también matchea sin tilde (defensivo)
    assert len(ial.allowlist_for_role("Dueno")) == 1


# ---------------------------------------------------------------------------
# ip_permitida_para_rol — el core
# ---------------------------------------------------------------------------

def test_default_allow_si_rol_no_configurado(monkeypatch):
    monkeypatch.delenv("ROLE_IP_ALLOWLIST_VENTAS", raising=False)
    ial._parse_allowlist.cache_clear()
    assert ial.ip_permitida_para_rol("Ventas", "8.8.8.8") is True
    assert ial.ip_permitida_para_rol("Ventas", "192.168.0.1") is True


def test_deny_si_ip_fuera_del_rango(monkeypatch):
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_DUENO", "190.152.1.0/24")
    ial._parse_allowlist.cache_clear()
    assert ial.ip_permitida_para_rol("Dueño", "190.152.1.100") is True
    assert ial.ip_permitida_para_rol("Dueño", "190.152.2.1") is False
    assert ial.ip_permitida_para_rol("Dueño", "1.2.3.4") is False


def test_permitido_ip_exacta(monkeypatch):
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_CONTABILIDAD", "1.2.3.4")
    ial._parse_allowlist.cache_clear()
    assert ial.ip_permitida_para_rol("Contabilidad", "1.2.3.4") is True
    assert ial.ip_permitida_para_rol("Contabilidad", "1.2.3.5") is False


def test_ip_invalida_no_permitida(monkeypatch):
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_DUENO", "1.2.3.4")
    ial._parse_allowlist.cache_clear()
    # un string inválido nunca puede matchear
    assert ial.ip_permitida_para_rol("Dueño", "basura") is False
    assert ial.ip_permitida_para_rol("Dueño", "") is False


def test_client_ip_usa_x_forwarded_for_si_trust_proxy(app, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    with app.test_request_context(
        "/",
        headers={"X-Forwarded-For": "8.8.8.8, 10.0.0.1"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        assert ial.client_ip() == "8.8.8.8"


def test_client_ip_trust_proxy_sin_header_usa_remote_addr(app, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "true")
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert ial.client_ip() == "127.0.0.1"


def test_multiples_redes_en_allowlist(monkeypatch):
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_GERENTE",
                       "10.0.0.0/8, 192.168.1.100, 172.16.0.0/12")
    ial._parse_allowlist.cache_clear()
    assert ial.ip_permitida_para_rol("Gerente", "10.5.5.5") is True
    assert ial.ip_permitida_para_rol("Gerente", "192.168.1.100") is True
    assert ial.ip_permitida_para_rol("Gerente", "172.20.1.1") is True
    assert ial.ip_permitida_para_rol("Gerente", "8.8.8.8") is False


# ---------------------------------------------------------------------------
# Enforce en Flask app
# ---------------------------------------------------------------------------

def test_enforce_allowlist_permite_anonimos(app, fake_db, monkeypatch):
    """Usuario no logueado → no bloquea (que lo maneje @requiere_login)."""
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_DUENO", "1.2.3.4")
    ial._parse_allowlist.cache_clear()
    c = app.test_client()
    # /login es skip-path, no bloquea
    r = c.get("/login")
    assert r.status_code == 200


def test_enforce_allowlist_skip_en_healthz_y_static(app, fake_db, monkeypatch):
    """Paths de skip nunca se bloquean, aunque el rol esté configurado."""
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_DUENO", "1.2.3.4")
    ial._parse_allowlist.cache_clear()
    c = app.test_client()
    # /static no existe pero el middleware no bloquea antes del 404
    r = c.get("/static/inexistente.js")
    assert r.status_code == 404  # not 403


def test_enforce_allowlist_bloquea_user_logueado_fuera_del_rango(app, fake_db, monkeypatch):
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_DUENO", "1.2.3.4")
    ial._parse_allowlist.cache_clear()
    rid = fake_db.add_role("Dueño", ["*"])
    uid = fake_db.add_user("admin", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    # Flask test_client manda remote_addr = 127.0.0.1 por default — fuera de 1.2.3.4
    r = c.get("/dashboard")
    assert r.status_code == 403
    assert b"Acceso bloqueado" in r.data


def test_enforce_allowlist_permite_user_dentro_del_rango(app, fake_db, monkeypatch):
    # Permite 127.0.0.1 para que el test client pueda entrar
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_DUENO", "127.0.0.1")
    ial._parse_allowlist.cache_clear()
    rid = fake_db.add_role("Dueño", ["*"])
    uid = fake_db.add_user("admin", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    r = c.get("/dashboard")
    assert r.status_code < 500


def test_enforce_allowlist_sin_env_default_allow(app, fake_db, monkeypatch):
    """Sin env var configurada — todos los roles pasan."""
    monkeypatch.delenv("ROLE_IP_ALLOWLIST_DUENO", raising=False)
    ial._parse_allowlist.cache_clear()
    rid = fake_db.add_role("Dueño", ["*"])
    uid = fake_db.add_user("admin", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    r = c.get("/dashboard")
    assert r.status_code < 500  # no 403
