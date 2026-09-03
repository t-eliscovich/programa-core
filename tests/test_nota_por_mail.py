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
from datetime import date
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

# Un día hábil cualquiera: sin fecha explícita, `enviar_nota()` toma HOY y los
# tests de idempotencia se caerían solos un sábado (la nota sale lun-vie).
_JUEVES = date(2026, 8, 13)
_SABADO = date(2026, 8, 15)


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
         patch.object(mailer, "enviar", return_value={"ok": True, "id": "m1", "enviados": 1,
                                     "fallidos": 0, "motivo": ""}) as env, \
         patch.object(dia.db, "execute", return_value=1) as ex:
        r = dia.enviar_nota(_JUEVES)
    assert r["ok"] is True and r["destinatarios"] == 1
    env.assert_called_once()
    # El sello se toma con un UPDATE que exige que esté en NULL.
    assert "nota_enviada_en IS NULL" in ex.call_args_list[0][0][0]


def test_si_ya_se_mando_no_se_manda_de_nuevo():
    with patch.object(dia, "_rows", return_value=_cierre(enviada="2026-08-06")), \
         patch.object(mailer, "enviar") as env:
        r = dia.enviar_nota(_JUEVES)
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
        r = dia.enviar_nota(_JUEVES)
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
        r = dia.enviar_nota(_JUEVES)
    assert r["ok"] is False
    assert "nota_enviada_en = NULL" in ejecutadas[-1]


def test_sin_destinatarios_activos_ni_se_intenta():
    with patch.object(dia, "_rows", return_value=_cierre()), \
         patch.object(dia, "destinatarios",
                      return_value=[{"correo": "a@b.com", "activo": False}]), \
         patch.object(mailer, "enviar") as env:
        r = dia.enviar_nota(_JUEVES)
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


# ── El mail en HTML ─────────────────────────────────────────────────────────
# TMT 2026-08-13: *"los * en el mail se siguen viendo como asteriscos, podemos
# ver de que el formato nos quede mas lindo?"*.

_RES_OK = {"ok": True, "dia_parcial": False,
           "hasta": {"utilidad": 200538.0}, "d_utilidad": 43482.0,
           "ventas": {"n": 118, "kg": 15809.0, "us": 133547.0},
           "margen_pct": 37.9, "cobrado": 118132.0,
           "cobranza": {"n": 12, "us": 98765.0},
           "produccion": {"disponible": True, "producido": 11808.0,
                          "mes": {"producido": 116052.0}}}
_VM = {"n": 900, "kg": 105667.0, "us": 917352.0}


def _html(**over):
    from datetime import date
    res = {**_RES_OK, **over}
    with patch.object(dia, "resumen", return_value=res), \
         patch.object(dia, "ventas_del_mes", return_value=_VM), \
         patch.object(dia, "cobranza_del_mes",
                      return_value={"n": 300, "us": 654321.0}):
        return dia.nota_html(date(2026, 8, 12))


def test_el_mail_no_lleva_asteriscos_de_whatsapp():
    """`*negrita*` es sintaxis de WhatsApp: en un mail se ve como un asterisco
    al lado de cada título."""
    h = _html()
    assert "*" not in h
    assert "<div" in h and "200.538" in h


def test_el_html_no_depende_de_nada_externo():
    """Gmail y Outlook borran los <style> y bloquean las imágenes remotas: todo
    el estilo va en línea, con colores literales, y no hay ni un <img>."""
    h = _html()
    assert "<style" not in h and "<img" not in h
    assert "var(--" not in h
    assert h.count("style=\"") >= 5


def test_el_dia_en_verde_y_en_rojo():
    assert "+43.482" in _html() and dia._VERDE in _html()
    h = _html(d_utilidad=-43482.0)
    assert "−43.482" in h and dia._ROJO in h


# ── La grilla de F. ─────────────────────────────────────────────────────────
# F. (accionista) 2026-09-03: *"me gustaría sugerir este formato: kg · $/kg · $
# — venta, utilidad, producción y cobranzas, del día y del mes. Estos son los
# datos que más miramos cada día."*

