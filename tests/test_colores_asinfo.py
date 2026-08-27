"""El catálogo de COLORES y su clase de precio, traídos del atributo de Asinfo.

CONTEXTO (2026-08-17). Tamara: *"hay algunos colores que siguen sin mostrarse
en facturas proforma… ejemplo de color que no esta es cocoa, asinfo tambien
tiene la categoria. medio bajo etc."*.

Sin clase (1..5) un color no se puede cotizar: no hay fila de la matriz de
precios de donde sacar la cifra, así que la Factura Proforma no lo lista.
Ese día quedaban 56 colores activos de Asinfo en esa situación — 35 que ni
estaban en el catálogo y 21 cargados sin clase, COCOA entre ellos.

Lo que se prueba es lo que puede COBRAR MAL o romper el catálogo:

1. El padre del atributo Color ES la clase (BLANCOS/BAJOS/MEDIOS/JASPEADOS/
   FUERTES → 1..5) y completa a los que no la tienen.
2. ESPECIALES queda AFUERA: esa clase no existe en la matriz (cinco filas, no
   seis) y cotizarla con la fila de otra sería inventarle un precio.
3. Manda Asinfo también sobre lo YA cargado (decisión de Tamara): si difiere,
   se corrige y se REPORTA — es un cambio de precio, no un detalle.
4. El $/kg de tintura (`costo`) y el nombre ya cargado NUNCA se pisan.
5. Si Metabase no contesta no se toca NADA: un puente caído no es un catálogo
   vacío.
"""
from __future__ import annotations

import db
from modules.precios import colores_asinfo as ca


def _armar(monkeypatch, asinfo_rows, pc_rows, contesto=True):
    """Mockea Asinfo y la base, y captura todo lo que el sync escribe."""
    cap = {"sql": [], "logs": []}

    monkeypatch.setattr(ca, "traer_asinfo", lambda: (asinfo_rows, contesto))
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: pc_rows)

    def fake_execute(sql, params=None, conn=None):
        cap["sql"].append((" ".join(sql.split()), params))
        return 1

    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(ca, "_guardar_log",
                        lambda usuario, rep: cap["logs"].append(rep))
    monkeypatch.setattr(ca, "_limpiar_cache", lambda: None)
    return cap


def _asinfo(cod, nombre, padre):
    return {"cod": cod, "nombre": nombre, "padre": padre}


def _pc(cod, color="", clase=None):
    return {"cod": cod, "color": color, "clase": clase}


def test_cocoa_estaba_sin_clase_y_asinfo_se_la_pone(monkeypatch):
    """El caso que disparó todo: COA existía, sin clase, y no se podía cotizar."""
    cap = _armar(monkeypatch, [_asinfo("COA", "COCOA", "MEDIOS")],
                 [_pc("COA", "COCOA", None)])
    r = ca.sincronizar()
    assert r["ok"] and r["con_clase"] == ["COA"]
    assert r["altas"] == [] and r["reclasificados"] == []
    sql, params = cap["sql"][0]
    assert sql.startswith("UPDATE scintela.tinto_costos SET clase=%s")
    assert params[0] == 3 and params[-1] == "COA"


def test_color_que_no_esta_se_da_de_alta_con_su_clase(monkeypatch):
    cap = _armar(monkeypatch, [_asinfo("MNG", "MANGO", "BAJOS")], [])
    r = ca.sincronizar()
    assert r["altas"] == ["MNG"]
    sql, params = cap["sql"][0]
    assert sql.startswith("INSERT INTO scintela.tinto_costos")
    # El costo de tintura nace en 0: no es un dato de Asinfo.
    assert params[0] == "MNG" and params[1] == "MANGO" and params[2] == 2


def test_especiales_queda_afuera(monkeypatch):
    """Tie dye, jean, drill, rayón: su clase no está en la matriz de precios."""
    cap = _armar(monkeypatch, [
        _asinfo("TDL", "TIE DYE LILA", "ESPECIALES"),
        _asinfo("JNY", "JEAN NAVY", "ESPECIALES"),
    ], [])
    r = ca.sincronizar()
    assert r["especiales"] == 2
    assert r["altas"] == [] and r["con_clase"] == []
    assert cap["sql"] == []          # no escribió NADA
    assert "ESPECIALES" in ca.frase(r)


