"""El monto de un cheque YA aplicado a facturas arrastra a la factura.

TMT 2026-08-24 (dueña): *"Cuando edito un cheque pero que ya fue aplicado a
una factura, creo que no está funcionando bien. Cambié el monto"*. Era
cierto: `editar()` cambiaba `cheque.importe` y dejaba `chequesxfact` y
`factura.abono/saldo/stat` como estaban. La factura seguía diciendo que le
habían pagado una plata que el cheque ya no valía.

Ella eligió que el programa AJUSTE SOLO la aplicación y que ANTES muestre en
pantalla qué va a hacer. Estos tests cubren las tres piezas: la cuenta pura,
el freno de `editar()` sin confirmar, y la pantalla de confirmación.
"""

from __future__ import annotations

import contextlib
from datetime import date

import pytest

# ── la cuenta pura ──────────────────────────────────────────────────────────


def _ap(idx: int, id_fact: int, importe) -> dict:
    return {"id_chequexfact": idx, "id_fact": id_fact, "importe": importe}


def test_plan_recorte_monto_que_sube_no_toca_nada():
    from modules.cheques import queries

    assert queries._plan_recorte(1200, [_ap(1, 10, 1035.07)]) == []


def test_plan_recorte_monto_igual_no_toca_nada():
    from modules.cheques import queries

    assert queries._plan_recorte(1200, [_ap(1, 10, 1200)]) == []


def test_plan_recorte_baja_una_sola_aplicacion():
    """El caso que pisó la dueña: 1.200,00 aplicados enteros, monto a 1.035,07."""
    from modules.cheques import queries

    r = queries._plan_recorte(1035.07, [_ap(1, 279246, 1200)])
    assert len(r) == 1
    assert float(r[0]["recorte"]) == pytest.approx(164.93)
    assert float(r[0]["aplicado_despues"]) == pytest.approx(1035.07)
    assert r[0]["se_borra"] is False


def test_plan_recorte_baja_arranca_por_la_ULTIMA_aplicacion():
    """Se recorta desde la última factura pagada, no prorrateado entre todas."""
    from modules.cheques import queries

    aplic = [_ap(1, 10, 500), _ap(2, 11, 300), _ap(3, 12, 200)]
    r = queries._plan_recorte(600, aplic)  # sobran 400
    assert [x["id_fact"] for x in r] == [12, 11]
    assert float(r[0]["recorte"]) == pytest.approx(200)  # la 12 se va entera
    assert r[0]["se_borra"] is True
    assert float(r[1]["recorte"]) == pytest.approx(200)  # a la 11 le queda 100
    assert float(r[1]["aplicado_despues"]) == pytest.approx(100)
    assert r[1]["se_borra"] is False
    # La 10 no se toca: el recorte cortó antes de llegar a ella.
    assert 10 not in [x["id_fact"] for x in r]


def test_plan_recorte_cheque_negativo_recorta_hacia_cero():
    """Una nota de crédito es un cheque NEGATIVO: 'recortar' es subir a cero."""
    from modules.cheques import queries

    r = queries._plan_recorte(-80, [_ap(1, 10, -100)])
    assert len(r) == 1
    assert float(r[0]["recorte"]) == pytest.approx(-20)
    assert float(r[0]["aplicado_despues"]) == pytest.approx(-80)


# ── el freno de editar() ────────────────────────────────────────────────────


