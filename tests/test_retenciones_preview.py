"""Preview de retenciones de Asinfo: la pantalla tiene que clasificar IGUAL
que el aplicador.

TMT 2026-08-03 (dueña: "porque no tiene aplicada? de cuando son?"): el health
decía 45 sin factura y la pantalla 998, con la MISMA ventana y las MISMAS 1280
retenciones. Causa: `_factura_por_numero` (el aplicador) tiene fallback por
`numf` entero desde el 21/07 — las facturas de origen dBase tienen
`numf_completo` NULL porque el DBF sólo trae el número entero — y
`preview_retenciones_asinfo` (la pantalla) nunca copió esa segunda rama. Las
851 de diferencia estaban aplicadas hacía rato: la pantalla daba un falso
negativo, y hacía creer que faltaba más de un millón de facturación.
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ───────────────────────────── _numf_sri ─────────────────────────────

def test_numf_sri():
    from modules.retenciones import queries as q
    assert q._numf_sri("001-099-000179468") == 179468
    assert q._numf_sri("179468") == 179468
    assert q._numf_sri("") is None
    assert q._numf_sri(None) is None
    assert q._numf_sri("SIN-DIGITOS") is None


# ───────────────────────────── stub de db ─────────────────────────────

class _FetchAllStub:
    """Despacha por el texto del SQL. Registra qué queries se corrieron."""

    def __init__(self, por_completo=(), por_numf=(), retenciones=(), clientes=()):
        self.por_completo = list(por_completo)
        self.por_numf = list(por_numf)
        self.retenciones = list(retenciones)
        self.clientes = list(clientes)
        self.vistas = []

    def fetch_all(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        self.vistas.append(s)
        if "from scintela.factura" in s and "numf_completo = any" in s:
            return [dict(f) for f in self.por_completo]
        if "from scintela.factura" in s and "numf = any" in s:
            return [dict(f) for f in self.por_numf]
        if "from scintela.retencion" in s:
            return [dict(r) for r in self.retenciones]
        if "from scintela.cliente" in s:
            return [dict(c) for c in self.clientes]
        return []


def _patch(monkeypatch, stub, ret_map):
    import db
    from modules.asinfo import service as svc
    monkeypatch.setattr(db, "fetch_all", stub.fetch_all)
    monkeypatch.setattr(svc, "retenciones_periodo", lambda d, h: ret_map)


def _fac(numf, numf_completo=None, importe=1000.0, abono=0.0, saldo=1000.0,
         codigo_cli="EDU", id_factura=7, stat="Z", retencion=0.0):
    return {"id_factura": id_factura, "codigo_cli": codigo_cli, "numf": numf,
            "numf_completo": numf_completo, "importe": importe, "abono": abono,
            "retencion": retencion, "saldo": saldo, "stat": stat}


# ───────────────────── el fallback por numf (el fix) ─────────────────────

def test_preview_encuentra_por_numf_cuando_numf_completo_es_null(monkeypatch):
    """La factura vino del dBase (numf_completo NULL). ANTES: "sin factura en
    PC". AHORA: la encuentra por el numf y dice la verdad — ya estaba."""
    from modules.retenciones import queries as q
    stub = _FetchAllStub(
        por_completo=[],                                   # no matchea por SRI
        por_numf=[_fac(178485, None)],                     # sí por numf entero
        retenciones=[{"codigo_cli": "EDU", "numf": 178485}],
        clientes=[{"codigo_cli": "EDU", "nombre": "EDUARDO"}],
    )
    _patch(monkeypatch, stub, {"001-099-000178485": {"ret_total": 382.40}})
    out = q.preview_retenciones_asinfo("2026-06-04", "2026-08-03")
    fila = out["filas"][0]
    assert fila["estado"] == "ya"
    assert fila["via_numf"] is True
    assert fila["numf"] == 178485
    assert fila["cliente"] == "EDUARDO"
    assert out["resumen"]["sin_factura"] == 0
    assert out["resumen"]["ya"] == 1
    assert out["resumen"]["via_numf"] == 1


