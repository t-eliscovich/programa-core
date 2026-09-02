"""Auto-carga de facturas+retenciones del día — hilo de fondo (sin abrir nada).

Dueña 2026-07-23: "quiero no tener que ir a ninguna página". La auto-carga del
día (`facturas.views._auto_cargar_facturas_hoy`) ya corría al ABRIR /facturas y
al ENTRAR al programa (/operaciones), pero ambas dependen de que alguien abra una
pantalla. Este hilo daemon la corre SOLO, en el servidor, cada _INTERVALO_SECS,
así las facturas y retenciones de HOY entran solas aunque nadie tenga el programa
abierto.

Reusa la MISMA función (idempotente, fail-soft, dedup por N° SRI, lock + throttle
interno de 60s), así que:
  · nunca duplica (mismo guard que la carga por pantalla);
  · no encima corridas con las que dispara un usuario al entrar;
  · corre SIN contexto de request — verificado: el camino de creación
    (_resolver_cliente_asinfo → queries.crear → aplicar_retenciones_asinfo) no
    usa flask g/request/session (igual que el warmup lee Asinfo desde un hilo).

Fail-soft total: cualquier excepción se loguea y el hilo sigue.
Apagable con AUTOCARGA_FACTURAS=0. No corre bajo pytest. Intervalo configurable
con AUTOCARGA_FACTURAS_SECS (default 120s).
"""
from __future__ import annotations

import logging
import os
import threading
import time

_LOG = logging.getLogger(__name__)

_started = False


def _intervalo_secs() -> int:
    try:
        v = int(os.environ.get("AUTOCARGA_FACTURAS_SECS", "120"))
        return v if v >= 60 else 60  # el throttle interno es 60s: no bajar de ahí
    except (TypeError, ValueError):
        return 120


def _loop_facturas() -> None:
    """El hilo RÁPIDO: sólo las facturas del día. Nada más.

    TMT 2026-08-24 (dueña): *"fijate si el resto se puede acelerar"*, mirando
    la pantalla del día con varias facturas en ámbar a la vez. La carga vivía
    en el mismo ciclo que el sync de clientes, el de colores, los químicos, la
    tejeduría, la traza y la foto del día: el `sleep` era de 120 s, pero con
    todo eso en fila el ciclo REAL medido ese día daba ~6 minutos, y la demora
    entre "Asinfo la emitió" y "está en el programa" tenía mediana de 8,8 min,
    9 de cada 10 dentro de 14 min y la peor en 29,5 min.

    Ahora las facturas corren solas en su propio hilo, con su propio reloj, sin
    esperar a nadie. El resto del ciclo queda igual, en `_loop`.
    """
    time.sleep(8)  # dejar terminar el arranque de la app
    intervalo = _intervalo_secs()
    while True:
        try:
            # Import perezoso: facturas.views ya está cargado (blueprint
            # registrado), evita cualquier ciclo en tiempo de import.
            from modules.facturas.views import _auto_cargar_facturas_hoy

            res = _auto_cargar_facturas_hoy()
            if res.get("cargadas") or res.get("ret") or res.get("anuladas"):
                _LOG.info(
                    "auto-carga facturas (fondo): %s facturas, %s retenciones, "
                    "%s dadas de baja por anulación en Asinfo",
                    res.get("cargadas", 0), res.get("ret", 0),
                    res.get("anuladas", 0),
                )
        except Exception as e:  # noqa: BLE001 -- el hilo no muere nunca
            _LOG.warning("auto-carga facturas (fondo) ciclo: %s", e)
        time.sleep(intervalo)


