"""Vigía de facturas ANULADAS en Asinfo (TMT 2026-07-28, caso 180441).

Propiedad crítica: es FAIL-CLOSED. Si Asinfo no contesta, o si el día no tiene
datos, o si la fila no la trajo la auto-carga, NO se anula nada. Anular de más
(borrar una venta real de la cartera) es mucho peor que anular de menos.
"""
from datetime import timedelta

import db
from filters import today_ec
from modules.asinfo import service as svc
from modules.facturas import queries as fq
from modules.facturas import vigia_anuladas as vig


def _sri(n: int) -> str:
    return f"001-099-{n:09d}"


def _pc(numf, *, usuario="asinfo-carga", fecha=None, cli="DSS", importe=100.0,
        abono=0.0):
    return {
        "id_factura": numf, "numf": numf, "numf_completo": _sri(numf),
        "fecha": fecha or today_ec(), "codigo_cli": cli, "importe": importe,
        "abono": abono, "saldo": importe - abono, "stat": "Z",
        "usuario_crea": usuario,
    }


def _asinfo(numf, fecha=None):
    return {"numero": _sri(numf), "tipo": "FACTURA", "cliente_codigo": "DSS",
            "kg": 10, "usd": 100.0,
            "fecha": (fecha or today_ec()).isoformat()}


def _stub(monkeypatch, asinfo_rows, pc_rows):
    monkeypatch.setattr(svc, "facturas_periodo", lambda d, h: asinfo_rows)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: pc_rows)


def test_anula_la_que_asinfo_ya_no_reporta(monkeypatch):
    """El caso real: 180441 cargada por la auto-carga y anulada después."""
    _stub(monkeypatch, [_asinfo(180449)], [_pc(180441), _pc(180449)])
    anuladas = []
    monkeypatch.setattr(
        fq, "anular",
        lambda idf, motivo=None, usuario=None: anuladas.append((idf, usuario)) or 1,
    )

    res = vig.correr()

    assert res["ok"] is True
    assert [f["numf"] for f in res["para_anular"]] == [180441]
    assert anuladas == [(180441, vig.USUARIO_VIGIA)]
    assert len(res["anuladas"]) == 1
    assert res["bloqueadas"] == []


def test_asinfo_mudo_no_toca_nada(monkeypatch):
    """Guard 1: sin datos de Asinfo no se puede concluir que algo esté anulado."""
    _stub(monkeypatch, [], [_pc(180441)])
    monkeypatch.setattr(fq, "anular", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no debe anular nada")))

    res = vig.correr()

    assert res["ok"] is False
    assert res["para_anular"] == []
    assert res["anuladas"] == []


def test_asinfo_explota_no_toca_nada(monkeypatch):
    def _boom(d, h):
        raise RuntimeError("metabase caído")
    monkeypatch.setattr(svc, "facturas_periodo", _boom)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [_pc(180441)])

    res = vig.correr()

    assert res["ok"] is False
    assert "metabase" in res["motivo"]
    assert res["anuladas"] == []