class _FakeDB:
    """Fake que SÍ filtra por los params — un fake que no filtra tapa el bug."""

    def __init__(self, cheque: dict, aplic: list[dict], facturas: dict[int, dict]):
        self.cheque = dict(cheque)
        self.aplic = [dict(a) for a in aplic]
        self.facturas = {k: dict(v) for k, v in facturas.items()}
        self.executes: list[tuple[str, tuple]] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        p = tuple(params or ())
        if "from scintela.cheque where id_cheque" in s:
            return dict(self.cheque) if p and p[0] == self.cheque["id_cheque"] else None
        # `por_id()` (el que usa la vista) busca por no_cheque O por id.
        if "from scintela.cheque c" in s:
            quiere = {str(x) for x in p}
            propios = {str(self.cheque["id_cheque"]), str(self.cheque["no_cheque"])}
            return dict(self.cheque) if quiere & propios else None
        if "from scintela.factura where id_factura" in s:
            f = self.facturas.get(p[0] if p else None)
            return {"retencion": 0, **f} if f else None
        if "from scintela.chequesxfact" in s and "limit 1" in s:
            vivas = [a for a in self.aplic if a["id_fact"] == p[1]]
            return {"x": 1} if vivas else None
        if "from scintela.mov_doble" in s:
            return None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        p = tuple(params or ())
        if "from scintela.chequesxfact cxf" in s:
            if not p or p[0] != self.cheque["id_cheque"]:
                return []
            fuera = []
            for a in self.aplic:
                f = self.facturas.get(a["id_fact"], {})
                fuera.append(
                    {
                        **a,
                        "numf": f.get("numf"),
                        "numf_completo": f.get("numf_completo"),
                        "fact_importe": f.get("importe"),
                        "fact_abono": f.get("abono"),
                        "fact_retencion": f.get("retencion", 0),
                        "fact_saldo": f.get("saldo"),
                        "fact_stat": f.get("stat"),
                    }
                )
            return fuera
        return []

    def execute(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        p = tuple(params or ())
        self.executes.append((s, p))
        if "delete from scintela.chequesxfact" in s:
            self.aplic = [a for a in self.aplic if a["id_chequexfact"] != p[0]]
        if "update scintela.chequesxfact" in s:
            for a in self.aplic:
                if a["id_chequexfact"] == p[2]:
                    a["importe"] = p[0]
        if "update scintela.factura" in s:
            f = self.facturas.get(p[4])
            if f:
                f["abono"], f["saldo"], f["stat"] = p[0], p[1], p[2]
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        return {"id_mov_doble": 1}

    @contextlib.contextmanager
    def tx(self):
        yield object()

    def apply_to(self, monkeypatch, db_mod):
        for n in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
            monkeypatch.setattr(db_mod, n, getattr(self, n))


def _fake_basico() -> _FakeDB:
    return _FakeDB(
        cheque={
            "id_cheque": 102656,
            "no_cheque": "4885",
            "codigo_cli": "IIA",
            "stat": "Z",
            "fechad": date(2026, 12, 14),
            "importe": 1200,
            "no_banco": 17,
            "doc_banco": None,
            "fecha": date(2026, 12, 14),
        },
        aplic=[{"id_chequexfact": 6890, "id_fact": 279246, "importe": 1200}],
        facturas={
            279246: {
                "id_factura": 279246,
                "numf": 180286,
                "numf_completo": "001-099-000180286",
                "importe": 4037.54,
                "abono": 3967.32,
                "retencion": 70.22,
                "saldo": 0,
                "stat": "T",
            }
        },
    )


@pytest.fixture(autouse=True)
def _sin_periodo_guard(monkeypatch):
    import periodo_guard

    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda f: None)


def test_editar_sin_confirmar_no_toca_el_cheque_aplicado(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _fake_basico()
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="aplicado a facturas"):
        queries.editar(102656, importe=1035.07, usuario="tamara")
    assert not any("update scintela.cheque" in s for s, _ in fake.executes)


def test_editar_sin_aplicaciones_sigue_cambiando_el_monto(monkeypatch):
    """El freno es ANGOSTO: un cheque sin aplicar se edita como siempre."""
    import db as db_mod
    from modules.cheques import queries

    fake = _fake_basico()
    fake.aplic = []
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(102656, importe=1035.07, usuario="tamara")
    assert res["ajuste"] is None
    assert any("update scintela.cheque" in s for s, _ in fake.executes)


def test_editar_confirmado_le_devuelve_el_saldo_a_la_factura(monkeypatch):
    import db as db_mod
    import mov_doble as md_mod
    from modules.cheques import queries

    fake = _fake_basico()
    fake.apply_to(monkeypatch, db_mod)
    monkeypatch.setattr(md_mod, "registrar", lambda **kw: 1)

    res = queries.editar(
        102656, importe=1035.07, ajustar_aplicaciones=True, usuario="tamara"
    )
    assert res["ajuste"]["facturas_tocadas"] == 1
    assert res["ajuste"]["total_recortado"] == pytest.approx(164.93)
    f = fake.facturas[279246]
    assert f["abono"] == pytest.approx(3802.39)
    assert f["saldo"] == pytest.approx(164.93)
    assert f["stat"] == "A"  # ya no está cancelada
    # La aplicación quedó recortada, no borrada.
    assert float(fake.aplic[0]["importe"]) == pytest.approx(1035.07)