def _loop() -> None:
    time.sleep(10)  # dejar terminar el arranque de la app (y que el warmup arranque)
    intervalo = _intervalo_secs()
    while True:
        try:
            # TMT 2026-07-29 (dueña): conversión automática anticipo → compra
            # BAP cuando Asinfo marca recibida la importación. Se cuelga de
            # ESTE ciclo (no se agrega un hilo más) y trae su propio freno de
            # 30 min + switch en base + AUTOBAP=0. Fail-soft por su cuenta.
            try:
                from modules.importaciones import autobap as _autobap
                ab = _autobap.correr_si_toca()
                if ab.get("convertidas"):
                    _LOG.info(
                        "autobap (fondo): %s importación(es) convertidas por $%s",
                        ab.get("convertidas"), ab.get("importe"),
                    )
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("autobap (fondo): %s", e)
            # TMT 2026-07-30 (dueña): "tejeduría tiene que correr sola". Antes
            # la carga de compras de tejeduría se disparaba SÓLO cuando alguien
            # abría /produccion-tejeduria-asinfo — si nadie entraba, no cargaba
            # nada. Se cuelga de este ciclo con su propio freno de 30 min
            # (TEJEDURIA_AUTO=0 la apaga) y cuenta lo que cargó en la campanita.
            try:
                from modules.tejeduria_asinfo import service as _tej
                tj = _tej.correr_si_toca()
                if tj.get("creadas"):
                    _LOG.info(
                        "tejeduría (fondo): %s compra(s) por $%s",
                        tj.get("creadas"), tj.get("importe"),
                    )
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("tejeduría (fondo): %s", e)
            # TMT 2026-08-05 (dueña): el sync del maestro de CLIENTES con
            # Asinfo corre solo cada hora de 07:00 a 19:00 EC — sin cron del EC2,
            # igual que todo lo demás de este ciclo. El guard de ventana vive
            # en el log del sync (una corrida manual dentro de la ventana
            # también cuenta). SYNC_CLIENTES_AUTO=0 lo apaga.
            try:
                from modules.clientes import sync_asinfo as _sync_cli
                sc = _sync_cli.correr_si_toca()
                if sc.get("corrio"):
                    rep = sc.get("reporte") or {}
                    _LOG.info(
                        "sync clientes (fondo): %s actualizados, altas: %s, "
                        "%s conflictos",
                        rep.get("actualizados", 0),
                        ", ".join(rep.get("altas") or []) or "ninguna",
                        len(rep.get("conflictos") or []),
                    )
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("sync clientes (fondo): %s", e)
            # TMT 2026-08-17 (Tamara: "hay colores que siguen sin mostrarse
            # en facturas proforma… asinfo tambien tiene la categoria"). El
            # catálogo de COLORES y su clase de precio se sincronizan con el
            # atributo Color de Asinfo en las MISMAS ventanas que los clientes
            # (11:00 y 16:00 EC). SYNC_COLORES_AUTO=0 lo apaga.
            try:
                from modules.precios import colores_asinfo as _sync_col
                sk = _sync_col.correr_si_toca()
                if sk.get("corrio"):
                    rep = sk.get("reporte") or {}
                    _LOG.info(
                        "sync colores (fondo): %s altas, %s con clase nueva, "
                        "%s reclasificados",
                        len(rep.get("altas") or []),
                        len(rep.get("con_clase") or []),
                        len(rep.get("reclasificados") or []),
                    )
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("sync colores (fondo): %s", e)
            # TMT 2026-09-02 (Tamara: "si cambian los precios en asinfo
            # tenemos que cambiarlos en programa core"). Una vez por hora se
            # mira el NÚMERO de versión de la lista de precios de Asinfo; si
            # cambió, campanita para quien edita precios. No pisa nada solo.
            # PRECIOS_ASINFO_AUTO=0 lo apaga.
            try:
                from modules.precios import asinfo_lista as _precios_asinfo
                pa = _precios_asinfo.correr_si_toca()
                if pa.get("corrio") and (pa.get("reporte") or {}).get("cambio"):
                    _LOG.info("lista de precios de Asinfo: versión nueva %s",
                              (pa.get("reporte") or {}).get("version"))
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("versión precios Asinfo (fondo): %s", e)
            # TMT 2026-07-30 (dueña): las COMPRAS LOCALES de hilo (HY, EP) se
            # cargan solas cuando Asinfo marca la recepción — no tienen anticipo,
            # así que el pasivo nace al recibir. Mismo patrón que tejeduría: kg de
            # Asinfo × tarifa del tarifario, freno propio de 30 min,
            # HILO_LOCAL_AUTO=0 lo apaga. Fail-soft por su cuenta.
            try:
                from modules.compras_locales import service as _hloc
                hl = _hloc.correr_si_toca()
                if hl.get("creadas"):
                    _LOG.info(
                        "hilo local (fondo): %s compra(s) por $%s",
                        hl.get("creadas"), hl.get("importe"),
                    )
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("hilo local (fondo): %s", e)
            # TMT 2026-07-30 (dueña): las compras de QUÍMICOS de formulas ya
            # no esperan al día siguiente — se cargan el mismo día y, si la
            # factura sigue creciendo mientras la tipean, esta corrida le
            # corrige el importe (y el posdat). Freno propio de 30 min;
            # FORMULAS_COMPRAS_AUTOSYNC=0 lo apaga.
            try:
                from modules.compras import formulas_bridge as _fbr
                fq = _fbr.correr_si_toca()
                if fq.get("creadas") or fq.get("ajustadas"):
                    _LOG.info(
                        "químicos formulas (fondo): %s compra(s) por $%s, "
                        "%s corregida(s)",
                        fq.get("creadas"), fq.get("importe"), fq.get("ajustadas"),
                    )
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("químicos formulas (fondo): %s", e)
            # TMT 2026-07-31 (dueña): "si no cargaron después de 30 días, pero
            # nada de rojo en resultados — en la campanita". Importaciones que
            # llegaron y quedaron sin toda su plata cargada. El umbral sale de
            # la medición: 34 de 35 tienen la plata completa antes de los 21
            # días, así que a los 30 no queda maduración normal. Freno propio de
            # 6 h, un aviso por grupo (clave idempotente), IMPORT_SIN_PLATA=0 lo
            # apaga. Fail-soft por su cuenta.
            try:
                from modules.importaciones import vigilancia as _vig
                vg = _vig.revisar_si_toca()
                if vg.get("avisados"):
                    _LOG.info(
                        "importaciones sin plata (fondo): %s aviso(s) de %s caso(s)",
                        vg.get("avisados"), vg.get("casos"),
                    )
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("importaciones sin plata (fondo): %s", e)
            # TMT 2026-07-30 (dueña): "agregar en la campanita, a fin de día,
            # venta total kg y total facturas $" — a las 18:00 de ECUADOR, uno
            # solo por día (la clave del aviso lo garantiza).
            try:
                from modules.facturas import aviso_ventas as _ventas
                # TMT 2026-08-13 (dueña): los avisos de umbral (5/10/15/20
                # mil kg) se RETIRARON — *"si está pinned, ya no necesitamos
                # anuncio por cada 5k, 10k, 15k y 20k goal"*: el número del día
                # está fijo arriba de la campanita. Queda sólo el cierre.
                _ventas.correr_si_toca()
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("ventas del día (fondo): %s", e)
            # TMT 2026-07-31 (dueña): "guardá la data y fijate en un rato
            # también". La grabadora: una foto del balance con TODOS sus
            # componentes cada 5 minutos, para que un salto de las 09:16 a las
            # 10:34 deje rastro y se pueda mirar QUÉ se movió en vez de
            # discutir hipótesis. Va última a propósito: es la que menos
            # importa del ciclo y no tiene por qué demorar a las otras.
            # TMT 2026-08-11 (dueña): *"al día siguiente si no se cargó"*. Un
            # despacho sin factura a media tarde es la tarde normal — el 93 %
            # se factura dentro de la hora — y avisarlo ahí quedó feo: *"no me
            # gustó el aviso, parecía que algo estaba mal cuando no era el
            # caso"*. Lo del día en curso se cuenta como CONTEXTO en el
            # renglón de /informes/dia; acá sólo va lo que amaneció sin
            # cargar, que sí es un problema. Un aviso por despacho.
            try:
                from modules.asinfo import despacho_sin_factura as _dsf
                ds = _dsf.revisar_si_toca()
                if ds.get("avisados"):
                    _LOG.info("despachos sin factura (fondo): %s aviso(s)",
                              ds.get("avisados"))
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("despachos sin factura (fondo): %s", e)
            # TMT 2026-08-11 (dueña): el hilo que sale de bodega SIN orden de
            # fabricación no entra a "en proceso" y se cae del balance — el
            # 11/08 fueron 8.100 kg a Ponce y $ 24.327 de utilidad. Va a la
            # campanita para enterarse por ahí y no por la utilidad. Freno
            # propio de 15 min, un aviso por despacho, HILO_SIN_OF=0 lo apaga.
            try:
                from modules.asinfo import hilo_sin_of as _hsof
                hs = _hsof.revisar_si_toca()
                if hs.get("avisados"):
                    _LOG.info("hilo sin orden de fabricación (fondo): %s aviso(s)",
                              hs.get("avisados"))
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("hilo sin orden de fabricación (fondo): %s", e)
            try:
                from modules.informes import traza as _traza
                _traza.registrar_si_toca()
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("traza de la utilidad (fondo): %s", e)
            # TMT 2026-08-04 (dueña): "quiero agregar un check diario... comparás
            # balance de mañana y a fin de día". Las dos capturas ancla (07:00 y
            # 19:00 de ECUADOR) con la foto de detalle documento por documento.
            # La grabadora de arriba no alcanza para esto: guarda agregados, y
            # además el sync del dBase borra POSDAT/COMPRA/DOLARES/ACTIVOS/
            # RETIROS enteras, así que "qué se cargó hoy" sólo se puede
            # contestar diffeando una foto propia. Idempotente por el índice
            # único (fecha_ec, momento) — no por una variable de proceso, que
            # se pierde en cada restart.
            try:
                from modules.informes import dia as _dia
                _dia.correr_si_toca()
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("explicación del día (fondo): %s", e)
            # TMT 2026-08-20 (dueña): la foto de SALDOS (y con ella la
            # competencia entera: kilos vendidos, ranking, premio del mes y el
            # congelamiento de la meta del día de la largada) sólo se movía si
            # alguien entraba a /analisis/parado y apretaba «Actualizar». Cinco
            # días antes de la largada la foto era del 18/08 19:42. Freno
            # propio de 3 h contra `parado_refresh.actualizado`, ANALISIS_AUTO=0
            # lo apaga.
            try:
                from modules.analisis import auto_refresco as _saldos
                _saldos.correr_si_toca()
            except Exception as e:  # noqa: BLE001 -- nunca frena el ciclo
                _LOG.warning("saldos (fondo): %s", e)
        except Exception as e:  # noqa: BLE001 -- el hilo no muere nunca
            _LOG.warning("auto-carga facturas (fondo) ciclo: %s", e)
        time.sleep(intervalo)


