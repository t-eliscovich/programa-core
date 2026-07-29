"""Devolver a cartera un cheque cancelado con un anticipo.

TMT 2026-07-29. `cancelar_por_anticipo` (06/07) pone el cheque en 'X' y
BORRA su fila de posdatados. Su docstring decía que el reverso era "manual,
NO automatizado a propósito" — pero /historial mostraba igual el ↺, que no
llevaba a ninguna parte: 21 movs así.

Dos cosas se arreglaron y las dos se testean:

  1. la cancelación ahora GUARDA la posdat antes de borrarla (mismo patrón
     que `deshacer_neteo`), porque sin snapshot no hay de dónde deducir su
     fecha ni su concepto;
  2. el reverso restaura stat + posdat, y se NIEGA cuando restaurar pisaría
     algo: cheque ya movido, mov ya reversado, o cancelación vieja sin
     snapshot de un cheque postdatado (ahí reconstruir sería inventar).

Lo que el reverso NO hace es ajustar el espejo NB=98 (saldo a favor): al
des-cancelar un cheque el sobrante del anticipo debería subir por su
importe, pero eso es una decisión contable. Se devuelve el espejo para que
la pantalla lo diga con números — y ESO también se testea, porque un aviso
que no llega es lo mismo que no avisar.
"""
from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

import pytest


class _FakeDB:
    def __init__(self, mov, cheque, espejo=None):
        self.mov = dict(mov) if mov else None
        self.cheque = dict(cheque) if cheque else None
        self.espejo = dict(espejo) if espejo else None
        self.sql: list[tuple[str, tuple]] = []

    def fetch_one(self, sql: str, params: Any = None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.mov_doble" in s:
            return dict(self.mov) if self.mov else None
        if "id_cheque_padre" in s:
            return dict(self.espejo) if self.espejo else None
        if "from scintela.cheque" in s:
            return dict(self.cheque) if self.cheque else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql: str, params: Any = None, conn=None):
        self.sql.append((" ".join((sql or "").split()).lower(), tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        return None

    def apply_to(self, monkeypatch, db_mod):
        for n, f in (("fetch_one", self.fetch_one), ("fetch_all", self.fetch_all),
                     ("execute", self.execute),
                     ("execute_returning", self.execute_returning)):
            monkeypatch.setattr(db_mod, n, f)

        @contextlib.contextmanager
        def _fake_tx(*_a, **_k):
            yield object()

        monkeypatch.setattr(db_mod, "tx", _fake_tx)

    def escribio(self, needle: str) -> list[tuple[str, tuple]]:
        return [s for s in self.sql if needle in s[0]]


def _mov(stat_previo="Z", estado="activo", snap=None, con_snap=True,
         tipo="cheque_cancelado_por_anticipo", id_anticipo=700):
    meta = {"id_cheque": 501, "stat_previo": stat_previo,
            "id_cheque_anticipo": id_anticipo, "monto_anticipo": 5000.0,
            "codigo_cli": "AIG"}
    if con_snap:
        meta["posdat_borradas"] = snap if snap is not None else [
            {"fecha": "2026-06-01", "fechad": "2026-08-15", "prov": "AIG",
             "num": 501, "importe": 1200.0, "concepto": "ch.4471"},
        ]
    return {"id_mov_doble": 8801, "tipo": tipo, "origen_id": 501,
            "destino_id": id_anticipo, "estado": estado, "metadata": meta,
            "importe": 1200.0, "fecha_operacion": date(2026, 7, 10)}


def _ch(stat="X", fecha=date(2026, 6, 1), fechad=date(2026, 8, 15)):
    return {"id_cheque": 501, "no_cheque": "4471", "stat": stat,
            "codigo_cli": "AIG", "importe": 1200.0, "fecha": fecha,
            "fechad": fechad}


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    import modules.cheques.queries as q
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda f: None)
    import mov_doble
    reg: list[dict] = []
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: reg.append(kw) or 1)
    return reg


def _correr(monkeypatch, mov, ch, espejo=None):
    import db as db_mod
    import modules.cheques.queries as q
    fake = _FakeDB(mov, ch, espejo)
    fake.apply_to(monkeypatch, db_mod)
    return q, fake


def test_devuelve_el_cheque_a_su_stat_y_restaura_la_posdat(monkeypatch, _stubs):
    q, fake = _correr(monkeypatch, _mov("Z"), _ch("X"))
    res = q.reversar_cancelacion_por_anticipo(8801, usuario="tamara")
    assert res["stat_restaurado"] == "Z"
    assert res["posdat_restauradas"] == 1
    upd = fake.escribio("update scintela.cheque")
    assert len(upd) == 1 and upd[0][1][0] == "Z"
    ins = fake.escribio("insert into scintela.posdat")
    assert len(ins) == 1, "la fila de pasivos tiene que volver"
    # Y vuelve con SU fecha y SU concepto, no con los de hoy.
    assert "ch.4471" in ins[0][1]


