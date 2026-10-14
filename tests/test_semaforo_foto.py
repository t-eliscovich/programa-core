"""El bloque "Cobranza" del mail diario (TMT 2026-10-13): quién salió y quién
entró al rojo contra la foto anterior, los pagos grandes y el total en rojo."""
from modules.analisis import semaforo_foto as sf
from modules.informes import dia


def _c(cod, color, saldo, reb=0, nombre="", motivos=None):
    return {"cod": cod, "color": color, "saldo": saldo, "reb": reb,
            "nombre": nombre or cod, "motivos": motivos or []}


def _d(*filas):
    return {f["cod"]: f for f in filas}


def test_sale_del_rojo_el_que_ayer_era_rojo_y_hoy_no():
    antes = {"GUG": _c("GUG", "rojo", 4300), "MSS": _c("MSS", "rojo", 2000)}
    hoy = _d(_c("GUG", "amar", 1000), _c("MSS", "rojo", 2000))
    r = sf.comparar(antes, hoy, {"GUG": 3300})
    assert [x["cod"] for x in r["salieron"]] == ["GUG"]
    assert r["salieron"][0]["color"] == "amar"
    assert r["salieron"][0]["pagado"] == 3300
    assert r["entraron"] == []


def test_el_que_pago_todo_y_no_esta_mas_tambien_salio():
    r = sf.comparar({"AAA": _c("AAA", "rojo", 5000)}, {})
    assert r["salieron"][0]["color"] is None


def test_entra_al_rojo_con_su_motivo_y_tambien_el_nuevo():
    antes = {"AAA": _c("AAA", "amar", 5000)}
    hoy = _d(_c("AAA", "rojo", 5000, motivos=["Factura impaga de 151 días"]),
             _c("BBB", "inc", 9000, motivos=["Cheque devuelto"]))
    r = sf.comparar(antes, hoy)
    assert [x["cod"] for x in r["entraron"]] == ["BBB", "AAA"]
    assert r["entraron"][1]["motivo"] == "Factura impaga de 151 días"


def test_el_rojo_chico_no_cuenta_salvo_devuelto():
    antes = {"CHI": _c("CHI", "rojo", 300), "DEV": _c("DEV", "rojo", 300, reb=300)}
    r = sf.comparar(antes, {})
    assert [x["cod"] for x in r["salieron"]] == ["DEV"]
    assert r["total_antes"] == {"n": 1, "saldo": 300}


def test_total_en_rojo_hoy_y_antes():
    antes = {"A": _c("A", "rojo", 1000), "B": _c("B", "rojo", 2000)}
    hoy = _d(_c("A", "rojo", 1500), _c("C", "verde", 9000))
    r = sf.comparar(antes, hoy)
    assert r["total"] == {"n": 1, "saldo": 1500}
    assert r["total_antes"] == {"n": 2, "saldo": 3000}


def test_pagos_por_cliente_y_formas():
    cobros = [
        {"cod": "LUT", "nombre": "L", "importe": 20000, "no_banco": 10, "dias": 60},
        {"cod": "LUT", "nombre": "L", "importe": 1086.49, "no_banco": 90, "dias": 0},
        {"cod": "VGA", "nombre": "V", "importe": 4871.54, "no_banco": 99, "dias": 0},
        {"cod": "XYZ", "nombre": "X", "importe": 6000, "no_banco": 10, "dias": 1},
    ]
    pc = sf.por_cliente(cobros)
    assert pc["LUT"]["formas"] == ["cheque a 60 días", "depósito"]
    grandes = sf.pagos_grandes(pc, 5000)
    assert [g["cod"] for g in grandes] == ["LUT", "XYZ"]
    assert grandes[1]["formas"] == ["cheque al día"]


def _bloque(**kw):
    base = {"salieron": [], "entraron": [], "pagos": [], "umbral": 5000,
            "total": {"n": 14, "saldo": 230000},
            "total_antes": {"n": 15, "saldo": 241000}}
    base.update(kw)
    return dia._cobranza_semaforo_html(base, "hoy")


