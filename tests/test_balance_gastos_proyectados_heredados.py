"""Andrés 2026-09-02 — la fila "Utilidad Esperada" del balance con el
presupuesto de gastos del mes todavía sin cargar.

Complemento de `test_gastos_proyectados_rollover_mes.py`: ese fija la herencia
en la query; éste fija que la VISTA la usa (la utilidad ya no se infla el día 1)
y que además lo DICE en la ayuda de la fila, para que nadie tome como definitivo
un número armado con el presupuesto del mes pasado.
"""
from __future__ import annotations

from unittest.mock import patch

from modules.informes import queries as informes_queries


def _login(app, fake_db, permisos=("informes.ver",)):
    rid = fake_db.add_role("Informes", list(permisos))
    uid = fake_db.add_user("u", b"$2b$12$fake", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


class _Bal(dict):
    """Balance de mentira: las claves que el template pide y este test no
    modela devuelven 0, así que la pantalla renderiza entera sin tener que
    replicar las ~80 claves de `informe_balance()`."""

    def __missing__(self, _k):
        return 0


def _balance_fake():
    """Balance mínimo con las filas que lee el recálculo de views.balance()."""
    return _Bal({
        "diagnostico": {"advertencias": [], "componentes": {}},
        "resultados": {
            "tabla": [
                {"label": "Ventas", "kg": 40_000.0, "ukg": 8.10, "us": 324_000.0},
                {"label": "Proyección", "kg": 320_000.0, "ukg": 8.10,
                 "us": 2_592_000.0},
                {"label": "Materia Prima", "kg": 40_000.0, "ukg": 2.92,
                 "us": 116_800.0},
                {"label": "Colorantes/Quím.", "kg": 40_000.0, "ukg": 0.64,
                 "us": 25_600.0},
                {"label": "Utilidad Esperada", "kg": 0.0, "ukg": 0.0, "us": 0.0,
                 "ayuda": "vieja"},
            ]
        },
    })


def _fila_utilidad_esperada(bal):
    return next(f for f in bal["resultados"]["tabla"]
                if f["label"] == "Utilidad Esperada")


def _correr(app, fake_db, proy):
    c = _login(app, fake_db)
    bal = _balance_fake()
    with patch.object(informes_queries, "informe_balance", return_value=bal), \
         patch.object(informes_queries, "venta_proyectada_mes_get",
                      return_value=None), \
         patch.object(informes_queries, "gastos_proyectado_mes_get",
                      return_value=proy), \
         patch("modules.posdat.queries.persistir_acumulacion_yy", lambda: None), \
         patch("modules.iniciales.views.auto_cerrar_mes_si_corresponde",
               lambda: None):
        r = c.get("/informes/balance")
    assert r.status_code == 200, r.data[:400]
    return r, _fila_utilidad_esperada(bal)


# Costo directo = 320.000 × (2,92 + 0,64) × 1,045
COSTO_DIRECTO = 320_000.0 * (2.92 + 0.64) * 1.045
VENTA_PROY = 2_592_000.0


def test_presupuesto_propio_del_mes_no_avisa_nada(app, fake_db):
    proy = {"periodo": "2026-09", "tej": 130_000.0, "tin": 372_000.0,
            "adm": 305_000.0, "heredado": False, "periodo_origen": "2026-09"}
    r, fila = _correr(app, fake_db, proy)
    assert round(fila["us"], 2) == round(VENTA_PROY - 807_000.0 - COSTO_DIRECTO, 2)
    assert "todavía no se cargó" not in fila["ayuda"]
    assert b"agosto 2026" not in r.data


def test_presupuesto_heredado_descuenta_igual_y_lo_avisa(app, fake_db):
    """El caso del 02/09: septiembre sin cargar, hereda agosto."""
    proy = {"periodo": "2026-09", "tej": 130_000.0, "tin": 372_000.0,
            "adm": 305_000.0, "heredado": True, "periodo_origen": "2026-08",
            "periodo_origen_nom": "agosto 2026"}
    r, fila = _correr(app, fake_db, proy)

    esperado = VENTA_PROY - 807_000.0 - COSTO_DIRECTO
    assert round(fila["us"], 2) == round(esperado, 2)
    # El bug: sin descontar el gasto fijo la fila salía 807k más arriba.
    assert round(VENTA_PROY - COSTO_DIRECTO - esperado, 2) == 807_000.0

    assert "todavía no se cargó" in fila["ayuda"]
    assert "agosto 2026" in fila["ayuda"]
    assert "agosto 2026" in r.data.decode("utf-8")


def test_dia_1_sin_ventas_ni_tintura_no_da_una_utilidad_absurda(app, fake_db):
    """Los dos agujeros del arranque de mes, juntos, como se ven el día 1.

    Sin ventas cargadas la Proyección se arma con el precio meta de Iniciales, y
    el costo directo con la tarifa meta de colorantes (`costo_var_ukg`, que trae
    la propia fila Proyección). Antes: Proyección 0 y colorantes 0 → la Utilidad
    Esperada salía en −(gastos + MP), un rojo que no era real.
    """
    proy = {"periodo": "2026-09", "tej": 130_000.0, "tin": 372_000.0,
            "adm": 305_000.0, "heredado": True, "periodo_origen": "2026-08",
            "periodo_origen_nom": "agosto 2026"}
    c = _login(app, fake_db)
    bal = _Bal({
        "diagnostico": {"advertencias": [], "componentes": {}},
        "resultados": {"tabla": [
            # Día 1: ni una factura, ni un kg tinturado.
            {"label": "Ventas", "kg": 0.0, "ukg": 0.0, "us": 0.0},
            {"label": "Proyección", "kg": 320_000.0, "ukg": 8.57,
             "us": 320_000.0 * 8.57, "costo_var_ukg": 2.92 + 0.64},
            {"label": "Materia Prima", "kg": 0.0, "ukg": 2.92, "us": 0.0},
            {"label": "Colorantes/Quím.", "kg": 0.0, "ukg": 0.0, "us": 0.0},
            {"label": "Utilidad Esperada", "kg": 0.0, "ukg": 0.0, "us": 0.0,
             "ayuda": "vieja"},
        ]},
    })
    with patch.object(informes_queries, "informe_balance", return_value=bal), \
         patch.object(informes_queries, "venta_proyectada_mes_get",
                      return_value=None), \
         patch.object(informes_queries, "gastos_proyectado_mes_get",
                      return_value=proy), \
         patch("modules.posdat.queries.persistir_acumulacion_yy", lambda: None), \
         patch("modules.iniciales.views.auto_cerrar_mes_si_corresponde",
               lambda: None):
        r = c.get("/informes/balance")
    assert r.status_code == 200, r.data[:400]

    fila = _fila_utilidad_esperada(bal)
    esperado = 320_000.0 * 8.57 - 807_000.0 - 320_000.0 * (2.92 + 0.64) * 1.045
    assert round(fila["us"], 2) == round(esperado, 2)
    assert fila["us"] > 0, "el día 1 la Utilidad Esperada no tiene por qué ser negativa"


def test_heredado_sin_nombre_lindo_cae_al_periodo_crudo(app, fake_db):
    proy = {"periodo": "2026-09", "tej": 1.0, "tin": 0.0, "adm": 0.0,
            "heredado": True, "periodo_origen": "2026-08"}
    _r, fila = _correr(app, fake_db, proy)
    assert "2026-08" in fila["ayuda"]
