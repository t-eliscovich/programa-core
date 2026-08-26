"""El código de barras de la clave de acceso — el mismo que imprime Asinfo.

⭐ El test que importa no es que "dibuje un código de barras": es que dibuje
EXACTAMENTE el mismo que el papel. `ANCHOS_DEL_PAPEL` son los 169 anchos
—barras y espacios, en módulos— leídos con `pdfplumber` del PDF real de la
001-099-000182675, rectángulo por rectángulo. Si alguien toca la tabla de
patrones o la forma de armar los símbolos, esto se pone rojo.

De paso deja escrito lo que no era obvio: los 49 dígitos son impares, así que
Asinfo les pone un CERO ADELANTE y los codifica en el subconjunto C, de a dos.
"""
from __future__ import annotations

import pytest

from modules._lib import code128

CLAVE = "2608202601179112576200120010990001826750017716919"

#: Los 169 anchos del código de barras del PDF, empezando por una barra.
ANCHOS_DEL_PAPEL = [int(c) for c in (
    "211232222221314111121241222221314111231212134111231212321122"
    "221114221231222122221231222122221213214121212222223211321221"
    "2412112122221232211221141122142211321111432331112"
)]


def test_las_barras_son_las_del_papel():
    """169 anchos, 310 módulos, uno por uno."""
    assert code128.anchos(CLAVE) == ANCHOS_DEL_PAPEL


def test_ocupa_los_mismos_modulos_que_el_papel():
    assert sum(ANCHOS_DEL_PAPEL) == 310
    assert code128.ancho_total(CLAVE) == 330      # con los dos silencios


def test_arranca_en_el_subconjunto_C_y_termina_en_parada():
    vals = code128.valores(CLAVE)
    assert vals[0] == code128.ARRANQUE_C
    assert vals[-1] == code128.PARADA


def test_los_49_digitos_se_aparean_con_un_cero_adelante():
    """Impares no se pueden codificar de a dos: el cero de adelante no es
    decorativo, es lo que hace que el símbolo de arranque sea Start C."""
    vals = code128.valores(CLAVE)
    # arranque + 25 pares + verificador + parada
    assert len(vals) == 1 + 25 + 1 + 1
    assert vals[1] == 2 and vals[2] == 60     # "02" y "60" de 0+2608...


def test_el_verificador_es_el_del_estandar():
    vals = code128.valores(CLAVE)
    esperado = (vals[0] + sum(i * v for i, v in enumerate(vals[1:-2], start=1))) % 103
    assert vals[-2] == esperado


def test_todos_los_simbolos_suman_once_modulos():
    """Menos el de parada, que tiene siete anchos y suma trece."""
    for i, patron in enumerate(code128.PATRONES):
        suma = sum(int(c) for c in patron)
        assert suma == (13 if i == code128.PARADA else 11), i


def test_no_hay_dos_simbolos_iguales():
    """Un patrón repetido haría que el lector lea otra cosa."""
    assert len(set(code128.PATRONES)) == len(code128.PATRONES)


def test_solo_se_dibujan_las_barras_negras():
    barras = code128.barras(CLAVE)
    assert len(barras) == len(ANCHOS_DEL_PAPEL) // 2 + 1
    assert barras[0] == (code128.SILENCIO, ANCHOS_DEL_PAPEL[0])
    # la última barra tiene que terminar antes del silencio de la derecha
    fin, ancho = barras[-1]
    assert fin + ancho <= code128.ancho_total(CLAVE) - code128.SILENCIO


@pytest.mark.parametrize("basura", ["", "  ", None, "12a4", "001-099-000182675"])
def test_lo_que_no_son_digitos_no_se_codifica(basura):
    """Un código de barras que dice cualquier cosa es peor que no tenerlo."""
    with pytest.raises(ValueError):
        code128.anchos(basura)


def test_un_numero_par_de_digitos_no_lleva_relleno():
    assert len(code128.valores("2608")) == 1 + 2 + 1 + 1
