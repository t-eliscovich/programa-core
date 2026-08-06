"""La traza baja a DOCUMENTO — y el stock, a CAUDAL.

TMT 2026-08-06 (dueña, mirando /informes/traza): *"¿podés explicarme mejor el
movimiento exacto que causó la movida?"* y, sobre el stock: *"pero stock
también, por qué sube y por qué baja"*.

Hasta la mig 0171 la grabadora guardaba once totales cada cinco minutos y la
explicación por documento corría dos veces por día. Estos tests cubren lo que
cambió: el motor de la foto (`modules/informes/foto.py`) corriendo a la
cadencia de la traza, los caudales del stock, y que los links del detalle
apunten a pantallas que existen.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.informes import foto as motor  # noqa: E402
from modules.informes import traza as t  # noqa: E402
from modules.informes.foto import _f  # noqa: E402

# ── Helpers ─────────────────────────────────────────────────────────────────

def _guardada(filas: list[dict]) -> dict:
    return {(f["componente"], f["doc_id"]): f for f in filas}


def _fila(comp, doc, imp, etiqueta="x", cantidad=None, precio=None) -> dict:
    return {"componente": comp, "doc_id": doc, "importe": imp,
            "etiqueta": etiqueta, "cantidad": cantidad, "precio": precio}


def _caudal(etapa, sentido, kg, mes="2026-08") -> dict:
    return {"componente": motor.FLUJO, "doc_id": f"{etapa}:{sentido}:{mes}",
            "etiqueta": motor.CAUDALES[(etapa, sentido)],
            "importe": 0.0, "cantidad": kg, "precio": None}


def _etapa(nombre, kg, ukg) -> dict:
    return _fila("vsto", f"#{nombre}", round(kg * ukg, 2),
                 etiqueta=f"Stock {nombre}", cantidad=kg, precio=ukg)


# ── El invariante, ahora cada cinco minutos ─────────────────────────────────

def test_el_invariante_se_cumple_en_una_ventana_de_cinco_minutos():
    """La ventana se achicó de doce horas a cinco minutos; la promesa no.

    Reproduce la fila real del 06/08 16:14, donde la traza mostraba
    "Facturas +11.399" sin decir de quién: tres facturas que suman 11.399,30.
    """
    vieja = _guardada([_fila("facturas", "f1", 5000.0)])
    nueva = [
        _fila("facturas", "f1", 5000.0),                       # sin cambios
        _fila("facturas", "f2", 162.17, "Factura … · GBC"),
        _fila("facturas", "f3", 495.67, "Factura … · SAC"),
        _fila("facturas", "f4", 10741.46, "Factura … · AJT"),
    ]
    movs = motor.diff(nueva, vieja)
    assert round(sum(m["aporte"] for m in movs), 2) == 11399.30
    assert {m["doc_id"] for m in movs} == {"f2", "f3", "f4"}
    # Y la más grande queda primera: es la que contesta la pregunta.
    assert movs[0]["doc_id"] == "f4"


# ── El stock: por qué sube y por qué baja ───────────────────────────────────

def test_el_stock_se_abre_por_caudal_cuando_los_numeros_cierran():
    """1.000 kg tejidos y 400 despachados no son "el stock se movió +600":
    son dos noticias, y una es buena y la otra es normal."""
    vieja = _guardada([_etapa("tejido", 10000.0, 2.0),
                       _caudal("tejido", "ingreso", 5000.0),
                       _caudal("tejido", "egreso", 2000.0)])
    nueva = [_etapa("tejido", 10600.0, 2.0),
             _caudal("tejido", "ingreso", 6000.0),      # +1.000 en la ventana
             _caudal("tejido", "egreso", 2400.0)]       # −400 en la ventana
    movs = motor.diff(nueva, vieja)

    etiquetas = {m["etiqueta"]: m["aporte"] for m in movs}
    assert any("se tejió tela cruda" in e for e in etiquetas)
    assert any("salió tela cruda a tintorería" in e for e in etiquetas)
    # Y sigue cerrando contra el Δ del componente.
    assert round(sum(m["aporte"] for m in movs), 2) == 1200.0


def test_si_los_caudales_no_cierran_no_se_parte_el_delta():
    """Los caudales y el saldo salen de dos lecturas de Asinfo con cachés
    propias: en cinco minutos se desfasan. Partir el Δ con números que no
    cierran sería fabricar una precisión que no tenemos — así que se informa
    de a un renglón, con los caudales de nota al pie."""
    vieja = _guardada([_etapa("tejido", 10000.0, 2.0),
                       _caudal("tejido", "ingreso", 5000.0),
                       _caudal("tejido", "egreso", 2000.0)])
    nueva = [_etapa("tejido", 10600.0, 2.0),
             _caudal("tejido", "ingreso", 5100.0),      # sólo +100: no explica los +600
             _caudal("tejido", "egreso", 2000.0)]
    movs = motor.diff(nueva, vieja)

    kilos = [m for m in movs if m["doc_id"].endswith(":kilos")]
    assert len(kilos) == 1
    assert kilos[0]["aporte"] == 1200.0
    # El caudal que sí se conoce no se tira: va en la etiqueta.
    assert "se tejió tela cruda 100 kg" in kilos[0]["etiqueta"]
    assert round(sum(m["aporte"] for m in movs), 2) == 1200.0


def test_la_tarifa_se_informa_aparte_de_los_kilos():
    """Un cambio de $/kg revalúa TODO el stock de un saque sin que se haya
    producido ni vendido nada. Mezclarlo con los kilos borra la diferencia."""
    vieja = _guardada([_etapa("hilado", 1000.0, 3.00)])
    nueva = [_etapa("hilado", 1000.0, 3.05)]
    movs = motor.diff(nueva, vieja)
    assert len(movs) == 1
    assert movs[0]["aporte"] == 50.0
    assert "→" in movs[0]["etiqueta"]
    assert "3,0000" in movs[0]["etiqueta"]        # formato Ecuador, no yanqui


def test_una_tarifa_que_no_cambio_a_la_vista_no_es_un_renglon():
    """🚨 TMT 2026-08-06: la pantalla decía "cambió el $/kg: $ 5,2591 →
    $ 5,2591". Con 300.000 kg, una diferencia en la quinta decimal da varios
    dólares y generaba un renglón que no dice nada. Esos centavos no se
    pierden: los absorbe el renglón de kilos."""
    vieja = _guardada([_etapa("terminado", 300000.0, 5.25910)])
    nueva = [{**_etapa("terminado", 300042.85, 5.25911)}]
    movs = motor.diff(nueva, vieja)
    etiq = [m["etiqueta"] or "" for m in movs]
    assert not any("→" in e for e in etiq)
    assert not any("redondeo" in e for e in etiq)
    # …y el Δ del componente sigue cerrando al centavo.
    d = round(_f(nueva[0]["importe"]) - _f(vieja[("vsto", "#terminado")]["importe"]), 2)
    assert round(sum(m["aporte"] for m in movs), 2) == d


def test_el_redondeo_de_la_particion_no_es_un_renglon_propio():
    """Tiene que existir para que el invariante cierre, pero "Stock: redondeo
    de la partición" no significa nada para nadie: se dobla en la parte que lo
    generó."""
    vieja = _guardada([_etapa("tejido", 10000.0, 2.3333),
                       _caudal("tejido", "ingreso", 0.0),
                       _caudal("tejido", "egreso", 0.0)])
    nueva = [_etapa("tejido", 10333.0, 2.7777),
             _caudal("tejido", "ingreso", 333.0),
             _caudal("tejido", "egreso", 0.0)]
    movs = motor.diff(nueva, vieja)
    assert not any("redondeo" in (m["etiqueta"] or "") for m in movs)
    d = round(_f(nueva[0]["importe"]) - _f(vieja[("vsto", "#tejido")]["importe"]), 2)
    assert round(sum(m["aporte"] for m in movs), 2) == d


def test_los_caudales_no_son_plata_y_no_generan_movimiento_propio():
    """Si un caudal se contara como movimiento, los kilos entrarían dos veces
    en la suma y el invariante se rompería."""
    vieja = _guardada([_caudal("terminado", "ingreso", 1000.0)])
    nueva = [_caudal("terminado", "ingreso", 4000.0)]
    assert motor.diff(nueva, vieja) == []


def test_el_cambio_de_mes_no_parece_una_baja_gigante():
    """El acumulado del caudal se reinicia el 1°. Si la clave no llevara el mes
    adentro, el Δ del primer minuto de septiembre sería el mes de agosto en
    negativo."""
    vieja = _guardada([_caudal("tejido", "ingreso", 90000.0, mes="2026-08")])
    nueva = [_caudal("tejido", "ingreso", 120.0, mes="2026-09")]
    assert motor.diff(nueva, vieja) == []


# ── La primera foto ─────────────────────────────────────────────────────────

def test_la_primera_foto_se_reconoce_por_la_marca_y_no_por_estar_vacia():
    """🚨 Si "primera" se decidiera por si la tabla está vacía, una foto
    legítimamente vacía —todo cobrado, todo pagado— haría que la vuelta
    siguiente diffeara contra la nada y cada factura viva de la cartera
    saliera como "venta de este minuto"."""
    assert motor.es_primera({}) is True
    assert motor.es_primera(_guardada([
        {"componente": motor.META, "doc_id": "iniciada", "importe": 0.0},
    ])) is False
    # Una foto sin documentos pero YA iniciada no es la primera.
    vacia_pero_iniciada = _guardada([
        {"componente": motor.META, "doc_id": "iniciada", "importe": 0.0}])
    assert motor.es_primera(vacia_pero_iniciada) is False


def test_la_marca_viaja_en_cada_foto():
    with patch.object(motor, "_det_flujo", return_value=[]), \
         patch.object(motor, "_rows", return_value=[]), \
         patch.object(motor, "_det_bancos", return_value=[]):
        det = motor.detalle({"diagnostico": {"componentes": {}}})
    assert (motor.META, "iniciada") in {(d["componente"], d["doc_id"]) for d in det}


# ── La foto se actualiza por diferencia, no a lo bruto ──────────────────────

class _ConnFalso:
    def __init__(self):
        self.sql = []


def test_aplicar_toca_solo_lo_que_cambio():
    """🚨 Son ~7.100 filas y 288 vueltas por día. Borrar y reescribir la foto
    entera —como hacía la versión de dos capturas diarias— serían dos millones
    de escrituras diarias para mover, en una vuelta típica, menos de
    cincuenta."""
    vieja = _guardada([
        _fila("facturas", "f1", 1000.0),      # queda igual
        _fila("facturas", "f2", 500.0),       # cambia
        _fila("facturas", "f3", 300.0),       # desaparece
    ])
    nueva = [
        _fila("facturas", "f1", 1000.0),
        _fila("facturas", "f2", 250.0),
        _fila("facturas", "f4", 900.0),       # nace
    ]
    ejecutadas = []
    with patch.object(motor.db, "execute",
                      side_effect=lambda sql, *a, **k: ejecutadas.append(sql)):
        res = motor.aplicar(_ConnFalso(), nueva, vieja)

    assert res == {"altas": 1, "cambios": 1, "bajas": 1}
    # Una sentencia para el upsert (alta + cambio) y una para la baja. Y
    # ningún DELETE sin WHERE: la fila que no se movió no se toca.
    assert len(ejecutadas) == 2
    assert any(s.startswith("INSERT INTO scintela.traza_detalle") for s in ejecutadas)
    assert any(s.startswith("DELETE FROM scintela.traza_detalle WHERE") for s in ejecutadas)
    assert not any(s.strip() == "DELETE FROM scintela.traza_detalle" for s in ejecutadas)


def test_un_cambio_por_debajo_del_umbral_igual_actualiza_la_foto():
    """No genera movimiento (es ruido de centavos) pero SÍ se guarda: si no,
    un documento que se corre medio centavo por vuelta nunca alcanzaría el
    umbral y la foto se iría quedando atrás en silencio."""
    vieja = _guardada([_fila("facturas", "f1", 1000.000)])
    nueva = [_fila("facturas", "f1", 1000.005)]
    assert motor.diff(nueva, vieja) == []                 # no es noticia
    with patch.object(motor.db, "execute"):
        assert motor.aplicar(_ConnFalso(), nueva, vieja)["cambios"] == 1


# ── Los links del detalle apuntan a pantallas que existen ───────────────────

def test_el_doc_id_se_traduce_a_la_tabla_de_origen():
    assert t._ref("f123") == ("factura", 123)
    assert t._ref("c7") == ("cheque", 7)
    assert t._ref("#hilado:tarifa") == (None, None)       # sintética
    assert t._ref("") == (None, None)
    assert t._ref("z9") == (None, None)                   # prefijo desconocido


def test_los_links_del_detalle_resuelven_contra_el_url_map(app):
    """🚨 Los links de este repo son strings hardcodeados, no `url_for`: una
    ruta que no existe no se ve desde el código, sólo como 404 al clickear
    (TMT 2026-08-03: *"cuando clické el link de compra 473 me dice 404"*).
    Este test recorre TODOS los prefijos que el detalle sabe linkear. Si mañana
    alguien agrega uno sin pantalla, falla acá y no en la cara de la dueña.
    """
    from modules.historial.queries import link_origen

    adaptador = app.url_map.bind("localhost")
    rotos = []
    for prefijo in sorted(t.PREFIJO_TABLA):
        tabla, rid = t._ref(f"{prefijo}473")
        assert tabla, f"el prefijo {prefijo!r} no mapea a ninguna tabla"
        url, etiqueta = link_origen({"origen_table": tabla, "origen_id": rid})
        assert etiqueta, f"{tabla}: etiqueta vacía"
        if url is None:
            continue                                      # sin ficha, a propósito
        try:
            adaptador.match(urlsplit(url).path, method="GET")
        except MethodNotAllowed:
            pass                                          # la ruta existe
        except NotFound:
            rotos.append((prefijo, tabla, url))
    assert rotos == [], (
        "estos links del detalle de la traza dan 404: "
        + ", ".join(f"{p} ({tb}) → {u}" for p, tb, u in rotos))


def test_los_caudales_se_pueden_apagar_sin_tocar_codigo():
    """Son tres consultas a Asinfo por vuelta. Si alguna vez le pesan al ERP,
    se apagan por entorno y el stock vuelve a informarse en un solo renglón —
    la pantalla sigue andando, sólo dice menos."""
    with patch.dict(os.environ, {"TRAZA_CAUDALES": "0"}):
        assert motor._det_flujo() == []


# ── La grilla de saldos: apagar lo que no se movió ──────────────────────────

def test_la_grilla_marca_los_kilos_y_la_tarifa_que_se_movieron():
    """TMT 2026-08-06: *"agregame los distintos stocks"*. La grilla muestra
    SALDOS, y un saldo de siete dígitos que no cambió pesa lo mismo que el que
    sí: sin marcar cuál se movió habría que restar a ojo, fila contra fila."""
    viejo = {"utilidad": 100.0, "hilado_kg": 1899100.0, "tejido_kg": 294986.0,
             "terminado_kg": 313339.0, "hilado_ukg": 3.0513}
    nuevo = dict(viejo, utilidad=150.0, tejido_kg=296136.0, hilado_ukg=3.0591)
    fila = t.con_deltas([nuevo, viejo])[0]
    assert "tejido_kg" in fila["movidas"]
    assert "hilado_ukg" in fila["movidas"]          # la 4ª decimal cuenta
    assert "hilado_kg" not in fila["movidas"]
    assert "terminado_kg" not in fila["movidas"]


def test_la_foto_mas_vieja_no_tiene_nada_marcado():
    """No hay contra qué compararla: marcar todo sería mentir."""
    fila = t.con_deltas([{"utilidad": 100.0}])[0]
    assert fila["d_utilidad"] is None
    assert fila["movidas"] == set()


def test_las_columnas_de_la_grilla_existen_en_la_foto():
    """Si alguien agrega una columna que la tabla no guarda, la pantalla
    renderiza vacío sin avisar. Acá avisa."""
    from modules.informes.traza import _fila_desde_balance
    campos = set(_fila_desde_balance({"diagnostico": {"componentes": {}}}))
    for col in t.COLUMNAS_DELTA:
        assert col in campos, f"la grilla pide {col} y la foto no lo guarda"
    for col, _label in t.COLUMNAS_KG:
        assert col in campos, f"la grilla pide {col} y la foto no lo guarda"


def test_la_grilla_trae_el_delta_por_componente():
    """TMT 2026-08-06: *"el formato no me gustó pero claramente necesitamos más
    columnas"*. La celda muestra el Δ —que ya es la respuesta— y queda vacía
    cuando ese componente no se movió."""
    viejo = {"utilidad": 100.0, "facturas": 1000.0, "totp": 500.0, "caja": 50.0}
    nuevo = {"utilidad": 1900.0, "facturas": 3000.0, "totp": 600.0, "caja": 50.0}
    fila = t.con_deltas([nuevo, viejo])[0]
    assert fila["delta"]["facturas"] == 2000.0
    assert fila["delta"]["totp"] == -100.0        # pasivo: aporta al revés
    assert "caja" not in fila["delta"]            # no se movió → celda vacía


def test_una_ventana_vieja_no_dice_que_no_se_movio_nada():
    """🚨 "Sin movimientos" y "sin registro" no son lo mismo. Una ventana
    anterior a la grabadora de detalle se ve igual que una en la que de verdad
    no pasó nada —las dos con la lista vacía— y decirle a la dueña "no se movió
    ningún documento" sobre una ventana de $2.000 es mentirle."""
    par = [{"id_traza": 5, "utilidad": 2000.0, "facturas": 2000.0, "cuando": "17:23"},
           {"id_traza": 4, "utilidad": 0.0, "facturas": 0.0, "cuando": "17:17"}]
    with patch.object(t.db, "fetch_all", return_value=par), \
         patch.object(t, "movimientos", return_value=[]), \
         patch.object(t, "_desde_cuando_hay_detalle", return_value=99):
        vieja = t.una(5)
    assert vieja["sin_registro"] is True
    # …y el desglose por componente, que SÍ existe, queda a la vista.
    assert any(m["col"] == "facturas" for m in vieja["movio"])

    with patch.object(t.db, "fetch_all", return_value=par), \
         patch.object(t, "movimientos", return_value=[]), \
         patch.object(t, "_desde_cuando_hay_detalle", return_value=1):
        nueva = t.una(5)
    assert nueva["sin_registro"] is False   # la grabadora ya estaba: no pasó nada


