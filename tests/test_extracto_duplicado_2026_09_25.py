"""TMT 2026-09-25 — Alex: el extracto del 24-25/09 quedó DOS veces en la sesión.

Dos caminos lo hacían:
1. Dos subidas a la vez (doble click en "Procesar →"): las dos miraban la
   sesión antes de que la otra guardara y las dos agregaban las filas.
2. Borrar con la ✕, volver a subir el archivo y después "Restaurar borradas":
   la borrada volvía encima de la que había entrado de nuevo.
"""
import json
from datetime import date
from decimal import Decimal

from modules.conciliacion import sesion as _sesion
from modules.conciliacion.parser_banco import MovBanco


def _mov(documento, monto="18620.93", tipo="D", fecha=date(2026, 9, 25)):
    return MovBanco(
        fecha=fecha, concepto=f"PAGO SENAE {documento}", documento=documento,
        monto=Decimal(monto), tipo=tipo, oficina="AG.", saldo=None, codigo="",
    )


class _Base:
    """Una fila de sesión con compare-and-swap real sobre el payload."""

    def __init__(self, payload=None):
        self.payload = json.dumps(payload or [])
        self.antes_de_guardar = None  # hook: simula otra subida en el medio

    def fila(self):
        return {"id": 7, "no_banco": 10, "cerrada_en": None,
                "extracto_nombre": "x.xlsx",
                "extracto_payload": json.loads(self.payload)}

    def execute(self, sql, params=None, conn=None):
        if self.antes_de_guardar:
            hook, self.antes_de_guardar = self.antes_de_guardar, None
            hook()
        if "AND COALESCE(extracto_payload" in sql:
            nuevo, _nombre, _hash, _id, viejo = params
            if json.loads(viejo) != json.loads(self.payload):
                return 0
            self.payload = nuevo
            return 1
        self.payload = params[0]
        return 1

    def filas_vivas(self):
        return [d for d in json.loads(self.payload) if not d.get("_borrado")]


def _enchufar(monkeypatch, base):
    monkeypatch.setattr(_sesion, "sesion_abierta", lambda no_banco: base.fila())
    monkeypatch.setattr(_sesion, "sesion_por_id", lambda sid: base.fila())
    monkeypatch.setattr(_sesion.db, "execute", base.execute)
    monkeypatch.setattr(_sesion.db, "fetch_one", lambda sql, params=None, conn=None: base.fila())


def test_dos_subidas_a_la_vez_no_duplican(monkeypatch):
    base = _Base()
    _enchufar(monkeypatch, base)
    # Las dos subidas miran la sesión cuando todavía está vacía.
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas", lambda no_banco: set())
    archivo = [_mov("26753953"), _mov("26754216", "15064.76")]

    # Mientras la segunda está por guardar, la primera termina de guardar.
    def primera_subida_guarda():
        base.payload = json.dumps([_sesion._mov_to_dict(m) for m in archivo])
    base.antes_de_guardar = primera_subida_guarda

    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="alex", movs=archivo, extracto_nombre="x.xlsx")

    assert sid == 7
    assert n_added == 0
    assert n_skipped == 2
    assert len(base.filas_vivas()) == 2, "cada movimiento una sola vez"


def test_subida_normal_sigue_agregando(monkeypatch):
    base = _Base([_sesion._mov_to_dict(_mov("111"))])
    _enchufar(monkeypatch, base)
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas", lambda no_banco: set())

    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="alex",
        movs=[_mov("111"), _mov("222", "40000", "C")], extracto_nombre="x.xlsx")

    assert (n_added, n_skipped) == (1, 1)
    assert sorted(d["documento"] for d in base.filas_vivas()) == ["111", "222"]


def test_si_no_logra_guardar_avisa_en_vez_de_pisar(monkeypatch):
    base = _Base()
    _enchufar(monkeypatch, base)
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas", lambda no_banco: set())
    monkeypatch.setattr(_sesion.db, "execute", lambda *a, **k: 0)
    try:
        _sesion.crear_sesion(no_banco=10, usuario="alex",
                             movs=[_mov("1")], extracto_nombre="x.xlsx")
    except RuntimeError as e:
        assert "Volvé a subirlo" in str(e)
    else:
        raise AssertionError("tenía que avisar")


def test_restaurar_no_duplica_lo_que_se_volvio_a_subir(monkeypatch):
    viva = _sesion._mov_to_dict(_mov("26753953"))
    borrada = {**viva, "_borrado": True}
    otra_borrada = {**_sesion._mov_to_dict(_mov("999", "159.94", "C")), "_borrado": True}
    base = _Base([borrada, otra_borrada, viva])
    _enchufar(monkeypatch, base)

    n, ya = _sesion.restaurar_movs_extracto(7)

    assert (n, ya) == (1, 1)
    docs = sorted(d["documento"] for d in base.filas_vivas())
    assert docs == ["26753953", "999"], "la 26753953 queda UNA vez"


def test_restaurar_dos_iguales_que_el_banco_trajo_dos_veces(monkeypatch):
    """Si el archivo trae la fila dos veces de verdad y se borraron las dos,
    restaurar devuelve las dos (no hay ninguna viva que las repita)."""
    fila = _sesion._mov_to_dict(_mov("34146049", "255", "C"))
    base = _Base([{**fila, "_borrado": True}, {**fila, "_borrado": True}])
    _enchufar(monkeypatch, base)

    n, ya = _sesion.restaurar_movs_extracto(7)

    assert (n, ya) == (2, 0)
    assert len(base.filas_vivas()) == 2


def test_firma_payload_igual_a_la_del_mov():
    m = _mov("26753953")
    assert _sesion.firma_payload(_sesion._mov_to_dict(m)) == _sesion.firma_mov(m).upper()


def test_si_la_sesion_se_cerro_en_el_medio_avisa(monkeypatch):
    base = _Base()
    _enchufar(monkeypatch, base)
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas", lambda no_banco: set())
    monkeypatch.setattr(_sesion.db, "execute", lambda *a, **k: 0)
    monkeypatch.setattr(_sesion, "sesion_por_id",
                        lambda sid: {**base.fila(), "cerrada_en": "2026-09-25"})
    try:
        _sesion.crear_sesion(no_banco=10, usuario="alex",
                             movs=[_mov("1")], extracto_nombre="x.xlsx")
    except RuntimeError as e:
        assert "se cerró" in str(e)
    else:
        raise AssertionError("tenía que avisar")


def test_restaurar_sin_sesion_no_hace_nada(monkeypatch):
    monkeypatch.setattr(_sesion.db, "fetch_one", lambda *a, **k: None)
    assert _sesion.restaurar_movs_extracto(0) == (0, 0)
    assert _sesion.restaurar_movs_extracto(7) == (0, 0)
