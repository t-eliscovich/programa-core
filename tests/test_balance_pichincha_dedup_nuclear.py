"""Anti-regression tests para balance_pichincha.calcular() — dedup nuclear.

Caso de origen (sesión #35, 2026-06-03):
  saldo libros banco       = $2.374.620,96
  pendientes históricos    = $69.895,71  (neto)
  saldo_banco_esperado     = $2.444.516,67  ← target correcto

Bug: el extracto crudo del banco (mov-02-06.xlsx) cargado en la sesión
trae ~$589.673,66 en débitos que YA ESTÁN en `transacciones_bancarias`
(porque PC contabilizó los pagos SENAE, transferencias, etc. el mismo
día). Sin dedup nuclear, esos débitos se contaban como "pendiente_banco"
DOBLE — una vez en `transacciones_bancarias`, otra en el payload de la
sesión — y el saldo_banco_esperado caía a $1.937.208,95 (incorrecto).

Estos tests fijan el contrato:
  1) Extracto cuyas filas existen en `transacciones_bancarias` → balance
     vuelve a $2.444.516,67 (±$1).
  2) Re-subir el MISMO extracto NO infla el balance.
  3) Smoke test: balance nunca drift >$100 sin causa explicable.

Mockeamos `db.fetch_one` / `db.fetch_all` para no tocar RDS.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─── Helpers ──────────────────────────────────────────────────────────


# Caso #35: parámetros de partida (verdad)
SALDO_LIBROS = 2_374_620.96
NETO_HISTOS = 69_895.71
TARGET_ESPERADO = 2_444_516.67  # = SALDO_LIBROS + NETO_HISTOS (sin pendientes PC)


def _extracto_sesion_35_mov_02_06() -> list[dict]:
    """Simula el extracto mov-02-06...xlsx con débitos que YA están en
    `transacciones_bancarias`.

    Total débitos = $589.673,66 (4 movs típicos del extracto real:
    pago SENAE, transferencia grande, ND interbancaria, otra ND).
    """
    return [
        {"fecha": "2026-06-02", "documento": "38078012", "monto": 89356.41, "tipo": "D"},
        {"fecha": "2026-06-02", "documento": "38078013", "monto": 250000.00, "tipo": "D"},
        {"fecha": "2026-06-02", "documento": "38078014", "monto": 200000.00, "tipo": "D"},
        {"fecha": "2026-06-02", "documento": "38078015", "monto": 50317.25, "tipo": "D"},
    ]


def _tx_bancarias_rows_que_duplican(extracto: list[dict]) -> list[dict]:
    """Para cada fila del extracto, fabrica una fila simétrica en
    `transacciones_bancarias` (mismo fecha + monto, documento 'CH').

    Esto modela el caso real: PC contabilizó los pagos el mismo día y
    cuando el extracto del banco llega, esas filas YA están en la DB.
    """
    rows = []
    for m in extracto:
        rows.append(
            {
                "fecha": date.fromisoformat(m["fecha"]),
                "documento": "CH",  # tipo D inferido de doc
                "importe": m["monto"],
            }
        )
    return rows


def _make_fake_db(*, extracto_rows: list[dict], tx_rows: list[dict],
                  saldo_libros: float = SALDO_LIBROS,
                  neto_histos: float = NETO_HISTOS):
    """Construye un mock de `db` con fetch_one/fetch_all que devuelven
    respuestas coherentes con el caso de testing.

    Para evitar regex SQL, distinguimos por palabras clave en la query.
    """
    class _FakeDB:
        def fetch_one(self, sql, params=None):
            s = " ".join(sql.split()).lower()
            # Saldo PC actual (1er query)
            if "t.fecha, t.saldo, t.id_transaccion" in s:
                return {
                    "saldo": saldo_libros,
                    "fecha": date(2026, 6, 2),
                    "id_transaccion": 99999,
                }
            # Pendientes históricos banco
            if "banco_historicos_pendientes" in s and "neto_pend" in s:
                return {
                    "neto_pend": neto_histos,
                    "sum_cred": neto_histos if neto_histos > 0 else 0,
                    "sum_deb": 0 if neto_histos > 0 else abs(neto_histos),
                    "n_cred": 1 if neto_histos > 0 else 0,
                    "n_deb": 0 if neto_histos > 0 else 1,
                    "n_pend": 1,
                }
            # Pendientes PC (a conciliar)
            if "transacciones_bancarias" in s and "count(*) as n" in s and "stat" in s:
                return {
                    "n": 0,
                    "signed": 0.0,
                    "sum_cred": 0.0,
                    "sum_deb": 0.0,
                    "n_cred": 0,
                    "n_deb": 0,
                }
            # Sesión abierta — extracto payload
            if "banco_conciliacion_sesion" in s and "extracto_payload" in s:
                return {"id": 35, "extracto_payload": extracto_rows}
            return None

        def fetch_all(self, sql, params=None):
            s = " ".join(sql.split()).lower()
            # Matches activos (vacío — el bug es justo que se borraron)
            if "banco_conciliacion_match" in s and "real_fecha" in s:
                return []
            # Histos pendientes para dedup_firmas (mismo de arriba pero
            # como rows individuales) — devolvemos 1 fila
            if "banco_historicos_pendientes" in s and "fecha, documento, monto, tipo" in s:
                return [
                    {
                        "fecha": date(2023, 2, 15),
                        "documento": "OLD",
                        "monto": neto_histos,
                        "tipo": "C",
                    }
                ]
            # Transacciones bancarias para tx_firmas (dedup nuclear)
            if "transacciones_bancarias" in s and "t.fecha, t.documento, t.importe" in s:
                return tx_rows
            # pendientes_conciliar_rows (preview en UI)
            if "transacciones_bancarias" in s and "id_transaccion" in s and "importe_signed" in s:
                return []
            return []

    return _FakeDB()


# ─── Test 1: balance vuelve a target con dedup nuclear ───────────────


def test_balance_target_con_dedup_nuclear():
    """Extracto con 4 débitos que YA están en transacciones_bancarias.
    Con el dedup nuclear, todos se ignoran como "pendientes" y el
    saldo_banco_esperado debe quedar igual a saldo_libros + neto_histos.
    """
    from modules.conciliacion import balance_pichincha as bp

    extracto = _extracto_sesion_35_mov_02_06()
    tx_rows = _tx_bancarias_rows_que_duplican(extracto)
    fake = _make_fake_db(extracto_rows=extracto, tx_rows=tx_rows)

    # balance_pichincha hace `import db as _db` adentro de calcular(),
    # así que parcheamos sys.modules['db'] directamente.
    with patch.dict(sys.modules, {"db": fake}):
        resultado = bp.calcular()

    assert resultado, "calcular() devolvió dict vacío — error interno"
    assert "saldo_banco_esperado" in resultado
    esperado = resultado["saldo_banco_esperado"]
    delta = abs(esperado - TARGET_ESPERADO)
    assert delta <= 1.0, (
        f"saldo_banco_esperado = {esperado:.2f}, target = {TARGET_ESPERADO:.2f}, "
        f"delta = {delta:.2f} > 1.0. El dedup nuclear no está filtrando bien."
    )
    # Y los movs del extracto deben haber sido contados como dedup_vs_tx
    assert resultado.get("dedup_vs_tx", 0) == len(extracto), (
        f"Esperaba {len(extracto)} filas dedupeadas vs tx, "
        f"got {resultado.get('dedup_vs_tx')}"
    )


# ─── Test 2: duplicar el upload NO infla el balance ──────────────────


def test_re_subir_mismo_extracto_no_infla_balance():
    """Re-subir el mismo extracto (o duplicar las filas en el payload)
    debe seguir devolviendo el mismo balance — todas las filas se
    dedupean contra tx + las copias entre sí no inflan porque ambas
    matchean la misma firma.
    """
    from modules.conciliacion import balance_pichincha as bp

    extracto_simple = _extracto_sesion_35_mov_02_06()
    # Duplicamos cada fila — simula "re-upload" o doble parse del mismo xlsx
    extracto_duplicado = extracto_simple + extracto_simple
    tx_rows = _tx_bancarias_rows_que_duplican(extracto_simple)

    fake_simple = _make_fake_db(extracto_rows=extracto_simple, tx_rows=tx_rows)
    fake_doble = _make_fake_db(extracto_rows=extracto_duplicado, tx_rows=tx_rows)

    with patch.dict(sys.modules, {"db": fake_simple}):
        bal_simple = bp.calcular()
    with patch.dict(sys.modules, {"db": fake_doble}):
        bal_doble = bp.calcular()

    delta = abs(bal_simple["saldo_banco_esperado"] - bal_doble["saldo_banco_esperado"])
    assert delta <= 0.01, (
        f"Duplicar el extracto cambió el balance: "
        f"simple={bal_simple['saldo_banco_esperado']:.2f} vs "
        f"doble={bal_doble['saldo_banco_esperado']:.2f}, delta={delta:.2f}. "
        "Esto significa que el dedup no es idempotente."
    )


# ─── Test 3: smoke — drift máximo $100 sin causa ─────────────────────


def test_extracto_fuera_de_libros_no_infla_balance():
    """REGRESIÓN −500k (TMT 2026-06-04 dueña: "hay −500k sin explicarse").

    El caso real: el extracto mov-02-06 traía ~−557k de débitos (PAGO SENAE,
    etc.) que NO se dedupean limpio contra transacciones_bancarias (desfase
    de fecha / montos agrupados). El viejo bloque los sumaba a pendientes de
    banco y el saldo_banco_esperado se desplomaba a ~1.887.337.

    Nueva regla: pendientes de banco = la hoja (históricos). El extracto NO
    aporta nunca. Acá mandamos un extracto SIN contraparte en tx (tx_rows
    vacío) y el balance debe quedar clavado en el target de históricos.
    """
    from modules.conciliacion import balance_pichincha as bp

    extracto = [
        {"fecha": "2026-06-02", "documento": "16241744", "monto": 15441.80, "tipo": "D"},
        {"fecha": "2026-06-02", "documento": "17416319", "monto": 14602.70, "tipo": "D"},
        {"fecha": "2026-06-02", "documento": "99999999", "monto": 527135.01, "tipo": "D"},
    ]
    # tx_rows vacío → el dedup nuclear NO puede filtrar nada. Con el bug
    # viejo, estos −557k entrarían como pendiente y hundirían el saldo.
    fake = _make_fake_db(extracto_rows=extracto, tx_rows=[])

    with patch.dict(sys.modules, {"db": fake}):
        resultado = bp.calcular()

    esperado = resultado["saldo_banco_esperado"]
    assert abs(esperado - TARGET_ESPERADO) <= 1.0, (
        f"saldo_banco_esperado = {esperado:.2f}, target = {TARGET_ESPERADO:.2f}. "
        "El extracto crudo volvió a inflar pendientes de banco (regresión −500k)."
    )


# ─── TMT 2026-06-17: anti-regresión para Fix Bug 2 ─────────────────────
# Caso: DEPOSITO NO IDENTIFICADO con tipo=NULL aparece en el xlsx pero
# no sumaba al sum_cred del balance porque CASE WHEN tipo='C' no lo
# pescaba. Fix usa COALESCE(tipo, 'C') en SQL.


def test_query_sql_usa_coalesce_tipo_default_C():
    """Verifica que la query de pendientes históricos defaultea tipo=NULL
    a 'C' (matchea el comportamiento del render del xlsx)."""
    import inspect

    from modules.conciliacion import balance_pichincha as bp
    src = inspect.getsource(bp.calcular)
    # Debe estar COALESCE(tipo, 'C') en SQL para no perder filas tipo=NULL
    assert "COALESCE(tipo, 'C')" in src, (
        "balance_pichincha.calcular() debe usar COALESCE(tipo, 'C') en SQL "
        "para sumar filas sin tipo (caso DEPOSITO NO IDENTIFICADO sin fecha)."
    )


@pytest.mark.parametrize("n_extra_extracto", [0, 1, 4, 10])
def test_drift_balance_acotado(n_extra_extracto):
    """Para varios tamaños del extracto (todas filas que existen en tx),
    el saldo_banco_esperado nunca debe drift más de $100 vs el target.
    Si alguna de estas variantes infla, el dedup nuclear se rompió.
    """
    from modules.conciliacion import balance_pichincha as bp

    base = _extracto_sesion_35_mov_02_06()
    # Agregamos n_extra filas — todas con la misma firma pero documentos
    # distintos (simula re-numeración del banco). Todas las versiones
    # deben dedupearse correctamente porque la firma usa fecha+monto+tipo,
    # NO documento.
    extra = []
    for i in range(n_extra_extracto):
        for m in base:
            extra.append({
                "fecha": m["fecha"],
                "documento": f"renum_{i}_{m['documento']}",
                "monto": m["monto"],
                "tipo": m["tipo"],
            })
    extracto = base + extra
    tx_rows = _tx_bancarias_rows_que_duplican(base)
    fake = _make_fake_db(extracto_rows=extracto, tx_rows=tx_rows)

    with patch.dict(sys.modules, {"db": fake}):
        resultado = bp.calcular()

    esperado = resultado["saldo_banco_esperado"]
    drift = abs(esperado - TARGET_ESPERADO)
    assert drift <= 100.0, (
        f"n_extra_extracto={n_extra_extracto}: balance={esperado:.2f}, "
        f"target={TARGET_ESPERADO:.2f}, drift={drift:.2f}. "
        "El dedup nuclear permitió que el extracto inflara el balance."
    )
