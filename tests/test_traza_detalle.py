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
from pathlib import Path
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


# ── El stock: un renglón, y los kilos en la columna de cada etapa ──────────

def test_el_stock_es_un_renglon_que_dice_de_donde_a_donde():
    """🚨 TMT 2026-08-06, después de cinco intentos: *"podría ser 'pasó de
    terminada a cruda', + en una columna y − en la otra. that is it"*.

    Y el caso que rompía todo lo anterior: salen 68,16 kg de tejido y entran
    65,40 a terminado. NO coinciden —merma, timing de Asinfo— así que la lógica
    de "esto es un traspaso" los rechazaba y salían tres renglones. Con los Δ
    en la columna de cada etapa no hace falta que coincidan.
    """
    vieja = _guardada([_etapa("tejido", 299700.00, 3.5591),
                       _etapa("terminado", 312440.00, 5.2591)])
    nueva = [_etapa("tejido", 299631.84, 3.5591),
             _etapa("terminado", 312505.40, 5.2591)]
    movs = motor.diff(nueva, vieja)

    assert len(movs) == 1, [m["etiqueta"] for m in movs]
    assert movs[0]["etiqueta"] == "tejido → terminado"
    d = round(sum(_f(f["importe"]) for f in nueva)
              - sum(_f(v["importe"]) for v in vieja.values()), 2)
    assert movs[0]["aporte"] == d              # cierra al centavo, sin resto


def test_si_solo_entra_o_solo_sale_lo_dice_asi():
    entra = motor.diff([_etapa("terminado", 312505.0, 5.2591)],
                       _guardada([_etapa("terminado", 312440.0, 5.2591)]))
    assert entra[0]["etiqueta"] == "entró a terminado"
    sale = motor.diff([_etapa("tejido", 299631.0, 3.5591)],
                      _guardada([_etapa("tejido", 299700.0, 3.5591)]))
    assert sale[0]["etiqueta"] == "salió de tejido"


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
    with patch.object(motor, "_rows", return_value=[]), \
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


# ── El link desde Resultados ────────────────────────────────────────────────

def test_resultados_ofrece_el_link_a_trazabilidad(app):
    """TMT 2026-08-06: *"¿podés poner un link desde resultados que se llame
    trazabilidad así pueden entrar el resto?"*. La pantalla vivía sólo por URL.

    🚨 Y el link tiene que resolver: en este repo son strings hardcodeados y una
    ruta que no existe sólo se ve como 404 al clickear."""
    barra = app.jinja_env.loader.get_source(app.jinja_env, "_ui.html")[0]
    assert "('traza', 'Trazabilidad')" in barra
    # Al lado de Historia, no al final (TMT: "poné el link al lado de historia").
    # TMT 2026-08-07: el botón pasó a llamarse "Historia" (antes "Historial").
    assert barra.index("'Historia'") < barra.index("'Trazabilidad'") < barra.index("'Gastos del mes'")
    app.url_map.bind("localhost").match("/informes/traza", method="GET")


# ── `fecha_modifica` se sella sola ──────────────────────────────────────────

def test_la_migracion_0172_no_lista_las_tablas_a_mano():
    """🚨 El bug que arregla es "alguien se olvidó de escribir la columna". Si
    la migración llevara la lista de tablas escrita a mano, el próximo que
    agregue una tabla se olvidaría igual: la lista sale de information_schema.

    Origen: la cobranza de PGQ del 06/08 canceló tres facturas y las tres
    quedaron con `usuario_modifica = 'alex'` y `fecha_modifica` en NULL — no
    había forma de preguntar qué se cobró entre las 17:17 y las 17:23.
    """
    sql = (Path(_REPO_ROOT) / "migrations" / "0172_sellar_fecha_modifica.sql").read_text(
        encoding="utf8")
    assert "information_schema.columns" in sql
    assert "fecha_modifica" in sql
    assert "BEFORE UPDATE" in sql
    # Idempotente: la migración se puede volver a correr sin romper nada.
    assert "DROP TRIGGER IF EXISTS" in sql
    assert "CREATE OR REPLACE FUNCTION" in sql


# ── El listado avisa cuál no cierra, y se puede filtrar ─────────────────────

