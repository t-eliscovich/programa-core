"""VENDEDOR y MAIL en el bloque de contacto del estado de cuenta.

TMT 2026-08-03 (dueña, sobre el screenshot de AREQUIPA DEFAZ RONIE SANTIAGO):
"en esta información breve que hay del cliente podemos adicionar VENDEDOR /
MAIL. Puedes hacer en otra columna, no quiero que tanto espacio nos quite
información o sea dejar ese espacio celeste y otra columna".

Segundo mensaje, mismo día: "pone codigo mejor, chequia que tengas los
codigos". El vendedor va como CÓDIGO (`cliente.vend`), no como nombre.

Dos cosas, y las dos importan:
  1. Los datos tienen que VENIR: `cliente.correo` y `cliente.vend`.
  2. Tienen que ir en una SEGUNDA COLUMNA — el hueco vacío que ella marcó a la
     derecha de la dirección — y no debajo, para no alargar el bloque ni
     achicar lo que ya estaba.

Estado de la data el 03/08 (/admin/sql, 3.973 clientes): 2.456 con código de
vendedor cargado; `correo` cargado en **1 solo cliente** (DGG). El campo Mail
va a salir "—" casi siempre hasta que se carguen — se completa desde
"Editar cliente".

Mismo patrón que tests/test_estado_cuenta_totales.py: inspeccionan el source,
no tocan Postgres.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

_TPL = (
    Path(__file__).resolve().parents[1]
    / "modules" / "informes" / "templates" / "informes" / "estado_cuenta.html"
)


def _src_estado_cuenta() -> str:
    from modules.informes import queries as iq
    return inspect.getsource(iq.estado_cuenta_cliente)


def _sql_cliente() -> str:
    """El SELECT de la ficha del cliente (el primero de la función)."""
    src = _src_estado_cuenta()
    i = src.find("FROM scintela.cliente")
    assert i > 0, "no encontré la query de la ficha del cliente"
    j = src.rfind("SELECT", 0, i)
    return src[j:src.find('"""', i)]


def test_la_query_del_cliente_trae_correo_y_vendedor():
    sql = _sql_cliente()
    assert "AS correo" in sql, "el mail del cliente (cliente.correo) no se trae"
    assert re.search(r"c\.vend[^\n]*AS vend", sql), "el vendedor no se trae"


def test_el_vendedor_es_el_CODIGO_sin_joinear_la_tabla_de_vendedores():
    """Dueña 2026-08-03: "pone codigo mejor".

    Y no es sólo preferencia: `scintela.vendedor` NO tiene nombres. Verificado
    ese día por /admin/sql — las 21 filas tienen `nombre` = `codigo` ('BED' →
    'BED', 'EDG' → 'EDG'). El LEFT JOIN devolvía exactamente el mismo string
    que `c.vend`, a cambio de un join. Si algún día se les carga nombre de
    verdad desde /comisiones, este test es el lugar donde discutirlo.
    """
    # Sin los comentarios: el propio SQL explica ahí por qué NO se joinea.
    sql = "\n".join(
        ln for ln in _sql_cliente().splitlines() if not ln.strip().startswith("--")
    )
    assert "scintela.vendedor" not in sql, (
        "el join a scintela.vendedor no aporta nada: esa tabla tiene "
        "nombre = codigo. En pantalla va el código de cliente.vend."
    )
    assert "NULLIF(TRIM(c.vend), '')" in sql, (
        "el código tiene que venir trimmeado — cliente.vend es CHAR(3) en el "
        "dBase y llega con espacios"
    )


def test_el_bloque_de_contacto_es_de_dos_columnas():
    """Lo pidió explícito: 'en otra columna', no debajo."""
    html = _TPL.read_text(encoding="utf-8")
    i = html.index("Dirección (2)")
    # El <div> que abre el bloque está justo arriba de las direcciones.
    j = html.rfind('class="no-print mb-5', 0, i)
    assert j > 0, "no encontré el contenedor del bloque de contacto"
    contenedor = html[j:html.index(">", j)]
    assert "grid" in contenedor and "grid-cols-2" in contenedor, (
        f"el bloque de contacto dejó de ser una grilla de 2 columnas: {contenedor}"
    )


