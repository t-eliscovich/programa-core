"""La explicación del día (TMT 2026-08-04).

Dueña: *"quiero agregar un check diario, quiero que cada día puedas darme una
explicación de por qué subió la utilidad. comparás balance de mañana y a fin de
día"*. Y, sobre el residuo, sin lugar a interpretación: *"no debería haber
residuo.. y tenemos que ir entrenando esto"*.

Lo que estos tests fijan:

  · **El invariante.** Δ utilidad = Σ aportes de los movimientos, al centavo.
    Es la promesa entera del sistema: si no cierra, la pantalla miente.
  · **El `#ajuste` no es cuadratura de conveniencia.** Aparece SÓLO cuando el
    detalle por documento no llega al componente, y queda marcado
    `sin_explicar` para que baje el % explicado. Un sistema que cuadra
    escondiendo lo que no entiende es peor que uno que no cuadra.
  · **Los pasivos aportan al revés.** Deuda que sube = utilidad que baja.
  · **El stock se parte en kilos y tarifa**, sin perder un centavo en el
    redondeo. Son dos noticias distintas y una de ellas (la tarifa) no produjo
    ni vendió nada.
  · **Las capturas ancla son idempotentes por la DB**, no por una variable de
    proceso: el server reinicia y la variable se pierde.
"""
from __future__ import annotations

import os
from datetime import date
from unittest.mock import patch

import pytest

from modules.informes import dia

# ── Helpers ─────────────────────────────────────────────────────────────────

def _bal(**comp) -> dict:
    base = {
        "utilidad": 100.0, "patr": 1000.0,
        "salcaj": 0.0, "salbanc_total": 0.0, "totc": 0.0, "totf": 0.0,
        "antic": 0.0, "vsto": 0.0, "vqx": 0.0, "umaq": 0.0, "uact": 0.0,
        "totp": 0.0, "uret": 0.0,
    }
    base.update(comp)
    return {"diagnostico": {"componentes": base}, "stock_etapas": {}}


def _foto(filas: list[dict]) -> dict:
    return {(f["componente"], f["doc_id"]): f for f in filas}


def _fila(comp, doc, imp, etiqueta="x", cantidad=None, precio=None) -> dict:
    return {"componente": comp, "doc_id": doc, "importe": imp,
            "etiqueta": etiqueta, "cantidad": cantidad, "precio": precio}


# ── El invariante ───────────────────────────────────────────────────────────

def test_delta_utilidad_es_la_suma_de_los_aportes():
    """La promesa entera: cada peso del Δ tiene un documento con nombre.

    Escenario de un día real: se cobró una factura de $1.000 con un cheque, se
    facturó una venta nueva de $2.000 y entró una deuda de $300.

        cobranza  → −1.000 (factura) + 1.000 (cheque) = 0   ← traspaso
        venta     → +2.000                                   ← utilidad
        deuda     → +300 de pasivo, que aporta −300          ← utilidad

    Δ utilidad esperado: +1.700.
    """
    vieja = _foto([
        _fila("facturas", "f1", 1000.0),
        _fila("totp", "p1", 500.0),
    ])
    nueva = [
        _fila("facturas", "f2", 2000.0),          # venta nueva
        _fila("cheques", "c1", 1000.0),           # el cheque de la cobranza
        _fila("totp", "p1", 500.0),               # la deuda vieja sigue
        _fila("totp", "p2", 300.0),               # deuda nueva
    ]
    movs = dia._diff(nueva, vieja)
    assert round(sum(m["aporte"] for m in movs), 2) == 1700.0

    # ...y cada pata está por separado, no netada de antemano.
    por_doc = {m["doc_id"]: m for m in movs}
    assert por_doc["f1"]["aporte"] == -1000.0
    assert por_doc["c1"]["aporte"] == 1000.0
    assert por_doc["f2"]["aporte"] == 2000.0
    assert por_doc["p2"]["aporte"] == -300.0      # pasivo: al revés


def test_pasivo_que_sube_baja_la_utilidad():
    movs = dia._diff([_fila("totp", "p9", 1000.0)], {})
    assert movs[0]["delta"] == 1000.0
    assert movs[0]["aporte"] == -1000.0


def test_pasivo_que_se_paga_sube_la_utilidad():
    movs = dia._diff([], _foto([_fila("totp", "p9", 1000.0)]))
    assert movs[0]["tipo"] == "baja"
    assert movs[0]["aporte"] == 1000.0


def test_el_ruido_de_centavos_no_genera_movimiento():
    vieja = _foto([_fila("facturas", "f1", 1000.00)])
    assert dia._diff([_fila("facturas", "f1", 1000.005)], vieja) == []


# ── El #ajuste: honesto, no cosmético ───────────────────────────────────────

def test_ajuste_aparece_cuando_el_detalle_no_llega_al_componente():
    """Si los documentos suman $800 y el balance dice $1.000, faltan $200 y
    tienen que quedar A LA VISTA, no repartidos ni escondidos."""
    with patch.object(dia, "_det_facturas", return_value=[
            {"doc_id": "f1", "etiqueta": "F1", "importe": 800.0}]), \
         patch.object(dia, "_det_cheques", return_value=[]), \
         patch.object(dia, "_det_caja", return_value=[]), \
         patch.object(dia, "_det_bancos", return_value=[]), \
         patch.object(dia, "_det_antic", return_value=[]), \
         patch.object(dia, "_det_totp", return_value=[]), \
         patch.object(dia, "_det_uret", return_value=[]), \
         patch.object(dia, "_det_activos", return_value=[]):
        det = dia.detalle(_bal(totf=1000.0))
    aj = [d for d in det if d["doc_id"] == "#ajuste:facturas"]
    assert len(aj) == 1
    assert aj[0]["importe"] == 200.0
    # Y la suma del componente cierra contra el balance.
    assert round(sum(d["importe"] for d in det if d["componente"] == "facturas"), 2) == 1000.0


def test_sin_ajuste_cuando_los_documentos_ya_explican_todo():
    with patch.object(dia, "_det_facturas", return_value=[
            {"doc_id": "f1", "etiqueta": "F1", "importe": 1000.0}]), \
         patch.object(dia, "_det_cheques", return_value=[]), \
         patch.object(dia, "_det_caja", return_value=[]), \
         patch.object(dia, "_det_bancos", return_value=[]), \
         patch.object(dia, "_det_antic", return_value=[]), \
         patch.object(dia, "_det_totp", return_value=[]), \
         patch.object(dia, "_det_uret", return_value=[]), \
         patch.object(dia, "_det_activos", return_value=[]):
        det = dia.detalle(_bal(totf=1000.0))
    assert not [d for d in det if str(d["doc_id"]).startswith("#ajuste")]


def test_el_ajuste_no_se_hace_pasar_por_explicado():
    """Un `#ajuste` tiene que salir marcado `sin_explicar`. Si se contara como
    explicado, la cuadratura taparía justamente lo que hay que entrenar."""
    movs = dia._diff([_fila("facturas", "#ajuste:facturas", 200.0)], {})
    assert movs[0]["familia"] == "sin_explicar"
    assert movs[0]["regla"] == "Sin explicar todavía"


def test_una_fuente_caida_no_tumba_la_captura():
    """Si Asinfo o formulas_app no contestan, el componente queda sin detalle
    pero el `#ajuste` lo absorbe entero y la foto igual se toma."""
    with patch.object(dia, "_det_facturas", side_effect=RuntimeError("Asinfo caído")), \
         patch.object(dia, "_det_cheques", return_value=[]), \
         patch.object(dia, "_det_caja", return_value=[]), \
         patch.object(dia, "_det_bancos", return_value=[]), \
         patch.object(dia, "_det_antic", return_value=[]), \
         patch.object(dia, "_det_totp", return_value=[]), \
         patch.object(dia, "_det_uret", return_value=[]), \
         patch.object(dia, "_det_activos", return_value=[]):
        det = dia.detalle(_bal(totf=5000.0))
    aj = [d for d in det if d["doc_id"] == "#ajuste:facturas"]
    assert aj and aj[0]["importe"] == 5000.0


