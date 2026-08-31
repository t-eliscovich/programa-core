"""El código del programa se puede cargar A MANO desde /importaciones.

TMT 2026-08-31 (dueña, MTG3756): la Nota de Asinfo vino sin "( MD 1 )" y sin
eso nada cruza — ni el anticipo, ni los kg, ni la campanita se resuelve.
*"¿No podemos hacer que valga como está cargado?"* → mig 0237: el código se
guarda en scintela.importacion_codigo y el programa se lo APPENDEA a la nota
al leerla de Asinfo, así todos los parsers ven lo mismo que si el proveedor
lo hubiera escrito.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.asinfo import service as svc  # noqa: E402


def test_con_codigo_manual_parchea_la_nota_y_no_toca_la_cache():
    cacheada = [{"im_numero": "IM-0000654", "nota": "MTG3756 ---2"}]
    with patch.object(svc, "_codigos_manuales",
                      return_value={"IM-0000654": "MD 1"}):
        out = svc._con_codigo_manual(cacheada)
    assert out[0]["nota"] == "MTG3756 ---2 ( MD 1 )"
    # La lista cacheada quedó intacta: sin esto, cada llamada volvería a
    # appendear y la nota crecería sin fin.
    assert cacheada[0]["nota"] == "MTG3756 ---2"


def test_con_codigo_manual_no_duplica_si_ya_esta():
    filas = [{"im_numero": "IM-1", "nota": "MTG3756 ( MD 1 ) ---2"}]
    with patch.object(svc, "_codigos_manuales", return_value={"IM-1": "MD 1"}):
        out = svc._con_codigo_manual(filas)
    assert out[0]["nota"].count("( MD 1 )") == 1


def test_la_nota_parcheada_parsea_como_si_viniera_de_asinfo():
    from concepto_parser import parse_nota_importacion
    with patch.object(svc, "_codigos_manuales",
                      return_value={"IM-0000654": "MD 1"}):
        out = svc._con_codigo_manual(
            [{"im_numero": "IM-0000654", "nota": "MTG3756 ---2"}])
    code = parse_nota_importacion(out[0]["nota"])
    assert code.get("prov") == "MD" and code.get("numero") == 1
    # Y la base/partida no se rompen: el "( MD 1 )" va entre paréntesis, que
    # el parser de la base descarta igual que el código real.
    from modules.importaciones.service import _partida_de
    assert _partida_de(out[0]["nota"]) == ("MTG3756", 2)


def _login(app):
    from flask import g

    @app.before_request
    def _entra():
        g.user = {"id_usuario": 0, "username": "tamara", "id_rol": 0,
                  "nombre_rol": "Accionista", "activo": True, "vend": None}
        g.permisos = {"*"}


def test_post_codigo_invalido_no_escribe(app, client):
    import db
    _login(app)
    escritos = []
    with patch.object(db, "execute",
                      side_effect=lambda sql, params=None:
                      escritos.append(sql) if "importacion_codigo" in sql else None):
        r = client.post("/importaciones/codigo",
                        data={"im_numero": "IM-1", "codigo": "cualquier cosa"})
    assert r.status_code == 302
    assert escritos == []  # la bitácora escribe; el código NO


def test_post_codigo_guarda_para_las_partidas_hermanas(app, client):
    """La factura partida (---1/---2, misma nota base) recibe el código en
    TODAS sus partidas sin código de una — es la misma factura del proveedor."""
    import db
    _login(app)
    filas = [
        {"im_numero": "IM-0000654", "nota": "MTG3756 ---2"},
        {"im_numero": "IM-0000653", "nota": "MTG3756 ---1"},
        {"im_numero": "IM-0000650", "nota": "AYF02871 ( AI 48)"},
    ]
    escritos = []
    with patch.object(svc, "importaciones_asinfo", return_value=filas), \
         patch.object(db, "fetch_one", return_value={"ok": 1}), \
         patch.object(db, "execute",
                      side_effect=lambda sql, params=None:
                      escritos.append(params) if "importacion_codigo" in sql else None):
        r = client.post("/importaciones/codigo",
                        data={"im_numero": "IM-0000654", "codigo": "md 1"})
    assert r.status_code == 302
    assert sorted((p[0], p[1]) for p in escritos) == [
        ("IM-0000653", "MD 1"), ("IM-0000654", "MD 1")]


def test_post_codigo_proveedor_inexistente_avisa(app, client):
    import db
    _login(app)
    escritos = []
    with patch.object(db, "fetch_one", return_value=None), \
         patch.object(db, "execute",
                      side_effect=lambda sql, params=None:
                      escritos.append(sql) if "importacion_codigo" in sql else None):
        r = client.post("/importaciones/codigo",
                        data={"im_numero": "IM-1", "codigo": "ZZ 9"})
    assert r.status_code == 302
    assert escritos == []


def test_si_la_nota_ya_trae_codigo_manda_asinfo():
    """Dueña 31/08: "si está bien desde Asinfo, se matchea solo". Si después
    de cargar el código a mano alguien lo pone también en la Nota de Asinfo,
    manda el de Asinfo — el manual se ignora (dos códigos en una nota sería
    una lotería para el parser)."""
    filas = [{"im_numero": "IM-1", "nota": "MTG3756 ( MD 2 ) ---1"}]
    with patch.object(svc, "_codigos_manuales", return_value={"IM-1": "MD 1"}):
        out = svc._con_codigo_manual(filas)
    assert out[0]["nota"] == "MTG3756 ( MD 2 ) ---1"  # intacta: gana Asinfo
