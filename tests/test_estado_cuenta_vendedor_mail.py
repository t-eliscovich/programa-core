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
    # TMT 2026-08-26: las seis consultas se mudaron a `estado_cuenta_lote`
    # (una sola vez para todos los clientes en vez de 7 por cliente). El de
    # UNO ahora es un envoltorio de ése, así que el SQL a leer está acá.
    return inspect.getsource(iq.estado_cuenta_lote)


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


def _grilla() -> str:
    """El bloque `.ec-datos` de la ficha — los datos del cliente."""
    html = _TPL.read_text(encoding="utf-8")
    abre = '<div class="ec-datos">'
    i = html.index(abre) + len(abre)
    return html[i:html.index("{# — Los cuatro números", i)]


def test_vendedor_y_mail_estan_en_el_mismo_bloque_que_la_direccion():
    """El pedido del 03/08 era *"en otra columna, no quiero que tanto espacio
    nos quite información"*: Vendedor y Mail tenían que aprovechar el hueco de
    la derecha y NO colgarse debajo de la dirección alargando el bloque.

    El 04/08 el header se rediseñó y este bloque terminó volviendo al formato
    compacto de siempre (dueña: *"me gusta más esta parte de la antigua"*),
    dentro de la ficha. Sigue siendo de dos columnas y Vendedor/Mail siguen en
    la segunda — que es lo que este test protege.
    """
    grilla = _grilla()
    for campo in ("Dirección:", "Vendedor:", "Mail:"):
        assert f'<span class="ec-lbl">{campo}</span>' in grilla, f"«{campo}» salió del bloque"
    assert "grid-template-columns: repeat(2" in _TPL.read_text(encoding="utf-8"), (
        "el bloque de contacto dejó de ser de dos columnas"
    )
    # Vendedor y Mail van en la MISMA columna (la segunda), no en un apéndice.
    i_col2 = grilla.index('>Tel:</span>')
    assert grilla.index('>Vendedor:</span>') > i_col2
    assert grilla.index('>Mail:</span>') > i_col2


def test_el_mail_va_pegado_al_telefono():
    """Dueña 04/08: *"mail y teléfono deberían estar más juntos"*. El orden sale
    del Directorio de contactos (`clientes/contactos.html`), donde son columnas
    vecinas: se leen juntos porque se usan juntos."""
    grilla = _grilla()
    assert grilla.index('>Tel:</span>') < grilla.index('>Mail:</span>')


def test_las_dos_direcciones_del_dBase_se_siguen_mostrando():
    """El dBase guarda la dirección en DOS renglones (direccion1 + direccion2).
    Las DOS tienen que salir: la segunda suele traer la referencia de entrega."""
    grilla = _grilla()
    assert "_d1" in grilla and "_d2" in grilla
    assert '<span class="ec-lbl">Dirección (2):</span>' in grilla


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


def test_vendedor_y_mail_se_muestran_siempre_aunque_esten_vacios():
    """Antes el bloque entero colgaba de un `{% if %}` que miraba dirección /
    RUC / tel: a un cliente al que sólo le cargaron el mail no se lo veía nunca.
    Y en la primera versión del rediseño los campos volvían a esconderse solos.

    Ahora cada campo se dibuja siempre; sin dato muestra "—". Que el hueco se
    VEA es la mitad del punto: el 03/08 había 1 solo cliente con correo cargado
    de 3.973, y el "—" es lo que hace obvio que falta cargarlo desde "Editar
    cliente".
    """
    grilla = _grilla()
    for campo in ("Vendedor:", "Mail:"):
        antes = grilla[:grilla.index(f'>{campo}</span>')]
        assert antes.count("{% if") == antes.count("{% endif %}"), (
            f"{campo} quedó dentro de un {{% if %}}: un cliente sin ese dato "
            f"deja de ver el renglón y no se entera de que falta cargarlo"
        )
        resto = grilla[grilla.index(f'>{campo}</span>'):]
        assert "—" in resto[:resto.index("</div>")], f"{campo} sin dato tiene que mostrar —"
