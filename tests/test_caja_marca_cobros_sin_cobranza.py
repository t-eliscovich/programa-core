"""La caja marca los cobros en efectivo que quedaron sin cobranza detrás.

TMT 2026-08-03. La dueña cargó 5 cobros en efectivo por la pantalla de caja
(DPS 1.500 · STU 1.000 · YHJ 1.000 · CHI 400 · ECH 502,86 = $4.402,86): entró
la plata, pero sin cobranza la deuda del cliente NO bajó —a YHJ le sobraban
$1.000 y a CHI $400 contra el dBase, clavados— y no contaba para la comisión
del vendedor. En el dBase esto no puede pasar: `PASOCAJA` (ALTAS.PRG L870-893)
crea la entrada de caja A PARTIR del cheque, nunca al revés.

La lista de caja ahora las marca y ofrece convertirlas. Lo que se protege:
que el marcador no se dispare de más (conceptos `CH.*` que no son clientes) ni
de menos (filas que ya tienen cheque).
"""
from __future__ import annotations

import inspect
import os
import re
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.caja import queries as cq  # noqa: E402

_SQL = inspect.getsource(cq.movimientos)
_TPL = os.path.join(_REPO_ROOT, "modules", "caja", "templates", "caja", "lista.html")


def _tpl() -> str:
    with open(_TPL, encoding="utf-8") as fh:
        return fh.read()


def test_la_lista_expone_el_flag():
    assert "AS sin_cobranza" in _SQL
    assert "AS cli_concepto" in _SQL


def test_solo_marca_entradas():
    """Una SALIDA `DEV.<cliente>` es una devolución, no un cobro sin aplicar."""
    assert "c.tipo = 'E'" in _SQL


def test_no_marca_las_que_ya_tienen_cheque():
    assert "c.id_cheque IS NULL" in _SQL


def test_no_marca_conceptos_que_no_son_clientes():
    """`CH.MJM` y `CH.RETAZOS` existen y NO son clientes: el botón fallaría."""
    assert "EXISTS (SELECT 1 FROM scintela.cliente" in _SQL
    assert "SUBSTRING(c.concepto FROM 4)" in _SQL


def test_el_like_va_escapado_para_psycopg():
    """`LIKE 'CH.%'` con paramstyle pyformat necesita `%%` o revienta."""
    assert "LIKE 'CH.%%'" in _SQL
    assert "LIKE 'CH.%'" not in _SQL.replace("LIKE 'CH.%%'", "")


def test_el_template_ofrece_convertir_con_el_id_de_caja():
    tpl = _tpl()
    assert "desde_caja={{ m.id_caja }}" in tpl, "sin el id no puede adoptar la fila"
    assert "no_banco=99" in tpl, "tiene que abrir como EFECTIVO"
    assert "m.sin_cobranza" in tpl


def test_el_link_manda_la_fecha_de_la_fila_no_la_de_hoy():
    """Si abriera con fecha de hoy, el cobro de julio se cargaría en agosto."""
    tpl = _tpl()
    m = re.search(r'href="(/cheques/nuevo\?desde_caja=.*?)"', tpl, re.S)
    assert m, "no encontré el link"
    href = m.group(1)
    assert "fechad={{ m.fecha }}" in href
    assert "today" not in href and "now" not in href


def test_el_boton_esta_gateado_por_permiso():
    tpl = _tpl()
    assert "tiene_permiso('cheques.aplicar')" in tpl


def test_el_importe_va_en_absoluto():
    """La caja guarda el importe POSITIVO y el signo en `tipo`."""
    tpl = _tpl()
    assert "m.importe | abs" in tpl


def test_el_link_manda_tambien_la_fecha_de_recibido():
    """Sin `fecha_recibido`, el cheque nace con fecha de HOY (agosto) y la
    adopción de una fila de caja de JULIO falla por el guard de fecha."""
    tpl = _tpl()
    m = re.search(r'href="(/cheques/nuevo\?desde_caja=.*?)"', tpl, re.S)
    assert "fecha_recibido={{ m.fecha }}" in m.group(1)


def test_el_form_de_cobranza_arrastra_desde_caja():
    """Si el hidden se pierde, el alta crea una SEGUNDA fila de caja."""
    f = os.path.join(_REPO_ROOT, "modules", "cheques", "templates", "cheques", "nuevo.html")
    with open(f, encoding="utf-8") as fh:
        tpl = fh.read()
    assert 'name="desde_caja"' in tpl
    assert "request.form.get('desde_caja')" in tpl
    assert "request.args.get('desde_caja')" in tpl


def test_adoptando_una_caja_el_fechad_es_el_de_la_fila_no_hoy():
    """Visto en vivo con CHI el 04/08: el cheque nació con fechad=03/08
    adoptando una caja del 21/07 → el cobro se iba a la comisión de AGOSTO.

    TMT 2026-08-04 (tarde) — este test exigía una rama
    `if es_deposito and desde_caja` aparte. Ya no existe **porque el mismo
    bug estaba también en la otra puerta** (el alta común: caso ECH,
    cobranza del 21/07 guardada con fechad 04/08). Ahora la regla es una
    sola y vive en `views.fechad_por_defecto`. Se protege lo mismo, pero por
    comportamiento y no por forma — ver
    `tests/test_cheques_fechad_cobranza_retroactiva.py`.
    """
    from datetime import date

    from modules.cheques.views import fechad_por_defecto

    caja_de_julio = date(2026, 7, 21)
    hoy = date(2026, 8, 4)
    assert (
        fechad_por_defecto(
            es_deposito=True, stat="B", fechad_tipeada=hoy, fecha_cobranza=caja_de_julio,
        )
        == caja_de_julio
    )
