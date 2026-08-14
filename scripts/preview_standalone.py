"""Preview de una pantalla que se ve BIEN fuera de la app.

TMT 2026-08-14 (dueña, sobre un preview que le llegó sin estilos): *"tu preview
se ve así: buscá una manera más fácil de mostrarme y que se vea bien"*.

`vista_local.py` guarda el HTML tal cual lo manda Flask, con `<link
href="/static/tailwind.css">` y los `<script src="/static/...">`. Abierto como
archivo suelto, esas rutas no existen: la pantalla queda en texto negro sobre
blanco, con los SVG gigantes y el menú desplegado.

Esto deja UN archivo que se abre en cualquier lado: el CSS va embebido, los
scripts se sacan (un preview no interactúa) y las imágenes de /static quedan
como base64 si son chicas.

    python3 scripts/preview_standalone.py /bancos/10 -o /tmp/x.html
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def standalone(html: str) -> str:
    """Devuelve el HTML sin depender de nada externo."""
    # 1) CSS embebido.
    def _css(m: re.Match) -> str:
        href = m.group(1)
        f = STATIC / href.split("/static/")[-1].split("?")[0]
        if not f.exists():
            return ""
        return "<style>\n" + f.read_text(encoding="utf-8", errors="ignore") + "\n</style>"

    html = re.sub(r'<link[^>]+href="([^"]*?/static/[^"]+\.css[^"]*)"[^>]*>',
                  _css, html)
    # 2) Los scripts no aportan a un preview y algunos pintan al arrancar.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)
    html = re.sub(r"<script\b[^>]*/>", "", html)

    # 3) Imágenes de /static → base64 (el logo, los íconos).
    def _img(m: re.Match) -> str:
        src = m.group(2)
        f = STATIC / src.split("/static/")[-1].split("?")[0]
        if not f.exists() or f.stat().st_size > 512_000:
            return m.group(0)
        mime = mimetypes.guess_type(f.name)[0] or "image/png"
        b64 = base64.b64encode(f.read_bytes()).decode()
        return f'{m.group(1)}"data:{mime};base64,{b64}"'

    html = re.sub(r'(<img[^>]+src=)"([^"]*?/static/[^"]+)"', _img, html)
    return html


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("ruta", help="p.ej. /bancos/10")
    p.add_argument("-o", "--out", type=Path, default=Path("/tmp/preview.html"))
    a = p.parse_args()

    sys.path.insert(0, str(ROOT / "scripts"))
    import vista_local as V

    V.asegurar_postgres()
    V.asegurar_db()

    tmp = a.out.with_suffix(".raw.html")
    if V.render(a.ruta, tmp) != 0:
        return 1
    a.out.write_text(standalone(tmp.read_text(encoding="utf-8", errors="ignore")),
                     encoding="utf-8")
    tmp.unlink(missing_ok=True)
    print(f"[preview] {a.ruta} → {a.out} ({a.out.stat().st_size:,} bytes, autocontenido)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
