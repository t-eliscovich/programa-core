"""De dónde sale el mail del cliente y quién le gana a quién.

TMT 2026-08-03. La dueña pidió ver el mail en el estado de cuenta y preguntó
si se podían **adivinar** los que están cortados. La respuesta fue que casi
nunca hace falta: Asinfo le manda la factura electrónica al cliente, así que
tiene el mail real y completo de ~3.500. Sus dos decisiones, que son las que
clavan estos tests:

  - **"solo los vacíos"** → Asinfo RELLENA, no pisa. Lo que alguien cargó a
    mano en la ficha gana siempre.
  - **"muestro los dos"** → cuando Asinfo tiene un contacto distinto, se
    muestran los dos y elige la persona.

Son tests de una función PURA: no tocan Postgres ni Metabase (en CI no hay
ninguno de los dos).
"""
from __future__ import annotations

from modules.clientes import mail_asinfo as ma

FICHA = "cargado.a.mano@empresa.com"
OBS_OK = "ventas@americanspirit.ec"
ASINFO = "contabilidad@americanspirit.ec"


# ---------------------------------------------------------------------------
# Leer el mail de la Observación (y saber si quedó cortado)
# ---------------------------------------------------------------------------


def test_lee_el_mail_aunque_haya_notas_pegadas():
    """Se tipeó durante años junto con la forma de pago y el vendedor."""
    for obs, esperado in [
        ("ventas@americanspirit.ec MBT", "ventas@americanspirit.ec"),
        ("isabel1981@yahoo.es    CONTADO", "isabel1981@yahoo.es"),
        ("compras@almogascialtda.com  60", "compras@almogascialtda.com"),
    ]:
        assert ma.mail_de_observacion(obs)[0] == esperado, obs


def test_un_mail_que_termina_justo_en_30_esta_cortado():
    """El campo OBS del dBase es CHAR(30). Éste es el caso peligroso: parsea
    como un mail perfectamente válido y NO lo es."""
    obs = "contabilidadnortextil@gmail.co"          # 30 caracteres exactos
    assert len(obs) == ma.LARGO_OBSERVACION
    mail, cortado = ma.mail_de_observacion(obs)
    assert mail == obs and cortado is True, (
        "un mail que llega al tope del campo tiene que marcarse como cortado; "
        "si no, se muestra 'gmail.co' como si fuera bueno"
    )


def test_si_despues_del_mail_hay_texto_entonces_NO_esta_cortado():
    """`isabel1981@yahoo.es    CONTADO` llega a 30 pero el mail terminó antes:
    lo que sigue prueba que no lo cortaron."""
    obs = "isabel1981@yahoo.es    CONTADO"
    assert len(obs) == ma.LARGO_OBSERVACION
    assert ma.mail_de_observacion(obs)[1] is False


# ---------------------------------------------------------------------------
# Los mails de la propia Intela — la trampa de Asinfo
# ---------------------------------------------------------------------------


def test_los_mails_de_intela_no_son_del_cliente():
    """811 clientes tienen cargado un mail de Intela en Asinfo: es el relleno
    de facturación electrónica para el que no dio mail. Tomarlo por bueno
    haría que el estado de cuenta se le mande a Intela misma."""
    for m in ("facturaelectronica@intela.com.ec", "contabilidad@intela.com.ec",
              "hbintela@hotmail.com", "HBINTELA@HOTMAIL.COM"):
        assert ma.es_mail_de_la_casa(m), m


def test_no_se_filtra_por_parecido():
    """Comparación exacta / por dominio: un cliente que se llame parecido, o
    que tenga 'intela' en su propio dominio, NO puede quedar afuera."""
    for m in ("ventas@intelanet.com.ec", "intela@gmail.com",
              "compras@textilintela.com", ""):
        assert not ma.es_mail_de_la_casa(m), m


def test_asinfo_con_mail_de_la_casa_es_como_no_tener_nada():
    r = ma.resolver(None, None, "facturaelectronica@intela.com.ec")
    assert r["mail"] == "" and r["alternativo"] == ""


# ---------------------------------------------------------------------------
# La prioridad — "solo los vacíos"
# ---------------------------------------------------------------------------


