"""El resumen de cobranza del día ya no miente con "sin aplicar a facturas".

TMT 2026-08-04. Alex, con la tirilla del 03/08 en la mano: *"en el caso de MTM
los dos fueron abonos a factura, pero refleja sin aplicar facturas … CEM
corresponde a anticipos para pago de un cheque … JOH igualmente anticipo para
pago de cheque … esto se entrega a contabilidad y no van a saber qué hacer con
el 'sin aplicar facturas'"*.

Los tres casos que marcó ya estaban explicados en `mov_doble`; la impresión
leía sólo `chequesxfact`. Los datos de estos tests son los REALES de
producción del 03/08 (ids, importes y metadata verificados contra prod).
"""
from __future__ import annotations

from datetime import date

from modules.cheques import concepto_cobro


class _FakeDB:
    """Devuelve filas fijas según qué tabla menciona el SQL."""

    def __init__(self, movs, cheques=None, totalizar=None, facturas=None):
        self.movs, self.cheques, self.totalizar = movs, cheques or [], totalizar or []
        self.facturas = facturas or []
        self.sqls: list[str] = []

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        self.sqls.append(s)
        if "totalizar_estado_cuenta" in s:
            return self.totalizar
        if "from scintela.mov_doble" in s:
            return self.movs
        if "from scintela.factura" in s:
            return self.facturas
        if "from scintela.cheque" in s:
            return self.cheques
        return []

    def execute(self, sql, params=None, conn=None):  # bootstrap_columna
        self.sqls.append(" ".join(sql.split()).lower())
        return 0


def _md(tipo, origen, destino, *, importe=None, batch=None, meta=None):
    return {
        "tipo": tipo, "origen_id": origen, "destino_id": destino,
        "importe": importe, "batch_id": batch, "metadata": meta or {},
    }


def _usar(monkeypatch, fake):
    monkeypatch.setattr(concepto_cobro, "_db", fake)


# --------------------------------------------------------------------------
# MTM — aplicado a factura, y TOTALIZAR borró el vínculo 16 minutos después
# --------------------------------------------------------------------------

_MTM_MOVS = [
    _md("cheque_creado", 101712, 101712, meta={"codigo_cli": "MTM"}),
    _md("cheque_aplicado_a_factura", 101712, 276593, importe=300.0,
        meta={"numf": 177617, "id_factura": 276593,
              "saldo_factura_post": 1759.28, "stat_factura_post": "A"}),
    _md("cheque_creado", 101713, 101713, meta={"codigo_cli": "MTM"}),
    _md("cheque_aplicado_a_factura", 101713, 276593, importe=300.0,
        meta={"numf": 177617, "id_factura": 276593,
              "saldo_factura_post": 1459.28, "stat_factura_post": "A"}),
]


def test_mtm_aplicado_aunque_totalizar_borro_el_vinculo(monkeypatch):
    """El caso literal del reclamo: NO puede decir "sin aplicar a facturas"."""
    _usar(monkeypatch, _FakeDB(
        _MTM_MOVS,
        totalizar=[{"codigo_cli": "MTM", "cuando": date(2026, 8, 3)}],
        facturas=[{"id_factura": 276593, "importe": 2059.28}],
    ))
    out = concepto_cobro.explicaciones([101712, 101713],
                                       {101712: "MTM", 101713: "MTM"})
    assert set(out) == {101712, 101713}
    for cid in (101712, 101713):
        assert out[cid]["origen"] == "totalizar"
        # La dueña: "quiero que en cobranza quede guardado qué factura se pagó".
        assert "177617" in out[cid]["texto"]
        assert "TOTALIZAR" in out[cid]["texto"]
        # La columna "importe" de la tirilla necesita el total de la factura,
        # que mov_doble NO guarda: sale de un lookup a scintela.factura
        # ("mientras siga quedando encolumnado y prolijo sí").
        assert out[cid]["facturas"] == [
            {"numf": 177617, "importe": 300.0, "fact_importe": 2059.28,
             "saldo_post": 1759.28 if cid == 101712 else 1459.28,
             "stat_post": "A"}
        ]


