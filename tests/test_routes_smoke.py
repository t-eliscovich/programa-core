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
import types
from pathlib import Path

# Make project importable when run directly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Default env so create_app doesn't complain.
os.environ.setdefault("SECRET_KEY", "test-key")
os.environ.setdefault("DISABLE_BOOT_SYNC", "1")


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


ALL_PERMS = {"*"}


def _apply_db_stubs(db_stub):
    """Pisa db.* con los stubs y devuelve los originales para restaurarlos.

    TMT 2026-07-26: antes esto pisaba `db` y `auth.load_logged_in_user` PARA
    SIEMPRE (sin monkeypatch, sin restore), así que todo test que corriera
    DESPUÉS de este archivo veía un usuario con permisos `*`. Eso hacía fallar
    —sólo en suite completa— a cualquier test que verifique que SIN permiso no
    pasa nada. Ahora se guarda y se restaura.
    """
    import db as real_db
    previos = {}
    for name, fn in db_stub.items():
        previos[name] = getattr(real_db, name, None)
        setattr(real_db, name, fn)
    return previos


def _restaurar(previos_db, loader_previo):
    import auth as real_auth
    import db as real_db
    for name, fn in (previos_db or {}).items():
        if fn is not None:
            setattr(real_db, name, fn)
    if loader_previo is not None:
        real_auth.load_logged_in_user = loader_previo


def build_app():
    """Devuelve (app, deshacer). `deshacer()` restaura los globals pisados.

    🚨 **`app.py` se importa ACÁ, ANTES de pisar el loader, y ese orden es el
    arreglo.** `app.py` hace `from auth import load_logged_in_user` en el
    import, o sea que se queda con UNA COPIA del nombre. Si el PRIMER import de
    `app.py` de todo el proceso ocurría acá —con el loader ya pisado—, `app.py`
    guardaba el `fake_loader` **para siempre**: `deshacer()` devuelve el de
    `auth`, que a esa altura ya no es el que la app registró. A partir de ahí
    toda app nueva nacía con un usuario logueado y wildcard de permisos, y los
    tests que verifican al ANÓNIMO (`test_requiere_login_redirects`, los 404
    por falta de permiso) fallaban… pero sólo si este archivo corría antes.

    Eso es lo que ponía el CI rojo sin que nadie tocara nada: con `-n auto` el
    reparto de archivos entre workers cambia en cada corrida (CI #2187 y #2189
    rojos, #2188 y #2190 verdes, mismo código), y con el CI rojo el deploy se
    frena solo y queda "bloqueado" — dos deploys del 18/08 quedaron así.
    Importando primero, la copia de `app.py` ya está tomada y el pisotón no la
    alcanza. TMT 2026-08-18.

    ⚠ Ojo con "arreglarlo" pisando también `app_mod.load_logged_in_user`: se
    probó, y ahí el smoke SÍ entra con wildcard a todas las rutas… y
    `/admin/health/simulacro-cierre` devuelve 500. O sea que este smoke hoy
    recorre las rutas como ANÓNIMO (302 al login, que no es 500 y pasa). Es una
    debilidad real del test, pero se anota aparte: no se tapa un 500 de
    producción adentro de un arreglo de flakiness.
    """
    import auth as real_auth

    previos_db = _apply_db_stubs(_make_db_stub())
    sys.modules["scripts.sync_stat_from_xlsx_boot"] = types.SimpleNamespace(maybe_run_once=lambda: None)

    # ⭐ El import de `app.py` va ACÁ: después de los stubs de la base (como
    # siempre fue) y ANTES de pisar el loader (lo nuevo). Ver el docstring.
    import app as app_mod

    # Patch auth.load_logged_in_user so request_ctx always has our fake user.
    loader_previo = real_auth.load_logged_in_user

    def fake_loader():
        from flask import g, session
        session["usuario_id"] = 1
        g.user = _make_fake_user()
        g.permisos = set(ALL_PERMS)

    real_auth.load_logged_in_user = fake_loader

    app = app_mod.create_app()
    app.config["TESTING"] = True
    return app, lambda: _restaurar(previos_db, loader_previo)


def iter_get_routes(app):
    """Yield (endpoint, url) for every static GET rule."""
    skip_endpoints = {"conciliacion.hub_selftest"}
    skip_prefixes = ("/static", "/_debug")
    for rule in app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()):
            continue
        if rule.endpoint in skip_endpoints:
            continue
        if rule.rule.startswith(skip_prefixes):
            continue
        if "<" in rule.rule:
            # URLs with path params — skip, they need real ids.
            continue
        yield rule.endpoint, rule.rule


def collect_route_failures(app):
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
        # /healthz/ready (healthz.readiness) y /_healthz (healthz)
        # intencionalmente devuelven 503 si la DB está caída. En el smoke test
        # la DB es un stub sin SELECT 1, así que 503 es esperado — no es falla
        # real. TMT 2026-06-03: se sumó /_healthz (endpoint "healthz") como
        # health-check de monitoring externo; antes solo se excluía readiness,
        # por eso la CI rompía en cada commit.
        if code >= 500 and endpoint not in ("healthz.readiness", "healthz"):
            body = rv.get_data(as_text=True)[:500]
            failed.append((endpoint, url, code, body))
    return total, failed


def test_static_get_routes_render_without_500():
    app, deshacer = build_app()
    try:
        total, failed = collect_route_failures(app)
    finally:
        deshacer()
    formatted = "\n".join(
        f"{code} {endpoint} {url}\n{msg[:800]}" for endpoint, url, code, msg in failed
    )
    assert total > 0
    assert not failed, formatted


def main() -> int:
    app, _deshacer = build_app()
    total, failed = collect_route_failures(app)

    print(f"\n{total} GET routes walked, {len(failed)} failures\n")
    for endpoint, url, code, msg in failed:
        print(f"  FAIL {code}  {endpoint:40s}  {url}")
        print("     " + msg.replace("\n", "\n     ")[:800])
        print()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_build_app_no_deja_pisado_el_loader_de_app_py():
    """El candado del flaky: `build_app()` no puede dejar a `app.py` con el
    login falso pegado.

    `app.py` hace `from auth import load_logged_in_user`, así que se queda con
    una COPIA del nombre. Si su primer import del proceso pasara con el loader
    ya pisado, esa copia quedaría con el `fake_loader` para siempre —
    `deshacer()` devuelve el de `auth`, no el de `app`— y toda app posterior
    nacería logueada y con wildcard. Eso ponía el CI rojo una corrida sí y otra
    no según cómo `-n auto` repartiera los archivos (18/08/2026).

    Por eso el test SACA `app` de `sys.modules` antes: es la única forma de
    reproducir "todavía no estaba importado", que es el único caso en que el
    orden adentro de `build_app()` cambia algo.
    """
    import sys

    import auth as real_auth

    loader_real = real_auth.load_logged_in_user
    modulo_previo = sys.modules.pop("app", None)
    try:
        _, deshacer = build_app()
        deshacer()
        import app as app_mod
        assert app_mod.load_logged_in_user is loader_real
    finally:
        if modulo_previo is not None:
            sys.modules["app"] = modulo_previo