def hilo_de_facturas_vivo() -> bool:
    """¿Está corriendo el hilo que carga las facturas del día?

    TMT 2026-08-24 (dueña): *"cargar la página facturas es un poco lento"*.
    Abrir /facturas disparaba la carga EN LA REQUEST: la pantalla esperaba a
    Asinfo (medido: ~1 s de más cada vez que le tocaba correr). Con el hilo
    andando eso no hace falta — las facturas ya entran solas. Si el hilo está
    apagado (AUTOCARGA_FACTURAS=0) o no arrancó, la pantalla vuelve a cargarlas
    ella misma: nadie se queda sin la carga.
    """
    return any(t.name == "auto-carga-facturas" and t.is_alive()
               for t in threading.enumerate())


def start_auto_carga_thread() -> bool:
    """Arranca el hilo (una sola vez). Devuelve True si arrancó."""
    global _started
    if _started:
        return False
    if os.environ.get("AUTOCARGA_FACTURAS", "1").strip() == "0":
        _LOG.info("auto-carga facturas (fondo) APAGADA por AUTOCARGA_FACTURAS=0")
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    _started = True
    # DOS hilos: el de facturas con su propio reloj (que es el que la dueña
    # mira), y el lento con todo lo demás. Antes era uno solo y las facturas
    # esperaban en la fila detrás de la traza y los syncs.
    threading.Thread(
        target=_loop_facturas, name="auto-carga-facturas", daemon=True
    ).start()
    threading.Thread(
        target=_loop, name="ciclo-de-fondo", daemon=True
    ).start()
    _LOG.info("auto-carga facturas (fondo) iniciada (cada %ss)", _intervalo_secs())
    return True