# ── Stock: kilos vs tarifa ──────────────────────────────────────────────────

def test_el_stock_se_parte_en_kilos_y_tarifa_sin_perder_plata():
    """1.000 kg a $2 → 1.200 kg a $2,50.

        Δ total   = 3.000 − 2.000 = +1.000
        por kilos = (1200 − 1000) × 2,00 =   +400
        por tarifa=  1200 × (2,50 − 2,00) =  +600
    """
    vieja = _foto([_fila("vsto", "#hilado", 2000.0, "Stock hilado", 1000.0, 2.0)])
    nueva = [_fila("vsto", "#hilado", 3000.0, "Stock hilado", 1200.0, 2.5)]
    movs = dia._diff(nueva, vieja)
    assert round(sum(m["aporte"] for m in movs), 2) == 1000.0
    subs = {m["doc_id"]: m for m in movs}
    assert subs["#hilado:kilos"]["delta"] == 400.0
    assert subs["#hilado:tarifa"]["delta"] == 600.0


def test_stock_solo_tarifa_no_dice_que_entraron_kilos():
    """Una revaluación no produjo ni vendió nada. Si la pantalla dijera
    'entraron kilos' estaría mintiendo sobre la fábrica."""
    vieja = _foto([_fila("vsto", "#hilado", 2000.0, "Stock hilado", 1000.0, 2.0)])
    nueva = [_fila("vsto", "#hilado", 2100.0, "Stock hilado", 1000.0, 2.1)]
    movs = dia._diff(nueva, vieja)
    assert len(movs) == 1
    assert movs[0]["doc_id"] == "#hilado:tarifa"
    assert "$/kg" in movs[0]["regla"]


def test_la_particion_del_stock_no_pierde_centavos():
    """Con números feos el redondeo de las dos patas puede no dar el Δ exacto.
    Lo que sobra sale como 'resto' — jamás se descarta, porque descartarlo
    rompería el invariante."""
    vieja = _foto([_fila("vsto", "#hilado", 3333.33, "Stock hilado", 1111.11, 3.0)])
    nueva = [_fila("vsto", "#hilado", 3777.77, "Stock hilado", 1234.567, 3.0599)]
    movs = dia._diff(nueva, vieja)
    assert round(sum(m["delta"] for m in movs), 2) == round(3777.77 - 3333.33, 2)


# ── Reglas ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("comp", "tipo", "doc", "delta", "familia"), [
    ("facturas", "alta", "f1", 500.0, "utilidad"),      # venta
    ("facturas", "cambio", "f1", -500.0, "traspaso"),   # abono
    ("cheques", "alta", "c1", 500.0, "traspaso"),       # cobranza
    ("caja", "alta", "k1", -50.0, "utilidad"),          # gasto
    ("caja", "alta", "#apertura", 50.0, "traspaso"),
    ("totp", "alta", "p1", 500.0, "utilidad"),          # deuda nueva
    ("totp", "baja", "p1", -500.0, "traspaso"),         # deuda pagada
    ("umaq", "cambio", "a1", -12.0, "utilidad"),        # amortización
    ("uret", "alta", "r1", 900.0, "traspaso"),
    ("facturas", "alta", "#ajuste:facturas", 1.0, "sin_explicar"),
])
def test_familia_de_cada_regla(comp, tipo, doc, delta, familia):
    _nombre, fam = dia.regla(comp, tipo, doc, delta)
    assert fam == familia
    assert fam in dia.FAMILIAS


def test_la_amortizacion_diaria_tiene_nombre_propio():
    """La maquinaria baja sola todos los días (MENU.PRG prorratea por día del
    mes). Sin un nombre, cada mañana parece una pérdida misteriosa."""
    nombre, _ = dia.regla("umaq", "cambio", "a1", -37.5)
    assert nombre == "Amortización del día"


def test_una_cobranza_netea_cero_sin_emparejar_documentos():
    """Nadie matchea el cheque con la factura: cada pata aporta con el signo de
    su componente y la suma da cero sola."""
    movs = dia._diff(
        [_fila("cheques", "c1", 1000.0)],
        _foto([_fila("facturas", "f1", 1000.0)]),
    )
    assert round(sum(m["aporte"] for m in movs), 2) == 0.0


def test_una_cobranza_con_retencion_no_netea_y_esa_es_la_noticia():
    """La factura baja $1.000 pero sólo entran $970 de cheque: los $30 de
    retención son exactamente la pérdida del día."""
    movs = dia._diff(
        [_fila("cheques", "c1", 970.0)],
        _foto([_fila("facturas", "f1", 1000.0)]),
    )
    assert round(sum(m["aporte"] for m in movs), 2) == -30.0


# ── Horarios: Ecuador, no el server ─────────────────────────────────────────

def test_antes_de_las_siete_no_captura():
    with patch.object(dia, "ahora_ec") as a:
        a.return_value.hour = 6
        assert dia.correr_si_toca()["motivo"] == "todavía no son las 7"


def test_a_las_ocho_captura_la_de_la_manana():
    with patch.object(dia, "ahora_ec") as a, \
         patch.object(dia, "hoy_ec", return_value=date(2026, 8, 4)), \
         patch.object(dia, "_rows", return_value=[]), \
         patch.object(dia, "capturar", return_value={"ok": True, "movimientos": 3}) as cap:
        a.return_value.hour = 8
        assert dia.correr_si_toca()["capturado"] == "manana"
        cap.assert_called_once_with("manana")


def test_a_las_veinte_captura_el_cierre_primero():
    """El cierre va primero: si el server estuvo caído todo el día y arranca a
    las 20:00, la captura que importa es la del cierre."""
    with patch.object(dia, "ahora_ec") as a, \
         patch.object(dia, "hoy_ec", return_value=date(2026, 8, 4)), \
         patch.object(dia, "_rows", return_value=[]), \
         patch.object(dia, "capturar", return_value={"ok": True, "movimientos": 0}) as cap:
        a.return_value.hour = 20
        assert dia.correr_si_toca()["capturado"] == "cierre"
        cap.assert_called_once_with("cierre")


def test_no_repite_la_captura_que_ya_esta_hecha():
    with patch.object(dia, "ahora_ec") as a, \
         patch.object(dia, "hoy_ec", return_value=date(2026, 8, 4)), \
         patch.object(dia, "_rows", return_value=[{"momento": "manana"}]), \
         patch.object(dia, "capturar") as cap:
        a.return_value.hour = 10
        dia.correr_si_toca()
        cap.assert_not_called()


def test_se_puede_apagar_por_env():
    with patch.dict(os.environ, {"DIA_EXPLICACION": "0"}):
        assert dia.correr_si_toca()["motivo"] == "apagado"


def test_la_hora_de_ecuador_no_es_la_del_server():
    """El server corre en UTC. A las 23:00 UTC en Ecuador son las 18:00 del
    mismo día — si se usara la hora del server, el cierre saldría un día
    corrido y compararía contra la foto equivocada."""
    from datetime import UTC, datetime
    with patch("modules.informes.dia.datetime") as dt:
        dt.now.return_value = datetime(2026, 8, 4, 23, 0, tzinfo=UTC)
        assert dia.ahora_ec().hour == 18
        assert dia.hoy_ec() == date(2026, 8, 4)


# ── La explicación ──────────────────────────────────────────────────────────

def _caps(u0, u1, **comp):
    c0 = {"id_captura": 1, "hora": "19:00", "utilidad": u0, "nota": None,
          "fecha_ec": date(2026, 8, 3)}
    c1 = {"id_captura": 2, "hora": "19:00", "utilidad": u1, "nota": None,
          "fecha_ec": date(2026, 8, 4)}
    for c, _s in dia.COMPONENTES:
        c0[c] = 0.0
        c1[c] = comp.get(c, 0.0)
    return [c0, c1]