def test_la_grilla_lleva_las_ocho_filas_de_f():
    h = _html()
    for rot in ("Venta del día", "Venta del mes", "Utilidad del día",
                "Utilidad del mes", "Producción del día", "Producción del mes",
                "Cobranzas del día", "Cobranzas del mes"):
        assert rot in h, rot
    # las tres columnas, con el $/kg calculado de los mismos kilos y pesos
    assert ">kg<" in h and ">$/kg<" in h
    assert "8,45" in h        # 133.547 / 15.809 del día
    assert "8,68" in h        # 917.352 / 105.667 del mes
    assert "98.765" in h and "654.321" in h   # cobranzas día y mes
    assert "116.052" in h                     # producción del mes


def test_cobranzas_son_lo_ingresado_no_lo_derivado():
    """F.: *"estos serían los cheques o eft. ingresados"*. El `cobrado`
    derivado (facturado − Δ cartera) no va al mail."""
    h = _html(cobrado=118132.0, cobranza={"n": 1, "us": 500.0})
    assert "118.132" not in h and "500" in h


def test_si_un_dato_no_esta_la_fila_no_va():
    """Mismo criterio que el texto de WhatsApp: una fila en cero no dice nada."""
    h = _html(produccion={"disponible": False}, cobranza={"n": 0, "us": 0.0},
              ventas={"n": 0, "kg": 0, "us": 0}, margen_pct=None)
    for rot in ("Producción del día", "Venta del día", "Margen",
                "Cobranzas del día"):
        assert rot not in h
    assert "200.538" in h          # la utilidad del mes queda siempre


def test_el_tramo_corto_se_avisa_tambien_en_el_mail():
    assert "no son 24 h" in _html(dia_parcial=True)


def test_el_finde_dice_del_finde_y_no_del_dia():
    from datetime import date
    res = {**_RES_OK, "dias": 2}
    with patch.object(dia, "resumen_finde", return_value=res), \
         patch.object(dia, "ventas_del_mes", return_value=_VM), \
         patch.object(dia, "cobranza_del_mes", return_value={"n": 0, "us": 0.0}):
        h = dia.nota_finde_html(date(2026, 8, 17))
    assert "Venta del finde" in h and "Utilidad del finde" in h
    assert "del día" not in h and "Cobranzas del mes" not in h


def test_cobranza_entre_cuenta_por_dia_de_ingreso_sin_espejos():
    """Mismo día de ingreso que /cheques/resumen-dia; el espejo NB=98 no es
    plata que entró y las anuladas tampoco."""
    from datetime import date
    with patch.object(dia, "_rows", return_value=[{"n": 3, "us": 1500.0}]) as rows:
        r = dia.cobranza_entre(date(2026, 9, 1), date(2026, 9, 3))
    sql = rows.call_args[0][0]
    assert r == {"n": 3, "us": 1500.0}
    assert "fecha_recibido" in sql and "<> 98" in sql and "('X', 'Y')" in sql
    assert rows.call_args[0][1] == (date(2026, 9, 1), date(2026, 9, 3))
    with patch.object(dia, "_rows", return_value=[]) as rows:
        assert dia.cobranza_del_mes(date(2026, 9, 3)) == {"n": 0, "us": 0.0}
    assert rows.call_args[0][1] == (date(2026, 9, 1), date(2026, 9, 3))


def test_sin_resumen_el_html_es_vacio_y_el_mail_sale_igual():
    """El texto plano es el que manda: si el HTML no se puede armar, no se
    manda HTML y listo."""
    from datetime import date
    with patch.object(dia, "resumen", return_value={"ok": False}):
        assert dia.nota_html(date(2026, 8, 12)) == ""


def test_ses_recibe_las_DOS_versiones():
    """Nunca HTML solo: sin alternativa de texto el mail puntúa peor en los
    filtros de spam, y este mail ya se comió ese problema."""
    class _Cli:
        def __init__(self): self.msg = None
        def send_email(self, **kw):
            self.msg = kw["Message"]
            return {"MessageId": "abc"}

    cli = _Cli()
    with patch.dict(os.environ, {"MAIL_ENVIAR": "1"}), \
         patch("boto3.client", return_value=cli):
        r = mailer.enviar("asunto", "texto plano", ["a@b.com"],
                          html="<div>hola</div>")
    assert r["ok"] and r["id"] == "abc"
    assert cli.msg["Body"]["Text"]["Data"] == "texto plano"
    assert cli.msg["Body"]["Html"]["Data"] == "<div>hola</div>"


