"""Andrés 2026-09-02 — *"la proyección de la utilidad está dando un valor raro"*.

`scintela.gastos_proyectado_mes` (el presupuesto por rubro que se carga en
/informes/gastos, fila "Gs. Proy. mes actual") NO tenía rollover de mes: la fila
se crea recién cuando alguien la guarda. El día 1 de cada mes no existía todavía,
`gastos_proyectado_mes_get()` devolvía {tej: 0, tin: 0, adm: 0}, y como la fila
"Utilidad Esperada" del balance es

    venta proyectada − GASTOS PROYECTADOS − costo directo

el gasto fijo del mes entero se caía de la cuenta y la utilidad salía inflada por
todo el presupuesto (cientos de miles de U$). De yapa, la columna "Proyecciones"
de Tejeduría / Tintorería / Administración / Costo Total quedaba en blanco.

Ahora hereda el último período cargado (mismo criterio que el rollover de
`scintela.iniciales`) y avisa que lo hizo.
"""
from __future__ import annotations

from modules.informes import queries

AGOSTO = {"periodo": "2026-08", "tej": 130_000.0, "tin": 372_000.0, "adm": 305_000.0}


def _install(monkeypatch, *, filas):
    """`filas` = lista de dicts con periodo/tej/tin/adm que "hay" en la tabla."""

    def fake_fetch_one(sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if "FROM scintela.gastos_proyectado_mes WHERE periodo = %s" in s:
            per = params[0]
            return next((f for f in filas if f["periodo"] == per), None)
        if "FROM scintela.gastos_proyectado_mes WHERE periodo < %s" in s:
            per = params[0]
            previas = [
                f for f in filas
                if f["periodo"] < per
                and (f.get("tej") or 0) + (f.get("tin") or 0) + (f.get("adm") or 0) > 0
            ]
            if not previas:
                return None
            return max(previas, key=lambda f: f["periodo"])
        raise AssertionError(f"query inesperada: {s}")

    monkeypatch.setattr(queries.db, "fetch_one", fake_fetch_one, raising=True)
    monkeypatch.setattr(queries, "_periodo_actual_ec", lambda: "2026-09", raising=True)


def test_mes_con_presupuesto_propio_no_hereda(monkeypatch):
    septiembre = {"periodo": "2026-09", "tej": 1.0, "tin": 2.0, "adm": 3.0}
    _install(monkeypatch, filas=[AGOSTO, septiembre])
    r = queries.gastos_proyectado_mes_get()
    assert (r["tej"], r["tin"], r["adm"]) == (1.0, 2.0, 3.0)
    assert r["heredado"] is False
    assert r["periodo_origen"] == "2026-09"


def test_mes_nuevo_hereda_el_ultimo_presupuesto_cargado(monkeypatch):
    # El caso del 02/09: la fila de septiembre no existe todavía.
    _install(monkeypatch, filas=[AGOSTO])
    r = queries.gastos_proyectado_mes_get()
    assert r["periodo"] == "2026-09"
    assert (r["tej"], r["tin"], r["adm"]) == (130_000.0, 372_000.0, 305_000.0)
    assert r["heredado"] is True
    assert r["periodo_origen"] == "2026-08"
    assert r["periodo_origen_nom"] == "agosto 2026"


def test_hereda_el_mas_reciente_y_saltea_los_meses_en_cero(monkeypatch):
    # Un mes en cero no es presupuesto: es lo mismo que no tener fila.
    julio = {"periodo": "2026-07", "tej": 99_000.0, "tin": 0.0, "adm": 0.0}
    agosto_cero = {"periodo": "2026-08", "tej": 0.0, "tin": 0.0, "adm": 0.0}
    _install(monkeypatch, filas=[julio, agosto_cero])
    r = queries.gastos_proyectado_mes_get()
    assert r["periodo_origen"] == "2026-07"
    assert r["tej"] == 99_000.0


def test_sin_ningun_presupuesto_previo_devuelve_ceros(monkeypatch):
    _install(monkeypatch, filas=[])
    r = queries.gastos_proyectado_mes_get()
    assert (r["tej"], r["tin"], r["adm"]) == (0.0, 0.0, 0.0)
    assert r["heredado"] is False
    assert r["periodo_origen"] is None


def test_periodo_explicito_respeta_el_argumento(monkeypatch):
    _install(monkeypatch, filas=[AGOSTO])
    assert queries.gastos_proyectado_mes_get("2026-08")["heredado"] is False


def test_periodo_nombre_es_tolera_basura():
    assert queries._periodo_nombre_es("2026-01") == "enero 2026"
    assert queries._periodo_nombre_es("") == ""
    assert queries._periodo_nombre_es(None) == ""
    assert queries._periodo_nombre_es("2026-13") == "2026-13"
    assert queries._periodo_nombre_es("no-es-un-periodo") == "no-es-un-periodo"


def test_la_utilidad_esperada_no_se_infla_al_arrancar_el_mes(monkeypatch):
    """La razón de ser del fix, en plata.

    Sin herencia el presupuesto sale 0 y la Utilidad Esperada del balance se va
    para arriba por exactamente el gasto fijo del mes.
    """
    _install(monkeypatch, filas=[AGOSTO])
    heredado = queries.gastos_proyectado_mes_get()
    gastos_heredados = heredado["tej"] + heredado["tin"] + heredado["adm"]

    # La cuenta de views.balance() para la fila "Utilidad Esperada".
    proy_kg, precio, mp_ukg, col_ukg = 320_000.0, 8.10, 2.92, 0.64
    costo_directo = proy_kg * (mp_ukg + col_ukg) * 1.045

    up_con_fix = proy_kg * precio - gastos_heredados - costo_directo
    up_bug = proy_kg * precio - 0.0 - costo_directo

    assert round(up_bug - up_con_fix, 2) == round(gastos_heredados, 2)
    assert gastos_heredados > 800_000  # el desvío que veía Andrés, no un redondeo
