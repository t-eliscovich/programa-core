"""Devolverle al cliente un cheque devuelto: la deuda pasa a una nota de débito.

TMT 2026-09-30 (Andrés pidió la rutina; Tamara decidió cómo). En el dBase se
pasaba el cheque a X y se cargaba a mano un abono NEGATIVO en las facturas.
Tamara: *"la factura murió hace mucho. mejor registremos un valor por cobrar.
Debería quedar como una nota de débito a la cuenta"* — *"sin vencimiento"*.

Lo que se clava acá es lo que puede salir caro:
  - las facturas que pagaba el cheque NO se reabren (ni abono ni vínculos);
  - la nota nace sin vencimiento, sin kilos y por el importe del cheque;
  - sólo se devuelve un cheque DEVUELTO (1/2/3);
  - la vuelta atrás no deja plata pagando una nota anulada;
  - la nota de débito no se cuenta como VENTA.
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _FakeDB:
    def __init__(self, cheque=None, mov=None, nd=None, cobros_nd=0):
        self.cheque, self.mov, self.nd, self.cobros_nd = cheque, mov, nd, cobros_nd
        self.sql: list[tuple[str, tuple]] = []

    @staticmethod
    def _n(sql):
        return " ".join((sql or "").split()).lower()

    def fetch_one(self, sql, params=None, conn=None):
        s = self._n(sql)
        if "from scintela.mov_doble" in s:
            return dict(self.mov) if self.mov else None
        if "count(*) as n from scintela.chequesxfact" in s:
            return {"n": self.cobros_nd}
        if "from scintela.cheque " in s:
            return dict(self.cheque) if self.cheque else None
        if "from scintela.factura" in s:
            return dict(self.nd) if self.nd else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        self.sql.append((self._n(sql), tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.sql.append((self._n(sql), tuple(params or ())))
        return {"id_factura": 777}

    def escribio(self, needle):
        return [x for x in self.sql if needle in x[0]]


@pytest.fixture
def _run(monkeypatch):
    def _go(**kw):
        import db as db_mod
        import mov_doble
        from modules.cheques import devolucion_cliente as dev
        fake = _FakeDB(**kw)
        for n in ("fetch_one", "fetch_all", "execute", "execute_returning"):
            monkeypatch.setattr(db_mod, n, getattr(fake, n))

        @contextlib.contextmanager
        def _tx(*a, **k):
            yield object()
        monkeypatch.setattr(db_mod, "tx", _tx)
        monkeypatch.setattr(dev, "asegurar_fecha_abierta", lambda f: None)
        monkeypatch.setattr(dev, "today_ec", lambda: date(2026, 9, 30))
        movs: list[dict] = []
        monkeypatch.setattr(mov_doble, "registrar",
                            lambda **k: movs.append(k) or 55)
        return dev, fake, movs
    return _go


def _ch(stat="1", importe=1405.91, no_banco=10):
    return {"id_cheque": 103872, "no_cheque": "2161", "stat": stat,
            "codigo_cli": "BED", "importe": importe, "no_banco": no_banco,
            "fechaout": date(2026, 9, 21)}


def test_crea_la_nota_de_debito_sin_vencimiento_por_el_importe(_run):
    dev, fake, movs = _run(cheque=_ch())
    res = dev.devolver(103872, usuario="andres")
    assert res["numero_nd"] == "ND-2161"
    ins = fake.escribio("insert into scintela.factura")
    assert len(ins) == 1
    sql, p = ins[0]
    assert "null" in sql.split("values")[1]          # vencimiento NULL
    assert p[0] == 2161 and p[2] == "BED"            # numf, cliente
    assert p[3] == 1405.91 and p[4] == 1405.91       # importe = saldo
    assert "ND" in p and "ND-2161" in p
    assert movs and movs[0]["tipo"] == "cheque_devuelto_al_cliente"
    assert movs[0]["metadata"]["stat_previo"] == "1"


def test_el_cheque_pasa_a_x_con_su_marca(_run):
    dev, fake, _ = _run(cheque=_ch(stat="2"))
    dev.devolver(103872, usuario="andres", motivo="lo retiró el cliente")
    upd = fake.escribio("update scintela.cheque")
    assert len(upd) == 1 and "stat='x'" in upd[0][0]
    assert "devuelto al cliente" in upd[0][1][1]


def test_las_facturas_que_pagaba_no_se_reabren(_run):
    """El corazón del pedido: ni abono de facturas ni borrar vínculos."""
    dev, fake, _ = _run(cheque=_ch())
    dev.devolver(103872)
    assert not fake.escribio("update scintela.factura")
    assert not fake.escribio("delete from scintela.chequesxfact")


@pytest.mark.parametrize("stat", ["Z", "P", "B", "C", "X", "D", "V"])
def test_solo_se_devuelve_un_cheque_devuelto(_run, stat):
    dev, fake, movs = _run(cheque=_ch(stat=stat))
    with pytest.raises(ValueError, match="DEVUELTO"):
        dev.devolver(103872)
    assert not fake.sql and not movs


def test_un_anticipo_no_se_devuelve(_run):
    dev, fake, _ = _run(cheque=_ch(no_banco=98))
    with pytest.raises(ValueError, match="anticipo"):
        dev.devolver(103872)
    assert not fake.sql


def _mov(estado="activo", stat_previo="1"):
    return {"id_mov_doble": 55, "tipo": "cheque_devuelto_al_cliente",
            "origen_id": 103872, "destino_id": 777, "estado": estado,
            "importe": 1405.91,
            "metadata": {"stat_previo": stat_previo,
                         "fechaout_previo": "2026-09-21"}}


def _nd(abono=0):
    return {"id_factura": 777, "numf_completo": "ND-2161", "stat": "Z",
            "abono": abono, "retencion": 0}


def test_deshacer_anula_la_nota_y_devuelve_el_cheque(_run):
    dev, fake, movs = _run(cheque=_ch(stat="X"), mov=_mov(), nd=_nd())
    res = dev.deshacer(55, usuario="tamara")
    assert res["stat_restaurado"] == "1"
    assert "stat='x'" in fake.escribio("update scintela.factura")[0][0]
    ch = fake.escribio("update scintela.cheque")[0]
    assert ch[1][0] == "1" and ch[1][1] == "2026-09-21"
    assert movs[0]["id_original"] == 55


def test_no_se_deshace_si_la_nota_ya_tiene_cobros(_run):
    dev, fake, _ = _run(cheque=_ch(stat="X"), mov=_mov(), nd=_nd(abono=100))
    with pytest.raises(ValueError, match="ya tiene cobros"):
        dev.deshacer(55)
    assert not fake.sql


def test_no_se_deshace_si_hay_un_cobro_vinculado(_run):
    dev, fake, _ = _run(cheque=_ch(stat="X"), mov=_mov(), nd=_nd(), cobros_nd=1)
    with pytest.raises(ValueError, match="ya tiene cobros"):
        dev.deshacer(55)
    assert not fake.sql


def test_no_se_deshace_dos_veces(_run):
    dev, fake, _ = _run(cheque=_ch(stat="X"), mov=_mov(estado="reversado"), nd=_nd())
    with pytest.raises(ValueError, match="ya se deshizo"):
        dev.deshacer(55)


def test_no_pisa_un_cheque_que_alguien_movio(_run):
    dev, fake, _ = _run(cheque=_ch(stat="Z"), mov=_mov(), nd=_nd())
    with pytest.raises(ValueError, match="no en X"):
        dev.deshacer(55)
    assert not fake.sql


@pytest.mark.parametrize("stat", ["1", "2", "3"])
def test_la_x_de_un_devuelto_es_devolverlo_al_cliente(stat):
    from modules.cheques import queries
    xs = [t for t in queries.transiciones_para(stat) if t["stat_destino"] == "X"]
    assert len(xs) == 1
    assert xs[0]["endpoint"] == "cheques.devolver_al_cliente"


def test_la_x_de_cartera_sigue_siendo_anular():
    from modules.cheques import queries
    xs = [t for t in queries.transiciones_para("Z") if t["stat_destino"] == "X"]
    assert xs and xs[0]["endpoint"] == "cheques.anular_error_carga"


def test_la_nota_se_muestra_con_su_numero_entero():
    from filters import num_doc
    assert num_doc({"tipo": "ND", "numf_completo": "ND-2161", "numf": 2161}) == "ND-2161"
    assert num_doc({"tipo": "N", "numf_completo": "NTEN-10546", "numf": 10546}) == 10546
    assert num_doc({"tipo": "F", "numf_completo": None, "numf": 185444}) == 185444


def test_la_nota_de_debito_no_es_venta():
    """Cada suma de "lo facturado" deja afuera la ND (no es mercadería)."""
    root = Path(_REPO_ROOT)
    for rel in ("modules/analisis/cobranza.py", "modules/facturas/aviso_ventas.py",
                "modules/comisiones/queries.py"):
        assert "<> 'ND'" in (root / rel).read_text(encoding="utf-8"), rel
    assert (root / "modules/informes/queries.py").read_text(
        encoding="utf-8").count("COALESCE(f.tipo, '') <> 'ND'") >= 3


def test_el_tipo_nd_es_parte_del_vocabulario():
    from modules.facturas import tipo_doc
    assert tipo_doc.normalizar("ND") == "ND"
    assert tipo_doc.etiqueta("ND") == "Nota de débito"


def test_el_historial_sabe_deshacerlo():
    from modules.historial import views as hv
    assert "cheque_devuelto_al_cliente" in hv._REVERSO_DISPATCH


def test_un_cheque_devuelto_al_cliente_no_se_desaplica(monkeypatch):
    """Desaplicarlo reabriría las facturas: el cliente debería dos veces."""
    import db as db_mod
    from modules.cheques import queries as q

    @contextlib.contextmanager
    def _tx(*a, **k):
        yield object()
    monkeypatch.setattr(db_mod, "tx", _tx)
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None)
    monkeypatch.setattr(db_mod, "fetch_one", lambda *a, **k: {
        "id_cheque": 1, "no_cheque": "2161", "stat": "X",
        "observacion": "[DEV] devuelto al cliente → ND-2161"})
    escrito = []
    monkeypatch.setattr(db_mod, "execute", lambda *a, **k: escrito.append(a))
    with pytest.raises(ValueError, match="nota de débito"):
        q.desaplicar_factura(id_cheque=1, id_factura=6)
    assert not escrito
