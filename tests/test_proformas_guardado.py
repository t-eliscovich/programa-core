"""Guardar una proforma, reabrirla y que el barrido de los 7 días la borre.

TMT 2026-08-11. Alex por WhatsApp: *"podemos dejar la opción de guardar las
proformas, ya que en ocasiones los clientes piden aumentar o quitar algo y lo
que ahora toca es repetir la proforma en cada petición"*.

Estos tests corren contra Postgres de verdad (`-m db`) porque lo que hay que
probar es justamente el viaje de ida y vuelta por la base: lo que se guarda
tiene que volver IGUAL al formulario. Un fake que devuelve lo que le pusiste no
prueba nada de eso.

Lo que fijan, en orden de importancia:

  1. Round-trip: kilos, cantidad tipeada, precio, descuento y flete vuelven
     iguales — si esto se rompe, Alex reabre una proforma y le da otro número
     al cliente.
  2. Reabrir NO pisa: quedan las dos y la nueva apunta a la vieja.
  3. Volver a apretar Imprimir en la MISMA pantalla regraba, no duplica.
  4. El barrido borra por EDAD (se prueba moviendo el reloj, no esperando una
     semana) y se lleva el detalle con él.
"""
from __future__ import annotations

from datetime import date

import pytest

CODIGO = "ZQX"   # cliente de prueba, no existe en la data real


@pytest.fixture
def cliente_prueba(migrated_db):
    """Un cliente propio, y la base como estaba al terminar."""
    import db

    fila = db.fetch_one(
        "SELECT id_cliente FROM scintela.cliente WHERE codigo_cli = %s", (CODIGO,)
    )
    creado = False
    if not fila:
        fila = db.execute_returning(
            "INSERT INTO scintela.cliente (codigo_cli, nombre, descuento, pago) "
            "VALUES (%s, %s, %s, %s) RETURNING id_cliente",
            (CODIGO, "CLIENTE DE PRUEBA PROFORMAS", "0", "C"),
        )
        creado = True
    id_cliente = fila["id_cliente"]
    try:
        yield id_cliente
    finally:
        db.execute(
            "DELETE FROM scintela.proforma_cabecera WHERE id_cliente = %s", (id_cliente,)
        )
        if creado:
            db.execute("DELETE FROM scintela.cliente WHERE id_cliente = %s", (id_cliente,))


LINEAS = [
    # Factura Proforma: 100 piezas → 2.200 kg a 9,12 (precio CON IVA)
    {"tela": "jersey", "nombre_producto": "JERSEY", "color": "NEGRO", "clase": 5,
     "cantidad": 100, "cantidad_kilos": 2200, "precio_unitario": 9.12},
    {"tela": "cuellos", "nombre_producto": "CUELLOS", "color": "BLANCO", "clase": 1,
     "cantidad": 330, "cantidad_kilos": 10, "precio_unitario": 12.50},
]


def _crear(**kw):
    from modules.proformas import queries as Q
    base = dict(
        codigo_cli=CODIGO, fecha=date(2026, 8, 11), lineas=LINEAS,
        pct_volumen=10, aplica_contado=True, pct_contado=5.0,
        flete=25, es_pedido=True, observaciones="entregar el jueves",
        usuario="alex",
    )
    base.update(kw)
    return Q.crear(**base)


@pytest.mark.db
def test_round_trip_lo_guardado_vuelve_igual(cliente_prueba):
    """Lo que Alex tipeó es lo que la pantalla le devuelve al reabrir."""
    from modules.proformas import queries as Q

    res = _crear()
    vuelto = Q.para_formulario(res["id_proforma"])

    assert vuelto["es_pedido"] is True
    assert vuelto["form"]["codigo_cli"] == CODIGO
    assert vuelto["form"]["fecha"] == "11/08/2026"
    assert vuelto["form"]["descuento_volumen"] == "10"
    assert vuelto["form"]["flete"] == "25"
    assert vuelto["form"]["observaciones"] == "entregar el jueves"

    assert [ln["label"] for ln in vuelto["lineas"]] == ["JERSEY", "CUELLOS"]
    assert [ln["tela"] for ln in vuelto["lineas"]] == ["jersey", "cuellos"]
    assert [ln["color"] for ln in vuelto["lineas"]] == ["NEGRO", "BLANCO"]
    # Las PIEZAS tipeadas y los KILOS son dos cosas distintas y vuelven las dos.
    assert [ln["cantidad"] for ln in vuelto["lineas"]] == [100.0, 330.0]
    assert [ln["kg"] for ln in vuelto["lineas"]] == [2200.0, 10.0]
    # El precio vuelve CON IVA, como se guardó: es el que el cliente tiene en
    # el papel. Nadie lo divide por 1,15 en el camino.
    assert [ln["precio"] for ln in vuelto["lineas"]] == [9.12, 12.50]


@pytest.mark.db
def test_los_totales_no_se_creen_lo_que_manda_el_browser(cliente_prueba):
    """La plata la calcula el servidor sobre las líneas que llegaron."""
    from modules.proformas import queries as Q

    res = _crear()
    esperado = Q.calcular_totales(
        [{"cantidad_kilos": 2200, "precio_unitario": 9.12},
         {"cantidad_kilos": 10, "precio_unitario": 12.50}],
        pct_volumen=10, aplica_contado=True, pct_contado=5.0, flete=25,
    )
    guardado = Q.detalle(res["id_proforma"])["cabecera"]
    assert float(guardado["subtotal"]) == esperado["subtotal"]
    assert float(guardado["monto_descuento_contado"]) == esperado["monto_descuento_contado"]
    assert float(guardado["monto_descuento_volumen"]) == esperado["monto_descuento_volumen"]
    assert float(guardado["flete"]) == 25.0
    assert float(guardado["total_final"]) == esperado["total_final"]


