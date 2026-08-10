"""Dos pares de fuentes que TIENEN que coincidir, chequeados en el CI.

`static/tailwind.css` es un build CONGELADO: se regenera a mano con
`scripts/build-tailwind.sh`. Entre build y build, una clase de Tailwind nueva
en un template **no renderiza y no avisa** — el 05/08/2026 había ~488 así.
Y los links entre pantallas son strings hardcodeados, no `url_for`: uno que
apunta a una ruta que no existe sólo se ve como 404 al clickear.

Los dos son estáticos —templates, CSS y `url_map` viven en el repo—, así que el
lugar donde se atajan es el CI y no un ⚠ del día siguiente: acá el merge no
pasa, allá alguien se entera mañana. (El drift que SÍ necesita la base viva
—`config/roles.py` contra `seguridad.permiso`— vive en
`/admin/health/drift`.)

Los dos empiezan en CERO el 2026-08-10. Si este test se pone en rojo, la
respuesta NO es agrandar una lista de excepciones: es correr
`bash scripts/build-tailwind.sh` (clases) o arreglar el link.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

RAIZ = Path(_REPO_ROOT)

#: Prefijos de variante de Tailwind — hay que pelarlos para saber de qué
#: familia es una clase (`dark:hover:bg-x` es familia `bg`).
VARIANTES = (
    "sm:", "md:", "lg:", "xl:", "2xl:", "dark:", "hover:", "focus:",
    "focus-within:", "focus-visible:", "group-hover:", "group-open:",
    "disabled:", "file:", "placeholder:", "odd:", "even:", "first:", "last:",
    "print:", "active:", "checked:", "peer-checked:", "motion-safe:",
    "motion-reduce:",
)

#: Archivos HTML que NO son pantallas de la app (borradores sueltos en el repo
#: y la carpeta `preview/`). El build de Tailwind tampoco los escanea, así que
#: incluirlos sería comparar contra algo que nadie compila.
def _es_template_de_la_app(p: Path) -> bool:
    s = str(p.relative_to(RAIZ))
    if s.startswith((".tw-build", "node_modules", "preview/")):
        return False
    return not p.name.startswith(".tmp_")


def _templates() -> list[Path]:
    return [p for p in RAIZ.rglob("*.html") if _es_template_de_la_app(p)]


def _familia(clase: str) -> str:
    b = clase
    cambio = True
    while cambio:
        cambio = False
        for v in VARIANTES:
            if b.startswith(v):
                b = b[len(v):]
                cambio = True
                break
    return b.split("-")[0].split("[")[0]


def _clases_definidas_en_css() -> set[str]:
    out: set[str] = set()
    for c in (RAIZ / "static").rglob("*.css"):
        txt = c.read_text(encoding="utf-8", errors="ignore")
        out |= {x.replace("\\", "")
                for x in re.findall(r"(?<![\w\\])\.((?:\\.|[-\w])+)", txt)}
    return out


def _clases_propias_de_los_templates() -> set[str]:
    """Las que cada template define en su propio `<style>`.

    ⚠ Sin lookbehind a propósito: `th.col-header` y `input.p` también definen
    la clase. Con el lookbehind puesto, esas cuatro daban falso positivo.
    """
    out: set[str] = set()
    for p in _templates():
        s = p.read_text(encoding="utf-8", errors="ignore")
        for bloque in re.findall(r"<style[^>]*>(.*?)</style>", s, re.S):
            out |= set(re.findall(r"\.([-\w]+)", bloque))
    return out


def _clases_usadas() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for p in _templates():
        s = p.read_text(encoding="utf-8", errors="ignore")
        s = re.sub(r"<style[^>]*>.*?</style>", "", s, flags=re.S)
        s = re.sub(r"<script[^>]*>.*?</script>", "", s, flags=re.S)
        rel = str(p.relative_to(RAIZ))
        for attr in re.findall(r'class="([^"]*)"', s):
            tenia_jinja = "{{" in attr or "{%" in attr
            attr = re.sub(r"\{[{%].*?[%}]\}", " ", attr, flags=re.S)
            for t in attr.split():
                if not re.fullmatch(r"[-\w:/.\[\]%#()!]+", t):
                    continue
                # Un `bg-{{ _color }}-100` deja los pedazos `bg-` y `-100`:
                # no son clases, son las dos mitades de una que se arma sola
                # (y que va en el safelist de scripts/build-tailwind.sh).
                if tenia_jinja and (t.startswith("-") or t.endswith("-")
                                    or t.endswith(":")):
                    continue
                out.add((t, rel))
    return out


def test_no_hay_clases_de_tailwind_que_no_esten_compiladas():
    """⭐ La clase que no está en el CSS no hace NADA y no avisa.

    Sólo se miran los tokens cuya FAMILIA existe en el CSS compilado: así
    `w-16` (familia `w`, que existe) cuenta y `btn-guardar` (familia `btn`,
    que es un hook de JS) no. El criterio se ajusta solo cuando el CSS
    cambia — no hay lista escrita a mano que mantener.
    """
    definidas = _clases_definidas_en_css()
    propias = _clases_propias_de_los_templates()
    familias = {_familia(c) for c in definidas}

    fantasma: dict[str, str] = {}
    for clase, tpl in _clases_usadas():
        if clase in definidas or clase in propias:
            continue
        if _familia(clase) in familias:
            fantasma.setdefault(clase, tpl)

    assert not fantasma, (
        f"{len(fantasma)} clase(s) de Tailwind usadas y NO compiladas — no "
        f"renderizan y no avisan. Corré `bash scripts/build-tailwind.sh`. "
        f"Ejemplos: "
        + ", ".join(f"{c} ({t})" for c, t in sorted(fantasma.items())[:8])
    )


def test_los_links_literales_apuntan_a_pantallas_que_existen():
    """Un `href="/lo-que-sea"` escrito a mano tiene que resolver en el url_map.

    Generaliza `test_historial_links_resuelven`, que sólo cubría el mapeo del
    historial: los 404 aparecían recién cuando alguien clickeaba.
    """
    from tests.test_routes_smoke import build_app

    app, deshacer = build_app()
    try:
        adapter = app.url_map.bind("localhost")

        def resuelve(u: str) -> bool:
            try:
                adapter.match(u, method="GET")
                return True
            except Exception as e:  # noqa: BLE001
                # Existe pero no acepta GET: la ruta está, el link también.
                return e.__class__.__name__ == "MethodNotAllowed"

        rotos: dict[str, str] = {}
        for p in _templates():
            s = p.read_text(encoding="utf-8", errors="ignore")
            for href in re.findall(r'href="(/[^"{}]*)"', s):
                u = href.split("?")[0].split("#")[0]
                if not u or u.startswith("/static"):
                    continue
                if not resuelve(u):
                    rotos.setdefault(u, str(p.relative_to(RAIZ)))
    finally:
        deshacer()

    assert not rotos, (
        f"{len(rotos)} link(s) apuntan a una ruta que no existe: "
        + ", ".join(f"{u} (en {t})" for u, t in sorted(rotos.items())[:8])
    )
