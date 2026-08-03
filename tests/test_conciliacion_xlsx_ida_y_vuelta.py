"""El Excel de pendientes tiene que poder re-importarse sin envenenarse.

QUÉ PASÓ (03/08/2026). El ciclo normal de trabajo es: bajar el Excel de
pendientes, trabajarlo, y volver a subirlo con "Hacer prevalecer". Pero el
generador escribía el RESUMEN contable al pie de la MISMA hoja, con el
rótulo en la columna C (CODIGO) y el importe en la D (VALOR) — y
`hoja_parser` lee el DOCUMENTO justamente de la columna C.

Para el importador, `"SALDO BANCO (extracto)"` no era un rótulo: era un
número de documento válido. Y la lista negra que debía frenarlo comparaba
contra el CONCEPTO (columna B), que en esas filas viene VACÍA — así que no
matcheaba nunca. Encima comparaba por igualdad exacta, así que
`"SALDO BANCO (extracto)"` tampoco habría entrado por el sufijo.

Resultado: se cargaron las 6 líneas del resumen como pendientes del banco,
**$8.304.132,19** de créditos fantasma, y el export siguiente salió con
DIFERENCIA −8.323.357,19. La dueña las borró a mano y avisó que "el saldo
de banco y otras líneas salieron repetidas".

⚠ El arreglo NO puede ser "una fila sin fecha no es un movimiento": hay
pendientes legítimos sin fecha, a pedido expreso de la dueña (2026-06-04,
*"quiero que los -15.835,60 prevalezcan aunque no tengan fecha"*). Ese
caso está clavado abajo.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

openpyxl = pytest.importorskip("openpyxl")

from modules.conciliacion.hoja_parser import (  # noqa: E402
    _es_fila_de_resumen,
    parse_hoja_pendientes,
)

# Los 7 rótulos exactos que escribe `_generar_xlsx_pendientes`.
ROTULOS_RESUMEN = [
    ("+ Pendientes banco créditos", 253236.34),
    ("− Pendientes banco débitos", -135970.23),
    ("AJUSTE", 117266.11),
    ("SALDO SISTEMA (conciliado)", 2566365.84),
    ("TOTAL", 2683631.95),
    ("SALDO BANCO (extracto)", 2683631.95),
    ("DIFERENCIA", 0),
]

MOVS = [
    ("09/04/2026", "DEPOSITO", "41508270", 590.27),
    ("13/04/2026", "DEPOSITO", "115025288", 2300.0),
    ("31/07/2026", "2607310E0914-INTELA C-PAG-CASH 07 31", "45873196", -24147.29),
]


def _libro(*, resumen_en_hoja_aparte: bool):
    """Reproduce el layout de `_generar_xlsx_pendientes`."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MOVIMIENTOS PENDIENTES"
    ws["A1"] = "MOVIMIENTOS PENDIENTES"
    ws["A2"] = "Pichincha · Sesión #60 · 2026-08-03 09:27 · 3 movs"
    for col, h in enumerate(["FECHA", "DETALLE", "CODIGO", "VALOR", "DETALLE"], 1):
        ws.cell(row=4, column=col, value=h)
    r = 5
    for fecha, concepto, doc, valor in MOVS:
        ws.cell(row=r, column=1, value=fecha)
        ws.cell(row=r, column=2, value=concepto)
        ws.cell(row=r, column=3, value=doc)
        ws.cell(row=r, column=4, value=valor)
        r += 1
    destino = wb.create_sheet("RESUMEN") if resumen_en_hoja_aparte else ws
    # El rótulo va a la columna 3 (CODIGO) en la hoja de movs — ahí estaba
    # el veneno — y a la 1 en la hoja RESUMEN.
    col_label = 1 if resumen_en_hoja_aparte else 3
    col_val = 2 if resumen_en_hoja_aparte else 4
    rr = 4 if resumen_en_hoja_aparte else r + 1
    for label, val in ROTULOS_RESUMEN:
        destino.cell(row=rr, column=col_label, value=label)
        destino.cell(row=rr, column=col_val, value=val)
        rr += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_ida_y_vuelta_resumen_en_hoja_aparte():
    """Con el resumen en su propia hoja, vuelven EXACTAMENTE los movimientos."""
    filas, dropped, ignoradas = parse_hoja_pendientes(
        _libro(resumen_en_hoja_aparte=True), return_dropped=True)
    assert len(filas) == len(MOVS), (
        f"el archivo que genera la app no se puede re-importar: volvieron "
        f"{len(filas)} filas y se escribieron {len(MOVS)}"
    )
    assert not ignoradas, "el parser ni siquiera debería VER la hoja RESUMEN"
    assert round(sum(
        f["monto"] if f["tipo"] == "C" else -f["monto"] for f in filas), 2
    ) == round(sum(m[3] for m in MOVS), 2)
    assert {f["documento"] for f in filas} == {m[2] for m in MOVS}