def test_dia_sin_datos_en_asinfo_se_saltea(monkeypatch):
    """Guard 3: si Asinfo no trajo NADA de ese día, el día no se juzga."""
    hoy = today_ec()
    ayer = hoy - timedelta(days=1)
    _stub(monkeypatch, [_asinfo(180449, hoy)], [_pc(180441, fecha=ayer)])
    monkeypatch.setattr(fq, "anular", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no debe anular nada")))

    res = vig.correr()

    assert res["ok"] is True
    assert res["para_anular"] == []


def test_otro_origen_se_sugiere_pero_no_se_anula(monkeypatch):
    """Guard 2: lo tipeado a mano o venido del dBase lo decide una persona."""
    _stub(monkeypatch, [_asinfo(180449)],
          [_pc(180441, usuario="andres"), _pc(180442, usuario="dbf-import")])
    monkeypatch.setattr(fq, "anular", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no debe anular nada")))

    res = vig.correr()

    assert res["para_anular"] == []
    assert {f["numf"] for f in res["sugeridas"]} == {180441, 180442}


def test_sin_numero_sri_se_ignora(monkeypatch):
    """Guard 5: sin N° SRI no hay con qué comparar."""
    fila = _pc(180441)
    fila["numf_completo"] = None
    _stub(monkeypatch, [_asinfo(180449)], [fila])

    res = vig.correr()

    assert res["para_anular"] == []


def test_bloqueada_si_tiene_cobranza(monkeypatch):
    """Guard 4: `queries.anular` manda — si hay cheques/retenciones, a mano."""
    _stub(monkeypatch, [_asinfo(180449)], [_pc(180441, abono=50.0)])

    def _anular(idf, motivo=None, usuario=None):
        raise ValueError("hay cheques aplicados ACTIVOS a esta factura")
    monkeypatch.setattr(fq, "anular", _anular)

    res = vig.correr()

    assert res["anuladas"] == []
    assert len(res["bloqueadas"]) == 1
    assert "cheques" in res["bloqueadas"][0]["motivo"]


def test_dry_run_no_escribe(monkeypatch):
    _stub(monkeypatch, [_asinfo(180449)], [_pc(180441)])
    monkeypatch.setattr(fq, "anular", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("dry-run no debe escribir")))

    res = vig.correr(dry_run=True)

    assert len(res["para_anular"]) == 1
    assert res["anuladas"] == []


def test_correr_si_toca_respeta_throttle_y_apagado(monkeypatch):
    _stub(monkeypatch, [_asinfo(180449)], [_pc(180441)])
    monkeypatch.setattr(fq, "anular", lambda *a, **k: 1)

    monkeypatch.setenv("VIGIA_ANULADAS", "0")
    assert vig.correr_si_toca() == {"corrio": False, "anuladas": 0, "bloqueadas": 0}

    monkeypatch.setenv("VIGIA_ANULADAS", "1")
    # _ultimo_ts MUY en el pasado (no 0.0). El throttle usa time.monotonic();
    # en un runner/contenedor recién arrancado monotonic() < 900s (el intervalo),
    # y con _ultimo_ts=0.0 `ahora - 0 < 900` daba True → no corría y el test
    # fallaba de forma intermitente. Con un sentinel negativo grande la
    # diferencia siempre supera el intervalo → corre determinísticamente.
    monkeypatch.setattr(vig, "_ultimo_ts", -1.0e9)
    primera = vig.correr_si_toca()
    assert primera["corrio"] is True
    assert primera["anuladas"] == 1
    # Segunda corrida inmediata: el throttle la saltea.
    assert vig.correr_si_toca()["corrio"] is False


def test_ventana_y_envs(monkeypatch):
    monkeypatch.setenv("VIGIA_ANULADAS_DIAS", "3")
    assert vig.dias_ventana() == 3
    monkeypatch.setenv("VIGIA_ANULADAS_DIAS", "no-es-un-numero")
    assert vig.dias_ventana() == 7
    monkeypatch.setenv("VIGIA_ANULADAS_DIAS", "999")
    assert vig.dias_ventana() == 60
    monkeypatch.setenv("VIGIA_ANULADAS_SECS", "10")
    assert vig._intervalo_secs() == 60  # nunca por debajo del piso
    monkeypatch.setenv("VIGIA_ANULADAS_SECS", "x")
    assert vig._intervalo_secs() == 900

    _stub(monkeypatch, [_asinfo(180449)], [])
    monkeypatch.setenv("VIGIA_ANULADAS_DIAS", "5")
    res = vig.detectar()
    assert (res["hasta"] - res["desde"]).days == 4


def test_vigia_corre_aunque_asinfo_no_tenga_facturas_de_hoy(monkeypatch):
    """TMT 2026-07-28: a la mañana temprano Asinfo todavía no emitió nada.

    Antes `_auto_cargar_facturas_hoy` cortaba ahí y el vigía no corría hasta
    la primera factura del día. Ahora el ciclo sigue.
    """
    from modules.facturas import views as fv

    monkeypatch.setattr(fv, "_auto_carga_ultimo_ts", 0.0)
    monkeypatch.setattr(svc, "facturas_periodo", lambda d, h: [])
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [])
    llamado = {"n": 0}

    def _vigia():
        llamado["n"] += 1
        return {"corrio": True, "anuladas": 3, "bloqueadas": 1}
    monkeypatch.setattr(vig, "correr_si_toca", _vigia)

    res = fv._auto_cargar_facturas_hoy()

    assert llamado["n"] == 1
    assert res["anuladas"] == 3
    assert res["anuladas_bloqueadas"] == 1