def test_texto_del_totalizar_lleva_la_fecha(monkeypatch):
    """La dueña lo escribió así: "…redistribuido por TOTALIZAR del 03/08"."""
    _usar(monkeypatch, _FakeDB(
        _MTM_MOVS,
        totalizar=[{"codigo_cli": "MTM", "cuando": date(2026, 8, 3)}],
    ))
    txt = concepto_cobro.etiquetas_automaticas([101712], {101712: "MTM"})[101712]
    assert txt == ("aplicado a la factura 177617 · el TOTALIZAR del 03/08 "
                   "redistribuyó el abono")


def test_sin_totalizar_conocido_no_inventa_fecha(monkeypatch):
    _usar(monkeypatch, _FakeDB(_MTM_MOVS, totalizar=[]))
    txt = concepto_cobro.etiquetas_automaticas([101712], {101712: "MTM"})[101712]
    assert txt.startswith("aplicado a la factura 177617")
    assert "TOTALIZAR" in txt and "del " not in txt


# --------------------------------------------------------------------------
# CEM — anticipo con espejo NB=98; el mov de cancelación apunta al ESPEJO
# --------------------------------------------------------------------------

_CEM_MOVS = [
    _md("cheque_creado", 101721, 101721, batch="3c44", meta={"es_anticipo": True}),
    _md("cheque_anticipo_espejo", 101721, 101722,
        meta={"id_cheque_padre": 101721, "id_cheque_espejo": 101722}),
    _md("cheque_creado", 101723, 101723, batch="3c44", meta={"es_anticipo": True}),
    _md("cheque_anticipo_espejo", 101723, 101724,
        meta={"id_cheque_padre": 101723, "id_cheque_espejo": 101724}),
    _md("cheque_cancelado_por_anticipo", 99741, 101722, batch="b616",
        meta={"id_cheque_anticipo": 101722, "monto_anticipo": 4020.69}),
    _md("anticipo_neteado", 101722, 99741, batch="b616"),
    _md("anticipo_neteado", 101724, 99741, batch="b616"),
]
_CHEQUE_CANCELADO_CEM = [
    {"id_cheque": 99741, "no_cheque": "645", "banco": "PICHINCHA",
     "fechad": date(2026, 8, 21), "importe": 4020.69},
]


def test_cem_los_dos_depositos_dicen_que_cancelaron_el_cheque(monkeypatch):
    """El mov apunta al ESPEJO (101722), no al depósito: hay que traducirlo.

    Y `anticipo_neteado` es el que salva al SEGUNDO depósito — el
    `cheque_cancelado_por_anticipo` referencia a uno solo.
    """
    _usar(monkeypatch, _FakeDB(_CEM_MOVS, cheques=_CHEQUE_CANCELADO_CEM))
    out = concepto_cobro.etiquetas_automaticas([101721, 101723])
    assert out[101721] == "anticipo · canceló el cheque 645 ($4.020,69)"
    assert out[101723] == out[101721]


def test_monto_en_formato_ecuador(monkeypatch):
    """Punto = miles, coma = decimales. Nunca formato US."""
    _usar(monkeypatch, _FakeDB(_CEM_MOVS, cheques=_CHEQUE_CANCELADO_CEM))
    txt = concepto_cobro.etiquetas_automaticas([101721])[101721]
    assert "$4.020,69" in txt and "4,020.69" not in txt


# --------------------------------------------------------------------------
# JOH — anticipo consumido entero (sin espejo); el mov apunta a UNA de las
# dos filas y la otra se salva por el batch del alta
# --------------------------------------------------------------------------

_JOH_MOVS = [
    _md("cheque_creado", 101750, 101750, batch="898b", meta={"es_anticipo": True}),
    _md("cheque_creado", 101751, 101751, batch="898b", meta={"es_anticipo": True}),
    _md("cheque_cancelado_por_anticipo", 99366, 101750,
        meta={"id_cheque_anticipo": 101750, "monto_anticipo": 1028.0}),
]