def test_red_para_los_excel_viejos_con_el_resumen_en_la_misma_hoja():
    """Los archivos ya descargados traen el resumen adentro — se filtra igual.

    Este es el caso exacto del 03/08: seis rótulos en la columna CODIGO,
    $8.304.132,19 de créditos fantasma.
    """
    filas, dropped, ignoradas = parse_hoja_pendientes(
        _libro(resumen_en_hoja_aparte=False), return_dropped=True)
    assert len(filas) == len(MOVS), (
        "volvió a entrar el resumen contable como si fueran movimientos"
    )
    # Las 7 se reportan explícitamente en vez de descartarse en silencio
    # (DIFERENCIA ahora también, por rótulo — antes sólo la salvaba el
    # filtro de valor cero).
    assert len(ignoradas) == 7
    assert {"AJUSTE", "TOTAL"} <= {i["rotulo"] for i in ignoradas}


@pytest.mark.parametrize("rotulo", [r[0] for r in ROTULOS_RESUMEN])
def test_todos_los_rotulos_del_resumen_se_reconocen(rotulo):
    """Incluidos los que llevan sufijo — por eso NO alcanza igualdad exacta."""
    assert _es_fila_de_resumen(rotulo) == rotulo
    assert _es_fila_de_resumen("", rotulo) == rotulo


def test_un_pendiente_de_verdad_no_se_confunde_con_resumen():
    """⚠ El filtro NO puede ser "contiene": se comía datos buenos.

    `AJUSTE AC97 SIN FECHA` es el pendiente que la dueña pidió que prevalezca
    (2026-06-04) y `DE AJUSTE CONCILIACION` lo carga la propia conciliación
    en el banco. Un filtro que se lleva puesto un dato bueno es peor que el
    bug que arregla.
    """
    for texto in ("DEPOSITO", "TRANSFERENCIA DIRECTA DE PENA ERAZO",
                  "PAGO SENAE 51775463", "DEPOSITO CHEQUE",
                  "AJUSTE AC97 SIN FECHA", "DE AJUSTE CONCILIACION",
                  "SALDO A FAVOR CLIENTE", "TOTALIZADOR",
                  "2607310E0914-INTELA C-PAG-CASH 07 31"):
        assert _es_fila_de_resumen(texto, "45873196") is None, texto