def test_preview_con_abono_previo_proyecta_el_mismo_saldo(monkeypatch):
    """TMT 2026-08-07: con un abono manual previo, el preview proyecta el
    saldo restando las DOS cosas por separado (10 abonados + 15 retenidos
    sobre 100 → 75), igual que el aplicador. El saldo no cambia respecto de la
    regla del 06/08; lo que cambia es que el abono queda en 10, no en 25."""
    from modules.retenciones import queries as q
    stub = _FetchAllStub(
        por_completo=[_fac(1234, "001-099-000001234", importe=100.0,
                           abono=10.0, saldo=90.0, stat="A")],
        clientes=[{"codigo_cli": "EDU", "nombre": "EDUARDO"}],
    )
    _patch(monkeypatch, stub, {"001-099-000001234": {"ret_total": 15.0}})
    out = q.preview_retenciones_asinfo("2026-08-01", "2026-08-06")
    fila = out["filas"][0]
    assert fila["estado"] == "se_aplica"
    assert fila["saldo_nuevo"] == 75.0  # 100 − 10 abonados − 15 retenidos
    assert out["resumen"]["se_aplica"] == 1


def test_preview_rete_gt_saldo(monkeypatch):
    """Espejo del freno del aplicador: la retención dejaría el saldo negativo
    → 'rete_gt_saldo', no tildable."""
    from modules.retenciones import queries as q
    stub = _FetchAllStub(
        por_completo=[_fac(1234, "001-099-000001234", importe=100.0,
                           abono=90.0, saldo=10.0, stat="A")],
        clientes=[{"codigo_cli": "EDU", "nombre": "EDUARDO"}],
    )
    _patch(monkeypatch, stub, {"001-099-000001234": {"ret_total": 27.01}})
    out = q.preview_retenciones_asinfo("2026-08-01", "2026-08-06")
    fila = out["filas"][0]
    assert fila["estado"] == "rete_gt_saldo"
    assert fila["saldo_nuevo"] is None
    assert out["resumen"]["rete_gt_saldo"] == 1
    assert out["resumen"]["se_aplica"] == 0


def test_preview_por_numf_tambien_se_puede_aplicar(monkeypatch):
    """Encontrada por numf y SIN retención registrada → se aplica (no queda
    escondida en 'sin factura', que no es tildable)."""
    from modules.retenciones import queries as q
    stub = _FetchAllStub(
        por_numf=[_fac(178485, None, importe=1000.0, abono=0.0, saldo=1000.0)],
        retenciones=[],
        clientes=[{"codigo_cli": "EDU", "nombre": "EDUARDO"}],
    )
    _patch(monkeypatch, stub, {"001-099-000178485": {"ret_total": 100.0}})
    out = q.preview_retenciones_asinfo("2026-06-04", "2026-08-03")
    fila = out["filas"][0]
    assert fila["estado"] == "se_aplica"
    # 'Z': sin abono, la retención baja el saldo pero no abona nada (07/08).
    assert fila["saldo_nuevo"] == 900.0 and fila["stat_nuevo"] == "Z"
    assert out["resumen"]["se_aplica"] == 1
    assert out["resumen"]["total_a_aplicar"] == 100.0


def test_preview_sin_factura_de_verdad(monkeypatch):
    """Las que NO están por ningún camino siguen dando sin_factura: el fix no
    tapa el problema real, lo deja solo."""
    from modules.retenciones import queries as q
    stub = _FetchAllStub(por_completo=[], por_numf=[])
    _patch(monkeypatch, stub, {"001-099-000178485": {"ret_total": 382.40}})
    out = q.preview_retenciones_asinfo("2026-06-04", "2026-08-03")
    assert out["filas"][0]["estado"] == "sin_factura"
    assert out["filas"][0]["via_numf"] is False
    assert out["resumen"]["sin_factura"] == 1
    assert out["resumen"]["via_numf"] == 0


def test_preview_numf_completo_gana_y_no_marca_via_numf(monkeypatch):
    """Si matchea por SRI completo, esa gana y no se marca 'por N°'."""
    from modules.retenciones import queries as q
    stub = _FetchAllStub(
        por_completo=[_fac(180758, "001-099-000180758")],
        por_numf=[_fac(999, None, codigo_cli="OTRO", id_factura=99)],
        retenciones=[{"codigo_cli": "EDU", "numf": 180758}],
        clientes=[{"codigo_cli": "EDU", "nombre": "EDUARDO"}],
    )
    _patch(monkeypatch, stub, {"001-099-000180758": {"ret_total": 276.85}})
    out = q.preview_retenciones_asinfo("2026-06-04", "2026-08-03")
    fila = out["filas"][0]
    assert fila["estado"] == "ya" and fila["via_numf"] is False
    assert fila["numf"] == 180758
    # ni siquiera sale a buscar por numf si ya matchearon todas
    assert not any("from scintela.factura" in s and "numf = any" in s
                   for s in stub.vistas)


