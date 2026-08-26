"""El aviso que ya se solucionó dice que se solucionó.

TMT 2026-08-26 (dueña): *"si un aviso ya se solucionó habría que avisar que se
solucionó"*. El mecanismo existía desde el 11/08 (`avisos.resolver`: el MISMO
renglón pasa a ✅ y vuelve a no leído, en vez de apagarse en silencio); lo que
faltaba era engancharlo en los dos vigías que dejaban avisos colgados para
siempre — las facturas frenadas y las importaciones sin plata.

⭐ La regla que ordena todo el archivo: **un aviso se resuelve sólo cuando se
puede VERIFICAR que se arregló**, nunca porque el caso "ya no aparezca".
Desaparecer también puede ser que la ventana se corrió o que la lectura vino
corta, y ahí decir "listo" es mentir.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.avisos import queries as q  # noqa: E402
from modules.facturas import vigia_anuladas as va  # noqa: E402
from modules.importaciones import vigilancia as vig  # noqa: E402

NUM = "001-099-000182658"


# ── El helper: de qué avisé y todavía no resolví ───────────────────────────
def test_pide_los_que_siguen_siendo_un_problema():
    visto = {}

    def _fa(sql, params):
        visto["sql"] = " ".join(sql.split())
        visto["params"] = params
        return [{"id_aviso": 7, "clave": va.CLAVE_FRENADA + NUM}]

    with patch.object(q, "_tiene_archivado", return_value=True), \
         patch.object(q.db, "fetch_all", side_effect=_fa):
        out = q.abiertos_por_clave(va.CLAVE_FRENADA)
    assert out == [{"id_aviso": 7, "clave": va.CLAVE_FRENADA + NUM}]
    assert visto["params"] == (va.CLAVE_FRENADA + "%",)
    # El ya resuelto no se vuelve a tocar, y el que ella cerró se queda cerrado.
    assert "nivel <> 'ok'" in visto["sql"]
    assert "NOT archivado" in visto["sql"]


def test_sin_prefijo_no_pregunta_nada():
    assert q.abiertos_por_clave("") == []


def test_el_helper_es_fail_soft():
    with patch.object(q, "_tiene_archivado", return_value=False), \
         patch.object(q.db, "fetch_all", side_effect=RuntimeError("boom")):
        assert q.abiertos_por_clave("vigia-frenada-") == []


# ── Facturas frenadas ───────────────────────────────────────────────────────
def _res(*, frenadas=(), ok=True):
    return {"ok": ok, "desde": date(2026, 8, 20), "hasta": date(2026, 8, 26),
            "frenadas": [{"numf_completo": n} for n in frenadas]}


def _resolver_facturas(res, filas, abiertos=((NUM, 7),)):
    resueltos = []
    with patch.object(va.db, "fetch_all", return_value=list(filas)), \
         patch("modules.avisos.queries.abiertos_por_clave",
               return_value=[{"id_aviso": i, "clave": va.CLAVE_FRENADA + n}
                             for n, i in abiertos]), \
         patch("modules.avisos.queries.resolver",
               side_effect=lambda id_aviso, **kw:
                   resueltos.append((id_aviso, kw)) or True):
        va._resolver_los_que_ya_se_arreglaron(res)
    return resueltos


def test_la_factura_que_se_anulo_pasa_a_listo():
    filas = [{"numf_completo": NUM, "fecha": date(2026, 8, 25), "stat": "X"}]
    assert _resolver_facturas(_res(), filas) == [
        (7, {"titulo": f"{NUM} · listo, se anuló",
             "detalle": "Ya no suma en la venta ni en la cartera."}),
    ]


def test_si_asinfo_la_volvio_a_reportar_tambien_es_listo():
    """Estaba viva y la lista de Asinfo venía corta: no era una anulación."""
    filas = [{"numf_completo": NUM, "fecha": date(2026, 8, 25), "stat": "Z"}]
    out = _resolver_facturas(_res(), filas)
    assert out[0][1]["titulo"] == f"{NUM} · listo, Asinfo la volvió a reportar"


def test_la_que_quedo_fuera_de_la_ventana_no_se_da_por_resuelta():
    """Dejó de aparecer porque dejamos de mirarla, no porque se arreglara."""
    filas = [{"numf_completo": NUM, "fecha": date(2026, 7, 1), "stat": "Z"}]
    assert _resolver_facturas(_res(), filas) == []


def test_la_que_sigue_frenada_hoy_no_se_toca():
    assert _resolver_facturas(_res(frenadas=[NUM]), []) == []


def test_con_asinfo_mudo_no_se_resuelve_nada():
    assert _resolver_facturas(_res(ok=False), []) == []


# ── Importaciones ───────────────────────────────────────────────────────────
def _im(kg, importe, *, im="IM-A", dias_atras=35):
    from filters import today_ec
    rec = today_ec() - timedelta(days=dias_atras)
    return {"im_numero": im, "recibida": True, "fecha_recepcion": str(rec),
            "fecha": str(rec), "prov": "MH", "numero": 66,
            "numero_hasta": None, "grupo_id": im, "grupo_ims": [im],
            "grupo_kg": kg, "kg": kg,
            "compra": {"items": [{"id_compra": 1, "fecha": str(rec),
                                  "importe": importe}]},
            "anticipo": None}


def _resolver_importaciones(rows):
    resueltos = []
    with patch("modules.avisos.queries.abiertos_por_clave",
               side_effect=lambda p: (
                   [{"id_aviso": 9, "clave": "import-sin-plata:IM-A"}]
                   if "sin-plata" in p else [])), \
         patch("modules.avisos.queries.resolver",
               side_effect=lambda id_aviso, **kw:
                   resueltos.append((id_aviso, kw)) or True):
        vig._resolver_los_arreglados(rows)
    return resueltos


def test_la_importacion_que_ya_tiene_su_plata_pasa_a_listo():
    assert _resolver_importaciones([_im(47730.0, 47730.0 * 3.0)]) == [
        (9, {"titulo": "MH 66 · listo, ya tiene toda la plata",
             "detalle": "Quedó en 3,00 el kilo."}),
    ]


def test_la_que_sigue_sin_plata_no_se_resuelve():
    """2,30 el kilo sigue abajo de la banda: el problema es el mismo."""
    assert _resolver_importaciones([_im(47730.0, 47730.0 * 2.3)]) == []


def test_sin_lectura_no_se_resuelve_nada():
    """Sin datos no se afirma nada — la misma regla de toda la alarma."""
    assert _resolver_importaciones([]) == []