def test_la_ficha_le_gana_a_todo():
    r = ma.resolver(FICHA, f"{OBS_OK} CONTADO", ASINFO)
    assert r["mail"] == FICHA and r["origen"] == "ficha"


def test_asinfo_no_pisa_la_observacion_completa():
    """"Solo los vacíos": si la Observación tiene un mail entero, ése manda."""
    r = ma.resolver(None, f"{OBS_OK} MBT", ASINFO)
    assert r["mail"] == OBS_OK and r["origen"] == "observacion"


def test_asinfo_rellena_cuando_no_hay_nada():
    r = ma.resolver(None, "CONTADO 60 DIAS", ASINFO)
    assert r["mail"] == ASINFO and r["origen"] == "asinfo"


def test_sin_ninguna_fuente_no_inventa():
    r = ma.resolver(None, "CONTADO", None)
    assert r["mail"] == "" and r["alternativo"] == "" and r["incompleto"] == ""


# ---------------------------------------------------------------------------
# "Muestro los dos"
# ---------------------------------------------------------------------------


def test_si_asinfo_tiene_otro_contacto_se_muestran_los_dos():
    r = ma.resolver(None, f"{OBS_OK} MBT", ASINFO)
    assert r["mail"] == OBS_OK
    assert r["alternativo"] == ASINFO and r["origen_alt"] == "asinfo"


def test_si_es_el_mismo_mail_no_se_repite():
    r = ma.resolver(None, f"{OBS_OK} MBT", OBS_OK.upper())
    assert r["alternativo"] == "", "no mostrar dos veces el mismo mail"


# ---------------------------------------------------------------------------
# El caso que motivó todo: el mail cortado
# ---------------------------------------------------------------------------


def test_asinfo_completa_el_cortado_y_eso_no_es_adivinar():
    """98 de los 225 cortados caen acá: el mail de Asinfo ARRANCA con el
    fragmento que quedó, o sea es literalmente el mismo mail completo."""
    r = ma.resolver(None, "contabilidadnortextil@gmail.co",
                    "contabilidadnortextil@gmail.com")
    assert r["mail"] == "contabilidadnortextil@gmail.com"
    assert r["origen"] == "asinfo_completa"
    assert r["incompleto"] == "", (
        "si es el mismo mail completo no hay nada que advertir"
    )


def test_un_fragmento_cortado_nunca_se_muestra_como_mail_valido():
    """Sin Asinfo, `...@gmail.co` NO puede ir en el mailto: mandaría el
    estado de cuenta a un dominio que no es."""
    r = ma.resolver(None, "contabilidadnortextil@gmail.co", None)
    assert r["mail"] == "", "un fragmento no es un mail"
    assert r["incompleto"] == "contabilidadnortextil@gmail.co", (
        "pero sí se muestra aparte, marcado, para que se vea que hay algo a "
        "medio cargar y se pueda completar"
    )


def test_tambien_se_reconoce_el_fragmento_que_ni_parece_mail():
    """Los cortes más brutales se comieron el dominio entero y NO parsean como
    mail. Eran 110 de los 225: si no se reconocen, desaparecen en silencio."""
    for obs in ("contabilidad@textilesalvarez.c", "fabricadecamisetaslex@hotmail.",
                "9sociacionartesanosbuenvestir@"):
        assert len(obs) == ma.LARGO_OBSERVACION, obs
        frag, cortado = ma.mail_de_observacion(obs)
        assert cortado is True and frag, obs


def test_cortado_con_otro_mail_en_asinfo_avisa_de_los_dos():
    r = ma.resolver(None, "contabilidad@textilesalvarez.c",
                    "ventas@textilesalvarez.com")
    assert r["mail"] == "ventas@textilesalvarez.com"
    assert r["incompleto"] == "contabilidad@textilesalvarez.c", (
        "Asinfo trae OTRO contacto, no el completado: el fragmento tiene que "
        "quedar a la vista"
    )


# ---------------------------------------------------------------------------
# Cruce PC ↔ Asinfo
# ---------------------------------------------------------------------------