def test_los_kilos_que_pasan_de_una_etapa_a_la_siguiente_son_UN_renglon():
    """🚨 TMT 2026-08-06, mirando cuatro renglones para explicar $ 36:
    *"ejemplo esto, +21kg de tejido a terminado. listo, todo el resto no
    entiendo para qué"*.

    La tela que se termina sale de tejido y entra a terminado en el mismo
    instante. Contarlo como dos movimientos es la contabilidad hablando sola;
    lo que aporta a la utilidad es la diferencia de precio entre las etapas —
    el valor que le agregó el proceso.
    """
    vieja = _guardada([_etapa("tejido", 299659.25, 3.5591),
                       _etapa("terminado", 312486.53, 5.2591)])
    nueva = [_etapa("tejido", 299637.60, 3.5591),
             _etapa("terminado", 312508.18, 5.2591)]
    movs = motor.diff(nueva, vieja)

    assert len(movs) == 1, [m["etiqueta"] for m in movs]
    assert movs[0]["etiqueta"] == "22 kg tejido→terminado"
    assert movs[0]["aporte"] == pytest.approx(21.65 * (5.2591 - 3.5591), abs=0.02)
    # Y sigue cerrando contra el Δ del componente, al centavo.
    d = round(sum(_f(f["importe"]) for f in nueva)
              - sum(_f(v["importe"]) for v in vieja.values()), 2)
    assert round(sum(m["aporte"] for m in movs), 2) == d


