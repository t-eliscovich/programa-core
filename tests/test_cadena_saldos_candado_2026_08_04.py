"""El candado de la cadena de saldos y el re-encadenado hacia atrás.

TMT 2026-08-04. La dueña, sobre los 7 quiebres que quedaban abiertos del
03/08: *"sigue siendo un problema la cadena? pensé que estaba arreglado"* y,
cuando le contesté que hacía un mes que no aparecía uno nuevo,
*"hoy no se usó, pero ayer hubo otro quiebre, no nos debería pasar más"*.

Tenía razón en las dos. La evidencia de que el motor estaba sano era la
**ausencia de uso**, no la ausencia de bug: el quiebre del 03/08 apareció y se
reparó el mismo día, y el 04/08 no se cargó nada. "Hace rato que no pasa" no
es una garantía.

Tres cosas se aprendieron mirando las 1.333 filas de Pichincha en vivo:

1. ⭐ **La convención de signos del dBase SÍ se puede leer.** Se creía que no
   (por eso el health usaba criterio `ABS` y por eso se había descartado
   derivar el saldo sumando). Con la regla `documento ∈ DOCS_ENTRADA ⇒ +`,
   1.326 de 1.333 filas encadenan exacto — y las 7 excepciones son
   exactamente los 7 quiebres reales. Lo que estaba roto era el **orden**
   (saldo estampado por `id`, leído por `(fecha, id)`), no los signos.

2. ⭐ **El `delta_stored_vs_signed` del health era ruido estructural.**
   `saldo_signed` sumaba los movimientos sin la APERTURA del banco, así que
   la diferencia contra el running guardado valía la apertura —en Pichincha
   **2.962.335,77**— todos los días, pasara lo que pasara. La alerta gritaba
   "el patrimonio está corrido por esa plata" sobre un número que no era
   plata. Un ⚠ que siempre está prendido entrena a ignorar el panel.

3. ⭐ **Re-encadenar hacia adelante mueve el cierre; hacia atrás, no.** Por eso
   el dry-run desde el 29/06 se abortó el 03/08: el cierre es el número que
   publica el Balance y el que la conciliación ya validó contra el extracto
   (+2,00). Anclando en la última fila y caminando al pasado, el cierre queda
   invariante POR CONSTRUCCIÓN — se plancha la historia sin tocar el presente.

Y la respuesta al "no nos debería pasar más" no es un chequeo a la mañana
siguiente: es que **una transacción que deja la cadena partida no commitee**.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_bank_helpers import _FakeBankDB  # noqa: E402


def _fila(id_tx, fecha, doc, importe, saldo, concepto="x"):
    return {
        "id_transaccion": id_tx, "fecha": fecha, "documento": doc,
        "concepto": concepto, "importe": importe, "saldo": saldo,
        "no_banco": 10, "no_cta": None, "usuario_crea": "dbf-import",
    }


def _ledger_con_costura():
    """La forma real de Pichincha: un bloque backdated estampado al nivel
    equivocado porque el saldo se escribió en orden de INSERCIÓN.

    En orden (fecha, id) —el que lee todo el sistema— la fila del 29/06
    aparece con un saldo de 3.000 cuando le corresponde 998.
    """
    return [
        _fila(1, date(2026, 6, 28), "DE", 100, 1000.0, "apertura del tramo"),
        _fila(5, date(2026, 6, 29), "ND", 2, 3000.0, "GS. cheq. CLR"),
        _fila(2, date(2026, 6, 30), "DE", 50, 1048.0, "1 ch.DMA"),
        _fila(3, date(2026, 7, 1), "DE", 10, 1058.0, "dep. 1 ch."),
    ]


def _instalar(monkeypatch, filas):
    import bank_helpers
    fake = _FakeBankDB(filas)
    monkeypatch.setattr(bank_helpers.db, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(bank_helpers.db, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(bank_helpers.db, "execute", fake.execute)
    monkeypatch.setattr(bank_helpers.db, "execute_returning", fake.execute_returning)
    return fake


# --- 1) el re-encadenado hacia atrás -------------------------------------


def test_retro_plancha_la_costura_sin_mover_el_cierre(monkeypatch):
    """⭐ EL PUNTO DE TODO: la utilidad no se mueve ni un centavo.

    El cierre es lo que `saldo_bancos()` publica como BANCOS, y con eso el
    patrimonio y la utilidad. Anclar ahí y caminar al pasado lo deja
    invariante por construcción — no por suerte ni por revisión a ojo.
    """
    import bank_helpers
    fake = _instalar(monkeypatch, _ledger_con_costura())

    cierre_antes = fake.filas[-1]["saldo"]
    assert len(bank_helpers.contar_quiebres(no_banco=10)) == 2

    n = bank_helpers.reencadenar_retro(None, no_banco=10)

    assert n == 1, "sólo la fila del 29/06 estaba mal; las demás son ruido"
    por_id = {f["id_transaccion"]: f["saldo"] for f in fake.filas}
    assert por_id[5] == 998.0, "1048 − 50 = 998: el nivel que le tocaba"
    assert por_id[3] == cierre_antes, "EL CIERRE NO SE PUEDE MOVER"
    assert por_id[1] == 1000.0, "la primera fila tampoco: el opening queda"
    assert bank_helpers.contar_quiebres(no_banco=10) == []


def test_retro_dry_run_no_escribe_y_muestra_lo_mismo_que_aplicaria(monkeypatch):
    """El dry-run tiene que ser EXACTAMENTE lo que el aplicar escribe.

    Si el plan y la escritura pueden diferir, mirarlo antes no sirve de nada
    — que es todo el sentido de la pantalla. [[feedback_dry_run_delta_uniforme]]
    """
    import bank_helpers
    fake = _instalar(monkeypatch, _ledger_con_costura())

    plan = bank_helpers.reencadenar_retro(None, no_banco=10, dry_run=True)
    assert [f["saldo"] for f in fake.filas] == [1000.0, 3000.0, 1048.0, 1058.0], \
        "el dry-run escribió algo"

    prometido = {f["id_transaccion"]: f["saldo_nuevo"] for f in plan}
    bank_helpers.reencadenar_retro(None, no_banco=10)
    assert {f["id_transaccion"]: f["saldo"] for f in fake.filas} == prometido


def test_retro_aborta_si_moveria_la_primera_fila(monkeypatch):
    """GUARDA ESPEJO del desastre del 2026-05-12 (Pichincha −917.651,96).

    Aquel fue por el otro lado: un walk sin ancla destruyó el opening. Con
    los dos extremos clavados —cierre por construcción, opening por esta
    guarda— un re-encadenado no puede inventar plata. Si los dos extremos no
    reconcilian, no es una costura de orden: es un faltante, y lo tiene que
    mirar una persona contra el extracto.
    """
    import bank_helpers
    filas = _ledger_con_costura()
    filas[0]["saldo"] = 5000.0  # el opening no reconcilia con el cierre
    _instalar(monkeypatch, filas)

    with pytest.raises(ValueError, match="PRIMERA fila"):
        bank_helpers.reencadenar_retro(None, no_banco=10, dry_run=True)


def test_retro_es_idempotente(monkeypatch):
    """Correrlo dos veces no puede mover nada la segunda."""
    import bank_helpers
    fake = _instalar(monkeypatch, _ledger_con_costura())
    bank_helpers.reencadenar_retro(None, no_banco=10)
    saldos = [f["saldo"] for f in fake.filas]
    assert bank_helpers.reencadenar_retro(None, no_banco=10) == 0
    assert [f["saldo"] for f in fake.filas] == saldos


# --- 2) el candado de commit ---------------------------------------------


def test_assert_cadena_intacta_revienta_con_una_costura(monkeypatch):
    import bank_helpers
    _instalar(monkeypatch, _ledger_con_costura())

    with pytest.raises(bank_helpers.CadenaRotaError) as e:
        bank_helpers.assert_cadena_intacta(None, no_banco=10)
    msg = str(e.value)
    assert "No se guardó nada" in msg
    assert "GS. cheq. CLR" in msg, "el mensaje tiene que decir QUÉ fila"


def test_el_candado_no_frena_una_carga_de_hoy_por_una_costura_vieja(monkeypatch):
    """Acotar por `desde_fecha` no es un detalle: es lo que hace que el
    candado se pueda prender ANTES de terminar de limpiar la historia.

    Si un quiebre del 29/06 bloqueara el alta de hoy, la oficina no podría
    trabajar y el candado se terminaría sacando.
    """
    import bank_helpers
    _instalar(monkeypatch, _ledger_con_costura())

    bank_helpers.assert_cadena_intacta(
        None, no_banco=10, desde_fecha=date(2026, 7, 1))

    with pytest.raises(bank_helpers.CadenaRotaError):
        bank_helpers.assert_cadena_intacta(
            None, no_banco=10, desde_fecha=date(2026, 6, 29))


def test_insert_backdated_pasa_por_el_candado(monkeypatch):
    """El caso del 03/08 entero: la conciliación inserta una fila BACKDATED.

    Con el ancla por (fecha, id) el insert ya deja la cadena bien; el candado
    lo verifica en la misma transacción en vez de confiar. Si algún día el
    walk se vuelve a romper, la operación no commitea — no llega al balance.
    """
    import bank_helpers
    fake = _instalar(monkeypatch, [
        _fila(1, date(2026, 7, 1), "DE", 1000, 1000.0),
        _fila(2, date(2026, 8, 3), "CH", 100, 900.0),
    ])
    bank_helpers.insert_movimiento_bancario(
        None, no_banco=10, no_cta=None, fecha=date(2026, 7, 30),
        documento="ND", importe=2.96, concepto="Comisiones e impuestos",
        usuario="conciliacion",
    )
    assert bank_helpers.contar_quiebres(no_banco=10) == []
    ordenadas = sorted(fake.filas, key=lambda f: (f["fecha"], f["id_transaccion"]))
    assert [f["saldo"] for f in ordenadas] == [1000.0, 997.04, 897.04]


# --- 3) una sola regla de signos -----------------------------------------


def test_la_regla_de_signos_de_sql_es_la_misma_que_la_de_python():
    """`_sql_signed_delta` y `_signed_delta` tienen que dar lo mismo.

    Había TRES reglas distintas conviviendo (`DOCS_ENTRADA` en bank_helpers,
    `IN ('CH','ND')` en saldo_bancos, `IN ('CH','ND','RE','GS','PA')` en el
    fallback de `_saldo_previo`). Hoy la tabla sólo tiene DE/ND/CH/NC y las
    tres coinciden, así que la divergencia es invisible — hasta que aparezca
    un documento nuevo. [[feedback_coherencia_numeros_una_fuente]]
    """
    import bank_helpers
    sql = bank_helpers._sql_signed_delta("t")
    for doc in bank_helpers.DOCS_ENTRADA:
        assert f"'{doc}'" in sql
        assert bank_helpers._signed_delta(doc, 100) == 100
    for doc in ("CH", "ND", "RE", "GS", "PA"):
        assert f"'{doc}'" not in sql
        assert bank_helpers._signed_delta(doc, 100) == -100


# --- 4) el health deja de gritar la apertura -----------------------------


def test_health_no_alerta_por_la_apertura_del_banco():
    """⭐ LA REGRESIÓN QUE IMPORTA. Los números reales de Pichincha del 04/08.

    `stored` 1.960.392,09 y `signed` −1.001.943,68 difieren en 2.962.335,77 —
    que es, al centavo, la apertura del banco. No es plata perdida: es la
    plata que Pichincha tenía antes de la primera fila cargada. Comparar el
    running guardado contra una suma que no la incluye no puede dar otra cosa.
    """
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    stat, alerts = _evaluar_cadena(
        no_banco=10, nombre="PICHINCHA",
        stored=1960392.09, signed=-1001943.68, derivado=1960392.09,
        breaks=[], n_nulls=0, dias=120,
    )
    assert stat["apertura_implicita"] == 2962335.77
    assert stat["delta_stored_vs_derivado"] == 0.0
    assert alerts == [], (
        "el health venía gritando la apertura como si fuera cadena rota; "
        "un ⚠ diario por algo legítimo entrena a ignorar el panel"
    )


def test_health_si_alerta_cuando_el_balance_no_es_derivable():
    """El invariante DE VERDAD. Con esto, el 03/08 se cazaba solo.

    Aquel día `stored` estaba 155.187,31 arriba de apertura + movimientos.
    El health de entonces sólo miraba los saltos fila a fila; éste mira el
    número que el Balance publica.
    """
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    _stat, alerts = _evaluar_cadena(
        no_banco=10, nombre="PICHINCHA",
        stored=2644585.87, signed=0.0, derivado=2489398.56,
        breaks=[], n_nulls=0, dias=120,
    )
    assert [a["category"] for a in alerts] == ["saldo_no_derivable"]
    assert alerts[0]["severity"] == "high"
    assert "155,187.31" in alerts[0]["msg"]


def test_health_sin_derivado_no_inventa_alerta():
    """Si `saldo_derivado` no vino (banco sin filas), no se alerta."""
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    stat, alerts = _evaluar_cadena(
        no_banco=99, nombre="VACIO", stored=0.0, signed=0.0, derivado=None,
        breaks=[], n_nulls=0, dias=120,
    )
    assert stat["delta_stored_vs_derivado"] is None
    assert alerts == []


# --- 5) el candado está ENCHUFADO a los writes ---------------------------
#
# Los tests de arriba prueban que `assert_cadena_intacta` sabe ver un
# quiebre. Estos prueban lo otro, que es lo que la dueña pidió: que un write
# que dejaría la cadena partida NO llegue a commitear. Se fuerza el fallo
# rompiendo a propósito el motor y se verifica que la operación revienta.


def test_insert_que_dejaria_costura_no_commitea(monkeypatch):
    """Si el walk-forward dejara de correr, el alta tiene que reventar.

    Sin el candado esto guardaba en silencio una fila backdated con saldo
    del futuro — el bug del 03/08 exacto — y el Balance publicaba el número
    inflado hasta que alguien mirara el health a la mañana siguiente.
    """
    import bank_helpers
    _instalar(monkeypatch, [
        _fila(1, date(2026, 7, 1), "DE", 1000, 1000.0),
        _fila(2, date(2026, 8, 3), "CH", 100, 900.0),
    ])
    # el walk-forward "se rompe": no re-encadena las filas posteriores
    monkeypatch.setattr(bank_helpers, "recompute_saldos_desde",
                        lambda *a, **kw: 0)

    with pytest.raises(bank_helpers.CadenaRotaError, match="No se guardó nada"):
        bank_helpers.insert_movimiento_bancario(
            None, no_banco=10, no_cta=None, fecha=date(2026, 7, 30),
            documento="ND", importe=2.96, concepto="Comisiones e impuestos",
            usuario="conciliacion",
        )


def test_recompute_que_deja_costura_no_commitea(monkeypatch):
    """Y el re-encadenado hacia adelante se audita a sí mismo."""
    import bank_helpers
    _instalar(monkeypatch, _ledger_con_costura())
    # el motor de signos "se rompe": todo delta pasa a valer 0
    monkeypatch.setattr(bank_helpers, "_signed_delta", lambda *a, **kw: 0.0)

    with pytest.raises(bank_helpers.CadenaRotaError):
        bank_helpers.recompute_saldos_desde(
            None, no_banco=10, ancla_fecha=date(2026, 6, 28))


# --- 6) lo que vive en SQL se testea leyendo el fuente --------------------
#
# No hay Postgres en CI, así que el fake implementa el filtro en Python y una
# mutación del SQL pasaría inadvertida. Estos dos claven la forma de la
# consulta, que es donde está la sutileza.


def test_la_ventana_se_calcula_ANTES_de_filtrar_por_fecha():
    """⭐ El orden importa: si `desde_fecha` filtrara dentro del CTE, la
    primera fila de la ventana se quedaría sin anterior y **el quiebre del
    borde se perdería** — justo el que uno está buscando después de un write.

    Por eso el `LAG` corre sobre todas las filas del banco y el recorte por
    fecha va en el SELECT de afuera.
    """
    import inspect
    import bank_helpers

    src = " ".join(inspect.getsource(bank_helpers.contar_quiebres).split())
    cte, afuera = src.split("SELECT * FROM w", 1)
    assert "fecha >= %s::date" not in cte, (
        "el recorte por fecha se metió adentro del CTE: el LAG ya no ve la "
        "fila anterior al borde de la ventana"
    )
    assert "AND (%s::date IS NULL OR fecha >= %s::date)" in " ".join(
        afuera.split()), (
        "la cláusula de recorte cambió de forma: o dejó de recortar, o dejó "
        "de ver todo cuando no se pasa desde_fecha"
    )


def test_el_sql_de_quiebres_usa_la_regla_firmada_unica():
    import inspect
    import bank_helpers

    src = inspect.getsource(bank_helpers.contar_quiebres)
    assert "{_sql_signed_delta('t')} AS sgn" in src, (
        "el criterio de signos se duplicó en vez de salir de la fuente única"
    )
    assert "ABS((saldo - saldo_prev) - sgn)" in src, (
        "volvió el criterio ABS: deja pasar la fila que se mueve el importe "
        "correcto para el lado equivocado"
    )