def test_joh_el_hermano_del_batch_tambien_queda_explicado(monkeypatch):
    """Sin la propagación por batch, el depósito de $770 quedaba mudo."""
    _usar(monkeypatch, _FakeDB(
        _JOH_MOVS,
        cheques=[{"id_cheque": 99366, "no_cheque": "", "banco": "PICHINCHA",
                  "fechad": date(2026, 8, 4), "importe": 1028.0}],
    ))
    out = concepto_cobro.etiquetas_automaticas([101750, 101751])
    assert set(out) == {101750, 101751}
    # 1.430 de los 2.043 cheques vivos NO tienen no_cheque (99366 entre
    # ellos). La dueña: "poné el número de cheque o algo" → fallback banco +
    # fecha de depósito, nunca el id interno.
    for cid in (101750, 101751):
        assert out[cid] == (
            "anticipo · canceló el cheque Pichincha dep. 04/08 ($1.028,00)")
        assert "99366" not in out[cid]
        assert "101750" not in out[cid]


def test_anticipo_suelto_sin_cancelar_nada(monkeypatch):
    _usar(monkeypatch, _FakeDB([
        _md("cheque_creado", 500, 500, meta={"es_anticipo": True}),
    ]))
    assert concepto_cobro.etiquetas_automaticas([500])[500] == (
        "anticipo · saldo a favor del cliente")


# --------------------------------------------------------------------------
# Bordes
# --------------------------------------------------------------------------

def test_cobro_sin_huella_no_recibe_etiqueta(monkeypatch):
    """Los 240 depósitos de `dbf-import` no tienen nada en mov_doble: se
    quedan como estaban (o con el concepto que se tipee a mano)."""
    _usar(monkeypatch, _FakeDB([_md("cheque_creado", 700, 700, meta={})]))
    assert concepto_cobro.explicaciones([700]) == {}


def test_solo_mira_movs_activos_y_de_los_tipos_conocidos(monkeypatch):
    fake = _FakeDB([])
    _usar(monkeypatch, fake)
    concepto_cobro.explicaciones([1])
    sql = fake.sqls[-1]
    assert "estado = 'activo'" in sql
    for t in ("cheque_aplicado_a_factura", "cheque_anticipo_espejo",
              "cheque_cancelado_por_anticipo", "anticipo_neteado"):
        assert t in str(concepto_cobro._TIPOS)
    assert t  # noqa: B015 -- los 4 tipos hacen falta, ver docstring del módulo


def test_falla_de_db_no_rompe_la_impresion(monkeypatch):
    class _Boom:
        def fetch_all(self, *a, **k):
            raise RuntimeError("RDS caído")

        def execute(self, *a, **k):
            raise RuntimeError("RDS caído")

    _usar(monkeypatch, _Boom())
    assert concepto_cobro.explicaciones([1, 2]) == {}
    concepto_cobro._bootstrapped = False
    concepto_cobro.bootstrap_columna()  # fail-soft: no debe propagar


def test_sin_ids_no_consulta(monkeypatch):
    fake = _FakeDB([])
    _usar(monkeypatch, fake)
    assert concepto_cobro.explicaciones([]) == {}
    assert fake.sqls == []


def test_limpiar_concepto():
    assert concepto_cobro.limpiar("  Abono a cheques  ") == "Abono a cheques"
    assert concepto_cobro.limpiar("   ") is None
    assert concepto_cobro.limpiar(None) is None
    assert len(concepto_cobro.limpiar("x" * 500)) == concepto_cobro.MAX_LEN


def test_presets_incluyen_los_dos_que_pidio_alex():
    """Textual: "o tener preestablecido: Abono a cheques, anticipo a facturas"."""
    assert "Abono a cheques" in concepto_cobro.PRESETS
    assert "Anticipo a facturas" in concepto_cobro.PRESETS
