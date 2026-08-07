"""La nota del cierre, por mail.

TMT 2026-08-06: *"la nota diaria por mail… hacelo con teliscovich@gmail.com"*.

El bloqueo de WhatsApp era regulatorio (la Business API de Meta exige template
aprobado, número verificado y un BSP con costo por conversación); el mail no
tiene ninguno. Lo que sí tiene es una trampa: el hilo de fondo pasa cada dos
minutos, así que sin idempotencia EN LA BASE la nota sale cada dos minutos.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules._lib import mailer  # noqa: E402
from modules.informes import dia  # noqa: E402

# ── El transporte ───────────────────────────────────────────────────────────

def test_sin_destinatarios_no_manda_nada():
    r = mailer.enviar("asunto", "texto", [])
    assert r["ok"] is False and r["motivo"] == "sin destinatarios"


def test_se_apaga_por_entorno_sin_tocar_codigo():
    """Igual que DIA_EXPLICACION: si algo sale mal a las 18:00 de un viernes,
    se apaga sin un deploy."""
    with patch.dict(os.environ, {"MAIL_ENVIAR": "0"}):
        assert mailer.habilitado() is False
        assert "apagado" in mailer.motivo_no_disponible()
        assert mailer.enviar("a", "b", ["x@y.com"])["ok"] is False


def test_un_error_de_ses_no_levanta():
    """Esto cuelga del hilo de fondo: un mail que no sale no puede tumbar la
    captura del cierre."""
    with patch.object(mailer, "habilitado", return_value=True), \
         patch.dict(sys.modules, {"boto3": None}):
        r = mailer.enviar("a", "b", ["x@y.com"])
    assert r["ok"] is False and r["motivo"]


# ── La idempotencia ─────────────────────────────────────────────────────────

def _cierre(enviada=None):
    return [{"id_captura": 7, "nota_enviada_en": enviada}]


def test_la_nota_sale_una_sola_vez_por_dia():
    """🚨 El sello va en la BASE, no en una variable de proceso: el hilo pasa
    cada dos minutos y el server reinicia. Y se pone ANTES de mandar, con un
    UPDATE condicional, así dos procesos simultáneos no mandan los dos."""
    with patch.object(dia, "_rows", return_value=_cierre()), \
         patch.object(dia, "destinatarios",
                      return_value=[{"correo": "teliscovich@gmail.com", "activo": True}]), \
         patch.object(mailer, "habilitado", return_value=True), \
         patch.object(dia, "mensaje_whatsapp", return_value="hola"), \
         patch.object(mailer, "enviar", return_value={"ok": True, "id": "m1"}) as env, \
         patch.object(dia.db, "execute", return_value=1) as ex:
        r = dia.enviar_nota()
    assert r["ok"] is True and r["destinatarios"] == 1
    env.assert_called_once()
    # El sello se toma con un UPDATE que exige que esté en NULL.
    assert "nota_enviada_en IS NULL" in ex.call_args_list[0][0][0]


def test_si_ya_se_mando_no_se_manda_de_nuevo():
    with patch.object(dia, "_rows", return_value=_cierre(enviada="2026-08-06")), \
         patch.object(mailer, "enviar") as env:
        r = dia.enviar_nota()
    assert r["ok"] is False and "ya se mandó" in r["motivo"]
    env.assert_not_called()


def test_si_otro_proceso_gano_la_carrera_este_no_manda():
    """Los dos ven `nota_enviada_en` en NULL; el UPDATE condicional sólo lo
    agarra uno."""
    with patch.object(dia, "_rows", return_value=_cierre()), \
         patch.object(dia, "destinatarios",
                      return_value=[{"correo": "a@b.com", "activo": True}]), \
         patch.object(mailer, "habilitado", return_value=True), \
         patch.object(dia.db, "execute", return_value=0), \
         patch.object(mailer, "enviar") as env:
        r = dia.enviar_nota()
    assert r["ok"] is False and "ya se mandó" in r["motivo"]
    env.assert_not_called()


def test_si_el_envio_falla_se_libera_para_reintentar():
    """Si el sello quedara puesto, un error de red se comería la nota del día."""
    ejecutadas = []
    with patch.object(dia, "_rows", return_value=_cierre()), \
         patch.object(dia, "destinatarios",
                      return_value=[{"correo": "a@b.com", "activo": True}]), \
         patch.object(mailer, "habilitado", return_value=True), \
         patch.object(dia, "mensaje_whatsapp", return_value="hola"), \
         patch.object(mailer, "enviar", return_value={"ok": False, "motivo": "red"}), \
         patch.object(dia.db, "execute",
                      side_effect=lambda sql, *a, **k: ejecutadas.append(sql) or 1):
        r = dia.enviar_nota()
    assert r["ok"] is False
    assert "nota_enviada_en = NULL" in ejecutadas[-1]


def test_sin_destinatarios_activos_ni_se_intenta():
    with patch.object(dia, "_rows", return_value=_cierre()), \
         patch.object(dia, "destinatarios",
                      return_value=[{"correo": "a@b.com", "activo": False}]), \
         patch.object(mailer, "enviar") as env:
        r = dia.enviar_nota()
    assert "destinatarios" in r["motivo"]
    env.assert_not_called()


# ── Los destinatarios ───────────────────────────────────────────────────────

def test_un_correo_que_no_es_un_correo_no_entra():
    for malo in ("", "sin-arroba", "con espacio@x.com", "a@" + "x" * 300):
        ok, _msg = dia.agregar_destinatario(malo, "", "tamara")
        assert ok is False, malo


def test_el_correo_se_guarda_en_minusculas():
    with patch.object(dia.db, "execute") as ex:
        ok, _ = dia.agregar_destinatario("  Teliscovich@Gmail.COM ", "Tamara", "tamara")
    assert ok is True
    assert ex.call_args[0][1][0] == "teliscovich@gmail.com"


# ── El aviso de lo que no cerró ─────────────────────────────────────────────

def test_las_fotos_viejas_no_cuentan_como_que_no_cierran():
    """⭐ No es que no cierren: nunca tuvieron registro. Contarlas sería meter
    200 problemas inventados en la nota que la dueña lee una vez por día."""
    filas = [{"id_traza": 1, "d": 5000.0, "explicado": 0.0, "recon": 2, "n": 2},
             {"id_traza": 2, "d": 1000.0, "explicado": 400.0, "recon": 0, "n": 3}]
    with patch.object(dia, "_rows", return_value=filas):
        r = dia.ventanas_sin_cerrar()
    assert r["n"] == 1                 # sólo la segunda
    assert r["monto"] == 600.0


def test_la_linea_solo_aparece_si_hay_algo_que_decir():
    """Una línea que sale todos los días entrena a no leerla."""
    with patch.object(dia, "_rows", return_value=[]):
        assert dia.ventanas_sin_cerrar()["n"] == 0
