"""La hoja imprimible de un totalizar YA HECHO. 18/09/2026.

TMT (dueña): *"cuando totalizamos imprimimos, ¿no? quisiera re-entrar a ese
archivo"*. La hoja de confirmación se arma con datos vivos y no se guarda;
la corrida sí guarda `antes`/`despues`, y de ahí se re-arma la misma hoja.
"""
from __future__ import annotations

import inspect
from datetime import date, datetime
from pathlib import Path

import pytest

from modules.informes import queries as iq

ROOT = Path(__file__).resolve().parent.parent
PREVIEW = (ROOT / "modules/informes/templates/informes/totalizar_preview.html").read_text()
HOJA = (ROOT / "modules/informes/templates/informes/hoja_totalizar.html").read_text()
EC = (ROOT / "modules/informes/templates/informes/estado_cuenta.html").read_text()


def _mov(md, estado="activo"):
    return {"id_mov_doble": 45, "tipo": "totalizar_estado_cuenta", "estado": estado,
            "usuario": "alex", "fecha_creacion": datetime(2026, 9, 9, 20, 36),
            "metadata": md}


MD = {
    "codigo_cli": "MTM", "pool": 3988.62, "n_links_borrados": 8,
    "antes": [
        {"id": 1, "numf": "001-099-000176310", "importe": 550.10, "abono": 0, "saldo": 550.10, "stat": "Z"},
        {"id": 2, "numf": "001-099-000177617", "importe": 2059.28, "abono": 666.30, "saldo": 1392.98, "stat": "A"},
    ],
    "despues": [
        {"id": 1, "abono": 550.10, "saldo": 0, "stat": "T"},
        {"id": 2, "abono": 2059.28, "saldo": 0, "stat": "T"},
    ],
}


def _db(monkeypatch, mov, vivas):
    calls = {"one": 0}

    def fetch_one(sql, params=None, conn=None):
        calls["one"] += 1
        if "mov_doble" in sql:
            return mov
        if "scintela.cliente" in sql:
            return {"codigo_cli": "MTM", "nombre": "MI CLIENTE"}
        return None

    def fetch_all(sql, params=None, conn=None):
        if "chequesxfact" in sql:
            return []
        return vivas

    monkeypatch.setattr(iq.db, "fetch_one", fetch_one)
    monkeypatch.setattr(iq.db, "fetch_all", fetch_all)


VIVAS = [
    {"id_factura": 1, "numf": 176310, "numf_completo": "001-099-000176310", "fecha": date(2026, 5, 28), "retencion": 0},
    {"id_factura": 2, "numf": 177617, "numf_completo": "001-099-000177617", "fecha": date(2026, 6, 16), "retencion": 0},
]


def test_la_hoja_tiene_la_misma_forma_que_la_de_confirmacion(monkeypatch):
    _db(monkeypatch, _mov(MD), VIVAS)
    d = iq.totalizar_hoja_guardada(45)
    assert d["cliente"]["codigo_cli"] == "MTM"
    assert [f["numf"] for f in d["filas"]] == [176310, 177617]
    f = d["filas"][1]
    assert (f["abono_actual"], f["stat_actual"], f["abono_nuevo"], f["stat_nuevo"]) == (666.30, "A", 2059.28, "T")
    assert f["cambia"] and f["fecha"] == date(2026, 6, 16)
    assert d["pool"] == 3988.62 and d["n_links"] == 8
    assert (d["n_T"], d["n_A"], d["n_Z"]) == (2, 0, 0)
    assert d["sum_saldo_antes"] == 1943.08 and d["sum_saldo_despues"] == 0
    assert d["corrida"]["fecha"] == date(2026, 9, 9) and d["corrida"]["usuario"] == "alex"
    assert d["corrida"]["deshecha"] is False


def test_una_corrida_deshecha_lo_dice(monkeypatch):
    _db(monkeypatch, _mov(MD, estado="reversado"), VIVAS)
    assert iq.totalizar_hoja_guardada(45)["corrida"]["deshecha"] is True
    assert "Este totalizar después se deshizo" in HOJA


def test_sin_foto_no_hay_hoja(monkeypatch):
    _db(monkeypatch, _mov({"codigo_cli": "MTM"}), VIVAS)
    assert iq.totalizar_hoja_guardada(45) == {}