def test_explicar_sin_con_que_comparar_lo_dice_en_castellano():
    """Una sola foto y ninguna de ayer: no hay ventana."""
    caps = [{"id_captura": 1, "hora": "07:00", "utilidad": 10.0}]
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "_rows", return_value=[]):
        e = dia.explicar(date(2026, 8, 4))
    assert e["hasta"] is None
    assert "ayer" in e["motivo"]
    assert e["desde"]["hora"] == "07:00"


def test_la_ventana_es_de_24_horas_contra_el_cierre_de_ayer():
    """⭐ TMT 2026-08-05: "se produce 24/7". Comparar 07:00→19:00 dejaba fuera
    el turno noche y hacía parecer que la fábrica producía la mitad."""
    ayer = {"id_captura": 9, "hora": "19:00", "fecha_ec": date(2026, 8, 3)}
    hoy = [{"id_captura": 10, "hora": "07:00", "fecha_ec": date(2026, 8, 4)},
           {"id_captura": 11, "hora": "19:00", "fecha_ec": date(2026, 8, 4)}]
    with patch.object(dia, "capturas", return_value=hoy), \
         patch.object(dia, "_rows", return_value=[ayer]):
        d, h = dia.ventana(date(2026, 8, 4))
    assert d["id_captura"] == 9      # el cierre de AYER, no la mañana de hoy
    assert h["id_captura"] == 11


def test_sin_foto_de_ayer_el_tramo_queda_marcado_como_parcial():
    """Sin la punta de ayer el tramo es más corto que un día y no se puede
    comparar contra otros días. Se avisa en vez de disimularlo."""
    hoy = [{"id_captura": 1, "hora": "07:00", "fecha_ec": date(2026, 8, 4), "utilidad": 0.0},
           {"id_captura": 2, "hora": "19:00", "fecha_ec": date(2026, 8, 4), "utilidad": 100.0}]
    for c, _s in dia.COMPONENTES:
        hoy[0][c] = 0.0
        hoy[1][c] = 0.0
    with patch.object(dia, "capturas", return_value=hoy), \
         patch.object(dia, "_rows", side_effect=[[], []]):
        e = dia.explicar(date(2026, 8, 4))
    assert e["dia_parcial"] is True
    assert e["d_utilidad"] == 100.0


def test_explicar_cierra_sin_residuo_y_marca_el_cien_por_ciento():
    movs = [
        {"aporte": 2000.0, "familia": "utilidad", "regla": "Venta facturada",
         "componente": "facturas"},
        {"aporte": -300.0, "familia": "utilidad", "regla": "Deuda nueva cargada",
         "componente": "totp"},
        {"aporte": -1000.0, "familia": "traspaso", "regla": "Abono a factura",
         "componente": "facturas"},
        {"aporte": 1000.0, "familia": "traspaso", "regla": "Cheque recibido",
         "componente": "cheques"},
    ]
    caps = _caps(0.0, 1700.0, facturas=1000.0)
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "ventana", return_value=(caps[0], caps[1])), \
         patch.object(dia, "_rows", return_value=movs):
        e = dia.explicar(date(2026, 8, 4))
    assert e["d_utilidad"] == 1700.0
    assert e["residuo"] == 0.0
    assert e["explicado_pct"] == 100.0
    assert e["ok"] is True
    assert e["sin_explicar"] == []
    # Los traspasos netean cero y quedan visibles como familia.
    fam = {f["familia"]: f["aporte"] for f in e["familias"]}
    assert fam["traspaso"] == 0.0
    assert fam["utilidad"] == 1700.0


def test_explicar_baja_el_porcentaje_cuando_hay_algo_sin_explicar():
    movs = [
        {"aporte": 900.0, "familia": "utilidad", "regla": "Venta facturada",
         "componente": "facturas"},
        {"aporte": 100.0, "familia": "sin_explicar", "regla": "Sin explicar todavía",
         "componente": "vqx", "etiqueta": "Químicos: sin explicar"},
    ]
    caps = _caps(0.0, 1000.0)
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "ventana", return_value=(caps[0], caps[1])), \
         patch.object(dia, "_rows", return_value=movs):
        e = dia.explicar(date(2026, 8, 4))
    assert e["residuo"] == 0.0          # la cuenta cierra igual...
    assert e["explicado_pct"] == 90.0   # ...pero no se hace la desentendida
    assert e["ok"] is False
    assert len(e["sin_explicar"]) == 1


def test_explicar_denuncia_el_descuadre_si_lo_hubiera():
    """Si el Δ de la utilidad no coincide con la suma de los movimientos hay un
    bug. Tiene que salir en la pantalla, no quedar tapado."""
    caps = _caps(0.0, 5000.0)
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "ventana", return_value=(caps[0], caps[1])), \
         patch.object(dia, "_rows", return_value=[
             {"aporte": 1000.0, "familia": "utilidad", "regla": "Venta facturada",
              "componente": "facturas"}]):
        e = dia.explicar(date(2026, 8, 4))
    assert e["residuo"] == 4000.0
    assert e["ok"] is False


def test_los_componentes_salen_ordenados_por_cuanto_pesaron():
    caps = _caps(0.0, 0.0, facturas=100.0, totp=5000.0, caja=-20.0)
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "ventana", return_value=(caps[0], caps[1])), \
         patch.object(dia, "_rows", return_value=[]):
        e = dia.explicar(date(2026, 8, 4))
    assert [c["col"] for c in e["componentes"]] == ["totp", "facturas", "caja"]
    # El pasivo entra con el signo dado vuelta.
    assert e["componentes"][0]["delta"] == 5000.0
    assert e["componentes"][0]["aporte"] == -5000.0


def test_racha_limpia_se_corta_en_el_primer_dia_con_algo_sin_explicar():
    with patch.object(dia, "_rows", return_value=[
            {"fecha_ec": date(2026, 8, 3), "ciegos": 0},
            {"fecha_ec": date(2026, 8, 2), "ciegos": 0},
            {"fecha_ec": date(2026, 8, 1), "ciegos": 4},
            {"fecha_ec": date(2026, 7, 31), "ciegos": 0}]):
        assert dia.racha_limpia() == 2


# ── Contra Postgres de verdad ───────────────────────────────────────────────
# El diff en memoria es una cosa; el round-trip (insertar movimientos, pisar la
# foto rodante, que el índice único frene la segunda captura del mismo momento)
# es otra, y es donde una migración mal escrita se nota. Corre solo si hay una
# DB a mano: `DIA_TEST_DSN=postgresql://... pytest tests/test_dia_explicacion.py`

_DSN = os.environ.get("DIA_TEST_DSN", "")
sin_db = pytest.mark.skipif(not _DSN, reason="sin DIA_TEST_DSN")


@pytest.fixture()
def pg():
    import psycopg2
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE scintela.dia_captura, scintela.dia_movimiento, "
                    "scintela.dia_detalle RESTART IDENTITY CASCADE")
    yield conn
    conn.close()


@pytest.fixture()
def db_real(pg, monkeypatch):
    """Apunta `db` del módulo a la DB de prueba, respetando conn= y tx()."""
    import contextlib

    import psycopg2.extras

    def fetch_all(sql, params=None, conn=None):
        c = conn or pg
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]

    def execute(sql, params=None, conn=None):
        c = conn or pg
        with c.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.rowcount

    def execute_returning(sql, params=None, conn=None):
        c = conn or pg
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            r = cur.fetchone()
            return dict(r) if r else None

    @contextlib.contextmanager
    def tx():
        yield pg

    monkeypatch.setattr(dia.db, "fetch_all", fetch_all)
    monkeypatch.setattr(dia.db, "execute", execute)
    monkeypatch.setattr(dia.db, "execute_returning", execute_returning)
    monkeypatch.setattr(dia.db, "tx", tx)
    return pg