def test_pendiente_SIN_FECHA_sigue_entrando():
    """Pedido expreso de la dueña (2026-06-04) — no se puede filtrar por fecha.

    *"quiero que los -15.835,60 prevalezcan aunque no tengan fecha. ponele un
    tag sin fecha pero sumalo igual"*.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    for col, h in enumerate(["FECHA", "DETALLE", "CODIGO", "VALOR"], 1):
        ws.cell(row=1, column=col, value=h)
    ws.cell(row=2, column=2, value="AJUSTE AC97 SIN FECHA")
    ws.cell(row=2, column=3, value="AC97")
    ws.cell(row=2, column=4, value=-15835.60)
    buf = io.BytesIO()
    wb.save(buf)

    filas = parse_hoja_pendientes(buf.getvalue())
    assert len(filas) == 1, "se filtró un pendiente legítimo que no tiene fecha"
    assert filas[0]["fecha"] is None
    assert filas[0]["monto"] == 15835.60
    assert filas[0]["tipo"] == "D"


# --- el health que lo caza si ya está adentro ------------------------------


def _pend(id_, concepto, documento, monto, fecha="2026-07-31"):
    return {"id": id_, "concepto": concepto, "documento": documento,
            "monto": monto, "fecha": fecha, "tipo": "C"}


def test_health_caza_el_resumen_cargado_como_pendiente():
    from modules.admin_dbase.health_audit_view import _evaluar_pendientes

    filas = [
        _pend(1, "DEPOSITO", "41508270", 590.27),
        _pend(2, "", "+ Pendientes banco créditos", 253236.34, fecha=None),
        _pend(3, "", "SALDO BANCO (extracto)", 2683631.95, fecha=None),
        _pend(4, "", "AJUSTE", 117266.11, fecha=None),
    ]
    stat, alerts = _evaluar_pendientes(
        no_banco=10, nombre="PICHINCHA", filas=filas, saldo_banco=2489392.64)
    assert stat["n_rotulos_de_resumen"] == 3
    assert stat["monto_rotulos"] == 3054134.40
    assert "resumen_como_pendiente" in {a["category"] for a in alerts}


def test_health_no_alerta_por_los_sin_fecha_legitimos():
    """Los sin fecha se CUENTAN pero no alertan — la dueña los pidió."""
    from modules.admin_dbase.health_audit_view import _evaluar_pendientes

    filas = [_pend(1, "AJUSTE AC97 SIN FECHA", "AC97", 15835.60, fecha=None)]
    stat, alerts = _evaluar_pendientes(
        no_banco=10, nombre="PICHINCHA", filas=filas, saldo_banco=2489392.64)
    assert stat["n_sin_fecha"] == 1
    assert alerts == [], "un ⚠ diario por algo legítimo entrena a ignorar el panel"


def test_health_callado_cuando_esta_limpio():
    from modules.admin_dbase.health_audit_view import _evaluar_pendientes

    filas = [_pend(1, "DEPOSITO", "41508270", 590.27),
             _pend(2, "DEPOSITO CHEQUE", "34535095", 3000.0)]
    stat, alerts = _evaluar_pendientes(
        no_banco=10, nombre="PICHINCHA", filas=filas, saldo_banco=2489392.64)
    assert stat["n_rotulos_de_resumen"] == 0
    assert alerts == []


def test_el_generador_escribe_el_resumen_en_OTRA_hoja():
    """Invariante de estructura — el arreglo de raíz vive acá.

    Los tests de arriba prueban que el parser aguanta las dos formas, pero
    ninguno mira el generador real. Si mañana alguien vuelve a poner el
    resumen al pie de la hoja de movimientos, el archivo se vuelve a
    envenenar y todo lo demás sigue en verde.

    Mismo patrón que `test_yy_persist_motor_unico`: se mira el fuente.
    """
    import inspect

    from modules.conciliacion.banco_v2_view import _generar_xlsx_pendientes

    src = inspect.getsource(_generar_xlsx_pendientes)
    assert 'wb.create_sheet("RESUMEN")' in src, (
        "el resumen contable volvió a la hoja de movimientos — el Excel que "
        "genera la app se puede re-importar y envenenar el backlog"
    )
    # El bloque que vuelca `rows_resumen` NO puede escribir en `ws`.
    i = src.index("for label, val in rows_resumen:")
    bloque = src[i:src.index("column_dimensions", i)]
    assert "ws.cell(" not in bloque, (
        "el resumen se está escribiendo en la hoja de movimientos (`ws`); "
        "tiene que ir a la hoja RESUMEN (`ws_res`)"
    )
    assert "ws_res.cell(" in bloque
