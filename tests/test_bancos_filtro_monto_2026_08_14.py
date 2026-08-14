"""El filtro **Monto** del libro del banco busca igual que el de cheques.

TMT 2026-08-14 (dueña): *"hacé que monto se busque igual que en cheques, no
el exacto"* y, sobre el signo, *"si ponen − sólo menos"*.

Hasta hoy `/bancos/<n>?monto=500` hacía `t.importe = 500` clavado. Dos cosas
lo volvían inútil:

1. **Los centavos.** Casi ningún movimiento del banco es un dólar redondo;
   tipear `500` para buscar el débito de `500,32` no traía NADA, y la pantalla
   no dice por qué. La casa ya resolvió esto en facturas (2026-07-12) y en
   cheques: entero = el DÓLAR ENTERO, con centavos = exacto. Acá se reusa esa
   misma regla, ahora en `parsers.monto_rango`, para que las tres pantallas no
   puedan volver a divergir.
2. **El signo.** En `transacciones_bancarias` el importe es SIGNADO: los
   débitos van en negativo. Con match exacto, buscar el cheque de 500 que
   SALIÓ exigía tipear `-500` — y nadie lo hace. Ahora sin signo se compara
   contra el ABSOLUTO (entró o salió, da igual) y el menos adelante queda como
   el filtro explícito de salidas.

Lo que NO cambia: el saldo del banco (plata real, no depende del filtro) y el
resto de los filtros.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bancos import queries as bq  # noqa: E402
from parsers import monto_rango  # noqa: E402

NO_BANCO = 10          # Pichincha


def _entra(valor: str, rango) -> bool:
    lo, hi, _ = rango
    return lo <= Decimal(valor) <= hi


# ── 1) la regla de la casa ───────────────────────────────────────────────────

def test_un_entero_trae_el_DOLAR_ENTERO():
    """El caso que motivó el pedido: tipear 500 tiene que encontrar 500,32."""
    r = monto_rango("500")
    assert (r[0], r[1]) == (Decimal("500"), Decimal("500.999999"))
    assert _entra("500.00", r)
    assert _entra("500.32", r)
    assert _entra("500.99", r)
    assert not _entra("501.00", r)
    assert not _entra("499.99", r)


def test_con_centavos_sigue_siendo_EXACTO():
    """Quien escribe los centavos sabe lo que busca: no se le ensancha."""
    r = monto_rango("500,51")
    assert _entra("500.51", r)
    assert not _entra("500.52", r)
    assert not _entra("500.50", r)


def test_los_miles_estilo_ecuador_no_se_rompen():
    """1.234,56 es mil doscientos treinta y cuatro con 56, no 1,23456."""
    r = monto_rango("1.234,56")
    assert _entra("1234.56", r)
    assert not _entra("1.23", r)


def test_el_menos_adelante_pide_SOLO_SALIDAS():
    lo, hi, negativo = monto_rango("-500")
    assert negativo is True
    # el rango se sigue expresando en positivo: el SQL lo da vuelta
    assert (lo, hi) == (Decimal("500"), Decimal("500.999999"))


def test_sin_signo_no_pide_lado():
    assert monto_rango("500")[2] is False


@pytest.mark.parametrize("crudo", [None, "", "   ", "abc", "-", ","])
def test_lo_que_no_es_un_monto_no_filtra(crudo):
    """Basura en el campo = sin filtro. Nunca un 500 ni una lista vacía."""
    assert monto_rango(crudo) is None


def test_es_LA_MISMA_regla_que_cheques_y_facturas():
    """Las bandas son EXACTAMENTE las que cheques y facturas tienen escritas a
    mano desde 2026-07-12 (+0.999999 el entero, ±0.005 los centavos). Si
    alguien las mueve en una pantalla sola, falla acá."""
    entero = monto_rango("500")
    centavos = monto_rango("500,51")
    assert entero[1] - entero[0] == Decimal("0.999999")
    assert centavos[1] - centavos[0] == Decimal("0.01")


# ── 2) el SQL de la lista ────────────────────────────────────────────────────

class _CapturaSQL:
    """`movimientos()` hace varias consultas (conciliación, cheques del
    depósito, nombres de proveedor): hay que quedarse con la que filtra."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def fetch_all(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params or {}))
        return []

    def fetch_one(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params or {}))
        return {}

    def principal(self) -> tuple[str, dict]:
        for sql, params in self.queries:
            if isinstance(params, dict) and "monto_min" in params:
                return sql, params
        raise AssertionError(
            "ninguna consulta recibió monto_min; se hicieron: "
            + " | ".join(q[:70] for q, _ in self.queries))


