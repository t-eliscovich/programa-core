"""Las 5 sugerencias del día para la competencia (Inicio de Mi Cartera).

Dueña 28/09/2026: *"ofrezcamos parecido para clientes que ya les vendieron…
esta tela en estos colores a x persona. y ya manda el mensaje por whatsapp"*
· *"queremos sacar cantidad no puntos"* · *"no mostremos segunda y habla con
usted"* · *"el código en la misma línea"* · *"que cambie diario"* · *"agrega
para trackear en uso de la app esto"*.
"""
from __future__ import annotations

from datetime import date
from urllib.parse import unquote

import bcrypt
import pytest

from modules.mi_cartera import queries as q
from modules.mi_cartera import sugerencias as s


def _saldo():
    return {
        "Microfibra": [
            {"color": "NEG", "kg": 772, "segunda": True},
            {"color": "FRE", "kg": 539},
            {"color": "PLO", "kg": 310},
            {"color": "ATQ", "kg": 239},
            {"color": "CHI", "kg": 9},       # menos de 15 kg: no se ofrece
        ],
        "Fleece 102": [{"color": "AMR", "kg": 212}, {"color": "CPA", "kg": 167}],
        "Beltis": [{"color": "PAL", "kg": 124}],
    }


# ── La regla ────────────────────────────────────────────────────────────────

def test_ordena_por_kilos_y_no_por_puntos():
    """Una tela que el cliente lleva en volumen va antes que una chica, sin
    mirar cuántos puntos vale."""
    llam = [{"codigo_cli": "AAA", "tela": "Beltis", "kg": 1200},       # 200 kg/2 meses → topa 124
            {"codigo_cli": "BBB", "tela": "Fleece 102", "kg": 6000}]  # topa en 379
    out = s.elegir(_saldo(), [], llam, {"Beltis", "Fleece 102"})
    assert [o["codigo_cli"] for o in out] == ["BBB", "AAA"]
    assert out[0]["kg_posible"] == 379.0
    assert out[1]["kg_posible"] == 124.0


def test_no_le_ofrece_los_colores_que_ya_se_llevo_ni_los_chicos():
    compras = [{"codigo_cli": "CEL", "tela": "Microfibra", "colores": {"NEG"}, "kg": 61}]
    out = s.elegir(_saldo(), compras, [], {"Microfibra"})
    assert [c["color"] for c in out[0]["colores"]] == ["FRE", "PLO", "ATQ"]
    # Lo que compró en la carrera es "lo que suele llevar".
    assert out[0]["kg_posible"] == 61.0


def test_un_cliente_una_vez_y_una_tela_dos_veces_como_mucho():
    llam = [{"codigo_cli": c, "tela": "Microfibra", "kg": 900} for c in ("A1", "A2", "A3")]
    llam += [{"codigo_cli": "A1", "tela": "Fleece 102", "kg": 900}]
    out = s.elegir(_saldo(), [], llam, {"Microfibra", "Fleece 102"})
    cods = [o["codigo_cli"] for o in out]
    assert len(cods) == len(set(cods))
    assert sum(1 for o in out if o["tela"] == "Microfibra") == 2


def test_primero_lo_que_el_vendio_despues_el_relleno():
    """RMY y JQU vendieron poco: se completa con otras telas, pero sin pasar
    por delante de las que sí vendió."""
    llam = [{"codigo_cli": "X", "tela": "Beltis", "kg": 60},
            {"codigo_cli": "Y", "tela": "Fleece 102", "kg": 9000}]
    out = s.elegir(_saldo(), [], llam, {"Beltis"}, cuantas=1)
    assert [o["codigo_cli"] for o in out] == ["X"]
    out = s.elegir(_saldo(), [], llam, {"Beltis"}, cuantas=5)
    assert {o["codigo_cli"] for o in out} == {"X", "Y"}


def test_como_mucho_cinco():
    llam = [{"codigo_cli": f"C{i}", "tela": t, "kg": 500}
            for i, t in enumerate(["Microfibra", "Fleece 102", "Beltis"] * 4)]
    assert len(s.elegir(_saldo(), [], llam, {"Microfibra", "Fleece 102", "Beltis"})) == 5


# ── El mensaje ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tel,esperado", [
    ("032366098 0986026503", "593986026503"),
    ("0998848999  2827803", "593998848999"),
    ("+593 987654321", "593987654321"),
    ("2634826", None),
    ("", None),
])
def test_celular(tel, esperado):
    assert s.celular(tel) == esperado


def test_el_celular_del_portal_gana():
    assert s.celular("0991111111", "0982222222") == "593991111111"


@pytest.mark.parametrize("nombre,pila", [
    ("ANDRADE ZAMBRANO IDA ISABEL", "Ida"),
    ("HOMETEXTIL S.A.", ""),
    ("CARLEX CIA LTDA", ""),
    ("CALI HECTOR", ""),
])
def test_nombre_de_pila(nombre, pila):
    assert s.nombre_de_pila(nombre) == pila


