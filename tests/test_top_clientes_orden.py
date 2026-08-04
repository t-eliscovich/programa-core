"""El "Top 10 clientes" del estado de cuenta tiene que estar BIEN ordenado.

Reporte dueña 2026-08-04 (screenshot del panel): el último renglón rompía la
escalera — PUE 66.321,46 aparecía DEBAJO de PGQ 65.096,53.

CAUSA: `cartera_por_cliente()` ordenaba por `pct`, que está **redondeado a 1
decimal**. Dos clientes con saldos distintos caen en el mismo "1,2%", y el
desempate quedaba en el orden arbitrario que devolvió el SQL (sin ORDER BY).
No es cosmético: con 10 renglones cortados de una lista mal ordenada, el
décimo puede no ser el décimo.
"""
from __future__ import annotations

from unittest.mock import patch

from modules.informes import queries as iq


def _filas(saldos: list[tuple[str, float]]) -> list[dict]:
    return [
        {"codigo_cli": cod, "nombre": cod, "facturas": s, "cheques": 0,
         "saldo_total": s, "n_facturas": 1}
        for cod, s in saldos
    ]


def test_ordena_por_saldo_aunque_el_porcentaje_empate():
    """El caso del screenshot: dos saldos distintos con el MISMO pct."""
    # Con un total grande, 65.096,53 y 66.321,46 redondean al mismo 1,2%.
    relleno = [("BIG", 5_000_000.0)]
    filas = _filas(relleno + [("PGQ", 65_096.53), ("PUE", 66_321.46)])
    with patch.object(iq.db, "fetch_all", return_value=filas):
        orden = [r["codigo_cli"] for r in iq.cartera_por_cliente()]
    assert orden.index("PUE") < orden.index("PGQ"), (
        f"66.321,46 tiene que ir ARRIBA de 65.096,53 — quedó {orden}"
    )


def test_el_pct_empatado_era_el_que_rompia_el_orden():
    """Blindaje del diagnóstico: si los dos pct no empataran, el test de
    arriba pasaría aun con el bug puesto."""
    filas = _filas([("BIG", 5_000_000.0), ("PGQ", 65_096.53), ("PUE", 66_321.46)])
    with patch.object(iq.db, "fetch_all", return_value=filas):
        por_cod = {r["codigo_cli"]: r for r in iq.cartera_por_cliente()}
    assert por_cod["PUE"]["pct"] == por_cod["PGQ"]["pct"], (
        "el escenario perdió sentido: los pct ya no empatan"
    )


def test_la_lista_entera_queda_en_escalera():
    filas = _filas([("A", 10.0), ("B", 300.0), ("C", 25.5), ("D", 300.0),
                    ("E", 0.5), ("F", -40.0)])
    with patch.object(iq.db, "fetch_all", return_value=filas):
        saldos = [float(r["saldo_total"]) for r in iq.cartera_por_cliente()]
    assert saldos == sorted(saldos, reverse=True), saldos


def test_sin_filas_no_explota():
    with patch.object(iq.db, "fetch_all", return_value=[]):
        assert iq.cartera_por_cliente() == []
