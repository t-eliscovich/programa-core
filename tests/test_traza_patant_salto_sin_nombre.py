"""Tamara 2026-09-02 — el salto de ±2,68 M en /informes/traza sin nada atribuido.

La traza guarda la `utilidad` y los ONCE componentes de `patr`. Pero

    utilidad = patr − PATANT

y PATANT (el patrimonio del último cierre) no estaba en la lista. Cuando cambia,
el Δ de la utilidad se mueve y las once celdas quedan VACÍAS — el síntoma exacto
de "no cierra", sin nada que mirar.

Pasó de verdad el 02/09/2026: entre las 08:02 y las 08:20 la utilidad saltó a
+1.971.282 y volvió a −709.403 (Δ +2.679.072 y −2.677.092) con la única celda
escrita en Stock (+1.450 y +531). No se movió nada del patrimonio: se movió el
cierre contra el que se compara. Una regeneración de snapshot borra la fila de
`scintela.historia` y la vuelve a insertar; en esa ventana `historia_ultimo_mes`
devuelve un cierre más viejo. Durante esos 18 minutos el balance mostró 1,97
MILLONES de utilidad a cualquiera que lo abriera.

El invariante de esta pantalla no se negocia: Δ utilidad = Σ aportes, y si no
cierra la diferencia se muestra CON NOMBRE. Nunca se esconde y nunca se reparte.
"""
from __future__ import annotations

from modules.informes import traza


def _foto(id_traza, utilidad, patr_neto, *, vsto=6_000_000.0, uret=0.0, **kw):
    base = {
        "id_traza": id_traza, "utilidad": utilidad, "patr_neto": patr_neto,
        "caja": 21_235.0, "bancos": 933_889.0, "cheques": 2_670_087.35,
        "facturas": 5_188_890.85, "antic": 2_300_000.0, "vsto": vsto,
        "vqx": 341_307.24, "umaq": 1_038_550.0, "uact": 2_364_564.0,
        "totp": 3_132_957.02, "uret": uret,
    }
    base.update(kw)
    return base


def _aportes(fila):
    return {m["col"]: m["aporte"] for m in fila["movio"]}


def test_un_cambio_de_patant_deja_de_ser_un_salto_anonimo():
    """El caso del 02/09: nada del patrimonio se movió, sólo el cierre."""
    # patant = patr_neto + uret - utilidad. Misma patr_neto en las dos fotos:
    #   antes:   21.024.369 - (-708.781) = 21.733.150
    #   después: 21.024.369 -  1.971.282 = 19.053.087   (cierre más viejo)
    antes = _foto(1, -708_781.0, 21_024_369.0)
    despues = _foto(2, 1_971_282.0, 21_024_369.0)

    fila = traza.con_deltas([despues, antes])[0]

    assert fila["d_utilidad"] == 2_680_063.0
    ap = _aportes(fila)
    # El patrimonio NO se movió: ningún componente de patr aparece.
    assert set(ap) == {"patant"}, ap
    # Y el aporte de PATANT explica el salto ENTERO, al centavo.
    assert ap["patant"] == 2_680_063.0
    assert round(sum(ap.values()), 2) == fila["d_utilidad"]


def test_el_movimiento_lleva_nombre_en_castellano():
    fila = traza.con_deltas([
        _foto(2, 1_971_282.0, 21_024_369.0),
        _foto(1, -708_781.0, 21_024_369.0),
    ])[0]
    assert fila["movio"][0]["label"] == "Cierre anterior (PATANT)"


def test_va_a_la_columna_otros_asi_la_fila_no_queda_vacia():
    """La dueña sacó dividendos/maquinaria/terrenos de la grilla; PATANT va con
    ellos en "Otros", que existe justamente para que ninguna fila muestre un Δ
    con todas las celdas en blanco."""
    fila = traza.con_deltas([
        _foto(2, 1_971_282.0, 21_024_369.0),
        _foto(1, -708_781.0, 21_024_369.0),
    ])[0]
    assert fila["delta"].get("otros") == 2_680_063.0


def test_un_dia_normal_no_le_inventa_movimiento_al_cierre():
    """Con el mismo PATANT, el Δ lo explican los componentes de siempre."""
    antes = _foto(1, 100_000.0, 21_832_772.07, vsto=6_000_000.0)
    # Vendió: sube utilidad y sube patr_neto lo mismo, patant queda igual.
    despues = _foto(2, 112_000.0, 21_844_772.07, vsto=6_012_000.0)
    fila = traza.con_deltas([despues, antes])[0]
    ap = _aportes(fila)
    assert "patant" not in ap
    assert ap["vsto"] == 12_000.0


def test_fotos_viejas_sin_patr_neto_no_inventan_un_salto():
    """Antes de que existiera `patr_neto` no hay con qué despejar PATANT.
    Saltear es correcto; tomar el None como 0 metería un movimiento del tamaño
    del patrimonio entero."""
    antes = _foto(1, 100_000.0, None)
    despues = _foto(2, 112_000.0, 21_844_772.07)
    fila = traza.con_deltas([despues, antes])[0]
    assert "patant" not in _aportes(fila)
    assert traza._patant_de(antes) is None


def test_patant_despejado_da_el_cierre_real_de_agosto():
    """Comprobación contra producción: la foto de las 21:20 del 02/09 tenía
    utilidad 112.570,69 y dividendos 10.000; el cierre del 31/08 guardado en
    scintela.historia es 21.732.772,07."""
    foto = _foto(9, 112_570.69, 21_835_342.76, uret=10_000.0)
    assert round(traza._patant_de(foto), 2) == 21_732_772.07
