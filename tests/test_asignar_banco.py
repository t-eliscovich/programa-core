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
    _proponer_movimiento_diferencia,
    _subset_para_target,
    asignar_banco_a_programa,
    reconciliacion_completa,
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


# ── TODO-O-NADA (dueña 2026-07-14): la conciliación es ATÓMICA ──────────
# "deberías dejar TODO pendiente si no se puede matchear, no aprobar la una
# de las partes". La decisión de confirmar se toma ANTES de tocar la DB con
# reconciliacion_completa(); si NO es completa → no se confirma nada.


def test_todo_o_nada_parcial_no_confirma():
    # Selección que matchea PARCIAL (sobra 1 banco) → incompleta → NADA.
    banco = [("b1", 5000.0), ("b2", 4200.0), ("b3", 777.0)]  # b3 no cuadra
    prog = [("p1", 5000.0), ("p2", 4200.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    completa, pcs_sin_banco = reconciliacion_completa(asign, sobrantes, [p for p, _ in prog])
    assert completa is False          # ← el view NO entra al loop de confirmar
    assert sobrantes == ["b3"]
    assert pcs_sin_banco == []


def test_todo_o_nada_pc_sin_contraparte_no_confirma():
    # M > N: un PC sin banco (contraparte) → incompleta → NADA (ni stat-only).
    banco = [("b1", 5000.0)]
    prog = [("p1", 5000.0), ("p2", 3000.0)]  # p2 no tiene banco
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    completa, pcs_sin_banco = reconciliacion_completa(asign, sobrantes, [p for p, _ in prog])
    assert completa is False
    assert sobrantes == []
    assert pcs_sin_banco == ["p2"]    # el mov de programa queda PENDIENTE


def test_todo_o_nada_completa_si_confirma():
    # Selección que cierra COMPLETA (todo banco atado, todo PC con contraparte).
    banco = [("b1", 5000.0), ("b2", 1000.0), ("b3", 3200.0)]
    prog = [("p1", 5000.0), ("p2", 4200.0)]  # p2 = b2+b3
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    completa, pcs_sin_banco = reconciliacion_completa(asign, sobrantes, [p for p, _ in prog])
    assert completa is True
    assert sobrantes == []
    assert pcs_sin_banco == []


def test_todo_o_nada_multi_pc_uno_no_cierra_bloquea_todo():
    # Aunque UN PC cierre perfecto, si el otro no cierra la selección entera
    # queda pendiente (no se aprueba la pata que sí cerraba).
    banco = [("b1", 5000.0), ("b2", 3000.0)]
    prog = [("p1", 5000.0), ("p2", 9999.0)]  # p2 no cuadra con nada
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    completa, pcs_sin_banco = reconciliacion_completa(asign, sobrantes, [p for p, _ in prog])
    assert completa is False
    # b1 cerraría con p1, pero como p2 no cierra, el view no confirma NADA.
    assert "p1" in asign            # la asignación existe...
    assert sobrantes == ["b2"]      # ...pero hay sobrante → incompleta
    assert pcs_sin_banco == ["p2"]


def test_subset_helper_prefiere_mas_chico():
    # 400 = {b_400} tamaño1 lo maneja el llamador; acá probamos que entre
    # tamaño 2 y 3 elija el de tamaño 2.
    avail = [("a", 100.0), ("b", 300.0), ("c", 150.0), ("d", 250.0)]
    # target 400 → {a,b}=400 (tam 2) o {a,c,...}. Debe devolver 2 elementos.
    idxs = _subset_para_target(avail, 400.0, 0.50, 18)
    assert idxs is not None
    assert len(idxs) == 2
    assert abs(sum(avail[i][1] for i in idxs) - 400.0) <= 0.50


# ── AGREGAR MOVIMIENTO POR LA DIFERENCIA (dueña 2026-07-14) ──────────────
# Cuando la selección NO cierra SOLO porque el banco tiene un residuo limpio
# (todo PC matcheó, hay sobrantes, |dif|>tol), se ofrece crear el mov faltante.
# `_proponer_movimiento_diferencia` es PURA: decide si/qué se propone.


def test_diferencia_residuo_credito_propone_NC():
    # (i) Residuo limpio de banco >0.50: banco tiene crédito de más → NC.
    # banco: p1 cierra con b1; b2 (crédito 40) queda sobrante (< $300).
    banco = [("b1", 5000.0), ("b2", 40.0)]
    prog = [("p1", 5000.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    completa, pcs_sin_banco = reconciliacion_completa(asign, sobrantes, [p for p, _ in prog])
    assert completa is False
    prop = _proponer_movimiento_diferencia(banco, prog, sobrantes, pcs_sin_banco)
    assert prop is not None
    assert prop["documento"] == "NC"
    assert prop["sentido"] == "crédito"
    assert prop["importe"] == 40.0
    assert prop["diferencia"] == 40.0
    assert prop["requiere_confirmar"] is False


def test_diferencia_residuo_debito_propone_ND():
    # Banco con débito de más (residuo negativo) → ND.
    banco = [("b1", 5000.0), ("b2", -120.0)]
    prog = [("p1", 5000.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    completa, pcs_sin_banco = reconciliacion_completa(asign, sobrantes, [p for p, _ in prog])
    prop = _proponer_movimiento_diferencia(banco, prog, sobrantes, pcs_sin_banco)
    assert prop is not None
    assert prop["documento"] == "ND"
    assert prop["sentido"] == "débito"
    assert prop["importe"] == 120.0
    assert prop["diferencia"] == -120.0


def test_diferencia_pcs_sin_banco_no_propone():
    # (ii) Si algún PC quedó sin contraparte → NO se propone (no es residuo
    # limpio de banco, es un faltante de programa sin matchear).
    banco = [("b1", 5000.0)]
    prog = [("p1", 5000.0), ("p2", 3000.0)]  # p2 sin banco
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    completa, pcs_sin_banco = reconciliacion_completa(asign, sobrantes, [p for p, _ in prog])
    assert pcs_sin_banco == ["p2"]
    prop = _proponer_movimiento_diferencia(banco, prog, sobrantes, pcs_sin_banco)
    assert prop is None


def test_diferencia_sin_sobrantes_no_propone():
    # (iii) Sin sobrantes de banco → nada que ajustar → None.
    banco = [("b1", 5000.0)]
    prog = [("p1", 5000.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    completa, pcs_sin_banco = reconciliacion_completa(asign, sobrantes, [p for p, _ in prog])
    assert sobrantes == []
    prop = _proponer_movimiento_diferencia(banco, prog, sobrantes, pcs_sin_banco)
    assert prop is None


def test_diferencia_menor_a_tol_no_propone():
    # (iv) |dif|<=0.50 ya lo absorbe la tolerancia → None. Fabricamos un
    # sobrante chico artificial (pasando sobrantes/pcs_sin_banco explícitos).
    banco = [("b1", 5000.0), ("b2", 0.40)]
    prog = [("p1", 5000.0)]
    # b2 no cuadra con nada → sobrante; dif total = 0.40 <= 0.50.
    prop = _proponer_movimiento_diferencia(banco, prog, ["b2"], [])
    assert prop is None


def test_diferencia_mayor_a_300_requiere_confirmar():
    # (v) |dif|>300 → requiere_confirmar True.
    banco = [("b1", 5000.0), ("b2", 500.0)]
    prog = [("p1", 5000.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog)
    completa, pcs_sin_banco = reconciliacion_completa(asign, sobrantes, [p for p, _ in prog])
    prop = _proponer_movimiento_diferencia(banco, prog, sobrantes, pcs_sin_banco)
    assert prop is not None
    assert prop["importe"] == 500.0
    assert prop["requiere_confirmar"] is True
    assert prop["umbral_confirmar"] == 300.0


# ── forzar_asignacion_completa (dueña 2026-07-17: aceptar diferencia) ──
# La vista previa ya advirtió la diferencia; si la dueña confirma igual,
# la asignación se cierra a la fuerza por cercanía de monto (gap asumido).

def test_forzar_1a1_con_diferencia_de_1():
    # El caso reportado: DEPOSITO 16.795,76 vs DE CAJA 16.796,76 (Δ=1.00 > tol).
    from modules.conciliacion.banco_v2_view import forzar_asignacion_completa
    banco = [(0, 16795.76)]
    prog = [("p1", 16796.76)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog, tol=0.50)
    completa, _pcs = reconciliacion_completa(asign, sobrantes, ["p1"])
    assert not completa  # sin aceptar_diferencia sigue fallando (regresión)
    asign2, sobr2 = forzar_asignacion_completa(asign, sobrantes, banco, prog)
    completa2, pcs2 = reconciliacion_completa(asign2, sobr2, ["p1"])
    assert completa2
    assert asign2 == {"p1": [0]} and sobr2 == []


def test_forzar_elige_el_mas_cercano():
    # Dos PC sueltos, dos banco sobrantes → cada uno toma el más cercano.
    from modules.conciliacion.banco_v2_view import forzar_asignacion_completa
    banco = [(0, 100.0), (1, 5000.0)]
    prog = [("chico", 103.0), ("grande", 4990.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog, tol=0.50)
    asign2, sobr2 = forzar_asignacion_completa(asign, sobrantes, banco, prog)
    assert asign2 == {"grande": [1], "chico": [0]}
    assert sobr2 == []


def test_forzar_sobrante_extra_va_al_grupo_mas_cercano():
    # 2 banco → 1 PC: el que cuadra matchea solo; el sobrante se suma al grupo.
    from modules.conciliacion.banco_v2_view import forzar_asignacion_completa
    banco = [(0, 100.0), (1, 7.0)]
    prog = [("p1", 100.0)]
    asign, sobrantes = asignar_banco_a_programa(banco, prog, tol=0.50)
    assert asign == {"p1": [0]} and sobrantes == [1]
    asign2, sobr2 = forzar_asignacion_completa(asign, sobrantes, banco, prog)
    assert asign2 == {"p1": [0, 1]} and sobr2 == []


def test_forzar_no_muta_entrada_y_sin_pcs_no_inventa():
    # Sin ningún PC no hay a quién atar → sobrantes quedan (no inventa nada).
    from modules.conciliacion.banco_v2_view import forzar_asignacion_completa
    asign_in = {}
    asign2, sobr2 = forzar_asignacion_completa(asign_in, [0], [(0, 9.0)], [])
    assert sobr2 == [0] and asign2 == {}
    # Y no muta el dict de entrada cuando sí hay PCs.
    asign_in = {"p1": [0]}
    forzada, _ = forzar_asignacion_completa(asign_in, [1], [(0, 1.0), (1, 2.0)], [("p1", 1.0)])
    assert asign_in == {"p1": [0]} and forzada == {"p1": [0, 1]}
