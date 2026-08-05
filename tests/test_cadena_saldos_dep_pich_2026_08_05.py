"""DEP.PICH. (banco 90) — el falso positivo del 05/08/2026.

QUÉ PASÓ. El health del día amaneció con `ok: false` y una alerta `high`:

    DEP.PICH.: el saldo que usa el BALANCE (-455.89) no coincide con
    apertura + movimientos (0.00) — difieren -455.89. Ese es el patrimonio
    y la utilidad corridos por esa plata. Re-encadenar en /bancos/reencadenar.

Las dos frases eran falsas:

1. **El Balance NO publicaba −455,89.** Desde el 04/08 `saldo_bancos()`
   calcula el saldo como APERTURA + suma firmada (`saldo_origen ==
   'derivado'`). Para el 90 eso da 0,00, y 0,00 es lo que entró al
   patrimonio. Los −455,89 salían de `saldo_stored`, la columna running
   leída con el filtro `ABS(saldo) > 0.5` — un filtro que saltea la última
   fila justamente por valer cero y se trae la anterior. El health la
   rotulaba `saldo_usado_por_el_balance` y esa etiqueta había quedado vieja.

2. **Re-encadenar no arreglaba nada.** El banco tiene 2 filas: un ND de
   455,89 del 23/06 y su reverso NC del 25/06. La cadena está entera (0
   quiebres) y el re-encadenado hacia atrás ancla en el cierre (0,00) y
   estampa −455,89 en la primera fila: exactamente lo que ya estaba. El
   dry-run en vivo dijo "FILAS QUE CAMBIAN 0". Mandar a apretar ese botón
   costó una sesión.

LA LECCIÓN: cuando cambia de dónde sale un número, hay que cambiar también
lo que lo vigila. Un chequeo que mira una fuente que la app abandonó no
avisa de menos — avisa DE MÁS, y un ⚠ diario por algo legítimo entrena a
ignorar el panel entero.
"""

import pytest

# Números REALES del banco 90 medidos en producción el 05/08/2026.
DEP_PICH = dict(
    no_banco=90, nombre="DEP.PICH.",
    stored=0.0,             # lo que el Balance publica (apertura + movs)
    columna_running=-455.89,  # la columna `saldo` con el filtro ABS > 0.5
    origen="derivado",
    signed=0.0, derivado=0.0,
    breaks=[], n_nulls=0, dias=120,
)


def test_dep_pich_no_alerta_porque_el_balance_publica_cero():
    """⭐ LA REGRESIÓN. Sin plata corrida no hay alerta."""
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    stat, alerts = _evaluar_cadena(**DEP_PICH)

    assert stat["saldo_usado_por_el_balance"] == 0.0
    assert stat["delta_stored_vs_derivado"] == 0.0
    assert alerts == [], (
        "el Balance publica 0,00 y apertura + movimientos da 0,00: no hay "
        "un centavo corrido. Un ⚠ diario por algo legítimo entrena a "
        "ignorar el panel entero"
    )


def test_dep_pich_deja_ver_la_columna_desfasada_sin_alertar():
    """La columna sigue en −455,89 y la pantalla del banco la muestra.

    Se publica como STAT, no como alerta: planchar la columna es una
    decisión aparte de la dueña (dijo que no el 03/08) y no mueve plata.
    """
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    stat, alerts = _evaluar_cadena(**DEP_PICH)

    assert stat["saldo_columna_running"] == -455.89
    assert stat["delta_columna_vs_derivado"] == -455.89
    assert stat["saldo_origen"] == "derivado"
    assert [a["category"] for a in alerts] == []


def test_si_el_balance_cae_a_la_columna_la_alerta_SI_salta():
    """El candado del candado: sin apertura, la columna ES el Balance.

    Mismo banco, mismos números, pero `saldo_bancos()` cayendo a la escalera
    vieja (`origen='stored'`): ahí el Balance publica −455,89 de verdad y
    eso sí es patrimonio corrido. La alerta tiene que volver.
    """
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    _stat, alerts = _evaluar_cadena(
        **{**DEP_PICH, "stored": -455.89, "origen": "stored"})

    assert [a["category"] for a in alerts] == ["saldo_no_derivable"]
    assert alerts[0]["severity"] == "high"
    assert "-455.89" in alerts[0]["msg"]


def test_sin_quiebres_la_alerta_no_manda_a_reencadenar():
    """Re-encadenar con 0 quiebres es un no-op — no lo recomiendes.

    El 05/08 el mensaje decía "Re-encadenar en /bancos/reencadenar" para un
    banco con la cadena entera. El dry-run contestó "filas que cambian: 0".
    """
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    _stat, alerts = _evaluar_cadena(
        **{**DEP_PICH, "stored": -455.89, "origen": "stored"})

    msg = alerts[0]["msg"]
    assert "la cadena no tiene quiebres" in msg
    assert "APERTURA" in msg


def test_con_quiebres_si_manda_a_reencadenar():
    """Con la cadena partida, re-encadenar SÍ es el botón correcto."""
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    quiebre = {
        "id_transaccion": 1, "fecha": "2026-06-29", "documento": "ND",
        "concepto": "x", "importe": 2.96, "saldo_prev": 2464557.97,
        "saldo": 2619748.24, "sgn": -2.96, "concepto_prev": "y",
    }
    _stat, alerts = _evaluar_cadena(
        no_banco=10, nombre="PICHINCHA", stored=2644585.87, signed=0.0,
        derivado=2489398.56, origen="stored", columna_running=2644585.87,
        breaks=[quiebre], n_nulls=0, dias=120,
    )

    cats = [a["category"] for a in alerts]
    assert "saldo_no_derivable" in cats
    no_deriv = next(a for a in alerts if a["category"] == "saldo_no_derivable")
    assert "Re-encadenar en /bancos/reencadenar." in no_deriv["msg"]
    assert "no tiene quiebres" not in no_deriv["msg"]


def test_el_caller_le_pasa_el_saldo_publicado_no_la_columna():
    """⭐ EL BUG EXACTO, en el punto donde se cometió.

    `cadena_saldos()` le pasaba `saldo_stored` a `_evaluar_cadena` y lo
    rotulaba `saldo_usado_por_el_balance`. Este test lee el fuente porque el
    endpoint necesita Postgres y en CI no hay: es la misma técnica que
    `test_auditar_agrupa_por_batch.py`.
    """
    import inspect

    from modules.admin_dbase import health_audit_view as mod

    src = inspect.getsource(mod.cadena_saldos)
    assert 'stored=float(b.get("saldo") or 0)' in src, (
        "el health tiene que vigilar el número que el Balance PUBLICA"
    )
    assert 'columna_running=float(b.get("saldo_stored") or 0)' in src
    assert 'stored=float(b.get("saldo_stored") or 0)' not in src, (
        "la columna running dejó de ser el saldo del Balance el 04/08"
    )


@pytest.mark.parametrize("origen", ["derivado", "stored", "signed", "raw"])
def test_banco_sano_nunca_alerta_venga_de_donde_venga(origen):
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    _stat, alerts = _evaluar_cadena(
        no_banco=32, nombre="INTERNACI", stored=3761.19, signed=3761.19,
        derivado=3761.19, origen=origen, columna_running=3761.19,
        breaks=[], n_nulls=0, dias=120,
    )
    assert alerts == []
