#!/usr/bin/env python3
"""Medir TODAS las pantallas del programa, sin deploy y sin tocar producción.

    python3 scripts/medir_pantallas.py                    # todas, ordenadas
    python3 scripts/medir_pantallas.py /cheques /facturas  # sólo estas
    python3 scripts/medir_pantallas.py --guardar antes.json
    python3 scripts/medir_pantallas.py --comparar antes.json

⭐ PARA QUÉ, y en qué se diferencia de /admin/pantallas

Son las dos mitades de la misma pregunta y ninguna reemplaza a la otra:

  · **/admin/pantallas** (en producción) dice QUÉ está lento de verdad, con el
    uso real y los datos reales. Es el termómetro.
  · **esto** (local) sirve para PROBAR UN CAMBIO antes de subirlo: se mide,
    se toca, se vuelve a medir, y las dos fotos se comparan. En producción eso
    no se puede hacer sin deployar dos veces.

⚠ Y lo que NO es: una predicción de cuánto va a tardar en el servidor. La base
local no tiene ni los datos ni el hardware de la fábrica. Lo que se lee acá es
la DIFERENCIA entre dos corridas —y sobre todo la columna de CONSULTAS, que no
depende del hardware ni del volumen: si una pantalla hace 600 consultas, hace
600 acá y allá—.

Esa columna es la que más veces contestó "¿por qué está lenta?": 600 consultas
es una consulta adentro de un `for`, y eso se arregla siempre.

SETUP: el mismo Postgres embebido de `scripts/vista_local.py` — ver su docstring.
Con la base de tests (poca data) los milisegundos dicen poco pero las consultas
ya se cuentan bien. Para que los tiempos signifiquen algo, poner un dump real en
$DUMP_LOCAL.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# ⚠ Por ruta y no `from scripts import …`: `scripts/` no es un paquete (no
# tiene `__init__.py`) y agregárselo cambiaría cómo lo miran coverage y ruff.
import vista_local  # noqa: E402


def _app(permisos: str, rol: str, vend: str | None):
    os.environ.update(vista_local._env())
    os.environ.setdefault("WARMUP_ASINFO", "0")
    os.environ.setdefault("AUTOCARGA_FACTURAS", "0")
    os.environ.setdefault("PDF_NAVEGADOR_PERSISTENTE", "0")
    from flask import g

    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_local():
        g.user = {"id_usuario": 0, "username": "local", "id_rol": 0,
                  "nombre_rol": rol, "activo": True, "vend": vend}
        g.permisos = {x.strip() for x in permisos.split(",") if x.strip()}

    return app


def _rutas_sin_parametros(app) -> list[str]:
    """Las pantallas que se pueden abrir sin inventar un id.

    Se saltean las que tienen `<algo>` en la ruta (habría que inventar un
    cliente o una factura que exista) y las que no son pantallas: estáticos,
    healthz, logout y el runner de migraciones —que ESCRIBE—.
    """
    return sorted({
        r.rule for r in app.url_map.iter_rules()
        if "GET" in (r.methods or ()) and "<" not in r.rule
        and not r.rule.startswith(("/static", "/healthz", "/logout", "/admin/migra"))
    })


def medir(app, rutas: list[str], vueltas: int = 2) -> list[dict]:
    """Abre cada pantalla y anota cuánto tardó y cuántas consultas hizo.

    Se abre `vueltas` veces y se guarda la MEJOR: la primera visita paga cosas
    que no son de la pantalla (el plan de las consultas, las cachés frías del
    proceso) y lo que se quiere comparar entre dos corridas es el piso.
    """
    import db

    cuenta = [0]
    original = {}
    for nombre in ("fetch_one", "fetch_all", "execute", "execute_returning"):
        original[nombre] = getattr(db, nombre)

        def envolver(fn):
            def wrap(*a, **k):
                cuenta[0] += 1
                return fn(*a, **k)
            return wrap

        setattr(db, nombre, envolver(original[nombre]))

    cliente = app.test_client()
    salida = []
    try:
        for ruta in rutas:
            mejor, consultas, codigo, tam = None, 0, 0, 0
            for _ in range(vueltas):
                cuenta[0] = 0
                t0 = time.perf_counter()
                try:
                    r = cliente.get(ruta, follow_redirects=True)
                    codigo, tam = r.status_code, len(r.data)
                except Exception as e:  # noqa: BLE001 -- una pantalla rota no corta el barrido
                    codigo, tam = f"ERROR {type(e).__name__}", 0
                ms = (time.perf_counter() - t0) * 1000
                if mejor is None or ms < mejor:
                    mejor, consultas = ms, cuenta[0]
            salida.append({"ruta": ruta, "ms": round(mejor), "consultas": consultas,
                           "kb": round(tam / 1024), "codigo": codigo})
    finally:
        for nombre, fn in original.items():
            setattr(db, nombre, fn)
    salida.sort(key=lambda f: -f["ms"])
    return salida


def imprimir(filas: list[dict], top: int) -> None:
    print(f"{'ms':>7} {'consultas':>10} {'kB':>7}  pantalla")
    for f in filas[:top]:
        aviso = "  ← una consulta por fila" if f["consultas"] >= 30 else ""
        print(f"{f['ms']:>7} {f['consultas']:>10,} {f['kb']:>7,}  {f['ruta']} "
              f"[{f['codigo']}]{aviso}")
    print(f"\n{len(filas)} pantallas · {sum(f['ms'] for f in filas) / 1000:,.1f} s "
          f"· {sum(f['consultas'] for f in filas):,} consultas en total")


def comparar(antes: list[dict], ahora: list[dict]) -> None:
    """Las dos fotos, lado a lado. Lo que importa es lo que CAMBIÓ."""
    viejo = {f["ruta"]: f for f in antes}
    filas = [(a["ms"] - viejo[a["ruta"]]["ms"],
              a["consultas"] - viejo[a["ruta"]]["consultas"], a)
             for a in ahora if a["ruta"] in viejo]
    filas.sort(key=lambda x: x[0])
    movidas = [f for f in filas if abs(f[0]) >= 20 or f[1] != 0]
    if not movidas:
        print("Ninguna pantalla se movió (más de 20 ms o alguna consulta).")
        return
    print(f"{'ms':>16} {'consultas':>16}  pantalla")
    for dms, _dq, a in movidas:
        vi = viejo[a["ruta"]]
        print(f"{vi['ms']:>6} → {a['ms']:<7} {vi['consultas']:>6} → {a['consultas']:<7}"
              f"  {a['ruta']}   {'▼' if dms < 0 else '▲'} {abs(dms)} ms")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("rutas", nargs="*", help="pantallas a medir (default: todas)")
    p.add_argument("--top", type=int, default=25, help="cuántas mostrar (default 25)")
    p.add_argument("--vueltas", type=int, default=2)
    p.add_argument("--guardar", type=Path, help="escribe la foto en un .json")
    p.add_argument("--comparar", type=Path, help="compara contra una foto guardada")
    p.add_argument("--permisos", default="*")
    p.add_argument("--rol", default="Accionista")
    p.add_argument("--vend", default=None, help="medir como VENDEDOR (p.ej. PPR)")
    a = p.parse_args()

    vista_local.asegurar_postgres()
    vista_local.asegurar_db()
    app = _app(a.permisos, a.rol, a.vend)
    rutas = a.rutas or _rutas_sin_parametros(app)
    print(f"[medir] {len(rutas)} pantallas, {a.vueltas} vueltas cada una…\n")
    filas = medir(app, rutas, a.vueltas)

    if a.comparar:
        comparar(json.loads(a.comparar.read_text()), filas)
    else:
        imprimir(filas, a.top)
    if a.guardar:
        a.guardar.write_text(json.dumps(filas, indent=1))
        print(f"\nfoto guardada en {a.guardar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
