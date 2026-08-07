"""Dry run de las retenciones viejas que quedaron adentro del abono.

TMT 2026-08-07. La dueña, mirando el resultado de la migración 0179: *"todas
las retenciones están en abono"*. Y es cierto para lo anterior a junio: mientras
el dBase fue la fuente de verdad, RETENCIO.PRG sumaba la retención al abono y
Programa Core nunca supo que ese pedazo del abono era una retención.

🚨 Lo que se protege acá es que NO se confunda con aplicar una retención nueva.
Aplicar DESCUENTA del saldo; estas ya están descontadas. Si alguien reusa el
aplicador para esto, cada cliente queda debiendo de menos dos veces. La única
operación válida es MOVER: abono −= rete, retencion += rete, saldo IGUAL.
"""
from __future__ import annotations

import pytest


def _fac(numf, sri, importe, abono, retencion=0.0, saldo=None):
    return {"id_factura": numf, "codigo_cli": "DEM", "numf": numf,
            "numf_completo": sri, "fecha": None, "importe": importe,
            "abono": abono, "retencion": retencion,
            "saldo": importe - abono - retencion if saldo is None else saldo,
            "stat": "A"}


@pytest.fixture
def escenario(monkeypatch):
    """4 retenciones de Asinfo, una de cada caso."""
    from modules.asinfo import service as asinfo
    from modules.retenciones import queries as q

    sri = lambda n: f"001-099-{n:09d}"  # noqa: E731
    facturas = {
        # el abono es EXACTAMENTE la retención → se mueve entero
        sri(1001): _fac(1001, sri(1001), 1000.0, 30.44),
        # el abono tiene un pago real ADEMÁS de la retención → se mueve la parte
        sri(1002): _fac(1002, sri(1002), 2000.0, 500.00),
        # ya separada por la mig 0179
        sri(1003): _fac(1003, sri(1003), 800.0, 0.0, retencion=24.35),
        # el abono no llega a cubrir la retención de Asinfo
        sri(1004): _fac(1004, sri(1004), 500.0, 5.00),
        # ya cancelada: saldo 0 (la dueña: "no la toques")
        sri(1005): _fac(1005, sri(1005), 900.0, 900.0, saldo=0.0),
        # totalizada a mano: stat T aunque la resta no dé 0 (drift del dBase)
        sri(1006): dict(_fac(1006, sri(1006), 700.0, 21.31, saldo=0.0), stat="T"),
    }
    retes = {sri(1001): {"ret_total": 30.44}, sri(1002): {"ret_total": 60.88},
             sri(1003): {"ret_total": 24.35}, sri(1004): {"ret_total": 15.22},
             sri(1005): {"ret_total": 27.40}, sri(1006): {"ret_total": 21.31},
             sri(9999): {"ret_total": 12.00}}  # no existe en PC

    monkeypatch.setattr(asinfo, "retenciones_periodo", lambda d, h: retes)
    monkeypatch.setattr(
        q, "_match_asinfo_a_facturas",
        lambda numeros: (
            {n: facturas[n] for n in numeros if n in facturas},
            set(),
            {"DEM|1003"},                      # 1003 ya tiene fila de retención
            {"DEM": "DEMO TEXTIL"},
        ))
    return q.preview_retencion_en_abono("2026-01-01", "2026-05-31"), facturas


def _por_numf(out, numf):
    return next(f for f in out["filas"] if f["numf"] == numf)


def test_el_saldo_no_se_mueve_en_ninguna_fila(escenario):
    """El invariante que hace que esto sea seguro: mover no es descontar."""
    out, facturas = escenario
    for fila in out["filas"]:
        if fila["estado"] != "se_mueve":
            continue
        f = next(x for x in facturas.values() if x["numf"] == fila["numf"])
        # el saldo que muestra es el actual, y la cuenta sigue cerrando con los
        # valores NUEVOS: importe − abono_nuevo − retención_nueva == saldo
        assert fila["saldo"] == f["saldo"]
        assert abs(f["importe"] - fila["abono_nuevo"]
                   - fila["retencion_nueva"] - fila["saldo"]) < 0.005


