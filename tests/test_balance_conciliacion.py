"""Tests del CONTRATO de la conciliación del balance.

Estos tests son la garantía de que `BALANCE_CONCEPTS` y
`conciliacion_balance()` no se desincronizan: si alguien agrega o saca
un componente del balance sin tocar la conciliación, CI bloquea el
merge.

Ver `docs/CONCILIACION_CONTRACT.md` para detalle.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

# Asegurar que la raíz del proyecto está en sys.path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Forzamos ENV=development antes de importar para que los self-check
# levanten AssertionError (más fuerte que warning de log).
os.environ["ENV"] = "development"


# ---------- FakeDB local: routea SQL → respuestas conocidas. ----------

class _FakeBalanceDB:
    """DB stub que reconoce las queries de informe_balance() y devuelve
    respuestas válidas. Los valores son arbitrarios pero consistentes
    (sumas que cuadran) — los tests miden estructura, no aritmética."""

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()

        # totf() — facturas vivas
        if "from scintela.factura" in s and "stat in" in s and "saldo" in s and "from scintela.factura f" not in s:
            return {"total": 1000.0, "saldo": 1000.0, "n": 5, "importe": 1500.0}

        # facturas conteos por bucket — hay varias variantes en conciliacion
        if "from scintela.factura" in s and "stat = 't'" in s:
            return {"n": 2, "importe": 200.0}
        if "from scintela.factura" in s and "stat in ('x','y')" in s:
            return {"n": 1, "importe": 50.0}
        if "from scintela.factura" in s and ("stat not in ('x','y')" in s or "stat is null" in s):
            return {"n": 100, "importe": 50000.0}

        # totc() — cheques en cartera
        if "from scintela.cheque" in s and "stat in" in s and "group by" not in s:
            return {"total": 2000.0}

        # caja
        if "from scintela.caja" in s and "saldo" in s and "count" not in s:
            return {"saldo": 100.0}
        if "from scintela.caja" in s and "count" in s:
            return {"n": 1240}

        # posdat_totales
        if "from scintela.posdat" in s and "pos1" in s:
            return {"pos1": 50.0, "pos2": 30.0, "totp": 200.0}
        # posdat module-style: solo abiertas con importe>0
        if "from scintela.posdat" in s and "<> 9" in s and "importe" in s and "<= 0" not in s:
            return {"n": 87, "total": 195.0}
        # posdat con importe<=0
        if "from scintela.posdat" in s and "<> 9" in s and "<= 0" in s:
            return {"n": 3, "total": 5.0}
        # posdat pagadas
        if "from scintela.posdat" in s and "= 9" in s:
            return {"n": 540, "total": 5800.0}

        # activos
        if "from scintela.activos" in s and ("'mck'" in s or "in ('m','c','k')" in s):
            return {"umaq": 998.0, "uact": 2422.0}
        if "from scintela.activos" in s and "valor" in s and "count" in s:
            return {"n": 12}

        # dolares (anticipos sin breakdown)
        if "from scintela.dolares" in s and "st is null" in s:
            return {"total": 1139.0}

        # retiros
        if "from scintela.retiros" in s and "63 days" in s:
            return {"total": 159.0}
        if "from scintela.retiros" in s and "year" in s and "current_date" in s:
            return {"n": 8, "total": 1200.0}
        if "from scintela.retiros" in s and "from scintela.retiros" in s and "where" not in s:
            return {"n": 100, "total": 50000.0}
        if "from scintela.retiros" in s:
            return {"n": 100, "total": 50000.0}

        # historia
        if "from scintela.historia" in s:
            return {
                "id_historia": 1, "fecha": date(2026, 4, 10),
                "ustock": 8076.0, "uqui": 300.0, "patrimonio": 19000.0, "usuti": 500.0,
                "kvent": 100000, "uvent": 800000, "kcom": 50000, "ucom": 200000,
                "ktej": 90000, "utej": 150000, "ktin": 80000, "utin": 250000,
                "gasto": 200000, "gstotal": 1000000, "costo": 1000000,
                "kp": 0, "ktint": 0, "kr": 0, "kv": 0,
                "hilado": 0, "tejido": 0, "terminado": 0,
            }

        # iniciales
        if "from scintela.iniciales" in s:
            return {
                "mesnum": 4, "mesnom": "abril", "yy": 2026,
                "kprog": 250000, "pretej": 180000, "pretin": 200000,
                "preadm": 300000, "pretot": 1000000,
                "hilado": 1000, "tejido": 500, "terminado": 300,
                "um": 2.0, "uk": 3.0, "uf": 4.0, "uq": 0,
            }

        # venta_anual
        if "uvent_anual" in s.lower() or "from scintela.historia" in s and "limit 12" in s:
            return {"uvent_anual": 9000000.0, "kvent_anual": 1100000.0}

        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()

        # cheques_por_stat
        if "from scintela.cheque" in s and "group by stat" in s:
            return [
                {"stat": "Z", "n": 10, "total": 1000.0},
                {"stat": "1", "n": 2, "total": 100.0},
                {"stat": "2", "n": 1, "total": 50.0},
                {"stat": "3", "n": 0, "total": 0.0},
                {"stat": "P", "n": 5, "total": 600.0},
                {"stat": "D", "n": 3, "total": 250.0},
                {"stat": "B", "n": 50, "total": 1500.0},
                {"stat": "A", "n": 5, "total": 100.0},
                {"stat": "R", "n": 2, "total": 45.0},
            ]

        # saldo_bancos
        if "from scintela.banco b" in s:
            return [
                {"no_banco": 1, "nombre": "PICHINCHA",
                 "saldo_stored": 1656.0, "saldo_signed": 0.0, "saldo_raw": 0.0,
                 "n_transacciones": 320},
                {"no_banco": 2, "nombre": "INTERNACIONAL",
                 "saldo_stored": 3.7, "saldo_signed": 0.0, "saldo_raw": 0.0,
                 "n_transacciones": 140},
            ]

        # dolares por st (breakdown)
        if "from scintela.dolares" in s and "group by" in s:
            return [
                {"st": "(vivo)", "n": 5, "total": 1139.0},
                {"st": "X",     "n": 2, "total": 200.0},
            ]

        # activos por tipo (breakdown)
        if "from scintela.activos" in s and "group by" in s:
            return [
                {"tipo": "I", "n": 5, "total": 2422.0},
                {"tipo": "M", "n": 4, "total": 800.0},
                {"tipo": "C", "n": 2, "total": 150.0},
                {"tipo": "K", "n": 1, "total": 48.0},
            ]

        return []

    def execute(self, sql, params=None, conn=None):
        return 0

    def execute_returning(self, sql, params=None, conn=None):
        return None


@pytest.fixture
def fake_balance_db(monkeypatch):
    import db as db_module
    fake = _FakeBalanceDB()
    monkeypatch.setattr(db_module, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(db_module, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(db_module, "execute", fake.execute)
    monkeypatch.setattr(db_module, "execute_returning", fake.execute_returning)
    return fake


# ---------- TESTS DEL CONTRATO ----------


def test_balance_concepts_es_inmutable_y_completo():
    """`BALANCE_CONCEPTS` debe contener exactamente los 9 conceptos
    canonical, en orden, sin duplicados. Si alguien lo cambia, este
    test cambia también — pero al menos lo van a leer."""
    from modules.informes.queries import BALANCE_CONCEPTS

    assert isinstance(BALANCE_CONCEPTS, tuple), \
        "BALANCE_CONCEPTS debe ser tuple (inmutable a runtime)"
    assert len(BALANCE_CONCEPTS) == len(set(BALANCE_CONCEPTS)), \
        "BALANCE_CONCEPTS tiene duplicados — cada concepto debe aparecer una sola vez"
    # Estos son los 9 que existen al cierre de batch 20. Si agregás uno,
    # actualizá este test también.
    esperados = (
        "CAJA",
        "BANCOS",
        "CHEQUES (TOTC)",
        "FACTURAS (TOTF)",
        "ANTICIPOS",
        "MAQ/EQUIP. + TERR/EDIF/INS.",
        "STOCK MP+PROD. + STOCK QUI. + UTILIDAD",
        "PASIVOS (TOTP)",
        "DIVID. (URET)",
    )
    assert esperados == BALANCE_CONCEPTS, (
        "BALANCE_CONCEPTS cambió. Si agregaste/sacaste un componente "
        "del balance, actualizá también este test, conciliacion_balance(), "
        "informe_balance() y balance.html. Ver docs/CONCILIACION_CONTRACT.md."
    )


def test_conciliacion_emite_exactamente_los_conceptos_de_balance(fake_balance_db):
    """`conciliacion_balance()` debe emitir una fila por concepto en el
    orden exacto de `BALANCE_CONCEPTS`. Si no, romper merge."""
    from modules.informes.queries import BALANCE_CONCEPTS, conciliacion_balance

    rows = conciliacion_balance()
    conceptos_emitidos = tuple(r["concepto"] for r in rows)
    assert conceptos_emitidos == BALANCE_CONCEPTS, (
        f"conciliacion_balance() emitió {conceptos_emitidos}\n"
        f"pero BALANCE_CONCEPTS dice {BALANCE_CONCEPTS}.\n"
        "Si agregaste/sacaste un componente del balance, actualizá AMBAS estructuras. "
        "Ver docs/CONCILIACION_CONTRACT.md."
    )


def test_cada_fila_tiene_llaves_requeridas(fake_balance_db):
    """Cada fila debe tener concepto/balance/modulo/match/diff/detalle/nota."""
    from modules.informes.queries import CONCILIACION_REQUIRED_KEYS, conciliacion_balance

    for fila in conciliacion_balance():
        faltantes = CONCILIACION_REQUIRED_KEYS - set(fila.keys())
        assert not faltantes, (
            f"Fila '{fila.get('concepto')}' falta llaves: {faltantes}. "
            f"Llaves requeridas: {sorted(CONCILIACION_REQUIRED_KEYS)}"
        )


def test_balance_y_modulo_son_numericos(fake_balance_db):
    """`balance` y `modulo` siempre deben ser numéricos (no None ni str)."""
    from modules.informes.queries import conciliacion_balance

    for fila in conciliacion_balance():
        for k in ("balance", "modulo", "diff"):
            v = fila[k]
            assert isinstance(v, int | float), (
                f"Fila '{fila['concepto']}' tiene {k}={v!r} ({type(v).__name__}); "
                "debe ser numérico"
            )


def test_match_es_bool_y_consistente_con_diff(fake_balance_db):
    """`match` debe ser bool. Si es False, |diff| > 0.5; si es True, |diff| ≤ 0.5."""
    from modules.informes.queries import conciliacion_balance

    for fila in conciliacion_balance():
        match = fila["match"]
        diff = abs(float(fila["diff"]))
        assert isinstance(match, bool), \
            f"Fila '{fila['concepto']}' tiene match={match!r}; debe ser bool"
        if match:
            assert diff <= 0.5, (
                f"Fila '{fila['concepto']}' dice match=True pero diff={diff:.2f}; "
                "tolerancia es 0.50"
            )


def test_detalle_es_lista_de_tuplas(fake_balance_db):
    """`detalle` debe ser una lista de tuplas (label, valor)."""
    from modules.informes.queries import conciliacion_balance

    for fila in conciliacion_balance():
        det = fila["detalle"]
        assert isinstance(det, list), \
            f"Fila '{fila['concepto']}' tiene detalle no-lista: {type(det).__name__}"
        for i, item in enumerate(det):
            assert isinstance(item, tuple) and len(item) == 2, (
                f"Fila '{fila['concepto']}' detalle[{i}] no es tupla (label, valor): {item!r}"
            )
            label, _valor = item
            assert isinstance(label, str) and label.strip(), \
                f"Fila '{fila['concepto']}' detalle[{i}] label vacío o no-str"


def test_nota_documenta_origen_o_diferencia(fake_balance_db):
    """Cada fila debe tener una `nota` no-vacía. Es lo que el gerente lee
    cuando quiere entender de dónde sale el número."""
    from modules.informes.queries import conciliacion_balance

    for fila in conciliacion_balance():
        nota = fila["nota"]
        assert isinstance(nota, str) and len(nota.strip()) >= 20, (
            f"Fila '{fila['concepto']}' tiene nota corta o vacía: {nota!r}. "
            "Cada fila debe explicar de dónde sale (cita PRG, fórmula, filtro)."
        )


def test_facturas_balance_y_modulo_cuadran_por_construccion(fake_balance_db):
    """FACTURAS (TOTF) usa el MISMO filtro en balance y módulo
    (saldo>0 AND stat IN Z/A/null). Por construcción siempre cuadran.
    Si este test falla, el filtro divergió en alguno de los dos lados."""
    from modules.informes.queries import conciliacion_balance

    facturas = next(
        r for r in conciliacion_balance() if r["concepto"] == "FACTURAS (TOTF)"
    )
    assert facturas["match"], (
        f"TOTF del balance ({facturas['balance']}) no coincide con módulo "
        f"({facturas['modulo']}). El filtro divergió. Revisar totf() en queries.py "
        "vs el WHERE en /facturas?vista=cartera."
    )


def test_cheques_balance_es_suma_de_stats_que_entran_a_totc(fake_balance_db):
    """TOTC = Z + 1 + 2 + 3 + P + D (PRG línea 24). El balance y módulo
    deben coincidir cuando suman los mismos stats."""
    from modules.informes.queries import conciliacion_balance

    cheques = next(
        r for r in conciliacion_balance() if r["concepto"] == "CHEQUES (TOTC)"
    )
    assert cheques["match"], (
        f"TOTC del balance ({cheques['balance']}) no coincide con suma de stats "
        f"que entran a TOTC ({cheques['modulo']}). Verificar totc() y el filtro "
        "STAT IN ('Z','1','2','3','P','D')."
    )


def test_bancos_cuadra_por_construccion(fake_balance_db):
    """BANCOS = SALBANC1 + SALBANC2 = (saldos bancos) + POS1 + POS2.
    Por construcción cuadra siempre — si no, hay bug en saldo_bancos()
    o posdat_totales()."""
    from modules.informes.queries import conciliacion_balance

    bancos = next(
        r for r in conciliacion_balance() if r["concepto"] == "BANCOS"
    )
    assert bancos["match"], (
        f"BANCOS del balance no cuadra con su detalle. "
        f"balance={bancos['balance']} vs modulo={bancos['modulo']}. Bug serio."
    )


def test_self_check_detecta_drift(fake_balance_db, monkeypatch):
    """Si BALANCE_CONCEPTS y los conceptos emitidos por la función divergen,
    el self-check interno debe levantar AssertionError. Forzamos el drift
    pisando BALANCE_CONCEPTS con una tupla con un concepto extra que la
    función no emite, y esperamos la excepción."""
    import modules.informes.queries as q

    fantasma = q.BALANCE_CONCEPTS + ("CONCEPTO_FANTASMA",)
    monkeypatch.setattr(q, "BALANCE_CONCEPTS", fantasma)

    with pytest.raises(AssertionError, match="CONCILIACION_CONTRACT"):
        q.conciliacion_balance()


def test_self_check_detecta_falta_de_llave(fake_balance_db, monkeypatch):
    """Si una fila no tiene todas las llaves requeridas, el self-check
    también debe romper."""
    import modules.informes.queries as q

    # Forzamos que la función emita una fila sin "nota" (llave requerida)
    original = q.conciliacion_balance

    def emitir_sin_nota():
        # Llamamos al original a través de un by-pass del self-check:
        # corremos paso a paso sin usar el wrapper. Más simple: usamos
        # monkey-patch en CONCILIACION_REQUIRED_KEYS para agregar una
        # llave que ninguna fila tiene.
        return original()

    monkeypatch.setattr(
        q, "CONCILIACION_REQUIRED_KEYS",
        q.CONCILIACION_REQUIRED_KEYS | frozenset(["llave_que_no_existe"])
    )

    with pytest.raises(AssertionError, match="falta llaves"):
        q.conciliacion_balance()


def test_bancos_no_se_pierde_si_no_banco_no_es_1_o_2(monkeypatch):
    """Regresión 2026-04-30: si los IDs de banco en la DB no coinciden con
    el legacy mapping (Pichincha=1, Internacional=2), salbanc no puede
    salir 0. El total tiene que sumar TODOS los bancos, sea cual sea
    el no_banco."""
    import db as db_module
    fake = _FakeBalanceDB()
    monkeypatch.setattr(db_module, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(db_module, "fetch_all", fake.fetch_all)

    # Patch saldo_bancos directamente para simular Pichincha con no_banco=3
    # (no en el legacy mapping). pos1/pos2 quedan en 0.
    import modules.informes.queries as q
    monkeypatch.setattr(q, "saldo_bancos", lambda: [
        {"no_banco": 3, "nombre": "PICHINCHA",
         "saldo": 1_839_438.25, "saldo_origen": "stored",
         "saldo_stored": 1_839_438.25, "saldo_signed": 0.0, "saldo_raw": 0.0,
         "n_transacciones": 552},
    ])
    monkeypatch.setattr(q, "posdat_totales", lambda: {"pos1": 0.0, "pos2": 0.0, "totp": 1_606_561.86})

    b = q.informe_balance()
    assert abs(b["salbanc"] - 1_839_438.25) < 1.0, (
        f"salbanc={b['salbanc']} cuando Pichincha tiene no_banco=3 (saldo 1.84M). "
        "Si esto da 0, el cálculo está hardcodeando no_banco=1/2 otra vez. "
        "salbanc DEBE ser SUM(todos los bancos) + pos1 + pos2."
    )


def test_math_check_pasa_con_data_consistente(fake_balance_db):
    """`_verificar_balance_math()` no debe reportar errores cuando los
    números están internamente consistentes."""
    from modules.informes.queries import _verificar_balance_math, informe_balance

    b = informe_balance()
    errores = _verificar_balance_math(b)
    assert errores == [], (
        "El balance generado por informe_balance() viola alguna invariante "
        "matemática:\n  - " + "\n  - ".join(errores)
    )


def test_math_check_detecta_subt_corrupto(fake_balance_db):
    """Si SUBT no es CAJA+BANCOS+CHEQUES+FACTURAS, el check rompe."""
    from modules.informes.queries import _verificar_balance_math, informe_balance

    b = informe_balance()
    b["subt"] = b["subt"] + 1000   # corromper a propósito
    errores = _verificar_balance_math(b)
    assert any("SUBT" in e for e in errores), (
        f"Debió detectar SUBT corrupto, pero los errores fueron: {errores}"
    )


def test_math_check_detecta_totl_corrupto(fake_balance_db):
    """Si TOTL no es SUBT+VSTO+VQX+UMAQ+UACT+URET+ANTIC, romper."""
    from modules.informes.queries import _verificar_balance_math, informe_balance

    b = informe_balance()
    b["totl"] = b["totl"] - 500
    errores = _verificar_balance_math(b)
    assert any("TOTL" in e for e in errores), (
        f"Debió detectar TOTL corrupto: {errores}"
    )


def test_math_check_detecta_patr_corrupto(fake_balance_db):
    """Si PATR ≠ TOTL−TOTP, romper."""
    from modules.informes.queries import _verificar_balance_math, informe_balance

    b = informe_balance()
    b["patr"] = b["patr"] + 999
    errores = _verificar_balance_math(b)
    assert any("PATR" in e for e in errores), (
        f"Debió detectar PATR corrupto: {errores}"
    )


def test_math_check_detecta_salbanc_no_cuadra_con_bancos(fake_balance_db):
    """Si SALBANC ≠ SUM(saldos bancos) + POS1 + POS2, romper."""
    from modules.informes.queries import _verificar_balance_math, informe_balance

    b = informe_balance()
    b["salbanc"] = b["salbanc"] + 1234567   # forzar drift
    errores = _verificar_balance_math(b)
    assert any("SALBANC" in e for e in errores), (
        f"Debió detectar SALBANC drift: {errores}"
    )


def test_informe_balance_rompe_en_dev_con_math_corrupta(fake_balance_db, monkeypatch):
    """ENV=development → si las cuentas no cuadran, AssertionError. CI lo agarra."""
    import modules.informes.queries as q

    # Forzar que `subt` calculado en informe_balance() sea inconsistente
    # pisando totf() y totc() después de que cart se compute.
    # En la práctica nunca debería pasar, pero el test garantiza que
    # si pasa, el balance NO se renderiza y el desarrollador se entera.
    original_verificar = q._verificar_balance_math

    def verificar_que_falla(b):
        errores = original_verificar(b)
        # Forzar al menos un error para validar que la assertion sale.
        return errores + ["forzado: invariante de prueba"]

    monkeypatch.setenv("ENV", "development")
    monkeypatch.setattr(q, "_verificar_balance_math", verificar_que_falla)

    with pytest.raises(AssertionError, match="Invariantes del balance"):
        q.informe_balance()


def test_informe_balance_no_rompe_en_prod_con_math_corrupta(fake_balance_db, monkeypatch):
    """ENV=production → no rompe la página, pero anexa el error al banner."""
    import modules.informes.queries as q

    original_verificar = q._verificar_balance_math

    def verificar_que_falla(b):
        return original_verificar(b) + ["forzado: invariante de prueba"]

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setattr(q, "_verificar_balance_math", verificar_que_falla)

    b = q.informe_balance()    # NO debe raisear
    advertencias = b["diagnostico"]["advertencias"]
    assert any("invariante de prueba" in a for a in advertencias), (
        f"En prod, los errores de math deberían anexarse a advertencias, pero salieron: {advertencias}"
    )


def test_informe_balance_incluye_conciliacion(fake_balance_db):
    """`informe_balance()` debe incluir `conciliacion` en el dict que devuelve.
    Si alguien remueve esa línea, el balance.html no muestra el panel y
    el contrato se rompe silenciosamente."""
    from modules.informes.queries import BALANCE_CONCEPTS, informe_balance

    b = informe_balance()
    assert "conciliacion" in b, (
        "informe_balance() no devolvió 'conciliacion' en el dict. "
        "El template no va a mostrar el panel."
    )
    assert isinstance(b["conciliacion"], list), \
        f"b['conciliacion'] no es lista: {type(b['conciliacion']).__name__}"
    assert len(b["conciliacion"]) == len(BALANCE_CONCEPTS), (
        f"informe_balance().conciliacion tiene {len(b['conciliacion'])} filas "
        f"pero BALANCE_CONCEPTS tiene {len(BALANCE_CONCEPTS)}."
    )
