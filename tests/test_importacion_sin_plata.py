"""Importaciones que llegaron y quedaron sin toda su plata cargada.

TMT 2026-07-31. La versión anterior de esta alarma salía en rojo arriba del
Informe y la dueña la sacó: *"me sacás todo esto en rojo de Resultados"*. El
motivo importa más que el cartel — *"pero creo que lo cargará Andrés cuando
llegue"*: los kilos entran el día que llega el contenedor y la plata después,
así que TODA importación pasa por una ventana con el US$/kg bajo. La alarma
encontraba el estado normal de una importación reciente, no un error.

Medido (35 grupos, 18 meses): mediana 10 días, 34 de 35 cierran en ≤ 21, el más
lento 54. Umbral elegido por la dueña: **30 días**, y **a la campanita, no al
balance**.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from modules.importaciones import vigilancia as vig


def _im(codigo_prov, numero, dias_atras, kg, importe, *, im="IM-1"):
    """Una importación recibida hace `dias_atras` días, con `importe` cargado."""
    from filters import today_ec
    rec = today_ec() - timedelta(days=dias_atras)
    return {
        "im_numero": im, "recibida": True, "fecha_recepcion": str(rec),
        "fecha": str(rec - timedelta(days=60)),
        "prov": codigo_prov, "numero": numero, "numero_hasta": None,
        "grupo_id": im, "grupo_ims": [im], "grupo_kg": kg, "kg": kg,
        "compra": {"items": [{"id_compra": 1, "fecha": str(rec),
                              "importe": importe}]} if importe else None,
        "anticipo": None,
    }


def _casos(rows, dias=30):
    with patch.object(vig, "_dias_umbral", return_value=dias), \
         patch("modules.importaciones.service.importaciones_con_cruce",
               return_value=rows):
        return vig.importaciones_fuera_de_banda()


# ── El umbral: 30 días ───────────────────────────────────────────────────────
def test_una_importacion_recien_llegada_no_avisa():
    """MH 66-67 llegó hace 4 días con 2,31 US$/kg. Eso es "se está cargando",
    y es exactamente lo que la dueña sacó de la pantalla."""
    assert _casos([_im("MH", 66, 4, 47730.0, 110439.62)]) == []


def test_a_los_20_dias_todavia_no_avisa():
    """9 de cada 10 importaciones tienen toda su plata a los 19 días."""
    assert _casos([_im("MH", 66, 20, 47730.0, 110439.62)]) == []


def test_a_los_30_dias_avisa():
    casos = _casos([_im("MH", 66, 31, 47730.0, 110439.62)])
    assert len(casos) == 1
    assert casos[0]["codigo"] == "MH 66"
    assert casos[0]["falta_plata"] is True
    assert casos[0]["usd_kg"] == 2.3138


def test_el_caso_ac_76_75():
    """Medido: recibida el 18/12/2025, 50.200 kg, un solo movimiento de 8.360.
    225 días sin que nadie lo viera."""
    casos = _casos([_im("AC", 76, 225, 50200.0, 8360.0)])
    assert len(casos) == 1
    assert casos[0]["dias"] == 225
    assert casos[0]["usd_kg"] < 0.2
    # Cuánto habría que cargar para entrar en banda por abajo.
    assert casos[0]["faltarian_us"] == round(50200.0 * 2.7 - 8360.0, 2)


# ── Dentro de banda no avisa, por vieja que sea ─────────────────────────────
def test_en_banda_no_avisa_aunque_sea_vieja():
    assert _casos([_im("AC", 30, 400, 24494.0, 74000.0)]) == []


# ── Por arriba de la banda también avisa, con otro texto ───────────────────
def test_usd_kg_alto_avisa_como_faltan_kilos():
    casos = _casos([_im("AC", 94, 60, 22992.64, 99519.84)])
    assert len(casos) == 1
    assert casos[0]["falta_plata"] is False
    assert casos[0]["usd_kg"] > 4.0
    assert casos[0]["faltarian_us"] == 0.0


# ── En tránsito no se mira ─────────────────────────────────────────────────
def test_no_recibida_no_se_mira():
    r = _im("AC", 40, 300, 24000.0, 0.0)
    r["recibida"] = False
    r["fecha_recepcion"] = None
    assert _casos([r]) == []


# ── El $ se cuenta UNA vez por compra, aunque la partida tenga dos mitades ──
def test_la_plata_no_se_cuenta_dos_veces_en_una_partida():
    """Las dos mitades de un grupo pueden tener colgada LA MISMA compra. Sin
    dedup por id_compra el importe se dobla y la alarma no salta nunca."""
    from filters import today_ec
    rec = today_ec() - timedelta(days=60)
    comp = {"items": [{"id_compra": 7, "fecha": str(rec), "importe": 60000.0}]}
    mitades = []
    for im in ("IM-A", "IM-B"):
        r = _im("AC", 25, 60, 24494.0, 0.0, im=im)
        r["grupo_id"] = "IM-A+IM-B"
        r["grupo_ims"] = ["IM-A", "IM-B"]
        r["grupo_kg"] = 48988.0
        r["compra"] = comp
        mitades.append(r)
    casos = _casos(mitades)
    assert len(casos) == 1
    assert casos[0]["importe"] == 60000.0        # una vez, no 120.000
    assert casos[0]["kg"] == 48988.0
    assert casos[0]["usd_kg"] == round(60000.0 / 48988.0, 4)


# ── Fail-soft ──────────────────────────────────────────────────────────────
def test_asinfo_caido_no_inventa():
    with patch("modules.importaciones.service.importaciones_con_cruce",
               side_effect=RuntimeError("down")):
        assert vig.importaciones_fuera_de_banda() == []


# ── El aviso va a la campanita, una sola vez por grupo ─────────────────────
def test_avisa_una_vez_por_grupo_y_a_la_campanita():
    vig._ultima_corrida = 0.0
    caso = {"grupo_id": "IM-A+IM-B", "codigo": "MH 66", "ims": ["IM-A", "IM-B"],
            "kg": 47730.0, "importe": 110439.62, "usd_kg": 2.3138,
            "recepcion": "2026-06-01", "dias": 60, "falta_plata": True,
            "faltarian_us": 18432.38}
    vistos = []
    with patch.object(vig, "importaciones_fuera_de_banda", return_value=[caso]), \
         patch("modules.avisos.queries.avisar",
               side_effect=lambda **kw: vistos.append(kw) or True):
        out = vig.revisar_si_toca()
    assert out["avisados"] == 1
    assert vistos[0]["fuente"] == "importaciones"
    assert vistos[0]["nivel"] == "alerta"
    # `clave` es lo que hace que no se repita en cada corrida de 6 h.
    assert vistos[0]["clave"] == "import-sin-plata:IM-A+IM-B"
    assert "MH 66" in vistos[0]["titulo"] and "30" not in vistos[0]["titulo"]
    assert "60 días" in vistos[0]["titulo"]


def test_el_freno_evita_repetir_en_cada_ciclo():
    vig._ultima_corrida = 0.0
    with patch.object(vig, "importaciones_fuera_de_banda", return_value=[]), \
         patch("modules.avisos.queries.avisar", return_value=True):
        assert vig.revisar_si_toca()["corrio"] is True
        assert vig.revisar_si_toca()["corrio"] is False


def test_se_puede_apagar(monkeypatch):
    monkeypatch.setenv("IMPORT_SIN_PLATA", "0")
    vig._ultima_corrida = 0.0
    assert vig.revisar_si_toca() == {"corrio": False, "motivo": "apagado"}


def test_el_umbral_por_defecto_es_30(monkeypatch):
    monkeypatch.delenv("IMPORT_SIN_PLATA_DIAS", raising=False)
    assert vig._dias_umbral() == 30
    monkeypatch.setenv("IMPORT_SIN_PLATA_DIAS", "45")
    assert vig._dias_umbral() == 45


# ── Y NO vuelve al balance ─────────────────────────────────────────────────
def test_el_ciclo_de_fondo_la_corre():
    import inspect

    from modules._lib import autocarga_facturas as ac
    src = inspect.getsource(ac._loop)
    assert "vigilancia" in src and "revisar_si_toca" in src


def _dummy():
    """El date/timedelta importado se usa arriba; acá sólo para el linter."""
    return date, timedelta
