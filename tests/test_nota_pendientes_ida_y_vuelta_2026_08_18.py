"""La NOTA que escribe Alex tiene que sobrevivir el viaje del Excel.

QUÉ PASÓ (18/08/2026). La dueña: *"conciliacion. me imprime cualquier cosa
en el detalle. ni siquiera se si es la sucursal original, y me borra
detalles que alex si habia escrito"*.

Eran dos bugs pegados, los dos en la QUINTA columna del Excel de pendientes:

  1. **El rótulo mentía.** La columna se llamaba `DETALLE` — igual que la
     segunda — y el generador escribía ``detalle or oficina``. Como casi
     ninguna fila tenía nota, lo que salía impreso era la OFICINA del banco
     (AG. NORTE, EL QUINCHE, MERCADO CENTRAL…). Un mismo encabezado con dos
     contenidos distintos según la fila. En el archivo del 18/08: 212 filas
     con nombre de agencia, 34 vacías y UNA sola nota de verdad (``TTY``, la
     que venía de la carga inicial, migración 0055).

  2. **La vuelta se comía la nota.** `_localizar_columnas` detectaba la
     segunda columna DETALLE y la guardaba en ``cols["detalle2"]``…  que
     `parse_hoja_pendientes` NUNCA leía. La nota se mapeaba y se tiraba al
     piso. Después "Hacer prevalecer" borra las filas que no están en el
     archivo y re-inserta las que faltan — sin nota. Cada viaje del Excel
     borraba lo que Alex había escrito a mano.

DECISIÓN de la dueña (18/08): la columna es la NOTA de Alex y NADA MÁS. La
oficina se mira en la pantalla (panel Banco), no en este archivo.

Lo que se clava acá:
  · la quinta columna se llama NOTA (no un segundo DETALLE ambiguo);
  · una fila con oficina y sin nota sale VACÍA — la oficina no se cuela;
  · la nota escrita vuelve a entrar al re-importar (el invariante que
    faltaba: bajar → escribir → subir → sigue estando);
  · los archivos VIEJOS de Alex (dos columnas DETALLE) se siguen leyendo;
  · el badge "⚠ NO IDENT." no se guarda como nota ni se acumula.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

openpyxl = pytest.importorskip("openpyxl")

from modules.conciliacion.hoja_parser import (  # noqa: E402
    BADGE_NO_IDENT,
    limpiar_badge,
    parse_hoja_pendientes,
)

# Fila con nota de Alex Y oficina: la nota gana, la oficina NO se imprime.
_CON_NOTA = {
    "fecha": date(2026, 5, 8), "tipo": "C", "monto": 329.49,
    "concepto": "TRANSFERENCIA DIRECTA DE MENDIETA MARIN BETTY PRISCILLA",
    "documento": "5060382", "oficina": "AG. NORTE", "detalle": "TTY",
}
# Fila SIN nota pero CON oficina: la que salía impresa como si fuera detalle.
_SIN_NOTA = {
    "fecha": date(2026, 7, 21), "tipo": "C", "monto": 1751.80,
    "concepto": "DEPOSITO", "documento": "45297011",
    "oficina": "MERCADO CENTRAL", "detalle": None,
}
_NO_IDENT = {
    "fecha": date(2026, 7, 23), "tipo": "C", "monto": 500.0,
    "concepto": "DEPOSITO NO IDENTIFICADO", "documento": "18773913",
    "oficina": "AG. NORTE", "detalle": "revisar con Andrés",
}


@pytest.fixture()
def hoja(tmp_path, monkeypatch):
    """Genera el Excel REAL con el generador de producción, sin base."""
    from modules.conciliacion import banco_v2_view as v

    monkeypatch.setattr(v, "_PDF_DIR", tmp_path)
    monkeypatch.setattr(
        v._db, "fetch_all",
        lambda sql, params=None, **kw: (
            [] if "banco_conciliacion_match" in sql
            else [dict(_CON_NOTA), dict(_SIN_NOTA), dict(_NO_IDENT)]
        ),
    )
    monkeypatch.setattr(v._sesion, "estado_sesion", lambda s, nb: {})
    monkeypatch.setattr(v._sesion, "cargar_movs", lambda s: [])
    path = v._generar_xlsx_pendientes(
        {"id": 67, "no_banco": 10, "saldo_banco_objetivo": 2683631.95,
         "saldo_banco_detectado": None},
        {"saldo_si_concilio_todo": 2566365.84,
         "pendientes_banco_creditos": 2581.29,
         "pendientes_banco_debitos": 0},
    )
    assert path, "openpyxl tiene que estar para este test"
    return Path(path)


def _filas(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    filas = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return filas


def test_la_quinta_columna_se_llama_nota(hoja):
    """El rótulo repetido `DETALLE` era la mitad del problema: el mismo
    nombre para el concepto del banco y para la nota de Alex."""
    headers = next(f for f in _filas(hoja) if f and f[0] == "FECHA")
    assert headers[:5] == ["FECHA", "DETALLE", "CODIGO", "VALOR", "NOTA"]


def test_la_oficina_no_se_imprime_como_nota(hoja):
    """La fila del 21/07 tiene oficina MERCADO CENTRAL y ninguna nota: la
    celda tiene que salir VACÍA. Antes salía 'MERCADO CENTRAL' bajo un
    encabezado que decía DETALLE."""
    fila = next(f for f in _filas(hoja) if f[2] == "45297011")
    assert not (fila[4] or "").strip()
    texto_hoja = " | ".join(str(c) for f in _filas(hoja) for c in f if c)
    assert "MERCADO CENTRAL" not in texto_hoja
    assert "AG. NORTE" not in texto_hoja


def test_la_nota_de_alex_si_se_imprime(hoja):
    fila = next(f for f in _filas(hoja) if f[2] == "5060382")
    assert fila[4] == "TTY"


def test_la_nota_vuelve_al_re_importar(hoja):
    """⭐ EL INVARIANTE QUE FALTABA: bajar el archivo, subirlo de nuevo, y
    la nota sigue ahí. Es el ciclo de trabajo real de Alex."""
    rows = parse_hoja_pendientes(str(hoja))
    por_doc = {r["documento"]: r for r in rows}
    assert por_doc["5060382"]["nota"] == "TTY"
    assert por_doc["45297011"]["nota"] == ""


def test_el_badge_no_ident_no_se_guarda_como_nota(hoja):
    """El export le cuelga '⚠ NO IDENT.' a los depósitos no identificados
    (pedido de la dueña, 17/06). Si el importador lo guardara, a la vuelta
    siguiente saldría duplicado y después triplicado."""
    fila = next(f for f in _filas(hoja) if f[2] == "18773913")
    assert BADGE_NO_IDENT in fila[4]
    rows = parse_hoja_pendientes(str(hoja))
    nota = next(r for r in rows if r["documento"] == "18773913")["nota"]
    assert nota == "revisar con Andrés"
    assert BADGE_NO_IDENT not in nota


def test_limpiar_badge_no_acumula():
    assert limpiar_badge(f"TTY {BADGE_NO_IDENT} {BADGE_NO_IDENT}") == "TTY"
    assert limpiar_badge(BADGE_NO_IDENT) == ""
    assert limpiar_badge(None) == ""


def test_archivo_viejo_con_dos_detalle_sigue_leyendo_la_nota(tmp_path):
    """Alex tiene archivos YA BAJADOS con el encabezado viejo (DETALLE dos
    veces). Tienen que seguir entrando con su nota."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["MOVIMIENTOS PENDIENTES"])
    ws.append(["FECHA", "DETALLE", "CODIGO", "VALOR", "DETALLE"])
    ws.append(["08/05/2026", "TRANSFERENCIA DIRECTA", "5060382", 329.49, "TTY"])
    p = tmp_path / "viejo.xlsx"
    wb.save(p)
    wb.close()
    rows = parse_hoja_pendientes(str(p))
    assert rows[0]["nota"] == "TTY"