def test_un_html_vacio_no_agrega_la_parte_html():
    class _Cli:
        def __init__(self): self.msg = None
        def send_email(self, **kw):
            self.msg = kw["Message"]
            return {"MessageId": "abc"}

    cli = _Cli()
    with patch.dict(os.environ, {"MAIL_ENVIAR": "1"}), \
         patch("boto3.client", return_value=cli):
        mailer.enviar("asunto", "texto", ["a@b.com"], html="   ")
    assert "Html" not in cli.msg["Body"]


# ── Lunes a viernes ─────────────────────────────────────────────────────────

def test_el_fin_de_semana_la_nota_no_sale(): 
    """TMT 2026-08-14: *"solo lun-viernes mandar mail diario"*. Ni se mira la
    base: el freno corta antes de tomar el sello, así que el lunes la captura
    del sábado sigue sin sello y nadie manda la nota de un día viejo."""
    for d in (_SABADO, date(2026, 8, 16)):  # sábado y domingo
        with patch.object(dia, "_rows") as rows, patch.object(mailer, "enviar") as env:
            r = dia.enviar_nota(d)
        assert r["ok"] is False and "lunes a viernes" in r["motivo"], d
        rows.assert_not_called()
        env.assert_not_called()


def test_el_boton_de_prueba_manda_igual_un_sabado():
    """El freno es para el envío AUTOMÁTICO. Si ella aprieta "Mandarla ahora",
    sale — es su decisión, y probar un sábado tiene que poder hacerse."""
    with patch.object(dia, "_rows", return_value=_cierre()), \
         patch.object(dia, "destinatarios",
                      return_value=[{"correo": "a@b.com", "activo": True}]), \
         patch.object(mailer, "habilitado", return_value=True), \
         patch.object(dia, "mensaje_whatsapp", return_value="hola"), \
         patch.object(dia, "nota_html", return_value="<p>hola</p>"), \
         patch.object(mailer, "enviar", return_value={"ok": True, "id": "m1", "enviados": 1,
                                     "fallidos": 0, "motivo": ""}) as env, \
         patch.object(dia.db, "execute", return_value=1):
        r = dia.enviar_nota(_SABADO, forzar=True)
    assert r["ok"] is True
    env.assert_called_once()


# ── El fin de semana, en un solo mail el lunes ──────────────────────────────
# TMT 2026-08-17: *"una sola, sáb+dom juntos"*. El 14/08 se apagó el mail del
# sábado y del domingo; sin esto la fábrica produce dos días que no cuenta
# nadie, porque la nota del lunes a la noche arranca del cierre del domingo.

_LUNES = date(2026, 8, 17)


def _manana(enviada=None):
    return [{"id_captura": 21, "nota_enviada_en": enviada}]


def test_la_nota_del_finde_sale_el_lunes_y_una_sola_vez():
    """Mismo sello en la BASE que la del cierre, pero en la captura de la
    MAÑANA del lunes: es la foto con la que sale."""
    with patch.object(dia, "_rows", return_value=_manana()) as rows, \
         patch.object(dia, "destinatarios",
                      return_value=[{"correo": "a@b.com", "activo": True}]), \
         patch.object(mailer, "habilitado", return_value=True), \
         patch.object(dia, "mensaje_finde", return_value="hola"), \
         patch.object(dia, "nota_finde_html", return_value="<p>hola</p>"), \
         patch.object(mailer, "enviar", return_value={"ok": True, "id": "m1", "enviados": 1,
                                     "fallidos": 0, "motivo": ""}) as env, \
         patch.object(dia.db, "execute", return_value=1) as ex:
        r = dia.enviar_nota_finde(_LUNES)
    assert r["ok"] is True and r["destinatarios"] == 1
    env.assert_called_once()
    assert "momento = 'manana'" in rows.call_args[0][0]
    assert "nota_enviada_en IS NULL" in ex.call_args_list[0][0][0]
    # El asunto lo dice: no es "el cierre del lunes".
    assert "finde 15–16 ago" in env.call_args[0][0]


def test_la_del_finde_no_sale_los_otros_dias():
    for d in (date(2026, 8, 18), date(2026, 8, 21), date(2026, 8, 16)):
        with patch.object(dia, "_rows") as rows, patch.object(mailer, "enviar") as env:
            r = dia.enviar_nota_finde(d)
        assert r["ok"] is False and "los lunes" in r["motivo"], d
        rows.assert_not_called()
        env.assert_not_called()


