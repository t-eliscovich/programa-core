"""Edición inline de /proveedores (TMT 2026-07-31).

Dueña: *"dejame editar proveedores"*. La lista sólo dejaba tocar el TIPO, así
que RUC / teléfono / representante / plazo / retenciones quedaban como habían
venido del dBase y no había pantalla para corregirlos.

Lo que se prueba acá es lo que puede lastimar:
  · el nombre de la columna del UPDATE NUNCA sale del request (whitelist);
  · cada campo de la whitelist EXISTE en scintela.proveedor;
  · los montos se leen en formato EU (1.234,56) y vuelven en formato EU;
  · el nombre no se puede vaciar.
"""
import re
from decimal import Decimal
from pathlib import Path

from modules.proveedores import views


def test_la_whitelist_es_la_unica_fuente_del_nombre_de_columna():
    """El f-string del UPDATE se arma con la clave de `_CAMPOS_INLINE`.

    Si alguna vez alguien la reemplaza por el valor crudo del request, esto es
    un INSERT/DROP a discreción. El test clava que la lista es cerrada y que
    ningún nombre trae nada raro.
    """
    for campo in views._CAMPOS_INLINE:
        assert re.fullmatch(r"[a-z_]+", campo), campo


def test_cada_campo_editable_existe_en_la_tabla():
    """Un typo acá sería un 500 recién al tipear en esa celda, en producción."""
    dump = Path("tests/fixtures/legacy_minimal_dump.sql").read_text()
    cuerpo = dump.split("CREATE TABLE scintela.proveedor")[1].split(");")[0]
    columnas = {
        m.group(1) for m in re.finditer(r"^\s*([a-z_]+)\s+\w", cuerpo, re.M)
    }
    faltan = set(views._CAMPOS_INLINE) - columnas
    assert not faltan, f"no existen en scintela.proveedor: {faltan}"


def test_los_montos_vuelven_en_formato_EU():
    """Punto = miles, coma = decimal. Nunca formato US en esta pantalla."""
    assert views._mostrar("num", Decimal("1234.5")) == "1.234,50"
    assert views._mostrar("num", Decimal("1.75")) == "1,75"
    assert views._mostrar("num", None) == ""


def test_el_plazo_vuelve_sin_decimales():
    assert views._mostrar("int", 30) == "30"


def test_las_retenciones_admiten_decimales():
    """`1,75` es una retención real en Ecuador — y se guardaba como `2`.

    Las columnas eran INTEGER: al estrenar la pantalla se tipeó 1,75 en PUGI
    GROUP y volvió 2,00, redondeado en silencio por Postgres. La mig 0147 las
    pasa a numeric(6,2).
    """
    mig = Path("migrations/0147_proveedor_retenciones_con_decimales.sql").read_text()
    assert "ALTER COLUMN retbase TYPE numeric(6,2)" in mig
    assert "ALTER COLUMN retiva  TYPE numeric(6,2)" in mig
    assert views._CAMPOS_INLINE["retbase"][0] == "num"
    assert views._CAMPOS_INLINE["retiva"][0] == "num"


def test_la_pantalla_devuelve_lo_QUE_QUEDO_GUARDADO():
    """RETURNING, no un UPDATE a ciegas.

    Si la columna recorta el valor, la dueña tiene que verlo en el acto en vez
    de irse creyendo que guardó otra cosa. Es justo lo que pasó con 1,75 → 2.
    """
    src = Path("modules/proveedores/views.py").read_text()
    cuerpo = src.split("def api_editar_campo")[1]
    assert "RETURNING {campo} AS guardado" in cuerpo
    assert "execute_returning" in cuerpo


def test_el_nombre_no_se_puede_vaciar():
    src = Path("modules/proveedores/views.py").read_text()
    cuerpo = src.split("def api_editar_campo")[1]
    assert 'campo == "nombre" and not valor' in cuerpo


def test_la_lista_ya_no_imprime_el_literal_None():
    """Jinja renderiza None como el TEXTO "None" — así se veía PUGI GROUP."""
    html = Path(
        "modules/proveedores/templates/proveedores/lista.html").read_text()
    for campo in ("ruc", "telefono", "representante", "nombre"):
        assert f"p.{campo} or ''" in html, campo
