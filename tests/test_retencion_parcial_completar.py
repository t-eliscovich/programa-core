"""Cuando Asinfo dice MÁS de lo que tiene la factura: completar la diferencia.

TMT 2026-08-07 (dueña: *"podés hacerlo manual para esas?"*). Quedaron 4
facturas de junio con la retención cargada a medias — $101,52 — y no había
forma de arreglarlas: el guard `ya` mira si EXISTE la fila, no si el importe
coincide, así que las contaba como "ya estaban" y no las mostraba ni las
dejaba tildar.

Pasa por dos motivos reales: Asinfo tiene dos comprobantes por la misma factura
(fuente e IVA) y acá entró uno solo, o el backfill de la mig 0179 topeó la
retención al abono disponible (`LEAST`).

⚠️ Completar va **apagado por default**: sólo lo hace la aplicación de las
TILDADAS, donde una persona eligió fila por fila. Si lo hiciera el cron,
tocaría saldos viejos sin que nadie lo mire — y justo eso es lo que la dueña
pidió no hacer ("no corrijas nada de antes de agosto").
"""
from __future__ import annotations

import contextlib

import pytest


class _Stub:
    def __init__(self, factura, ya=True):
        self.factura = factura
        self.ya = ya
        self.updates = []
        self.inserts = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura" in s:
            return dict(self.factura)
        if "from scintela.retencion" in s:
            return {"x": 1} if self.ya else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        if "update scintela.factura" in " ".join(sql.split()).lower():
            self.updates.append(tuple(params))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        if "insert into scintela.retencion" in " ".join(sql.split()).lower():
            self.inserts.append(tuple(params))
            return {"id_retencion": 7}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _fac(retencion, saldo, importe=4126.55, abono=0.0):
    return {"id_factura": 1, "codigo_cli": "AL1", "numf": 177629,
            "numf_completo": "001-099-000177629", "fecha": None,
            "importe": importe, "abono": abono, "retencion": retencion,
            "saldo": saldo, "stat": "A"}


@pytest.fixture
def stub(monkeypatch):
    import db
    from modules.retenciones import queries as q
    s = _Stub(_fac(53.83, 4072.72))     # AL1 177629: tiene 53,83, Asinfo 125,60
    for a in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, a, getattr(s, a))
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None, raising=False)
    monkeypatch.setattr(q._md, "registrar", lambda **k: {"id_mov_doble": 1})
    return s


def test_sin_completar_sigue_contestando_ya(stub):
    """El cron y la aplicación en bloque no cambian de comportamiento."""
    from modules.retenciones import queries as q
    assert q._aplicar_una_por_numero("001-099-000177629", 125.60, "cron") == "ya"
    assert not stub.updates


def test_completar_aplica_solo_la_diferencia(stub):
    """125,60 − 53,83 = 71,77. Ni un peso más."""
    from modules.retenciones import queries as q
    r = q._aplicar_una_por_numero("001-099-000177629", 125.60, "t", completar=True)
    assert r == "aplicada"
    retencion_new, saldo_new = stub.updates[0][0], stub.updates[0][1]
    assert retencion_new == 125.60
    assert saldo_new == round(4126.55 - 0.0 - 125.60, 2)
    # la fila nueva registra la DIFERENCIA, para que la suma dé lo de Asinfo
    assert stub.inserts[0][2] == 71.77


def test_si_coincide_con_asinfo_no_hace_nada(stub):
    """Mismo importe = nada que completar."""
    from modules.retenciones import queries as q
    stub.factura = _fac(125.60, 4000.95)
    assert q._aplicar_una_por_numero(
        "001-099-000177629", 125.60, "t", completar=True) == "ya"
    assert not stub.updates


def test_una_diferencia_de_centavos_tambien_se_aplica(stub):
    """AL1 177722: tiene 126,81 y Asinfo dice 126,84. Son 3 centavos, y se
    aplican igual — el objetivo es que la columna diga EXACTAMENTE lo que dice
    Asinfo. Un tope tipo "menos de X no vale la pena" sería un número inventado
    que después nadie sabe de dónde salió."""
    from modules.retenciones import queries as q
    stub.factura = _fac(126.81, 4040.85, importe=4167.66)
    assert q._aplicar_una_por_numero(
        "001-099-000177629", 126.84, "t", completar=True) == "aplicada"
    assert stub.updates[0][0] == 126.84
    assert stub.inserts[0][2] == 0.03


def test_no_completa_si_lo_que_falta_supera_el_saldo(stub):
    """El freno de siempre: dejaría el saldo negativo."""
    from modules.retenciones import queries as q
    stub.factura = _fac(53.83, 10.0)
    assert q._aplicar_una_por_numero(
        "001-099-000177629", 125.60, "t", completar=True) == "rete_gt_saldo"
    assert not stub.updates


def test_la_pantalla_las_muestra_y_las_deja_tildar():
    """Antes caían en 'ya estaba' y no había forma ni de verlas."""
    from pathlib import Path
    tpl = Path("modules/facturas/templates/facturas/"
               "retenciones_asinfo.html").read_text(encoding="utf-8")
    assert "'parcial'" in tpl
    assert "in ('se_aplica', 'parcial')" in tpl, (
        "la fila parcial tiene que ser tildable, si no no se puede arreglar")


def test_el_total_del_mensaje_es_lo_aplicado_no_lo_de_asinfo(monkeypatch):
    """Si el flash sumara el total de Asinfo, prometería 125,60 cuando se
    aplicaron 71,77."""
    import db
    from modules.retenciones import queries as q
    monkeypatch.setattr(db, "fetch_one",
                        lambda sql, params=None, conn=None: {"importe": 71.77})
    assert q._ultimo_aplicado("001-099-000177629", 125.60) == 71.77
