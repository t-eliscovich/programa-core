"""Mercadería que salió y no facturó: renglón durante el día, aviso al otro.

TMT 2026-08-11, en dos pasos. Primero avisaba a las 17:00 con todo lo que ese
día seguía sin factura; estrenó con 6 despachos y quedó feo — cuatro se
facturaron esa misma tarde y dos estaban anulados: *"no me gustó el aviso,
parecía que algo estaba mal cuando no era el caso"*. La regla nueva la dio
ella: *"al día siguiente si no se cargó"*. El renglón de contexto durante el
día también se descartó: *"durante el día un renglón, esto no"*.

Lo que estos tests fijan:

  · **Lo del día en curso no se dice en ningún lado.** Ni campanita ni
    renglón: *"durante el día un renglón, esto no"*. Es el test que de verdad
    hay que defender.
  · **Lo que amaneció sin factura SÍ**, un aviso por despacho (clave por
    número), para que el mismo caso no grite todas las mañanas.
  · **El reloj de Asinfo es UTC y `fecha_creacion` no.** La consulta tiene que
    preguntar la hora de Ecuador o el día se corre cinco horas.
"""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from modules.asinfo import despacho_sin_factura as ds

HOY = [
    {"numero": "DES-000095225", "dia": "2026-08-11", "kg": 880.4, "nota": "PUYO",
     "creado": "16:44", "horas": 0.3},
    {"numero": "DES-000095228", "dia": "2026-08-11", "kg": 21.9, "nota": "",
     "creado": "16:59", "horas": 0.1},
]
AYER = [
    {"numero": "DES-000095134", "dia": "2026-08-10", "kg": 987.8,
     "nota": "TONO CEN S/M ADJUNTO FOTO", "creado": "10:07", "horas": 31.5},
]


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    monkeypatch.delenv("DESPACHO_SIN_FACTURA", raising=False)
    monkeypatch.delenv("DESPACHO_SIN_FACTURA_HORA", raising=False)


def _a_las(h):
    """Lo que devuelve `_ahora_ec()`: el reloj de Ecuador, no el del server."""
    return datetime(2026, 8, 11, h, 30, tzinfo=UTC)


def _correr(casos, hora_ec=8):
    puestos = []
    with patch.object(ds, "_ahora_ec", return_value=_a_las(hora_ec)), \
         patch.object(ds, "pendientes", return_value=casos), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: puestos.append(kw) or True):
        res = ds.revisar_si_toca()
    return res, puestos


# ── la campanita ────────────────────────────────────────────────────────────

def test_lo_de_hoy_no_enciende_la_campanita():
    """⭐ El aviso que la molestó. Los 6 del estreno tenían menos de 41 minutos
    y cuatro se facturaron esa misma tarde: a media tarde esto es una tarde
    normal, no una alarma."""
    res, puestos = _correr(HOY, hora_ec=17)
    assert res["avisados"] == 0
    assert res["motivo"] == "no quedó nada sin cargar"
    assert puestos == []


def test_lo_que_amanecio_sin_factura_si_avisa():
    res, puestos = _correr(HOY + AYER)
    assert res["avisados"] == 1
    a = puestos[0]
    assert a["titulo"] == ("Despacho sin factura del 2026-08-10 · 988 kg · "
                           "DES-000095134")
    assert a["nivel"] == "alerta"
    assert "TONO CEN S/M ADJUNTO FOTO" in a["detalle"]


def test_un_aviso_por_despacho_no_uno_por_dia():
    """La clave va por número: si nadie lo carga, el mismo despacho no grita
    todas las mañanas hasta que se lo ignore por costumbre."""
    otro = dict(AYER[0], numero="DES-000095140", dia="2026-08-09")
    _, puestos = _correr(AYER + [otro])
    assert [p["clave"] for p in puestos] == ["desp-sin-factura:DES-000095134",
                                             "desp-sin-factura:DES-000095140"]


def test_de_madrugada_no_dice_nada():
    """A las 3 de la mañana no hay nadie para cargar la factura."""
    res, puestos = _correr(AYER, hora_ec=3)
    assert res["avisados"] == 0
    assert res["motivo"] == "todavía no son las 8"
    assert puestos == []


def test_apagado_por_env(monkeypatch):
    monkeypatch.setenv("DESPACHO_SIN_FACTURA", "0")
    res, puestos = _correr(AYER)
    assert res["motivo"] == "apagado"
    assert puestos == []


# ── la consulta ────────────────────────────────────────────────────────────

def _sql():
    visto = {}

    def _fake(db_id, sql, *a, **k):
        visto["db"], visto["sql"] = db_id, sql
        return []

    with patch("modules._lib.metabase_client.fetch_dataset", side_effect=_fake):
        ds.pendientes()
    return visto


def test_los_anulados_quedan_afuera_por_definicion():
    """28 de los 32 'sin factura' de agosto estaban ANULADOS: volvieron a
    bodega y no les corresponde factura. Contarlos convierte la operación
    normal de un depósito en una alarma."""
    assert "dc.fecha_anulacion IS NULL" in _sql()["sql"]


def test_va_contra_asinfo_y_solo_lo_no_facturado():
    v = _sql()
    assert v["db"] == 2
    assert "dc.indicador_generado_factura = 0" in v["sql"]


def test_la_consulta_pregunta_la_hora_de_ecuador_no_la_del_server():
    """🚨 Verificado en vivo el 11/08: `GETDATE()` devolvió las 00:12 del 12
    cuando en Ecuador eran las 19:12, pero `fecha_creacion` venía en hora
    local (el último despacho del día, 17:51). Con `GETDATE()` pelado el día
    se corre cinco horas: después de las 19:00 EC la consulta dejaba de ver el
    día en curso, y la antigüedad salía 5 h de más."""
    sql = _sql()["sql"]
    assert "DATEADD(hour, -5, GETDATE())" in sql
    # No queda ni un `GETDATE()` suelto: los dos usos —el corte del día y la
    # antigüedad— tienen que estar corridos.
    assert sql.count("GETDATE()") == sql.count("DATEADD(hour, -5, GETDATE())")
    assert "CAST(GETDATE() AS date)" not in sql


def test_mira_treinta_dias_para_atras():
    """Sólo hoy no alcanza: el aviso es al día siguiente."""
    assert "DATEADD(day, -30," in _sql()["sql"]


def test_un_despacho_sin_kilos_no_se_lista():
    with patch("modules._lib.metabase_client.fetch_dataset",
               return_value=[{"numero": "DES-1", "kg": 0, "nota": "",
                              "creado": "", "dia": "2026-08-11",
                              "minutos": 60}]):
        assert ds.pendientes() == []


def test_si_asinfo_no_habla_no_se_rompe_nada():
    with patch("modules._lib.metabase_client.fetch_dataset",
               side_effect=RuntimeError("Metabase 502")):
        assert ds.pendientes() == []