def test_hacer_prevalecer_guarda_la_nota_en_la_base():
    """`reemplazar_historicos_desde_hoja` hace DELETE de TODO el banco y
    re-inserta. Si el INSERT no lleva `detalle`, subir la hoja borra lo
    escrito a mano — que es exactamente lo que reportó la dueña."""
    from modules.conciliacion import hoja_parser as hp

    vistos = []

    class _FakeDB:
        @staticmethod
        def execute(sql, params=None, conn=None):
            vistos.append((sql, params))
            return 1

    import sys as _sys
    monkey = _sys.modules.get("db")
    _sys.modules["db"] = _FakeDB
    try:
        hp.reemplazar_historicos_desde_hoja(
            10,
            [{"fecha": date(2026, 5, 8), "concepto": "TRANSFERENCIA",
              "documento": "5060382", "monto": 329.49, "tipo": "C",
              "nota": "TTY", "fila": 3}],
            conn=object(),
        )
    finally:
        if monkey is not None:
            _sys.modules["db"] = monkey
        else:
            _sys.modules.pop("db", None)

    inserts = [(s, p) for s, p in vistos if "INSERT" in s]
    assert inserts, "tiene que haber un INSERT"
    sql, params = inserts[0]
    assert "detalle" in sql, "el INSERT tiene que escribir la columna detalle"
    assert "TTY" in params, "la nota tiene que llegar a la base"