def test_html_dia_quieto_solo_total():
    h = _bloque()
    assert "Salieron del rojo" not in h and "Entraron" not in h
    assert "En rojo: <b>14</b>" in h and "ayer 15" in h


def test_html_sin_foto_anterior_no_compara():
    assert "ayer" not in _bloque(total_antes=None)


def test_html_corta_en_cinco_y_dice_cuantos_mas():
    sal = [{"cod": f"C{i}", "nombre": "x", "color": "verde", "antes": 1, "ahora": 0,
            "pagado": 0} for i in range(7)]
    h = _bloque(salieron=sal)
    assert "C4" in h and "C5" not in h and "+2 más" in h


def test_el_bloque_no_tumba_el_mail(monkeypatch):
    def _explota(*a, **k):
        raise RuntimeError("sin base")
    monkeypatch.setattr(sf, "para_la_nota", _explota)
    from datetime import date
    assert dia._cobranza_semaforo(date(2026, 10, 13), "hoy") == ""


def test_rojo_a_incobrable_no_sale_del_rojo():
    r = sf.comparar({"A": _c("A", "rojo", 2000)}, _d(_c("A", "inc", 2000)))
    assert r["salieron"] == [] and r["entraron"] == []


def test_foto_vieja_sin_saldo_no_inventa_el_total_de_ayer():
    antes = {"A": {"color": "rojo", "saldo": None, "reb": None, "motivos": "x · y"}}
    r = sf.comparar(antes, _d(_c("A", "verde", 0)))
    assert [x["cod"] for x in r["salieron"]] == ["A"]
    assert r["total_antes"] is None
    assert "ayer" not in dia._cobranza_semaforo_html(
        dict(r, pagos=[], umbral=5000), "hoy")


def test_el_motivo_de_la_foto_es_el_primero():
    assert sf._primer_motivo("Factura impaga de 160 días · Debe 1,3 veces") == \
        "Factura impaga de 160 días"


def test_la_foto_guarda_nombre_saldo_y_devueltos(monkeypatch):
    from datetime import date

    from modules.mi_cartera import avisos_vend as av
    ejec = []

    class _DB:
        def execute(self, sql, params=None, conn=None):
            ejec.append((sql, params))
            return 1

        def fetch_one(self, *a, **k):
            return {"f": None}

        def fetch_all(self, *a, **k):
            return []
    monkeypatch.setattr(av, "db", _DB())
    av.foto_semaforo(date(2026, 10, 13), colores={
        "GUG": {"color": "rojo", "motivos": ["m"], "nombre": "TEXTILES", "saldo": 4300,
                "reb": 0}})
    (sql, p), = ((s, p) for s, p in ejec if "INSERT INTO scintela.semaforo_foto" in s)
    assert p[-3:] == (["TEXTILES"], [4300.0], [0.0])
    assert any("DELETE FROM scintela.semaforo_foto" in s for s, _ in ejec)


def test_el_mail_pisa_la_foto_del_cierre_y_la_migracion_agrega_el_saldo():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    d = (root / "modules/informes/dia.py").read_text(encoding="utf-8")
    i = d.index("def enviar_nota(")
    assert "_guardar_foto_semaforo(fecha)" in d[i:d.index("def enviar_nota_finde(")]
    sql = (root / "migrations/0262_semaforo_foto_saldo.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS saldo" in sql


def test_sin_foto_anterior_no_dice_quien_entro_ni_salio(monkeypatch):
    monkeypatch.setattr(sf.cobranza, "colores",
                        lambda: {"A": _c("A", "rojo", 5000, motivos=["x"])})
    monkeypatch.setattr(sf, "cobros_del_tramo", lambda d, h: [])
    monkeypatch.setattr(sf, "foto_anterior", lambda d: (None, {}))
    r = sf.para_la_nota("2026-10-13", "2026-10-13")
    assert r["entraron"] == [] and r["salieron"] == []
    assert r["total"] == {"n": 1, "saldo": 5000}
