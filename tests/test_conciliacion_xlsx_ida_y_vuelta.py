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

QUÉ CAMBIÓ (04/08/2026). La dueña pidió el resumen de vuelta en la MISMA
hoja: *"el archivo de la conciliacion me esta separando los movimientos y
el resumen en hojas distintas, podemos unificar en uno"*. Volver al pie sin
volver al bug se apoya en tres cosas, y las tres se prueban acá:

  1. la fila marcador `RESUMEN CONTABLE`, donde el parser DEJA de leer
     (corte por posición: no depende de conocer los rótulos);
  2. el rótulo en la columna B (CONCEPTO) y no en la C, así la lista negra
     `_FILAS_CIERRE` sí matchea si alguien borra el marcador;
  3. el invariante de siempre: generar el archivo y re-importarlo no puede
     crear NI UN pendiente nuevo.
"""
from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

openpyxl = pytest.importorskip("openpyxl")

from modules.conciliacion.hoja_parser import (  # noqa: E402
    MARCADOR_RESUMEN,
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


def _libro(layout: str):
    """Reproduce los tres layouts que existieron de `_generar_xlsx_pendientes`.

    · ``"pie_viejo"``  — resumen al pie, rótulo en la C (VENENO del 03/08).
    · ``"aparte"``     — resumen en su propia solapa (fix del 03/08).
    · ``"pie_nuevo"``  — resumen al pie detrás del marcador `RESUMEN
      CONTABLE`, rótulo en la B (pedido de la dueña, 04/08). Es el que
      escribe la app HOY.
    """
    assert layout in ("pie_viejo", "aparte", "pie_nuevo")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MOVIMIENTOS PENDIENTES"
    ws["A1"] = "MOVIMIENTOS PENDIENTES"
    ws["A2"] = "Pichincha · Sesión #60 · 2026-08-04 09:27 · 3 movs"
    for col, h in enumerate(["FECHA", "DETALLE", "CODIGO", "VALOR", "DETALLE"], 1):
        ws.cell(row=4, column=col, value=h)
    r = 5
    for fecha, concepto, doc, valor in MOVS:
        ws.cell(row=r, column=1, value=fecha)
        ws.cell(row=r, column=2, value=concepto)
        ws.cell(row=r, column=3, value=doc)
        ws.cell(row=r, column=4, value=valor)
        r += 1

    if layout == "aparte":
        destino, col_label, col_val, rr = wb.create_sheet("RESUMEN"), 1, 2, 4
    elif layout == "pie_viejo":
        # El rótulo en la columna 3 (CODIGO): ahí estaba el veneno.
        destino, col_label, col_val, rr = ws, 3, 4, r + 1
    else:
        destino, col_label, col_val = ws, 2, 4
        rr = r + 1
        ws.cell(row=rr, column=1, value=MARCADOR_RESUMEN)
        rr += 1

    for label, val in ROTULOS_RESUMEN:
        destino.cell(row=rr, column=col_label, value=label)
        destino.cell(row=rr, column=col_val, value=val)
        rr += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _volvieron_solo_los_movs(filas):
    assert len(filas) == len(MOVS), (
        f"el archivo que genera la app no se puede re-importar: volvieron "
        f"{len(filas)} filas y se escribieron {len(MOVS)}"
    )
    assert round(sum(
        f["monto"] if f["tipo"] == "C" else -f["monto"] for f in filas), 2
    ) == round(sum(m[3] for m in MOVS), 2)
    assert {f["documento"] for f in filas} == {m[2] for m in MOVS}


def test_ida_y_vuelta_resumen_al_pie_detras_del_marcador():
    """⭐ El layout de HOY (04/08): una sola hoja, y aun así no se envenena.

    Este es EL test del pedido de la dueña. Si alguien saca el marcador o
    mueve el rótulo a la columna CODIGO, vuelven los $8.304.132,19.
    """
    filas, dropped, ignoradas = parse_hoja_pendientes(
        _libro("pie_nuevo"), return_dropped=True)
    _volvieron_solo_los_movs(filas)
    assert not dropped
    # Una sola ignorada: el marcador. De ahí para abajo no se leyó NADA —
    # por eso no hay 7 rótulos reportados como en el layout viejo.
    assert [i["rotulo"] for i in ignoradas] == [MARCADOR_RESUMEN]


def test_ida_y_vuelta_resumen_en_hoja_aparte():
    """Los archivos generados entre el 03 y el 04/08 siguen entrando bien."""
    filas, dropped, ignoradas = parse_hoja_pendientes(
        _libro("aparte"), return_dropped=True)
    _volvieron_solo_los_movs(filas)
    assert not ignoradas, "el parser ni siquiera debería VER la hoja RESUMEN"


def test_red_para_los_excel_viejos_con_el_resumen_en_la_misma_hoja():
    """Los archivos ya descargados traen el resumen adentro — se filtra igual.

    Este es el caso exacto del 03/08: seis rótulos en la columna CODIGO,
    $8.304.132,19 de créditos fantasma.
    """
    filas, dropped, ignoradas = parse_hoja_pendientes(
        _libro("pie_viejo"), return_dropped=True)
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


def test_el_generador_escribe_el_marcador_antes_del_resumen():
    """Invariante de estructura — el arreglo de raíz vive acá.

    Los tests de arriba prueban que el parser aguanta las tres formas, pero
    ninguno mira el generador real. Ahora que el resumen volvió a la hoja de
    movimientos (pedido de la dueña), lo único que lo hace seguro es que el
    generador escriba el MARCADOR justo antes — y que el rótulo caiga en la
    columna 2 (CONCEPTO) y no en la 3 (CODIGO), que fue el veneno del 03/08.

    Mismo patrón que `test_yy_persist_motor_unico`: se mira el fuente.
    """
    import inspect

    from modules.conciliacion.banco_v2_view import _generar_xlsx_pendientes

    src = inspect.getsource(_generar_xlsx_pendientes)
    i_marcador = src.find("_MARCADOR_RESUMEN")
    i_bloque = src.index("for label, val in rows_resumen:")
    assert 0 <= i_marcador < i_bloque, (
        "el resumen se escribe en la hoja de movimientos SIN el marcador "
        "RESUMEN CONTABLE arriba — el parser no tiene dónde frenar y el "
        "Excel vuelve a envenenar el backlog al re-importarse"
    )
    bloque = src[i_bloque:src.index("column_dimensions", i_bloque)]
    assert "column=2, value=label" in bloque, (
        "el rótulo del resumen tiene que ir a la columna 2 (CONCEPTO): ahí "
        "es donde `_FILAS_CIERRE` lo puede cazar si falta el marcador"
    )
    assert "column=3" not in bloque, (
        "el rótulo volvió a la columna 3 (CODIGO) — es exactamente el bug "
        "del 03/08/2026 ($8.304.132,19 de créditos fantasma)"
    )


def test_los_dos_importadores_frenan_en_el_marcador():
    """`parse_xlsx` (el otro camino de subida) tiene que frenar igual.

    Si sólo frena `hoja_parser`, el mismo archivo se importa limpio por una
    pantalla y envenenado por la otra — el peor de los mundos, porque el
    síntoma aparece días después y en otro lado.
    """
    from modules.conciliacion.parser_xlsx import parse_xlsx

    deps = parse_xlsx(_libro("pie_nuevo"))
    # parse_xlsx sólo devuelve créditos (valor > 0): los 2 depósitos.
    assert len(deps) == 2, [d.concepto for d in deps]
    assert {d.codigo for d in deps} == {"41508270", "115025288"}


def test_END_TO_END_el_archivo_REAL_de_la_app_se_re_importa_limpio(
        tmp_path, monkeypatch):
    """⭐ El test que no se puede engañar: generador REAL → parser REAL.

    Los de arriba replican el layout a mano; este corre
    `_generar_xlsx_pendientes` de verdad, abre el .xlsx que sale y lo mete
    por `parse_hoja_pendientes`. Si el resumen vuelve a colarse como
    movimientos —por el motivo que sea, incluso uno que no se nos ocurrió—
    acá se ve.
    """
    from modules.conciliacion import banco_v2_view as v

    pendientes = [
        {"fecha": f, "concepto": c, "documento": d, "monto": abs(m),
         "oficina": "AG. NORTE", "detalle": "",
         "tipo": "C" if m > 0 else "D"}
        for f, c, d, m in [
            (date(2026, 4, 9), "DEPOSITO", "41508270", 590.27),
            (date(2026, 4, 13), "DEPOSITO", "115025288", 2300.0),
            (date(2026, 7, 31), "INTELA C-PAG-CASH 07 31", "45873196", -24147.29),
        ]
    ]

    def _fake_fetch_all(sql, params=None, conn=None):
        if "banco_historicos_pendientes" in sql:
            return list(pendientes)
        return []

    monkeypatch.setattr(v._db, "fetch_all", _fake_fetch_all)
    monkeypatch.setattr(v._sesion, "estado_sesion", lambda s, nb: {})
    monkeypatch.setattr(v._sesion, "cargar_movs", lambda s: [])
    monkeypatch.setattr(v, "_PDF_DIR", tmp_path)

    sesion = {"id": 60, "no_banco": 10, "saldo_banco_objetivo": 2683631.95,
              "saldo_banco_detectado": None}
    balance = {"saldo_si_concilio_todo": 2566365.84,
               "pendientes_banco_creditos": 2890.27,
               "pendientes_banco_debitos": 24147.29}

    ruta = v._generar_xlsx_pendientes(sesion, balance)
    assert ruta, "el generador no produjo el archivo"

    # 1. Una sola hoja — que es lo que pidió la dueña.
    wb = openpyxl.load_workbook(ruta)
    assert wb.sheetnames == ["MOVIMIENTOS PENDIENTES"], (
        f"el archivo volvió a salir partido en solapas: {wb.sheetnames}")
    plano = "\n".join(
        " | ".join(str(c) for c in fila if c is not None)
        for fila in wb.active.iter_rows(values_only=True)
    )
    wb.close()
    # 2. Y el resumen está adentro de esa hoja (no se perdió).
    assert MARCADOR_RESUMEN in plano
    assert "SALDO BANCO (extracto)" in plano

    # 3. Re-importarlo no crea NI UN pendiente nuevo.
    filas, dropped, ignoradas = parse_hoja_pendientes(
        Path(ruta).read_bytes(), return_dropped=True)
    assert len(filas) == len(pendientes), (
        f"al re-importar el archivo que genera la app volvieron {len(filas)} "
        f"filas y se habían escrito {len(pendientes)}: "
        + str([f["documento"] for f in filas])
    )
    assert {f["documento"] for f in filas} == {p["documento"] for p in pendientes}
    assert not dropped
    assert [i["rotulo"] for i in ignoradas] == [MARCADOR_RESUMEN]
