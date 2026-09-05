"""La grabadora de la utilidad (TMT 2026-07-31).

Dueña, después de un día entero de hipótesis: *"mirálo ahora. guardá la data y
fijate en un rato también. no podemos tener utilidad menor a la mañana"*.

El problema de fondo no era ninguna de las hipótesis: era que no había con qué
comparar. `scintela.historia` guarda UNA fila por día, así que un salto de las
09:16 a las 10:34 no quedaba registrado en ningún lado y había que reconstruirlo
de memoria. Esto guarda la utilidad JUNTO CON sus componentes cada 5 minutos.

Lo que estos tests fijan:
  · la foto se arma del balance y no inventa columnas;
  · el Δ se calcula contra la foto ANTERIOR y dice QUÉ lo movió;
  · los pasivos aportan con el signo dado vuelta (suben → utilidad baja);
  · la grabadora NO puede tumbar nada: si falla, devuelve False y sigue.
"""
from unittest.mock import patch

import pytest

from modules.informes import traza as t


@pytest.fixture(autouse=True)
def _limpio():
    t._ultimo_ts = 0.0
    yield
    t._ultimo_ts = 0.0


BALANCE = {
    "diagnostico": {"componentes": {
        "utilidad": 582358.0, "patr": 21368271.0, "uret": 211393.12,
        "salcaj": 40659.0, "salbanc_total": 2564877.0,
        "totc": 2544807.0, "totf": 5112947.0,
        "antic": 2138524.55, "vsto": 8351691.0, "vqx": 409279.0,
        "umaq": 1072300.0, "uact": 2379014.0, "totp": 3457219.37,
    }},
    "stock_etapas": {
        "hilado": {"kg": 1899100.0, "ukg": 2.9970},
        "tejido": {"kg": 294986.0},
        "terminado": {"kg": 313339.0},
    },
    "kg": {"kvent": 329908.0, "uvent": 2814059.0},
    "hilado_valuacion": {"compras": 404657.0, "compras_us": 1224937.0,
                         "kg_sin_costo": 0.0},
}


def test_la_foto_sale_del_balance():
    f = t._fila_desde_balance(BALANCE)
    assert f["utilidad"] == 582358.0
    assert f["vsto"] == 8351691.0
    assert f["antic"] == 2138524.55
    assert f["totp"] == 3457219.37
    assert f["hilado_ukg"] == 2.9970
    assert f["venta_kg"] == 329908.0
    # Patrimonio NETO: el de la pantalla, sin los dividendos adentro.
    assert f["patr_neto"] == pytest.approx(21368271.0 - 211393.12)


# ⭐ Desde la mig 0171 `registrar()` devuelve un DICT y no un bool: el ancla del
# día (`dia.capturar`) necesita el `id_traza` de la foto y el balance ya
# calculado, para colgarse de esa misma foto en vez de leer el balance dos
# veces. `registrar_si_toca()` sigue devolviendo bool — es lo único que el
# hilo de fondo mira.

def test_un_balance_vacio_no_se_guarda():
    """Sin componentes no hay foto que valga: mejor ninguna que una en cero."""
    with patch.object(t.db, "execute") as ex:
        res = t.registrar(bal={})
    assert res["ok"] is False
    assert res["motivo"] == "balance sin componentes"
    ex.assert_not_called()


def test_si_la_base_falla_la_grabadora_no_rompe_nada():
    with patch.object(t.db, "execute", side_effect=RuntimeError("sin tabla")):
        res = t.registrar(bal=BALANCE)                # no levanta
    assert res["ok"] is False
    assert res["motivo"]                              # queda por qué falló


def test_guarda_una_sola_vez_por_intervalo():
    with patch.object(t, "registrar", return_value={"ok": True}) as reg:
        assert t.registrar_si_toca() is True
        for _ in range(10):
            assert t.registrar_si_toca() is False
    assert reg.call_count == 1


def test_pasado_el_intervalo_vuelve_a_guardar():
    with patch.object(t, "registrar", return_value={"ok": True}) as reg:
        t.registrar_si_toca()
        t._ultimo_ts -= t._intervalo() + 1
        assert t.registrar_si_toca() is True
    assert reg.call_count == 2


# ── El Δ y el "quién lo movió" ─────────────────────────────────────────────

def _foto(**kw):
    base = {"utilidad": 0.0, "caja": 0.0, "bancos": 0.0, "cheques": 0.0,
            "facturas": 0.0, "antic": 0.0, "vsto": 0.0, "vqx": 0.0,
            "umaq": 0.0, "uact": 0.0, "totp": 0.0, "uret": 0.0}
    base.update(kw)
    return base


def test_el_delta_se_calcula_contra_la_foto_anterior():
    filas = [_foto(utilidad=600000.0), _foto(utilidad=574000.0)]  # nueva primero
    out = t.con_deltas(filas)
    assert out[0]["d_utilidad"] == 26000.0
    assert out[1]["d_utilidad"] is None      # la más vieja no tiene contra qué


