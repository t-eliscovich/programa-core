"""El devengo YY/RT ya no depende de que alguien abra /posdat (Tamara 2026-09-25).

  1. El UPDATE del motor va con el baseline leído como candado: si otro
     proceso ya devengó la fila (rowcount 0), no se cuenta ni se suma dos veces.
  2. El calentador del servidor lo corre una vez por día y reintenta si falla.
  3. La foto de la noche devenga antes de fotografiar y avisa si quedó alguna
     provisión sin devengar.
"""
from datetime import date

from modules.posdat import queries as pq


def _stub_persist(monkeypatch, rowcount):
    import db as _db
    updates = []
    rows = [{"id_posdat": 7, "prov": "YY", "concepto": "SUELDOS",
             "importe": 1000.0, "baseline_date": date(2026, 9, 24)}]

    def fake_fetch_all(sql, params=None, **kw):
        if "FROM scintela.posdat" in sql:
            return [dict(r) for r in rows]
        return []

    def fake_execute(sql, params=None, **kw):
        updates.append((sql, params))
        return rowcount

    monkeypatch.setattr(_db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(_db, "fetch_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setattr(pq, "_resolver_cuotas",
                        lambda rs: [r.__setitem__("cuota_mensual", 130500.0) for r in rs])
    return updates


def test_update_lleva_el_baseline_leido_como_candado(monkeypatch):
    updates = _stub_persist(monkeypatch, rowcount=1)
    n = pq.persistir_acumulacion_yy(date(2026, 9, 25))
    assert n == 1
    sql, params = updates[0]
    assert "AND baseline_date = %s" in sql
    assert params[1] == date(2026, 9, 25)
    assert params[3] == date(2026, 9, 24)
    assert params[0] == round(1000.0 + 130500.0 / 30, 2)


def test_si_otro_ya_devengo_no_se_cuenta(monkeypatch):
    _stub_persist(monkeypatch, rowcount=0)
    assert pq.persistir_acumulacion_yy(date(2026, 9, 25)) == 0


def test_calentador_devenga_una_vez_por_dia_y_reintenta(monkeypatch):
    import filters
    from modules._lib import warmup
    llamadas = []
    dia = {"hoy": date(2026, 9, 25)}
    monkeypatch.setattr(filters, "today_ec", lambda: dia["hoy"])
    falla = {"si": True}

    def fake_persist(hoy=None):
        llamadas.append(hoy)
        if falla["si"]:
            raise RuntimeError("base caída")
        return 12

    monkeypatch.setattr(pq, "persistir_acumulacion_yy", fake_persist)
    monkeypatch.setattr(warmup, "_devengo_hecho", None)
    assert warmup.devengar_posdatados_del_dia() is False      # falló
    falla["si"] = False
    assert warmup.devengar_posdatados_del_dia() is True       # reintenta
    assert warmup.devengar_posdatados_del_dia() is False      # ya hecho hoy
    dia["hoy"] = date(2026, 9, 26)
    assert warmup.devengar_posdatados_del_dia() is True       # día nuevo
    assert llamadas == [date(2026, 9, 25)] * 2 + [date(2026, 9, 26)]


def test_el_loop_del_calentador_devenga_antes_de_calentar():
    import inspect

    from modules._lib import warmup
    src = inspect.getsource(warmup._loop)
    assert src.index("devengar_posdatados_del_dia()") < src.index("_warm_once()")


def test_la_foto_de_la_noche_devenga_antes_de_fotografiar():
    import inspect

    from modules.admin_dbase import health_audit_view as h
    src = inspect.getsource(h.ejecutar_foto_diaria)
    assert src.index("persistir_acumulacion_yy()") < src.index("crear_snapshot_diario()\n")
    assert "quedaron sin devengar" in src
