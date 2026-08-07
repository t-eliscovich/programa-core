"""El agujero por el que se perdían retenciones, y el freno del barrido.

TMT 2026-08-07 (dueña: *"lo que me parece más importante que estos $1.229: el
cron se está perdiendo retenciones por diseño"*).

🚨 EL AGUJERO. La consulta a Asinfo filtra por la fecha de la **FACTURA**, no
por la del cobro — medido pidiendo el 1–10/05: vuelven sólo facturas del 4 al
8/05, ninguna de afuera. Como el cron pide siempre "los últimos 60 días", una
retención que Asinfo registre hoy contra una factura vieja no la mira nadie
NUNCA. Así quedaron huérfanas 23 retenciones de febrero a mayo.

⚠️ Y el arreglo tiene su propia trampa: en las facturas de la época del dBase
la retención puede estar ya sumada adentro del abono, sin fila en
`scintela.retencion` — el guard `ya` no la ve. Barrer hacia atrás sin freno le
descontaría el saldo DOS veces al cliente, en silencio. De ahí
`solo_sin_abono`.
"""
from __future__ import annotations

import contextlib
from datetime import date

import pytest

# ── el freno: no tocar una factura que ya tiene abono ──────────────────────

class _Stub:
    def __init__(self, factura):
        self.factura = factura
        self.updates = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura" in s:
            return dict(self.factura)
        return None  # sin retención registrada: el guard `ya` no frena

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        if "update scintela.factura" in " ".join(sql.split()).lower():
            self.updates.append(tuple(params))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        return {"id_retencion": 1}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _fac(abono):
    return {"id_factura": 7, "codigo_cli": "TNZ", "numf": 173868,
            "numf_completo": "001-099-000173868", "fecha": date(2026, 4, 23),
            "importe": 9980.0, "abono": abono, "retencion": 0.0,
            "saldo": 9980.0 - abono, "stat": "Z"}


@pytest.fixture
def stub(monkeypatch):
    import db
    from modules.retenciones import queries as q
    s = _Stub(_fac(0.0))
    for attr in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, attr, getattr(s, attr))
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None, raising=False)
    monkeypatch.setattr(q._md, "registrar", lambda **k: {"id_mov_doble": 1})
    return s


def test_con_abono_cero_el_barrido_aplica(stub):
    """Sin abono no hay nada adentro que pueda estar duplicado."""
    from modules.retenciones import queries as q
    r = q._aplicar_una_por_numero("001-099-000173868", 173.57, "cron",
                                  solo_sin_abono=True)
    assert r == "aplicada"
    assert stub.updates, "tenía que descontar el saldo"


def test_con_abono_el_barrido_NO_toca(stub):
    """El abono puede tener la retención adentro (dBase): descontarla otra vez
    le sacaría al cliente dos veces la misma plata, sin que nadie lo vea."""
    from modules.retenciones import queries as q
    stub.factura = _fac(173.57)          # justo el importe de la retención
    r = q._aplicar_una_por_numero("001-099-000173868", 173.57, "cron",
                                  solo_sin_abono=True)
    assert r == "tiene_abono"
    assert not stub.updates, "no puede tocar una factura con abono"


def test_sin_el_freno_sigue_aplicando_como_siempre(stub):
    """La aplicación normal (a mano o de los últimos 60 días) no cambia."""
    from modules.retenciones import queries as q
    stub.factura = _fac(173.57)
    r = q._aplicar_una_por_numero("001-099-000173868", 173.57, "web")
    assert r == "aplicada"


# ── el barrido elige UN mes viejo por día, sin guardar estado ───────────────

def _mes(hoy):
    from modules.admin_dbase.health_audit_view import _mes_del_barrido
    return _mes_del_barrido(hoy, 60)


def test_cada_dia_le_toca_un_mes_distinto():
    """Rota por el día del mes: no depende de que ayer haya corrido, ni de
    guardar en ningún lado por dónde iba."""
    meses = set()
    for dia in range(1, 29):
        r = _mes(date(2026, 8, dia))
        if r:
            meses.add(r[0].strftime("%Y-%m"))
    assert len(meses) >= 8, f"barre poco: {sorted(meses)}"


def test_no_vuelve_a_pedir_lo_que_ya_cubre_la_ventana_de_60_dias():
    """Pedirle a Asinfo dos veces el mismo rango en la misma corrida es puro
    costo: la consulta tarda 10-30 s y un mes entero llegó a colgar la app."""
    from datetime import timedelta
    hoy = date(2026, 8, 7)
    piso = hoy - timedelta(days=60)
    for dia in range(1, 29):
        r = _mes(date(2026, 8, dia))
        if r:
            assert r[1] < piso, f"el mes {r} pisa la ventana de 60 días"


def test_el_barrido_llega_a_los_meses_que_quedaron_huerfanos():
    """Febrero a mayo de 2026 — los que nadie miraba."""
    meses = {r[0].strftime("%Y-%m") for dia in range(1, 29)
             if (r := _mes(date(2026, 8, dia)))}
    for m in ("2026-02", "2026-03", "2026-04", "2026-05"):
        assert m in meses, f"{m} no entra en el barrido: {sorted(meses)}"


def test_el_barrido_va_siempre_con_el_freno_puesto(monkeypatch):
    """Si alguien le saca `solo_sin_abono`, el barrido pasa a poder descontar
    dos veces. Que se entere acá."""
    from modules.admin_dbase import health_audit_view as h
    visto = {}

    def _fake(desde, hasta, usuario="web", solo_sin_abono=False):
        visto.update(desde=desde, hasta=hasta, solo_sin_abono=solo_sin_abono)
        return {"n_aplicadas": 0}
    import modules.retenciones.queries as rq
    monkeypatch.setattr(rq, "aplicar_retenciones_asinfo", _fake)
    out = h._barrer_un_mes_viejo(date(2026, 8, 7), 60)
    assert out["ok"] is True
    assert visto["solo_sin_abono"] is True