@pytest.mark.db
def test_reabrir_deja_las_dos_y_la_nueva_apunta_a_la_vieja(cliente_prueba):
    """Decisión de la dueña: la que el cliente ya tiene en la mano no se toca."""
    from modules.proformas import queries as Q

    primera = _crear()
    segunda = _crear(id_origen=primera["id_proforma"], lineas=LINEAS[:1])

    assert segunda["id_proforma"] != primera["id_proforma"]
    vieja = Q.detalle(primera["id_proforma"])
    nueva = Q.detalle(segunda["id_proforma"])
    assert vieja is not None, "la anterior tiene que seguir existiendo"
    assert len(vieja["items"]) == 2, "y sin tocarle las líneas"
    assert nueva["cabecera"]["id_origen"] == primera["id_proforma"]
    assert nueva["origen_vive"] is True


@pytest.mark.db
def test_origen_borrado_no_deja_un_link_roto(cliente_prueba):
    """Si el barrido se llevó la anterior, la ficha lo dice en vez de linkear."""
    import db

    from modules.proformas import queries as Q

    primera = _crear()
    segunda = _crear(id_origen=primera["id_proforma"])
    db.execute("DELETE FROM scintela.proforma_cabecera WHERE id_proforma = %s",
               (primera["id_proforma"],))

    nueva = Q.detalle(segunda["id_proforma"])
    assert nueva["cabecera"]["id_origen"] == primera["id_proforma"]
    assert nueva["origen_vive"] is False


@pytest.mark.db
def test_regrabar_la_misma_no_deja_dos(cliente_prueba):
    """Alex ve un error en el papel, lo corrige y vuelve a apretar Imprimir."""
    import db

    from modules.proformas import queries as Q

    res = _crear()
    Q.actualizar(
        res["id_proforma"], codigo_cli=CODIGO, fecha=date(2026, 8, 11),
        lineas=LINEAS[:1], pct_volumen=0, aplica_contado=True, flete=0,
        observaciones=None,
    )
    n = db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.proforma_cabecera WHERE id_cliente = %s",
        (cliente_prueba,),
    )["n"]
    assert n == 1, "regrabar no puede crear una proforma nueva"

    d = Q.detalle(res["id_proforma"])
    assert len(d["items"]) == 1, "las líneas viejas no pueden quedar colgadas"
    assert float(d["cabecera"]["monto_descuento_volumen"]) == 0
    assert float(d["cabecera"]["flete"]) == 0


@pytest.mark.db
def test_regrabar_una_que_ya_no_existe_avisa(cliente_prueba):
    """El barrido se la llevó con la pantalla abierta: la ruta cae a crear una
    nueva, y para eso necesita enterarse."""
    from modules.proformas import queries as Q

    res = _crear()
    import db
    db.execute("DELETE FROM scintela.proforma_cabecera WHERE id_proforma = %s",
               (res["id_proforma"],))
    with pytest.raises(LookupError):
        Q.actualizar(res["id_proforma"], codigo_cli=CODIGO, fecha=date(2026, 8, 11),
                     lineas=LINEAS, pct_volumen=0, flete=0)


@pytest.mark.db
def test_el_barrido_borra_por_EDAD_y_se_lleva_el_detalle(cliente_prueba):
    """Se prueba MOVIENDO EL RELOJ (envejeciendo la fila), no esperando una
    semana: la de 8 días se va, la de hoy queda."""
    import db

    from modules.proformas import queries as Q

    vieja = _crear()
    nueva = _crear()
    db.execute(
        "UPDATE scintela.proforma_cabecera SET creado_en = NOW() - INTERVAL '8 days' "
        "WHERE id_proforma = %s", (vieja["id_proforma"],),
    )

    borradas = Q.purgar_viejas(7)

    assert borradas == 1
    assert Q.detalle(vieja["id_proforma"]) is None
    assert Q.detalle(nueva["id_proforma"]) is not None
    huerfanas = db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.proforma_detalle WHERE id_proforma = %s",
        (vieja["id_proforma"],),
    )["n"]
    assert huerfanas == 0, "el detalle se tiene que ir con la cabecera"


@pytest.mark.db
def test_el_barrido_no_toca_las_de_justo_7_dias(cliente_prueba):
    """El corte es 'más de 7 días', no 'de 7 días o más'."""
    import db

    from modules.proformas import queries as Q

    justa = _crear()
    db.execute(
        "UPDATE scintela.proforma_cabecera SET creado_en = NOW() - INTERVAL '6 days 23 hours' "
        "WHERE id_proforma = %s", (justa["id_proforma"],),
    )
    assert Q.purgar_viejas(7) == 0
    assert Q.detalle(justa["id_proforma"]) is not None


@pytest.mark.db
def test_una_proforma_sin_lineas_no_se_guarda(cliente_prueba):
    from modules.proformas import queries as Q

    with pytest.raises(ValueError):
        _crear(lineas=[{"cantidad_kilos": 0, "precio_unitario": 0}])
    with pytest.raises(ValueError):
        Q.crear(codigo_cli="NOEXISTE", fecha=date(2026, 8, 11), lineas=LINEAS)