def test_si_la_del_finde_ya_se_mando_no_se_repite():
    with patch.object(dia, "_rows", return_value=_manana(enviada="2026-08-17")), \
         patch.object(mailer, "enviar") as env:
        r = dia.enviar_nota_finde(_LUNES)
    assert r["ok"] is False and "ya se mandó" in r["motivo"]
    env.assert_not_called()


def test_sin_cierre_del_viernes_no_se_sella_nada():
    """Un lunes sin tramo que contar no puede quemar el sello: si se sellara,
    el mail no saldría nunca aunque el cierre apareciera dos minutos después."""
    with patch.object(dia, "_rows", return_value=_manana()), \
         patch.object(dia, "mensaje_finde", return_value=""), \
         patch.object(dia.db, "execute") as ex, \
         patch.object(mailer, "enviar") as env:
        r = dia.enviar_nota_finde(_LUNES)
    assert r["ok"] is False and "comparar" in r["motivo"]
    ex.assert_not_called()
    env.assert_not_called()


def test_si_falla_el_envio_del_finde_se_libera():
    ejecutadas = []
    with patch.object(dia, "_rows", return_value=_manana()), \
         patch.object(dia, "destinatarios",
                      return_value=[{"correo": "a@b.com", "activo": True}]), \
         patch.object(mailer, "habilitado", return_value=True), \
         patch.object(dia, "mensaje_finde", return_value="hola"), \
         patch.object(dia, "nota_finde_html", return_value="<p>h</p>"), \
         patch.object(mailer, "enviar", return_value={"ok": False, "motivo": "red"}), \
         patch.object(dia.db, "execute",
                      side_effect=lambda sql, *a, **k: ejecutadas.append(sql) or 1):
        r = dia.enviar_nota_finde(_LUNES)
    assert r["ok"] is False
    assert "nota_enviada_en = NULL" in ejecutadas[-1]


def test_el_boton_de_prueba_del_finde_anda_cualquier_dia():
    """La vista previa y el botón tienen que funcionar un miércoles: si sólo se
    pudieran probar un lunes a la mañana, se revisarían el día que sale mal."""
    with patch.object(dia, "hoy_ec", return_value=date(2026, 8, 19)), \
         patch.object(dia, "_rows", return_value=_manana()), \
         patch.object(dia, "destinatarios",
                      return_value=[{"correo": "a@b.com", "activo": True}]), \
         patch.object(mailer, "habilitado", return_value=True), \
         patch.object(dia, "mensaje_finde", return_value="hola"), \
         patch.object(dia, "nota_finde_html", return_value="<p>h</p>"), \
         patch.object(mailer, "enviar", return_value={"ok": True, "id": "m1", "enviados": 1,
                                     "fallidos": 0, "motivo": ""}) as env, \
         patch.object(dia.db, "execute", return_value=1):
        r = dia.enviar_nota_finde(forzar=True)
    assert r["ok"] is True
    # Un miércoles, el finde que cuenta es el de ESA semana (15 y 16).
    assert "finde 15–16 ago" in env.call_args[0][0]


def test_el_lunes_del_finde_es_el_de_la_semana_y_el_domingo_no_cuenta_el_suyo():
    assert dia.lunes_del_finde(date(2026, 8, 17)) == date(2026, 8, 17)   # lunes
    assert dia.lunes_del_finde(date(2026, 8, 19)) == date(2026, 8, 17)   # miércoles
    # Un domingo, su propio fin de semana todavía no cerró: cuenta el anterior.
    assert dia.lunes_del_finde(date(2026, 8, 16)) == date(2026, 8, 10)


def test_el_rotulo_del_finde_a_caballo_de_dos_meses():
    """Un finde puede empezar en un mes y terminar en otro: "31–1 oct" sería
    mentira. Lunes 02/11/2026 = sábado 31/10 + domingo 01/11."""
    corto, largo = dia.rotulo_finde(date(2026, 11, 2))
    assert corto == "finde 31 oct–1 nov"
    assert largo == "sábado 31 de octubre y domingo 1 de noviembre"

    corto, largo = dia.rotulo_finde(date(2026, 8, 31))  # lunes 31/08
    assert corto == "finde 29–30 ago"
    assert largo == "sábado 29 y domingo 30 de agosto"


