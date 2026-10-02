"""El recordatorio automático de estado de cuenta — cada 2 semanas, los
lunes, salvo feriado de Ecuador (TMT 14/09/2026).

Lo que protegen estos tests:
* apagado (por switch o por env) no manda nada;
* sin próxima corrida programada, o si todavía no llegó la fecha, no manda;
* si la fecha programada es un lunes feriado (EC/Quito), NO manda y el
  puntero salta 14 días igual — no se corre al día siguiente;
* si manda, usa el contenido "recordatorio" (no "lanzamiento") y el puntero
  avanza 14 días desde la fecha que tocaba (no desde hoy);
* la corrida se TOMA antes de mandar: si otro proceso ya la tomó, no manda
  (02/10/2026: salió dos veces a todos);
* antes de la hora mínima, ese mismo día, no manda todavía.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.portal_aviso import envio, queries, recordatorio  # noqa: E402

CLIENTE = {"codigo_cli": "AJT", "nombre": "TOTOY BUITRON ANDRES JULIO", "vend": "EDG",
           "saldo": 10225.02, "correo": "contabilidad@totoy.com"}

# Un lunes cualquiera que NO es feriado en 2026.
LUNES_HABIL = date(2026, 9, 28)
# Lunes de Pichincha trasladado (feriado EC 2026, ver test_feriados()).
LUNES_FERIADO = date(2026, 5, 25)


def _reset(monkeypatch):
    monkeypatch.setattr(recordatorio, "_auto_ultimo", None)
    monkeypatch.delenv("PORTAL_AVISO_EC_AUTO", raising=False)
    monkeypatch.setattr(recordatorio, "_ahora_ec",
                        lambda: datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc))


def _mandar_falso(monkeypatch):
    llamadas = []

    def mandar(filas, quien, tipo="cliente", a="", contenido="lanzamiento"):
        llamadas.append({"filas": filas, "quien": quien, "tipo": tipo,
                          "a": a, "contenido": contenido})
        return {"enviados": len(filas), "fallidos": 0, "sin_correo": 0}

    monkeypatch.setattr(envio, "mandar", mandar)
    return llamadas


def _reclamar_falso(monkeypatch, gana=True):
    fijadas = []

    def reclamar(prevista, siguiente):
        if gana:
            fijadas.append(siguiente)
        return gana

    monkeypatch.setattr(queries, "reclamar_corrida_ec", reclamar)
    return fijadas


def _avisos_falso(monkeypatch):
    import modules.avisos.queries as avisos_queries
    avisados = []
    monkeypatch.setattr(avisos_queries, "avisar",
                        lambda **kw: avisados.append(kw) or True)
    return avisados


def test_apagado_por_switch_no_manda(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: False)
    llamadas = _mandar_falso(monkeypatch)
    res = recordatorio.correr_si_toca()
    assert res["corrio"] is False
    assert not llamadas


def test_apagado_por_env_no_manda(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("PORTAL_AVISO_EC_AUTO", "0")
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: True)
    llamadas = _mandar_falso(monkeypatch)
    res = recordatorio.correr_si_toca()
    assert res["corrio"] is False
    assert not llamadas


def test_sin_proxima_programada_no_manda(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: True)
    monkeypatch.setattr(queries, "proxima_corrida_ec", lambda: None)
    llamadas = _mandar_falso(monkeypatch)
    res = recordatorio.correr_si_toca()
    assert res["corrio"] is False
    assert not llamadas


def test_todavia_no_llega_la_fecha_no_manda(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: True)
    monkeypatch.setattr(queries, "proxima_corrida_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(recordatorio, "today_ec", lambda: LUNES_HABIL - timedelta(days=1))
    llamadas = _mandar_falso(monkeypatch)
    res = recordatorio.correr_si_toca()
    assert res["corrio"] is False
    assert not llamadas


def test_antes_de_la_hora_minima_no_manda_todavia(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: True)
    monkeypatch.setattr(queries, "proxima_corrida_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(recordatorio, "today_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(recordatorio, "_ahora_ec",
                        lambda: datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc))
    llamadas = _mandar_falso(monkeypatch)
    res = recordatorio.correr_si_toca()
    assert res["corrio"] is False
    assert not llamadas


def test_feriado_no_manda_y_igual_empuja_el_puntero_14_dias(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: True)
    monkeypatch.setattr(queries, "proxima_corrida_ec", lambda: LUNES_FERIADO)
    monkeypatch.setattr(recordatorio, "today_ec", lambda: LUNES_FERIADO)
    fijadas = _reclamar_falso(monkeypatch)
    llamadas = _mandar_falso(monkeypatch)
    res = recordatorio.correr_si_toca()
    assert res["corrio"] is False
    assert res["motivo"] == "feriado, salteado"
    assert not llamadas  # NO mandó nada
    assert fijadas == [LUNES_FERIADO + timedelta(days=14)]  # pero el puntero avanzó


def test_manda_con_contenido_recordatorio_y_empuja_14_dias(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: True)
    monkeypatch.setattr(queries, "proxima_corrida_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(recordatorio, "today_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(queries, "lista", lambda: [CLIENTE])
    fijadas = _reclamar_falso(monkeypatch)
    llamadas = _mandar_falso(monkeypatch)
    avisados = _avisos_falso(monkeypatch)

    res = recordatorio.correr_si_toca()

    assert res["corrio"] is True
    assert res["enviados"] == 1
    assert len(llamadas) == 1
    assert llamadas[0]["contenido"] == "recordatorio"
    assert [f["codigo_cli"] for f in llamadas[0]["filas"]] == ["AJT"]
    assert fijadas == [LUNES_HABIL + timedelta(days=14)]
    assert len(avisados) == 1
    assert "estado de cuenta" in avisados[0]["titulo"].lower()


def test_nadie_con_correo_no_manda_pero_empuja_el_puntero(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: True)
    monkeypatch.setattr(queries, "proxima_corrida_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(recordatorio, "today_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(queries, "lista", lambda: [{**CLIENTE, "correo": ""}])
    fijadas = _reclamar_falso(monkeypatch)
    llamadas = _mandar_falso(monkeypatch)

    res = recordatorio.correr_si_toca()

    assert res["corrio"] is False
    assert res["motivo"] == "nadie con correo"
    assert not llamadas
    assert fijadas == [LUNES_HABIL + timedelta(days=14)]


def test_el_texto_del_recordatorio_no_dice_portal_nuevo():
    texto = envio.texto_recordatorio("TOTOY BUITRON ANDRES JULIO")
    assert "portal nuevo" not in texto.lower()
    assert "estado de cuenta" in texto.lower()
    assert envio.PORTAL_URL in texto


def test_el_html_del_recordatorio_no_dice_portal_nuevo():
    html = envio.html_recordatorio("TOTOY BUITRON ANDRES JULIO")
    assert "portal nuevo" not in html.lower()
    assert envio.PORTAL_URL in html


# ---------------------------------------------------------------------------
# TMT 02/10/2026 — el recordatorio salió DOS veces a todos (09:33 y 09:36 EC):
# dos procesos del servidor vieron la fecha vencida a la vez.
# ---------------------------------------------------------------------------


def test_si_otro_proceso_ya_tomo_la_corrida_no_manda(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: True)
    monkeypatch.setattr(queries, "proxima_corrida_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(recordatorio, "today_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(queries, "lista", lambda: [CLIENTE])
    _reclamar_falso(monkeypatch, gana=False)
    llamadas = _mandar_falso(monkeypatch)
    res = recordatorio.correr_si_toca()
    assert res["corrio"] is False
    assert "otro proceso" in res["motivo"]
    assert not llamadas


def test_dos_procesos_a_la_vez_mandan_una_sola_vez(monkeypatch):
    """Dos hilos con su propio freno en memoria (como dos procesos del
    servidor) leen la MISMA fecha vencida al mismo tiempo; la base (acá, un
    dict con candado que hace lo que hace el UPDATE condicional) deja ganar
    a uno solo."""
    import threading

    monkeypatch.delenv("PORTAL_AVISO_EC_AUTO", raising=False)
    monkeypatch.setattr(recordatorio, "_ahora_ec",
                        lambda: datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(queries, "ec_auto_encendido", lambda: True)
    monkeypatch.setattr(recordatorio, "today_ec", lambda: LUNES_HABIL)
    monkeypatch.setattr(queries, "lista", lambda: [CLIENTE])
    _avisos_falso(monkeypatch)

    base = {"proxima": LUNES_HABIL}
    cerrojo = threading.Lock()
    leyeron = threading.Barrier(2)

    def proxima():
        v = base["proxima"]
        leyeron.wait(timeout=5)  # los dos leen la fecha vieja antes de seguir
        return v

    def reclamar(prevista, siguiente):
        with cerrojo:
            if base["proxima"] != prevista:
                return False
            base["proxima"] = siguiente
            return True

    monkeypatch.setattr(queries, "proxima_corrida_ec", proxima)
    monkeypatch.setattr(queries, "reclamar_corrida_ec", reclamar)
    llamadas = _mandar_falso(monkeypatch)

    def proceso():
        recordatorio._auto_ultimo = None  # cada proceso tiene su freno propio
        recordatorio.correr_si_toca()

    hilos = [threading.Thread(target=proceso) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=10)

    assert len(llamadas) == 1
    assert base["proxima"] == LUNES_HABIL + timedelta(days=14)