def test_dice_QUE_se_movio_y_lo_ordena_por_tamano():
    filas = [
        _foto(utilidad=600000.0, vsto=8_400_000.0, antic=2_000_000.0),
        _foto(utilidad=574000.0, vsto=8_330_000.0, antic=2_010_000.0),
    ]
    out = t.con_deltas(filas)
    movs = out[0]["movio"]
    assert movs[0]["col"] == "vsto" and movs[0]["aporte"] == 70000.0
    assert movs[1]["col"] == "antic" and movs[1]["aporte"] == -10000.0


def test_los_pasivos_aportan_al_reves():
    """Si los pasivos SUBEN, la utilidad baja: el aporte tiene que ser negativo."""
    filas = [_foto(utilidad=574000.0, totp=3_457_000.0),
             _foto(utilidad=600000.0, totp=3_406_000.0)]
    out = t.con_deltas(filas)
    mov = out[0]["movio"][0]
    assert mov["col"] == "totp"
    assert mov["delta"] == 51000.0 and mov["aporte"] == -51000.0


def test_el_ruido_de_centavos_no_ensucia_la_lista():
    filas = [_foto(utilidad=1.0, vsto=100.40), _foto(utilidad=0.0, vsto=100.0)]
    assert t.con_deltas(filas)[0]["movio"] == []


def test_las_bajadas_son_las_que_hay_que_mirar():
    """Dueña: 'no podemos tener utilidad menor a la mañana'."""
    filas = t.con_deltas([
        _foto(utilidad=560000.0),   # bajó 40.000
        _foto(utilidad=600000.0),   # subió 26.000
        _foto(utilidad=574000.0),
    ])
    bajas = t.bajadas(filas, umbral=1000.0)
    assert len(bajas) == 1
    assert bajas[0]["d_utilidad"] == -40000.0


def test_sin_fotos_no_explota():
    assert t.con_deltas([]) == []
    assert t.bajadas([]) == []


# ---------------------------------------------------------------------------
# El sticky de las tres primeras columnas (TMT 2026-08-09: "corregir estos
# espacios en blanco que quedaron")
# ---------------------------------------------------------------------------
def _plantilla_traza() -> str:
    from pathlib import Path
    return Path("modules/informes/templates/informes/traza.html").read_text(
        encoding="utf-8")


def test_el_offset_del_sticky_se_mide_con_el_ancho_fraccionario():
    """`offsetWidth` REDONDEA, y medio píxel de más empuja la columna.

    Medido en producción el 09/08: `--tz1` valía 144 px contra una columna de
    131 → el sticky clavaba la celda 13 px a la derecha de su lugar (así
    funciona: si la posición natural queda antes de `left`, la corre), dejaba
    blanco atrás y tapaba la columna de al lado.
    """
    t_ = _plantilla_traza()
    assert "cells[0].getBoundingClientRect().width" in t_
    assert "cells[1].getBoundingClientRect().width" in t_
    assert "cells[0].offsetWidth" not in t_, "offsetWidth redondea: se desfasa"
    assert "cells[1].offsetWidth" not in t_


def test_vuelve_a_medir_cuando_la_columna_cambia_de_ancho():
    """La medición no estaba mal: quedaba VIEJA.

    Corre al parsear, con la tipografía de respaldo; cuando entra Inter las
    columnas se angostan y el `resize` de la ventana no se dispara. Un
    ResizeObserver sobre las dos celdas cubre la fuente, el zoom, la ventana y
    cualquier cambio de contenido.
    """
    t_ = _plantilla_traza()
    assert "new ResizeObserver(medir)" in t_
    assert t_.count("ro.observe(") == 2


# ---------------------------------------------------------------------------
# Ir más atrás que las últimas 200 fotos (TMT 2026-08-09: "¿cuán atrás podemos
# ir? porque quizás quiero ver un finde también")
# ---------------------------------------------------------------------------
def test_el_rango_pide_los_dias_y_un_ancla_anterior():
    """La foto extra NO es un bonus: sin ella la fila más vieja no tiene Δ.

    `con_deltas` compara cada foto contra la de abajo. Si el rango arranca
    justo el sábado a las 00:04, esa fila es la última de la lista y quedaría
    con el Δ vacío — que es justo la que cuenta qué pasó durante la noche.
    """
    visto = {}

    def _fake(sql, params=None):
        visto["sql"], visto["params"] = sql, params
        return [{"id_traza": 2, "en_rango": True},
                {"id_traza": 1, "en_rango": False}]

    with patch.object(t.db, "fetch_all", side_effect=_fake):
        filas = t.entre("2026-08-01", "2026-08-02")
    assert [f["en_rango"] for f in filas] == [True, False]
    # El ancla se pide por creado_en, no por fecha: la foto anterior al rango
    # puede ser de otro día y hasta de otro mes.
    assert "creado_en < (SELECT MIN(creado_en) FROM rango)" in visto["sql"]
    assert visto["params"] == ("2026-08-01", "2026-08-02",
                               "2026-08-01", "2026-08-02")


def test_el_rango_tiene_tope():
    """Pedir del 31/07 a hoy son miles de filas: se muestran las más nuevas."""
    with patch.object(t.db, "fetch_all", return_value=[]) as f:
        t.entre("2026-07-31", "2026-08-09", n=99999)
    assert f"LIMIT {t.TOPE_RANGO}" in f.call_args[0][0]


