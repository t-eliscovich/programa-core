"""La corrida de retenciones deja rastro donde ella mira: historial y campanita.

TMT 2026-08-07 (dueña): *"poner en el historial cuando se cargan x retenciones
por x valor, y lo mismo con abono manual"* y *"también con las retenciones
pongámoslo en campanita/notificaciones, cortito y al pie"*.
"""
from __future__ import annotations

import contextlib

import pytest


class _Stub:
    """Base de mentira: guarda lo que se escribe y contesta lo mínimo."""

    def __init__(self, facturas):
        self.facturas = facturas
        self.mov_dobles: list[dict] = []
        self.avisos: list[dict] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura" in s:
            num = (params or [None])[0]
            return dict(self.facturas.get(num) or {}) or None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "insert into scintela.retencion" in s:
            return {"id_retencion": 1}
        if "insert into scintela.aviso" in s:
            self.avisos.append({"fuente": params[0], "titulo": params[2],
                                "detalle": params[3], "importe": params[4],
                                "cantidad": params[5], "clave": params[7]})
            return {"id_aviso": 1}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _fac(numf, sri):
    return {"id_factura": numf, "codigo_cli": "DEM", "numf": numf,
            "numf_completo": sri, "importe": 1000.0, "abono": 0.0,
            "retencion": 0.0, "saldo": 1000.0, "stat": "Z"}


@pytest.fixture
def corrida(monkeypatch):
    """Aplica 3 retenciones de Asinfo y devuelve (resultado, stub)."""
    import db
    from modules.asinfo import service as asinfo
    from modules.retenciones import queries as q

    numeros = [f"001-099-00000{n}" for n in (1001, 1002, 1003)]
    stub = _Stub({sri: _fac(1000 + i, sri) for i, sri in enumerate(numeros)})
    for attr in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, attr, getattr(stub, attr))
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None, raising=False)
    monkeypatch.setattr(q._md, "registrar",
                        lambda **k: stub.mov_dobles.append(k) or {"id_mov_doble": 1})
    monkeypatch.setattr(asinfo, "retenciones_periodo",
                        lambda d, h: {sri: {"ret_total": 40.0} for sri in numeros})
    res = q.aplicar_retenciones_asinfo("2026-08-01", "2026-08-07", usuario="cron")
    return res, stub


def test_toda_la_corrida_comparte_un_batch(corrida):
    """Las 3 quedan bajo UN batch_id → el historial las agrupa en una tarjeta
    ("3 retenciones · $120,00") en vez de tres renglones sueltos."""
    res, stub = corrida
    assert res["n_aplicadas"] == 3
    batches = {m.get("batch_id") for m in stub.mov_dobles}
    assert len(batches) == 1, f"esperaba un solo batch, hay {len(batches)}"
    assert next(iter(batches)), "el batch_id no puede ser None"


def test_dos_corridas_no_comparten_batch(monkeypatch, corrida):
    """La del mediodía y la de la tarde son operaciones distintas."""
    res, stub = corrida
    from modules.retenciones import queries as q
    primera = {m.get("batch_id") for m in stub.mov_dobles}
    stub.facturas = {sri: _fac(2000 + i, sri)
                     for i, sri in enumerate(stub.facturas)}
    stub.mov_dobles.clear()
    q.aplicar_retenciones_asinfo("2026-08-01", "2026-08-07", usuario="cron")
    segunda = {m.get("batch_id") for m in stub.mov_dobles}
    assert primera.isdisjoint(segunda)


def test_el_concepto_nombra_la_factura_por_su_numero(corrida):
    """No por el id interno: ella busca 176100, no #106."""
    _, stub = corrida
    conceptos = [m["concepto"] for m in stub.mov_dobles]
    assert conceptos and all(c.startswith("Retención $ 40,00 a la factura 10")
                             for c in conceptos), conceptos
    # formato de acá: punto = miles, coma = decimales
    assert "$ 1.000,00" in conceptos[0], conceptos[0]


def test_el_aviso_es_corto_y_dice_cuantas_y_cuanto(corrida):
    """*"cortito y al pie, se cargaron x retenciones por x monto y ya"*."""
    res, stub = corrida
    assert len(stub.avisos) == 1
    av = stub.avisos[0]
    assert av["titulo"] == "Se cargaron 3 retenciones por $ 120,00"
    assert av["detalle"] is None, "el aviso no lleva renglón de explicación"
    assert av["fuente"] == "retenciones"
    assert av["cantidad"] == 3 and float(av["importe"]) == 120.0


def test_la_clave_del_aviso_cuelga_del_batch_no_del_dia(corrida):
    """El cron pasa 11:00 y 16:00: con clave diaria el segundo aviso se lo
    comía el ON CONFLICT y las retenciones de la tarde no avisaban nunca."""
    _, stub = corrida
    clave = stub.avisos[0]["clave"]
    batch = next(iter({m.get("batch_id") for m in stub.mov_dobles}))
    assert clave.endswith(batch), clave


def test_sin_retenciones_aplicadas_no_hay_aviso(monkeypatch):
    """El cron corre todos los días; si no aplicó nada, no ensucia la campanita."""
    import db
    from modules.asinfo import service as asinfo
    from modules.retenciones import queries as q
    stub = _Stub({})
    for attr in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, attr, getattr(stub, attr))
    monkeypatch.setattr(asinfo, "retenciones_periodo", lambda d, h: {})
    res = q.aplicar_retenciones_asinfo("2026-08-01", "2026-08-07")
    assert res["n_aplicadas"] == 0
    assert stub.avisos == []


def test_retenciones_tiene_nombre_en_el_buzon():
    """Sin la entrada en FUENTES el buzón muestra la clave cruda y el filtro
    por fuente no lista las retenciones."""
    from modules.avisos.queries import FUENTES
    assert FUENTES.get("retenciones") == "Retenciones"


# ── el conteo de la tarjeta sale del batch entero, no de la página ─────────

def test_resumen_batches_agrupa_por_tipo(monkeypatch):
    import db
    from modules.historial import queries as hq
    filas = [
        {"batch_id": "b1", "tipo": "retencion_asinfo_aplicada", "n": 14, "total": 140.0},
        {"batch_id": "b2", "tipo": "cheque_creado", "n": 2, "total": 4000.0},
        {"batch_id": "b2", "tipo": "cheque_aplicado_a_factura", "n": 3, "total": 4000.0},
    ]
    monkeypatch.setattr(db, "fetch_all", lambda sql, params=None: filas)
    out = hq.resumen_batches(["b1", "b2"])
    assert out["b1"]["por_tipo"]["retencion_asinfo_aplicada"] == (14, 140.0)
    assert out["b2"]["n"] == 5          # todas las patas
    assert out["b2"]["por_tipo"]["cheque_creado"] == (2, 4000.0)


def test_resumen_batches_sin_ids_no_consulta(monkeypatch):
    import db
    from modules.historial import queries as hq

    def _boom(*a, **k):
        raise AssertionError("no debería consultar sin batch_ids")
    monkeypatch.setattr(db, "fetch_all", _boom)
    assert hq.resumen_batches([]) == {}
    assert hq.resumen_batches(None) == {}
