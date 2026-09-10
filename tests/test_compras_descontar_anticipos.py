"""Descontar anticipos de la deuda de una compra (dueña 10/09/2026).

Caso real: la compra de colorantes COLOURTEX (C2) entra por el puente de
formulas con la deuda entera (248.137,86) y ya había 97.388,28 pagados como
anticipos (CAE + MAPFRE + INI). Desde la ficha se tildan los anticipos, pasan
a aplicados (st='B'), la deuda baja al saldo y la compra queda parcial.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _FakeDB:
    def __init__(self):
        self.compra = {"id_compra": 700, "numero": 10330, "codigo_prov": "C2",
                       "importe": 248137.86, "stat": "", "cuenta_pagada": None}
        self.posdat = {"id_posdat": 9001, "importe": 248137.86, "banc": 0, "anulada": False}
        self.dolares = {
            2903: {"id_dolares": 2903, "cta": "C2", "importe": 64607.00, "st": "", "concepto": "INI"},
            2904: {"id_dolares": 2904, "cta": "C2", "importe": 836.70, "st": "", "concepto": "MAPFRE"},
            2950: {"id_dolares": 2950, "cta": "C2", "importe": 31944.58, "st": "", "concepto": "CAE"},
            2960: {"id_dolares": 2960, "cta": "MC", "importe": 556.50, "st": "", "concepto": "HOTEL"},
        }
        self.movs: dict[int, dict] = {}
        self.executes: list[tuple[str, tuple]] = []

    # -- lecturas -----------------------------------------------------------
    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.compra where id_compra" in s:
            return dict(self.compra)
        if "from scintela.posdat where prov" in s or ("from scintela.posdat" in s and "where prov" in s):
            return dict(self.posdat) if not self.posdat.get("anulada") else None
        if "from scintela.posdat where id_posdat" in s:
            return dict(self.posdat)
        if "from scintela.mov_doble where id_mov_doble" in s:
            return dict(self.movs.get(params[0]) or {}) or None
        raise AssertionError(f"fetch_one inesperado: {s[:90]}")

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.dolares where id_dolares in" in s:
            return [dict(self.dolares[i]) for i in params if i in self.dolares]
        if "from scintela.dolares where upper(trim(cta))" in s:
            return [dict(d) for d in self.dolares.values()
                    if d["cta"] == params[0] and not d["st"]]
        if "from scintela.mov_doble where origen_table = 'compra'" in s:
            return [dict(m) for m in self.movs.values()
                    if m["origen_id"] == params[0] and m["tipo"] == params[1]
                    and m["estado"] == "activo"]
        raise AssertionError(f"fetch_all inesperado: {s[:90]}")

    # -- escrituras ---------------------------------------------------------
    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        if s.startswith("update scintela.dolares set st = 'b'"):
            for i in params[1:]:
                self.dolares[i]["st"] = "B"
        elif s.startswith("update scintela.dolares set st = ''"):
            for i in params[1:]:
                if self.dolares[i]["st"] == "B":
                    self.dolares[i]["st"] = ""
        elif s.startswith("update scintela.posdat set importe"):
            self.posdat["importe"] = params[0]
        elif s.startswith("update scintela.compra set cuenta_pagada"):
            self.compra["cuenta_pagada"] = params[0]
        elif "set estado='reversado'" in s:
            self.movs[params[1]]["estado"] = "reversado"
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "insert into scintela.mov_doble" in s:
            idm = 500 + len(self.movs)
            # orden de columnas de mov_doble.registrar
            (fecha, tipo, ot, oid, dt, did, imp, _con, _usr, estado, _orig, meta, _b) = params
            self.movs[idm] = {"id_mov_doble": idm, "tipo": tipo, "origen_table": ot,
                              "origen_id": oid, "destino_table": dt, "destino_id": did,
                              "importe": imp, "estado": estado, "fecha": fecha,
                              "metadata": meta}
            return {"id_mov_doble": idm}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


@pytest.fixture
def fake(monkeypatch):
    import db as db_mod
    f = _FakeDB()
    for name in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db_mod, name, getattr(f, name))
    return f


def test_descontar_baja_la_deuda_y_aplica_los_anticipos(fake):
    from modules.compras import queries as q
    r = q.descontar_anticipos(700, [2903, 2904, 2950], usuario="tamara")
    assert r["total"] == pytest.approx(97388.28)
    assert r["deuda_despues"] == pytest.approx(150749.58)
    assert fake.posdat["importe"] == pytest.approx(150749.58)
    assert all(fake.dolares[i]["st"] == "B" for i in (2903, 2904, 2950))
    assert fake.dolares[2960]["st"] == ""
    assert fake.compra["cuenta_pagada"] == "P"
    md = fake.movs[r["id_mov_doble"]]
    assert md["tipo"] == "compra_descuenta_anticipos" and md["destino_id"] == 9001
    meta = json.loads(md["metadata"])
    assert {a["id_dolares"] for a in meta["anticipos"]} == {2903, 2904, 2950}
    assert meta["deuda_antes"] == pytest.approx(248137.86)


def test_descontar_todo_deja_la_compra_cubierta(fake):
    from modules.compras import queries as q
    fake.posdat["importe"] = 97388.28
    r = q.descontar_anticipos(700, [2903, 2904, 2950])
    assert r["deuda_despues"] == 0
    assert fake.compra["cuenta_pagada"] == "A"


def test_descontar_rechaza_anticipo_de_otro_proveedor(fake):
    from modules.compras import queries as q
    with pytest.raises(ValueError, match="es de 'MC'"):
        q.descontar_anticipos(700, [2903, 2960])
    assert fake.dolares[2903]["st"] == ""


def test_descontar_rechaza_anticipo_ya_aplicado(fake):
    from modules.compras import queries as q
    fake.dolares[2904]["st"] = "B"
    with pytest.raises(ValueError, match="ya está aplicado"):
        q.descontar_anticipos(700, [2904])


def test_descontar_rechaza_si_supera_la_deuda(fake):
    from modules.compras import queries as q
    fake.posdat["importe"] = 50000
    with pytest.raises(ValueError, match="superan|destildá"):
        q.descontar_anticipos(700, [2903])
    assert fake.posdat["importe"] == 50000


def test_descontar_sin_deuda_abierta(fake):
    from modules.compras import queries as q
    fake.posdat["banc"] = 1
    with pytest.raises(ValueError, match="deuda abierta"):
        q.descontar_anticipos(700, [2903])


def test_descontar_sin_tildar_nada(fake):
    from modules.compras import queries as q
    with pytest.raises(ValueError, match="al menos un anticipo"):
        q.descontar_anticipos(700, [])


def test_deshacer_revive_los_anticipos_y_sube_la_deuda(fake):
    from modules.compras import queries as q
    r = q.descontar_anticipos(700, [2903, 2904, 2950])
    out = q.deshacer_descuento_anticipos(r["id_mov_doble"], usuario="tamara")
    assert out["n"] == 3
    assert fake.posdat["importe"] == pytest.approx(248137.86)
    assert all(fake.dolares[i]["st"] == "" for i in (2903, 2904, 2950))
    assert fake.compra["cuenta_pagada"] is None
    assert fake.movs[r["id_mov_doble"]]["estado"] == "reversado"
    rev = [m for m in fake.movs.values() if m["tipo"].endswith("_reverso")]
    assert len(rev) == 1 and rev[0]["importe"] == pytest.approx(-97388.28)
    with pytest.raises(ValueError, match="ya está deshecho"):
        q.deshacer_descuento_anticipos(r["id_mov_doble"])


def test_deshacer_se_niega_si_la_deuda_ya_se_pago_con_cheque(fake):
    from modules.compras import queries as q
    r = q.descontar_anticipos(700, [2950])
    fake.posdat["banc"] = 1
    with pytest.raises(ValueError, match="cheque"):
        q.deshacer_descuento_anticipos(r["id_mov_doble"])
    assert fake.dolares[2950]["st"] == "B"


def test_descuentos_de_anticipos_lista_lo_activo(fake):
    from modules.compras import queries as q
    r = q.descontar_anticipos(700, [2950])
    ds = q.descuentos_de_anticipos(700)
    assert len(ds) == 1 and ds[0]["importe"] == pytest.approx(31944.58)
    assert ds[0]["anticipos"][0]["concepto"] == "CAE"
    q.deshacer_descuento_anticipos(r["id_mov_doble"])
    assert q.descuentos_de_anticipos(700) == []


def test_editar_importe_se_frena_con_descuento_activo(fake, monkeypatch):
    """El posdat guarda el SALDO tras el descuento: editar el importe de la
    compra lo rompería, igual que con un pago parcial."""
    from modules.compras import queries as q
    monkeypatch.setattr(q, "asegurar_fecha_abierta", lambda *a, **k: None)
    q.descontar_anticipos(700, [2950])
    orig = fake.fetch_one

    def fetch_one(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.mov_doble where origen_table" in s:
            return {"importe": 31944.58}
        if "from scintela.compra where id_compra" in s:
            return {**fake.compra, "fecha": date(2026, 9, 10), "fechad": date(2026, 11, 9),
                    "id_transaccion": None, "tipo": "Q", "concepto": "", "observacion": ""}
        return orig(sql, params, conn)
    import db as db_mod
    monkeypatch.setattr(db_mod, "fetch_one", fetch_one)
    with pytest.raises(ValueError, match="pago parcial activo"):
        q.editar(700, importe=1000, usuario="t")