def test_el_mensaje_habla_de_usted_y_no_dice_segunda():
    sug = {"nombre": "ANDRADE ZAMBRANO IDA ISABEL", "tela": "Microfibra 1.2",
           "whatsapp": "593999999999",
           "colores": [{"color": "COR"}, {"color": "BLA", "segunda": True},
                       {"color": "CIE"}]}
    m = s.mensaje(sug)
    assert m == ("Hola Ida, ¿cómo está? Le cuento que tenemos Microfibra 1.2 en "
                 "COR, BLA y CIE, lista para entregar. ¿Le separo?")
    assert "segunda" not in m.lower() and "seg" not in m.lower()
    link = s.link_whatsapp(sug)
    assert link.startswith("https://wa.me/593999999999?text=")
    assert unquote(link.split("text=")[1]) == m
    assert s.link_whatsapp({**sug, "whatsapp": None}) is None


# ── La pantalla ─────────────────────────────────────────────────────────────

def _hash(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4))


@pytest.fixture
def vendedor(app, client, fake_db):
    rid = fake_db.add_role("Vendedor", ["micartera.ver"])
    fake_db.add_user("felipe", _hash("Vendedor2026"), rid, vend="FL1")
    r = client.post("/login", data={"username": "felipe", "password": "Vendedor2026"})
    assert r.status_code in (302, 303)
    return client


def _inicio_vacio(monkeypatch):
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(q, "nombre_vendedor", lambda vend: "Felipe Lopez")
    monkeypatch.setattr(q, "ventas_kg", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "meta_periodo", lambda *a, **k: None)
    monkeypatch.setattr(q, "ventas_kg_por_semana", lambda *a, **k: [])
    monkeypatch.setattr(q, "cobrado", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "comision_mes", lambda *a, **k: 0.0)
    monkeypatch.setattr(q, "por_cobrar",
                        lambda vend: {"saldo": 0, "vencido": 0, "n_clientes": 0})


_SUG = {"orden": 1, "codigo_cli": "IIA", "nombre": "ANDRADE ZAMBRANO IDA ISABEL",
        "tela": "Microfibra 1.2", "kg_posible": 447.0, "whatsapp": "593999999999",
        "colores": [{"color": "COR", "kg": 164}, {"color": "BLA", "kg": 142, "segunda": True}]}


def test_la_tarjeta_va_arriba_con_el_codigo_en_la_linea(vendedor, monkeypatch):
    _inicio_vacio(monkeypatch)
    sug = {**_SUG, "link": s.link_whatsapp(_SUG)}
    monkeypatch.setattr(s, "del_dia", lambda vend, hoy: [sug])
    html = vendedor.get("/mi-cartera").data.decode()
    assert "Competencia · 1 sugerencia para hoy" in html
    assert "IIA · ANDRADE ZAMBRANO IDA ISABEL" in html
    assert "BLA seg" in html                       # el vendedor sí lo ve
    assert "compran estas telas" not in html      # la bajada se sacó
    assert "/whatsapp" in html and "wa.me" not in html  # pasa por el contador
    assert html.index("sug-card") < html.index('class="seg"')


def test_sin_sugerencias_no_hay_tarjeta(vendedor, monkeypatch):
    _inicio_vacio(monkeypatch)
    monkeypatch.setattr(s, "del_dia", lambda vend, hoy: [])
    assert 'class="card sug-card"' not in vendedor.get("/mi-cartera").data.decode()