@sin_db
def test_db_captura_diffea_y_pisa_la_foto_rodante(db_real):
    """Dos capturas seguidas: la segunda tiene que ver lo que cambió respecto
    de la primera, y la foto rodante tiene que quedar con el estado nuevo."""
    with patch.object(dia, "detalle", return_value=[]):
        dia.capturar("manual", bal=_bal())          # la primera no diffea nunca
    with patch.object(dia, "detalle", return_value=[
            _fila("facturas", "f1", 1000.0, "Factura 1")]):
        r1 = dia.capturar("manana", bal=_bal(utilidad=0.0, totf=1000.0))
    assert r1["ok"] and r1["movimientos"] == 1

    with patch.object(dia, "detalle", return_value=[
            _fila("facturas", "f2", 3000.0, "Factura 2")]):
        r2 = dia.capturar("cierre", bal=_bal(utilidad=2000.0, totf=3000.0))
    assert r2["ok"]

    movs = dia._rows("SELECT * FROM scintela.dia_movimiento WHERE id_captura = %s",
                     (r2["id_captura"],))
    por_doc = {m["doc_id"]: m for m in movs}
    assert float(por_doc["f1"]["aporte"]) == -1000.0   # se fue
    assert float(por_doc["f2"]["aporte"]) == 3000.0    # entró
    assert round(sum(float(m["aporte"]) for m in movs), 2) == 2000.0

    foto = dia._rows("SELECT doc_id FROM scintela.dia_detalle")
    assert [f["doc_id"] for f in foto] == ["f2"]


@sin_db
def test_db_no_hay_dos_capturas_del_mismo_momento(db_real):
    """El hilo de fondo pasa cada 2 minutos: entre las 19:00 y la medianoche
    llama 150 veces. Tiene que entrar UNA — y la guarda es el índice único, no
    una variable de proceso, porque el server reinicia."""
    with patch.object(dia, "detalle", return_value=[]):
        a = dia.capturar("cierre", bal=_bal())
        b = dia.capturar("cierre", bal=_bal())
    assert a["ok"] is True
    assert b["ok"] is False
    assert b["motivo"] == "ya había captura de este momento"
    n = dia._rows("SELECT COUNT(*) AS n FROM scintela.dia_captura")[0]["n"]
    assert n == 1


@sin_db
def test_db_las_manuales_no_estan_limitadas(db_real):
    """El índice único es parcial a propósito: capturar a mano dos veces
    seguidas para mirar algo tiene que poder hacerse."""
    with patch.object(dia, "detalle", return_value=[]):
        assert dia.capturar("manual", bal=_bal())["ok"]
        assert dia.capturar("manual", bal=_bal())["ok"]


@sin_db
def test_db_el_invariante_sobrevive_al_round_trip(db_real):
    """El test que resume todo: guardar y volver a leer no puede cambiar el
    número. Δ utilidad = Σ aportes, leído de la base."""
    with patch.object(dia, "detalle", return_value=[
            _fila("facturas", "f1", 1000.0, "Factura 1"),
            _fila("totp", "p1", 500.0, "Deuda 1")]):
        dia.capturar("manana", bal=_bal(utilidad=0.0, totf=1000.0, totp=500.0))
    with patch.object(dia, "detalle", return_value=[
            _fila("facturas", "f1", 0.0, "Factura 1"),
            _fila("cheques", "c1", 1000.0, "Cheque 1"),
            _fila("facturas", "f2", 2000.0, "Factura 2"),
            _fila("totp", "p1", 500.0, "Deuda 1"),
            _fila("totp", "p2", 300.0, "Deuda 2")]):
        dia.capturar("cierre", bal=_bal(utilidad=1700.0, totf=2000.0, totc=1000.0,
                                        totp=800.0))
    e = dia.explicar(dia.hoy_ec())
    assert e["d_utilidad"] == 1700.0
    assert e["residuo"] == 0.0
    assert e["ok"] is True


# ── La pantalla ─────────────────────────────────────────────────────────────
# Jinja falla en runtime, no en import: sin un render de verdad, un typo en el
# template se descubre en producción. Estos tests lo renderizan entero.

def _login(app, fake_db, permisos=("informes.ver",)):
    rid = fake_db.add_role("Informes", list(permisos))
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _explicado(**over):
    e = {
        "fecha": date(2026, 8, 4), "capturas": [], "ok": True, "motivo": "",
        "desde": {"id_captura": 1, "hora": "07:00", "utilidad": 100.0, "nota": None},
        "hasta": {"id_captura": 2, "hora": "19:00", "utilidad": 1800.0, "nota": None},
        "d_utilidad": 1700.0,
        "componentes": [{"col": "totp", "label": "Pasivos", "delta": 300.0,
                         "aporte": -300.0},
                        {"col": "facturas", "label": "Facturas", "delta": 1000.0,
                         "aporte": 1000.0}],
        "movimientos": [{"componente": "facturas", "tipo": "alta", "doc_id": "f2",
                         "etiqueta": "Factura 2 · ABC", "regla": "Venta facturada",
                         "delta": 2000.0, "aporte": 2000.0, "familia": "utilidad"}],
        "familias": [{"familia": "utilidad", "aporte": 1700.0, "n": 2}],
        "reglas": [{"regla": "Venta facturada", "familia": "utilidad",
                    "aporte": 2000.0, "n": 1}],
        "sin_explicar": [], "residuo": 0.0, "explicado_pct": 100.0,
    }
    e.update(over)
    return e