def test_padre_desconocido_no_inventa_clase(monkeypatch):
    """Si mañana Asinfo agrega un padre nuevo, se avisa; no se adivina."""
    cap = _armar(monkeypatch, [_asinfo("XXX", "COLOR RARO", "PASTELES")], [])
    r = ca.sincronizar()
    assert r["sin_padre"] == ["XXX"] and r["altas"] == []
    assert cap["sql"] == []


def test_clase_distinta_se_corrige_y_se_reporta(monkeypatch):
    """Manda Asinfo (Tamara 17/08): A.ORO estaba FUERTES y se factura MEDIOS."""
    cap = _armar(monkeypatch, [_asinfo("AOR", "AMARILLO ORO", "MEDIOS")],
                 [_pc("AOR", "A.ORO", 5)])
    r = ca.sincronizar()
    assert r["reclasificados"] == [
        {"cod": "AOR", "color": "A.ORO", "de": 5, "a": 3}]
    sql, params = cap["sql"][0]
    assert "clase=%s" in sql and params[0] == 3
    # El nombre corto del catálogo NO se pisa con el largo de Asinfo.
    assert "color=%s" not in sql
    assert "cambiaron de clase" in ca.frase(r)


def test_el_que_ya_coincide_no_se_toca(monkeypatch):
    cap = _armar(monkeypatch, [_asinfo("NEG", "NEGRO", "FUERTES")],
                 [_pc("NEG", "NEGRO", 5)])
    r = ca.sincronizar()
    assert cap["sql"] == []
    assert r["altas"] == [] and r["con_clase"] == [] and r["reclasificados"] == []
    assert "ya estaba al día" in ca.frase(r)


def test_nombre_vacio_se_rellena(monkeypatch):
    cap = _armar(monkeypatch, [_asinfo("CHO", "CHOCOLATE", "FUERTES")],
                 [_pc("CHO", "", None)])
    ca.sincronizar()
    sql, params = cap["sql"][0]
    assert "clase=%s" in sql and "color=%s" in sql
    assert params[0] == 5 and params[1] == "CHOCOLATE"


def test_metabase_caido_no_toca_nada(monkeypatch):
    """Un puente caído NO es un catálogo vacío (la lección del 29/07)."""
    cap = _armar(monkeypatch, [], [_pc("COA", "COCOA", None)], contesto=False)
    r = ca.sincronizar()
    assert r["ok"] is False
    assert cap["sql"] == []
    assert cap["logs"] and cap["logs"][0]["ok"] is False


def test_es_idempotente(monkeypatch):
    """Correrlo dos veces no escribe dos veces: la segunda ya coincide."""
    filas = [_asinfo("COA", "COCOA", "MEDIOS")]
    cap = _armar(monkeypatch, filas, [_pc("COA", "COCOA", None)])
    ca.sincronizar()
    assert len(cap["sql"]) == 1
    cap2 = _armar(monkeypatch, filas, [_pc("COA", "COCOA", 3)])
    ca.sincronizar()
    assert cap2["sql"] == []


def test_nombre_largo_no_rompe_el_insert():
    """`tinto_costos.color` es varchar(30): se recorta acá, no en Postgres."""
    assert ca._MAX_COLOR == 30 and ca._MAX_COD == 5


def test_la_ventana_de_colores_quedo_en_11_y_16():
    """Hasta el 27/08/2026 este test afirmaba que colores y clientes
    compartían ventanas (11:00 y 16:00 EC). Ese día el sync de CLIENTES
    pasó a correr cada hora de 07 a 19 (Tamara: en Asinfo cambian dirección
    y vendedor, y tiene que llegar más seguido); los colores no necesitan
    esa frecuencia y se quedan en 11 y 16. Si alguien mueve la de colores,
    que se entere acá."""
    from modules.clientes import sync_asinfo as cli

    assert ca._VENTANAS_UTC == (16, 21)
    assert cli._VENTANA_EC_DESDE == 7 and cli._VENTANA_EC_HASTA == 19


def test_apagable_por_env(monkeypatch):
    monkeypatch.setenv("SYNC_COLORES_AUTO", "0")
    assert ca.correr_si_toca() == {"corrio": False}
