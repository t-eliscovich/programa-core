"""«Cliente desde» y «Compra promedio» en la ficha del cliente.

TMT 2026-08-30 — Jaime (oficina): *"podemos adicionar como información desde
cuándo es cliente y qué monto promedio de compra tiene? en la opción 4V del
dBase se podía ver el promedio de venta en kilos y dólares"*.

Las reglas de qué fecha se muestra son funciones puras en
`modules/clientes/primera_compra_asinfo.py` — se testean acá sin Postgres.
El bloque del template se verifica por SOURCE (mismo criterio que
test_estado_cuenta_ficha_header.py: en CI no hay base).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from modules.clientes.primera_compra_asinfo import (
    cliente_desde,
    etiqueta_desde,
)

HOY = date(2026, 8, 30)


# ─────────────────────────── cliente_desde ───────────────────────────


def test_gana_la_memoria_mas_vieja():
    """PC arranca en 2025; Asinfo sabe que compra desde 2022. Gana Asinfo."""
    assert cliente_desde(date(2025, 1, 23), date(2022, 6, 30)) == date(2022, 6, 30)


def test_sin_espejo_queda_la_fecha_local():
    """Espejo sin refrescar (o cliente sin RUC): vale la primera factura local."""
    assert cliente_desde(date(2025, 1, 23), None) == date(2025, 1, 23)


def test_cliente_sin_facturas_no_inventa_fecha():
    assert cliente_desde(None, None) is None


def test_cliente_nuevo_cargado_primero_en_pc():
    """Un cliente nuevo puede facturarse en PC antes de que el espejo lo vea."""
    assert cliente_desde(date(2026, 8, 1), date(2026, 8, 15)) == date(2026, 8, 1)


# ─────────────────────────── etiqueta_desde ───────────────────────────


def test_etiqueta_mes_anio_y_antiguedad():
    assert etiqueta_desde(date(2022, 6, 30), HOY) == "jun 2022 · hace 4 años"


def test_etiqueta_un_anio_en_singular():
    assert etiqueta_desde(date(2025, 1, 23), HOY) == "ene 2025 · hace 1 año"


def test_etiqueta_menos_de_un_anio_sin_hace():
    """A un cliente de marzo no se le dice "hace 0 años"."""
    assert etiqueta_desde(date(2026, 3, 10), HOY) == "mar 2026"


def test_el_arranque_del_registro_no_se_vende_como_fecha_exacta():
    """Una primera factura de 2019 cae donde ARRANCA el registro de Asinfo:
    el cliente casi seguro es más viejo. Se dice "o antes", no una precisión
    que el dato no tiene — esto va en CERTIFICADOS que se le dan al cliente."""
    assert etiqueta_desde(date(2019, 9, 15), HOY) == "2019 o antes"


def test_sin_fecha_etiqueta_vacia():
    assert etiqueta_desde(None, HOY) == ""


# ─────────────────────────── el bloque en la ficha ───────────────────────────

_TPL = (
    Path(__file__).resolve().parents[1]
    / "modules" / "informes" / "templates" / "informes" / "estado_cuenta.html"
).read_text(encoding="utf-8")


def test_la_ficha_muestra_las_dos_lineas():
    """Las dos líneas pedidas están en el template, con la misma etiqueta
    simple que el resto del bloque (ec-lbl) y SIEMPRE presentes (con «—» si
    no hay dato, para que un campo vacío no se confunda con uno que no
    existe)."""
    assert 'Cliente desde:' in _TPL
    assert 'Compra promedio:' in _TPL
    assert 'compras_resumen' in _TPL


def test_la_vista_pasa_el_resumen_best_effort():
    """La vista lo calcula con `_safe`: si la consulta falla, la ficha se
    abre igual (la lección del 404 mentiroso de NDL, 2026-08-03)."""
    src = (
        Path(__file__).resolve().parents[1] / "modules" / "informes" / "views.py"
    ).read_text(encoding="utf-8")
    assert "compras_resumen_cliente(codigo_up), {})" in src
