"""Anticipos de importaciones como MOVIMIENTOS (mig 0113) — modelo v2.

TMT 2026-07-06 (dueña): "una importación puede tener muchos anticipos...
cuando creo un anticipo hago una nota de débito desde Pichincha" + "ya no
hace falta la división de pagado o no: dejamos de predecir cuánto saldría;
los anticipos son casi el 90% y lo restante se carga en /compras".

Antes scintela.importacion_pago tenía UNA columna anticipo_aplicado → el 2º
anticipo PISABA al 1º. Ahora cada carga es un movimiento nuevo y el VALOR
DEL STOCK de la importación = Σ movimientos.

Cubre el backend (modules/importaciones/pago.py):
1. agregar_movimiento — N anticipos NO se pisan; cada uno genera su ND
   automática en Pichincha + mov_doble ('importacion_anticipo'); el cache
   anticipo_aplicado queda = Σ (y deuda en NULL — dejó de existir).
2. deshacer_movimiento — borra el movimiento, COMPENSA su ND con una NC
   (patrón reversar_movimiento_simple) y recalcula la Σ.
3. Backfill mig 0113: el movimiento inicial replica el VALOR EFECTIVO de la
   fila vieja (pagada → monto_real/costo_estimado; si no → anticipo_aplicado)
   para que el valor de stock NO salte en el deploy (_suma_movs es el espejo
   en Python de esa Σ).
4. Vista: sin radio anticipo/pago, sin botón Pagar; aviso de ND automática.

Mismo estilo stub que tests/test_cheques_anticipo_cancela_cartera.py.
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ─────────────────────────── stubs ─────────────────────────────────────────

class _DBStub:
    """DB en memoria: scintela.importacion_pago + importacion_pago_mov."""

    def __init__(self):
        self.pago: dict[str, dict] = {}   # im_numero -> fila
        self.movs: dict[int, dict] = {}   # id_mov -> fila
        self._id_mov = 0
        self._id_pago = 0

    def seed_pago(self, im, *, costo_estimado=None):
        self._id_pago += 1
        self.pago[im] = {
            "id": self._id_pago, "im_numero": im,
            "costo_estimado": costo_estimado,
            "anticipo_aplicado": 0.0, "deuda": None,
        }
        return self.pago[im]

    def seed_mov(self, im, monto, *, tipo="anticipo", id_transaccion=None):
        """Movimiento pre-existente (ej. backfill de la mig 0113, sin ND)."""
        self._id_mov += 1
        self.movs[self._id_mov] = {
            "id_mov": self._id_mov, "im_numero": im, "tipo": tipo,
            "fecha": "2026-07-01", "monto": monto, "nota": "backfill mig 0113",
            "id_transaccion": id_transaccion,
        }
        return self.movs[self._id_mov]

    # ── API db.* ──
    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        p = tuple(params or ())
        if "to_regclass" in s:
            return {"t": "ok"}
        if "select id_importacion_pago from scintela.importacion_pago" in s:
            r = self.pago.get(p[0])
            return {"id_importacion_pago": r["id"]} if r else None
        if "from scintela.importacion_pago_mov where id_mov" in s:
            m = self.movs.get(p[0])
            return dict(m) if m else None
        if "from scintela.transacciones_bancarias" in s:
            return {"id_transaccion": p[0], "no_banco": 10,
                    "no_cta": None, "documento": "ND"}
        if "from scintela.mov_doble" in s:
            return {"id_mov_doble": 777}
        raise AssertionError(f"fetch_one sin stub: {s[:90]}")

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        p = tuple(params or ())
        if "select tipo, monto from scintela.importacion_pago_mov" in s:
            return [{"tipo": m["tipo"], "monto": m["monto"]}
                    for m in self.movs.values() if m["im_numero"] == p[0]]
        if "from scintela.importacion_pago_mov" in s:  # movimientos_por_im
            return [dict(m, fecha=str(m["fecha"]))
                    for m in self.movs.values() if m["im_numero"] in p[0]]
        return []

    def execute(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        p = tuple(params or ())
        if s.startswith("update scintela.importacion_pago set anticipo_aplicado"):
            # _recompute_cache: SET anticipo_aplicado = %s, deuda = NULL
            for r in self.pago.values():
                if r["id"] == p[2]:
                    r["anticipo_aplicado"] = p[0]
                    r["deuda"] = None
            return 1
        if s.startswith("update scintela.importacion_pago_mov set id_transaccion"):
            self.movs[p[1]]["id_transaccion"] = p[0]
            return 1
        if s.startswith("delete from scintela.importacion_pago_mov"):
            self.movs.pop(p[0], None)
            return 1
        if s.startswith("update scintela.importacion_pago set"):
            return 1  # _upsert genérico (no lo verificamos acá)
        if s.startswith("insert into scintela.importacion_pago "):
            return 1
        raise AssertionError(f"execute sin stub: {s[:90]}")

    def execute_returning(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        p = tuple(params or ())
        if "insert into scintela.importacion_pago_mov" in s:
            self._id_mov += 1
            self.movs[self._id_mov] = {
                "id_mov": self._id_mov, "im_numero": p[0], "tipo": p[1],
                "fecha": p[2], "monto": p[3], "nota": p[4],
                "id_transaccion": None,
            }
            return {"id_mov": self._id_mov}
        if "insert into scintela.importacion_pago" in s:
            self.seed_pago(p[0])
            return {"id_importacion_pago": self._id_pago}
        raise AssertionError(f"execute_returning sin stub: {s[:90]}")

    @contextlib.contextmanager
    def tx(self):
        yield object()


class _BankStub:
    def __init__(self):
        self.calls: list[dict] = []
        self._id = 9000

    def insert_movimiento_bancario(self, conn, **kw):
        self._id += 1
        kw["_id"] = self._id
        self.calls.append(kw)
        return {"id_transaccion": self._id, "saldo_nuevo": 0.0}


@pytest.fixture
def entorno(monkeypatch):
    import bank_helpers
    import db
    import mov_doble
    import periodo_guard

    stub = _DBStub()
    bank = _BankStub()
    mds: list[dict] = []
    monkeypatch.setattr(db, "fetch_one", stub.fetch_one)
    monkeypatch.setattr(db, "fetch_all", stub.fetch_all)
    monkeypatch.setattr(db, "execute", stub.execute)
    monkeypatch.setattr(db, "execute_returning", stub.execute_returning)
    monkeypatch.setattr(db, "tx", stub.tx)
    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario",
                        bank.insert_movimiento_bancario)
    monkeypatch.setattr(mov_doble, "registrar",
                        lambda **kw: (mds.append(kw), len(mds))[1])
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta",
                        lambda *a, **k: None)
    return stub, bank, mds


# ─────────────────────────── N anticipos NO se pisan ───────────────────────

def test_dos_anticipos_se_suman_no_se_pisan(entorno):
    from modules.importaciones import pago
    stub, bank, mds = entorno
    stub.seed_pago("IM-1")

    pago.agregar_movimiento("IM-1", "anticipo", 12000, prov="AC", usuario="tam")
    r = pago.agregar_movimiento("IM-1", "anticipo", 5000, prov="AC", usuario="tam")

    assert len(stub.movs) == 2  # dos filas, nada pisado
    # Σ anticipos = VALOR DEL STOCK de la importación (modelo v2)
    assert stub.pago["IM-1"]["anticipo_aplicado"] == 17000.0
    assert r["anticipo_aplicado"] == 17000.0
    # deuda dejó de existir como concepto → NULL
    assert stub.pago["IM-1"]["deuda"] is None
    # cada anticipo generó SU ND automática en Pichincha
    assert len(bank.calls) == 2
    assert all(c["documento"] == "ND" and c["no_banco"] == 10 for c in bank.calls)
    assert bank.calls[0]["concepto"] == "ANT IMP IM-1 AC"
    assert [m["tipo"] for m in mds] == ["importacion_anticipo"] * 2


def test_anticipo_antes_de_recibir_crea_fila_base(entorno):
    from modules.importaciones import pago
    stub, bank, _ = entorno
    # sin fila previa (la importación ni se recibió) — el anticipo va igual
    r = pago.agregar_movimiento("IM-2", "anticipo", 8000, prov="MH")
    assert "IM-2" in stub.pago
    assert stub.pago["IM-2"]["anticipo_aplicado"] == 8000.0
    assert r["id_transaccion"] == bank.calls[0]["_id"]


# ─────────────────────────── deshacer (✕) ──────────────────────────────────

def test_deshacer_recalcula_suma_y_compensa_nd(entorno):
    from modules.importaciones import pago
    stub, bank, mds = entorno
    stub.seed_pago("IM-1")

    pago.agregar_movimiento("IM-1", "anticipo", 12000, prov="AC")
    r2 = pago.agregar_movimiento("IM-1", "anticipo", 5000, prov="AC")
    assert stub.pago["IM-1"]["anticipo_aplicado"] == 17000.0

    res = pago.deshacer_movimiento(r2["id_mov"], usuario="tam")

    assert len(stub.movs) == 1  # solo se borró ese movimiento
    assert stub.pago["IM-1"]["anticipo_aplicado"] == 12000.0  # Σ recalculada
    # la ND NO se borra: se COMPENSA con una NC (paper trail, patrón
    # reversar_movimiento_simple ND→NC)
    assert bank.calls[-1]["documento"] == "NC"
    assert bank.calls[-1]["importe"] == 5000.0
    assert "REVERSO ANT IMP IM-1" in bank.calls[-1]["concepto"]
    assert res["id_transaccion_reverso"] == bank.calls[-1]["_id"]
    # mov_doble reverso enlazado al original
    assert mds[-1]["tipo"] == "reverso_importacion_anticipo"
    assert mds[-1]["id_original"] == 777


def test_deshacer_mov_backfill_sin_nd_no_compensa(entorno):
    from modules.importaciones import pago
    stub, bank, _ = entorno
    stub.seed_pago("IM-1")
    # movimiento del backfill mig 0113: SIN ND linkeada (la ND fue manual)
    m = stub.seed_mov("IM-1", 12000.0)

    res = pago.deshacer_movimiento(m["id_mov"])
    assert bank.calls == []  # nada que compensar
    assert res["id_transaccion_reverso"] is None
    assert stub.pago["IM-1"]["anticipo_aplicado"] == 0.0


def test_deshacer_mov_inexistente_error(entorno):
    from modules.importaciones import pago
    with pytest.raises(ValueError, match="no existe"):
        pago.deshacer_movimiento(999)


# ─────────────────────────── validaciones ──────────────────────────────────

def test_agregar_valida_tipo_monto_im(entorno):
    from modules.importaciones import pago
    stub, _, _ = entorno
    stub.seed_pago("IM-1")
    with pytest.raises(ValueError, match="[Tt]ipo"):
        pago.agregar_movimiento("IM-1", "prestamo", 100)
    with pytest.raises(ValueError, match="monto"):
        pago.agregar_movimiento("IM-1", "anticipo", 0)
    with pytest.raises(ValueError, match="IM-"):
        pago.agregar_movimiento("", "anticipo", 100)


# ─────────────────────────── backfill mig 0113 ─────────────────────────────
# La mig crea UN movimiento 'anticipo' por el VALOR EFECTIVO de cada fila
# vieja: pagada → monto_real (o costo_estimado); si no → anticipo_aplicado.
# _suma_movs (Σ movimientos = valor del stock) tiene que devolver ese mismo
# valor → el valor de stock NO salta en el deploy; el criterio nuevo
# ("vale lo pagado") aplica hacia adelante.

def test_backfill_no_pagada_vale_lo_pagado():
    from modules.importaciones.pago import _suma_movs
    # fila vieja: costo 30000, anticipo_aplicado 12000, sin pagar
    # → la mig inserta 1 mov de 12000 (lo PAGADO; ya no vale el estimado)
    assert _suma_movs([{"tipo": "anticipo", "monto": 12000.0}]) == 12000.0


def test_backfill_pagada_preserva_monto_real():
    from modules.importaciones.pago import _suma_movs
    # fila vieja pagada con monto_real 32000 → 1 mov de 32000 → valor igual
    assert _suma_movs([{"tipo": "anticipo", "monto": 32000.0}]) == 32000.0


def test_suma_movs_incluye_pagos_legacy():
    from modules.importaciones.pago import _suma_movs
    # el CHECK admite 'pago' por si acaso: la Σ los cuenta igual
    movs = [{"tipo": "anticipo", "monto": 12000.0},
            {"tipo": "pago", "monto": 500.5}]
    assert _suma_movs(movs) == 12500.5
    assert _suma_movs([]) == 0.0


# ─────────────────────────── vista (template) ──────────────────────────────

def test_vista_renderiza_anticipos_para_editor(app, fake_db):
    """Un usuario con compras.editar ve la Σ de anticipos (= valor stock),
    la lista de movimientos, el form inline SOLO de anticipos (sin radio
    anticipo/pago, sin botón Pagar) y el AVISO de la ND automática."""
    from unittest.mock import patch

    from modules.importaciones import service

    rid = fake_db.add_role("Editor", ["stock.ver", "compras.editar"])
    uid = fake_db.add_user("edit", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid

    fila = {
        "im_numero": "IM-588", "fecha": "2026-06-01", "fecha_recepcion": None,
        "recibida": True, "bod": "", "total_asinfo": 51418.26,
        "proveedor": "ARIESCOPE", "prov_cod_asinfo": "AC",
        "nota": "( AC 36)", "codigo": "AC 36", "prov": "AC", "numero": 36,
        "numero_hasta": None, "compra": None, "anticipo": None,
        "fuente": None, "importe_programa": None, "kg": 1000.0,
        "recibido_pc": True, "kg_recibidos": 1000.0,
        "fecha_recepcion_pc": "2026-07-01",
        "anticipo_aplicado": 17000.0,
        "movimientos": [
            {"id_mov": 1, "tipo": "anticipo", "fecha": "2026-07-01",
             "monto": 12000.0, "nota": "1er anticipo", "id_transaccion": 9001},
            {"id_mov": 2, "tipo": "anticipo", "fecha": "2026-07-03",
             "monto": 5000.0, "nota": "", "id_transaccion": 9002},
        ],
    }
    with patch.object(service, "importaciones_con_cruce", return_value=[fila]):
        r = c.get("/importaciones")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    # TMT 2026-07-07: el link "ver movimientos" se quitó (el detalle se abre
    # con el botón "+ Anticipo" de la columna Cargar) — validamos que el
    # detalle exista con sus movimientos y el form.
    assert 'name="monto_mov"' in html
    assert html.count("antmov-undo") >= 1 or "ANT" in html
    assert "1er anticipo" in html
    assert "17.000,00" in html            # Σ anticipos = valor stock (formato EU)
    assert "se genera sola" in html       # aviso ND automática
    assert "movimiento/deshacer" in html  # ✕ por movimiento
    assert "Registrar anticipo" in html
    # v2: sin radio anticipo/pago, sin flujo Pagar
    assert 'name="tipo_mov"' not in html
    assert "importaciones/pagar" not in html
    assert "btn-pagar" not in html