def test_la_pantalla_del_dia_renderiza(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(dia, "explicar", return_value=_explicado()), \
         patch.object(dia, "racha_limpia", return_value=3):
        r = c.get("/informes/dia")
    assert r.status_code == 200, r.data[:400]
    cuerpo = r.data.decode()
    assert "Venta facturada" in cuerpo
    assert "explicado al 100" in cuerpo
    assert "3 días seguidos" in cuerpo


def test_la_pantalla_muestra_lo_que_falta_explicar(app, fake_db):
    """Lo que no se entiende tiene que estar en la cara, no en un log."""
    c = _login(app, fake_db)
    e = _explicado(ok=False, explicado_pct=88.0, sin_explicar=[
        {"componente": "vqx", "etiqueta": "Stock Químicos: sin explicar por documento",
         "aporte": -240.0, "delta": -240.0, "regla": "Sin explicar todavía",
         "familia": "sin_explicar", "tipo": "cambio", "doc_id": "#ajuste:vqx"}])
    with patch.object(dia, "explicar", return_value=e), \
         patch.object(dia, "racha_limpia", return_value=0):
        r = c.get("/informes/dia")
    cuerpo = r.data.decode()
    assert r.status_code == 200
    assert "Falta explicar" in cuerpo
    assert "Stock Químicos: sin explicar" in cuerpo
    assert "explicado 88.0" in cuerpo


def test_la_pantalla_avisa_cuando_todavia_no_hay_cierre(app, fake_db):
    c = _login(app, fake_db)
    e = _explicado(hasta=None, motivo="Todavía no hay con qué comparar: falta la "
                                      "captura de las 19:00.")
    with patch.object(dia, "explicar", return_value=e), \
         patch.object(dia, "racha_limpia", return_value=0):
        r = c.get("/informes/dia")
    assert r.status_code == 200
    assert "falta la captura" in r.data.decode()


def test_la_fecha_invalida_no_rompe_la_pantalla(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(dia, "explicar", return_value=_explicado()) as ex, \
         patch.object(dia, "racha_limpia", return_value=0), \
         patch.object(dia, "hoy_ec", return_value=date(2026, 8, 4)):
        assert c.get("/informes/dia?fecha=no-es-fecha").status_code == 200
    assert ex.call_args[0][0] == date(2026, 8, 4)


def test_sin_permiso_la_pantalla_no_existe(app, fake_db):
    """El repo gatea por operación y devuelve 404 (no 403) a propósito."""
    c = _login(app, fake_db, permisos=("cheques.ver",))
    assert c.get("/informes/dia").status_code == 404


# ── El arranque y el volumen ────────────────────────────────────────────────

@sin_db
def test_db_la_primera_captura_no_inventa_un_dia_gigante(db_real):
    """Sin foto anterior no hay con qué comparar. Si se diffeara contra la
    nada, cada factura viva de la cartera saldría como 'venta de hoy' y el
    primer día mostraría un Δ del tamaño del balance entero."""
    with patch.object(dia, "detalle", return_value=[
            _fila("facturas", "f1", 1000.0, "Factura 1"),
            _fila("facturas", "f2", 2000.0, "Factura 2")]):
        r = dia.capturar("manana", bal=_bal(totf=3000.0))
    assert r["ok"] and r["primera"] is True
    assert r["movimientos"] == 0
    # ...pero la foto SÍ queda guardada: mañana ya hay contra qué comparar.
    assert len(dia._rows("SELECT doc_id FROM scintela.dia_detalle")) == 2


@sin_db
def test_db_la_segunda_captura_ya_diffea(db_real):
    with patch.object(dia, "detalle", return_value=[_fila("facturas", "f1", 1000.0)]):
        dia.capturar("manana", bal=_bal(totf=1000.0))
    with patch.object(dia, "detalle", return_value=[_fila("facturas", "f1", 1500.0)]):
        r = dia.capturar("cierre", bal=_bal(totf=1500.0))
    assert r["primera"] is False
    assert r["movimientos"] == 1


@sin_db
def test_db_una_cartera_grande_entra_en_lotes(db_real):
    """La cartera viva son miles de documentos. Una fila por INSERT dejaría la
    transacción abierta demasiado tiempo en el hilo de fondo de producción."""
    grande = [_fila("facturas", f"f{i}", 10.0, f"Factura {i}") for i in range(1200)]
    with patch.object(dia, "detalle", return_value=[]):
        # Semilla: la foto queda VACÍA a propósito. "Primera" se decide por si
        # hubo alguna captura, no por si la foto tiene filas — si no, una foto
        # legítimamente vacía haría descartar la captura siguiente entera.
        dia.capturar("manana", bal=_bal())
    with patch.object(dia, "detalle", return_value=grande):
        r = dia.capturar("cierre", bal=_bal(totf=12000.0))
    assert r["ok"] and r["movimientos"] == 1200
    assert dia._rows("SELECT COUNT(*) AS n FROM scintela.dia_detalle")[0]["n"] == 1200
    assert dia._rows("SELECT COUNT(*) AS n FROM scintela.dia_movimiento")[0]["n"] == 1200


# ── El resumen para accionistas ─────────────────────────────────────────────
# TMT 2026-08-05: *"se produjo tanta tela, eso cambió de valor de x a x. se
# vendió tanto etc. algo más senior resumido digestible para accionistas"*.
#
# El desglose contable contesta "¿de dónde salió cada peso?". Esto contesta
# "¿qué pasó en la fábrica?", que es otra pregunta y se contesta en KILOS.

def _cap(u, **k):
    d = {"utilidad": u, "vsto": 0.0, "facturas": 0.0, "totp": 0.0,
         "hilado_kg": None, "hilado_ukg": None, "tejido_kg": None,
         "tejido_ukg": None, "terminado_kg": None, "terminado_ukg": None}
    d.update(k)
    return d


def _prod(producido=12837.75, vendido=16337.85, desp=680.0, pct=5.03, ofs=79):
    return {"disponible": True,
            "dias": {"filas": [{"periodo": "2026-08-04", "producido": producido,
                                "vendido": vendido, "desperdicio_kg": desp,
                                "desperdicio_pct": pct, "n_ofs": ofs,
                                "inicial": 330616.43, "final": 325233.93,
                                "otros": -1882.4},
                               {"periodo": "2026-08-03", "producido": 17984.7,
                                "vendido": 15894.55, "desperdicio_kg": 1048.42,
                                "desperdicio_pct": 5.51, "n_ofs": 92,
                                "inicial": 334518.38, "final": 330616.43,
                                "otros": -5992.1},
                               {"periodo": "2026-08-01", "producido": 0.0,
                                "vendido": 0.0, "desperdicio_kg": 0.0,
                                "desperdicio_pct": None, "n_ofs": 0,
                                "inicial": 317784.93, "final": 323891.18,
                                "otros": 6106.25}],
                     "total": {"producido": 30822.45, "vendido": 32992.1,
                               "desperdicio_kg": 1728.42, "n_ofs": 171}}}


def test_la_produccion_se_MIDE_no_se_deriva_del_stock():
    """⭐ TMT 2026-08-05: *"se produce 24/7, algo no sabés"*.

    La versión anterior hacía `Δ kg terminado + kg vendidos` y el 04/08 daba
    5.280 kg contra 12.838 reales — un error de 2,4×. Ahora sale de las
    órdenes de fabricación que cerraron, que es donde está medido.
    """
    with patch("modules.terminado_asinfo.service.resumen", return_value=_prod()):
        p = dia.produccion_del_dia(date(2026, 8, 4))
    assert p["disponible"] is True
    assert p["producido"] == 12837.75
    assert p["despachado"] == 16337.85
    assert p["n_ofs"] == 79


def test_la_produccion_trae_el_desperdicio():
    """~5% del terminado se evapora entre las dos puntas. Ni el stock ni las
    ventas lo muestran; si no viene explícito, no existe."""
    with patch("modules.terminado_asinfo.service.resumen", return_value=_prod()):
        p = dia.produccion_del_dia(date(2026, 8, 4))
    assert p["desperdicio_kg"] == 680.0
    assert p["desperdicio_pct"] == 5.03


def test_el_promedio_ignora_los_dias_sin_ordenes_cerradas():
    """Los kg se imputan al día en que la orden CIERRA. Un día en cero casi
    siempre significa "no cerró ninguna orden", no "no se trabajó": meterlo en
    el promedio lo hunde sin significar nada.

    Días con producción: 12.837,75 y 17.984,70 → promedio 15.411,23.
    """
    with patch("modules.terminado_asinfo.service.resumen", return_value=_prod()):
        p = dia.produccion_del_dia(date(2026, 8, 4))
    assert p["dias_con_produccion"] == 2
    assert p["promedio_dia"] == pytest.approx(15411.23, abs=0.01)


def test_sin_asinfo_la_produccion_no_inventa_un_cero():
    """Un cero se lee como "no se produjo nada", que es otra cosa que "no sé"."""
    with patch("modules.terminado_asinfo.service.resumen",
               side_effect=RuntimeError("Metabase caído")):
        assert dia.produccion_del_dia(date(2026, 8, 4)) == {"disponible": False}


def test_un_dia_sin_ordenes_cerradas_se_distingue_de_uno_sin_datos():
    with patch("modules.terminado_asinfo.service.resumen", return_value=_prod()):
        p = dia.produccion_del_dia(date(2026, 8, 2))     # no está en las filas
    assert p["disponible"] is True and p["sin_fila"] is True
    assert p["mes"]["producido"] == 30822.45


def test_resumen_estima_lo_cobrado():
    """cobrado ≈ facturado − Δ cartera. Se facturó 100.000 y la cartera sólo
    subió 40.000 → entraron 60.000."""
    caps = [_cap(0.0, facturas=1000000.0), _cap(0.0, facturas=1040000.0)]
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "produccion_del_dia", return_value={"disponible": False}), \
         patch.object(dia, "ventas_del_dia", return_value={"n": 5, "kg": 0.0, "us": 100000.0}), \
         patch.object(dia, "compras_del_dia", return_value={"n": 0, "kg": 0.0, "us": 0.0}):
        r = dia.resumen(date(2026, 8, 4))
    assert r["cobrado"] == 60000.0


def test_resumen_separa_produccion_de_revaluacion():
    """LA distinción que le importa a un accionista: la tela vale más porque
    se produjo, o porque cambió el costo del hilado.

    1.000 kg a $3 → 1.200 kg a $3,50:
        por kilos  = 200 × 3,00    = +600
        por tarifa = 1200 × 0,50   = +600
    El valor subió 1.200 pero la mitad no la produjo nadie.
    """
    caps = [_cap(0.0, hilado_kg=1000.0, hilado_ukg=3.0),
            _cap(0.0, hilado_kg=1200.0, hilado_ukg=3.5)]
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "produccion_del_dia", return_value={"disponible": False}), \
         patch.object(dia, "ventas_del_dia", return_value={"n": 0, "kg": 0.0, "us": 0.0}), \
         patch.object(dia, "compras_del_dia", return_value={"n": 0, "kg": 0.0, "us": 0.0}):
        r = dia.resumen(date(2026, 8, 4))
    assert r["por_kilos"] == 600.0
    assert r["por_tarifa"] == 600.0
    assert r["tarifa_quieta"] is False