def test_el_resumen_dice_de_que_clientes_es():
    """TMT 2026-08-06: *"decime algo de las facturas, de los abonos, eso es un
    poco más importante. ¿clientes quizás?"*. "3 facturas nuevas" no dice nada;
    de quién son sí."""
    movs = [
        {"regla": "Venta facturada", "aporte": 10741.46, "componente": "facturas",
         "etiqueta": "Factura 001-099-000181251 · AJT"},
        {"regla": "Venta facturada", "aporte": 495.67, "componente": "facturas",
         "etiqueta": "Factura 001-099-000181250 · SAC"},
        {"regla": "Venta facturada", "aporte": 162.17, "componente": "facturas",
         "etiqueta": "Factura 001-099-000181249 · GBC"},
    ]
    g = t.resumir(movs, 11399.30)[0]
    assert g["texto"] == "3 facturas nuevas · AJT, SAC, GBC"   # el mayor primero
    assert g["aporte"] == 11399.30


def test_un_concepto_largo_no_se_confunde_con_un_cliente():
    """"Caja S · FLETE MERCADERIA QUITO" tiene un "·" pero lo de atrás no es
    un código de cliente."""
    assert t._quien("Factura 172916 · PGQ") == "PGQ"
    assert t._quien("Caja S · FLETE MERCADERIA QUITO") == ""
    assert t._quien("22 kg tejido→terminado") == ""


def test_un_centavo_no_es_un_renglon():
    """TMT 2026-08-06, viendo "-0,00 kg · Stock hilado  −0,01": *"no movió nada
    '0', ¿por qué lo mostrás?"*."""
    movs = [{"regla": "Venta facturada", "aporte": 5000.0, "componente": "facturas",
             "etiqueta": "Factura 1 · AAA"},
            {"regla": "Stock hilado", "aporte": -0.01, "componente": "vsto",
             "etiqueta": "-0 kg"}]
    textos = [g["texto"] for g in t.resumir(movs, 4999.99)]
    assert textos == ["Factura 1 · AAA"]      # el centavo no sale
