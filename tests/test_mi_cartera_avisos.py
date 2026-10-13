"""La campanita del vendedor y "Avisar un problema" (13/10/2026).

Lo que protegen, en orden de gravedad:
  1. Que "pasó a rojo" sea un CAMBIO contra la foto anterior — el primer día
     no se avisa nada (si no, a cada vendedor le llegan de golpe todos sus
     rojos), y un cliente que ya era rojo ayer no se avisa de nuevo.
  2. Que el incobrable cuente como rojo para el vendedor (como en su ficha).
  3. Que los avisos de problemas le lleguen sólo a quien tiene `*`.
  4. Que la campanita nunca rompa una pantalla.
"""
from __future__ import annotations

from datetime import date

import bcrypt
import pytest

from modules.mi_cartera import avisos_vend as av


class _DB:
    """Base de mentira: graba lo que se ejecuta y contesta lo que se le pide."""

    def __init__(self, anterior=None, colores_ant=None, fichas=None):
        self.anterior = anterior
        self.colores_ant = colores_ant or []
        self.fichas = fichas or []
        self.ejecutado: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None, conn=None):
        self.ejecutado.append((sql, params))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.ejecutado.append((sql, params))
        if "INSERT INTO scintela.reporte_app" in sql:
            return {"id_reporte": 7}
        if "UPDATE scintela.reporte_app" in sql:
            return {"vend": "PPR", "texto": "no anda"}
        return None

    def fetch_one(self, sql, params=None, conn=None):
        if "MAX(fecha)" in sql:
            return {"f": self.anterior}
        return None

    def fetch_all(self, sql, params=None, conn=None):
        if "FROM scintela.semaforo_foto" in sql:
            return self.colores_ant
        if "FROM scintela.cliente" in sql:
            return [f for f in self.fichas if f["cod"] in params[0]]
        return []

    def avisos(self):
        return [p for s, p in self.ejecutado if "INSERT INTO scintela.vendedor_aviso" in s]


HOY = date(2026, 10, 14)
COLORES = {
    "GIC": {"color": "rojo", "motivos": ["Factura impaga de 152 días"]},
    "MYU": {"color": "rojo", "motivos": ["Tiene facturas impagas y no compró en 90 días"]},
    "ABC": {"color": "inc", "motivos": ["Factura impaga de 400 días"]},
    "VER": {"color": "verde", "motivos": []},
}
FICHAS = [{"cod": c, "nombre": f"CLIENTE {c}", "vend": "PPR"} for c in COLORES]


def test_el_primer_dia_solo_saca_la_foto(monkeypatch):
    fake = _DB(anterior=None, fichas=FICHAS)
    monkeypatch.setattr(av, "db", fake)
    r = av.foto_semaforo(HOY, colores=COLORES)
    assert r["pasaron"] == 0
    assert fake.avisos() == []
    # pero la foto sí se grabó, con los cuatro clientes
    (sql, params), = ((s, p) for s, p in fake.ejecutado
                      if "INSERT INTO scintela.semaforo_foto" in s)
    assert sorted(params[1]) == ["ABC", "GIC", "MYU", "VER"]


def test_avisa_solo_los_que_pasaron_a_rojo(monkeypatch):
    fake = _DB(anterior=date(2026, 10, 13), fichas=FICHAS, colores_ant=[
        {"codigo_cli": "GIC", "color": "amar"},   # pasó → aviso
        {"codigo_cli": "MYU", "color": "rojo"},   # ya era rojo → nada
        {"codigo_cli": "ABC", "color": "verde"},  # pasó a incobrable → aviso (rojo para él)
        {"codigo_cli": "VER", "color": "verde"},
    ])
    monkeypatch.setattr(av, "db", fake)
    r = av.foto_semaforo(HOY, colores=COLORES)
    assert r["pasaron"] == 2
    claves = sorted(p[-1] for p in fake.avisos())
    assert claves == ["rojo:ABC:2026-10-14", "rojo:GIC:2026-10-14"]
    gic = next(p for p in fake.avisos() if p[-1].startswith("rojo:GIC"))
    assert gic[0] == "PPR" and "pasó a rojo" in gic[1]
    assert gic[2] == "Factura impaga de 152 días"
    assert gic[3] == "/mi-cartera/cliente/GIC"


def test_un_cliente_nuevo_que_aparece_en_rojo_tambien_avisa(monkeypatch):
    fake = _DB(anterior=date(2026, 10, 13), fichas=FICHAS, colores_ant=[])
    monkeypatch.setattr(av, "db", fake)
    assert av.foto_semaforo(HOY, colores={"GIC": COLORES["GIC"]})["pasaron"] == 1


def test_la_campanita_nunca_rompe(monkeypatch):
    class Roto:
        def __getattr__(self, _):
            def f(*a, **k):
                raise RuntimeError("sin base")
            return f
    monkeypatch.setattr(av, "db", Roto())
    monkeypatch.setattr(av, "asegurar_foto_de_hoy", lambda: None)
    monkeypatch.setattr(av, "_competencia", lambda v: (None, False))
    assert av.n_nuevos("PPR") == 0
    assert av.lista("PPR") == ([], [])
    av.marcar_vistos("PPR")  # no tira


