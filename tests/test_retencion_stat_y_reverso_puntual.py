"""Una retención no "abona" nada, y se puede deshacer de a una.

TMT 2026-08-07 (dueña, mirando las 14 que se le aplicaron a TNZ):
*"retención no debería mover de Z a A"*. Tiene razón — **Z es "emitida, sin
abono" y A es "abonada parcialmente"**: si lo único que entró fue una
retención, nadie abonó. Venía mal desde el 06/08, cuando la retención se sumaba
al abono y marcarla 'A' era coherente; al sacarla a su columna propia (mig
0179) el aplicador siguió poniendo 'A' a mano. Medido ese día: 849 facturas en
'A' sin un peso de abono.

Y el reverso: hasta hoy sólo se podían deshacer POR PERÍODO, así que arreglar
una aplicada por error se llevaba puestas todas las buenas del mes.
"""
from __future__ import annotations

import contextlib
from datetime import date

import pytest

# ── el estado ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("saldo, abono, previo, esperado", [
    (100.0, 0.0, "Z", "Z"),      # sólo retención: sigue emitida
    (100.0, 0.0, "",  "Z"),      # legacy sin stat
    (100.0, 50.0, "A", "A"),     # hay un pago real
    (100.0, 50.0, "Z", "A"),     # el pago la mueve a abonada
    (0.0, 0.0, "Z", "T"),        # la retención la terminó de cubrir
    (0.004, 0.0, "Z", "T"),      # centavos
    (100.0, 0.0, "A", "A"),      # ya estaba abonada: no la degrada
])
def test_stat_tras_retencion(saldo, abono, previo, esperado):
    from modules.retenciones.queries import _stat_tras_retencion
    assert _stat_tras_retencion(saldo, abono, previo) == esperado


class _Stub:
    def __init__(self, factura):
        self.factura = factura
        self.updates = []
        self.deletes = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura" in s:
            return dict(self.factura)
        if "from scintela.retencion" in s:
            return None
        if "from scintela.mov_doble" in s:
            return dict(self.mov) if getattr(self, "mov", None) else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "update scintela.factura" in s:
            self.updates.append(tuple(params))
        elif "delete from scintela.retencion" in s:
            self.deletes.append(tuple(params))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        return {"id_retencion": 55}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _fac(**kw):
    base = {"id_factura": 7, "codigo_cli": "TNZ", "numf": 173868,
            "numf_completo": None, "fecha": date(2026, 4, 23),
            "importe": 9980.0, "abono": 0.0, "retencion": 0.0,
            "saldo": 9980.0, "stat": "Z"}
    base.update(kw)
    return base


@pytest.fixture
def stub(monkeypatch):
    import db
    from modules.retenciones import queries as q
    s = _Stub(_fac())
    for a in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, a, getattr(s, a))
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None, raising=False)
    monkeypatch.setattr(q._md, "registrar", lambda **k: {"id_mov_doble": 1})
    return s


def test_aplicar_una_retencion_deja_la_factura_en_Z(stub):
    """El caso de TNZ: la factura debe menos, pero nadie la abonó."""
    from modules.retenciones import queries as q
    assert q._aplicar_una_por_numero("001-099-000173868", 173.57, "t") == "aplicada"
    retencion, saldo, stat = stub.updates[0][0], stub.updates[0][1], stub.updates[0][2]
    assert retencion == 173.57
    assert saldo == 9806.43          # el saldo SÍ baja
    assert stat == "Z", "una retención sola no abona nada"


def test_con_abono_previo_si_queda_en_A(stub):
    from modules.retenciones import queries as q
    stub.factura = _fac(abono=500.0, saldo=9480.0, stat="A")
    q._aplicar_una_por_numero("001-099-000173868", 173.57, "t")
    assert stub.updates[0][2] == "A"


def test_si_la_retencion_cubre_todo_queda_en_T(stub):
    from modules.retenciones import queries as q
    stub.factura = _fac(importe=173.57, saldo=173.57)
    q._aplicar_una_por_numero("001-099-000173868", 173.57, "t")
    assert stub.updates[0][2] == "T"


# ── el reverso de a una ────────────────────────────────────────────────────

def test_deshacer_una_restaura_todo(stub):
    """Y va por id_mov_doble: la factura de origen dBase tiene numf_completo
    NULL, así que el reverso por número no la encontraría."""
    from modules.retenciones import queries as q
    stub.factura = _fac(retencion=173.57, saldo=9806.43, stat="Z")
    stub.mov = {"id_mov_doble": 88, "tipo": "retencion_asinfo_aplicada",
                "estado": "activo", "origen_id": 7, "importe": 173.57,
                "metadata": {"id_retencion": 55, "rete": 173.57,
                             "abono_previo": 0.0, "retencion_previa": 0.0,
                             "saldo_previo": 9980.0, "stat_previo": "Z"}}
    res = q.desaplicar_por_mov(88, usuario="t")
    assert res["numf"] == 173868 and res["saldo_restaurado"] == 9980.0
    abono, retencion, saldo, stat = stub.updates[0][:4]
    assert (abono, retencion, saldo, stat) == (0.0, 0.0, 9980.0, "Z")
    assert stub.deletes and stub.deletes[0][0] == 55


def test_no_deshace_dos_veces(stub):
    from modules.retenciones import queries as q
    stub.mov = {"id_mov_doble": 88, "tipo": "retencion_asinfo_aplicada",
                "estado": "reversado", "origen_id": 7, "importe": 173.57,
                "metadata": {}}
    with pytest.raises(ValueError, match="reversado"):
        q.desaplicar_por_mov(88, usuario="t")
    assert not stub.updates


def test_no_deshace_un_movimiento_de_otro_tipo(stub):
    from modules.retenciones import queries as q
    stub.mov = {"id_mov_doble": 88, "tipo": "cheque_aplicado_a_factura",
                "estado": "activo", "origen_id": 7, "importe": 10.0,
                "metadata": {}}
    with pytest.raises(ValueError, match="no es una retención"):
        q.desaplicar_por_mov(88, usuario="t")
    assert not stub.updates


def test_el_historial_ya_no_la_manda_a_deshacer_el_mes_entero():
    """El ↺ de la fila estaba bloqueado con el consejo "deshacelas por
    período" — que para UNA aplicada por error se lleva puestas las buenas."""
    from modules.historial.views import _PERMISO_REVERSO_INLINE, _REVERSO_BLOQUEADO
    assert "retencion_asinfo_aplicada" not in _REVERSO_BLOQUEADO
    assert _PERMISO_REVERSO_INLINE["retencion_asinfo_aplicada"] == "facturas.crear"
