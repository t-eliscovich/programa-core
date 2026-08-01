"""Cierre de mes — los dos bugs que se cazaron el 2026-08-01.

Contexto (lo que hace el legacy, para que no se vuelva a torcer):

* `MENU.PRG` L246-263 — al abrir el FoxPro el primer día de un mes nuevo
  agrega la fila de INICIALES **del mes en curso** (`REPLA MES WITH
  CMONTH(DATE())`) arrastrando el stock del mes que cerró.
* `INFORMES.PRG` PROCEDURE HISTORIA — la foto del mes guarda
  `REPLA PATRIMONIO WITH PATR-URET`, o sea **neto de retiros**, y el dedup
  final deja una sola fila por mes: la última que se grabó.

Los dos bugs eran, exactamente, apartarse de esas dos líneas.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.informes import queries as iq  # noqa: E402
from modules.iniciales import queries as qi  # noqa: E402


@contextmanager
def _tx_dummy():
    yield object()


# ---------------------------------------------------------------------------
# BUG 1 — cerrar_mes_auto creaba el mes SIGUIENTE
# ---------------------------------------------------------------------------

def _armar_fake_iniciales(monkeypatch, *, marker: str, ya_existe=None):
    """Ruteador mínimo de scintela.iniciales + sistema_meta."""
    capturado: dict = {}

    def fake_fetch_one(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "sistema_meta" in s:
            return {"valor": marker}
        if "where yy = %s and mesnum = %s" in s:
            return ya_existe
        if "order by id_iniciales desc" in s:
            # la ÚLTIMA fila (el "GO BOTT" del PRG)
            return {
                "hilado": 1_897_035.94, "tejido": 295_220.18,
                "terminado": 314_266.38, "vq": 409_196.19,
                "um": 3.0, "uk": 3.5, "uq": 0.6, "uf": 5.1,
                "kprog": 320_000, "gprog": 0, "numnot": 0,
                "pretot": 785_000,
            }
        return None

    def fake_execute_returning(sql, params=None, conn=None):
        capturado["insert_params"] = params
        return {"id_iniciales": 999}

    monkeypatch.setattr(qi.db, "tx", _tx_dummy)
    monkeypatch.setattr(qi.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(qi.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(qi.db, "execute_returning", fake_execute_returning)
    return capturado


def test_cerrar_mes_auto_crea_el_mes_en_curso_no_el_siguiente(monkeypatch):
    """El 1 de agosto tiene que crear AGOSTO, no septiembre.

    Antes el destino era `mes+1`, así que el hook de /informes/balance metía
    una fila de un mes futuro con el stock del mes que recién cerraba — la
    misma condición que el 2026-07-01 dejó el balance leyendo iniciales de
    un mes que todavía no había ocurrido.
    """
    cap = _armar_fake_iniciales(monkeypatch, marker="1900-01")

    res = qi.cerrar_mes_auto(fecha_cierre=date(2026, 8, 1), usuario="test")

    assert res["aplicado"] is True
    assert res["mes_destino"] == "2026-08"
    assert res["mes_origen"] == "2026-07"
    # El INSERT arranca con (mesnum, mesnom, yy, ...)
    mesnum, mesnom, yy = cap["insert_params"][0:3]
    assert mesnum == 8
    assert yy == 2026
    assert mesnom == "Agosto"


def test_cerrar_mes_auto_enero_arrastra_diciembre_del_anio_anterior(monkeypatch):
    cap = _armar_fake_iniciales(monkeypatch, marker="1900-01")

    res = qi.cerrar_mes_auto(fecha_cierre=date(2027, 1, 1), usuario="test")

    assert res["mes_destino"] == "2027-01"
    assert res["mes_origen"] == "2026-12"
    assert cap["insert_params"][0] == 1
    assert cap["insert_params"][2] == 2027


def test_cerrar_mes_auto_el_marker_adelantado_ya_no_bloquea(monkeypatch):
    """El marker quedó en '2026-08' (lo adelantó la versión vieja el 1/7) y
    después un sync del dBase TRUNCÓ scintela.iniciales y se llevó la fila.
    Con el corte viejo `ult_clave >= mes_dest_clave` el mes en curso se
    quedaba sin iniciales y nadie avisaba. El invariante real es si la fila
    existe, no lo que diga el marker."""
    cap = _armar_fake_iniciales(monkeypatch, marker="2026-08", ya_existe=None)

    res = qi.cerrar_mes_auto(fecha_cierre=date(2026, 8, 1), usuario="test")

    assert res["aplicado"] is True, res["razon"]
    assert cap["insert_params"][0] == 8


def test_cerrar_mes_auto_no_duplica_si_la_fila_ya_esta(monkeypatch):
    """Idempotente contra rollover_y_writeback_iniciales: si el cron ya creó
    la fila del mes, este camino no inserta nada."""
    cap = _armar_fake_iniciales(
        monkeypatch, marker="1900-01", ya_existe={"id_iniciales": 4242}
    )

    res = qi.cerrar_mes_auto(fecha_cierre=date(2026, 8, 1), usuario="test")

    assert res["aplicado"] is False
    assert res["id_iniciales_nuevo"] == 4242
    assert "insert_params" not in cap


# ---------------------------------------------------------------------------
# BUG 2 — la foto de cierre guardaba el patrimonio BRUTO y usret=0
# ---------------------------------------------------------------------------

def _bal(componentes: dict, **extra) -> dict:
    base = {
        "diagnostico": {"componentes": componentes},
        "kg": {},
        "stock_subpanels": {},
    }
    base.update(extra)
    return base


def _sin_foto_previa(monkeypatch):
    monkeypatch.setattr(iq.db, "fetch_one", lambda *a, **k: None)


def test_foto_de_cierre_guarda_patrimonio_neto_de_retiros(monkeypatch):
    """dBase: `REPLA PATRIMONIO WITH PATR-URET`.

    Guardar el bruto dejaba el PATANT inflado por los retiros del mes y la
    utilidad del mes siguiente arrancaba subestimada exactamente en ese
    monto (julio 2026: $211.400). Era la razón por la que cada mes había que
    anclar el cierre a mano.
    """
    _sin_foto_previa(monkeypatch)
    comp = {"patr": 21_419_100.0, "usret": 211_400.0, "utilidad": 633_200.0}
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: _bal(comp))
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: _bal(comp))

    res = iq.crear_snapshot_historia(2026, 7, dry_run=True)

    assert res["dry_run"] is True
    assert res["row"]["patrimonio"] == 21_419_100.0 - 211_400.0
    assert res["row"]["usret"] == 211_400.0
    assert res["row"]["retiro"] == 211_400.0
    assert res["row"]["usuti"] == 633_200.0


def test_foto_de_cierre_lee_los_retiros_de_la_rama_live(monkeypatch):
    """La rama LIVE del balance nombra los retiros `uret`; la AS-OF, `usret`.

    El código viejo sólo leía `usret`, así que por la rama live guardaba
    retiro=0 y usret=0 en el cierre — y encima no netaba el patrimonio.
    """
    _sin_foto_previa(monkeypatch)
    comp = {"patr": 1_000.0, "uret": 200.0, "utilidad": 50.0}
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: _bal(comp))
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: _bal(comp))

    row = iq.crear_snapshot_historia(2026, 7, dry_run=True)["row"]

    assert row["usret"] == 200.0
    assert row["retiro"] == 200.0
    assert row["patrimonio"] == 800.0


def test_foto_de_cierre_sin_retiros_no_cambia_nada(monkeypatch):
    """Invariante sobre el dominio entero: sin retiros, neto == bruto.
    [[feedback_ensanchar_dominio_invariante]]"""
    _sin_foto_previa(monkeypatch)
    comp = {"patr": 1_000.0, "utilidad": 50.0}
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: _bal(comp))
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: _bal(comp))

    row = iq.crear_snapshot_historia(2026, 7, dry_run=True)["row"]

    assert row["patrimonio"] == 1_000.0
    assert row["usret"] == 0.0


def test_foto_de_cierre_por_la_rama_live_no_guarda_banco_ni_stock_en_cero(monkeypatch):
    """La rama LIVE del balance expone `salbanc_total`+`salcaj` por separado y
    el stock/químico en el TOP-LEVEL; la AS-OF, `salbanc` y `total_us`.

    El mapeo leía sólo los nombres de la rama as-of, así que un cierre tomado
    por la rama live guardaba banco=0 y ustock=0 y la columna del Historial
    salía en cero. Lo cazó el dry-run del simulacro.
    """
    _sin_foto_previa(monkeypatch)
    comp = {
        "patr": 1_000.0, "usret": 0.0, "utilidad": 50.0,
        "salbanc_total": 600.0, "salcaj": 40.0,   # rama live: separados
        "vsto": 8_400.0, "vqx": 409.0,
    }
    bal = _bal(comp, vsto=8_410.0, vqx=409.2,
               stock=({"total": {"kg": 2_231_673.0}}))
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: bal)
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: bal)

    row = iq.crear_snapshot_historia(2026, 7, dry_run=True)["row"]

    assert row["banco"] == 640.0        # 600 + 40, no 0
    assert row["ustock"] == 8_410.0     # top-level, no 0
    assert row["uqui"] == 409.2
    assert row["stock"] == 2_231_673.0


def test_foto_de_cierre_por_la_rama_as_of_sigue_igual(monkeypatch):
    """La rama as-of no se toca: `salbanc` ya trae la caja adentro."""
    _sin_foto_previa(monkeypatch)
    comp = {"patr": 1_000.0, "usret": 0.0, "utilidad": 50.0, "salbanc": 640.0}
    bal = _bal(comp)
    bal["stock_subpanels"] = {"total_us": 8_400.0}
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: bal)
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: bal)

    row = iq.crear_snapshot_historia(2026, 7, dry_run=True)["row"]

    assert row["banco"] == 640.0
    assert row["ustock"] == 8_400.0


# ---------------------------------------------------------------------------
# BUG 3 — los FLUJOS DEL MES (y sobre todo los GASTOS) salían del mes anterior
# ---------------------------------------------------------------------------

def _resultados_vivos() -> dict:
    """Las 5 filas de costos del PRG, con los valores del ancla del dBase del
    30/06/2026 (`ancla-dbase-30-06`): utej 119.940 · utin 360.290 ·
    gasto 294.944 ⇒ gstotal 775.174."""
    return {
        "costos": [
            {"label": "MAT.PR.", "kg": 259_866.0, "us": 772_762.0},
            {"label": "TEJIDO", "kg": 306_315.0, "us": 119_940.0},
            {"label": "COL.QUI.", "kg": 341_884.0, "us": 88_000.0},
            {"label": "GS.PROC.", "kg": 300_000.0, "us": 360_290.0},
            {"label": "GASTOS", "kg": None, "us": 294_944.0},
        ],
        "stock": {"total": {"kg": 2_231_673.0}},
    }


def test_el_cierre_guarda_los_GASTOS_del_mes_no_cero(monkeypatch):
    """⭐ El bug que dejó a JULIO 2026 sin gastos en /iniciales/comparativo.

    `componentes` de la rama LIVE no tiene ni `gasto` ni `gstotal` (tiene 20
    claves y ninguna es esa), así que el mapeo caía al default 0. Enero–junio
    sí tenían gastos porque venían del dBase o de anclas a mano; julio fue el
    primer mes que fotografió PC y salió vacío.
    """
    _sin_foto_previa(monkeypatch)
    comp = {"patr": 1_000.0, "usret": 0.0, "utilidad": 50.0}  # SIN gasto/gstotal
    bal = _bal(comp, resultados=_resultados_vivos(), vsto=1.0, vqx=1.0)
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: bal)
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: bal)
    monkeypatch.setattr(iq, "_snap_live_mes_actual", lambda *a, **k: {})

    row = iq.crear_snapshot_historia(2026, 7, dry_run=True)["row"]

    assert row["gasto"] == 294_944.0
    # GSTOTAL = UTEJ + UTIN + GASTO (INFORMES.PRG L1348)
    assert row["gstotal"] == 775_174.0
    assert row["gstotal"] == row["utej"] + row["utin"] + row["gasto"]


def test_el_cierre_guarda_los_flujos_del_mes_en_curso(monkeypatch):
    """`bal["kg"]` viene de `historia_ultimo_mes()` = el mes ANTERIOR.

    Escribir eso en el cierre es peor que un 0: es un número plausible que no
    dispara ninguna alarma y que el legacy después relee como "mes anterior".
    """
    _sin_foto_previa(monkeypatch)
    comp = {"patr": 1_000.0, "usret": 0.0, "utilidad": 50.0}
    bal = _bal(comp, resultados=_resultados_vivos(), vsto=1.0, vqx=1.0)
    # kg = los valores STALE del cierre anterior; no tienen que aparecer.
    bal["kg"] = {"kcom": 111.0, "ktej": 222.0, "ktin": 333.0,
                 "ucom": 444.0, "utej": 555.0, "utin": 666.0,
                 "stock_kg": 777.0}
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: bal)
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: bal)
    monkeypatch.setattr(iq, "_snap_live_mes_actual", lambda *a, **k: {})

    row = iq.crear_snapshot_historia(2026, 7, dry_run=True)["row"]

    assert row["ucom"] == 772_762.0 and row["kcom"] == 259_866.0   # MAT.PR.
    assert row["utej"] == 119_940.0 and row["ktej"] == 306_315.0   # TEJIDO
    assert row["utin"] == 360_290.0                                # GS.PROC.
    assert row["ktin"] == 341_884.0                                # COL.QUI.
    assert row["stock"] == 2_231_673.0
    for stale in (111.0, 222.0, 333.0, 444.0, 555.0, 666.0, 777.0):
        assert stale not in row.values()


def test_sin_resultados_el_cierre_no_cambia_de_comportamiento(monkeypatch):
    """Guarda: si el balance cambia de forma y no se puede leer `resultados`
    (o es la rama AS-OF, que no lo trae), se vuelve al camino anterior — NUNCA
    se escriben ceros nuevos."""
    _sin_foto_previa(monkeypatch)
    comp = {"patr": 1_000.0, "usret": 0.0, "utilidad": 50.0,
            "gasto": 9_000.0, "gstotal": 12_000.0}
    bal = _bal(comp)  # sin `resultados`
    bal["kg"] = {"kcom": 111.0, "ucom": 444.0}
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: bal)
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: bal)

    row = iq.crear_snapshot_historia(2026, 7, dry_run=True)["row"]

    assert row["gasto"] == 9_000.0
    assert row["gstotal"] == 12_000.0
    assert row["kcom"] == 111.0 and row["ucom"] == 444.0


def test_gstotal_no_se_arma_con_un_sumando_faltante():
    """Una suma incompleta es peor que no escribir nada: se vería como un
    total razonable y estaría corta justo en el rubro que faltó."""
    parcial = {"costos": [
        {"label": "TEJIDO", "us": 100.0},
        {"label": "GS.PROC.", "us": 200.0},
        # falta GASTOS
    ]}
    viv = iq._flujos_vivos_del_mes({"resultados": parcial})

    assert viv["utej"] == 100.0 and viv["utin"] == 200.0
    assert "gstotal" not in viv
    assert "gasto" not in viv


def test_flujos_vivos_indexa_por_label_no_por_posicion():
    """Si mañana se agrega o reordena una fila de costos, el mapeo tiene que
    seguir apuntando al rubro correcto."""
    revuelto = {"costos": [
        {"label": "GASTOS", "us": 294_944.0},
        {"label": "NUEVA.FILA", "us": 1.0},
        {"label": "TEJIDO", "kg": 306_315.0, "us": 119_940.0},
        {"label": "GS.PROC.", "us": 360_290.0},
    ]}
    viv = iq._flujos_vivos_del_mes({"resultados": revuelto})

    assert viv["gasto"] == 294_944.0
    assert viv["utej"] == 119_940.0
    assert viv["utin"] == 360_290.0
    assert viv["gstotal"] == 775_174.0


def test_dry_run_no_escribe(monkeypatch):
    _sin_foto_previa(monkeypatch)
    comp = {"patr": 1_000.0, "usret": 200.0, "utilidad": 50.0}
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: _bal(comp))
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: _bal(comp))
    tocado: dict = {}
    monkeypatch.setattr(iq.db, "tx", _tx_dummy)
    monkeypatch.setattr(
        iq.db, "execute_returning",
        lambda *a, **k: tocado.setdefault("escribio", True))

    res = iq.crear_snapshot_historia(2026, 7, dry_run=True)

    assert res["aplicado"] is False
    assert tocado == {}


def test_forzar_rehace_la_foto_pisando_la_anterior(monkeypatch):
    """Mirror del dedup del FoxPro: dentro del mes gana la ÚLTIMA foto.

    Sin `forzar`, la primera foto del último día del mes (típicamente la
    foto diaria, tomada a media tarde) congelaba el cierre: todo lo que se
    cargaba después de esa hora quedaba fuera del PATANT del mes siguiente.
    """
    comp = {"patr": 1_000.0, "usret": 200.0, "utilidad": 50.0}
    monkeypatch.setattr(iq, "informe_balance", lambda *a, **k: _bal(comp))
    monkeypatch.setattr(iq, "informe_balance_as_of", lambda *a, **k: _bal(comp))
    # Ya hay una foto guardada para el 31/07 → sin forzar se corta.
    monkeypatch.setattr(iq.db, "fetch_one", lambda *a, **k: {"1": 1})

    sin_forzar = iq.crear_snapshot_historia(2026, 7)
    assert sin_forzar["aplicado"] is False
    assert "Ya existe snapshot de cierre" in sin_forzar["razon"]

    # …pero el DRY-RUN sí calcula igual: con una foto ya guardada, lo que se
    # quiere ver es cuánto cambiaría rehacerla. Si el dry-run se cortara acá,
    # el simulacro mostraría todo en null y parecería que no hay nada que
    # corregir. [[feedback_mostrar_lo_guardado]]
    seco = iq.crear_snapshot_historia(2026, 7, dry_run=True)
    assert seco["dry_run"] is True
    assert seco["ya_existe"] is True
    assert seco["row"]["patrimonio"] == 800.0

    sqls: list[str] = []

    def fake_execute(sql, params=None, conn=None):
        sqls.append(" ".join(sql.split()).lower())
        return 1

    monkeypatch.setattr(iq.db, "tx", _tx_dummy)
    monkeypatch.setattr(iq.db, "execute", fake_execute)
    monkeypatch.setattr(
        iq.db, "execute_returning", lambda *a, **k: {"id_historia": 777})

    con_forzar = iq.crear_snapshot_historia(2026, 7, forzar=True)

    assert con_forzar["aplicado"] is True
    assert con_forzar["id_historia"] == 777
    assert con_forzar["borradas"] == 1
    assert any("delete from scintela.historia where fecha" in s for s in sqls)
    # y lo que reescribe sigue siendo NETO de retiros
    assert con_forzar["patrimonio"] == 800.0