def test_editar_confirmado_que_sube_no_toca_la_factura(monkeypatch):
    import db as db_mod
    import mov_doble as md_mod
    from modules.cheques import queries

    fake = _fake_basico()
    fake.cheque["importe"] = 1035.07
    fake.aplic[0]["importe"] = 1035.07
    fake.facturas[279246].update(abono=3802.39, saldo=164.93, stat="A")
    fake.apply_to(monkeypatch, db_mod)
    monkeypatch.setattr(md_mod, "registrar", lambda **kw: 1)

    res = queries.editar(
        102656, importe=1200, ajustar_aplicaciones=True, usuario="tamara"
    )
    assert res["ajuste"]["facturas_tocadas"] == 0
    assert fake.facturas[279246]["saldo"] == pytest.approx(164.93)


def test_editar_no_deja_dar_vuelta_el_signo(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _fake_basico()
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="signo"):
        queries.editar(
            102656, importe=-1200, ajustar_aplicaciones=True, usuario="tamara"
        )


# ── el plan que ve la pantalla ──────────────────────────────────────────────


def test_plan_cambio_importe_arma_la_explicacion(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _fake_basico()
    fake.apply_to(monkeypatch, db_mod)

    plan = queries.plan_cambio_importe(102656, 1035.07)
    assert plan["toca_facturas"] is True
    assert plan["total_recorte"] == pytest.approx(164.93)
    fila = plan["facturas"][0]
    assert fila["numf_completo"] == "001-099-000180286"
    assert fila["saldo_antes"] == pytest.approx(0)
    assert fila["saldo_despues"] == pytest.approx(164.93)
    assert fila["stat_antes"] == "T"
    assert fila["stat_despues"] == "A"
    assert fila["toca"] is True


def test_plan_cambio_importe_sin_aplicaciones_es_none(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _fake_basico()
    fake.aplic = []
    fake.apply_to(monkeypatch, db_mod)

    assert queries.plan_cambio_importe(102656, 1035.07) is None


def test_plan_cambio_importe_que_sube_muestra_el_sobrante(monkeypatch):
    import db as db_mod
    from modules.cheques import queries

    fake = _fake_basico()
    fake.cheque["importe"] = 1035.07
    fake.aplic[0]["importe"] = 1035.07
    fake.apply_to(monkeypatch, db_mod)

    plan = queries.plan_cambio_importe(102656, 1200)
    assert plan["toca_facturas"] is False
    assert plan["sobrante"] == pytest.approx(164.93)


# ── la pantalla: el POST no toca nada hasta que ella confirma ───────────────


def _client_logueado(app, monkeypatch, fake: _FakeDB):
    from flask import g

    import db as db_mod

    fake.apply_to(monkeypatch, db_mod)

    @app.before_request
    def _login_falso():
        g.user = {"id_usuario": 1, "username": "tamara", "nombre_rol": "Accionista"}
        g.permisos = {"*"}

    return app.test_client()


def test_el_post_muestra_la_pantalla_antes_de_tocar_la_factura(app, monkeypatch):
    fake = _fake_basico()
    cli = _client_logueado(app, monkeypatch, fake)

    r = cli.post("/cheques/102656/actualizar", data={"importe": "1035,07"})
    assert r.status_code == 200  # la pantalla, NO el redirect de "ya guardé"
    html = r.get_data(as_text=True)
    assert "Cambiar el monto del cheque" in html
    assert "001-099-000180286" in html
    assert "164,93" in html
    # Y no se guardó nada todavía.
    assert not any("update scintela.cheque" in s for s, _ in fake.executes)
    assert fake.facturas[279246]["stat"] == "T"


def test_el_post_confirmado_si_guarda(app, monkeypatch):
    import mov_doble as md_mod

    fake = _fake_basico()
    cli = _client_logueado(app, monkeypatch, fake)
    monkeypatch.setattr(md_mod, "registrar", lambda **kw: 1)

    r = cli.post(
        "/cheques/102656/actualizar",
        data={"importe": "1035,07", "ajuste_confirmado": "1"},
    )
    assert r.status_code == 302
    assert fake.facturas[279246]["stat"] == "A"
    assert fake.facturas[279246]["saldo"] == pytest.approx(164.93)


def test_sin_aplicaciones_el_post_guarda_de_una(app, monkeypatch):
    """No molestar con una pantalla cuando no hay ninguna factura en juego."""
    fake = _fake_basico()
    fake.aplic = []
    cli = _client_logueado(app, monkeypatch, fake)

    r = cli.post("/cheques/102656/actualizar", data={"importe": "1035,07"})
    assert r.status_code == 302
    assert any("update scintela.cheque" in s for s, _ in fake.executes)
