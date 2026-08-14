"""Reversar y Eliminar por error de carga son DOS operaciones, no una.

TMT 2026-08-14 (dueña): *"vuelve a cartera Z y eliminado, no es lo mismo"* y
*"no quería que vuelva a cartera, quería directamente eliminarlo. error de
carga. pensá que se cargó directo como un depósito"*.

Hasta hoy la fila del banco tenía UN botón (🗑) que hacía las dos cosas según
lo que colgara de la fila, y el nombre mentía en la mitad de los casos:

  ↺ Reversar        el cheque EXISTE y estaba en cartera; se depositó mal.
                    Vuelve a cartera (Z) y sigue vivo. (= la ruta vieja)
  🗑 Error de carga  el cheque NACIÓ como este depósito (`crear()` con no_banco
                    90/91 lo deja en B sin pasar por cartera) y no existió
                    nunca. Mandarlo "de vuelta" a cartera le inventa un pasado
                    y deja un cheque fantasma en la cartera y en las
                    comisiones. Se anula CON el movimiento.

Lo que este test cuida y no se ve leyendo el happy path:

1. **La ND que no va.** `anular_por_error_de_carga` compensa un cheque
   depositado insertando una nota de débito, porque su caso normal es que el
   depósito NO se pueda tocar. Acá el depósito se BORRA, así que la ND movería
   el saldo dos veces y hablaría de un movimiento que ya no está.
2. **El orden.** Anular va ANTES de soltar los enlaces cheque↔transacción: la
   función los mira para el freno de conciliado (el del 05/08, caso MSS).
3. **Los frenos** siguen siendo los mismos que los del reverso.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NO_BANCO = 10
ID_TX = 45559
ID_CHEQUE = 7001


# ── 1) el motor: la compensación que no va ───────────────────────────────────

def test_anular_sin_compensacion_no_toca_el_banco(monkeypatch):
    """Con `sin_compensacion_bancaria`, un cheque depositado se anula SIN
    insertar la ND. Sin esta bandera el saldo se movía dos veces."""
    from modules.cheques import queries as cq

    insertados = []

    class _FakeBank:
        @staticmethod
        def insert_movimiento_bancario(conn, **kw):  # pragma: no cover - guard
            insertados.append(kw)
            return {"id_transaccion": 1}

        @staticmethod
        def recompute_saldos_desde(*a, **k):
            return None

    monkeypatch.setitem(sys.modules, "bank_helpers", _FakeBank)
    _stub_db_para_anular(monkeypatch, cq)

    res = cq.anular_por_error_de_carga(
        ID_CHEQUE, motivo="cargado dos veces", usuario="tamara",
        sin_compensacion_bancaria=True, conn=object(),
    )
    assert insertados == [], "insertó una ND compensatoria y no debía"
    assert res["compensacion"]["omitida"] is True


def test_sin_la_bandera_la_ND_SIGUE_saliendo(monkeypatch):
    """El camino de /cheques no cambia: ahí el depósito no se borra, así que
    la compensación es la que mantiene el saldo bien."""
    from modules.cheques import queries as cq

    insertados = []

    class _FakeBank:
        @staticmethod
        def insert_movimiento_bancario(conn, **kw):
            insertados.append(kw)
            return {"id_transaccion": 1}

        @staticmethod
        def recompute_saldos_desde(*a, **k):
            return None

    monkeypatch.setitem(sys.modules, "bank_helpers", _FakeBank)
    _stub_db_para_anular(monkeypatch, cq)

    cq.anular_por_error_de_carga(
        ID_CHEQUE, motivo="x", usuario="tamara", conn=object(),
    )
    assert len(insertados) == 1
    assert insertados[0]["documento"] == "ND"


def _stub_db_para_anular(monkeypatch, cq):
    """Cheque B de 130 sin facturas aplicadas, sin conciliar, sin posdat."""
    def fetch_one(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "FROM scintela.cheque WHERE id_cheque" in s:
            return {"id_cheque": ID_CHEQUE, "no_cheque": "102081", "stat": "B",
                    "codigo_cli": "MTM", "importe": 130.0, "no_banco": NO_BANCO,
                    "fechad": None}
        return None

    def fetch_all(sql, params=None, conn=None):
        return []

    monkeypatch.setattr(cq.db, "fetch_one", fetch_one)
    monkeypatch.setattr(cq.db, "fetch_all", fetch_all)
    monkeypatch.setattr(cq.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(cq.db, "execute_returning", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **k: None)
    import mov_doble as _md
    monkeypatch.setattr(_md, "registrar", lambda *a, **k: 1)


# ── 2) la ruta ───────────────────────────────────────────────────────────────

@pytest.fixture
def pantalla(app, monkeypatch):
    """La vista con la base fingida. Devuelve (client, log) donde `log` guarda
    en orden lo que se ejecutó."""
    import db as _db
    from modules.bancos import views as bv

    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    log: list[str] = []
    estado = {"conciliado": False, "mov_doble": False, "stat_cheque": "B",
              "usuario_crea": "alex"}

    def fetch_one(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "FROM scintela.transacciones_bancarias t" in s and "LEFT JOIN scintela.banco" in s:
            return {"id_transaccion": ID_TX, "no_banco": NO_BANCO,
                    "fecha": "2026-08-07", "documento": "DE", "importe": 130.0,
                    "concepto": "1 ch.MTM", "usuario_crea": estado["usuario_crea"],
                    "banco_nombre": "PICHINCHA"}
        if "banco_conciliacion_match" in s:
            return {"x": 1} if estado["conciliado"] else None
        if "mov_doble" in s:
            return {"id_mov_doble": 5} if estado["mov_doble"] else None
        if "FROM scintela.cheque WHERE id_cheque" in s:
            return {"id_cheque": ID_CHEQUE, "no_cheque": "102081",
                    "stat": estado["stat_cheque"], "importe": 130.0,
                    "codigo_cli": "MTM"}
        if "ORDER BY fecha ASC" in s:
            return {"ancla": 1}
        return None

    def fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "FROM scintela.chequextransaccion" in s:
            return [{"id_cheque": ID_CHEQUE}]
        return []

    def execute(sql, params=None, conn=None):
        s = " ".join(sql.split())
        if "DELETE FROM scintela.chequextransaccion" in s:
            log.append("soltar-enlaces")
        elif "INSERT INTO scintela.papelera_movimiento_banco" in s:
            log.append("papelera")
        elif "DELETE FROM scintela.transacciones_bancarias" in s:
            log.append("borrar-mov")
        return 1

    import contextlib

    monkeypatch.setattr(_db, "fetch_one", fetch_one)
    monkeypatch.setattr(_db, "fetch_all", fetch_all)
    monkeypatch.setattr(_db, "execute", execute)
    monkeypatch.setattr(_db, "tx", lambda: contextlib.nullcontext(object()))

    from modules.cheques import queries as cq

    def fake_anular(id_cheque, **kw):
        log.append(f"anular:{id_cheque}:"
                   f"{'sin-nd' if kw.get('sin_compensacion_bancaria') else 'con-nd'}")
        return {"ok": True}

    monkeypatch.setattr(cq, "anular_por_error_de_carga", fake_anular)
    monkeypatch.setattr(bv, "flash_exc",
                        lambda msg, e: log.append(f"EXPLOTO:{msg}:{e}"))

    import bank_helpers
    monkeypatch.setattr(bank_helpers, "recompute_saldos_desde",
                        lambda *a, **k: log.append("recompute"))

    return app.test_client(), log, estado


URL = f"/bancos/{NO_BANCO}/tx/{ID_TX}/error-carga"


def test_la_pantalla_de_confirmacion_abre_y_nombra_el_cheque(pantalla):
    client, log, _ = pantalla
    r = client.get(URL)
    assert r.status_code == 200
    html = r.data.decode()
    assert "error de carga" in html.lower()
    assert "102081" in html
    assert log == [], "un GET no puede tocar nada"


def test_anula_el_cheque_SIN_nota_de_debito(pantalla):
    """El corazón del pedido: se elimina, no se compensa."""
    client, log, _ = pantalla
    r = client.post(URL, data={"motivo": "se cargó dos veces"})
    assert r.status_code == 302
    assert f"anular:{ID_CHEQUE}:sin-nd" in log


def test_anula_ANTES_de_soltar_los_enlaces(pantalla):
    """`anular_por_error_de_carga` mira chequextransaccion para el freno de
    conciliado (05/08, caso MSS). Si los enlaces ya no están, el freno no ve
    nada y deja pasar la anulación de un depósito ya conciliado."""
    client, log, _ = pantalla
    client.post(URL, data={})
    assert log.index(f"anular:{ID_CHEQUE}:sin-nd") < log.index("soltar-enlaces")


def test_guarda_la_copia_ANTES_de_borrar_el_movimiento(pantalla):
    """Sin snapshot previo la papelera queda vacía: el SELECT de to_jsonb lee
    la fila que el DELETE ya se llevó."""
    client, log, _ = pantalla
    client.post(URL, data={})
    assert log.index("papelera") < log.index("borrar-mov")


def test_recalcula_el_saldo_al_final(pantalla):
    client, log, _ = pantalla
    client.post(URL, data={})
    assert log.index("borrar-mov") < log.index("recompute")


def test_un_movimiento_del_dbase_no_se_toca(pantalla):
    client, log, estado = pantalla
    estado["usuario_crea"] = "dbf-import"
    r = client.post(URL, data={})
    assert r.status_code == 302
    assert log == [], "no puede haber tocado nada"


def test_un_movimiento_conciliado_no_se_toca(pantalla):
    """Mismo freno que el reverso: primero se deshace la conciliación."""
    client, log, estado = pantalla
    estado["conciliado"] = True
    client.post(URL, data={})
    assert log == []


def test_un_movimiento_con_reverso_propio_no_se_toca(pantalla):
    client, log, estado = pantalla
    estado["mov_doble"] = True
    client.post(URL, data={})
    assert log == []


def test_un_cheque_ya_cerrado_solo_se_desengancha(pantalla):
    """stat X/T/R: volver a anularlo haría levantar a la función. Se suelta el
    enlace y el movimiento se va igual."""
    client, log, estado = pantalla
    estado["stat_cheque"] = "X"
    client.post(URL, data={})
    assert not any(x.startswith("anular:") for x in log)
    assert "borrar-mov" in log


# ── 3) la fila: dos botones, cada uno cuando aplica ──────────────────────────

def test_los_dos_botones_son_rutas_DISTINTAS(app):
    """El pedido era separarlas. Si mañana alguien vuelve a apuntar los dos al
    mismo endpoint, el ↺ vuelve a mentir."""
    rutas = {r.endpoint: str(r) for r in app.url_map.iter_rules()
             if r.endpoint.startswith("bancos.")}
    assert rutas["bancos.eliminar_error_carga"] != rutas["bancos.eliminar_movimiento_pc"]
