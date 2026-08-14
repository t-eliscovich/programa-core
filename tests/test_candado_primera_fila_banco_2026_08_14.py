"""El candado de la cadena del BANCO, ahora también sobre la PRIMERA fila.

⭐ POR QUÉ. `bank_helpers.contar_quiebres` filtraba `saldo_prev IS NOT NULL`:
arrancaba en la SEGUNDA fila de cada banco en orden `(fecha, id_transaccion)`.
Una fila sin predecesora no puede mostrar un salto, así que si la más VIEJA
tenía el saldo mal, no lo veía nadie — ni el alta de un movimiento, ni
`/admin/health/all`. Ya nos costó una sesión: una ND huérfana como primera
fila de Pichincha pasaba desapercibida en base virgen y recién aparecía cuando
dejaba de ser la primera.

La caja se quedó con ese hueco a propósito (14/08, `caja_helpers`): ahí la
primera fila ES la declaración de la apertura, así que contrastarla contra un
número sacado de ella misma daría OK siempre. **En banco no**: existe
`scintela.banco_apertura`, un número que se guarda aparte de las
transacciones. Por eso acá el chequeo se puede prender de verdad.

Prenderlo hoy no le frena la carga a nadie — medido el 14/08 sobre los 3
bancos con movimientos (1.762 filas): PICHINCHA 2.962.333,77, INTERNACI
3.761,19 y DEP.PICH. −455,89, los tres cuadran al centavo con su apertura.

⭐ Y NO CUALQUIER APERTURA SIRVE. La siembra de `banco_apertura` calculó
algunas aperturas DESDE las transacciones (`saldo de la última fila − suma de
los movimientos`). Con una así, la cuenta de la primera fila queda implicada
por los eslabones de adentro: si todos encadenan, cierra sola y no mide nada;
y si hay un quiebre interno, se lo devuelve a la fila más vieja como si fuera
de ella. Por eso sólo la apertura `'afirmada'` —la que puso una persona
mirando el extracto— habilita el chequeo, y el resto de los bancos queda con
el hueco DECLARADO (`primera_fila_chequeada` en el health), no heredado en
silencio.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
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


def _ledger_sano():
    """Apertura 900 + tres movimientos: 1.000 → 1.050 → 1.048."""
    return [
        _fila(1, date(2026, 6, 28), "DE", 100, 1000.0, "primera del banco"),
        _fila(2, date(2026, 6, 30), "DE", 50, 1050.0, "1 ch.DMA"),
        _fila(3, date(2026, 7, 1), "ND", 2, 1048.0, "GS. cheq. CLR"),
    ]


def _instalar(monkeypatch, filas):
    import bank_helpers
    fake = _FakeBankDB(filas)
    fake.apply_to(monkeypatch, bank_helpers.db)
    return fake


# ── 1) el hueco, y cómo se tapa ──────────────────────────────────────────

def test_la_primera_fila_mal_estampada_no_se_ve_sin_apertura(monkeypatch):
    """⭐ EL PUNTO CIEGO, clavado: sin apertura NO se ve. Es el estado en que
    vivió el banco hasta hoy, y por eso una ND huérfana como primera fila
    pasaba en base virgen."""
    import bank_helpers as bh

    filas = _ledger_sano()
    filas[0]["saldo"] = 4000.0      # la más vieja, 3.000 de más
    filas[1]["saldo"] = 4050.0      # el resto encadena a partir de ella
    filas[2]["saldo"] = 4048.0
    _instalar(monkeypatch, filas)

    assert bh.contar_quiebres(no_banco=10) == [], (
        "sin apertura, la fila más vieja no tiene contra qué contrastarse: "
        "ése es el hueco")


def test_con_la_apertura_afirmada_la_primera_fila_entra_al_chequeo(monkeypatch):
    """La otra mitad: pasándole la apertura, la misma fila salta."""
    import bank_helpers as bh

    filas = _ledger_sano()
    filas[0]["saldo"] = 4000.0
    filas[1]["saldo"] = 4050.0
    filas[2]["saldo"] = 4048.0
    _instalar(monkeypatch, filas)

    rotas = bh.contar_quiebres(no_banco=10, apertura=900.0)
    assert [r["id_transaccion"] for r in rotas] == [1]
    assert rotas[0]["saldo_prev"] == 900.0
    assert rotas[0]["es_primera"] is True, (
        "el que lee tiene que poder distinguir los dos problemas")


def test_con_la_apertura_correcta_no_se_inventa_un_quiebre(monkeypatch):
    """El espejo — un candado que se activa siempre se ve igual que uno que
    anda. Con la cadena sana y la apertura de verdad, silencio."""
    import bank_helpers as bh

    _instalar(monkeypatch, _ledger_sano())
    assert bh.contar_quiebres(no_banco=10, apertura=900.0) == []


def test_el_mensaje_distingue_la_apertura_del_salto_entre_filas(monkeypatch):
    """⭐ Son dos problemas distintos y se arreglan distinto.

    Un salto entre dos filas se plancha re-encadenando. Que la primera fila no
    cuadre con la apertura declarada, NO: el walk preserva justamente lo que
    está en discusión. Mandar a apretar el botón de re-encadenar ahí hace
    perder una tarde — ya pasó con DEP.PICH. el 05/08.
    """
    import bank_helpers as bh

    filas = _ledger_sano()
    filas[0]["saldo"] = 4000.0
    filas[1]["saldo"] = 4050.0
    filas[2]["saldo"] = 4048.0
    _instalar(monkeypatch, filas)

    with pytest.raises(bh.CadenaRotaError) as e:
        bh.assert_cadena_intacta(None, no_banco=10, apertura=900.0,
                                 contexto="Alta de DE prueba")
    msg = str(e.value)
    assert "la PRIMERA fila del banco 10 no cuadra con la apertura" in msg
    assert "sobran 3,000.00" in msg
    assert "NO se arregla re-encadenando" in msg
    assert "/bancos/apertura" in msg

    # y el otro caso sigue diciendo lo de siempre
    filas2 = _ledger_sano()
    filas2[1]["saldo"] = 9999.0
    filas2[2]["saldo"] = 9997.0
    _instalar(monkeypatch, filas2)
    with pytest.raises(bh.CadenaRotaError) as e2:
        bh.assert_cadena_intacta(None, no_banco=10, apertura=900.0)
    assert "el saldo saltó" in str(e2.value)
    assert "PRIMERA fila" not in str(e2.value)


# ── 2) quién puede autochequearse ────────────────────────────────────────

def _fake_apertura(monkeypatch, fila):
    """Hace que `scintela.banco_apertura` conteste `fila` (o nada)."""
    from modules.bancos import apertura as ap_mod

    def _fetch_one(sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "to_regclass" in s:
            return {"t": "scintela.banco_apertura"}
        if "from scintela.banco_apertura" in s:
            return fila
        return None

    monkeypatch.setattr(ap_mod._db, "fetch_one", _fetch_one)
    return ap_mod


def test_la_apertura_afirmada_habilita_el_candado(monkeypatch):
    ap_mod = _fake_apertura(
        monkeypatch, {"saldo_apertura": 2962335.77, "origen": "afirmada"})
    valor, motivo = ap_mod.apertura_para_candado(10)
    assert valor == 2962335.77
    assert motivo == "afirmada"


def test_la_apertura_SEMBRADA_no_puede_desmentir_a_las_transacciones(monkeypatch):
    """⭐ La que sale de las transacciones NO sirve de testigo.

    `_SIEMBRA_SQL` la calcula como `saldo de la última fila − suma de los
    movimientos`. Si todos los eslabones de adentro encadenan, la cuenta de la
    primera fila cierra por construcción (mide cero); y si hay un quiebre
    interno de X, la resta se lo devuelve como un −X en la fila más vieja, o
    sea le echa la culpa a la que no tuvo nada que ver. Peor que no chequear.
    """
    ap_mod = _fake_apertura(
        monkeypatch, {"saldo_apertura": 3761.19, "origen": "siembra"})
    valor, motivo = ap_mod.apertura_para_candado(32)
    assert valor is None
    assert "circular" in motivo and "siembra" in motivo


def test_un_banco_sin_fila_en_banco_apertura_queda_como_hasta_hoy(monkeypatch):
    """El hueco sigue abierto para ese banco — pero DICHO, no heredado."""
    ap_mod = _fake_apertura(monkeypatch, None)
    valor, motivo = ap_mod.apertura_para_candado(66)
    assert valor is None
    assert "no tiene apertura declarada" in motivo


def test_sin_la_tabla_no_revienta_ni_ensucia_la_transaccion(monkeypatch):
    """Si `banco_apertura` no existe todavía, el alta tiene que seguir.

    Y la pregunta va con `to_regclass` a propósito: un SELECT contra una tabla
    inexistente ABORTA la transacción entera en Postgres, así que el
    `try/except` salvaría el error pero el `db.tx()` del caller ya no podría
    commitear. El chequeo no puede ser el que rompa el alta que vino a cuidar.
    """
    from modules.bancos import apertura as ap_mod

    vistas = []

    def _fetch_one(sql, params=None, conn=None):
        vistas.append(" ".join((sql or "").split()).lower())
        return {"t": None}

    monkeypatch.setattr(ap_mod._db, "fetch_one", _fetch_one)
    valor, motivo = ap_mod.apertura_para_candado(10)
    assert valor is None
    assert "todavía no existe" in motivo
    assert len(vistas) == 1 and "to_regclass" in vistas[0], (
        "no se puede consultar la tabla sin haber preguntado si existe")


def test_el_wrapper_de_bank_helpers_no_deja_pasar_una_excepcion(monkeypatch):
    """Fail-soft: si la apertura no se puede leer, el candado vuelve a
    arrancar en la segunda fila. Nunca frena una carga por esto."""
    import bank_helpers as bh
    from modules.bancos import apertura as ap_mod

    def _explota(*a, **k):
        raise RuntimeError("la base se cayó")

    monkeypatch.setattr(ap_mod, "apertura_para_candado", _explota)
    assert bh.apertura_para_candado(None, no_banco=10) is None


# ── 3) el alta: los dos lados del mutante, sobre el fake ─────────────────

def test_el_alta_pasa_sin_ruido_con_la_cadena_sana(monkeypatch):
    import bank_helpers as bh

    fake = _instalar(monkeypatch, _ledger_sano())
    monkeypatch.setattr(bh, "apertura_para_candado", lambda c, *, no_banco: 900.0)

    r = bh.insert_movimiento_bancario(
        conn=object(), no_banco=10, no_cta=None, fecha=date(2026, 7, 2),
        documento="DE", importe=10, concepto="dep. de hoy")

    assert r["saldo_nuevo"] == 1058.0
    assert len(fake.filas) == 4
    assert bh.contar_quiebres(no_banco=10, apertura=900.0) == []


def test_el_alta_frena_cuando_la_primera_fila_esta_movida(monkeypatch):
    """⭐ Justo lo que hasta hoy el candado NO podía hacer.

    `desde_fecha` es la fecha del alta, así que el alta ve la primera fila
    cuando ésta cae DENTRO de su ventana: el caso típico es un banco cuyo
    ledger arranca el mismo día que se está cargando (un banco nuevo). Con la
    primera fila de hace meses, un alta de hoy no la mira — y está bien: un
    problema de junio no le puede frenar la carga de agosto a la oficina.
    """
    import bank_helpers as bh

    mismo_dia = date(2026, 7, 1)
    filas = [
        _fila(1, mismo_dia, "DE", 100, 4000.0, "primera del banco"),
        _fila(2, mismo_dia, "DE", 50, 4050.0, "1 ch.DMA"),
        _fila(3, mismo_dia, "ND", 2, 4048.0, "GS. cheq. CLR"),
    ]
    fake = _instalar(monkeypatch, filas)
    monkeypatch.setattr(bh, "apertura_para_candado", lambda c, *, no_banco: 900.0)

    with pytest.raises(bh.CadenaRotaError) as e:
        bh.insert_movimiento_bancario(
            conn=object(), no_banco=10, no_cta=None, fecha=mismo_dia,
            documento="DE", importe=10, concepto="dep. de hoy")
    assert "PRIMERA fila" in str(e.value)
    # el raise vive adentro del db.tx() del caller: rollback. Acá el fake no
    # tiene transacciones, pero el mensaje sí tiene que decirlo.
    assert "No se guardó nada" in str(e.value)
    assert len(fake.filas) == 4  # el fake ya la escribió; el rollback es del tx


def test_una_carga_mas_vieja_que_todo_se_reencadena_desde_la_apertura(monkeypatch):
    """⭐ EL FALSO POSITIVO QUE HUBO QUE TAPAR PARA PODER PRENDER ESTO.

    Cargar una fecha más vieja que todo lo cargado es legítimo y pasa. Pero
    `_saldo_previo` no encontraba ninguna fila anterior y devolvía **0**, así
    que el walk-forward re-estampaba el banco entero SIN la apertura: la
    columna quedaba corrida por esos 900 (en Pichincha, por 2.962.335,77) y el
    candado —que desde hoy sí mira la primera fila— rebotaba la carga.

    Ahora el walk arranca en la apertura afirmada, que es la plata que el
    banco tenía antes de la primera fila. La carga entra, la cadena queda
    coherente con la apertura, y el candado se calla.
    """
    import bank_helpers as bh

    fake = _instalar(monkeypatch, _ledger_sano())
    monkeypatch.setattr(bh, "apertura_para_candado", lambda c, *, no_banco: 900.0)

    r = bh.insert_movimiento_bancario(
        conn=object(), no_banco=10, no_cta=None, fecha=date(2026, 6, 1),
        documento="DE", importe=10, concepto="carga backdated")

    assert r["saldo_nuevo"] == 910.0, "900 de apertura + 10 que entraron"
    por_id = {f["id_transaccion"]: f["saldo"] for f in fake.filas}
    assert por_id[1] == 1010.0, "la que era primera se corrió los 10 que entraron"
    assert por_id[3] == 1058.0
    assert bh.contar_quiebres(no_banco=10, apertura=900.0) == []


def test_un_alta_de_hoy_no_mira_la_primera_fila_vieja(monkeypatch):
    """El recorte de `desde_fecha` sigue mandando: un problema de junio no le
    frena la carga de julio a la oficina. Si no, el candado no se podría
    prender hasta terminar de planchar la historia."""
    import bank_helpers as bh

    filas = _ledger_sano()
    filas[0]["saldo"] = 4000.0
    filas[1]["saldo"] = 4050.0
    filas[2]["saldo"] = 4048.0
    _instalar(monkeypatch, filas)
    monkeypatch.setattr(bh, "apertura_para_candado", lambda c, *, no_banco: 900.0)

    r = bh.insert_movimiento_bancario(
        conn=object(), no_banco=10, no_cta=None, fecha=date(2026, 7, 2),
        documento="DE", importe=10, concepto="dep. de hoy")
    assert r["saldo_nuevo"] == 4058.0


# ── 4) el health lo publica ──────────────────────────────────────────────

def test_el_health_dice_si_la_primera_fila_esta_mirada_o_no():
    """Va como STAT, no como alerta: un banco sin apertura afirmada no tiene
    nada malo hoy, tiene un chequeo de menos. Un ⚠ diario que nadie puede
    apagar entrena a ignorar el panel entero."""
    from modules.admin_dbase.health_audit_view import _evaluar_cadena

    stat, alerts = _evaluar_cadena(
        no_banco=32, nombre="INTERNACI", stored=3761.19, signed=0.0,
        derivado=3761.19, breaks=[], n_nulls=0, dias=120,
        apertura_candado=None,
        motivo_apertura="la apertura del banco 32 es de origen 'siembra'")
    assert stat["primera_fila_chequeada"] is False
    assert "siembra" in stat["primera_fila_por_que_no"]
    assert not alerts, "no chequear la primera fila NO es una alerta"

    stat2, _ = _evaluar_cadena(
        no_banco=10, nombre="PICHINCHA", stored=1000.0, signed=100.0,
        derivado=1000.0, breaks=[], n_nulls=0, dias=120,
        apertura_candado=2962335.77, motivo_apertura="afirmada")
    assert stat2["primera_fila_chequeada"] is True
    assert stat2["apertura_del_candado"] == 2962335.77
    assert stat2["primera_fila_por_que_no"] == ""


# ── 5) contra Postgres de verdad: EL MUTANTE ─────────────────────────────
#
# Todo lo de arriba corre sobre el fake. El candado, sin embargo, vive en una
# window function de SQL (`COALESCE(LAG(saldo) OVER (...), apertura)`): si la
# escribo mal, el fake no se entera. Estos dos la ejercen contra la base.
#
# Banco 78: NO se da de alta en `scintela.banco` a propósito. Así ninguna
# pantalla ni ningún otro test lo ve (todo lo que lista bancos sale de esa
# tabla), y este archivo no le cambia el paisaje al de al lado.

# ⭐ Tiene que ser < 90: por convención de la casa los códigos 90/91 (y de ahí
# para arriba) NO son bancos sino medios de cobranza —"DEP.PICH." es depositar
# DIRECTO en Pichincha, no otro banco— y el candado los deja afuera a propósito.
# Con un 78 este test medía el camino equivocado. TMT 2026-08-14.
BANCO_TEST = 78
PREFIJO = "CANDADO-APERT-TEST"
HOY = date.today()


def _limpiar_banco(conn) -> None:
    """Deja la base como estaba. Y crea la tabla si falta — ver abajo.

    `scintela.banco_apertura` NO nace de una migración: la crea
    `apertura._bootstrap()` la primera vez que alguien lee un saldo. Sobre una
    base virgen —que es como corre el CI— todavía no existe, y el DELETE de
    acá abajo reventaba con "relation does not exist" en la PRIMERA corrida y
    pasaba en la segunda. Ese es exactamente el fallo que no se ve corriendo
    el archivo solo dos veces seguidas. Se usa la MISMA DDL del módulo (no una
    copia) y sin la siembra, para no inventarle aperturas a los otros bancos.
    """
    from modules.bancos.apertura import _BOOTSTRAP_SQL
    cur = conn.cursor()
    cur.execute(_BOOTSTRAP_SQL)
    cur.execute("DELETE FROM scintela.transacciones_bancarias WHERE no_banco = %s",
                (BANCO_TEST,))
    cur.execute("DELETE FROM scintela.banco_apertura WHERE no_banco = %s",
                (BANCO_TEST,))
    conn.commit()


@pytest.fixture
def banco_limpio(real_db_conn, migrated_db):
    _limpiar_banco(real_db_conn)
    yield real_db_conn
    _limpiar_banco(real_db_conn)


def _afirmar_apertura(conn, valor, origen="afirmada") -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scintela.banco_apertura
               (no_banco, saldo_apertura, origen, nota, usuario)
        VALUES (%s, %s, %s, 'test', 'test')
         ON CONFLICT (no_banco) DO UPDATE
            SET saldo_apertura = EXCLUDED.saldo_apertura,
                origen = EXCLUDED.origen
        """,
        (BANCO_TEST, valor, origen),
    )
    conn.commit()


