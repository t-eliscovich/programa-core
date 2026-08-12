"""El $/kg del hilado no se recalcula cuando la fuente no contestó.

TMT 2026-08-12. La dueña abrió Resultados y vio la utilidad en **78.149**.
La traza del día:

    12/08 08:24   utilidad 142.485   $/kg 3,0443   compras 243.546 kg / 787.554 US$
    12/08 08:30   utilidad  78.149   $/kg 3,0201   compras       0 kg /       0 US$
                  └ cambió el $/kg de 3 etapas   −64.393

Asinfo dejó de contestar y las compras del mes se fueron a cero, porque la
lista de importaciones que las cruza vive allá. Sin compras el promedio
ponderado no se diluye y el $/kg vuelve al de apertura (3,0201): −0,0242 sobre
2.660.021 kg de stock = −64.393 de utilidad que nadie perdió.

Los dos guardias que teníamos miraban para otro lado:

· el de asimetría (31/07) busca "kg sin dólares" y "dólares sin kg"; acá
  vinieron LOS DOS en cero, que desde adentro es idéntico a un mes sin compras;
· `/admin/health/hilado-ukg` compara el $/kg mostrado contra el esperado CON
  esas mismas compras en cero — o sea, valida la cuenta y no los insumos.
  Dijo `ok: true` toda la mañana.

Regla de la dueña: *"no deberías bajarlo tanto, sólo a los 5 minutos
anteriores"* → el $/kg se queda donde estaba hasta que Asinfo vuelva.
"""
from unittest.mock import patch

import pytest

from modules.asinfo import service as sv
from modules.importaciones import service as isv

HI0, OPEN = 1_775_736.0, 3.0201
INV_INIC = {"hilo": HI0}
INV_ACT = {"hilo": 2_015_927.0, "en_proceso_tc": 45_880.0}
TARIFA_BUENA = 3.0443


@pytest.fixture(autouse=True)
def _sin_memoria():
    """Cada test arranca sin la tarifa del anterior."""
    sv._ULTIMA_TARIFA_HILADO = None
    yield
    sv._ULTIMA_TARIFA_HILADO = None


def _valuar(kg, us, dolares_propios=0.0, previa=0.0):
    rec = {"us": us, "kg": kg, "kg_con_costo": kg, "usd_kg": None}
    with patch.object(sv, "inventario_por_etapa_a_fecha", return_value=INV_INIC), \
         patch.object(sv, "inventario_por_etapa", return_value=INV_ACT), \
         patch.object(sv, "hilado_recibido_mes", return_value=kg), \
         patch.object(isv, "costo_hilado_recibido_mes", return_value=rec), \
         patch("modules.compras_locales.service.hilado_local_recibido_mes",
               return_value={"kg": 0.0, "us": 0.0}), \
         patch.object(sv, "dolares_hilo_del_mes", return_value=dolares_propios), \
         patch.object(sv, "_tarifa_hilado_previa", return_value=previa):
        return sv.mov_hilado_valuacion(2026, 8, OPEN)


# ── El caso del 12/08 ──────────────────────────────────────────────────────

def test_las_compras_en_cero_con_hilo_comprado_no_bajan_el_kg():
    out = _valuar(0.0, 0.0, dolares_propios=813_806.10, previa=TARIFA_BUENA)
    assert out["stock_act_ukg"] == pytest.approx(TARIFA_BUENA)
    assert out["tarifa_congelada"] is True
    assert "no contestó" in out["tarifa_motivo"]


def test_el_stock_no_se_revalua_mientras_asinfo_no_contesta():
    """Los 64.393: la diferencia entre sostener la tarifa y recalcularla."""
    sano = _valuar(243_546.0, 787_554.0)
    caido = _valuar(0.0, 0.0, dolares_propios=813_806.10, previa=sano["stock_act_ukg"])
    stock_total_kg = 2_660_021.0
    salto = (caido["stock_act_ukg"] - sano["stock_act_ukg"]) * stock_total_kg
    assert abs(salto) < 1.0, f"el stock se movió {salto:,.0f} sin que pasara nada"


def test_el_comportamiento_viejo_movia_64_mil():
    """Guard de regresión: documenta el tamaño de lo que se sacó."""
    sano = _valuar(243_546.0, 787_554.0)
    viejo = _valuar(0.0, 0.0, dolares_propios=0.0)  # sin testigo → como antes
    salto = (viejo["stock_act_ukg"] - sano["stock_act_ukg"]) * 2_660_021.0
    assert salto < -50_000, f"esperaba el escalón viejo, dio {salto:,.0f}"


# ── Lo que NO tiene que cambiar ────────────────────────────────────────────

def test_un_mes_sin_compras_de_verdad_sigue_usando_la_apertura():
    """Sin hilo comprado en el programa, cero compras es cero compras."""
    out = _valuar(0.0, 0.0, dolares_propios=0.0, previa=TARIFA_BUENA)
    assert out["stock_act_ukg"] == pytest.approx(OPEN)
    assert out["tarifa_congelada"] is False
    assert out["tarifa_motivo"] == ""


def test_con_compras_normales_diluye_como_siempre():
    out = _valuar(243_546.0, 787_554.0)
    esperado = (HI0 * OPEN + 787_554.0) / (HI0 + 243_546.0)
    assert out["avg_ukg"] == pytest.approx(esperado)
    assert out["tarifa_motivo"] == ""


