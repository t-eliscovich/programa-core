"""End-to-end smoke test — walk every GET route with a stubbed DB.

Goals:
- import `app` without hitting a real database,
- fake a logged-in "Dueño" user with every permission,
- hit every registered GET route and confirm we get a response code < 500,
- catch template / Jinja errors, NameErrors, missing columns, broken imports.

Run with:
    python -m pytest tests/test_routes_smoke.py -x
or bare:
    python tests/test_routes_smoke.py
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# Make project importable when run directly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Default env so create_app doesn't complain.
os.environ.setdefault("SECRET_KEY", "test-key")


# --------------------------------------------------------------------------
# Stub DB + auth before app import so no real Postgres is touched.
# --------------------------------------------------------------------------

def _make_db_stub():
    """Return a dict of db.* attributes that return safe empty values."""
    return {
        "init_pool":  lambda *a, **kw: None,
        "close_pool": lambda *a, **kw: None,
        "fetch_all":  lambda *a, **kw: [],
        "fetch_one":  lambda *a, **kw: None,
        "execute":    lambda *a, **kw: 0,
    }


def _make_fake_user():
    # Keys must match what auth.load_logged_in_user / templates / views read.
    return {
        "id_usuario": 1,
        "username":   "test",
        "id_rol":     1,
        "nombre_rol": "Dueño",
        "activo":     True,
    }


# Every permiso we've ever used in @requiere_permiso decorators.
ALL_PERMS = {
    "dashboard.ver",
    "facturas.ver", "facturas.exportar",
    "cheques.ver",  "cheques.exportar",
    "bancos.ver",   "bancos.exportar",
    "compras.ver",  "compras.exportar",
    "clientes.ver", "clientes.exportar",
    "proveedores.ver", "proveedores.exportar",
    "retenciones.ver", "retenciones.exportar",
    "caja.ver",     "caja.exportar",
    "capital.ver",  "capital.exportar",
    "provisiones.ver", "provisiones.exportar",
    "proformas.ver", "proformas.exportar",
    "informes.ver", "informes.exportar",
    "informes.cartera", "informes.deudas", "informes.flujo",
    "informes.gastos",  "informes.estado_cuenta",
    "informes.ventas",  "informes.retiros",
    "informes.historia", "informes.iniciales", "informes.activos",
    "informes.balance",
}


def _apply_db_stubs(db_stub):
    import db as real_db
    for name, fn in db_stub.items():
        setattr(real_db, name, fn)


def build_app():
    db_stub = _make_db_stub()
    _apply_db_stubs(db_stub)

    # Patch auth.load_logged_in_user so request_ctx always has our fake user.
    import auth as real_auth

    def fake_loader():
        from flask import g, session
        session["usuario_id"] = 1
        g.user = _make_fake_user()
        g.permisos = set(ALL_PERMS)

    real_auth.load_logged_in_user = fake_loader

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


def iter_get_routes(app):
    """Yield (endpoint, url) for every static GET rule."""
    skip_prefixes = ("/static", "/_debug")
    for rule in app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()):
            continue
        if rule.rule.startswith(skip_prefixes):
            continue
        if "<" in rule.rule:
            # URLs with path params — skip, they need real ids.
            continue
        yield rule.endpoint, rule.rule


def main() -> int:
    app = build_app()
    client = app.test_client()

    failed = []
    total = 0
    for endpoint, url in iter_get_routes(app):
        total += 1
        try:
            rv = client.get(url, follow_redirects=False)
        except Exception:
            failed.append((endpoint, url, 500, traceback.format_exc()))
            continue
        code = rv.status_code
        # /healthz/ready intencionalmente devuelve 503 si la DB está caída.
        # En el smoke test la DB es un stub sin SELECT 1, así que 503 es
        # esperado — no es falla real.
        if code >= 500 and endpoint != "healthz.readiness":
            body = rv.get_data(as_text=True)[:500]
            failed.append((endpoint, url, code, body))

    print(f"\n{total} GET routes walked, {len(failed)} failures\n")
    for endpoint, url, code, msg in failed:
        print(f"  FAIL {code}  {endpoint:40s}  {url}")
        print("     " + msg.replace("\n", "\n     ")[:800])
        print()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