def test_si_falla_la_base_el_inicio_sigue(vendedor, monkeypatch):
    _inicio_vacio(monkeypatch)
    monkeypatch.setattr(s, "_leer", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    r = vendedor.get("/mi-cartera")
    assert r.status_code == 200
    assert 'class="card sug-card"' not in r.data.decode()


def test_el_boton_anota_el_click_y_abre_whatsapp(vendedor, monkeypatch):
    anotados, usos = [], []
    monkeypatch.setattr(s, "una", lambda vend, f, o: {**_SUG, "link": s.link_whatsapp(_SUG)}
                        if (vend, f, o) == ("FL1", date(2026, 9, 28), 1) else None)
    monkeypatch.setattr(s, "anotar_whatsapp", lambda *a: anotados.append(a))
    import db
    real = db.execute
    monkeypatch.setattr(db, "execute", lambda sql, p=None, **k: usos.append(p)
                        if "uso_pantalla" in sql else real(sql, p, **k))
    r = vendedor.get("/mi-cartera/sugerencia/2026-09-28/1/whatsapp")
    assert r.status_code in (302, 303)
    assert r.headers["Location"].startswith("https://wa.me/593999999999")
    assert anotados == [("FL1", date(2026, 9, 28), 1)]
    assert usos and usos[0][3] == "mi_cartera.sugerencia_whatsapp" and usos[0][4] == "IIA"


def test_una_sugerencia_que_no_es_suya_da_404(vendedor, monkeypatch):
    monkeypatch.setattr(s, "una", lambda *a: None)
    assert vendedor.get("/mi-cartera/sugerencia/2026-09-28/9/whatsapp").status_code == 404
    assert vendedor.get("/mi-cartera/sugerencia/cualquiera/1/whatsapp").status_code == 404


def test_el_nombre_en_uso_de_la_app():
    from modules.uso.registro import NOMBRES, es_papel
    assert NOMBRES["mi_cartera.sugerencia_whatsapp"] == "WhatsApp de una sugerencia"
    # No es una impresión: se cuenta aparte, en su tabla.
    assert not es_papel("mi_cartera.sugerencia_whatsapp")


# ── La campanita ────────────────────────────────────────────────────────────

def test_campanita_en_clientes_y_numerito_en_inicio(vendedor, monkeypatch):
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(s, "pendientes", lambda vend, hoy: 5 if vend == "FL1" else 0)
    html = vendedor.get("/mi-cartera/clientes").data.decode()
    assert "5 sugerencias</b> de la competencia para hoy" in html
    assert "/mi-cartera#competencia" in html
    assert '<i class="cp-n">5</i>' in html


def test_en_el_inicio_no_va_el_aviso_porque_ya_esta_la_tarjeta(vendedor, monkeypatch):
    _inicio_vacio(monkeypatch)
    monkeypatch.setattr(s, "del_dia", lambda vend, hoy: [])
    monkeypatch.setattr(s, "pendientes", lambda vend, hoy: 5)
    html = vendedor.get("/mi-cartera").data.decode()
    assert 'class="campanita"' not in html


def test_sin_pendientes_no_hay_campanita(vendedor, monkeypatch):
    monkeypatch.setattr(q, "mis_clientes", lambda vend: [])
    monkeypatch.setattr(s, "pendientes", lambda vend, hoy: 0)
    html = vendedor.get("/mi-cartera/clientes").data.decode()
    assert 'class="campanita"' not in html and "cp-n" not in html.split("</style>")[-1]


def test_pendientes_se_apaga_cuando_abrio_el_inicio(monkeypatch):
    import db
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: {"si": 1})
    monkeypatch.setattr(s, "del_dia", lambda *a: [1, 2, 3, 4, 5])
    assert s.pendientes("FL1", date(2026, 9, 29)) == 0
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: None)
    assert s.pendientes("FL1", date(2026, 9, 29)) == 5
    assert s.pendientes("", date(2026, 9, 29)) == 0
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: 1 / 0)
    assert s.pendientes("FL1", date(2026, 9, 29)) == 0
    # Sin sugerencias hoy, no hay campanita.
    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(s, "del_dia", lambda *a: [])
    assert s.pendientes("FL1", date(2026, 9, 29)) == 0


def test_la_campanita_cuenta_solo_lo_que_abrio_despues_de_armarlas(monkeypatch):
    """El 28/09 FL1, RMY y EDG habían abierto el Inicio a la mañana, antes de
    que existieran las sugerencias, y la campanita no les salía."""
    import db
    vistos = []
    monkeypatch.setattr(db, "fetch_one", lambda sql, p=None, **k: vistos.append(sql))
    monkeypatch.setattr(s, "del_dia", lambda *a: [1])
    s.pendientes("FL1", date(2026, 9, 28))
    assert "creado_en" in vistos[0]


# ── Que cambie cada día (29/09/2026) ────────────────────────────────────────

def _muchos():
    return [{"codigo_cli": f"C{i}", "tela": t, "kg": 3000 - 100 * i}
            for i, t in enumerate(["Microfibra", "Fleece 102", "Beltis"] * 4)]


def test_un_cliente_que_salio_ayer_no_vuelve_si_hay_otros():
    telas = {"Microfibra", "Fleece 102", "Beltis"}
    ayer = s.elegir(_saldo(), [], _muchos(), telas)
    rec = {o["codigo_cli"]: 1 for o in ayer}
    hoy = s.elegir(_saldo(), [], _muchos(), telas, recientes=rec)
    assert len(hoy) == 5
    assert not ({o["codigo_cli"] for o in ayer} & {o["codigo_cli"] for o in hoy})


def test_si_no_alcanzan_vuelve_primero_el_que_salio_hace_mas():
    llam = [{"codigo_cli": c, "tela": "Microfibra", "kg": 900} for c in ("A", "B")]
    llam += [{"codigo_cli": "C", "tela": "Fleece 102", "kg": 900}]
    out = s.elegir(_saldo(), [], llam, {"Microfibra", "Fleece 102"},
                   recientes={"A": 1, "B": 3})
    assert [o["codigo_cli"] for o in sorted(out, key=lambda o: o["codigo_cli"])] == ["A", "B", "C"]
    # Con lugar para dos: el nuevo (C) y el que salió hace más (B).
    out = s.elegir(_saldo(), [], llam, {"Microfibra", "Fleece 102"}, cuantas=2,
                   recientes={"A": 1, "B": 3})
    assert {o["codigo_cli"] for o in out} == {"C", "B"}


def test_hace_mas_de_tres_dias_ya_no_cuenta():
    llam = [{"codigo_cli": "A", "tela": "Microfibra", "kg": 900},
            {"codigo_cli": "B", "tela": "Fleece 102", "kg": 100}]
    out = s.elegir(_saldo(), [], llam, {"Microfibra", "Fleece 102"}, cuantas=1,
                   recientes={"A": 4})
    assert [o["codigo_cli"] for o in out] == ["A"]
