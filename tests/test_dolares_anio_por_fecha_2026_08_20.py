"""El año que falta se deduce de la FECHA del anticipo (Tamara 2026-08-20).

*"hay algunos que nos falta poner de qué año son. Fijate si podés ir
identificando los de 2026, ejemplo si fue creado en 2026 entonces es de 2026"*
— eran 82 de 117 anticipos vivos con la celda Año vacía.

El 29/07 la dueña había bajado el año SUGERIDO porque para el AC 77 proponía
2025 (Asinfo sólo tenía la importación de la campaña 2025-26) cuando el
correcto era 2026. Lo que cambia ahora es de dónde sale el número: la fecha del
propio anticipo, que en ese mismo caso daba el año bueno. Y se deduce SÓLO
cuando no hay ninguna duda posible — ella eligió esa variante sobre "todos".

Lo que estos tests fijan:
  1. sin año y con el número usado por una sola campaña → el año de la fecha;
  2. el caso AC 77: la única importación es de 2025 y el anticipo de 2026 →
     manda la FECHA (2026), que es lo que rompía la sugerencia vieja;
  3. si hay DOS campañas a tiro del anticipo, no se adivina;
  4. pero una campaña vieja que la fecha ya descartó NO cuenta como duda —
     *"nadie va a cargar un anticipo para algo que ya llegó"*. Eran 55
     anticipos frenados por una AC/AI de 2024 recibida hace dos años;
  5. dos grupos plausibles tampoco se adivinan — mismo freno que la conversión
     automática, que no se afloja;
  6. no se pisa el año ya puesto (columna o `58/26` del concepto);
  7. los anticipos ya APLICADOS no se tocan;
  8. con Asinfo caído (índice vacío) no se deduce nada.
"""

from __future__ import annotations

from unittest.mock import patch

from modules.dolares import anio as anio_mod
from modules.importaciones import service as imp_service


def _im(im_numero, anio, fecha):
    return {"im_numero": im_numero, "anio_im": anio, "anio_im_campana": True,
            "fecha": fecha, "grupo_id": im_numero}


def _anticipo(**kw):
    f = {"fecha": "2026-04-06", "cta": "AC", "concepto": "39 SALDO",
         "importe": 8051.90, "st": " ", "anio_ref": None, "anio_col": None,
         "anio_origen": None}
    f.update(kw)
    return f


def _correr(filas, index, plausibles=None):
    """`plausibles=None` deja correr la ventana de fechas de verdad."""
    if plausibles is None:
        anio_mod.adjuntar_anio_por_fecha(filas, index_importaciones=index)
        return filas
    with patch.object(imp_service, "candidatas_plausibles",
                      lambda cands, fecha: plausibles):
        anio_mod.adjuntar_anio_por_fecha(filas, index_importaciones=index)
    return filas


def test_una_sola_campana_toma_el_anio_de_la_fecha():
    f = _anticipo()
    _correr([f], {("AC", 39): [_im("IM-0000584", 2026, "2026-05-27")]})
    assert f["anio_ref"] == 2026
    assert f["anio_origen"] == "fecha"


def test_el_caso_ac_77_manda_la_fecha_no_la_importacion():
    """La única importación es de 2025 y el anticipo de 2026: vale 2026."""
    f = _anticipo(fecha="2026-07-29", concepto="77 MAPFRE")
    _correr([f], {("AC", 77): [_im("IM-0000495", 2025, "2025-10-13")]})
    assert f["anio_ref"] == 2026


def test_dos_campanas_a_tiro_del_anticipo_no_se_adivinan():
    """Un anticipo de enero con una importación de octubre-2025 y otra de
    febrero-2026: las dos son posibles, la decide ella."""
    f = _anticipo(fecha="2026-01-15", concepto="58 SALDO")
    _correr([f], {("AC", 58): [_im("IM-0000495", 2025, "2025-10-13"),
                               _im("IM-0000527", 2026, "2026-02-11")]})
    assert f["anio_ref"] is None
    assert f["anio_origen"] is None


def test_la_campana_vieja_que_ya_llego_no_es_una_duda():
    """El caso de los 55: la AC 43 existe en 2024 y en 2026, pero la de 2024 se
    recibió dos años antes del anticipo — la ventana de fechas ya la descartó y
    contarla dejaba la celda vacía de gusto."""
    f = _anticipo(fecha="2026-04-06", concepto="43 INI")
    _correr([f], {("AC", 43): [_im("IM-0000641", 2026, "2026-08-19"),
                               _im("IM-0000248", 2024, "2024-03-21")]})
    assert f["anio_ref"] == 2026
    assert f["anio_origen"] == "fecha"


def test_dos_grupos_plausibles_tampoco():
    """Mismo freno que la conversión automática (guard 9): si el automático
    pedía el año, sigue pidiéndolo. Las dos son de 2026 — lo que no se sabe no
    es el año sino CUÁL, y poner el año no lo resuelve."""
    dos = [_im("IM-0000622", 2026, "2026-07-20"), _im("IM-0000527", 2026, "2026-02-11")]
    f = _anticipo(concepto="58 SALDO")
    _correr([f], {("AC", 58): dos}, plausibles=dos)
    assert f["anio_ref"] is None


def test_las_dos_mitades_de_una_partida_no_son_dos_grupos():
    """Una importación partida en ---1/---2 es la misma mercadería: comparten
    `grupo_id` y no tienen por qué frenar la deducción."""
    a = _im("IM-0000550", 2026, "2026-04-16")
    b = _im("IM-0000551", 2026, "2026-04-16")
    a["grupo_id"] = b["grupo_id"] = "IM-0000550"
    f = _anticipo(concepto="19 SALDO")
    _correr([f], {("AC", 19): [a, b]}, plausibles=[a, b])
    assert f["anio_ref"] == 2026


def test_no_pisa_el_anio_ya_puesto():
    f = _anticipo(fecha="2026-01-15", anio_ref=2025, anio_origen="columna")
    _correr([f], {("AC", 39): [_im("IM-0000584", 2026, "2026-05-27")]})
    assert f["anio_ref"] == 2025
    assert f["anio_origen"] == "columna"


def test_los_aplicados_no_se_tocan():
    f = _anticipo(st="A")
    _correr([f], {("AC", 39): [_im("IM-0000584", 2026, "2026-05-27")]})
    assert f["anio_ref"] is None


def test_sin_asinfo_no_se_deduce_nada():
    """El índice vacío es Asinfo caído, no «ese número no existe»."""
    f = _anticipo()
    _correr([f], {})
    assert f["anio_ref"] is None


def test_numero_sin_importacion_en_asinfo_igual_toma_el_anio():
    """Hay índice (Asinfo responde) pero ese número no está: nada con qué
    confundirse."""
    f = _anticipo(concepto="ANTICIPO")
    _correr([f], {("MH", 3): [_im("IM-0000100", 2026, "2026-01-01")]})
    assert f["anio_ref"] == 2026


def test_fecha_como_date_tambien():
    from datetime import date
    f = _anticipo(fecha=date(2026, 4, 6))
    _correr([f], {("AC", 39): [_im("IM-0000584", 2026, "2026-05-27")]})
    assert f["anio_ref"] == 2026