def test_vendedor_y_mail_estan_en_la_segunda_columna():
    html = _TPL.read_text(encoding="utf-8")
    i_dir = html.index("Dirección (2)")
    i_tel = html.index("Tel:</span>", i_dir)
    i_vend = html.index("Vendedor:</span>", i_dir)
    i_mail = html.index("Mail:</span>", i_dir)
    assert i_tel < i_vend < i_mail, (
        "Vendedor y Mail van DESPUÉS de la primera columna (dirección/RUC/tel)"
    )
    # Entre el teléfono y el vendedor tiene que cerrar una columna y abrir otra.
    entre = html[i_tel:i_vend]
    assert "</div>" in entre and "<div>" in entre, (
        "Vendedor/Mail quedaron dentro de la MISMA columna que la dirección"
    )


def test_la_query_trae_las_TRES_fuentes_del_mail_sin_decidir():
    """Dueña 2026-08-03: "mail está en observaciones y lo veo cuando pongo
    editar" — y después, sobre completar los cortados, "solo los vacíos".

    La query trae las tres fuentes CRUDAS (ficha, Observación, espejo de
    Asinfo) y no elige: la prioridad se resuelve en Python
    (`modules/clientes/mail_asinfo.resolver`) porque **en CI no hay Postgres**
    y una regla escrita en SQL no se puede testear. Ver tests/test_mail_asinfo.py.
    """
    sql = _sql_cliente()
    for campo in ("c.correo", "c.observacion", "ma.email"):
        assert campo in sql, f"falta traer {campo}"
    assert "LEFT JOIN scintela.cliente_mail_asinfo" in sql, (
        "el mail de Asinfo sale de la tabla espejo que refresca el cron; "
        "consultar Metabase en cada carga de pantalla sería inaceptable"
    )
    assert "LEFT JOIN" in sql, (
        "LEFT, no INNER: un cliente que no está en Asinfo, o con la tabla "
        "espejo vacía, tiene que seguir abriendo su estado de cuenta"
    )


def test_el_cruce_con_asinfo_es_por_los_10_digitos_del_ruc():
    sql = _sql_cliente()
    assert "ma.ruc10 = LEFT(regexp_replace" in sql
    assert ">= 10" in sql, (
        "un RUC corto no puede cruzar: un prefijo de 6 dígitos aparearía "
        "clientes al azar"
    )


def test_la_vista_no_arma_la_regla_del_mail_a_mano():
    """Si el Jinja empieza a decidir, la regla deja de estar testeada."""
    html = _TPL.read_text(encoding="utf-8")
    assert "data.cliente.mail" in html, "el mail ya viene resuelto de Python"
    assert "regexp" not in html.lower()


def test_la_tabla_espejo_se_bootstrapea_antes_de_consultar():
    """El deploy NO corre migraciones: si la tabla no existiera, el LEFT JOIN
    tiraría abajo la pantalla entera con 'relation does not exist'."""
    src = _src_estado_cuenta()
    i_boot = src.index("asegurar_tabla()")
    i_sql = src.index("FROM scintela.cliente c")
    assert i_boot < i_sql


def test_el_mail_es_clickeable():
    html = _TPL.read_text(encoding="utf-8")
    assert 'href="mailto:{{ _mail }}"' in html, (
        "el mail tiene que abrir el cliente de correo de un click"
    )


def test_el_bloque_aparece_aunque_solo_haya_vendedor_o_mail():
    """El `{% if %}` de guarda no puede seguir mirando sólo dirección/RUC/tel.

    Si no, un cliente al que sólo le cargaron el mail no lo ve nunca.
    """
    html = _TPL.read_text(encoding="utf-8")
    i = html.index("Dirección (2)")
    guarda = html[html.rfind("{% if", 0, html.rfind('class="no-print mb-5', 0, i)):i]
    assert "_vend" in guarda and "_mail" in guarda, (
        "la condición que muestra el bloque de contacto ignora vendedor/mail"
    )