def test_resumen_avisa_cuando_la_tarifa_no_se_movio():
    """El caso limpio: si el $/kg quedó quieto, TODO el cambio de stock son
    kilos de verdad. Vale decirlo — es lo que deja leer el número sin asterisco."""
    caps = [_cap(0.0, hilado_kg=1000.0, hilado_ukg=3.0437),
            _cap(0.0, hilado_kg=1200.0, hilado_ukg=3.0437)]
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "produccion_del_dia", return_value={"disponible": False}), \
         patch.object(dia, "ventas_del_dia", return_value={"n": 0, "kg": 0.0, "us": 0.0}), \
         patch.object(dia, "compras_del_dia", return_value={"n": 0, "kg": 0.0, "us": 0.0}):
        r = dia.resumen(date(2026, 8, 4))
    assert r["tarifa_quieta"] is True
    assert r["por_tarifa"] == 0.0


def test_resumen_sin_kilos_guardados_no_inventa():
    """Las capturas anteriores a la mig 0162 no tienen los kilos. El resumen
    tiene que salir sin la parte de producción, no con ceros —un cero dice
    'no se produjo nada', que es una afirmación falsa."""
    caps = [_cap(0.0), _cap(500.0)]
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "produccion_del_dia", return_value={"disponible": False}), \
         patch.object(dia, "ventas_del_dia", return_value={"n": 1, "kg": 10.0, "us": 100.0}), \
         patch.object(dia, "compras_del_dia", return_value={"n": 0, "kg": 0.0, "us": 0.0}):
        r = dia.resumen(date(2026, 8, 4))
    assert r["ok"] is True
    assert r["etapas"] == []
    assert r["tarifa_quieta"] is None
    # Sin la tarifa del terminado tampoco hay margen: se omite la clave en vez
    # de publicar un 0, que se leería como "vendimos sin ganar nada".
    assert "margen" not in r


def test_resumen_sin_dos_capturas_no_dice_nada():
    with patch.object(dia, "capturas", return_value=[]):
        assert dia.resumen(date(2026, 8, 4))["ok"] is False


def test_las_ventas_del_dia_excluyen_anuladas():
    with patch.object(dia, "_rows", return_value=[{"n": 3, "kg": 10, "us": 100}]) as rows:
        v = dia.ventas_del_dia(date(2026, 8, 4))
    sql = rows.call_args[0][0]
    assert "NOT IN ('X', 'Y')" in sql
    # Por fecha del DOCUMENTO: `fecha_crea` la pisa el sync del dBase.
    assert "WHERE fecha = %s" in sql and "fecha_crea" not in sql
    assert v == {"n": 3, "kg": 10.0, "us": 100.0}


def test_la_pantalla_muestra_el_resumen(app, fake_db):
    c = _login(app, fake_db)
    res = {
        "fecha": date(2026, 8, 4), "ok": True,
        "desde": {"vsto": 8538696.0}, "hasta": {"vsto": 8468758.0},
        "d_utilidad": 23268.0, "d_stock": -69938.0,
        "por_kilos": -69938.0, "por_tarifa": 0.0, "tarifa_quieta": True,
        "d_cartera": 76523.0, "d_deuda": 43007.0, "cobrado": 39707.0,
        "ventas": {"n": 113, "kg": 13565.0, "us": 116230.0},
        "compras": {"n": 2, "kg": 5000.0, "us": 15000.0},
        "precio_kg": 8.57, "margen": 52698.0, "margen_pct": 38.1,
        "costo_despachado": 85671.0,
        "produccion": {"disponible": True, "sin_fila": False,
                       "producido": 12837.75, "despachado": 16337.85,
                       "desperdicio_kg": 680.0, "desperdicio_pct": 5.03,
                       "n_ofs": 79, "promedio_dia": 15411.23,
                       "dias_con_produccion": 2,
                       "mes": {"producido": 30822.45, "vendido": 32992.1}},
        "etapas": [{"clave": "hilado", "rotulo": "Hilado", "kg0": 1899100.0,
                    "kg1": 1880000.0, "d_kg": -19100.0, "p0": 3.0437,
                    "p1": 3.0437, "d_p": 0.0, "us0": 5780000.0,
                    "us1": 5721000.0, "d_us": -59000.0,
                    "por_kilos": -59000.0, "por_tarifa": 0.0}],
    }
    with patch.object(dia, "explicar", return_value=_explicado()), \
         patch.object(dia, "resumen", return_value=res), \
         patch.object(dia, "racha_limpia", return_value=0):
        cuerpo = c.get("/informes/dia").data.decode()
    assert "El día contado" in cuerpo
    assert "113 facturas" in cuerpo
    assert "12.838 kg" in cuerpo            # producción MEDIDA
    assert "79 órdenes" in cuerpo
    assert "680 kg de" in cuerpo            # el desperdicio, explícito
    # Ojo: el texto del template viene cortado en varias líneas, así que la
    # aserción no puede cruzar un salto de línea del fuente.
    assert "no hay nada de revaluación adentro" in cuerpo   # tarifa quieta
    assert "3,0437" in cuerpo                               # y el $/kg a la vista
    # El resultado del día vive arriba de todo, en la cinta de KPIs, y la
    # ventana horaria al pie de la tarjeta. Antes había TRES lugares con el
    # mismo número (hero, "Resultado del día" y "El titular"); quedó uno.
    assert "Utilidad del día" in cuerpo


# ── La deuda (posdatados) y la cuenta del margen, escritas ──────────────────
# TMT 2026-08-05: *"no decís nada de posdatados"* y *"tela que valía x para
# nosotros la vendimos a tanto, margen = esto — me encanta esto pero escribilo"*.

def test_la_deuda_son_los_posdatados_con_banc_0_y_sin_anular():
    """Misma definición que el Balance. Los `banc = 9` son cheques ya emitidos,
    no deuda abierta: contarlos duplicaría el pasivo."""
    with patch.object(dia, "_rows", return_value=[{}]) as rows:
        dia.deuda_hoy(date(2026, 8, 5))
    sql = rows.call_args[0][0]
    assert "banc = 0" in sql
    assert "NOT COALESCE(anulada, FALSE)" in sql


def test_la_pantalla_cuenta_el_margen_en_una_frase(app, fake_db):
    """La cuenta en castellano, no sólo el porcentaje: es lo que hace que se
    entienda que la venta aporta el MARGEN y no el importe de la factura."""
    c = _login(app, fake_db)
    res = {"ok": True, "d_utilidad": 100.0, "d_stock": 0.0, "d_deuda": 0.0,
           "desde": {"vsto": 1.0}, "hasta": {"vsto": 1.0}, "cobrado": 1.0,
           "ventas": {"n": 92, "kg": 13459.0, "us": 116406.0},
           "compras": {"n": 0, "kg": 0.0, "us": 0.0},
           "margen": 45834.0, "margen_pct": 39.4, "costo_despachado": 70573.0,
           "produccion": {"disponible": False}, "etapas": []}
    with patch.object(dia, "explicar", return_value=_explicado()), \
         patch.object(dia, "resumen", return_value=res), \
         patch.object(dia, "deuda_hoy", return_value={"n": 0, "total": 0.0}), \
         patch.object(dia, "racha_limpia", return_value=0):
        cuerpo = c.get("/informes/dia").data.decode()
    assert "nos costaba" in cuerpo
    assert "la vendimos en" in cuerpo
    assert "70.573" in cuerpo and "45.834" in cuerpo