def test_abono_que_era_solo_retencion_queda_en_cero(escenario):
    out, _ = escenario
    fila = _por_numf(out, 1001)
    assert fila["estado"] == "se_mueve"
    assert fila["abono_actual"] == 30.44
    assert fila["abono_nuevo"] == 0.0
    assert fila["retencion_nueva"] == 30.44


def test_abono_con_pago_real_conserva_el_pago(escenario):
    """500 abonados de los cuales 60,88 eran retención → quedan 439,12 de pago."""
    out, _ = escenario
    fila = _por_numf(out, 1002)
    assert fila["estado"] == "se_mueve"
    assert fila["abono_nuevo"] == 439.12
    assert fila["retencion_nueva"] == 60.88


def test_la_que_ya_esta_separada_no_se_vuelve_a_tocar(escenario):
    """Si se moviera de nuevo, el cliente quedaría debiendo de menos dos veces."""
    out, _ = escenario
    fila = _por_numf(out, 1003)
    assert fila["estado"] == "ya_separada"
    assert fila["abono_nuevo"] is None and fila["retencion_nueva"] is None


def test_abono_mas_chico_que_la_retencion_va_a_mano(escenario):
    """Mover dejaría el abono negativo: no se decide solo."""
    out, _ = escenario
    fila = _por_numf(out, 1004)
    assert fila["estado"] == "abono_corto"
    assert fila["abono_nuevo"] is None


def test_la_que_no_esta_en_pc_no_rompe(escenario):
    out, _ = escenario
    assert out["resumen"]["sin_factura"] == 1


def test_una_factura_cerrada_no_se_toca(escenario):
    """TMT 2026-08-07 (dueña): *"si la factura ya fue totalizada o cancelada no
    la toques"*. Y no se pierde nada: las totalizadas ni siquiera se listan en
    el estado de cuenta, y con saldo 0 la cuenta cierra igual antes y después
    — el movimiento no se vería en ningún lado."""
    out, _ = escenario
    for numf in (1005, 1006):
        fila = _por_numf(out, numf)
        assert fila["estado"] == "cerrada", numf
        assert fila["abono_nuevo"] is None and fila["retencion_nueva"] is None
    assert out["resumen"]["cerrada"] == 2
    assert out["resumen"]["total_cerradas"] == round(27.40 + 21.31, 2)


def test_la_cerrada_no_entra_en_la_plata_a_mover(escenario):
    """Si entrara, el número de arriba prometería más de lo que va a pasar."""
    out, _ = escenario
    assert out["resumen"]["total_a_mover"] == round(30.44 + 60.88, 2)


def test_el_resumen_cuenta_y_suma_lo_que_se_moveria(escenario):
    out, _ = escenario
    r = out["resumen"]
    assert r["se_mueve"] == 2
    assert r["total_a_mover"] == round(30.44 + 60.88, 2)
    assert r["ya_separada"] == 1 and r["abono_corto"] == 1
    assert r["n_total"] == 7


def test_no_escribe_nada(monkeypatch, escenario):
    """Es un preview: si algún día alguien le mete un UPDATE, que falle acá."""
    import db
    from modules.asinfo import service as asinfo
    from modules.retenciones import queries as q

    def _prohibido(*a, **k):
        raise AssertionError("el dry run no puede escribir")
    monkeypatch.setattr(db, "execute", _prohibido)
    monkeypatch.setattr(db, "execute_returning", _prohibido)
    monkeypatch.setattr(asinfo, "retenciones_periodo", lambda d, h: {})
    assert q.preview_retencion_en_abono("2026-01-01", "2026-05-31")["filas"] == []


def test_la_pantalla_no_tiene_boton_de_aplicar():
    """Todavía no se decidió aplicarlo: la pantalla es sólo para mirar."""
    from pathlib import Path
    tpl = Path("modules/facturas/templates/facturas/"
               "retencion_en_abono.html").read_text(encoding="utf-8")
    assert "<form" in tpl and 'method="GET"' in tpl
    assert 'method="post"' not in tpl.lower(), "el dry run no postea nada"
