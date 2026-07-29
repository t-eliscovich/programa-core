"""El chequeo de salud tiene que servir: mirar lo que importa y poder correrse.

TMT 2026-07-29. `scripts/check_salud_dia.py` existía desde mayo pero:
  - sus 10 secciones eran invariantes INTERNAS de PC — ninguna comparaba
    contra el dBase ni miraba los bridges;
  - `check_provisiones` miraba UNA de las 12 provisiones (SR) y sólo si se
    había MOVIDO, nunca el monto;
  - su "bonus" leía el marcador de `correr_provisiones_diarias`, motor
    retirado del auto-run el 24/07 → reportaba OK sobre algo muerto;
  - y no lo corría NADIE: script suelto, sin cron ni pantalla.

Por eso no vio ninguno de los dos problemas del 29/07 (la cuota de A,E,C
cambiada a mano en el FoxPro, y la utilidad ~495.000 abajo por el fallback
silencioso de Asinfo).
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_salud_dia.py"


@pytest.fixture(scope="module")
def chk():
    spec = importlib.util.spec_from_file_location("check_salud_dia_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_existe_el_check_de_bridges(chk):
    """Sin esto, nadie vigila de dónde sale la utilidad."""
    assert "bridges" in chk.ALL_CHECKS, (
        "falta la sección BRIDGES: es la única que detecta que el balance esté "
        "valuando el stock con los kg del dBase porque Asinfo no contestó"
    )
    src = inspect.getsource(chk.check_bridges)
    assert "inventario_por_etapa" in src, (
        "BRIDGES tiene que preguntarle al inventario por etapa, no sólo si "
        "Metabase está configurado — el fallback pasa con Metabase configurado"
    )


def test_provisiones_compara_las_12_cuotas_contra_el_prg(chk):
    src = inspect.getsource(chk._cuotas_vs_prg)
    assert "cuotas_del_prg" in src and "cuotas_de_pc" in src, (
        "las cuotas se comparan contra el MENU.PRG (la fuente), no contra otra "
        "constante de PC — comparar PC contra PC no detecta que el dBase cambió"
    )


def _solo_codigo(src: str) -> str:
    """Devuelve el cuerpo de la función SIN comentarios ni docstring.

    El test mira el CÓDIGO. Los comentarios y docstrings sí nombran a
    `fecha_modifica` y `correr_provisiones_diarias` — justamente para explicar
    por qué se dejaron de usar — y no tienen que hacer fallar el assert.
    """
    import ast
    import textwrap

    mod = ast.parse(textwrap.dedent(src))
    fn = mod.body[0]
    cuerpo = fn.body
    if (cuerpo and isinstance(cuerpo[0], ast.Expr)
            and isinstance(getattr(cuerpo[0], "value", None), ast.Constant)
            and isinstance(cuerpo[0].value.value, str)):
        cuerpo = cuerpo[1:]
    return "\n".join(ast.unparse(n) for n in cuerpo)




def test_no_reporta_ok_sobre_el_motor_muerto(chk):
    """`correr_provisiones_diarias` salió del auto-run el 24/07: su marcador
    no puede seguir dando un OK tranquilizador."""
    src = _solo_codigo(inspect.getsource(chk.check_provisiones))
    assert "provisiones_diarias_ult_fecha" not in src, (
        "sigue leyendo el marcador de un motor que ya no corre"
    )


def test_liveness_mira_baseline_date_y_no_fecha_modifica(chk):
    """El motor vivo (`persistir_acumulacion_yy`) escribe importe+baseline_date
    y NUNCA fecha_modifica. Mirar la columna equivocada daba un ERROR rojo
    permanente y falso ("SRI sin actualizar hace 55 días")."""
    src = _solo_codigo(inspect.getsource(chk.check_provisiones))
    assert "baseline_date" in src, "el liveness tiene que mirar baseline_date"
    assert "fecha_modifica" not in src, (
        "sigue mirando fecha_modifica: esa columna no la toca el motor vivo, "
        "así que el check grita en falso"
    )
    assert "correr_provisiones_diarias" not in src, (
        "sigue culpando al motor retirado del auto-run el 24/07"
    )


def test_el_chequeo_es_solo_lectura(chk):
    src = _SCRIPT.read_text(encoding="utf-8")
    for kw in ("INSERT INTO", "UPDATE scintela", "DELETE FROM", "db.tx("):
        assert kw not in src, f"el chequeo de salud escribe: {kw}"


def test_hay_pantalla_para_correrlo():
    """Un health check que nadie corre no sirve. Tiene que estar en la UI."""
    from modules.admin_dbase import salud_view

    assert salud_view.bp.url_prefix == "/admin/salud"
    src = inspect.getsource(salud_view)
    for kw in ("INSERT INTO", "UPDATE scintela", "DELETE FROM", "db.tx("):
        assert kw not in src, f"/admin/salud escribe: {kw}"
    assert "requiere_permiso" in src, "la pantalla tiene que estar gateada"


def test_la_pantalla_esta_registrada_en_la_app(app):
    rutas = {r.rule for r in app.url_map.iter_rules()}
    assert "/admin/salud/" in rutas, "el blueprint de salud no quedó registrado"
    assert "/admin/salud/run" in rutas
