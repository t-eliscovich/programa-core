"""El total del panel "Posdatados contabilizados" de /informes/flujo/grafico.

TMT 2026-08-06 (dueña, captura): el encabezado decía "$ 0" con la tabla llena.
Bug de scoping de Jinja — un {% set %} adentro de un {% for %} no persiste
entre iteraciones, así que el acumulador quedaba en 0. Es el MISMO bug que el
panel gemelo "Gastos forzados" arregló el 15/07 con namespace(); acá se testea
que el panel de posdatados sume de verdad (y que None no lo rompa).
"""
import datetime as _dt

if not hasattr(_dt, "UTC"):  # py3.10
    _dt.UTC = _dt.UTC

from flask import g, render_template


def _render(app, posdat_filas, egresos_lista=()):
    with app.test_request_context("/informes/flujo/grafico"):
        # base.html esconde TODO el content sin g.user (mismo truco que
        # vista_local): sin esto la página renderiza solo el <head> y el
        # test pasa/falla contra una cáscara vacía.
        g.user = {"username": "test", "nombre_rol": "Accionista", "rol": 1}
        g.permisos = {"*"}
        return render_template(
            "informes/flujo_grafico.html",
            # datos NO puede ser vacío: {% if datos|length == 0 %} corta la
            # página en el empty-state y el panel de posdatados ni se renderiza.
            datos=[{"fecha": "2026-08-06", "saldo": 100.0, "tipo": "arranque"}],
            egresos_lista=list(egresos_lista),
            posdat_filas=posdat_filas,
            hoy="2026-08-06",
            ventana_dias=60,
            plazos={"cobro": 0, "deuda": 0},
        )


def test_total_posdat_suma_las_filas(app):
    html = _render(
        app,
        [
            {"id_posdat": 1, "fecha_efectiva": "2027-04-20", "fechad": "2027-04-20",
             "prov": "YY", "concepto": "SRI PROVISION", "importe": 250000.0, "banc": 0},
            {"id_posdat": 2, "fecha_efectiva": "2027-05-07", "fechad": "2027-05-07",
             "prov": "BP", "concepto": "13-48", "importe": 427500.0, "banc": 0},
        ]
    )
    # 250.000 + 427.500 = 677.500 — formato es-EC sin decimales (num_es(0)).
    assert "$ 677.500" in html


def test_total_posdat_none_no_rompe(app):
    html = _render(
        app,
        [
            {"id_posdat": 1, "fecha_efectiva": "2027-04-20", "fechad": None,
             "prov": "TJ", "concepto": "", "importe": None, "banc": 0},
            {"id_posdat": 2, "fecha_efectiva": "2027-05-07", "fechad": "2027-05-07",
             "prov": "BP", "concepto": "27/28", "importe": 17500.0, "banc": 0},
        ]
    )
    assert "$ 17.500" in html


def test_total_gastos_forzados_server_rendered(app):
    # TMT 2026-08-06 (dueña vio "$ 0" en la preview): el gf-total arrancaba
    # hardcodeado "$ 0" y solo el JS lo llenaba. Ahora el server lo rinde
    # con el total banc=9 de entrada (el JS después lo pisa sumando manuales).
    html = _render(
        app,
        [],
        egresos_lista=[
            {"id_posdat": 9, "fecha_efectiva": "2026-09-01", "fechad": "2026-09-01",
             "prov": "GG", "concepto": "pago gerencia", "importe": 12000.0,
             "banc": 9, "vencido": False},
        ],
    )
    assert 'id="gf-total">$ 12.000<' in html
