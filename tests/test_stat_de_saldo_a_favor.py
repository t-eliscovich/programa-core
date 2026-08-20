"""La regla del ESTADO de una factura vive en UN solo lugar.

TMT 2026-08-20. El arreglo del 01/07/2026 —"un saldo a FAVOR nunca queda en
'T', porque las T no se listan y el crédito del cliente desaparece de la
cartera y del estado de cuenta"— se había hecho sólo en
`cheques.aplicar_a_factura`. La resta ya estaba centralizada en `saldo_de`,
pero el estado seguía copiado a mano, con la fórmula vieja `saldo <= 0.01 →
'T'`, en cinco lugares más.

Este archivo hace dos cosas:
1. prueba la regla (`modules/facturas/queries.stat_de`);
2. PROHÍBE que la fórmula vieja vuelva a escribirse a mano, igual que
   `test_retencion_columna_propia` prohíbe `importe − abono`.
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.mark.parametrize("saldo,abono,esperado", [
    (0.0, 0.0, "T"),            # cancelada justa
    (0.004, 0.0, "T"),          # dentro de la tolerancia
    (-0.004, 500.0, "T"),       # idem, del otro lado del cero
    (-42.08, 500.0, "A"),       # 🚨 saldo a FAVOR: viva, no cancelada
    (-0.51, 0.0, "A"),          # crédito chico, pero crédito
    (-32532.35, 51642.95, "A"),  # el caso MWT, medido en producción
    (50.0, 10.0, "A"),          # abonada parcial
    (50.0, 0.0, "Z"),           # emitida, sin abono
    (85.0, 0.0, "Z"),           # sólo una retención encima: sigue en Z
])
def test_la_regla(saldo, abono, esperado):
    from modules.facturas.queries import stat_de
    assert stat_de(saldo, abono) == esperado


def test_el_credito_nunca_totaliza():
    """Ningún saldo negativo fuera de la tolerancia puede dar 'T'."""
    from modules.facturas.queries import stat_de
    for centavos in range(1, 400):
        saldo = -centavos / 100.0
        for tol in (0.005, 0.01, 0.50):
            if abs(saldo) <= tol:
                continue
            assert stat_de(saldo, 0.0, tol=tol) == "A", (saldo, tol)


def test_la_tolerancia_es_del_llamador():
    from modules.facturas.queries import stat_de
    # La cobranza olvida un residuo de hasta $0,50; los reversos, el centavo.
    assert stat_de(0.40, 100.0, tol=0.50) == "T"
    assert stat_de(0.40, 100.0, tol=0.01) == "A"


def test_retenciones_usa_la_misma_regla():
    """`_stat_tras_retencion` delega — no puede divergir en silencio."""
    from modules.facturas.queries import stat_de
    from modules.retenciones.queries import _stat_tras_retencion
    for saldo, abono in [(0, 0), (-10, 5), (10, 5), (10, 0), (-0.001, 0)]:
        assert _stat_tras_retencion(saldo, abono, "") == stat_de(saldo, abono)


# ── el candado ────────────────────────────────────────────────────────────
#: Los que recalculan el estado de una factura al mover abono/saldo.
_ARCHIVOS = [
    "modules/cheques/queries.py",
    "modules/facturas/queries.py",
    "modules/facturas/views.py",
    "modules/informes/queries.py",
    "modules/retenciones/queries.py",
]


def _es_saldo(n) -> bool:
    """¿Este nodo es un saldo? (`saldo`, `nuevo_saldo`, `f["saldo"]`, `saldo_de(…)`)"""
    if isinstance(n, ast.Name):
        return "saldo" in n.id.lower()
    if isinstance(n, ast.Attribute):
        return "saldo" in n.attr.lower()
    if isinstance(n, ast.Subscript):
        s = n.slice
        return (isinstance(s, ast.Constant) and isinstance(s.value, str)
                and "saldo" in s.value.lower())
    if isinstance(n, ast.Call):
        nom = getattr(n.func, "id", None) or getattr(n.func, "attr", "")
        if nom in ("float", "round"):
            return any(_es_saldo(a) for a in n.args)
        return nom == "saldo_de"
    return False


def _llamada_a(n, nombre: str) -> bool:
    return isinstance(n, ast.Call) and (
        (getattr(n.func, "id", None) or getattr(n.func, "attr", "")) == nombre)


def _comparaciones_sospechosas(src: str) -> list[str]:
    """`saldo <= k` con 0 <= k < 1 y sin `abs()`: mete los negativos en la bolsa del cero.

    Se banca que la variable se llame como sea — el candado anterior era una
    expresión regular sobre el texto y se le escapaban justo las dos formas
    más comunes (el ternario y el `elif` de dos renglones). Lo mismo que le
    pasó al candado de `importe − abono` con el reparto del totalizar, que
    escribía `imp - ab`.

    `saldo < -0.005` NO cae acá: comparar contra un número negativo ya es
    explícito sobre el signo. `abs(saldo) <= 0.005`, tampoco.
    """
    malas = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.Lt | ast.LtE):
            continue
        izq, der = node.left, node.comparators[0]
        if _llamada_a(izq, "abs") or not _es_saldo(izq):
            continue
        if not (isinstance(der, ast.Constant)
                and isinstance(der.value, int | float)
                and not isinstance(der.value, bool)):
            continue
        if 0 <= der.value < 1:
            malas.append(f"linea {node.lineno}: {ast.unparse(node)}")
    return malas


@pytest.mark.parametrize("rel", _ARCHIVOS)
def test_nadie_vuelve_a_decidir_el_estado_con_el_saldo_sin_signo(rel):
    ruta = os.path.join(_REPO_ROOT, rel)
    with open(ruta, encoding="utf-8") as fh:
        malas = _comparaciones_sospechosas(fh.read())
    assert not malas, (
        f"{rel}: el saldo se compara contra cero sin mirarle el signo. Un "
        f"saldo a FAVOR quedaría del lado del cancelado y el crédito del "
        f"cliente desaparece de la cartera y del estado de cuenta. Usá "
        f"`stat_de` (o `abs(saldo) <= tol` si es otra cosa). "
        + " | ".join(malas)
    )


def test_el_candado_agarra_las_formas_que_se_le_escapaban_a_la_regex():
    """Las tres formas reales que había en el código, y las legítimas."""
    agarra = [
        'nuevo_stat = "T" if nuevo_saldo <= 0.01 else "A"',
        'if saldo_nuevo <= 0.01:\n    stat = "T"',
        'if x:\n    pass\nelif nuevo_saldo <= 0.01:\n    s = "T"',
        'if f["saldo"] < 0.005:\n    s = "T"',
        'if saldo_de(i, a, r) <= 0.01:\n    s = "T"',
    ]
    pasa = [
        'if abs(saldo) <= 0.005:\n    s = "T"',
        'if saldo_nuevo < -0.005:\n    s = "A"',
        'if saldo > 0.005:\n    s = "Z"',
        'if abono <= 0.01:\n    s = "Z"',
    ]
    for src in agarra:
        assert _comparaciones_sospechosas(src), src
    for src in pasa:
        assert not _comparaciones_sospechosas(src), src
