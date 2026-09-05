"""El vigía del servidor — `modules/_lib/vigia_servidor.py`.

Tamara 2026-09-05: *"necesito una alarma que cuando esté así se corrija rápido"*.
Cada minuto mira la memoria; si falta, barre huérfanos, reinicia Metabase si
se pasó de su tope, y avisa (campanita + mail) sin repetirse.
"""
from __future__ import annotations

import pytest

from modules._lib import vigia_servidor as v

#: Un "ahora" grande: los frenos comparan contra 0.0 (nunca avisó).
T = 1_000_000.0


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    monkeypatch.setattr(v, "_ultimo_aviso", 0.0)
    monkeypatch.setattr(v, "_ultimo_metabase", 0.0)
    monkeypatch.setattr(v, "_en_episodio", False)
    monkeypatch.setattr(v.time, "sleep", lambda s: None)


def _servidor(monkeypatch, libres, java_mb=600, despues=None):
    lecturas = iter([libres] + ([despues] if despues is not None else [libres] * 5))

    def _estado():
        d = next(lecturas)
        return {"total_mb": 4036, "disponible_mb": d, "usado_pct": 90.0,
                "falta_memoria": d < v.MEMORIA_MINIMA_MB,
                "procesos": [{"nombre": "java.exe", "cuantos": 1, "memoria_mb": java_mb, "cpu_pct": 0},
                             {"nombre": "chrome.exe", "cuantos": 900, "memoria_mb": 5400, "cpu_pct": 0}],
                "navegadores": 900, "programa": {"pid": 1, "memoria_mb": 90}}

    monkeypatch.setattr(v.servidor, "estado", _estado)


def _espias(monkeypatch, muertos=0):
    hecho = {"barridas": 0, "metabase": 0, "avisos": []}

    def _barrer():
        hecho["barridas"] += 1
        return muertos

    monkeypatch.setattr(v, "_barrer", _barrer)
    monkeypatch.setattr(v, "_reiniciar_metabase", lambda: (hecho.__setitem__("metabase", hecho["metabase"] + 1) or "Metabase reiniciado"))
    monkeypatch.setattr(v, "_avisar", lambda titulo, detalle, nivel="alerta", clave="": (hecho["avisos"].append((titulo, detalle, nivel)) or {"campanita": True, "mail": 1}))
    return hecho


def test_con_memoria_no_hace_nada(monkeypatch):
    _servidor(monkeypatch, 1900)
    hecho = _espias(monkeypatch)
    r = v.revisar(ahora=T)
    assert r["acciones"] == [] and r["aviso"] is None
    assert hecho["barridas"] == 0 and hecho["avisos"] == []


def test_sin_memoria_barre_y_avisa_diciendo_que_hizo(monkeypatch):
    _servidor(monkeypatch, 47, despues=900)
    hecho = _espias(monkeypatch, muertos=1200)
    r = v.revisar(ahora=T)
    assert r["acciones"] == ["maté 1200 navegadores huérfanos"]
    assert hecho["metabase"] == 0  # java estaba dentro de su tope
    titulo, detalle, nivel = hecho["avisos"][0]
    assert titulo == "le falta memoria" and nivel == "alerta"
    assert "47 MB libres" in detalle
    assert "chrome.exe ×900 5400 MB" in detalle
    assert "maté 1200 navegadores huérfanos" in detalle
    assert "Quedaron 900 MB" in detalle


def test_si_java_se_paso_del_tope_reinicia_metabase_una_vez_cada_dos_horas(monkeypatch):
    _servidor(monkeypatch, 47, java_mb=1600)
    hecho = _espias(monkeypatch)
    r = v.revisar(ahora=T)
    assert any("java tenía 1600 MB: Metabase reiniciado" in a for a in r["acciones"])
    assert hecho["metabase"] == 1
    _servidor(monkeypatch, 47, java_mb=1600)
    v.revisar(ahora=T + 600)
    assert hecho["metabase"] == 1  # todavía no pasaron dos horas
    _servidor(monkeypatch, 47, java_mb=1600)
    v.revisar(ahora=T + v._METABASE_CADA_S + 1)
    assert hecho["metabase"] == 2


def test_no_repite_el_aviso_mientras_dura_el_episodio(monkeypatch):
    hecho = _espias(monkeypatch)
    for i in range(5):
        _servidor(monkeypatch, 60)
        v.revisar(ahora=T + i * 60)
    assert len(hecho["avisos"]) == 1
    _servidor(monkeypatch, 60)
    v.revisar(ahora=T + v._AVISO_CADA_S + 1)
    assert len(hecho["avisos"]) == 2


def test_avisa_cuando_la_memoria_vuelve(monkeypatch):
    hecho = _espias(monkeypatch)
    _servidor(monkeypatch, 60)
    v.revisar(ahora=T)
    _servidor(monkeypatch, 1800)
    v.revisar(ahora=T + 60)
    assert hecho["avisos"][-1][0] == "la memoria volvió"
    assert hecho["avisos"][-1][2] == "ok"
    # Y con memoria de sobra no vuelve a avisar que volvió.
    _servidor(monkeypatch, 1800)
    v.revisar(ahora=T + 120)
    assert len(hecho["avisos"]) == 2


def test_el_reinicio_de_metabase_solo_corre_en_windows(monkeypatch):
    monkeypatch.setattr(v.sys, "platform", "linux")
    assert "no es Windows" in v._reiniciar_metabase()


def test_los_correos_son_los_de_los_administradores(monkeypatch):
    import db

    visto = {}

    def _fetch_all(sql, params=None, conn=None):
        visto["sql"] = sql
        return [{"email": "tamara@x.com"}, {"email": None}]

    monkeypatch.setattr(db, "fetch_all", _fetch_all)
    assert v._correos_admin() == ["tamara@x.com"]
    assert "'*'" in visto["sql"] and "usuarios.admin" in visto["sql"]


def test_el_vigia_no_arranca_bajo_pytest():
    assert v.arrancar_en_segundo_plano() is False


def test_la_pantalla_muestra_al_vigia(app, fake_db, monkeypatch):
    from modules._lib import servidor

    monkeypatch.setattr(servidor, "estado", lambda: {
        "total_mb": 4036, "disponible_mb": 1900, "usado_pct": 50.0, "cpu_pct": 3.0,
        "nucleos": 2, "falta_memoria": False, "procesos": [], "navegadores": 8,
        "programa": {"pid": 1, "memoria_mb": 90}, "memoria_minima_mb": 400})
    monkeypatch.setattr(v, "estado", lambda: {"prendido": True, "hace_s": 12,
                                              "episodio_hace_s": 3600,
                                              "acciones": ["maté 1200 navegadores huérfanos"]})
    rid = fake_db.add_role("Tester", ["admin_dbase.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    html = c.get("/admin/pantallas").get_data(as_text=True)
    assert "mira la memoria cada minuto" in html
    assert "hace 60 min" in html
    assert "maté 1200 navegadores huérfanos" in html
