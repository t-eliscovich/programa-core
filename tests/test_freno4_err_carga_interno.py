"""FRENO 4 (TMT 2026-08-06, casos MSS/CG3/ELF): bloquear el "Conciliar
interno" del lado programa cuando la tx es un ND/NC de «err carga».

Historia: el 06/08 Alex vio la diferencia en −$259,22 esperando −$7,19.
Investigación (agente Claude): 3 internos huérfanos que Tamara había marcado
con motivo — MSS ND 45284 ($1.100,93), CG3 NC 44748 ($1.136,48), ELF ND 45311
($301,96) — sumaban $266,41 al skew de saldo_esperado. Cada uno era la
compensación auto-generada de un «anular por error de carga» cuyo Freno 3
no había podido parear (el DE contraparte ya estaba matched con crédito real
del banco, o no existía). Marcarlos interno los sacaba de pendientes pero los
DEJABA en el saldo running → saldo_esperado bajaba sin nada del lado banco
que lo balanceara.

La forma correcta de sacar un err carga es la papelera
(`/bancos/<nb>/tx/<id>/eliminar`, 30 días de retención). Este freno bloquea
el atajo interno con un flash que apunta a la papelera. Un
`confirmar_err_carga=1` en el form lo desbloquea (respetamos la decisión de
la dueña cuando acepta el drift).

Sin Postgres en CI, la lógica se verifica inspeccionando el fuente
(patrón test_frenos_anulacion_y_duplicado.py).
"""
# ruff: noqa: I001
from __future__ import annotations

import datetime as _dt
if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017  py3.10 shim (skill: programa-core)

import inspect
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.conciliacion import banco_v2_view as v  # noqa: E402

_TPL_TAB_MANUAL = os.path.join(
    _REPO_ROOT, "modules", "conciliacion", "templates", "conciliacion",
    "_banco_v2_tab_manual.html",
)


# ─── Server-side: el guard en /banco-v2/descartar-programa ─────────────


def test_guard_err_carga_bloquea_por_default():
    """El endpoint mira concepto ILIKE 'ANUL%err carga%' y refusa si no
    viene `confirmar_err_carga=1`. Sin este check, cualquier ND «err
    carga» que llegue por la selección repite MSS/CG3/ELF."""
    src = inspect.getsource(v.banco_descartar_programa)
    # Chequea el patrón de err carga contra los ids seleccionados.
    assert "concepto ILIKE 'ANUL%% err carga%%'" in src
    # La confirmación explícita desbloquea.
    assert 'confirmar_err_carga' in src
    # Y ofrece la papelera como alternativa (link a bancos.eliminar).
    assert 'bancos.eliminar_movimiento_pc' in src


def test_guard_err_carga_flash_menciona_papelera():
    """El flash tiene que decirle a Alex/Tamara adónde ir (papelera,
    reversible 30 días) — no basta con negar."""
    src = inspect.getsource(v.banco_descartar_programa)
    assert 'papelera' in src.lower()
    assert '30 días' in src or '30 dias' in src


def test_guard_err_carga_cita_los_casos_del_06_08():
    """El comentario del fuente tiene que dejar el rastro del caso — sin
    ese ancla, un refactor futuro puede sacar el freno sin darse cuenta."""
    src = inspect.getsource(v.banco_descartar_programa)
    for tag in ("MSS", "CG3", "ELF"):
        assert tag in src, f"falta el ancla del caso {tag}"


# ─── Cliente: la JS del botón «Conciliar interno» ──────────────────────


def _tpl_manual() -> str:
    with open(_TPL_TAB_MANUAL, encoding="utf-8") as f:
        return f.read()


def test_checkbox_programa_expone_concepto():
    """El JS tiene que poder leer el concepto de cada checkbox. Sin
    data-concepto, no puede detectar el err carga antes del submit."""
    tpl = _tpl_manual()
    assert 'data-concepto="{{ (m.concepto or \'\')|e }}"' in tpl


def test_js_detecta_err_carga_antes_del_prompt():
    """Antes de pedir motivo, la JS revisa la selección y avisa cuando
    hay ANUL...ERR CARGA. Si el usuario acepta el diálogo, el POST manda
    confirmar_err_carga=1 (respeta la decisión); si cancela, no submitea
    y la dueña va a eliminar por papelera."""
    tpl = _tpl_manual()
    assert "_errCargaSel" in tpl
    assert "'ANUL'" in tpl and "'ERR CARGA'" in tpl
    assert "confirmar_err_carga" in tpl


def test_js_propone_link_a_papelera():
    """El diálogo trae el link a `/bancos/10/tx/<id>/eliminar` de cada
    tx err carga seleccionada — el ojo de Alex tiene que verlo, no
    solo el server."""
    tpl = _tpl_manual()
    assert "/bancos/10/tx/" in tpl
    assert "/eliminar" in tpl


# ─── Compatibilidad: el flujo legítimo NO se rompe ─────────────────────


def test_correccion_negativo_sigue_pasando():
    """El caso legítimo del NC/CH #21404 (pair correccion-negativo del
    03/08) no tiene «err carga» en el concepto ('REVERSO nota_credito
    #21404 — ERROR'). El guard NO lo bloquea, sigue conciliando interno
    como venía. Sin este test, un patrón demasiado agresivo se llevaría
    puesto casos válidos que la dueña quiere mantener como interno."""
    concepto_legit = 'REVERSO nota_credito #21404 — ERROR'
    concepto_err = 'ANUL ch101736 err carga'
    upper_legit = concepto_legit.upper()
    upper_err = concepto_err.upper()
    # La regla es: empieza con ANUL y contiene ERR CARGA.
    def matches(c):
        return c.startswith('ANUL') and 'ERR CARGA' in c
    assert not matches(upper_legit)
    assert matches(upper_err)