def test_la_asimetria_tambien_sostiene_la_tarifa_en_vez_de_caer_a_la_apertura():
    """Mismo bug, la otra puerta: kilos sin dólares caían a la apertura."""
    out = _valuar(243_546.0, 0.0, previa=TARIFA_BUENA)
    assert out["asimetrico"] is True
    assert out["stock_act_ukg"] == pytest.approx(TARIFA_BUENA)
    assert out["tarifa_congelada"] is True


def test_sin_tarifa_anterior_cae_a_la_apertura_pero_lo_dice():
    """Proceso recién arrancado y sin fotos: no hay con qué sostenerla."""
    out = _valuar(0.0, 0.0, dolares_propios=813_806.10, previa=0.0)
    assert out["stock_act_ukg"] == pytest.approx(OPEN)
    assert out["tarifa_congelada"] is False
    assert out["tarifa_motivo"], "tiene que quedar el motivo para el cartel"


# ── La memoria ─────────────────────────────────────────────────────────────

def test_solo_se_recuerda_la_tarifa_calculada_con_las_compras_a_la_vista():
    _valuar(243_546.0, 787_554.0)
    buena = sv._ULTIMA_TARIFA_HILADO
    assert buena and buena["mm"] == 8
    _valuar(0.0, 0.0, dolares_propios=813_806.10, previa=buena["ukg"])
    assert buena == sv._ULTIMA_TARIFA_HILADO, "la foto enferma no puede ser el ancla"


# ── Los KILOS también se quedan quietos (TMT 2026-08-12, tarde) ────────────
# El fix de arriba salió a producción con Asinfo todavía caído, y el reinicio
# del deploy dejó a la vista la puerta de al lado: sin inventario y sin
# memoria, el balance caía a los kilos estilo dBase (apertura del mes ±
# movimientos) y la utilidad SUBIÓ de 78.149 a 172.565 — hilado 2.015.927 →
# 1.895.247 kg pero valuado a 3,4495 en vez de 3,0201. Dueña: *"subió de más,
# fijate. dbase no puede ser"* → *"tomar los kilos de la última foto de la
# traza, exactamente esto quiero"*.

from modules.informes import traza as _traza  # noqa: E402


def test_la_foto_ancla_ignora_las_sacadas_sin_asinfo():
    """`compras_us = 0` es justo la foto enferma: no puede ser el ancla."""
    fila = {"hilado_kg": 2_015_927.0, "hilado_ukg": 3.0443,
            "tejido_kg": 298_518.0, "terminado_kg": 345_576.0}
    with patch.object(_traza.db, "fetch_one", return_value=fila) as f:
        assert _traza.foto_stock_buena() == fila
    sql = f.call_args[0][0]
    assert "COALESCE(compras_us, 0) > 0" in sql
    assert "date_trunc" in sql, "el ancla tiene que ser del MES en curso"


def test_sin_foto_no_inventa_nada():
    with patch.object(_traza.db, "fetch_one", side_effect=RuntimeError("sin base")):
        assert _traza.foto_stock_buena() is None


def test_el_balance_prefiere_la_foto_antes_que_los_kilos_del_dbase():
    """El orden importa: primero el ancla, el dBase sólo si no hay ninguna."""
    import inspect

    from modules.informes import queries as q
    src = inspect.getsource(q.informe_balance)
    assert "foto_stock_buena()" in src
    i_foto = src.index("foto_stock_buena()")
    i_dbase = src.index("valuando con los kilos del dBase")
    assert i_foto < i_dbase, "el dBase tiene que ser el último recurso"
    # Y los kilos de la foto no pueden viajar con la tarifa de otra fuente.
    assert '_stock_fuente == "traza" and _foto_stk' in src
    assert 'h_um = float(_foto_stk["hilado_ukg"])' in src


# ── Y el balance de verdad, no sólo su código fuente ───────────────────────

from tests.test_balance_conciliacion import fake_balance_db  # noqa: E402,F401

FOTO = {
    "creado_en": None,
    "hilado_kg": 2_015_927.0, "hilado_ukg": 3.0443,
    "tejido_kg": 298_518.0, "terminado_kg": 345_576.0,
}


def _balance_sin_asinfo(monkeypatch, foto):
    from modules.asinfo import service as asvc
    from modules.informes import queries as q
    from modules.informes import traza as tz
    monkeypatch.setattr(asvc, "inventario_por_etapa",
                        lambda: {"disponible": False, "hilo_total": 0.0,
                                 "cruda_total": 0.0, "terminada": 0.0})
    monkeypatch.setattr(tz, "foto_stock_buena", lambda: foto)
    return q.informe_balance()


def test_sin_asinfo_el_balance_se_queda_en_la_foto(fake_balance_db, monkeypatch):
    b = _balance_sin_asinfo(monkeypatch, dict(FOTO))
    et = b["stock_etapas"]
    assert b["stock_fuente"] == "traza"
    assert et["hilado"]["kg"] == pytest.approx(2_015_927.0)
    assert et["tejido"]["kg"] == pytest.approx(298_518.0)
    assert et["terminado"]["kg"] == pytest.approx(345_576.0)
    # los kilos de la foto NO pueden viajar con la tarifa de otra fuente
    assert et["hilado"]["ukg"] == pytest.approx(3.0443)
    assert et["tejido"]["ukg"] == pytest.approx(3.5443)
    assert et["terminado"]["ukg"] == pytest.approx(5.2443)


def test_sin_foto_el_balance_avisa_que_valua_con_el_dbase(fake_balance_db, monkeypatch):
    b = _balance_sin_asinfo(monkeypatch, None)
    assert b["stock_fuente"] == "dbase"
    avisos = (b.get("diagnostico") or {}).get("advertencias") or []
    assert any("kilos del dBase" in a for a in avisos), avisos
