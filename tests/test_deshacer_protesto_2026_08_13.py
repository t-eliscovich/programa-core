"""Deshacer un protesto marcado por error.

Dueña, 13/08/2026, sobre el cheque #98658 de ADI (2.300,06, Bolivariana):
*"protesté por confusión … y después procedí a reversar la anulación pero veo
que sigue protestado"*. Marcar un cheque devuelto era un camino de ida:
`TRANSICIONES_VALIDAS` no ofrece 1→B —y no debería, volver a "depositado" no es
un paso del negocio— y el ↺ de /historial contestaba *"Tipo 'cheque_devuelto'
aún no tiene reverso automatizado"*. El cheque quedaba como deuda viva de un
cliente que ya había pagado.

Lo que se protege acá:

  · el cheque vuelve al estado del que SALIÓ (el que anotó el protesto), no a
    uno inventado;
  · la plata que movió el protesto (la ND del importe + el `GS. cheq.`) se
    compensa con NC, y NO se compensa dos veces;
  · si alguien tocó el cheque después del protesto, no se restaura nada;
  · los protestos anteriores a este commit —los seis del 12/08, que no anotaron
    qué movieron— se reconocen igual por el concepto de sus ND.
"""

import contextlib
from datetime import date

import pytest


class _DB:
    """Stub de `db` con las tres consultas que el deshacer necesita.

    Guarda los UPDATE/INSERT para poder afirmar sobre ellos. No modela SQL:
    matchea por un pedazo distintivo de cada consulta, que es lo que hace
    fallar al test si alguien cambia la consulta por otra cosa.
    """

    def __init__(self, *, mov, cheque, nds=(), nds_por_id=None):
        self.mov = mov
        self.cheque = dict(cheque)
        self.nds = list(nds)
        self.nds_por_id = list(nds_por_id or [])
        self.executes: list[tuple[str, tuple]] = []
        self.insertados: list[dict] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.mov_doble where id_mov_doble" in s:
            return self.mov
        if "from scintela.cheque where id_cheque" in s and "for update" in s:
            return {"stat": self.cheque["stat"]}
        if "from scintela.cheque where id_cheque" in s:
            return self.cheque
        if "from scintela.chequextransaccion" in s:
            return None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "numreferencia = %s" in s and "ilike 'ch.devuelto" in s:
            return self.nds
        if "tb.id_transaccion = any(%s)" in s:
            return self.nds_por_id
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((" ".join(sql.split()), tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _patch(monkeypatch, stub):
    import db

    monkeypatch.setattr(db, "fetch_one", stub.fetch_one)
    monkeypatch.setattr(db, "fetch_all", stub.fetch_all)
    monkeypatch.setattr(db, "execute", stub.execute)
    monkeypatch.setattr(db, "tx", stub.tx)

    import bank_helpers

    def _fake_insert(conn, **kw):
        stub.insertados.append(kw)
        return {"id_transaccion": 99000 + len(stub.insertados), "saldo_nuevo": 0.0}

    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario", _fake_insert)

    import mov_doble

    stub.movs = []
    monkeypatch.setattr(mov_doble, "registrar",
                        lambda **kw: stub.movs.append(kw) or 1)

    from modules.cheques import queries

    monkeypatch.setattr(queries, "asegurar_fecha_abierta", lambda *_a, **_k: None)


def _mov(meta, *, estado="activo", tipo="cheque_devuelto"):
    return {
        "id_mov_doble": 23520, "tipo": tipo, "estado": estado,
        "origen_id": 98658, "importe": 2300.06, "metadata": meta,
        "fecha_creacion": date(2026, 8, 12),
        "fecha_operacion": date(2026, 8, 12),
    }


def _cheque(**kw):
    base = {
        "id_cheque": 98658, "no_cheque": "", "stat": "1", "codigo_cli": "ADI",
        "importe": 2300.06, "banco": "BOLIVARIA", "no_banco": 37,
        "fecha": date(2026, 6, 28), "fechad": date(2026, 8, 17),
        "fechad_original": date(2026, 6, 29),
        "fecha_postergacion": date(2026, 8, 13),
    }
    base.update(kw)
    return base


META = {"id_cheque": 98658, "stat_prev": "B", "stat_destino": "1",
        "codigo_cli": "ADI"}


def _updates_de_cheque(stub):
    return [(s, p) for s, p in stub.executes
            if s.lower().startswith("update scintela.cheque")]


# ── el caso de la dueña: el protesto que no movió el banco ────────────────
def test_el_cheque_vuelve_al_estado_del_que_salio(monkeypatch):
    from modules.cheques import queries

    stub = _DB(mov=_mov(META), cheque=_cheque())
    _patch(monkeypatch, stub)

    res = queries.deshacer_devuelto(23520, usuario="tamara")

    assert res["stat_restaurado"] == "B", (
        "vuelve al estado que anotó el protesto, no a uno elegido por nosotros")
    ups = _updates_de_cheque(stub)
    assert len(ups) == 1 and ups[0][1][0] == "B"
    assert not stub.insertados, "este protesto no movió el banco: no hay nada que compensar"


def test_un_cheque_que_vuelve_al_banco_recupera_su_fecha_de_deposito(monkeypatch):
    """Se depositó el 29/06; mientras estuvo protestado se lo postergó al 17/08.

    Al volver a 'B' la fecha que vale es la del depósito: la futura sólo tenía
    sentido para un cheque protestado esperando cobrarse.
    """
    from modules.cheques import queries

    stub = _DB(mov=_mov(META), cheque=_cheque())
    _patch(monkeypatch, stub)

    res = queries.deshacer_devuelto(23520)

    assert res["fechad_restaurada"] == date(2026, 6, 29)
    sql, params = _updates_de_cheque(stub)[0]
    assert "fechad=%s" in sql and "fechad_original=null" in sql.lower()
    assert date(2026, 6, 29) in params


def test_si_vuelve_a_cartera_la_fecha_futura_no_se_toca(monkeypatch):
    """Un cheque que vuelve a Z/P/D todavía se va a cobrar: esa fecha es real."""
    from modules.cheques import queries

    stub = _DB(mov=_mov({**META, "stat_prev": "Z"}), cheque=_cheque())
    _patch(monkeypatch, stub)

    res = queries.deshacer_devuelto(23520)

    assert res["fechad_restaurada"] is None
    sql, _ = _updates_de_cheque(stub)[0]
    assert "fechad=%s" not in sql


# ── el caso con plata: la ND y su gasto ───────────────────────────────────
_NDS = [
    {"id_transaccion": 45705, "no_banco": 10, "fecha": date(2026, 8, 12),
     "importe": 2300.06, "concepto": "ch.devuelto 98657 ADI",
     "banco_nombre": "PICHINCHA", "ya_revertido": False},
    {"id_transaccion": 45706, "no_banco": 10, "fecha": date(2026, 8, 12),
     "importe": 2.00, "concepto": "GS. cheq. ADI",
     "banco_nombre": "PICHINCHA", "ya_revertido": False},
]


def test_la_nd_del_protesto_y_su_gasto_se_compensan_con_nc(monkeypatch):
    from modules.cheques import queries

    meta = {**META, "compensacion": {"id_nd": 45705, "id_gs": 45706,
                                     "links": [45700], "no_banco": 10}}
    stub = _DB(mov=_mov(meta), cheque=_cheque(), nds_por_id=_NDS)
    _patch(monkeypatch, stub)

    res = queries.deshacer_devuelto(23520)

    assert len(stub.insertados) == 2
    assert {n["documento"] for n in stub.insertados} == {"NC"}, (
        "la ND no se borra: se cancela con su opuesta, como el resto de los "
        "reversos de la app")
    assert sorted(n["importe"] for n in stub.insertados) == [2.00, 2300.06]
    assert len(res["compensados"]) == 2
    assert res["relinkeados"] == 1, "vuelve adentro del depósito del que salió"


def test_no_se_compensa_dos_veces(monkeypatch):
    """Si la NC ya está, deshacer de nuevo no vuelve a poner la plata."""
    from modules.cheques import queries

    meta = {**META, "compensacion": {"id_nd": 45705, "id_gs": 45706}}
    nds = [{**n, "ya_revertido": True} for n in _NDS]
    stub = _DB(mov=_mov(meta), cheque=_cheque(), nds_por_id=nds)
    _patch(monkeypatch, stub)

    res = queries.deshacer_devuelto(23520)

    assert not stub.insertados
    assert res["compensados"] == []


def test_los_protestos_viejos_se_reconocen_por_el_concepto(monkeypatch):
    """Los seis del 12/08 no anotaron qué movieron — se buscan por su ND.

    Es el fallback, y por eso el plan dice `exacto=False`: la pantalla muestra
    los movimientos con importe y banco para que un humano confirme que son
    ésos, en vez de tocarlos a ciegas.
    """
    from modules.cheques import queries

    stub = _DB(mov=_mov(META), cheque=_cheque(),
               nds=[{"id_transaccion": 45705}, {"id_transaccion": 45706}],
               nds_por_id=_NDS)
    _patch(monkeypatch, stub)

    plan = queries.plan_deshacer_devuelto(23520)

    assert plan["compensacion"]["exacto"] is False
    assert len(plan["compensacion"]["movimientos"]) == 2


# ── los frenos ────────────────────────────────────────────────────────────
def test_no_restaura_si_alguien_movio_el_cheque_despues(monkeypatch):
    from modules.cheques import queries

    stub = _DB(mov=_mov(META), cheque=_cheque(stat="X"))
    _patch(monkeypatch, stub)

    with pytest.raises(ValueError, match="alguien lo movió"):
        queries.deshacer_devuelto(23520)
    assert not stub.executes, "se niega SIN escribir nada"


def test_no_se_deshace_dos_veces(monkeypatch):
    from modules.cheques import queries

    stub = _DB(mov=_mov(META, estado="reversado"), cheque=_cheque())
    _patch(monkeypatch, stub)

    with pytest.raises(ValueError, match="ya se deshizo"):
        queries.deshacer_devuelto(23520)


def test_no_deshace_un_movimiento_de_otro_tipo(monkeypatch):
    from modules.cheques import queries

    stub = _DB(mov=_mov(META, tipo="cheque_depositado"), cheque=_cheque())
    _patch(monkeypatch, stub)

    with pytest.raises(ValueError, match="no un cheque marcado"):
        queries.deshacer_devuelto(23520)


def test_sin_stat_previo_no_se_adivina(monkeypatch):
    """Un protesto viejo sin metadata no se restaura a ciegas."""
    from modules.cheques import queries

    stub = _DB(mov=_mov({"id_cheque": 98658}), cheque=_cheque())
    _patch(monkeypatch, stub)

    with pytest.raises(ValueError, match="no dice de qué estado"):
        queries.deshacer_devuelto(23520)


# ── la huella que hace posible todo esto ──────────────────────────────────
def test_el_protesto_anota_lo_que_movio(monkeypatch):
    """`compensar_deposito_devuelto` deja los ids de lo que hizo.

    Sin esto el deshacer tiene que reconocer la ND por el texto del concepto,
    que es adivinar. Se prueba acá, del lado del que ESCRIBE, porque es la
    parte que se olvida cuando alguien agrega una compensación nueva.
    """
    from modules.cheques import queries

    class _Rec:
        def fetch_all(self, sql, params=None, conn=None):
            s = " ".join(sql.split()).lower()
            if "chequextransaccion cxt" in s and "'de'" in s:
                return [{"id_transaccion": 777, "no_banco": 10}]
            return []

        def fetch_one(self, sql, params=None, conn=None):
            s = " ".join(sql.split()).lower()
            if "numreferencia = %s" in s and "'nd'" in s:
                return {"m": None}
            if "from scintela.banco where no_banco" in s:
                return {"nombre": "PICHINCHA"}
            return None

        def execute(self, sql, params=None, conn=None):
            return 1

    import db
    rec = _Rec()
    monkeypatch.setattr(db, "fetch_all", rec.fetch_all)
    monkeypatch.setattr(db, "fetch_one", rec.fetch_one)
    monkeypatch.setattr(db, "execute", rec.execute)

    import bank_helpers
    seq = iter([{"id_transaccion": 45705}, {"id_transaccion": 45706}])
    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario",
                        lambda conn, **kw: next(seq))

    registro: dict = {}
    queries.compensar_deposito_devuelto(
        object(), id_cheque=98657, importe=2300.06, codigo_cli="ADI",
        no_cheque="", fecha=date(2026, 8, 12), usuario="alex",
        registro=registro,
    )

    assert registro["id_nd"] == 45705
    assert registro["id_gs"] == 45706
    assert registro["links"] == [777]
