"""Auto-carga de facturas del día (TMT 2026-07-21).

Propiedad crítica de seguridad: al correr en un GET, NO puede duplicar. Solo
crea las facturas de HOY cuyo N° SRI no exista ya en PC (bajo ningún cliente).
"""
import db
from modules.facturas import queries as fq
from modules.facturas import views as fv


def _reset_throttle(monkeypatch):
    # El throttle global usa time.monotonic(); reseteamos el último ts para que
    # la corrida no se saltee.
    monkeypatch.setattr(fv, "_auto_carga_ultimo_ts", 0.0)


def test_auto_carga_no_duplica_ni_carga_dias_previos(monkeypatch):
    from filters import today_ec
    hoy = today_ec()
    _reset_throttle(monkeypatch)

    # Asinfo de hoy: una YA cargada en PC (numf 500), una que FALTA (numf 501),
    # una DEVOLUCION de mercadería de hoy (numf 502 — AHORA se carga, TMT
    # 2026-07-26 dueña: "que carguen en el automático") y una de OTRA fecha
    # (se ignora).
    asinfo_rows = [
        {"numero": "001-099-000000500", "tipo": "FACTURA", "cliente_codigo": "KAM",
         "kg": 10, "usd": 100.0, "fecha": hoy.isoformat()},
        {"numero": "001-099-000000501", "tipo": "FACTURA", "cliente_codigo": "EDU",
         "kg": 20, "usd": 200.0, "fecha": hoy.isoformat()},
        {"numero": "001-099-000000502", "tipo": "DEVOLUCION", "cliente_codigo": "EDU",
         "kg": -5, "usd": -50.0, "fecha": hoy.isoformat()},
        {"numero": "001-099-000000499", "tipo": "FACTURA", "cliente_codigo": "EDU",
         "kg": 30, "usd": 300.0, "fecha": "2026-07-01"},
    ]
    from modules.asinfo import service as svc
    monkeypatch.setattr(svc, "facturas_periodo", lambda d, h: asinfo_rows)

    # PC de hoy: ya tiene la 500 (bajo cliente 'KM', distinto de Asinfo 'KAM',
    # numf_completo NULL — el caso que ANTES se duplicaba).
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [
        {"numf_completo": None, "numf": 500},
    ])

    creadas = []
    monkeypatch.setattr(fq, "crear", lambda **kw: creadas.append(kw) or {"numf": kw.get("numf")})
    monkeypatch.setattr(fv, "_resolver_cliente_asinfo",
                        lambda cli, u, numero=None, importe=None: (cli, False))
    from modules.retenciones import queries as rq
    monkeypatch.setattr(rq, "aplicar_retenciones_asinfo",
                        lambda d, h, usuario="x": {"n_aplicadas": 2})

    res = fv._auto_cargar_facturas_hoy()

    # La 501 (FACTURA que falta, de hoy) y la 502 (DEVOLUCION de mercadería de
    # hoy) se crean. La 500 ya está (dedupe por numf), la 499 es de otra fecha.
    assert res["cargadas"] == 2
    assert len(creadas) == 2
    por_numf = {c["numf"]: c for c in creadas}
    assert set(por_numf) == {501, 502}
    assert por_numf[501]["usuario"] == "asinfo-carga"  # cuenta en cartera
    # La devolución entra con kg/importe negativos y tipo 'DE'.
    assert float(por_numf[502]["kg"]) < 0
    assert float(por_numf[502]["importe"]) < 0
    assert por_numf[502]["tipo"] == "DE"
    # Retenciones de hoy aplicadas.
    assert res["ret"] == 2


def test_auto_carga_throttle(monkeypatch):
    # Si corrió hace <60s, no vuelve a correr (corrio=False).
    import time
    monkeypatch.setattr(fv, "_auto_carga_ultimo_ts", time.monotonic())
    res = fv._auto_cargar_facturas_hoy()
    assert res["corrio"] is False and res["cargadas"] == 0