def test_si_la_base_falla_el_rango_no_rompe_la_pantalla():
    with patch.object(t.db, "fetch_all", side_effect=RuntimeError("boom")):
        assert t.entre("2026-08-01", "2026-08-02") == []
        assert t.primera_foto() is None


def test_los_deltas_de_la_grilla_llevan_separador_de_miles():
    """🚨 TMT 2026-08-09, mirando la traza: *"¿podríamos revisar por qué
    algunos tienen punto y otros no?"*. La grilla escribía los Δ con
    `'%+d'|format` —"−73984"— y el detalle de la MISMA fila con `num_es`
    —"−73.984"—: dos formatos de número, uno arriba del otro. `%+d` es formato
    de C, no de Ecuador."""
    from filters import delta_es
    assert delta_es(-73984) == "-73.984"
    assert delta_es(1042) == "+1.042"
    assert delta_es(0) == "" and delta_es(None) == ""
    from pathlib import Path
    for nombre in ("traza.html", "traza_foto_detalle.html", "traza_foto.html"):
        txt = Path("modules/informes/templates/informes").joinpath(
            nombre).read_text(encoding="utf-8")
        assert "'%+d'" not in txt, f"{nombre} sigue con formato de C"
        assert "delta_es" in txt


# ── El freno vive en la base, no en el proceso ────────────────────────
#
# 🚨 25/08: *"hay algo raro, la traza no se está moviendo"*. Cada foto se
# guardaba DOS veces con segundos de diferencia —173 de las 357 del día— y la
# segunda salía con Δ 0 y las columnas vacías, así que la grilla parecía
# congelada. `_ultimo_ts` es una variable de PROCESO y hay más de un proceso.

def test_una_foto_de_hace_segundos_frena_la_siguiente():
    with patch.object(t.db, "fetch_one", return_value={"seg": 7.0}):
        assert t._muy_reciente(None, 300) is True


def test_pasado_el_intervalo_la_base_deja_sacar_la_foto():
    with patch.object(t.db, "fetch_one", return_value={"seg": 301.0}):
        assert t._muy_reciente(None, 300) is False


def test_la_primera_foto_no_tiene_contra_que_frenarse():
    """Tabla vacía: `MAX(creado_en)` es NULL y la foto tiene que entrar."""
    with patch.object(t.db, "fetch_one", return_value={"seg": None}):
        assert t._muy_reciente(None, 300) is False


def test_el_ciclo_de_fondo_frena_contra_la_base_y_no_contra_su_reloj():
    with patch.object(t, "registrar", return_value={"ok": True}) as reg:
        t.registrar_si_toca()
    assert reg.call_args.kwargs["min_gap_secs"] == t._intervalo()


def test_la_foto_repetida_no_llega_a_escribirse():
    """El freno se pregunta ADENTRO del lock y ANTES del INSERT."""
    from contextlib import contextmanager

    from modules.informes import foto as motor

    @contextmanager
    def _tx():
        yield object()

    def _fetch(sql, params=None, conn=None):
        return {"ok": True} if "advisory" in sql else {"seg": 6.0}

    with patch.object(t.db, "tx", _tx), \
         patch.object(t.db, "fetch_one", side_effect=_fetch), \
         patch.object(t.db, "execute_returning") as ins, \
         patch.object(motor, "guardada", return_value={}), \
         patch.object(motor, "detalle", return_value=[]), \
         patch.object(motor, "hay_desfase", return_value=False), \
         patch.object(motor, "es_primera", return_value=False), \
         patch.object(motor, "diff", return_value=[]), \
         patch.object(motor, "guardar_movimientos"), \
         patch.object(motor, "aplicar"):
        res = t.registrar(bal=BALANCE, min_gap_secs=300)

    assert res["ok"] is False
    assert "menos de un intervalo" in res["motivo"]
    ins.assert_not_called()


# ── La foto se fecha cuando se LEYÓ el saldo, no cuando se grabó ─────────────

def test_la_foto_lleva_la_hora_en_que_se_leyo_el_banco():
    """05/09/2026: con el server lento, entre leer el saldo del banco y grabar
    la foto pasó más de un minuto. Las notas de débito cargadas en ese hueco
    (4.354,66) se NOMBRARON en la foto de las 09:58 —cuyo saldo no las tenía—
    y su plata apareció en la de las 10:09: "Bancos: sin explicar +4.355" y
    después "−4.355". El borde de la ventana tiene que ser la lectura."""
    from datetime import UTC, datetime
    leido = datetime(2026, 9, 5, 14, 57, 0, tzinfo=UTC)
    f = t._fila_desde_balance({**BALANCE, "leido_en": leido})
    assert f["creado_en"] == leido


def test_sin_leido_en_la_foto_deja_que_la_tabla_ponga_la_hora():
    """Un balance viejo (o un test) sin `leido_en`: queda el DEFAULT."""
    f = t._fila_desde_balance(BALANCE)
    assert "creado_en" not in f