@pytest.fixture
def cap(monkeypatch):
    c = _CapturaSQL()
    monkeypatch.setattr(bq, "db", c)
    return c


def test_el_where_compara_por_RANGO_y_no_por_igual(cap):
    bq.movimientos(NO_BANCO, monto_min=500.0, monto_max=500.999999)
    sql, params = cap.principal()
    assert "BETWEEN %(monto_min)s::numeric AND %(monto_max)s::numeric" in sql
    assert "t.importe = %(monto)s::numeric" not in sql   # el viejo, muerto
    assert params["monto_min"] == 500.0


def test_sin_signo_el_where_usa_el_ABSOLUTO(cap):
    """El débito de −500 tiene que aparecer buscando 500."""
    bq.movimientos(NO_BANCO, monto_min=500.0, monto_max=500.999999)
    sql, params = cap.principal()
    assert "ABS(t.importe)" in sql
    assert params["monto_neg"] is False


def test_con_menos_el_where_da_vuelta_el_importe(cap):
    bq.movimientos(NO_BANCO, monto_min=500.0, monto_max=500.999999,
                   monto_negativo=True)
    sql, params = cap.principal()
    assert "THEN -t.importe" in sql
    assert params["monto_neg"] is True


def test_sin_monto_no_hay_filtro(cap):
    bq.movimientos(NO_BANCO, desde="2026-08-01")
    _, params = cap.principal()
    assert params["monto_min"] is None
    assert params["monto_max"] is None


def test_el_filtro_va_ANTES_del_tope_de_500(cap):
    """Mismo motivo que el filtro por id: recortar en Python después del
    LIMIT 500 dejaría afuera los movimientos viejos."""
    bq.movimientos(NO_BANCO, monto_min=500.0, monto_max=500.999999)
    sql, _ = cap.principal()
    assert sql.index("%(monto_min)s::numeric") < sql.index("LIMIT %(limite)s")


# ── 3) la pantalla ───────────────────────────────────────────────────────────

class _FakeAgregados:
    def __init__(self):
        self.agg: tuple[str, dict] | None = None

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "AS absoluta" in s:
            self.agg = (s, params or {})
            return {"n": 1, "signed": -500.32, "absoluta": 500.32}
        if "banco_conciliacion_match" in s:
            return {"n": 0, "signed": 0.0}
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        return 0


@pytest.fixture
def pantalla(app, monkeypatch):
    import db as _db
    from modules.bancos import views as bv

    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    vistos: dict = {}
    fake = _FakeAgregados()

    def fake_movimientos(no_banco, desde=None, hasta=None, **kw):
        vistos.clear()
        vistos.update(kw)
        return []

    monkeypatch.setattr(bv.queries, "movimientos", fake_movimientos)
    monkeypatch.setattr(bv.queries, "banco_info", lambda nb: {
        "no_banco": nb, "nombre": "PICHINCHA", "saldo_stored": 1_910_976.08})
    monkeypatch.setattr(_db, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(_db, "fetch_all", fake.fetch_all)
    return app.test_client(), vistos, fake


def test_la_pantalla_manda_el_rango_no_el_exacto(pantalla):
    client, vistos, _ = pantalla
    r = client.get(f"/bancos/{NO_BANCO}?monto=500")
    assert r.status_code == 200
    assert vistos["monto_min"] == 500.0
    assert vistos["monto_max"] == pytest.approx(500.999999)
    assert vistos["monto_negativo"] is False
    assert "monto" not in vistos          # el kwarg viejo ya no existe


def test_la_pantalla_pasa_el_menos(pantalla):
    client, vistos, _ = pantalla
    r = client.get(f"/bancos/{NO_BANCO}?monto=-500")
    assert r.status_code == 200
    assert vistos["monto_negativo"] is True


def test_el_encabezado_cuenta_CON_EL_MISMO_rango(pantalla):
    """"Mostrando N de M" y "Total filtrado" salen de un agregado aparte: si
    ese quedaba en `=` exacto, la pantalla decía 0 arriba de filas visibles."""
    client, _, fake = pantalla
    client.get(f"/bancos/{NO_BANCO}?monto=500")
    sql_agg, params_agg = fake.agg
    assert "BETWEEN %(monto_min)s::numeric AND %(monto_max)s::numeric" in sql_agg
    assert params_agg["monto_min"] == 500.0
    assert params_agg["monto_neg"] is False


def test_un_monto_ilegible_no_rompe_la_pantalla(pantalla):
    client, vistos, _ = pantalla
    r = client.get(f"/bancos/{NO_BANCO}?monto=hola")
    assert r.status_code == 200
    assert vistos["monto_min"] is None