def test_el_ruc_se_cruza_por_los_primeros_10_digitos():
    """PC guarda a veces la cédula pelada y Asinfo el RUC completo."""
    assert ma.ruc10("1752968204") == "1752968204"
    assert ma.ruc10("1752968204001") == "1752968204"
    assert ma.ruc10(" 175-296-8204 ") == "1752968204"


def test_un_ruc_corto_no_cruza_nada():
    """Devolver un prefijo de 6 dígitos aparearía clientes al azar."""
    for malo in ("", None, "12345", "abc"):
        assert ma.ruc10(malo) == "", malo


# ---------------------------------------------------------------------------
# Ingesta
# ---------------------------------------------------------------------------


def test_el_refresco_no_borra_la_foto_buena_si_metabase_se_cayo(monkeypatch):
    """Vaciar el espejo porque el puente falló dejaría a TODOS los clientes
    sin mail de golpe. Fail-soft de verdad: no escribe nada."""
    from modules._lib import metabase_client as mc

    escrituras = []
    monkeypatch.setattr(ma._db, "execute", lambda *a, **k: escrituras.append(a))
    monkeypatch.setattr(mc, "disponible", lambda: True)
    monkeypatch.setattr(mc, "fetch_dataset_estado", lambda *a, **k: ([], False))
    ma._bootstrapped = False
    r = ma.refrescar()
    assert r["ok"] is False and r["filas"] == 0
    # sólo el CREATE TABLE IF NOT EXISTS del bootstrap, ningún INSERT
    assert not [e for e in escrituras if "INSERT" in str(e)]


def test_el_refresco_descarta_los_mails_de_la_casa(monkeypatch):
    from modules._lib import metabase_client as mc

    inserts = []
    def _exec(sql, params=None):
        if "INSERT" in sql:
            inserts.append(params)
    monkeypatch.setattr(ma._db, "execute", _exec)
    monkeypatch.setattr(mc, "disponible", lambda: True)
    monkeypatch.setattr(mc, "fetch_dataset_estado", lambda *a, **k: ([
        {"ruc10": "1752968204001", "email": "cliente@gmail.com", "nombre_fiscal": "X"},
        {"ruc10": "0100091255001", "email": "facturaelectronica@intela.com.ec", "nombre_fiscal": "Y"},
        {"ruc10": "0909718603001", "email": "no-es-un-mail", "nombre_fiscal": "Z"},
        {"ruc10": "123", "email": "otro@gmail.com", "nombre_fiscal": "W"},
    ], True))
    ma._bootstrapped = False
    r = ma.refrescar()
    assert r["filas"] == 1 and r["descartadas_casa"] == 1
    assert inserts[0][0] == "1752968204", "la clave se guarda normalizada a 10"


