#!/usr/bin/env python3
"""Lint custom: cada query SQL contra scintela.factura/compra/dolares en
archivos del balance/reports debe incluir el filtro NO_BACKFILL_WHERE
(`usuario_crea != 'asinfo-backfill'`) o documentar la excepción con `# noqa: backfill`.

TMT 2026-06-10 — Capa 5 de protección contra el bug "utilidad inflada".

Origen del bug: las funciones TOTF/TOTC/anticipos/retiros en informes/queries.py
sumaban facturas/cheques/dolares/retiros sin filtrar usuario_crea. Cuando
Tamara cargó 320 facturas vía Asinfo manual, esas filas pasaron a TOTF live y
la utilidad infló +$420k. La lente debe atrapar el caso si alguien agrega una
query nueva sin el filtro.

Uso:
    python scripts/lint_no_backfill_filter.py [archivo1.py archivo2.py ...]

Sin argumentos, escanea modules/informes/queries.py (donde vive el bug típico).

Exit codes:
    0 = todo OK
    1 = encontró queries riesgosas sin filtro NO_BACKFILL_WHERE
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Tablas que afectan el balance/utilidad si se cuentan filas backfill.
TABLAS_RIESGOSAS = ("factura", "compra", "compras", "dolares", "retiros", "cheque")

# Patterns que matchean "agrega/suma desde tabla" — específicamente queries
# que probablemente hacen SUM/COUNT (aggregations que afectan balance).
AGGREGATION_HINTS = ("SUM(", "COUNT(", "GROUP BY")

# Frases que indican que la query SÍ tiene el filtro (cualquiera de estas).
FILTER_PATTERNS = (
    "asinfo-backfill",
    "NO_BACKFILL_WHERE",  # constante
    "no_backfill_where",
)

# Marker para suprimir el lint en una query legítima sin filtro
# (ej. listado en pantalla que muestra TODO, incluyendo backfill).
NOQA_MARKER = "# noqa: backfill"


def _extract_sql_strings(src: str) -> list[tuple[int, str]]:
    """Extrae triple-quoted strings (multi-line SQL) con line number.

    Retorna [(line_no, sql_text), ...].
    """
    out = []
    # Triple-quoted strings (most SQL queries are in those)
    for m in re.finditer(r'"""([^"\\]|"(?!"")|\\.)*"""|\'\'\'([^\'\\]|\'(?!\'\'\')|\\.)*\'\'\'',
                         src, re.DOTALL):
        text = m.group(0)
        line_no = src[:m.start()].count("\n") + 1
        out.append((line_no, text))
    return out


def _has_risky_aggregation(sql: str) -> bool:
    """¿La query es una SUM/COUNT contra alguna tabla riesgosa?"""
    sql_upper = sql.upper()
    if not any(h in sql_upper for h in (h.upper() for h in AGGREGATION_HINTS)):
        return False
    return any(f"SCINTELA.{t.upper()}" in sql_upper for t in TABLAS_RIESGOSAS)


def _has_filter(sql: str) -> bool:
    """¿La query incluye el filtro NO_BACKFILL_WHERE o equivalente?"""
    return any(p in sql for p in FILTER_PATTERNS)


def _has_noqa(src_around_line: str) -> bool:
    """¿Está marcada como excepción con # noqa: backfill?"""
    return NOQA_MARKER in src_around_line


def lint_file(path: Path) -> list[tuple[int, str]]:
    """Retorna lista de [(line_no, snippet)] con queries riesgosas sin filtro."""
    src = path.read_text(encoding="utf-8")
    violations = []
    for line_no, sql in _extract_sql_strings(src):
        if not _has_risky_aggregation(sql):
            continue
        if _has_filter(sql):
            continue
        # Check noqa marker en 3 líneas alrededor del string
        lines = src.splitlines()
        i = max(0, line_no - 3)
        j = min(len(lines), line_no + 3)
        ctx = "\n".join(lines[i:j])
        if _has_noqa(ctx):
            continue
        snippet = " ".join(sql.split())[:140]
        violations.append((line_no, snippet))
    return violations


def _baseline_path() -> Path:
    return Path(__file__).resolve().parent / "_lint_no_backfill_baseline.txt"


def _load_baseline() -> set[str]:
    p = _baseline_path()
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}


def _signature(snippet: str) -> str:
    """Firma de una violación insensible a line-number — usamos los primeros
    80 chars del snippet normalizado. Resistente a reordenamientos del archivo."""
    # Solo letras/dígitos, lowercase, primeros 80 chars
    norm = "".join(c.lower() for c in snippet if c.isalnum())[:80]
    return norm


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    update_baseline = "--update-baseline" in argv
    files_args = [a for a in argv if not a.startswith("--")]

    if files_args:
        files = [Path(f) for f in files_args]
    else:
        repo_root = Path(__file__).resolve().parent.parent
        files = [
            repo_root / "modules" / "informes" / "queries.py",
        ]

    all_violations = []  # list of (file, line_no, snippet, signature)
    for f in files:
        if not f.exists():
            print(f"  WARN: {f} no existe, skip")
            continue
        violations = lint_file(f)
        for line_no, snippet in violations:
            all_violations.append((f, line_no, snippet, _signature(snippet)))

    # Update baseline mode: dump signatures + exit 0.
    if update_baseline:
        sigs = sorted({sig for *_, sig in all_violations})
        _baseline_path().write_text("\n".join(sigs) + "\n", encoding="utf-8")
        print(
            f"Updated baseline: {_baseline_path()} ({len(sigs)} signatures). "
            f"Estas violaciones quedan ignoradas; nuevas violaciones harán fail."
        )
        return 0

    baseline = _load_baseline()
    nuevas = [v for v in all_violations if v[3] not in baseline]
    conocidas = [v for v in all_violations if v[3] in baseline]

    if nuevas:
        print("\n=== NUEVAS VIOLACIONES (no en baseline) ===")
        for f, ln, snip, _ in nuevas:
            try:
                rel = f.relative_to(Path.cwd())
            except ValueError:
                rel = f
            print(f"  {rel}:{ln} → {snip[:120]}")

    print(
        f"\nResumen: nuevas={len(nuevas)} · conocidas={len(conocidas)} · "
        f"baseline={len(baseline)}"
    )

    if nuevas:
        print(
            "\nFAIL: hay NUEVAS queries SUM/COUNT contra scintela.factura/compra/"
            "dolares/retiros/cheque sin filtro NO_BACKFILL_WHERE.\n"
            "Opciones:\n"
            "  1. Agregar `AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'` al WHERE.\n"
            "  2. Si es intencional (listado que muestra todo): `# noqa: backfill`.\n"
            "  3. Si reorganizaste y son las mismas que ya existían:\n"
            "     `python scripts/lint_no_backfill_filter.py --update-baseline`\n\n"
            "TMT 2026-06-10: este lint previene la regresión del bug 'utilidad inflada'."
        )
        return 1

    if conocidas:
        print(
            f"OK: {len(conocidas)} violaciones LEGACY en baseline ignoradas, sin nuevas. "
            f"(Si querés limpiar legacy: fixea + corre --update-baseline)."
        )
    else:
        print("OK: 0 violaciones.")
    return 0 if not strict or len(conocidas) == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
