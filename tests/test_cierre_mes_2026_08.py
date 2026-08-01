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

def _bal(componentes: dict) -> dict:
    return {
        "diagnostico": {"componentes": componentes},
        "kg": {},
        "stock_subpanels": {},
    }


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
