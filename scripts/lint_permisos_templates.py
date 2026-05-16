#!/usr/bin/env python3
"""Lint check — previene el bug G de la auditoría 2026-05-16.

Templates DEBEN usar `tiene_permiso('x.y')` en vez de `'x.y' in g.permisos`,
porque el primero honra el wildcard `*` que tiene el rol Dueño y el segundo no.

Si un template usa el patrón malo, el Dueño no ve el botón/sección protegida.

Uso:
    python scripts/lint_permisos_templates.py          # exit 1 si encuentra algo
    python scripts/lint_permisos_templates.py --fix    # auto-fix donde se pueda

Integrar en CI (.github/workflows/) o pre-commit (.pre-commit-config.yaml).
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIRS = [ROOT / "templates", ROOT / "modules"]

# Captura: opcional `g.permisos and `, después `'x.y' in g.permisos`.
PAT = re.compile(
    r"""(?:g\.permisos\s+and\s+)?       # 'g.permisos and' opcional
        ['"]([a-z_]+\.[a-z_]+)['"]\s+    # 'permiso.nombre'
        in\s+g\.permisos                  # in g.permisos
     """,
    re.VERBOSE,
)


def find_offenders():
    offenders: list[tuple[Path, int, str, str]] = []
    for tpl_dir in TEMPLATE_DIRS:
        if not tpl_dir.exists():
            continue
        for html in tpl_dir.rglob("*.html"):
            try:
                txt = html.read_text(encoding="utf-8")
            except Exception:
                continue
            for i, line in enumerate(txt.splitlines(), start=1):
                m = PAT.search(line)
                if m:
                    offenders.append((html, i, line.strip(), m.group(1)))
    return offenders


def autofix(offenders):
    """Reemplaza `g.permisos and 'x.y' in g.permisos` → `tiene_permiso('x.y')`."""
    by_file: dict[Path, list[tuple[int, str, str]]] = {}
    for path, ln, line, perm in offenders:
        by_file.setdefault(path, []).append((ln, line, perm))

    fixed = 0
    for path, items in by_file.items():
        txt = path.read_text(encoding="utf-8")
        for _, _, perm in items:
            old1 = f"g.permisos and '{perm}' in g.permisos"
            old2 = f'g.permisos and "{perm}" in g.permisos'
            old3 = f"'{perm}' in g.permisos"
            old4 = f'"{perm}" in g.permisos'
            nuevo = f"tiene_permiso('{perm}')"
            for o in (old1, old2, old3, old4):
                if o in txt:
                    txt = txt.replace(o, nuevo)
                    fixed += 1
                    break
        path.write_text(txt, encoding="utf-8")
    return fixed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fix", action="store_true", help="Reemplazar automáticamente.")
    args = p.parse_args()

    offenders = find_offenders()
    if not offenders:
        print("✓ No se encontraron templates usando `'x.y' in g.permisos` directo.")
        return 0

    print(f"⚠️  {len(offenders)} templates usan el patrón malo:")
    for path, ln, line, perm in offenders:
        rel = path.relative_to(ROOT)
        print(f"   {rel}:{ln}  ({perm})")
        print(f"       {line[:120]}")

    if args.fix:
        n = autofix(offenders)
        print(f"\n✅ {n} reemplazos hechos. Re-correr para verificar.")
        return 0

    print()
    print("Para arreglar: usá `tiene_permiso('x.y')` en vez de `'x.y' in g.permisos`")
    print("(o correr este script con --fix).")
    print("Razón: tiene_permiso() honra el wildcard `*` del rol Dueño; el `in` no.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