def test_otro_tipo_de_movimiento_no_es_una_hoja(monkeypatch):
    m = _mov(MD)
    m["tipo"] = "cheque_aplicado_a_factura"
    _db(monkeypatch, m, VIVAS)
    assert iq.totalizar_hoja_guardada(45) == {}


def test_la_ruta_existe_y_la_ve_quien_ve_la_cuenta(app):
    rutas = {r.rule for r in app.url_map.iter_rules()}
    assert "/informes/estado-cuenta/<codigo_cli>/totalizar/<int:id_mov_doble>/hoja" in rutas
    import modules.informes.views as _v
    src = inspect.getsource(_v.estado_cuenta_totalizar_hoja)
    assert "totalizar_hoja_guardada" in src and "hoja_totalizar.html" in src


def test_la_hoja_guardada_es_solo_lectura_y_tiene_su_forma():
    """Template propio: sin Confirmar ni corte por fecha, con Imprimir. Dos
    bloques Antes/Después con Cobro · $ · Saldo · Stat (dueña 18/09: dos
    columnas para cobro y monto, la letra A/T/Z, sin columna Abono), un
    renglón "sin cobro" para el abono sin cheque, y sin el sortable (rowspan)."""
    assert "Confirmar totalización" not in HOJA and 'name="hasta"' not in HOJA
    assert "window.print()" in HOJA
    assert HOJA.count(">Cobro</th>") == 2 and HOJA.count(">$</th>") == 2
    assert HOJA.count(">Saldo</th>") == 2 and HOJA.count(">Stat</th>") == 2
    assert ">Abono</th>" not in HOJA
    assert "'rotulo': 'sin cobro'" in HOJA
    assert "data-no-sort-table" in HOJA
    assert "size: landscape" in HOJA
    # La confirmación sigue siendo la de siempre (con su impresión tipo Excel).
    assert "hoja_guardada" not in PREVIEW and "Confirmar totalización" in PREVIEW
    assert "border: 1px solid #000 !important" in PREVIEW


def test_el_estado_de_cuenta_linkea_la_hoja_de_cada_corrida():
    assert "informes.estado_cuenta_totalizar_hoja" in EC
    assert ">Ver la hoja</a>" in EC


@pytest.mark.parametrize("nombre", ["estado_cuenta_totalizar"])
def test_la_confirmacion_sigue_igual(app, nombre):
    """La misma pantalla sin `hoja_guardada` sigue siendo la de confirmar."""
    import modules.informes.views as _v
    src = inspect.getsource(getattr(_v, nombre))
    assert "hoja_guardada" not in src


def test_la_hoja_dice_quien_pago_cada_factura_antes_y_despues(monkeypatch):
    """TMT 18/09 (dueña, sobre MTM): "acá no veo el cheque de 536"."""
    md = dict(MD, ids_reaplicados=[901])

    def fetch_all(sql, params=None, conn=None):
        if "chequesxfact_totalizado" in sql:
            return [{"id_fact": 2, "importe": 536.30, "rotulo": "47395434"}]
        if "id_chequexfact = ANY" in sql:
            return [{"id_fact": 1, "importe": 536.30, "rotulo": "47395434"}]
        return VIVAS

    _db(monkeypatch, _mov(md), VIVAS)
    monkeypatch.setattr(iq.db, "fetch_all", fetch_all)
    d = iq.totalizar_hoja_guardada(45)
    assert d["con_cobros"] is True
    f1, f2 = d["filas"]
    assert f2["cobros_antes"] == [{"rotulo": "47395434", "importe": 536.30}]
    assert f2["cobros_despues"] == []
    assert f1["cobros_antes"] == []
    assert f1["cobros_despues"] == [{"rotulo": "47395434", "importe": 536.30}]
    assert d["sum_cobros_antes"] == 536.30 and d["sum_cobros_despues"] == 536.30


def test_sin_ids_reaplicados_el_despues_son_los_vinculos_vivos(monkeypatch):
    """Corridas viejas (o repuestas con el botón): lo que hay hoy en la tabla viva."""
    vistos = []

    def fetch_all(sql, params=None, conn=None):
        vistos.append(sql)
        if "chequesxfact" in sql:
            return []
        return VIVAS

    _db(monkeypatch, _mov(MD), VIVAS)
    monkeypatch.setattr(iq.db, "fetch_all", fetch_all)
    iq.totalizar_hoja_guardada(45)
    assert any("x.id_fact = ANY(%s)" in s for s in vistos)
    assert not any("id_chequexfact = ANY" in s for s in vistos)