# ── Lo facturado va en BRUTO ────────────────────────────────────────────────
# TMT 2026-08-17: *"el mail llega lo facturado menos devoluciones, podemos
# mandar solo facturado"*.

def _res(devol_n=0, devol_us=0.0):
    return {"ok": True, "fecha": date(2026, 8, 17), "dia_parcial": False,
            "hasta": {"utilidad": 300000.0}, "d_utilidad": 1000.0,
            "ventas": {"n": 113, "kg": 14781.0, "us": 127267.0},
            "devoluciones": {"n": devol_n, "kg": 0.0, "us": devol_us},
            "produccion": {"disponible": False}, "cobrado": 5000.0,
            "cobranza": {"n": 0, "us": 0.0}, "margen_pct": 40.4}


def test_la_devolucion_tiene_su_renglon_y_no_se_come_la_venta():
    with patch.object(dia, "resumen", return_value=_res(6, 3772.0)), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 0, "kg": 0.0, "us": 0.0}), \
         patch.object(dia, "ventanas_sin_cerrar", return_value={"n": 0, "monto": 0.0}), \
         patch.object(dia, "motores_del_dia", return_value=[]):
        html = dia.nota_html(date(2026, 8, 17))
        txt = dia.mensaje_whatsapp(date(2026, 8, 17))
    # La venta se dice entera, no neteada.
    assert "127.267" in html and "127.267" in txt
    for cuerpo in (html, txt):
        assert "Devoluciones" in cuerpo
        assert "3.772" in cuerpo


def test_sin_devoluciones_el_renglon_no_aparece():
    """Un renglón que sale todos los días entrena a no leerlo."""
    with patch.object(dia, "resumen", return_value=_res()), \
         patch.object(dia, "ventas_del_mes",
                      return_value={"n": 0, "kg": 0.0, "us": 0.0}), \
         patch.object(dia, "ventanas_sin_cerrar", return_value={"n": 0, "monto": 0.0}), \
         patch.object(dia, "motores_del_dia", return_value=[]):
        html = dia.nota_html(date(2026, 8, 17))
        txt = dia.mensaje_whatsapp(date(2026, 8, 17))
    assert "Devoluciones" not in html and "Devoluciones" not in txt


def test_el_mes_del_pie_tambien_va_en_bruto():
    """Si el día va en bruto y el mes en neto, el pie contradice al renglón de
    arriba."""
    with patch.object(dia, "_rows", return_value=[{"n": 9, "kg": 1.0, "us": 2.0}]) as rows:
        dia.ventas_del_mes(date(2026, 8, 17))
    assert "kg > 0" in rows.call_args[0][0]


# ── 🎉 El día de 20.000 kg ──────────────────────────────────────────────────
# TMT 2026-08-25: *"poné emojis de fiestita por haber vendido más de 20k kilos
# (y siempre que eso pase) mencionalo"*.

def test_el_festejo_sale_recien_al_llegar_a_los_20000_kg():
    assert dia.fiesta_kilos({"ventas": {"kg": 19999.0}}) == 0.0
    assert dia.fiesta_kilos({"ventas": {"kg": 20000.0}}) == 20000.0
    assert dia.fiesta_kilos({"ventas": {"kg": 24312.0}}) == 24312.0


def test_el_finde_no_festeja_dos_dias_sumados():
    """Sábado y domingo juntos no son el DÍA de 20.000 kg."""
    assert dia.fiesta_kilos({"dias": 2, "ventas": {"kg": 31000.0}}) == 0.0


def test_sin_ventas_no_hay_nada_que_festejar():
    assert dia.fiesta_kilos({}) == 0.0
    assert dia.fiesta_kilos({"ventas": {"n": 0, "kg": 0}}) == 0.0


def test_el_mail_festeja_el_dia_de_20000_kg():
    h = _html(ventas={"n": 141, "kg": 24312.0, "us": 198420.0})
    assert "🎉" in h and "🥳" in h
    assert "20.000 kg" in h                 # el tope, dicho
    assert "24.312 kg</b>" in h             # y lo que se vendió de verdad


def test_un_dia_normal_no_festeja_nada():
    """Un festejo que sale todos los días deja de ser un festejo."""
    h = _html()                              # 15.809 kg
    assert "🎉" not in h and "🥳" not in h