def test_preview_rete_0_no_busca_factura(monkeypatch):
    from modules.retenciones import queries as q
    stub = _FetchAllStub()
    _patch(monkeypatch, stub, {"001-099-000178485": {"ret_total": 0.0}})
    out = q.preview_retenciones_asinfo("2026-06-04", "2026-08-03")
    assert out["filas"][0]["estado"] == "rete_0"
    assert out["resumen"]["rete_0"] == 1


def test_preview_rete_mayor_al_importe(monkeypatch):
    from modules.retenciones import queries as q
    stub = _FetchAllStub(
        por_numf=[_fac(178485, None, importe=50.0)],
        clientes=[{"codigo_cli": "EDU", "nombre": "EDUARDO"}],
    )
    _patch(monkeypatch, stub, {"001-099-000178485": {"ret_total": 382.40}})
    out = q.preview_retenciones_asinfo("2026-06-04", "2026-08-03")
    assert out["filas"][0]["estado"] == "rete_gt_importe"
    assert out["resumen"]["rete_gt_importe"] == 1


def test_preview_sin_retenciones_no_toca_la_db(monkeypatch):
    from modules.retenciones import queries as q
    stub = _FetchAllStub()
    _patch(monkeypatch, stub, {})
    out = q.preview_retenciones_asinfo("2026-06-04", "2026-08-03")
    assert out == {"filas": [], "resumen": out["resumen"]}
    assert out["resumen"]["n_total"] == 0
    assert stub.vistas == []


def test_preview_numero_sin_digitos_no_rompe(monkeypatch):
    """Un `numero` raro no puede tirar abajo la pantalla entera."""
    from modules.retenciones import queries as q
    stub = _FetchAllStub()
    _patch(monkeypatch, stub, {"SIN-DIGITOS": {"ret_total": 10.0}})
    out = q.preview_retenciones_asinfo("2026-06-04", "2026-08-03")
    assert out["filas"][0]["estado"] == "sin_factura"


# ───────────────── el rastro de las sin-factura en el lote ─────────────────

def test_lote_lista_las_sin_factura(monkeypatch):
    """Un contador no se puede investigar: el lote tiene que decir CUÁLES."""
    from modules.asinfo import service as svc
    from modules.retenciones import queries as q
    monkeypatch.setattr(svc, "retenciones_periodo", lambda d, h: {
        "001-099-000178485": {"ret_total": 382.40},
        "001-099-000176816": {"ret_total": 367.89},
        "001-099-000180758": {"ret_total": 276.85},
    })
    outcomes = {"001-099-000178485": "sin_factura",
                "001-099-000176816": "sin_factura",
                "001-099-000180758": "ya"}
    monkeypatch.setattr(q, "_aplicar_una_por_numero",
                        lambda numero, rete, usuario, batch_id=None, solo_sin_abono=False: outcomes[numero])
    r = q.aplicar_retenciones_asinfo("2026-06-04", "2026-08-03", usuario="cron")
    assert r["n_sin_factura"] == 2
    assert r["sin_factura"] == [
        {"numero": "001-099-000178485", "numf": 178485, "rete": 382.40},
        {"numero": "001-099-000176816", "numf": 176816, "rete": 367.89},
    ]
    assert r["total_sin_factura"] == 750.29


def test_lote_topea_el_detalle(monkeypatch):
    """El contador es exacto aunque el listado se tope."""
    from modules.asinfo import service as svc
    from modules.retenciones import queries as q
    n = q._MAX_DETALLE + 5
    monkeypatch.setattr(svc, "retenciones_periodo", lambda d, h: {
        f"001-099-{i:09d}": {"ret_total": 1.0} for i in range(n)})
    monkeypatch.setattr(q, "_aplicar_una_por_numero",
                        lambda numero, rete, usuario, batch_id=None, solo_sin_abono=False: "sin_factura")
    r = q.aplicar_retenciones_asinfo("2026-06-04", "2026-08-03")
    assert r["n_sin_factura"] == n
    assert len(r["sin_factura"]) == q._MAX_DETALLE
