"""El devengo YY/RT ya no depende de que alguien abra /posdat (Tamara 2026-09-25).

  1. El UPDATE del motor va con el baseline leído como candado: si otro
     proceso ya devengó la fila (rowcount 0), no se cuenta ni se suma dos veces.
  2. El calentador del servidor lo corre una vez por día y reintenta si falla.
  3. El día 1 no se guarda el devengo hasta que esté la foto de cierre del mes
     anterior (el cron de las 06:00 EC la saca en vivo), o hasta el mediodía.
  4. La foto de la noche devenga antes de fotografiar y avisa si quedó alguna
     provisión CON cuota sin devengar.
"""
from datetime import date, datetime

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


# ─── 1. candado ──────────────────────────────────────────────────────────────

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


# ─── 2/3. día 1 y la foto de cierre ─────────────────────────────────────────

def _cierre(monkeypatch, hecho):
    import db as _db
    vistos = []

    def fake_fetch_one(sql, params=None, **kw):
        vistos.append(params)
        return {"ok": 1} if hecho else None

    monkeypatch.setattr(_db, "fetch_one", fake_fetch_one)
    return vistos


def test_dia_comun_no_espera_nada(monkeypatch):
    vistos = _cierre(monkeypatch, hecho=False)
    assert pq.cierre_del_mes_pendiente(date(2026, 10, 2)) is False
    assert vistos == []  # ni consulta la base


def test_dia_1_sin_foto_de_cierre_espera(monkeypatch):
    vistos = _cierre(monkeypatch, hecho=False)
    # 03:00 EC = 08:00 UTC del 01/10
    assert pq.cierre_del_mes_pendiente(date(2026, 10, 1), datetime(2026, 10, 1, 8, 0)) is True
    assert vistos == [("2026-10",)]


def test_dia_1_con_foto_de_cierre_devenga(monkeypatch):
    _cierre(monkeypatch, hecho=True)
    assert pq.cierre_del_mes_pendiente(date(2026, 10, 1), datetime(2026, 10, 1, 12, 0)) is False


def test_dia_1_despues_del_mediodia_devenga_aunque_falte_el_cierre(monkeypatch):
    _cierre(monkeypatch, hecho=False)
    # 12:00 EC = 17:00 UTC
    assert pq.cierre_del_mes_pendiente(date(2026, 10, 1), datetime(2026, 10, 1, 17, 0)) is False


def test_dia_1_sin_tabla_no_frena(monkeypatch):
    import db as _db

    def boom(*a, **k):
        raise RuntimeError("no existe la tabla")

    monkeypatch.setattr(_db, "fetch_one", boom)
    assert pq.cierre_del_mes_pendiente(date(2026, 10, 1), datetime(2026, 10, 1, 8, 0)) is False


def test_motor_no_guarda_el_dia_1_antes_del_cierre(monkeypatch):
    updates = _stub_persist(monkeypatch, rowcount=1)
    monkeypatch.setattr(pq, "_hoy_ec", lambda: date(2026, 10, 1))
    monkeypatch.setattr(pq, "cierre_del_mes_pendiente", lambda hoy, ahora_utc=None: True)
    assert pq.persistir_acumulacion_yy() == 0
    assert updates == []


def test_motor_con_fecha_explicita_no_mira_el_cierre(monkeypatch):
    _stub_persist(monkeypatch, rowcount=1)
    monkeypatch.setattr(pq, "_hoy_ec", lambda: date(2026, 9, 25))
    monkeypatch.setattr(pq, "cierre_del_mes_pendiente",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))
    assert pq.persistir_acumulacion_yy(date(2026, 10, 1)) == 1


def test_calentador_devenga_una_vez_por_dia_y_reintenta(monkeypatch):
    import filters
    from modules._lib import warmup
    llamadas = []
    dia = {"hoy": date(2026, 9, 30)}
    monkeypatch.setattr(filters, "today_ec", lambda: dia["hoy"])
    estado = {"falla": True, "cierre_pendiente": False}

    def fake_persist(hoy=None):
        llamadas.append(dia["hoy"])
        if estado["falla"]:
            raise RuntimeError("base caída")
        return 12

    monkeypatch.setattr(pq, "persistir_acumulacion_yy", fake_persist)
    monkeypatch.setattr(pq, "cierre_del_mes_pendiente",
                        lambda hoy, ahora_utc=None: estado["cierre_pendiente"])
    monkeypatch.setattr(warmup, "_devengo_hecho", None)
    assert warmup.devengar_posdatados_del_dia() is False      # falló
    estado["falla"] = False
    assert warmup.devengar_posdatados_del_dia() is True       # reintenta
    assert warmup.devengar_posdatados_del_dia() is False      # ya hecho hoy
    dia["hoy"] = date(2026, 10, 1)
    estado["cierre_pendiente"] = True
    assert warmup.devengar_posdatados_del_dia() is False      # espera el cierre
    assert warmup.devengar_posdatados_del_dia() is False      # sigue esperando
    estado["cierre_pendiente"] = False
    assert warmup.devengar_posdatados_del_dia() is True       # ya se cerró
    assert llamadas == [date(2026, 9, 30)] * 2 + [date(2026, 10, 1)]