def test_la_pantalla_habla_de_la_deuda(app, fake_db):
    c = _login(app, fake_db)
    deuda = {"n": 152, "total": 3531577.0, "vencido": 524828.0,
             "prox7": 125699.0, "prox30": 264585.0}
    with patch.object(dia, "explicar", return_value=_explicado()), \
         patch.object(dia, "resumen", return_value={"ok": False}), \
         patch.object(dia, "deuda_hoy", return_value=deuda), \
         patch.object(dia, "racha_limpia", return_value=0):
        cuerpo = c.get("/informes/dia").data.decode()
    assert "3.531.577" in cuerpo          # el nivel
    assert "264.585" in cuerpo            # y CUÁNDO hay que pagarlo


def test_el_porcentaje_explicado_sobrevive_a_un_dia_sin_relato(app, fake_db):
    """🚨 La ventana y el % explicado son propiedades de la EXPLICACIÓN, no del
    relato. Vivían al pie de la tarjeta de la prosa y un día sin `resumen()`
    se los llevaba puestos: la pantalla dejaba de decir cuánto entendió."""
    c = _login(app, fake_db)
    with patch.object(dia, "explicar", return_value=_explicado()), \
         patch.object(dia, "resumen", return_value={"ok": False}), \
         patch.object(dia, "racha_limpia", return_value=0):
        cuerpo = c.get("/informes/dia").data.decode()
    assert "explicado al 100" in cuerpo
    assert "Utilidad del día" in cuerpo


# ── El mensaje de WhatsApp ──────────────────────────────────────────────────
# TMT 2026-08-05: *"me gustaría también mostrar la utilidad hasta hoy, algo
# para mandar chiquito e informativo a mí, andres y federico por whatsapp"*.

def _res_ok(**over):
    r = {
        "ok": True, "dia_parcial": False,
        "hasta": {"utilidad": 125331.0}, "d_utilidad": 56568.0,
        "ventas": {"n": 116, "kg": 16138.0, "us": 138369.0},
        "compras": {"n": 3, "kg": 0.0, "us": 9708.0},
        "cobrado": 61847.0, "margen_pct": 38.1, "margen": 52698.0,
        "produccion": {"disponible": True, "sin_fila": False,
                       "producido": 12837.75, "despachado": 16337.85,
                       "mes": {"producido": 30822.45, "vendido": 32992.1}},
    }
    r.update(over)
    return r


def test_el_titular_del_whatsapp_es_la_utilidad_DEL_MES():
    """⭐ La `utilidad` del balance YA ES la del mes acumulada (PATR − PATANT +
    retiros). No hay que calcularla: lo que cambia es el encuadre — el titular
    es el acumulado y el día pasa a ser su aporte."""
    with patch.object(dia, "resumen", return_value=_res_ok()), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 226, "kg": 31152.0, "us": 265343.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 4))
    assert "*Utilidad de ago: $ 125.331*" in m
    assert "Hoy +$ 56.568" in m


def test_el_whatsapp_no_lleva_tablas_ni_markdown_que_whatsapp_no_entienda():
    """WhatsApp sólo entiende *negrita* y _cursiva_. Una tabla con pipes o un
    encabezado markdown se ven como basura en un teléfono."""
    with patch.object(dia, "resumen", return_value=_res_ok()), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 226, "kg": 31152.0, "us": 265343.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 4))
    assert "|" not in m
    assert "#" not in m
    assert "**" not in m
    # El límite va como LITERAL a propósito: medir contra `dia.ANCHO_WA` sería
    # circular —el código define la constante y el test la acepta, así que
    # subirla haría pasar el test sin arreglar nada. 34 caracteres es lo que
    # entra sin que WhatsApp corte la línea por su cuenta en un teléfono.
    for linea in m.split("\n"):
        assert len(linea) <= 34, f"línea larga para un celular: {linea!r}"


def _motores(*filas):
    """Las reglas que `explicar()` devolvería, para el bloque *Lo movió hoy*."""
    return [{"regla": r, "familia": f, "aporte": a, "n": 1} for r, f, a in filas]


_MOTORES_TIPICOS = _motores(
    ("Venta facturada", "utilidad", 41200.0),
    ("Stock", "utilidad", 18900.0),
    ("Amortización del día", "utilidad", -3532.0),
)


def test_el_whatsapp_es_corto():
    """"Chiquito". Si no entra de un vistazo en el celular, no lo lee nadie.

    El tope subió de 12 a 16 el 05/08, cuando la nota pasó a contestar *por
    qué* subió la utilidad y no sólo *cuánto*: tres líneas de motores más el
    título y el aire. 16 sigue entrando en una pantalla de teléfono sin
    scrollear, que es lo que "chiquito" quiere decir.
    """
    with patch.object(dia, "resumen", return_value=_res_ok()), \
         patch.object(dia, "motores_del_dia", return_value=_MOTORES_TIPICOS), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 226, "kg": 31152.0, "us": 265343.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 4))
    assert len(m.split("\n")) <= 16


# ── *Lo movió hoy*: la nota contesta el porqué ──────────────────────────────
# TMT 2026-08-05: *"quiero un poco mejor"* sobre la nota diaria para Andrés y
# los accionistas. Lo que faltaba no era otro número: era el porqué del que ya
# está.

def test_el_whatsapp_dice_QUE_movio_la_utilidad():
    with patch.object(dia, "resumen", return_value=_res_ok()), \
         patch.object(dia, "motores_del_dia", return_value=_MOTORES_TIPICOS), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 226, "kg": 31152.0, "us": 265343.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 4))
    assert "*Lo movió hoy*" in m
    assert "Venta facturada" in m
    assert "+$ 41.200" in m
    # El signo menos es el U+2212, no un guión. Y el importe corto va con
    # relleno adelante: los tres montos quedan en columna, no cada uno pegado
    # al borde con el `$` bailando.
    assert "−$  3.532" in m
    assert "Stock                    +$ 18.900" in m


def test_el_porque_va_ARRIBA_de_los_kilos():
    """El que lee en el celular corta a la tercera línea: primero el porqué."""
    with patch.object(dia, "resumen", return_value=_res_ok()), \
         patch.object(dia, "motores_del_dia", return_value=_MOTORES_TIPICOS), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 226, "kg": 31152.0, "us": 265343.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 4))
    assert m.index("*Lo movió hoy*") < m.index("Producción")


def test_las_lineas_del_bloque_entran_en_el_celular():
    """Un nombre de regla largo se corta: 35 caracteres los parte WhatsApp
    donde quiere y el bloque deja de leerse en columna."""
    largos = _motores(
        ("Anticipos cuya mercadería ya entró al stock", "utilidad", 12345.67),
        ("Cheques emitidos sin debitar del banco", "utilidad", -890.1),
    )
    with patch.object(dia, "resumen", return_value=_res_ok()), \
         patch.object(dia, "motores_del_dia", return_value=largos), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 226, "kg": 31152.0, "us": 265343.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 4))
    for linea in m.split("\n"):
        assert len(linea) <= 34, f"línea larga para un celular: {linea!r}"
    assert "…" in m                  # se cortó, no se tiró la fila entera
    assert "$ 12.346" in m           # y el importe llegó entero


def test_un_dia_sin_motores_no_deja_un_titulo_colgado():
    """Misma regla que el resto: si el dato no dice nada, la línea no va."""
    with patch.object(dia, "resumen", return_value=_res_ok()), \
         patch.object(dia, "motores_del_dia", return_value=[]), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 226, "kg": 31152.0, "us": 265343.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 4))
    assert "Lo movió hoy" not in m
    assert "Utilidad de ago" in m


