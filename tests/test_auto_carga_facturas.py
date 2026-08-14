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
                        lambda cli, u, numero=None, importe=None, id_direccion=None: (cli, False))
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
    # La devolución entra con kg/importe negativos y tipo 'D'.
    # TMT 2026-08-14: antes acá decía 'DE' — el importador guardaba
    # `tipo_asinfo[:2]` porque la columna era varchar(2), y ese recorte dejaba
    # la base escrita en dos idiomas (D/DE, N/NT, C/NC…) y metía
    # NC_FINANCIERA y NCNT en la misma "NC". Ahora se guarda el código del
    # vocabulario único (modules/facturas/tipo_doc.py).
    assert float(por_numf[502]["kg"]) < 0
    assert float(por_numf[502]["importe"]) < 0
    assert por_numf[502]["tipo"] == "D"
    # Retenciones de hoy aplicadas.
    assert res["ret"] == 2


def test_auto_carga_throttle(monkeypatch):
    # Si corrió hace <60s, no vuelve a correr (corrio=False).
    import time
    monkeypatch.setattr(fv, "_auto_carga_ultimo_ts", time.monotonic())
    res = fv._auto_cargar_facturas_hoy()
    assert res["corrio"] is False and res["cargadas"] == 0


def test_auto_carga_trae_nc_financiera(monkeypatch):
    """TMT 2026-07-28 (dueña, caso NC 11495 'no consta en el estado de cuenta').

    Las NC financieras (kg=0, importe negativo) entraban a PC por el sync del
    dBase; al pararse el 10/07 dejaron de entrar y el saldo del cliente quedó
    inflado por el descuento. Ahora las trae la auto-carga, como el dBase:
    fila propia con importe/saldo negativos.
    """
    from filters import today_ec
    hoy = today_ec()
    _reset_throttle(monkeypatch)

    asinfo_rows = [
        # NC financiera nueva → se carga.
        {"numero": "001-099-000011495", "tipo": "NC_FINANCIERA", "cliente_codigo": "RST",
         "kg": 0, "usd": -148.32, "fecha": hoy.isoformat()},
        # NC financiera que ya está en PC por numf (la trajo el sync del dBase,
        # que NO llena numf_completo) → NO se duplica.
        {"numero": "001-099-000011480", "tipo": "NC_FINANCIERA", "cliente_codigo": "AJ2",
         "kg": 0, "usd": -12.52, "fecha": hoy.isoformat()},
    ]
    from modules.asinfo import service as svc
    monkeypatch.setattr(svc, "facturas_periodo", lambda d, h: asinfo_rows)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [
        {"numf_completo": None, "numf": 11480},
    ])

    creadas = []
    monkeypatch.setattr(fq, "crear", lambda **kw: creadas.append(kw) or {"numf": kw.get("numf")})
    monkeypatch.setattr(fv, "_resolver_cliente_asinfo",
                        lambda cli, u, numero=None, importe=None, id_direccion=None: (cli, False))
    from modules.retenciones import queries as rq
    monkeypatch.setattr(rq, "aplicar_retenciones_asinfo",
                        lambda d, h, usuario="x": {"n_aplicadas": 0})
    from modules.facturas import vigia_anuladas as vig
    monkeypatch.setattr(vig, "correr_si_toca",
                        lambda: {"corrio": False, "anuladas": 0, "bloqueadas": 0})

    res = fv._auto_cargar_facturas_hoy()

    assert res["cargadas"] == 1
    assert len(creadas) == 1
    nc = creadas[0]
    assert nc["numf"] == 11495
    assert float(nc["importe"]) == -148.32   # negativa: baja el saldo del cliente
    assert float(nc["kg"]) == 0
    assert nc["tipo"] == "NC"
    assert nc["usuario"] == "asinfo-carga"   # cuenta en cartera
