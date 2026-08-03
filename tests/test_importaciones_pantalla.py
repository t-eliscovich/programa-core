"""/importaciones: colapsar partidas, filtro de año, y la fecha en su columna.

TMT 2026-08-03 (dueña), tres pedidos sobre la misma pantalla:

  1. *"andres cargó algo hoy — eso no debería poner fecha recibida, la fecha
     recibida es una sola; quizás debajo de Fecha cuando se cargó la compra"*;
  2. *"necesitamos hide las recibidas en 2024 y 2025 salvo explícitamente
     filtrada"*;
  3. *"hay dos AI 15 que partimos los kg en dos, 11k y 11 — deberían ir todas en
     una, ¿cuál sería una manera elegante de agrupar y que ambas queden
     pagadas?"*.

La respuesta al 3 es que **no son dos**: IM-0000571 e IM-0000572 son la misma
factura del proveedor (AYF02653), 11.289,39 kg cada una, recibidas el mismo día.
Colapsadas, el grupo tiene sus 22.578,78 kg, sus compras una sola vez y UNA sola
marca de pagada.

**El test que más importa de este archivo es el último**: que el colapso no toque
el dato que alimenta la tarifa del hilado. Si se colara ahí, cada mitad llevaría
el kg del grupo entero y el costo del hilo se iría al doble.
"""
from __future__ import annotations

import inspect

from modules.importaciones import presentacion as pres


def _mitad(im, *, kg=11289.39, rec="2026-08-01", compras=None, anticipos=None):
    return {
        "origen": "importacion", "im_numero": im, "prov": "AI", "numero": 15,
        "numero_hasta": None, "codigo": "AI 15", "nota": f"AYF02653 ( AI 15 ) ----{im[-1]}",
        "fecha": "2026-05-05", "fecha_recepcion": rec, "recibida": bool(rec),
        "kg": kg, "kg_recibidos": None, "fecha_recepcion_pc": None,
        "recibido_pc": False, "anticipo_aplicado": 0.0,
        "grupo_id": "IM-0000571+IM-0000572",
        "grupo_ims": ["IM-0000571", "IM-0000572"],
        "grupo_kg": 22578.78, "grupo_completo": True,
        "compra": ({"items": compras, "n": len(compras), "tipo": "H",
                    "ids": [c["id_compra"] for c in compras],
                    "first_id": compras[0]["id_compra"],
                    "importe_total": sum(c["importe"] for c in compras)}
                   if compras else None),
        "anticipo": ({"items": anticipos, "n": len(anticipos),
                      "importe_total": sum(a["importe"] for a in anticipos)}
                     if anticipos else None),
        "movimientos": [],
        "pagada": False, "saldo": 0.0, "parcial": False,
    }


_COMPRAS = [
    {"id_compra": 901, "fecha": "2026-08-03", "importe": 15527.85},
    {"id_compra": 902, "fecha": "2026-08-01", "importe": 58367.20},
]


# ── AI 15: el caso real ─────────────────────────────────────────────────────
def test_ai_15_queda_en_una_sola_fila_con_los_kg_del_grupo():
    rows = [_mitad("IM-0000572", compras=_COMPRAS), _mitad("IM-0000571")]
    out = pres.colapsar_partidas(rows, lambda items: {
        "pagada": True, "saldo": 0.0, "parcial": False})
    assert len(out) == 1
    f = out[0]
    assert f["es_grupo"] is True and f["n_partidas"] == 2
    assert f["im_numero"] == "IM-0000571 + IM-0000572"
    assert f["kg"] == 22578.78                 # 11.289,39 × 2, del grupo
    assert f["fecha_recepcion"] == "2026-08-01"