def test_el_cron_nunca_levanta(monkeypatch):
    monkeypatch.setattr(ma, "refrescar", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    r = ma.refrescar_cron()
    assert r["ok"] is False and "boom" in r["error"]


# ---------------------------------------------------------------------------
# El 502 del 03/08 — un INSERT por fila dentro del request del cron
# ---------------------------------------------------------------------------


def test_el_refresco_hace_UN_solo_insert(monkeypatch):
    """La primera versión insertaba fila por fila: 3.500 viajes a RDS adentro
    del request de /admin/health/all → 502 por timeout del proxy, y la tabla
    quedó en 0 filas."""
    from modules._lib import metabase_client as mc

    sqls = []
    monkeypatch.setattr(ma._db, "execute", lambda sql, params=None: sqls.append(sql))
    monkeypatch.setattr(mc, "disponible", lambda: True)
    monkeypatch.setattr(mc, "fetch_dataset_estado", lambda *a, **k: ([
        {"ruc10": f"1{str(i).zfill(9)}001", "email": f"c{i}@gmail.com",
         "nombre_fiscal": f"CLI {i}"} for i in range(500)
    ], True))
    ma._bootstrapped = False
    r = ma.refrescar()
    assert r["filas"] == 500
    assert len([s for s in sqls if "INSERT" in s]) == 1, (
        f"un INSERT por fila otra vez: {len([s for s in sqls if 'INSERT' in s])}"
    )


def test_no_manda_el_mismo_ruc_dos_veces_en_el_insert(monkeypatch):
    """Postgres tira "ON CONFLICT cannot affect row a second time" si la MISMA
    clave viene repetida dentro del mismo INSERT. Pasa cuando Asinfo tiene la
    cédula y el RUC del mismo titular."""
    from modules._lib import metabase_client as mc

    params_vistos = []
    monkeypatch.setattr(
        ma._db, "execute",
        lambda sql, params=None: params_vistos.append(params) if "INSERT" in sql else None)
    monkeypatch.setattr(mc, "disponible", lambda: True)
    monkeypatch.setattr(mc, "fetch_dataset_estado", lambda *a, **k: ([
        {"ruc10": "1752968204", "email": "uno@gmail.com", "nombre_fiscal": "A"},
        {"ruc10": "1752968204001", "email": "dos@gmail.com", "nombre_fiscal": "A"},
    ], True))
    ma._bootstrapped = False
    r = ma.refrescar()
    assert r["filas"] == 1
    assert params_vistos[0].count("1752968204") == 1


def test_el_cron_no_repite_el_trabajo_si_ya_esta_fresco(monkeypatch):
    """/admin/health/all lo abre cualquiera desde el panel: sin este guard,
    cada visita dispara la consulta pesada a Asinfo."""
    monkeypatch.setattr(ma, "esta_fresco", lambda: True)
    monkeypatch.setattr(ma, "refrescar", lambda: (_ for _ in ()).throw(
        AssertionError("no tendría que haber refrescado")))
    r = ma.refrescar_cron()
    assert r["ok"] is True and "salteado" in r


# ---------------------------------------------------------------------------
# Deducir el final cortado — el pedido textual de la dueña
# ---------------------------------------------------------------------------


def test_deduce_el_ejemplo_que_dio_la_dueña():
    """"ejemplo este sabemos que es fabricadecamisetaslex@hotmail.com"."""
    assert ma.deducir("fabricadecamisetaslex@hotmail.") == \
        "fabricadecamisetaslex@hotmail.com"


def test_deduce_los_cortes_tipicos_de_hotmail_y_gmail():
    for frag, esperado in [
        ("contabilidadnortextil@gmail.co", "contabilidadnortextil@gmail.com"),
        ("guadalupeaguilar1711@hotmail.c", "guadalupeaguilar1711@hotmail.com"),
        ("andugatitavillafuerte@hotmailc", "andugatitavillafuerte@hotmail.com"),
        ("washington_martinez1971@hm.com", "washington_martinez1971@hotmail.com"),
        ("lauramartinezparedes19@hotma.c", "lauramartinezparedes19@hotmail.com"),
    ]:
        assert ma.deducir(frag) == esperado, frag


def test_NO_deduce_yahoo():
    """139 yahoo.com contra 85 yahoo.es en esta cartera: la moneda sale cara
    y cruz. Un mail mal deducido le manda el estado de cuenta a un
    desconocido — mejor el guión."""
    assert ma.deducir("confecciones patricia67@yah.es") == ""
    assert ma.deducir("mayraespincriollo@yahoo.") == ""


def test_NO_deduce_cuando_el_corte_se_comio_el_dominio_propio():
    """`...@cmdpublicidadte` puede terminar en cualquier cosa."""
    for frag in ("administrativo@cmdpublicidadte", "9sociacionartesanosbuenvestir@",
                 "contabilidad@textilesalvarez.c", ""):
        assert ma.deducir(frag) == "", frag


def test_la_deduccion_va_como_sugerencia_no_como_mail():
    """No puede entrar al mailto: nadie tiene que escribirle a un mail
    deducido sin confirmarlo."""
    r = ma.resolver(None, "fabricadecamisetaslex@hotmail.", None)
    assert r["mail"] == "", "una deducción no es el mail del cliente"
    assert r["sugerido"] == "fabricadecamisetaslex@hotmail.com"
    assert r["incompleto"] == "fabricadecamisetaslex@hotmail."


def test_si_asinfo_lo_tiene_no_se_deduce_nada():
    """El dato real le gana siempre a la deducción."""
    r = ma.resolver(None, "contabilidadnortextil@gmail.co",
                    "contabilidadnortextil@gmail.com")
    assert r["mail"] == "contabilidadnortextil@gmail.com"
    assert not r["sugerido"]