def test_los_traspasos_NO_entran_como_motores():
    """⭐ Un traspaso netea cero DE A PARES (factura −1.000 / cheque +1.000).
    Mostrar una punta sola —"Cheque recibido +$ 61.847"— haría parecer que la
    cobranza generó utilidad. No la genera: la cambia de lugar."""
    reglas = [
        {"regla": "Cheque recibido", "familia": "traspaso", "aporte": 61847.0},
        {"regla": "Abono a factura", "familia": "traspaso", "aporte": -61847.0},
        {"regla": "Venta facturada", "familia": "utilidad", "aporte": 138369.0},
    ]
    with patch.object(dia, "explicar", return_value={"reglas": reglas}):
        out = dia.motores_del_dia(date(2026, 8, 4))
    assert [r["regla"] for r in out] == ["Venta facturada"]


def test_un_ajuste_grande_SI_sale_en_la_nota():
    """🚨 Si lo que más movió la utilidad es un `#ajuste`, esconderlo sería
    mentir sobre el porqué justo el día que más importa saberlo."""
    reglas = [
        {"regla": "Venta facturada", "familia": "utilidad", "aporte": 1000.0},
        {"regla": "Sin explicar todavía", "familia": "sin_explicar",
         "aporte": -40000.0},
    ]
    with patch.object(dia, "explicar", return_value={"reglas": reglas}):
        out = dia.motores_del_dia(date(2026, 8, 4))
    assert out[0]["regla"] == "Sin explicar todavía"     # ordenado por |aporte|


def test_los_motores_van_ordenados_por_tamano_y_son_a_lo_sumo_tres():
    reglas = [{"regla": f"R{i}", "familia": "utilidad", "aporte": float(i)}
              for i in range(1, 8)]
    with patch.object(dia, "explicar", return_value={"reglas": reglas}):
        out = dia.motores_del_dia(date(2026, 8, 4))
    assert [r["regla"] for r in out] == ["R7", "R6", "R5"]


def test_si_la_explicacion_se_cae_la_nota_igual_sale():
    """La nota es un extra: no puede tumbar el resumen del día."""
    with patch.object(dia, "explicar", side_effect=RuntimeError("sin db")):
        assert dia.motores_del_dia(date(2026, 8, 4)) == []


def test_el_whatsapp_no_pone_lineas_de_relleno():
    """Un dato que no está no lleva línea: un cero o un guión ocupan lo mismo
    que un número y no dicen nada."""
    with patch.object(dia, "resumen", return_value=_res_ok(
            ventas={"n": 0, "kg": 0.0, "us": 0.0}, cobrado=0.0,
            margen_pct=None,
            produccion={"disponible": True, "sin_fila": True})), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 0, "kg": 0.0, "us": 0.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 4))
    assert "Ventas" not in m
    assert "Producción" not in m
    assert "Cobrado" not in m
    assert "Utilidad de ago" in m       # el titular siempre va


def test_el_whatsapp_avisa_si_el_tramo_no_es_de_24h():
    with patch.object(dia, "resumen", return_value=_res_ok(dia_parcial=True)), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 1, "kg": 1.0, "us": 1.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 4))
    assert "no son 24 h" in m


def test_sin_dia_que_explicar_no_hay_mensaje():
    """Vacío y no un texto con ceros: mandar 'utilidad $0' sería peor que no
    mandar nada."""
    with patch.object(dia, "resumen", return_value={"ok": False}):
        assert dia.mensaje_whatsapp(date(2026, 8, 4)) == ""


def test_las_ventas_del_mes_van_del_1_hasta_el_dia():
    with patch.object(dia, "_rows", return_value=[{"n": 5, "kg": 10, "us": 100}]) as rows:
        dia.ventas_del_mes(date(2026, 8, 4))
    sql = rows.call_args[0][0]
    assert "date_trunc('month'" in sql and "fecha <= %s" in sql
    assert "NOT IN ('X', 'Y')" in sql


def test_la_pantalla_trae_el_bloque_de_whatsapp(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(dia, "explicar", return_value=_explicado()), \
         patch.object(dia, "resumen", return_value={"ok": False}), \
         patch.object(dia, "mensaje_whatsapp",
                      return_value="*INTELA · mar 4 ago*\n\n*Utilidad de ago: $ 125.331*"), \
         patch.object(dia, "racha_limpia", return_value=0):
        cuerpo = c.get("/informes/dia").data.decode()
    assert "Para mandar" in cuerpo
    assert "Utilidad de ago" in cuerpo
    # El botón abre el WhatsApp de quien aprieta. Programa Core no manda nada.
    assert "https://wa.me/?text=" in cuerpo


# ── Los tres bugs que sólo se vieron mirando el mensaje REAL ────────────────
# Verificado en vivo el 05/08 con la pantalla ya deployada. Ninguno de los tres
# lo cazaban los tests con datos inventados: los tres nacen de que la realidad
# tiene combinaciones que uno no piensa.

def test_el_margen_usa_los_kilos_FACTURADOS_no_los_despachados():
    """05/08 en vivo: 545 kg facturados, 2.069 despachados → margen −134,7 %.

    La mercadería sale un día y se factura otro, así que son dos universos.
    La plata viene de la factura: el costo tiene que salir de LOS MISMOS kilos
    de esa factura, o el margen es una resta entre cosas distintas.
    """
    caps = [_cap(0.0, terminado_kg=1000.0, terminado_ukg=5.2437),
            _cap(0.0, terminado_kg=1000.0, terminado_ukg=5.2437)]
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "produccion_del_dia", return_value={
             "disponible": True, "sin_fila": False, "producido": 0.0,
             "despachado": 2069.0, "mes": {}}), \
         patch.object(dia, "ventas_del_dia",
                      return_value={"n": 4, "kg": 545.0, "us": 4622.0}), \
         patch.object(dia, "compras_del_dia",
                      return_value={"n": 0, "kg": 0.0, "us": 0.0}):
        r = dia.resumen(date(2026, 8, 5))
    # 545 × 5,2437 = 2.857,82 → margen 1.764,18 sobre 4.622 = 38,2 %
    assert r["costo_despachado"] == pytest.approx(2857.82, abs=0.01)
    assert r["margen_pct"] == pytest.approx(38.2, abs=0.1)
    assert r["margen_pct"] > 0, "un margen negativo acá es la resta mal hecha"


def test_dia_parcial_llega_hasta_el_mensaje_de_whatsapp():
    """Lo seteaba `explicar()` y el mensaje lo leía de `resumen()`, así que
    nunca llegaba: se mandaba un tramo corto sin avisar que no eran 24 h."""
    caps = [_cap(0.0), _cap(500.0)]
    caps[0]["fecha_ec"] = caps[1]["fecha_ec"] = date(2026, 8, 5)   # mismo día
    with patch.object(dia, "capturas", return_value=caps), \
         patch.object(dia, "produccion_del_dia", return_value={"disponible": False}), \
         patch.object(dia, "ventas_del_dia", return_value={"n": 0, "kg": 0.0, "us": 0.0}), \
         patch.object(dia, "compras_del_dia", return_value={"n": 0, "kg": 0.0, "us": 0.0}):
        r = dia.resumen(date(2026, 8, 5))
    assert r["dia_parcial"] is True
    with patch.object(dia, "resumen", return_value=r), \
         patch.object(dia, "ventas_del_mes", return_value={"n": 0, "kg": 0.0, "us": 0.0}):
        assert "no son 24 h" in dia.mensaje_whatsapp(date(2026, 8, 5))


def test_produccion_en_cero_no_ocupa_una_linea_del_whatsapp():
    """A media mañana siempre es 0 porque todavía no cerró ninguna orden.
    Poner "Producción 0 kg" es afirmar que la planta no hizo nada."""
    with patch.object(dia, "resumen", return_value=_res_ok(
            produccion={"disponible": True, "sin_fila": False, "producido": 0.0,
                        "despachado": 0.0, "mes": {"producido": 30822.45}})), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 226, "kg": 31152.0, "us": 265343.0}):
        m = dia.mensaje_whatsapp(date(2026, 8, 5))
    assert "Producción" not in m
    assert "Utilidad de ago" in m        # el titular sigue yendo
