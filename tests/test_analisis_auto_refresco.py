"""El refresco SOLO de la foto de saldos (hilo de fondo).

Lo que se prueba es lo que rompería la competencia sin dar ningún síntoma:

  · que el freno mire la BASE y no una variable de proceso (si no, cada deploy
    reinicia la cuenta y le pega a Asinfo de nuevo);
  · que un Asinfo caído no tire el ciclo abajo ni reintente cada dos minutos;
  · que el hilo de fondo lo llame de verdad — sin esa línea el módulo entero es
    código muerto y la pantalla se queda en la foto del día que alguien apretó
    el botón por última vez.
"""

from __future__ import annotations

import modules.analisis.auto_refresco as ar

# ⚠ El módulo se planta bajo pytest a propósito (mismo criterio que la
# auto-carga de facturas): los tests le sacan ese freno a mano.


def _sin_frenos(monkeypatch):
    monkeypatch.delenv("ANALISIS_AUTO", raising=False)
    monkeypatch.setattr(ar.os, "environ", {})
    monkeypatch.setattr(ar, "_ultimo_intento", None)


def test_no_corre_si_la_foto_es_reciente(monkeypatch):
    _sin_frenos(monkeypatch)
    monkeypatch.setattr(ar, "_toca", lambda: False)
    llamadas = []
    monkeypatch.setattr("modules.analisis.queries.actualizar",
                        lambda: llamadas.append(1))
    assert ar.correr_si_toca()["corrio"] is False
    assert not llamadas, "con la foto fresca no se le pega a Asinfo"


def test_corre_si_la_foto_esta_vieja(monkeypatch):
    _sin_frenos(monkeypatch)
    monkeypatch.setattr(ar, "_toca", lambda: True)
    monkeypatch.setattr("modules.analisis.queries.actualizar",
                        lambda: {"items": 710, "llamados": 649})
    r = ar.correr_si_toca()
    assert r["corrio"] is True
    assert r["items"] == 710 and r["llamados"] == 649


def test_el_freno_lo_decide_la_BASE_no_el_proceso(monkeypatch):
    """`parado_refresh.actualizado` es el reloj. Con un contador en memoria, un
    restart del servidor —o el botón que aprieta alguien a mano— quedaban
    fuera de la cuenta."""
    visto = {}

    def fake_fetch_one(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split())
        visto["params"] = params
        return {"toca": False}

    monkeypatch.setattr(ar.db, "fetch_one", fake_fetch_one)
    assert ar._toca() is False
    assert "scintela.parado_refresh" in visto["sql"]
    assert "NOW()" in visto["sql"], (
        "la resta la tiene que hacer Postgres: el contenedor está en UTC y "
        "Ecuador no")


def test_asinfo_caido_no_tira_el_ciclo_ni_reintenta_enseguida(monkeypatch):
    _sin_frenos(monkeypatch)
    monkeypatch.setattr(ar, "_toca", lambda: True)
    intentos = []

    def explota():
        intentos.append(1)
        raise RuntimeError("Metabase 502")

    monkeypatch.setattr("modules.analisis.queries.actualizar", explota)
    assert ar.correr_si_toca()["corrio"] is False   # no levanta
    assert ar.correr_si_toca()["corrio"] is False   # y el segundo ni prueba
    assert len(intentos) == 1, (
        "sin el freno de error, el ciclo de 120 s reintentaría para siempre")


def test_se_apaga_con_la_variable(monkeypatch):
    monkeypatch.setattr(ar.os, "environ", {"ANALISIS_AUTO": "0"})
    monkeypatch.setattr(ar, "_toca", lambda: True)
    monkeypatch.setattr("modules.analisis.queries.actualizar",
                        lambda: 1 / 0)
    assert ar.correr_si_toca()["corrio"] is False


def test_no_baja_de_media_hora(monkeypatch):
    monkeypatch.setattr(ar.os, "environ", {"ANALISIS_REFRESCO_HORAS": "0.01"})
    assert ar._horas() == 0.5
    monkeypatch.setattr(ar.os, "environ", {"ANALISIS_REFRESCO_HORAS": "cada tanto"})
    assert ar._horas() == 3.0


def test_el_hilo_de_fondo_lo_llama():
    """Sin esta línea el módulo no lo corre nadie y la pantalla se congela."""
    with open("modules/_lib/autocarga_facturas.py", encoding="utf-8") as f:
        src = f.read()
    assert "from modules.analisis import auto_refresco" in src
    assert "_saldos.correr_si_toca()" in src