def test_el_reporte_valida_y_avisa_a_la_oficina(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(av, "db", fake)
    dejados = []
    from modules.avisos import queries as avisos
    monkeypatch.setattr(avisos, "avisar", lambda **k: dejados.append(k) or True)
    with pytest.raises(ValueError):
        av.guardar_reporte(usuario="pablo", vend="PPR", texto="  ", pantalla="",
                           dispositivo="", user_agent="")
    with pytest.raises(ValueError):
        av.guardar_reporte(usuario="pablo", vend="PPR", texto="x", pantalla="",
                           dispositivo="", user_agent="",
                           foto=b"0" * (av.FOTO_MAX_BYTES + 1), foto_tipo="image/png")
    rid = av.guardar_reporte(usuario="pablo", vend="PPR", texto="no anda el stock",
                             pantalla="/mi-cartera/stock", dispositivo="Android",
                             user_agent="UA", foto=b"PDF", foto_tipo="application/pdf")
    assert rid == 7
    ins = next(p for s, p in fake.ejecutado if "INSERT INTO scintela.reporte_app" in s)
    assert ins[6] is None and ins[7] is None  # lo que no es imagen no se guarda
    (d,) = dejados
    assert d["fuente"] == "reporte_app" and d["url"] == "/admin/reportes-app/7"


def test_responder_le_llega_al_vendedor_y_resuelve_el_aviso(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(av, "db", fake)
    from modules.avisos import queries as avisos
    resueltos = []
    monkeypatch.setattr(avisos, "abiertos_por_clave",
                        lambda pre: [{"id_aviso": 3, "clave": "reporte_app:7"},
                                     {"id_aviso": 4, "clave": "reporte_app:70"}])
    monkeypatch.setattr(avisos, "resolver", lambda i, **k: resueltos.append(i) or True)
    assert av.responder(7, "Ya está, probá de nuevo", por="tamara") is True
    (p,) = fake.avisos()
    assert p[0] == "PPR" and p[-1] == "respuesta:7"
    assert resueltos == [3]  # el 70 empieza igual pero es otro


# ---------------------------------------------------------------------------
# Las pantallas
# ---------------------------------------------------------------------------

def _hash(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4))


@pytest.fixture
def vendedor(app, client, fake_db, monkeypatch):
    rid = fake_db.add_role("Vendedor", ["micartera.ver"])
    fake_db.add_user("pablo", _hash("Vendedor2026"), rid, vend="PPR")
    monkeypatch.setattr(av, "n_nuevos", lambda v: 0)
    r = client.post("/login", data={"username": "pablo", "password": "Vendedor2026"})
    assert r.status_code in (302, 303)
    return client


def test_la_bandeja_no_la_ve_un_vendedor(vendedor):
    assert vendedor.get("/admin/reportes-app").status_code == 404


def test_el_vendedor_manda_un_problema(vendedor, monkeypatch):
    guardados = []
    monkeypatch.setattr(av, "guardar_reporte", lambda **k: guardados.append(k) or 1)
    monkeypatch.setattr(av, "reportes_de", lambda u: [])
    assert vendedor.get("/mi-cartera/avisar-problema").status_code == 200
    r = vendedor.post("/mi-cartera/avisar-problema",
                      data={"texto": "no abre", "pantalla": "/mi-cartera/stock"})
    assert r.status_code in (302, 303) and "ok=1" in r.headers["Location"]
    (g,) = guardados
    assert g["usuario"] == "pablo" and g["vend"] == "PPR" and g["pantalla"] == "/mi-cartera/stock"


def test_abrir_los_avisos_los_marca_vistos(vendedor, monkeypatch):
    vistos = []
    monkeypatch.setattr(av, "lista", lambda v: ([{"tipo": "rojo", "titulo": "GIC pasó a rojo",
                                                  "detalle": None, "url": "#", "cuando": "hoy"}], []))
    monkeypatch.setattr(av, "marcar_vistos", lambda v: vistos.append(v))
    r = vendedor.get("/mi-cartera/avisos")
    assert r.status_code == 200 and "GIC pasó a rojo" in r.data.decode()
    assert vistos == ["PPR"]


# ---------------------------------------------------------------------------
# Mensajes con Jonathan (formulas_app.memo_mensajes)
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402

from modules._lib import formulas_memos as fm  # noqa: E402


@pytest.fixture
def pool_fake():
    original = fm._pool
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    fm._pool = MagicMock()
    fm._pool.getconn.return_value = conn
    yield cur, conn
    fm._pool = original


def test_el_vendedor_no_escribe_en_un_memo_ajeno(pool_fake):
    cur, conn = pool_fake
    cur.fetchone.return_value = None  # el memo no es suyo
    assert fm.escribir_mensaje("PDCL-1", "PPR", "hola") == (False, "sin_memo")
    assert not any("INSERT" in str(c) for c in cur.execute.call_args_list)
    conn.commit.assert_not_called()


def test_el_vendedor_escribe_en_su_memo(pool_fake):
    cur, conn = pool_fake
    cur.fetchone.return_value = (1,)
    assert fm.escribir_mensaje("PDCL-1", "ppr", "  para el viernes  ") == (True, "ok")
    sql, params = cur.execute.call_args_list[-1].args
    assert "INSERT INTO memo_mensajes" in sql and params == ("PDCL-1", "PPR", "para el viernes")
    conn.commit.assert_called_once()


def test_mensaje_vacio_ni_pregunta(pool_fake):
    cur, _ = pool_fake
    assert fm.escribir_mensaje("PDCL-1", "PPR", "   ") == (False, "vacio")
    cur.execute.assert_not_called()


def test_sin_bridge_los_mensajes_no_rompen():
    original = fm._pool
    fm._pool = None
    try:
        assert fm.mensajes(["X"]) == {}
        assert fm.escribir_mensaje("X", "PPR", "hola") == (False, "sin_bridge")
        assert fm.nuevos_de_la_fabrica("PPR") == []
        assert fm.mensajes_disponible() is False
        fm.marcar_leidos_por_vendedor(["X"])
    finally:
        fm._pool = original


def test_los_mensajes_de_jonathan_van_a_la_campanita(monkeypatch):
    from datetime import UTC, datetime
    monkeypatch.setattr(av, "charla_disponible", lambda: True)
    monkeypatch.setattr(fm, "nuevos_de_la_fabrica", lambda v: [
        {"numero": "PDCL-32677", "cliente": "GIC", "texto": "Sale el jueves",
         "creado_en": datetime(2026, 10, 13, 19, 20, tzinfo=UTC), "leido": False},
        {"numero": "PDCL-1", "cliente": "", "texto": "viejo",
         "creado_en": datetime(2026, 10, 1, tzinfo=UTC), "leido": True}])
    monkeypatch.setattr(av, "db", _DB())
    monkeypatch.setattr(av, "asegurar_foto_de_hoy", lambda: None)
    monkeypatch.setattr(av, "_competencia", lambda v: (None, False))
    nuevos, viejos = av.lista("PPR")
    assert [x["titulo"] for x in nuevos] == ["Jonathan, sobre PDCL-32677 (GIC)"]
    assert nuevos[0]["url"] == "/mi-cartera/pedidos#ped-PDCL-32677"
    assert [x["detalle"] for x in viejos] == ["viejo"]
    assert av.n_nuevos("PPR") == 1


def test_charlas_cuenta_solo_lo_no_leido_de_la_fabrica(monkeypatch):
    from datetime import UTC, datetime
    t = datetime(2026, 10, 13, 15, tzinfo=UTC)
    monkeypatch.setattr(av, "charla_disponible", lambda: True)
    monkeypatch.setattr(fm, "mensajes", lambda nums: {"A": [
        {"de": "vendedor", "texto": "?", "creado_en": t, "leido_en": None},
        {"de": "fabrica", "texto": "sí", "creado_en": t, "leido_en": None},
        {"de": "fabrica", "texto": "ok", "creado_en": t, "leido_en": t}]})
    charla, nuevos = av.charlas(["A"], "PPR")
    assert [m["de"] for m in charla["A"]] == ["vendedor", "fabrica", "fabrica"]
    assert nuevos == {"A": 1}


def test_mandar_mensaje_de_un_pedido_ajeno_da_404(vendedor, monkeypatch):
    from modules.mi_cartera import views
    monkeypatch.setattr(views.pedidos_service, "por_pedido",
                        lambda *a, **k: ([{"numero": "PDCL-9", "dueno": {"codigo": "EDG"}}], True))
    escritos = []
    monkeypatch.setattr(fm, "escribir_mensaje", lambda *a: escritos.append(a) or (True, "ok"))
    r = vendedor.post("/mi-cartera/pedidos/mensaje", data={"numero": "PDCL-9", "texto": "hola"})
    assert r.status_code == 404 and escritos == []


def test_mandar_mensaje_de_un_pedido_propio(vendedor, monkeypatch):
    from modules.mi_cartera import views
    monkeypatch.setattr(views.pedidos_service, "por_pedido",
                        lambda *a, **k: ([{"numero": "PDCL-9", "dueno": {"codigo": "PPR"}}], True))
    escritos = []
    monkeypatch.setattr(views.formulas_memos, "escribir_mensaje",
                        lambda *a: escritos.append(a) or (True, "ok"))
    r = vendedor.post("/mi-cartera/pedidos/mensaje", data={"numero": "PDCL-9", "texto": "hola"})
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("#ped-PDCL-9")
    assert escritos == [("PDCL-9", "PPR", "hola")]
