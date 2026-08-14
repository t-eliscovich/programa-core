"""Un solo vocabulario para el TIPO de documento, y un filtro que ande.

TMT 2026-08-14 (dueña): *"no anda el filtro"* y *"¿cuánto suman?"*. Abajo de
esos dos síntomas había TRES clasificadores que no se hablaban:

1. `factura.tipo` escrita en DOS idiomas — el importador viejo ponía una letra
   (F/N/C/D/X), el nuevo dos (FA/NT/NC/DE), porque la columna era `varchar(2)`
   y guardaba `tipo_asinfo[:2]`. Ese recorte metía `NC_FINANCIERA` y `NCNT` en
   la misma "NC": medido en producción, 235 financieras (kg=0) y 18 NCNT
   (kg<0) bajo la misma etiqueta.
2. La letra de la lista, adivinada en el template por prefijos. Las 263 NCNT
   viejas (tipo 'X') no encajaban en ninguna regla y caían en el `or negativo`
   final: se mostraban como **D · Devolución**.
3. El filtro, que no miraba la columna: le preguntaba a Asinfo en el momento.
   En la pestaña Estado esa consulta ni corre, así que filtrar daba CERO sobre
   35.526 facturas — y los totales del encabezado, que salen del SQL, nunca lo
   veían.

Vocabulario elegido por la dueña, igual al que Asinfo emite: F / D / N / NC /
NCNT.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.facturas import tipo_doc  # noqa: E402

# ── 1) el vocabulario ────────────────────────────────────────────────────────

def test_los_codigos_entran_en_la_columna():
    """La migración 0192 dejó `tipo` en varchar(4) — justo lo que mide NCNT.
    Si alguien agrega un código más largo, se guarda RECORTADO y en silencio,
    que es exactamente el bug que estamos arreglando."""
    for c in tipo_doc.CODIGOS:
        assert len(c) <= 4, f"'{c}' no entra en varchar(4)"


def test_la_X_ya_no_existe_como_codigo():
    """Se retiró a propósito: se lee como 'eliminada' (que es un `stat`, no un
    tipo) y ya hizo dudar a la dueña sobre qué eran esas 263 facturas."""
    assert "X" not in tipo_doc.CODIGOS


@pytest.mark.parametrize("viejo,esperado", [
    ("F", "F"), ("FA", "F"), ("FACTURA", "F"),
    ("N", "N"), ("NT", "N"), ("NTEN", "N"),
    ("D", "D"), ("DE", "D"), ("DEVOLUCION", "D"),
    ("C", "NC"), ("NC_FINANCIERA", "NC"),
    ("X", "NCNT"), ("NCNT", "NCNT"),
])
def test_los_dos_idiomas_viejos_caen_en_el_mismo_lugar(viejo, esperado):
    assert tipo_doc.normalizar(viejo) == esperado


# ── 2) el caso ambiguo, que es el que importa ────────────────────────────────

def test_NC_con_kilos_es_NCNT_y_sin_kilos_es_FINANCIERA():
    """EL BUG: `tipo_asinfo[:2]` convertía NC_FINANCIERA y NCNT en la misma
    "NC". Los separa la MERCADERÍA — una nota de crédito financiera es un
    descuento y no mueve kilos; una NCNT es mercadería que volvió."""
    assert tipo_doc.normalizar("NC", kg=0) == "NC"
    assert tipo_doc.normalizar("NC", kg=-91) == "NCNT"


def test_la_numeracion_manda_sobre_los_kilos():
    """`NCNT-01095` lo dice con todas las letras; no hace falta inferir."""
    assert tipo_doc.normalizar("NC", kg=0, numf_completo="NCNT-01095") == "NCNT"


def test_una_devolucion_no_se_confunde_con_una_NCNT():
    """Las dos devuelven mercadería (kg<0). La diferencia es la numeración: la
    devolución sale con número de FACTURA, la NCNT con serie propia."""
    assert tipo_doc.normalizar("D", kg=-5, numf_completo="001-099-000010727") == "D"
    assert tipo_doc.normalizar("X", kg=-91, numf_completo="NCNT-01095") == "NCNT"


def test_sin_tipo_se_deduce_de_la_numeracion_o_no_se_deduce():
    assert tipo_doc.normalizar(None, numf_completo="NCNT-00710") == "NCNT"
    assert tipo_doc.normalizar(None, numf_completo="NTEN-10309") == "N"
    # Un kg negativo NO alcanza: puede ser devolución o NCNT. Antes que
    # inventar, se deja sin clasificar.
    assert tipo_doc.normalizar(None, kg=-5) is None
    assert tipo_doc.normalizar("1") is None      # la fila basura de 2021
    assert tipo_doc.normalizar("") is None


# ── 3) el filtro va al SQL, antes de paginar ─────────────────────────────────

class _CapturaSQL:
    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def fetch_all(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params or {}))
        return []

    def fetch_one(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params or {}))
        return {}

    def principal(self):
        for sql, params in self.queries:
            if isinstance(params, dict) and "tipo" in params:
                return sql, params
        raise AssertionError("ninguna consulta recibió el parámetro tipo")


@pytest.fixture
def cap(monkeypatch):
    from modules.facturas import queries as fq
    c = _CapturaSQL()
    monkeypatch.setattr(fq, "db", c)
    return c


def test_el_tipo_filtra_en_el_WHERE(cap):
    """EL BUG QUE ESTE TEST CUIDA: filtrar en Python después de traer la página
    daba CERO en la pestaña Estado (72 páginas) y dejaba los totales del
    encabezado hablando del universo entero."""
    from modules.facturas import queries as fq
    fq.buscar(vista="estado", tipo="D")
    sql, params = cap.principal()
    assert params["tipo"] == "D"
    assert "UPPER(TRIM(COALESCE(f.tipo,''))) = %(tipo)s" in sql


def test_el_filtro_va_ANTES_del_LIMIT(cap):
    from modules.facturas import queries as fq
    fq.buscar(vista="estado", tipo="D")
    sql, _ = cap.principal()
    assert sql.index("%(tipo)s") < sql.rindex("LIMIT")


def test_el_contador_del_encabezado_recibe_el_MISMO_tipo(cap):
    """Si el agregado no lo recibiera, la pantalla seguiría diciendo 35.526
    arriba de las filas filtradas — que es el síntoma que reportó la dueña."""
    from modules.facturas import queries as fq
    fq.contar_filtrado(vista="estado", tipo="NCNT")
    _, params = cap.principal()
    assert params["tipo"] == "NCNT"


def test_se_pueden_pedir_las_que_no_tienen_tipo(cap):
    """4.720 facturas quedan sin clasificar y tienen que ser encontrables, no
    invisibles."""
    from modules.facturas import queries as fq
    fq.buscar(vista="estado", tipo="__SIN__")
    sql, params = cap.principal()
    assert params["tipo"] == "__SIN__"
    assert "__SIN__" in sql


def test_sin_filtro_no_se_toca_nada(cap):
    from modules.facturas import queries as fq
    fq.buscar(vista="cartera")
    _, params = cap.principal()
    assert params["tipo"] is None


# ── 4) el importador ─────────────────────────────────────────────────────────

def test_el_importador_ya_no_recorta_el_tipo():
    """`tipo_asinfo[:2]` era la causa raíz: partía NC_FINANCIERA y NCNT por la
    mitad y las dejaba iguales."""
    import inspect

    from modules.facturas import views as fv
    for fn in (fv.cargar_desde_asinfo, fv.cargar_desde_asinfo_bulk):
        src = inspect.getsource(fn)
        # Se mira el CÓDIGO, no los comentarios (que hablan del bug viejo).
        codigo = "\n".join(x.split("#")[0] for x in src.split("\n"))
        assert "[:2]" not in codigo, fn.__name__
        assert "tipo_doc.normalizar" in codigo, fn.__name__