def test_las_compras_se_muestran_una_sola_vez_y_el_grupo_queda_pagado():
    """El "sin cargar" de IM-0000571 era la mitad que no se quedó con la plata.
    Unidas, el grupo tiene sus 73.895,05 y UNA marca de pagada."""
    rows = [_mitad("IM-0000572", compras=_COMPRAS), _mitad("IM-0000571")]
    vistos = {}
    def _estado(items):
        vistos["n"] = len(items)
        return {"pagada": True, "saldo": 0.0, "parcial": False}
    out = pres.colapsar_partidas(rows, _estado)
    c = out[0]["compra"]
    assert c["n"] == 2
    assert c["importe_total"] == 73895.05
    assert [i["id_compra"] for i in c["items"]] == [901, 902]   # más nueva arriba
    assert out[0]["pagada"] is True
    # El estado se RECALCULA sobre las compras unidas, no se copia de una mitad.
    assert vistos["n"] == 2


def test_una_compra_colgada_de_las_dos_mitades_no_se_cuenta_doble():
    """Hoy el cruce le da cada compra a UNA sola mitad, así que no hay
    repetidos. El dedup va igual: el día que la atribución cambie, sin él la
    pantalla mostraría el doble de plata y nadie lo notaría."""
    rows = [_mitad("IM-0000572", compras=_COMPRAS),
            _mitad("IM-0000571", compras=_COMPRAS)]
    out = pres.colapsar_partidas(rows, None)
    assert out[0]["compra"]["n"] == 2
    assert out[0]["compra"]["importe_total"] == 73895.05      # no 147.790,10


def test_los_anticipos_de_las_dos_mitades_se_suman():
    a1 = [{"fecha": "2026-06-01", "importe": 30000.0}]
    a2 = [{"fecha": "2026-06-15", "importe": 20000.0}]
    rows = [_mitad("IM-0000572", anticipos=a1), _mitad("IM-0000571", anticipos=a2)]
    out = pres.colapsar_partidas(rows, None)
    assert out[0]["anticipo"]["n"] == 2
    assert out[0]["anticipo"]["importe_total"] == 50000.0
    assert out[0]["anticipo"]["items"][0]["fecha"] == "2026-06-15"  # más nuevo arriba
    assert out[0]["fuente"] == "anticipo"


def test_los_pagos_se_deduplican_por_transaccion():
    mv = {"id_transaccion": 55, "fecha": "2026-08-01", "monto": 100.0, "tipo": "nd"}
    rows = [_mitad("IM-0000572"), _mitad("IM-0000571")]
    rows[0]["movimientos"] = [mv]
    rows[1]["movimientos"] = [dict(mv)]
    out = pres.colapsar_partidas(rows, None)
    assert len(out[0]["movimientos"]) == 1


# ── Cuándo NO se colapsa ───────────────────────────────────────────────────
def test_no_se_colapsa_si_una_mitad_todavia_no_llego():
    """Un grupo a medio llegar se deja abierto: colapsarlo obligaría a inventar
    una respuesta para "¿está recibida?" y ninguna sería cierta."""
    rows = [_mitad("IM-0000572"), _mitad("IM-0000571", rec=None)]
    out = pres.colapsar_partidas(rows, None)
    assert len(out) == 2


def test_no_se_colapsa_un_grupo_descartado():
    rows = [_mitad("IM-0000572"), _mitad("IM-0000571")]
    for r in rows:
        r["grupo_completo"] = False
    assert len(pres.colapsar_partidas(rows, None)) == 2


def test_una_importacion_sola_no_se_toca():
    r = _mitad("IM-0000600")
    r["grupo_id"] = "IM-0000600"
    r["grupo_ims"] = ["IM-0000600"]
    out = pres.colapsar_partidas([r], None)
    assert len(out) == 1 and not out[0].get("es_grupo")


def test_las_compras_locales_no_se_colapsan():
    rows = [_mitad("CMTR-1"), _mitad("CMTR-2")]
    for r in rows:
        r["origen"] = "compra"
    assert len(pres.colapsar_partidas(rows, None)) == 2


# ── No muta las filas de entrada ───────────────────────────────────────────
def test_no_muta_las_filas_originales():
    """Las filas vienen del cache de Asinfo y las comparte el resto del
    programa. Mutarlas acá se filtraría al balance en la siguiente lectura."""
    rows = [_mitad("IM-0000572", compras=_COMPRAS), _mitad("IM-0000571")]
    antes = [dict(r) for r in rows]
    pres.colapsar_partidas(rows, None)
    for a, d in zip(antes, rows):
        assert a["kg"] == d["kg"] == 11289.39
        assert a["im_numero"] == d["im_numero"]
        assert "es_grupo" not in d


