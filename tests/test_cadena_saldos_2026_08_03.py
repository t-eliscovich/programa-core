"""El ancla del recompute va por (fecha, id) — no por id. TMT 2026-08-03.

QUÉ PASÓ. La dueña vio la utilidad en **37.658**. En tres cargas de
`/informes/balance` en cinco minutos, sin que nadie cargara nada, BANCOS
pasó de 2.492.722 a 2.647.891 (Pichincha **+155.169**) y la utilidad a
193.749. Al mismo tiempo la sesión de conciliación #60 marcaba una
diferencia de **+155.195,23** contra el extracto.

Era todo lo mismo. `saldo_bancos()` NO suma los movimientos: toma el `saldo`
running GUARDADO de la fila de mayor (fecha, id). Y
`recompute_saldos_desde(ancla_id=...)` mezclaba dos órdenes: sacaba el saldo
de arranque con `id_transaccion <` (orden de INSERCIÓN) y después caminaba
`ORDER BY fecha, id_transaccion` (orden de FECHA).

Con una fila BACKDATED (id alto, fecha vieja) — que es EXACTAMENTE lo que
inserta la conciliación cuando crea un `ND Comisiones e impuestos` o un
`DE AJUSTE CONCILIACION` — pasaban dos cosas:

  1. el saldo de arranque salía de la ÚLTIMA fila INSERTADA, de una fecha
     mucho posterior → la fila vieja quedaba estampada con un saldo del
     futuro;
  2. `id_transaccion >= ancla_id` dejaba AFUERA del walk a las filas de
     fecha posterior con id menor → la cadena quedaba partida en dos.

Medido en vivo: una fila de **$2,96** movió el saldo **+155.187,31**.
La conciliación se rompía a sí misma.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_bank_helpers import _FakeBankDB, _FakeCajaDB  # noqa: E402


def _cadena_entera(filas, key_id):
    """Devuelve los quiebres: |Δsaldo| tiene que valer |importe|."""
    ordenadas = sorted(filas, key=lambda f: (f["fecha"], f[key_id]))
    quiebres = []
    for prev, cur in zip(ordenadas, ordenadas[1:]):
        delta = abs(round(cur["saldo"] - prev["saldo"], 2))
        if abs(delta - abs(float(cur["importe"]))) > 0.02:
            quiebres.append((prev, cur))
    return quiebres


# --- bank_helpers ----------------------------------------------------------


def _escenario_pichincha():
    """Reproducción mínima del 03/08: una fila backdated con id alto.

    id 1 · 01/07 · DE 1.000 → 1.000
    id 2 · 03/08 · CH   100 →   900   (fecha POSTERIOR, id MENOR)
    id 3 · 30/07 · ND  2,96 →   ???   (BACKDATED, id MAYOR) ← la de la
                                       conciliación
    """
    return _FakeBankDB(filas_pre=[
        {"id_transaccion": 1, "fecha": date(2026, 7, 1),
         "documento": "DE", "importe": 1000, "saldo": 1000,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
        {"id_transaccion": 2, "fecha": date(2026, 8, 3),
         "documento": "CH", "importe": 100, "saldo": 900,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
        {"id_transaccion": 3, "fecha": date(2026, 7, 30),
         "documento": "ND", "importe": 2.96, "saldo": 0,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
    ])


def test_ancla_backdated_no_parte_la_cadena(monkeypatch):
    """El caso exacto que rompió Pichincha el 03/08/2026."""
    import bank_helpers as bh
    import db as db_mod

    fake = _escenario_pichincha()
    fake.apply_to(monkeypatch, db_mod)

    bh.recompute_saldos_desde(
        conn=object(), no_banco=10, no_cta=None, ancla_id=3,
    )

    por_id = {f["id_transaccion"]: f for f in fake.filas}
    # Cadena en orden de FECHA: 1.000 → −2,96 → −100
    assert por_id[1]["saldo"] == 1000.0
    assert por_id[3]["saldo"] == 997.04, (
        "la fila backdated quedó anclada en la última fila INSERTADA (de una "
        "fecha posterior) en vez de en la anterior por FECHA"
    )
    assert por_id[2]["saldo"] == 897.04

    assert not _cadena_entera(fake.filas, "id_transaccion"), (
        "la cadena del running saldo quedó partida — es el bug que hizo que "
        "el balance leyera un patrimonio inflado y que la conciliación "
        "mostrara esa misma diferencia contra el extracto"
    )


def test_ancla_backdated_no_saltea_filas_de_fecha_posterior_con_id_menor(monkeypatch):
    """`id_transaccion >= ancla_id` dejaba afuera del walk a la fila 2.

    La fila 2 es del 03/08 (posterior al ancla del 30/07) pero tiene id
    MENOR, así que el walk viejo no la tocaba: se quedaba con el saldo
    stale y la cadena quedaba en dos segmentos incoherentes.
    """
    import bank_helpers as bh
    import db as db_mod

    fake = _escenario_pichincha()
    fake.apply_to(monkeypatch, db_mod)

    n = bh.recompute_saldos_desde(
        conn=object(), no_banco=10, no_cta=None, ancla_id=3,
    )
    assert n == 2, "el walk tiene que re-estampar la backdated Y la posterior"

    ids_tocados = {
        p[1] for sql, p in fake.executes
        if "SET saldo" in sql
    }
    assert ids_tocados == {2, 3}


def test_ancla_borrada_ensancha_el_walk_nunca_lo_achica(monkeypatch):
    """Si el ancla ya no existe, arrancamos de la fecha más vieja que quedó."""
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[
        {"id_transaccion": 1, "fecha": date(2026, 7, 1),
         "documento": "DE", "importe": 1000, "saldo": 1000,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
        {"id_transaccion": 5, "fecha": date(2026, 7, 2),
         "documento": "CH", "importe": 100, "saldo": 0,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
    ])
    fake.apply_to(monkeypatch, db_mod)

    # ancla_id=3 no existe (la borraron entre medio).
    n = bh.recompute_saldos_desde(
        conn=object(), no_banco=10, no_cta=None, ancla_id=3,
    )
    assert n == 1
    por_id = {f["id_transaccion"]: f for f in fake.filas}
    assert por_id[5]["saldo"] == 900.0
    assert not _cadena_entera(fake.filas, "id_transaccion")


def test_ancla_id_sin_filas_no_explota(monkeypatch):
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)
    assert bh.recompute_saldos_desde(
        conn=object(), no_banco=10, no_cta=None, ancla_id=99,
    ) == 0


# --- caja_helpers (mismo bug) ---------------------------------------------


def test_caja_ancla_backdated_no_parte_la_cadena(monkeypatch):
    import caja_helpers as ch
    import db as db_mod

    # "E" = entrada (+1); cualquier otro tipo = salida (−1).
    fake = _FakeCajaDB(filas_pre=[
        {"id_caja": 1, "fecha": date(2026, 7, 1), "tipo": "E",
         "importe": 1000, "saldo": 1000},
        {"id_caja": 2, "fecha": date(2026, 8, 3), "tipo": "S",
         "importe": 100, "saldo": 900},
        {"id_caja": 3, "fecha": date(2026, 7, 30), "tipo": "S",
         "importe": 50, "saldo": 0},
    ])
    fake.apply_to(monkeypatch, db_mod)

    ch.recompute_saldos_desde(conn=object(), ancla_id=3)

    saldos = {}
    for sql, p in fake.executes:
        if "SET saldo" in sql:
            saldos[p[1]] = p[0]
    assert saldos == {3: 950.0, 2: 850.0}


# --- PATANT: gana la ÚLTIMA foto del mes ----------------------------------


def test_historia_ultimo_mes_desempata_por_id_historia(monkeypatch):
    """Sin desempate, el PATANT del mes que entra sale a la suerte.

    Desde a95c835b (01/08) cada entrada a /historico-12m toma una foto
    fresca: el último día del mes puede tener VARIAS filas. El 03/08 hubo
    3, con patrimonios que diferían 153.549. `ORDER BY fecha DESC LIMIT 1`
    devolvía una cualquiera — y el legacy RELEE ese snapshot y no lo
    recalcula nunca.
    """
    import db as db_mod
    from modules.informes import queries as iq

    visto = {}

    def _fake_fetch_one(sql, params=None, conn=None):
        visto["sql"] = " ".join((sql or "").split())
        return None

    monkeypatch.setattr(db_mod, "fetch_one", _fake_fetch_one)
    iq.historia_ultimo_mes()

    assert "ORDER BY fecha DESC, id_historia DESC" in visto["sql"], (
        "historia_ultimo_mes() volvió a quedar sin desempate: con dos fotos "
        "del último día del mes, el PATANT es no-determinista"
    )


# --- health: la divergencia tiene que ser VISIBLE --------------------------


def _break(imp, saldo_prev, saldo):
    return {
        "id_transaccion": 1, "fecha": date(2026, 7, 30), "documento": "ND",
        "concepto": "Comisiones e impuestos 17/06-30/07", "concepto_prev": "CC EMPAQUES",
        "importe": imp, "saldo_prev": saldo_prev, "saldo": saldo,
        # TMT 2026-08-04: el gap pasó a criterio FIRMADO (ver
        # `bank_helpers.contar_quiebres`). Un ND resta.
        "sgn": -imp,
    }


def test_health_cadena_saldos_caza_el_break_real():
    """Los números REALES de Pichincha del 03/08: $2,96 movió 155.187,31."""
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    stat, alerts = _evaluar_cadena(
        no_banco=10, nombre="PICHINCHA", stored=2644585.87, signed=2489398.56,
        breaks=[_break(2.96, 2464557.97, 2619748.24)], n_nulls=0, dias=120,
    )
    assert stat["n_breaks"] == 1
    # ⭐ TMT 2026-08-04 — con criterio FIRMADO el gap da 155.193,23 y no
    # 155.187,31. La diferencia son los 2×2,96 del propio movimiento: el
    # saldo tenía que BAJAR 2,96 y subió 155.190,27. Y 155.193,23 es
    # exactamente el Δ que el re-encadenado del 03/08 terminó aplicando a las
    # 107 filas — o sea el criterio firmado predice la corrección, el ABS no.
    assert stat["gap_total"] == 155193.23
    assert [a["category"] for a in alerts] == ["cadena_saldos_rota"]
    assert alerts[0]["severity"] == "high"
    assert "155,193.23" in alerts[0]["msg"]


def test_health_cadena_saldos_callada_cuando_esta_sana():
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    stat, alerts = _evaluar_cadena(
        no_banco=10, nombre="PICHINCHA", stored=1000.0, signed=1000.0,
        breaks=[], n_nulls=0, dias=120,
    )
    assert stat["gap_total"] == 0
    assert alerts == [], "un falso positivo diario entrena a ignorar el panel"


def test_health_cadena_saldos_tolera_centavos():
    """$0,02 de redondeo NO es un break — si no, el panel es ruido."""
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    _, alerts = _evaluar_cadena(
        no_banco=10, nombre="PICHINCHA", stored=1000.0, signed=1000.0,
        breaks=[_break(100.0, 1000.0, 899.99)], n_nulls=0, dias=120,
    )
    assert alerts == []


def test_health_cadena_saldos_avisa_saldos_null():
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    _, alerts = _evaluar_cadena(
        no_banco=10, nombre="PICHINCHA", stored=1000.0, signed=1000.0,
        breaks=[], n_nulls=3, dias=120,
    )
    assert [a["category"] for a in alerts] == ["saldo_null"]


# --- dry-run: mirar antes de escribir sobre plata de produccion -----------


def test_dry_run_no_escribe_y_devuelve_el_plan(monkeypatch):
    """`dry_run=True` devuelve exactamente lo que escribiría, sin tocar nada.

    Un recompute mal anclado dejó Pichincha en −917.651,96 el 2026-05-12.
    Mirarlo en seco primero es la misma disciplina del simulacro de cierre.
    """
    import bank_helpers as bh
    import db as db_mod

    fake = _escenario_pichincha()
    fake.apply_to(monkeypatch, db_mod)
    saldos_antes = {f["id_transaccion"]: f["saldo"] for f in fake.filas}

    plan = bh.recompute_saldos_desde(
        conn=object(), no_banco=10, no_cta=None,
        ancla_fecha=date(2026, 7, 30), dry_run=True,
    )

    assert {f["id_transaccion"]: f["saldo"] for f in fake.filas} == saldos_antes, (
        "el dry-run escribió en la base"
    )
    assert not [sql for sql, _ in fake.executes if "SET saldo" in sql]
    assert [(f["id_transaccion"], f["saldo_nuevo"]) for f in plan] == [
        (3, 997.04), (2, 897.04),
    ]
    # el plan trae el saldo guardado al lado del nuevo, para poder comparar
    assert plan[0]["saldo_actual"] == 0
    assert plan[1]["saldo_actual"] == 900


def test_dry_run_predice_exactamente_lo_que_aplica(monkeypatch):
    """Lo que el dry-run muestra es lo que el aplicar escribe. Sin sorpresas."""
    import bank_helpers as bh
    import db as db_mod

    fake = _escenario_pichincha()
    fake.apply_to(monkeypatch, db_mod)
    plan = bh.recompute_saldos_desde(
        conn=object(), no_banco=10, no_cta=None,
        ancla_fecha=date(2026, 7, 30), dry_run=True,
    )
    bh.recompute_saldos_desde(
        conn=object(), no_banco=10, no_cta=None, ancla_fecha=date(2026, 7, 30),
    )
    por_id = {f["id_transaccion"]: f["saldo"] for f in fake.filas}
    for f in plan:
        assert por_id[f["id_transaccion"]] == f["saldo_nuevo"]