def test_el_loop_del_calentador_devenga_antes_de_calentar():
    import inspect

    from modules._lib import warmup
    src = inspect.getsource(warmup._loop)
    assert src.index("devengar_posdatados_del_dia()") < src.index("_warm_once()")


# ─── 4. la foto de la noche ─────────────────────────────────────────────────

def _foto(monkeypatch, filas, cuotas, cierre_pendiente=False):
    import db as _db
    from modules.admin_dbase import health_audit_view as h
    from modules.informes import queries as iq

    monkeypatch.setattr(iq, "rollover_y_writeback_iniciales", lambda: {})
    monkeypatch.setattr(iq, "crear_snapshot_diario",
                        lambda: {"fecha": "2026-09-25", "patrimonio": 1.0, "ustock": 1.0})
    monkeypatch.setattr(pq, "persistir_acumulacion_yy", lambda hoy=None: 0)
    monkeypatch.setattr(pq, "_hoy_ec", lambda: date(2026, 9, 25))
    monkeypatch.setattr(pq, "cierre_del_mes_pendiente", lambda hoy, ahora_utc=None: cierre_pendiente)
    monkeypatch.setattr(pq, "_resolver_cuotas",
                        lambda rs: [r.__setitem__("cuota_mensual", cuotas.get(r["id_posdat"], 0)) for r in rs])
    monkeypatch.setattr(_db, "fetch_all", lambda sql, params=None, **k: [dict(f) for f in filas])
    monkeypatch.setattr(_db, "fetch_one", lambda *a, **k: None)
    return h.ejecutar_foto_diaria()


def _alertas_posdat(res):
    return [a for a in res["alerts"] if "POSDATADOS" in str(a) or "devengo" in str(a)]


def test_foto_avisa_si_una_provision_con_cuota_quedo_sin_devengar(monkeypatch):
    res = _foto(monkeypatch,
                [{"id_posdat": 1, "prov": "YY", "concepto": "SUELDOS",
                  "importe": 1.0, "baseline_date": date(2026, 9, 24)}],
                {1: 130500.0})
    al = _alertas_posdat(res)
    assert len(al) == 1 and "SUELDOS" in al[0]


def test_foto_no_avisa_por_una_fila_sin_cuota(monkeypatch):
    res = _foto(monkeypatch,
                [{"id_posdat": 1, "prov": "YY", "concepto": "VIEJA",
                  "importe": 1.0, "baseline_date": date(2026, 9, 1)},
                 {"id_posdat": 2, "prov": "RT", "concepto": "",
                  "importe": 1.0, "baseline_date": date(2026, 9, 25)}],
                {1: 0, 2: 182700.0})
    assert _alertas_posdat(res) == []


def test_foto_no_avisa_el_dia_1_antes_del_cierre(monkeypatch):
    res = _foto(monkeypatch,
                [{"id_posdat": 1, "prov": "YY", "concepto": "SUELDOS",
                  "importe": 1.0, "baseline_date": date(2026, 9, 24)}],
                {1: 130500.0}, cierre_pendiente=True)
    assert _alertas_posdat(res) == []


def test_foto_sigue_aunque_el_devengo_falle(monkeypatch):
    from modules.admin_dbase import health_audit_view as h
    from modules.informes import queries as iq

    monkeypatch.setattr(iq, "rollover_y_writeback_iniciales", lambda: {})
    monkeypatch.setattr(iq, "crear_snapshot_diario",
                        lambda: {"fecha": "2026-09-25", "patrimonio": 1.0, "ustock": 1.0})

    def boom(hoy=None):
        raise RuntimeError("base caída")

    monkeypatch.setattr(pq, "persistir_acumulacion_yy", boom)
    import db as _db
    monkeypatch.setattr(_db, "fetch_one", lambda *a, **k: None)
    res = h.ejecutar_foto_diaria()
    assert any("devengo de posdatados" in str(a) for a in res["alerts"])
    assert res["stats"]["hoy"]["patrimonio"] == 1.0
