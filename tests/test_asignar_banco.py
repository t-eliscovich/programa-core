"""Tests de la asignación multi-PC (N banco → M programa) por MONTO.

Cubre la función PURA `asignar_banco_a_programa` (y su helper `_subset_para_target`)
que reemplazó el viejo pareo 1:1-por-posición en `banco_manual_confirmar`.

REGLA DE ORO: NUNCA se ata un banco a un PC salvo que la suma firmada cuadre
dentro de |Δ|≤tol. Lo que no cuadra limpio queda como SOBRANTE (pendiente).

banco_v2_view.py NO está en el include de .coveragerc → estos tests no gatean
al 100%, pero blindan la lógica que toca plata.
"""
from __future__ import annotations

from modules.conciliacion.banco_v2_view import (
    _subset_para_target,
    asignar_banco_a_programa,
)


def test_1a1_exacto():
    # Dos banco, dos PC, cada uno cuadra exacto con uno → dos pares 1:1.
    banco = [("b1", 100.0), ("b2", 250.0)]
    prog = [("p1", 100.0), ("p2", 250.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    assert asign == {"p1": ["b1"], "p2": ["b2"]}
    assert sobrantes == []


def test_1a1_con_tolerancia():
    # Δ dentro de 0,50 se considera cuadrado.
    banco = [("b1", 100.30)]
    prog = [("p1", 100.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog, tol=0.50)
    assert asign == {"p1": ["b1"]}
    assert sobrantes == []


def test_n_a_1_deposito_partido():
    # Un depósito que el banco parte en 3 créditos contra 1 mov de programa.
    banco = [("b1", 10000.0), ("b2", 20000.0), ("b3", 13416.43)]
    prog = [("p1", 43416.43)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    assert set(asign["p1"]) == {"b1", "b2", "b3"}
    assert len(asign) == 1
    assert sobrantes == []


def test_multi_pc_parte_limpio():
    # 2 PC + 5 banco: un PC cuadra 1:1, el otro con la suma de 3; sobra 1.
    banco = [
        ("b1", 5000.0),   # → p1 exacto
        ("b2", 1000.0),   # ┐
        ("b3", 2000.0),   # ├ suman 4200 → p2
        ("b4", 1200.0),   # ┘
        ("b5", 777.0),    # no cuadra con nada → sobrante
    ]
    prog = [("p1", 5000.0), ("p2", 4200.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    assert asign["p1"] == ["b1"]
    assert set(asign["p2"]) == {"b2", "b3", "b4"}
    assert sobrantes == ["b5"]


def test_ambiguo_no_cuadra_queda_sobrante():
    # Ningún banco ni suma cuadra con el PC → PC sin asignar, todo sobrante.
    banco = [("b1", 100.0), ("b2", 200.0)]
    prog = [("p1", 999.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    assert asign == {}
    assert set(sobrantes) == {"b1", "b2"}


def test_signos_mixtos_debitos_y_creditos():
    # Débitos (−) y créditos (+) a la vez. Cada PC firmado recibe su lado.
    banco = [
        ("credito", 3000.0),
        ("debito", -1500.0),
        ("credito2", 500.0),   # 3000+500 = 3500 no lo usamos; queda para p_pos
    ]
    prog = [
        ("p_pos", 3500.0),   # 3000 + 500
        ("p_neg", -1500.0),  # el débito
    ]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    assert asign["p_neg"] == ["debito"]
    assert set(asign["p_pos"]) == {"credito", "credito2"}
    assert sobrantes == []


def test_prefiere_1a1_antes_que_grupo():
    # Hay un banco suelto que cuadra exacto Y un grupo que también cuadraría;
    # debe elegir el 1:1 (banco suelto) y no romperlo en un grupo.
    banco = [("suelto", 300.0), ("a", 100.0), ("b", 200.0)]
    prog = [("p1", 300.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    assert asign["p1"] == ["suelto"]
    assert set(sobrantes) == {"a", "b"}


def test_no_reutiliza_banco_entre_pc():
    # Un banco que cuadraría con dos PC solo puede ir a UNO. El PC grande
    # (se sirve primero) se lo queda; el otro queda sin banco.
    banco = [("b1", 500.0)]
    prog = [("p_grande", 500.0), ("p_chico", 500.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    # Solo un PC recibe el banco; ninguno lo comparte.
    total_asignados = sum(len(v) for v in asign.values())
    assert total_asignados == 1
    assert sobrantes == []


def test_greedy_cap_muchos_banco():
    # Con > cap banco disponibles cae a greedy: acumula de mayor a menor
    # hasta cuadrar el target. cap=3 fuerza el camino greedy.
    banco = [(f"b{i}", float(v)) for i, v in enumerate([1000, 900, 800, 700, 600])]
    prog = [("p1", 1900.0)]  # 1000 + 900
    asign, sobrantes = asignar_banco_a_programa(banco, prog, cap=3)
    assert set(asign["p1"]) == {"b0", "b1"}
    assert set(sobrantes) == {"b2", "b3", "b4"}


def test_greedy_overshoot_descarta_pc():
    # Greedy que se pasa del target sin cuadrar → descarta el PC (no inventa).
    banco = [(f"b{i}", float(v)) for i, v in enumerate([1000, 900, 800, 700])]
    prog = [("p1", 1500.0)]  # 1000→2000 overshoot; no hay combo greedy exacto
    asign, sobrantes = asignar_banco_a_programa(banco, prog, cap=3)
    assert asign == {}
    assert len(sobrantes) == 4


def test_subset_helper_devuelve_none_si_no_cuadra():
    avail = [("b1", 100.0), ("b2", 200.0)]
    assert _subset_para_target(avail, 999.0, 0.50, 18) is None


def test_subset_helper_prefiere_mas_chico():
    # 400 = {b_400} tamaño1 lo maneja el llamador; acá probamos que entre
    # tamaño 2 y 3 elija el de tamaño 2.
    avail = [("a", 100.0), ("b", 300.0), ("c", 150.0), ("d", 250.0)]
    # target 400 → {a,b}=400 (tam 2) o {a,c,...}. Debe devolver 2 elementos.
    idxs = _subset_para_target(avail, 400.0, 0.50, 18)
    assert idxs is not None
    assert len(idxs) == 2
    assert abs(sum(avail[i][1] for i in idxs) - 400.0) <= 0.50