def test_respeta_el_stat_previo_que_no_siempre_es_Z(monkeypatch, _stubs):
    """Un cheque postergado (P) o protestado (2) no vuelve a cartera 'Z':
    vuelve a donde estaba."""
    q, fake = _correr(monkeypatch, _mov("P"), _ch("X"))
    res = q.reversar_cancelacion_por_anticipo(8801)
    assert res["stat_restaurado"] == "P"
    assert fake.escribio("update scintela.cheque")[0][1][0] == "P"


def test_avisa_del_saldo_a_favor_que_no_ajusta(monkeypatch, _stubs):
    """El espejo NB=98 no se toca, pero se devuelve para que la pantalla lo
    diga. Sin esto el reverso deja el saldo a favor corto en silencio."""
    espejo = {"id_cheque": 999, "no_cheque": "ANT", "importe": -800.0, "stat": "Z"}
    q, fake = _correr(monkeypatch, _mov("Z"), _ch("X"), espejo)
    res = q.reversar_cancelacion_por_anticipo(8801)
    assert res["espejo"]["id_cheque"] == 999
    # Y NO lo tocó.
    assert not [s for s in fake.sql
                if "update scintela.cheque" in s[0] and 999 in s[1]]
    assert _stubs[-1]["metadata"]["espejo_no_ajustado"]["id_cheque"] == 999


def test_no_reversa_si_el_cheque_ya_no_esta_en_X(monkeypatch, _stubs):
    """Alguien lo movió después de la cancelación: restaurarlo lo pisaría."""
    q, fake = _correr(monkeypatch, _mov("Z"), _ch("B"))
    with pytest.raises(ValueError, match="stat='B'"):
        q.reversar_cancelacion_por_anticipo(8801)
    assert not fake.escribio("update scintela.cheque")


def test_no_reversa_dos_veces(monkeypatch, _stubs):
    q, fake = _correr(monkeypatch, _mov(estado="reversado"), _ch("X"))
    with pytest.raises(ValueError, match="reversado"):
        q.reversar_cancelacion_por_anticipo(8801)
    assert not fake.escribio("update scintela.cheque")


def test_cancelacion_vieja_de_un_postdatado_sin_snapshot_no_se_inventa(
        monkeypatch, _stubs):
    """Sin snapshot no hay fecha ni concepto de la posdat borrada. Antes que
    inventar un pasivo, se explica qué cargar a mano."""
    q, fake = _correr(monkeypatch, _mov(con_snap=False), _ch("X"))
    with pytest.raises(ValueError, match="anterior al 29/07"):
        q.reversar_cancelacion_por_anticipo(8801)
    assert not fake.escribio("update scintela.cheque")
    assert not fake.escribio("insert into scintela.posdat")


def test_cancelacion_vieja_de_un_cheque_al_dia_si_se_reversa(monkeypatch, _stubs):
    """Si el cheque NO era postdatado (fechad == fecha) no había posdat que
    perder, así que la falta de snapshot no bloquea nada."""
    q, fake = _correr(
        monkeypatch, _mov(con_snap=False),
        _ch("X", fecha=date(2026, 6, 1), fechad=date(2026, 6, 1)))
    res = q.reversar_cancelacion_por_anticipo(8801)
    assert res["sin_snapshot"] is True
    assert res["posdat_restauradas"] == 0
    assert fake.escribio("update scintela.cheque")


def test_snapshot_vacio_no_es_lo_mismo_que_sin_snapshot(monkeypatch, _stubs):
    """Lista vacía = "miré y no tenía posdat". Ausencia de la clave = "no
    miré". La primera reversa sin drama incluso en un postdatado."""
    q, fake = _correr(monkeypatch, _mov(snap=[]), _ch("X"))
    res = q.reversar_cancelacion_por_anticipo(8801)
    assert res["posdat_restauradas"] == 0
    assert res["sin_snapshot"] is False
    assert fake.escribio("update scintela.cheque")


def test_rechaza_un_mov_de_otro_tipo(monkeypatch, _stubs):
    q, _ = _correr(monkeypatch, _mov(tipo="cheque_creado"), _ch("X"))
    with pytest.raises(ValueError, match="no una cancelación"):
        q.reversar_cancelacion_por_anticipo(8801)


def test_no_restaura_a_un_stat_muerto(monkeypatch, _stubs):
    """Si la metadata dice que venía de 'T' (cancelada) algo está roto: no
    se restaura a un estado inconsistente."""
    q, fake = _correr(monkeypatch, _mov("T"), _ch("X"))
    with pytest.raises(ValueError, match="no es un estado vivo"):
        q.reversar_cancelacion_por_anticipo(8801)
    assert not fake.escribio("update scintela.cheque")


def test_el_reverso_queda_linkeado_al_original(monkeypatch, _stubs):
    q, _ = _correr(monkeypatch, _mov("Z"), _ch("X"))
    q.reversar_cancelacion_por_anticipo(8801, motivo="el anticipo era de otro cliente")
    kw = _stubs[-1]
    assert kw["tipo"] == "reverso_cheque_cancelado_por_anticipo"
    assert kw["id_original"] == 8801
    assert "otro cliente" in kw["concepto"]