def _sembrar(conn) -> list[int]:
    """Tres movimientos de HOY, arrancando de una apertura afirmada de 900.

    Todos el MISMO día a propósito: así la primera fila del banco cae dentro
    de la ventana (`desde_fecha`) de la carga siguiente, que es el caso en que
    el alta puede verla — un banco cuyo ledger arranca hoy. Con la primera
    fila de hace meses, un alta de hoy no la mira, y está bien.
    """
    import bank_helpers
    import db
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scintela.transacciones_bancarias
               (fecha, documento, concepto, importe, saldo, no_banco, usuario_crea)
        VALUES (%s, 'DE', %s, 100, 1000, %s, 'test')
        RETURNING id_transaccion
        """,
        (HOY, f"{PREFIJO} primera", BANCO_TEST),
    )
    ids = [cur.fetchone()[0]]
    conn.commit()
    with db.tx() as c:
        for i, (doc, imp) in enumerate((("DE", 50), ("ND", 2))):
            r = bank_helpers.insert_movimiento_bancario(
                c, no_banco=BANCO_TEST, no_cta=None,
                fecha=HOY, documento=doc, importe=imp,
                concepto=f"{PREFIJO} {i}", usuario="test")
            ids.append(r["id_transaccion"])
    return ids


@pytest.mark.db
def test_db_el_candado_agarra_la_primera_fila_movida_a_mano(banco_limpio):
    """⭐ EL MUTANTE, EN LAS DOS DIRECCIONES.

    (a) cadena sana + apertura afirmada → el alta entra sin ruido;
    (b) la PRIMERA fila movida a mano → el candado la agarra, que es
        exactamente lo que hasta hoy no podía hacer, y no se guarda nada.
    """
    import bank_helpers
    import db
    conn = banco_limpio
    _afirmar_apertura(conn, 900)
    _sembrar(conn)

    ap = bank_helpers.apertura_para_candado(None, no_banco=BANCO_TEST)
    assert ap == 900.0, "la apertura afirmada tiene que llegar al candado"
    assert bank_helpers.contar_quiebres(
        no_banco=BANCO_TEST, desde_fecha=HOY, apertura=ap) == []

    # (a) con la cadena sana, el alta pasa sin ruido
    with db.tx() as c:
        r = bank_helpers.insert_movimiento_bancario(
            c, no_banco=BANCO_TEST, no_cta=None, fecha=HOY,
            documento="DE", importe=10, concepto=f"{PREFIJO} sana",
            usuario="test")
    assert r["saldo_nuevo"] == 1058.0

    cur = conn.cursor()
    cur.execute("DELETE FROM scintela.transacciones_bancarias WHERE concepto = %s",
                (f"{PREFIJO} sana",))
    conn.commit()

    # (b) muevo LA PRIMERA fila, la que hasta hoy nadie miraba
    cur.execute(
        "UPDATE scintela.transacciones_bancarias SET saldo = saldo + 3000 "
        " WHERE no_banco = %s AND concepto = %s",
        (BANCO_TEST, f"{PREFIJO} primera"),
    )
    conn.commit()

    sin_ap = bank_helpers.contar_quiebres(no_banco=BANCO_TEST, desde_fecha=HOY)
    assert [r["es_primera"] for r in sin_ap] == [False], (
        "sin apertura sólo se ve el rebote en la fila SIGUIENTE — la primera "
        "sigue invisible: ése era el punto ciego")

    con_ap = bank_helpers.contar_quiebres(
        no_banco=BANCO_TEST, desde_fecha=HOY, apertura=ap)
    assert con_ap[0]["es_primera"] is True
    assert float(con_ap[0]["saldo_prev"]) == 900.0

    with pytest.raises(bank_helpers.CadenaRotaError) as e, db.tx() as c:
        bank_helpers.insert_movimiento_bancario(
            c, no_banco=BANCO_TEST, no_cta=None, fecha=HOY,
            documento="DE", importe=10, concepto=f"{PREFIJO} post-mutante",
            usuario="test")
    assert "PRIMERA fila" in str(e.value)

    cur.execute(
        "SELECT COUNT(*) FROM scintela.transacciones_bancarias WHERE concepto = %s",
        (f"{PREFIJO} post-mutante",),
    )
    assert cur.fetchone()[0] == 0, "el raise tiene que haber hecho rollback"


@pytest.mark.db
def test_db_con_apertura_sembrada_el_banco_sigue_con_el_hueco(banco_limpio):
    """El hueco declarado, contra la base: con `origen='siembra'` el alta
    entra igual aunque la primera fila esté movida. No es un descuido — está
    escrito en `ORIGENES_QUE_AFIRMAN_LA_APERTURA` y el health lo publica."""
    import bank_helpers
    import db
    conn = banco_limpio
    _afirmar_apertura(conn, 900, origen="siembra")
    _sembrar(conn)

    cur = conn.cursor()
    cur.execute(
        "UPDATE scintela.transacciones_bancarias SET saldo = saldo + 3000 "
        " WHERE no_banco = %s AND concepto = %s",
        (BANCO_TEST, f"{PREFIJO} primera"),
    )
    cur.execute(
        "UPDATE scintela.transacciones_bancarias SET saldo = saldo + 3000 "
        " WHERE no_banco = %s AND concepto <> %s",
        (BANCO_TEST, f"{PREFIJO} primera"),
    )
    conn.commit()

    assert bank_helpers.apertura_para_candado(None, no_banco=BANCO_TEST) is None
    with db.tx() as c:
        r = bank_helpers.insert_movimiento_bancario(
            c, no_banco=BANCO_TEST, no_cta=None, fecha=HOY,
            documento="DE", importe=10, concepto=f"{PREFIJO} con siembra",
            usuario="test")
    assert r["id_transaccion"], (
        "con apertura sembrada el candado sigue arrancando en la segunda fila")


# ---------------------------------------------------------------------------
# Los códigos 90/91 no son bancos
# ---------------------------------------------------------------------------
# ⭐ TMT 2026-08-14 (Tamara): «depósito Pichincha no es un banco distinto, es que
# directamente se deposita en Pichincha». Son medios de cobranza del dropdown;
# el movimiento real cae en el banco 10 (`_banco_real_para_deposito`, paridad
# ALTAS.PRG L171). Un rubro de cobranza no tiene saldo corriente que cuidar, así
# que el candado no le inventa una cadena — y sobre todo el health no lo publica
# como "primera fila chequeada", que sería decir que está vigilado algo que no
# es una cuenta.


@pytest.mark.parametrize("virtual", [90, 91])
def test_los_codigos_virtuales_no_tienen_apertura_para_el_candado(virtual):
    from modules.bancos import apertura

    ap, motivo = apertura.apertura_para_candado(virtual)
    assert ap is None
    assert "medio de cobranza" in motivo
    assert "no una cuenta bancaria" in motivo


def test_el_corte_es_90_no_una_lista_de_codigos():
    """La convención `no_banco < 90` ya vive en cuatro lugares del código.

    Si mañana aparece un 92, tiene que quedar afuera solo — por eso el corte y
    no una lista de códigos conocidos.
    """
    from modules.bancos import apertura

    assert apertura.apertura_para_candado(92)[0] is None
