"""El TOTALIZAR estado de cuenta se puede DESHACER.

TMT 2026-08-07 (dueña): clickeó el ↺ del totalizar de MLZ (mov #22176) y la
app contestó *"Reverso no disponible desde acá. Tipo
'totalizar_estado_cuenta' aún no tiene reverso automatizado."* Era cierto: el
totalizar pisaba abono/saldo/stat de N facturas y borraba los vínculos
cheque↔factura sin guardar nada, así que no había qué restaurar.

Ahora el mov_doble snapshotea:
  · `antes`   — importe/abono/saldo/stat de CADA factura del universo;
  · `despues` — lo que escribió (el CANDADO del reverso);
  · `links`   — las filas de chequesxfact borradas, serializables a jsonb.

Cubre:
1. ejecutar() guarda el snapshot completo y JSON-serializable.
2. preview: sin snapshot (totalizar viejo) → bloqueo explicativo.
3. preview: todo intacto → filas antes/después y ningún bloqueo.
4. preview: una factura tocada después → bloqueo que la nombra.
5. preview: mov ya reversado → bloqueo.
6. ejecutar el reverso: restaura sólo lo que cambia, repone links (salteando
   el que ya volvió a existir y el cheque que ya no está) y registra el
   mov_doble con id_original.
7. el dispatcher del Historial rutea el tipo a una ruta que EXISTE.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import date
from decimal import Decimal

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MD_IDX = 11  # posición de `metadata` en el INSERT de mov_doble.registrar


def _fact(id_, importe, abono, saldo, stat):
    return {
        "id_factura": id_, "numf": id_, "numf_completo": f"001-001-{id_:09d}",
        "fecha": date(2026, 6, id_), "importe": importe, "abono": abono,
        "saldo": saldo, "stat": stat,
    }


# ───────────────────────── 1. el snapshot se graba ────────────────────────

class _DBStubEjecutar:
    def __init__(self, facturas, links):
        self.facturas, self.links = facturas, links
        self.mov_dobles: list[tuple] = []

    def fetch_all(self, sql, params=None, conn=None):
        if "from scintela.chequesxfact" in " ".join(sql.split()).lower():
            return [dict(x) for x in self.links]
        return [dict(f) for f in self.facturas]

    def fetch_one(self, sql, params=None, conn=None):
        return None

    def execute(self, sql, params=None, conn=None):
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        if "insert into scintela.mov_doble" in " ".join(sql.split()).lower():
            self.mov_dobles.append(tuple(params))
            return {"id_mov_doble": 777}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _patch(monkeypatch, stub):
    import db
    for name in ("fetch_all", "fetch_one", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, name, getattr(stub, name))


def test_ejecutar_guarda_antes_despues_y_links(monkeypatch):
    from modules.informes import queries as q
    link = {
        "id_chequexfact": 5, "id_cheque": 900, "id_fact": 1,
        "fechaing": date(2026, 8, 7), "codigo_cli": "AAA",
        "importe": Decimal("60.00"), "no_banco": 90, "tipo": None,
        "stat_f": "A", "fecha_venci_f": None, "abono_f": Decimal("60.00"),
        "saldo_f": Decimal("40.00"), "usuario_crea": "andres",
    }
    stub = _DBStubEjecutar(
        [_fact(1, 100.0, 60.0, 40.0, "A"),
         _fact(2, 100.0, 90.0, 10.0, "A"),
         _fact(3, 100.0, 0.0, 100.0, "Z")],
        [link],
    )
    _patch(monkeypatch, stub)
    q.totalizar_estado_cuenta_ejecutar("aaa", usuario="tester")

    md = json.loads(stub.mov_dobles[0][_MD_IDX])   # tiene que ser serializable
    assert [a["id"] for a in md["antes"]] == [1, 2, 3]
    assert md["antes"][0] == {"id": 1, "numf": "001-001-000000001",
                              "importe": 100.0, "abono": 60.0, "saldo": 40.0,
                              "stat": "A"}
    assert md["despues"][0] == {"id": 1, "abono": 100.0, "saldo": 0.0,
                                "stat": "T"}
    # El link viaja con la fecha en ISO y los Decimal en float, y SIN el id
    # de la fila vieja (al reponerlo la secuencia da uno nuevo).
    assert md["links"] == [{
        "id_cheque": 900, "id_fact": 1, "fechaing": "2026-08-07",
        "codigo_cli": "AAA", "importe": 60.0, "no_banco": 90, "tipo": None,
        "stat_f": "A", "fecha_venci_f": None, "abono_f": 60.0,
        "saldo_f": 40.0, "usuario_crea": "andres",
    }]


# ────────────────────────────── 2-5. preview ──────────────────────────────

_ANTES = [
    {"id": 1, "numf": "F1", "importe": 100.0, "abono": 60.0, "saldo": 40.0, "stat": "A"},
    {"id": 2, "numf": "F2", "importe": 100.0, "abono": 90.0, "saldo": 10.0, "stat": "A"},
    {"id": 3, "numf": "F3", "importe": 100.0, "abono": 0.0, "saldo": 100.0, "stat": "Z"},
]
_DESPUES = [
    {"id": 1, "abono": 100.0, "saldo": 0.0, "stat": "T"},
    {"id": 2, "abono": 50.0, "saldo": 50.0, "stat": "A"},
    {"id": 3, "abono": 0.0, "saldo": 100.0, "stat": "Z"},
]


class _DBStubPreview:
    def __init__(self, mov, facturas):
        self.mov, self.facturas = mov, facturas
        self.updates: list[tuple] = []
        self.inserts: list[tuple] = []
        self.mov_dobles: list[tuple] = []
        self.existentes: set = set()
        self.cheques: set = {900, 901}

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.mov_doble" in s:
            return dict(self.mov) if self.mov else None
        if "from scintela.chequesxfact" in s:
            return {"x": 1} if tuple(params) in self.existentes else None
        if "from scintela.cheque " in s:
            return {"x": 1} if params[0] in self.cheques else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return [dict(f) for f in self.facturas]

    def execute(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "update scintela.factura" in s:
            self.updates.append(tuple(params))
        elif "insert into scintela.chequesxfact" in s:
            self.inserts.append(tuple(params))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        if "insert into scintela.mov_doble" in " ".join(sql.split()).lower():
            self.mov_dobles.append(tuple(params))
            return {"id_mov_doble": 888}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _mov(metadata, estado="activo"):
    return {"id_mov_doble": 22176, "tipo": "totalizar_estado_cuenta",
            "estado": estado, "metadata": metadata}


def _fact_viva(id_, abono, saldo, stat):
    return {"id_factura": id_, "numf": f"F{id_}", "numf_completo": f"F{id_}",
            "abono": abono, "saldo": saldo, "stat": stat}


_VIVAS_INTACTAS = [_fact_viva(1, 100.0, 0.0, "T"),
                   _fact_viva(2, 50.0, 50.0, "A"),
                   _fact_viva(3, 0.0, 100.0, "Z")]


def test_preview_totalizar_viejo_sin_snapshot_bloquea(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStubPreview(_mov({"codigo_cli": "AAA", "pool": 150.0}), [])
    _patch(monkeypatch, stub)
    prev = q.totalizar_reverso_preview(22176)
    assert "anterior al reverso automático" in prev["bloqueo"]
    assert prev["codigo_cli"] == "AAA"


def test_preview_intacto_arma_las_filas(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStubPreview(
        _mov({"codigo_cli": "AAA", "antes": _ANTES, "despues": _DESPUES,
              "links": [{"id_cheque": 900}]}),
        _VIVAS_INTACTAS)
    _patch(monkeypatch, stub)
    prev = q.totalizar_reverso_preview(22176)
    assert prev["bloqueo"] is None
    assert len(prev["filas"]) == 3
    # La 3ra queda igual antes y después → no cuenta como cambio.
    assert prev["n_cambian"] == 2
    assert [f["cambia"] for f in prev["filas"]] == [True, True, False]
    assert prev["filas"][0]["stat_actual"] == "T"
    assert prev["filas"][0]["stat_destino"] == "A"
    # El totalizar no crea ni borra deuda: el Σsaldo no se mueve.
    assert prev["sum_saldo_actual"] == prev["sum_saldo_destino"] == 150.0
    assert prev["n_links"] == 1


def test_preview_factura_tocada_despues_bloquea_y_la_nombra(monkeypatch):
    from modules.informes import queries as q
    vivas = [_fact_viva(1, 100.0, 0.0, "T"),
             _fact_viva(2, 80.0, 20.0, "A"),   # cobraron 30 después
             _fact_viva(3, 0.0, 100.0, "Z")]
    stub = _DBStubPreview(
        _mov({"codigo_cli": "AAA", "antes": _ANTES, "despues": _DESPUES}),
        vivas)
    _patch(monkeypatch, stub)
    prev = q.totalizar_reverso_preview(22176)
    assert "F2" in prev["bloqueo"] and "cambió después del totalizar" in prev["bloqueo"]


def test_preview_factura_borrada_bloquea(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStubPreview(
        _mov({"codigo_cli": "AAA", "antes": _ANTES, "despues": _DESPUES}),
        _VIVAS_INTACTAS[:2])
    _patch(monkeypatch, stub)
    prev = q.totalizar_reverso_preview(22176)
    assert "F3" in prev["bloqueo"] and "ya no está en la base" in prev["bloqueo"]


def test_preview_ya_reversado_bloquea(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStubPreview(
        _mov({"codigo_cli": "AAA", "antes": _ANTES, "despues": _DESPUES},
             estado="reversado"),
        _VIVAS_INTACTAS)
    _patch(monkeypatch, stub)
    prev = q.totalizar_reverso_preview(22176)
    assert "ya está reversado" in prev["bloqueo"]


def test_preview_de_otro_tipo_devuelve_vacio(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStubPreview(
        {"id_mov_doble": 1, "tipo": "cheque_aplicado_a_factura",
         "estado": "activo", "metadata": {}}, [])
    _patch(monkeypatch, stub)
    assert q.totalizar_reverso_preview(1) == {}


def test_preview_acepta_metadata_como_texto(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStubPreview(
        _mov(json.dumps({"codigo_cli": "AAA", "antes": _ANTES,
                         "despues": _DESPUES})),
        _VIVAS_INTACTAS)
    _patch(monkeypatch, stub)
    assert q.totalizar_reverso_preview(22176)["bloqueo"] is None


# ─────────────────────────── 6. ejecutar el reverso ───────────────────────

def _links_demo():
    base = {"fechaing": "2026-08-07", "codigo_cli": "AAA", "importe": 60.0,
            "no_banco": 90, "tipo": None, "stat_f": "A",
            "fecha_venci_f": None, "abono_f": 60.0, "saldo_f": 40.0,
            "usuario_crea": "andres"}
    return [
        {**base, "id_cheque": 900, "id_fact": 1},   # se repone
        {**base, "id_cheque": 901, "id_fact": 2},   # ya existe → se saltea
        {**base, "id_cheque": 999, "id_fact": 3},   # cheque borrado → se saltea
    ]


def test_ejecutar_restaura_repone_links_y_registra(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStubPreview(
        _mov({"codigo_cli": "AAA", "antes": _ANTES, "despues": _DESPUES,
              "links": _links_demo()}),
        _VIVAS_INTACTAS)
    stub.existentes = {(901, 2)}          # ese vínculo volvió a existir
    _patch(monkeypatch, stub)
    res = q.totalizar_reverso_ejecutar(22176, usuario="tamara")

    assert res["n_facturas"] == 3 and res["n_restauradas"] == 2
    # Sólo las dos que cambian; la 3ra no ensucia usuario_modifica.
    assert stub.updates == [(60.0, 40.0, "A", "tamara", 1),
                            (90.0, 10.0, "A", "tamara", 2)]
    # Un solo link repuesto (el 901 ya estaba, el 999 no tiene cheque).
    assert res["n_links_repuestos"] == 1 and len(stub.inserts) == 1
    assert stub.inserts[0][0] == 900 and stub.inserts[0][1] == 1
    # El mov de reverso apunta al original (id_original ⇒ lo marca reversado).
    mv = stub.mov_dobles[0]
    assert mv[1] == "reverso_totalizar_estado_cuenta"
    assert mv[10] == 22176
    assert json.loads(mv[_MD_IDX])["id_mov_deshecho"] == 22176


def test_ejecutar_sin_snapshot_explota_con_el_motivo(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStubPreview(_mov({"codigo_cli": "AAA"}), [])
    _patch(monkeypatch, stub)
    with pytest.raises(ValueError, match="anterior al reverso automático"):
        q.totalizar_reverso_ejecutar(22176)
    assert stub.updates == []


def test_ejecutar_de_otro_tipo_explota(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStubPreview(
        {"id_mov_doble": 1, "tipo": "gasto_simple", "estado": "activo",
         "metadata": {}}, [])
    _patch(monkeypatch, stub)
    with pytest.raises(ValueError, match="no es un totalizar"):
        q.totalizar_reverso_ejecutar(1)


# ───────────────────── 7. el ↺ del Historial llega a algún lado ───────────

def test_dispatcher_del_historial_rutea_a_una_ruta_que_existe(app):
    from modules.historial.views import _REVERSO_DISPATCH
    endpoint, kwargs_fn = _REVERSO_DISPATCH["totalizar_estado_cuenta"]
    assert endpoint == "informes.totalizar_reverso_confirmar"
    assert kwargs_fn({"id_mov_doble": 22176}) == {"id_mov_doble": 22176}
    assert endpoint in {r.endpoint for r in app.url_map.iter_rules()}


# ───────── 8. la traza junta las N facturas del totalizar en UN renglón ────

def test_eventos_expande_las_facturas_del_totalizar(monkeypatch):
    """Sin esto, las N−2 facturas que el mov no nombra caen al camino sin
    nombre y la traza las rotula por la FORMA del cambio: volver de 'T' a 'Z'
    deja el saldo igual al importe y salen como "facturas nuevas"."""
    from modules.informes import eventos as ev
    fila = {
        "id_mov_doble": 22253, "batch_id": None,
        "tipo": "reverso_totalizar_estado_cuenta",
        "origen_table": "factura", "origen_id": 1,
        "destino_table": "factura", "destino_id": 3,
        "importe": 10423.74, "concepto": "DESHACER totalizar MLZ",
        "usuario": "tamara", "estado": "reverso", "dia": date(2026, 8, 7),
        "metadata": {"codigo_cli": "MLZ", "n_facturas": 3,
                     "antes": [{"id": 1}, {"id": 2}, {"id": 3}]},
    }
    monkeypatch.setattr(ev.db, "fetch_all", lambda *a, **k: [fila])
    evs = ev.de_la_ventana("2026-08-07 00:00", "2026-08-07 23:59")
    assert set(evs[0]["docs"]) == {"f1", "f2", "f3"}
    # Las tres caen en el MISMO grupo ⇒ un solo renglón.
    idx = ev.indice(evs)
    assert len({idx[d]["grupo"] for d in ("f1", "f2", "f3")}) == 1


def _mov_fact(doc, etiqueta, aporte, regla):
    return {"doc_id": doc, "etiqueta": etiqueta, "aporte": aporte,
            "componente": "facturas", "regla": regla, "familia": "traspaso"}


def test_resumir_un_renglon_para_el_totalizar_aunque_netee_cero():
    from modules.informes import traza
    evento = {"tipo": "reverso_totalizar_estado_cuenta", "grupo": "m22253",
              "label": "Deshacer totalizar", "concepto": "DESHACER MLZ",
              "dia": date(2026, 8, 7),
              "meta": {"codigo_cli": "MLZ", "n_facturas": 3}}
    movs = [
        _mov_fact("f1", "Factura 169218 · MLZ", 1747.01, "Venta facturada"),
        _mov_fact("f2", "Factura 174039 · MLZ", 321.26, "Factura corregida en más"),
        _mov_fact("f3", "Factura 170918 · MLZ", -2068.27, "Venta facturada"),
    ]
    out = traza.resumir(movs, None, {"f1": evento, "f2": evento, "f3": evento})
    assert len(out) == 1
    g = out[0]
    # Netea cero — y aun así se muestra: tocó tres facturas.
    assert g["aporte"] == 0.0
    assert "totalizar" in g["texto"] and "MLZ" in g["texto"]
    assert g["texto"].startswith("↩")
    # El "(3)" pelado pasó a decir en qué unidad: "· 3 facturas". El
    # paréntesis cuenta HECHOS y acá el hecho es UNO que abarca tres facturas.
    assert "3 facturas" in g["texto"]
    # 🚨 lo que la dueña vio y no era cierto.
    assert "facturas nuevas" not in g["texto"]


def test_resumir_sin_evento_es_el_renglon_partido_de_antes():
    """Mutante de control: sin el índice de eventos vuelve el bug — tres
    renglones, uno de ellos rotulado "facturas nuevas"."""
    from modules.informes import traza
    movs = [
        _mov_fact("f1", "Factura 169218 · MLZ", 1747.01, "Venta facturada"),
        _mov_fact("f2", "Factura 174039 · MLZ", 321.26, "Factura corregida en más"),
        _mov_fact("f3", "Factura 170918 · MLZ", -2068.27, "Venta facturada"),
    ]
    out = traza.resumir(movs, None, {})
    assert len(out) == 2                       # se agrupan por REGLA, no por hecho
    assert any("facturas nuevas" in g["texto"] for g in out)
