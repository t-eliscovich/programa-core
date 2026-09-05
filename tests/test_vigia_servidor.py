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


# ---------------------------------------------------------------------------
# Fase 2: la historia de la memoria y la tendencia
# ---------------------------------------------------------------------------


def _filas(libres_por_hora):
    from datetime import datetime, timedelta

    t0 = datetime(2026, 9, 1, 0, 0)
    return [{"leido_en": t0 + timedelta(hours=i), "libres_mb": mb, "total_mb": 4036,
             "java_mb": 900, "chrome_mb": 400, "chrome_n": 16, "python_mb": 300,
             "procesos": 200, "cpu_pct": 5.0}
            for i, mb in enumerate(libres_por_hora)]


def test_la_tendencia_ve_la_fuga_lenta_antes_del_minimo():
    # 4 días bajando 10 MB por hora: del día 0 al 3 son 720 MB menos.
    filas = _filas([1900 - 10 * i for i in range(96)])
    t = v.tendencia(filas)
    assert t["alerta"] is True and t["baja_mb"] > v.TENDENCIA_MB


def test_una_memoria_estable_no_es_tendencia():
    filas = _filas([1800 + (50 if i % 2 else -50) for i in range(96)])
    assert v.tendencia(filas)["alerta"] is False


def test_con_pocas_lecturas_no_opina():
    assert v.tendencia(_filas([1800] * 5))["alerta"] is False


def test_guarda_una_lectura_por_hora_y_no_mas(monkeypatch):
    import db

    escritas = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: escritas.append(sql) or 1)
    monkeypatch.setattr(v, "_ultima_guardada", 0.0)
    _espias(monkeypatch)
    for t in (T, T + 60, T + 3601):
        _servidor(monkeypatch, 1900)
        v.revisar(ahora=t)
    inserts = [s for s in escritas if "INSERT INTO scintela.servidor_memoria" in s]
    borrados = [s for s in escritas if "DELETE FROM scintela.servidor_memoria" in s]
    assert len(inserts) == 2 and len(borrados) == 2


def test_el_health_avisa_de_la_tendencia(monkeypatch):
    from modules._lib import servidor

    monkeypatch.setattr(servidor, "memoria", lambda: {"total_mb": 4036, "disponible_mb": 1200, "usado_pct": 70.0})
    monkeypatch.setattr(servidor, "procesos", lambda n=8: [])
    monkeypatch.setattr(servidor, "cpu", lambda: {"cpu_pct": 1.0, "nucleos": 2})
    monkeypatch.setattr(servidor, "este_proceso", lambda: {"pid": 1, "memoria_mb": 100})
    monkeypatch.setattr(servidor, "navegadores", lambda: 8)
    monkeypatch.setattr(v, "historia", lambda: _filas([1900 - 10 * i for i in range(96)]))
    h = servidor.health()
    assert h["ok"] is False
    assert h["alerts"][0]["tipo"] == "servidor_memoria_en_baja"
    assert "viene bajando" in h["alerts"][0]["detalle"]


def test_la_pantalla_dibuja_la_curva(app, fake_db, monkeypatch):
    from modules._lib import servidor

    monkeypatch.setattr(servidor, "estado", lambda: {
        "total_mb": 4036, "disponible_mb": 1900, "usado_pct": 50.0, "cpu_pct": 3.0,
        "nucleos": 2, "falta_memoria": False, "procesos": [], "navegadores": 8,
        "programa": {"pid": 1, "memoria_mb": 90}, "memoria_minima_mb": 400})
    monkeypatch.setattr(v, "historia", lambda: _filas([1900, 1850, 1700, 1650, 300, 1600]))
    rid = fake_db.add_role("Tester", ["admin_dbase.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    html = c.get("/admin/pantallas").get_data(as_text=True)
    assert "<polyline" in html and "6 lecturas" in html
    assert "01/09 04:00 300 MB" in html
