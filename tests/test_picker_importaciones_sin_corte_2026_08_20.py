"""El picker de importaciones lista TODAS las del proveedor (Tamara 2026-08-20).

*"cuando quiero cargar un nuevo anticipo y busco 39 no me encuentra ese
anticipo"*.

`/importaciones/_api/abiertas/<prov>` —el que alimenta el cuadro celeste
"Importaciones de este proveedor" de /dolares y /compras/nueva— cortaba en las
30 más recientes. AC ya tiene 31 abiertas, así que la más VIEJA (AC 39,
IM-0000584, con 3 anticipos encima) se caía de la lista justo por ser la que
lleva más tiempo abierta, que es exactamente la que se sigue pagando.

Lo que estos tests fijan:
  1. con 31 importaciones del proveedor vuelven las 31 (nada de corte);
  2. la más vieja —la que el corte se comía— está entre ellas;
  3. sigue saliendo ordenada por número de AC ascendente (TMT 2026-07-23);
  4. las campañas ANTERIORES no se listan — *"nadie va a cargar un anticipo
     para algo que ya llegó"* (Tamara, el mismo día, cuando al sacar el corte
     aparecieron ~120 con 2024 y 2025 mezcladas).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.importaciones import service


@pytest.fixture
def cliente(app, fake_db):
    rid = fake_db.add_role("Tester", ["compras.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _filas():
    """31 importaciones de AC. La última —AC 39, la de fecha más antigua— es
    la que el corte de 30 dejaba afuera."""
    numeros = [
        29, 36, 37, 40, 41, 42, 43, 44, 46, 47, 48, 49, 50, 51, 52, 54, 55,
        57, 58, 59, 60, 61, 62, 63, 64, 66, 67, 72, 73, 77,
    ]
    filas = [
        {
            "im_numero": "IM-%07d" % (600 + i),
            "codigo": "AC %d" % n,
            "prov": "AC",
            "numero": n,
            "nota": "ACMT/EXP/2026-27/8%d ( AC %d)" % (100 + i, n),
            "fecha": "2026-08-01",
            "kg": 24497.64,
            "anticipo_aplicado": 0.0,
            "compra": {"n": 0, "importe_total": 0.0},
        }
        for i, n in enumerate(numeros)
    ]
    filas.append({
        "im_numero": "IM-0000584",
        "codigo": "AC 39",
        "prov": "AC",
        "numero": 39,
        "nota": "ACMT/EXP/2026-27/8196 (AC 39)",
        "fecha": "2026-05-27",
        "kg": 24358.0,
        "anticipo_aplicado": 64209.06,
        "compra": {"n": 0, "importe_total": 0.0},
    })
    return filas


def _pedir(cliente, filas=None, anios=None):
    """`anios` = {im_numero: año}; por defecto todas de la campaña 2026."""
    filas = _filas() if filas is None else filas

    def _anio(row):
        return {"anio": (anios or {}).get(row.get("im_numero"), 2026)}

    with patch.object(service, "importaciones_con_cruce", return_value=filas), \
            patch.object(service, "_anio_de", _anio):
        r = cliente.get("/importaciones/_api/abiertas/AC")
    assert r.status_code == 200
    return r.get_json()["importaciones"]


def test_vuelven_todas_no_solo_treinta(cliente):
    assert len(_pedir(cliente)) == 31


def test_la_mas_vieja_no_se_cae_de_la_lista(cliente):
    ims = _pedir(cliente)
    ac39 = [i for i in ims if i["numero"] == 39]
    assert ac39, "AC 39 —la importación abierta más vieja— no volvió en el picker"
    assert ac39[0]["im_numero"] == "IM-0000584"
    assert ac39[0]["anticipos"] == pytest.approx(64209.06)


def test_sigue_en_orden_numerico(cliente):
    nums = [i["numero"] for i in _pedir(cliente)]
    assert nums == sorted(nums)
    assert nums[1] == 36 and nums[2] == 37 and nums[3] == 39


def test_las_campanas_viejas_no_se_listan(cliente):
    """El nº de AC se reusa cada año: la AC 43 de 2024 no tiene por qué estar."""
    filas = _filas() + [{
        "im_numero": "IM-0000248",
        "codigo": "AC 43",
        "prov": "AC",
        "numero": 43,
        "nota": "ACMT/EXP/2024-25/5150 AC 43",
        "fecha": "2024-03-21",
        "kg": 22992.64,
        "anticipo_aplicado": 0.0,
        "compra": {"n": 0, "importe_total": 0.0},
    }]
    ims = _pedir(cliente, filas, {"IM-0000248": 2024})
    assert len(ims) == 31
    assert all(i["anio"] == 2026 for i in ims)
    assert [i["im_numero"] for i in ims if i["numero"] == 43] == ["IM-0000606"]


def test_la_campana_sale_de_los_datos_no_del_reloj(cliente):
    """Un proveedor cuya última importación es de 2024 sigue listando 2024 —
    si fuera por el año del calendario quedaría en cero."""
    filas = _filas()[:3]
    ims = _pedir(cliente, filas, {f["im_numero"]: 2024 for f in filas})
    assert len(ims) == 3
    assert {i["anio"] for i in ims} == {2024}