def test_el_listado_marca_las_ventanas_que_no_cierran():
    """⭐ La métrica del entrenamiento es el residuo, así que tiene que
    perseguir a la dueña y no ella a él: antes había que abrir fila por fila
    para descubrir que una ventana no cerraba."""
    filas = [{"id_traza": 9, "d_utilidad": 1000.0, "cuando": "17:23"},
             {"id_traza": 8, "d_utilidad": 500.0, "cuando": "17:17"}]
    agr = [{"id_traza": 9, "explicado": 400.0, "ciegos": 1},
           {"id_traza": 8, "explicado": 500.0, "ciegos": 0}]
    with patch.object(t.db, "fetch_all", return_value=agr), \
         patch.object(t, "_desde_cuando_hay_detalle", return_value=1):
        out = t.marcar_residuo(filas)
    assert out[0]["residuo"] == 600.0            # 1.000 − 400
    assert out[1]["residuo"] == 0.0
    assert [f["cuando"] for f in t.sin_cerrar(out)] == ["17:23"]


def test_una_foto_vieja_no_cuenta_como_que_no_cierra():
    """No es que no cierre: es que no hay con qué. Meterla en la lista de
    tareas sería inventar 200 problemas que no existen."""
    filas = [{"id_traza": 2, "d_utilidad": 5000.0, "cuando": "15:43"}]
    with patch.object(t.db, "fetch_all", return_value=[]), \
         patch.object(t, "_desde_cuando_hay_detalle", return_value=100):
        out = t.marcar_residuo(filas)
    assert out[0]["sin_registro"] is True
    assert out[0]["residuo"] is None
    assert t.sin_cerrar(out) == []


def test_el_filtro_por_componente_no_toca_los_deltas():
    """🚨 El filtro se aplica DESPUÉS de calcular los Δ. Filtrar antes
    compararía cada foto contra la anterior QUE PASÓ EL FILTRO, no contra la
    anterior de verdad, y todos los números saldrían mal."""
    filas = [{"cuando": "3", "movidas": {"facturas"}},
             {"cuando": "2", "movidas": {"vsto"}},
             {"cuando": "1", "movidas": {"facturas", "vsto"}}]
    assert [f["cuando"] for f in t.filtrar_por_componente(filas, "facturas")] == ["3", "1"]
    assert t.filtrar_por_componente(filas, "") == filas       # sin filtro, todo
    assert t.filtrar_por_componente(filas, "inventado") == filas


# ── Las fotos viejas, reconstruidas ─────────────────────────────────────────

def test_un_movimiento_reconstruido_no_cuenta_como_registro_real():
    """🚨 Si contara, `sin_registro` se apagaría para las 200 fotos viejas y el
    aviso de "ventanas que no cierran" las tomaría como problemas: 200 tareas
    inventadas tapando las de verdad. Lo reconstruido sale de `fecha_crea` y le
    falta todo lo aplicado — aplicar un cheque no dejaba fecha."""
    with patch.object(t.db, "fetch_one", return_value={"m": 500}) as fo:
        assert t._desde_cuando_hay_detalle() == 500
    assert "tipo <> 'reconstruido'" in fo.call_args[0][0]


def test_una_foto_vieja_con_reconstruccion_se_marca_como_tal():
    par = [{"id_traza": 5, "utilidad": 2000.0, "cuando": "10:05"},
           {"id_traza": 4, "utilidad": 0.0, "cuando": "10:00"}]
    movs = [{"componente": "facturas", "aporte": 1200.0, "regla": "Venta facturada",
             "etiqueta": "Factura 1 · AAA", "tipo": "reconstruido"}]
    with patch.object(t.db, "fetch_all", return_value=par), \
         patch.object(t, "movimientos", return_value=movs), \
         patch.object(t, "_desde_cuando_hay_detalle", return_value=99):
        f = t.una(5)
    assert f["sin_registro"] is True
    assert f["reconstruido"] is True
    # Y no se le exige que cierre: le falta lo aplicado, por definición.
    assert f["residuo"] is None


def test_el_boton_se_llama_historia_no_historial(app):
    """TMT 2026-08-07, con una captura del botón: *"a esto, lo podes llamar
    historia en vez de historial"*."""
    barra = app.jinja_env.loader.get_source(app.jinja_env, "_ui.html")[0]
    assert "('historico_12m', 'Historia')" in barra
    assert "('historico_12m', 'Historial')" not in barra
