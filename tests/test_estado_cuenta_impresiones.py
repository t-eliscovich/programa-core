"""Las tres impresiones del estado de cuenta y el pie con el total de cheques.

TMT 2026-08-03. Dueña: "cuando saco la impresión del estado de cuenta del
cliente, podemos generar igual dos botones — 1, que diga imprimir facturas y
salga esto" y "únicamente agregando abajo una línea q diga TOTAL CHEQUES XXX
(que son los cheques por cobrar)". Alex, por WhatsApp: "que sea para imprimir
SOLO listado de cheques… de momento se imprime incluso los depositados y para
enviar eso al cliente viene a ser innecesario".

Referencia: `CUENTA.PRG` L365-392 cierra el estado de cuenta con SALDO /
CHEQUES A DEPOSITAR / (+ CHEQUES PROTESTADOS) / TOTAL, pegados a las facturas.

Son tests de PLANTILLA (no tocan Postgres): leen los archivos y verifican los
ganchos que hacen andar las tres impresiones. Si alguien saca una clase o un
botón, esto falla.
"""

from __future__ import annotations

from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "modules/informes/templates/informes"
IMPRESO = (TPL / "_estado_cuenta_impreso.html").read_text(encoding="utf-8")
PANTALLA = (TPL / "estado_cuenta.html").read_text(encoding="utf-8")


def test_el_pie_de_facturas_trae_el_total_de_cheques_por_cobrar():
    """La línea que pidió la dueña, y el TOTAL, van PEGADOS a las facturas.

    Antes el total vivía al final de todo, después de la tabla de cheques: al
    imprimir sólo la hoja de facturas no salía.
    """
    i_tabla_facturas = IMPRESO.index("ec-bloque-facturas")
    i_cheques = IMPRESO.index("ec-bloque-cheques")
    i_total = IMPRESO.index("Total cheques")
    assert i_tabla_facturas < i_total < i_cheques, (
        "el pie con el total de cheques tiene que quedar dentro del bloque de "
        "facturas, antes de la tabla de cheques"
    )
    # Y el TOTAL suma saldo + cheques por cobrar + protestados (CUENTA.PRG).
    assert "(_saldo_neto or 0) + _ch_depositar + _ch_protestados" in IMPRESO
    assert "+ Cheques protestados" in IMPRESO


def test_hay_tres_botones_de_impresion():
    for etiqueta in (">Imprimir todo<", ">Imprimir facturas<", ">Imprimir cheques<"):
        assert etiqueta in PANTALLA, etiqueta
    assert "ecImprimir('facturas')" in PANTALLA
    assert "ecImprimir('cheques')" in PANTALLA


def test_cada_boton_oculta_el_bloque_que_no_toca():
    assert "body.ec-print-facturas .ec-bloque-cheques { display: none" in PANTALLA
    assert "body.ec-print-cheques  .ec-bloque-facturas { display: none" in PANTALLA


def test_imprimir_cheques_deja_afuera_los_ya_depositados():
    """Alex: mandarle al cliente los cheques que ya cobramos "viene a ser
    innecesario"."""
    assert "body.ec-print-cheques  tr.ec-ch-cobrado { display: none" in PANTALLA
    # La fila se marca con la clase sólo si el cheque pasó por el banco.
    assert "' ec-ch-cobrado' if stat_u in ('B','A')" in IMPRESO


def test_la_clase_de_impresion_se_limpia_siempre():
    """Si quedara pegada, la pantalla se vería mutilada después de imprimir."""
    assert "afterprint" in PANTALLA
    assert "setTimeout(limpiar, 3000)" in PANTALLA


def test_las_tres_pantallas_fechan_el_cheque_igual():
    """Cargado y Depositado tienen que salir de la MISMA expresión en las tres.

    TMT 2026-08-03. `/cheques` y la ficha se arreglaron a la mañana; este
    parcial tiene query propia y se quedó atrás: "Cargado" caía a `fecha_crea`
    (12/07/2026 en los ~3.200 del dBase) y "Depositado" leía `fechaing` en vez
    de `fechaout`. Es el papel que se le manda al cliente.
    """
    from modules.informes import queries as iq

    assert "dia_ingreso" in IMPRESO
    assert "(c.fechaout or c.fechaing)" in IMPRESO
    # La query del estado de cuenta usa la constante compartida, no una copia.
    fuente = Path(iq.__file__).read_text(encoding="utf-8")
    assert "_SQL_DIA_INGRESO_CHEQUE" in fuente
    assert "c.fechaout" in fuente

    for tpl_dir, nombre in (
        ("modules/cheques/templates/cheques", "lista.html"),
        ("modules/cheques/templates/cheques", "detalle.html"),
    ):
        html = (Path(__file__).resolve().parent.parent / tpl_dir / nombre).read_text(
            encoding="utf-8"
        )
        assert "dia_ingreso" in html, nombre
        assert "fechaout or " in html, nombre