def test_el_estado_de_pago_que_falla_no_rompe_la_pantalla():
    rows = [_mitad("IM-0000572", compras=_COMPRAS), _mitad("IM-0000571")]
    def _revienta(_items):
        raise RuntimeError("db caída")
    out = pres.colapsar_partidas(rows, _revienta)
    assert len(out) == 1 and out[0]["pagada"] is False


# ── El filtro de año ───────────────────────────────────────────────────────
def test_el_filtro_de_anio_default_es_el_anio_en_curso():
    src = inspect.getsource(__import__(
        "modules.importaciones.views", fromlist=["lista"]).lista)
    assert 'request.args.get("anio")' in src
    assert "str(today_ec().year)" in src
    # Un filtro de MES ya dice el año: no se pisan.
    assert 'anio = "todos"' in src


def test_lo_que_no_llego_se_muestra_siempre():
    """Si el filtro de año escondiera las en tránsito, desaparecería de la
    pantalla justo lo que está por entrar — que es lo que más se mira."""
    src = inspect.getsource(__import__(
        "modules.importaciones.views", fromlist=["lista"]).lista)
    i = src.index('if anio != "todos"')
    bloque = src[i:i + 500]
    assert 'not r.get("fecha_recepcion")' in bloque


# ── La fecha del detalle va bajo FECHA, no bajo RECIBIDA ───────────────────
def test_el_detalle_no_escribe_en_la_columna_recibida():
    """Dueña: "eso no debería poner fecha recibida, la fecha recibida es una
    sola". Las columnas son 1 Importación · 2 Fecha · 3 Proveedor · 4 Código ·
    5 Nota · 6 Kg · 7 Recibida · 8 Compras · 9 Anticipos. La fecha de carga
    tiene que caer en la 2, no en la 7."""
    from pathlib import Path
    html = Path("modules/importaciones/templates/importaciones/lista.html").read_text()
    for tag in ("COMPRA", "ANT", "PAGO"):
        i = html.index(f'class="dtl-tag" style="color:#'.join([""]) + f'>{tag}<')
        fila_ini = html.rindex("<tr class=\"acc-detail-row\">", 0, i)
        fila_fin = html.index("</tr>", i)
        fila = html[fila_ini:fila_fin]
        tds = fila.split("<td")
        # td[1] = columna 1 (vacía), td[2] = columna 2 (Fecha) → ahí va la fecha
        assert "dtl-fecha" in tds[2], f"{tag}: la fecha no está en la columna Fecha"


# ── EL TEST QUE MÁS IMPORTA ────────────────────────────────────────────────
def test_el_colapso_no_entra_al_camino_del_balance():
    """`importaciones_con_cruce` alimenta la tarifa del hilado, el balance,
    autobap y la alarma, y los cuatro leen FILA POR FILA. Si el colapso se
    colara ahí, cada mitad llevaría el kg del grupo entero y el costo del hilo
    se iría al doble. Sólo la vista puede llamarlo."""
    import subprocess
    r = subprocess.run(
        ["grep", "-rln", "colapsar_partidas", "--include=*.py", "."],
        capture_output=True, text=True)
    usuarios = {
        f for f in r.stdout.split()
        if f and "test_" not in f and "presentacion.py" not in f
    }
    assert usuarios == {"./modules/importaciones/views.py"}, usuarios


def test_la_nota_del_grupo_no_muestra_el_ordinal_de_una_mitad():
    """En una fila que ya dice "2 partidas", un "----2" suelto parece que se
    está mostrando sólo la mitad 2."""
    rows = [_mitad("IM-0000572", compras=_COMPRAS), _mitad("IM-0000571")]
    out = pres.colapsar_partidas(rows, None)
    assert out[0]["nota"] == "AYF02653 ( AI 15 )"
    assert "---" not in out[0]["nota"]
