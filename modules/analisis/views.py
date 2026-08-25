"""Sección Análisis — su propio menú, aparte del programa del día a día."""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

import db
from auth import requiere_login, requiere_permiso
from exports import csv_response
from filters import today_ec
from modules._lib import pdf_motor

from . import asinfo_parado, queries

analisis_bp = Blueprint("analisis", __name__, template_folder="templates")

_LOG = logging.getLogger("programa_core.analisis")

# El menú de la sección. Vive acá y no en el template para que agregar una
# pantalla sea una línea y no un copy-paste de HTML en dos lugares.
# Los 7 códigos que en Programa Core apuntan a DOS empresas distintas
# (migración 0155, lista abierta). Ahí el código no identifica a nadie y hay que
# mirar el RUC antes de llamar.
CODIGOS_AMBIGUOS = {"BLP", "BRC", "JQS", "LEC", "YMO", "JRP", "MTE"}

MENU = [
    {
        "url": "/analisis/parado",
        "titulo": "Saldos",
        "bajada": "Qué está quieto en bodega y a qué cliente llamarle por eso.",
        "listo": True,
    },
    {
        "url": "/analisis/parado/clientes",
        "titulo": "A quién ofrecerle qué",
        "bajada": "La hoja del vendedor: cliente por cliente, qué saldos "
                  "puede llevarse. Se imprime.",
        "listo": True,
    },
    {
        "url": "/analisis/competencia",
        "titulo": "Competencia",
        "bajada": "Cuánto queda de lo parado y quién va ganando. La ven todos, "
                  "vendedores incluidos.",
        "listo": True,
    },
    {
        "url": "/analisis/clientes",
        "titulo": "Clientes",
        "bajada": "Quién se movió, quién dejó de comprar, quién se está enfriando.",
        "listo": False,
    },
]


#: El menú que ve un VENDEDOR. Es otro, y no un subconjunto del de la oficina:
#: sus pantallas son otras rutas (las de /analisis/competencia/…). Mostrarle el
#: de la oficina sería darle tres links que le dan 404.
MENU_VENDEDOR = [
    {"url": "/analisis/competencia", "titulo": "Competencia", "listo": True},
    {"url": "/analisis/competencia/telas", "titulo": "Saldos",
     "listo": True},
    {"url": "/analisis/competencia/mi-hoja", "titulo": "Tu hoja", "listo": True},
]


@analisis_bp.context_processor
def _menu():
    """El menú de arriba lo ve todo template de la sección.

    Sólo las pantallas listas: un link a algo que todavía no existe da 404, y
    en esta app los links son strings hardcodeados — no se ve desde el código.
    """
    if (g.get("user") or {}).get("vend"):
        return {"menu_global": MENU_VENDEDOR}
    return {"menu_global": [m for m in MENU if m["listo"]]}


@analisis_bp.route("/analisis")
@requiere_login
@requiere_permiso("analisis.ver")
def inicio():
    return render_template("analisis/inicio.html", menu=MENU)


@analisis_bp.route("/analisis/parado")
@requiere_login
@requiere_permiso("analisis.ver")
def parado():
    # ⚠ Dos listas y no una. `base` es una fila por tela × color: de ahí salen
    # el resumen de arriba y el cuadro por grupo, que tienen que seguir
    # contando ÍTEMS (734), no renglones. `filas` es lo que se dibuja, abierto
    # por forma y calidad — ver `abrir_en_lineas`.
    base = queries.con_puntos(queries.items())
    filas = queries.abrir_en_lineas(base)
    # ⭐ LO QUE YA NO TIENE UN KILO NO SE DIBUJA (dueña 25/08/2026: "los que hay
    # 0 no tienen que estar"). El ítem que se vendió entero se queda en la
    # cohorte —"si empezamos a venderlas, que no se nos vayan de la lista"— y
    # sigue contando en el resumen y en la competencia, que salen de `base`.
    # Pero en la LISTA era un renglón de 0 kg, 0 puntos y grupo "—": no hay
    # nada que ofrecer ahí. Lo que se vendió tiene su propia tabla en el pie.
    filas = [f for f in filas if float(f.get("stock_kg") or 0) > 0]
    filas.sort(key=lambda f: -float(f.get("puntos_fila") or 0))
    # Los desplegables salen de las filas que se van a dibujar, no de una lista
    # aparte: si un grupo no tiene nada parado, no tiene por qué estar.
    grupos = sorted({f["categoria"] for f in filas if f["categoria"]})
    subgrupos = sorted(
        {(f["subcategoria"], f["categoria"] or "") for f in filas},
        key=lambda x: x[0])
    return render_template(
        "analisis/parado.html",
        filas=filas,
        grupos=grupos,
        subgrupos=[{"sub": s, "cat": c} for s, c in subgrupos],
        llamados=queries.llamados_por_tela(),
        resumen=queries.resumen(base, queries.kg_al_arrancar()),
        bolsa=queries.bolsa_congelada(),
        grupos_resumen=queries.por_grupo(base),
        estado=queries.estado(),
        # ⭐ La tabla de abajo: qué se vendió, qué día y quién.
        vendidos=queries.vendidos(queries.config("largada", "2026-08-25")),
        # ⚠ El texto de la pantalla sale de la MISMA constante que el filtro:
        # si un día se cambian los días, la explicación no puede quedar
        # diciendo otra cosa.
        dias_quieto=asinfo_parado.DIAS_QUIETO,
        codigos_ambiguos=CODIGOS_AMBIGUOS,
        ahora_anio=today_ec().year,
    )


@analisis_bp.route("/analisis/parado.csv")
@requiere_login
@requiere_permiso("analisis.ver")
def parado_csv():
    """
    La lista entera a Excel.

    ⭐ Baja SIEMPRE todo, sin aplicar los filtros de la pantalla. Los filtros de
    arriba son de JavaScript: replicarlos acá sería escribir la misma regla dos
    veces en dos lenguajes, y el día que una cambie el archivo va a decir algo
    distinto de la pantalla sin que nadie se entere. Además, el motivo por el
    que uno baja algo a Excel es justamente filtrarlo y pivotearlo ahí — el
    archivo trae GRUPO y SUBGRUPO como columnas para eso. El botón lo dice.
    """
    return csv_response(
        queries.con_puntos(queries.items()),
        columnas=[
            ("categoria", "Grupo"),
            ("subcategoria", "Subgrupo (tela)"),
            ("color", "Color"),
            ("color_nombre", "Nombre del color"),
            ("stock_kg", "Kg en saldo"),
            ("nivel", "Nivel"),
            ("puntos", "Puntos por kilo"),
            ("puntos_fila", "Puntos de la fila"),
            ("pct_total", "% del saldo"),
            ("kg_primera", "Kg de primera"),
            ("kg_segunda", "Kg de segunda"),
            ("kg_al_marcar", "Kg al marcarlo"),
            ("kg_vendidos", "Kg vendidos desde entonces"),
            ("estado", "Estado"),
            ("clientes", "Clientes que compran esta tela"),
            ("anio_pista", "Año de esos clientes"),
            ("ultima_venta", "Última venta"),
            ("fecha_marcado", "Marcado el"),
        ],
        filename="saldos.csv",
    )


@analisis_bp.route("/analisis/parado/clientes")
@requiere_login
@requiere_permiso("analisis.ver")
def parado_clientes():
    """
    La hoja del vendedor: qué telas ofrecerle a qué cliente.

    ⭐ Es la MISMA página en pantalla y en papel. `@media print` esconde el
    cascarón y listo. Dos plantillas para el mismo documento divergen a la
    primera corrección que se le hace a una sola — ya pasó en /mi-cartera, donde
    la oficina y el portal imprimieron órdenes distintos durante ocho días sin
    que nadie lo notara.

    ⭐ Y por eso el ORDEN viaja en la URL: lo que se ve es lo que se imprime.
    """
    vend = (request.args.get("vend") or "").strip().upper() or None
    orden = request.args.get("orden") or "codigo"
    if orden not in queries.ORDENES:
        orden = "oportunidad"
    return render_template(
        "analisis/parado_clientes.html",
        vend=vend, ordenes=queries.ORDENES,
        estado=queries.estado(),
        codigos_ambiguos=CODIGOS_AMBIGUOS,
        **queries.por_cliente(vend, orden),
    )


@analisis_bp.route("/analisis/competencia/telas.csv")
@requiere_login
def mis_telas_csv():
    """Las telas paradas a Excel. Sin clientes: es la lista de la fábrica."""
    return csv_response(
        queries.items(),
        columnas=[
            ("categoria", "Grupo"),
            ("subcategoria", "Subgrupo (tela)"),
            ("color", "Color"),
            ("stock_kg", "Kg en saldo"),
            ("pct_total", "% del saldo"),
            ("kg_primera", "Kg de primera"),
            ("kg_segunda", "Kg de segunda"),
            ("ultima_venta", "Última venta"),
        ],
        filename="saldos.csv",
    )


@analisis_bp.route("/analisis/competencia/mi-hoja.csv")
@requiere_login
def mi_hoja_csv():
    """La hoja propia a Excel. Mismo recorte por cartera que la pantalla."""
    vend = _vend_actual()
    orden = request.args.get("orden") or "codigo"
    if orden not in queries.ORDENES:
        orden = "oportunidad"
    return csv_response(
        queries.por_cliente_plano(None, orden, cartera_de=vend),
        columnas=[
            ("tipo", "Candidato o improbable"),
            ("orden_en_la_hoja", "Orden en la hoja"),
            ("codigo", "Código"),
            ("nombre", "Cliente"),
            ("provincia", "Provincia"),
            ("subcategoria", "Tela"),
            ("colores_parados", "Colores en saldo"),
            ("puntos", "Puntos por kilo"),
            ("kg_parado", "Kg en saldo de esa tela"),
            ("kg_cliente", "Kg que le vendimos"),
            ("ultima_compra", "Última compra"),
        ],
        filename=f"mi_hoja{'_' + vend if vend else ''}.csv",
    )


@analisis_bp.route("/analisis/parado/clientes.csv")
@requiere_login
@requiere_permiso("analisis.ver")
def parado_clientes_csv():
    """
    La hoja del vendedor a Excel — una fila por cliente × tela.

    Acá el filtro por vendedor SÍ viaja, porque no es un filtro de JavaScript:
    ya está en la URL de la pantalla y lo aplica la misma función que la dibuja
    (`por_cliente`). El orden también, para que el archivo salga en el mismo
    orden que el papel.
    """
    vend = (request.args.get("vend") or "").strip().upper() or None
    orden = request.args.get("orden") or "codigo"
    if orden not in queries.ORDENES:
        orden = "oportunidad"
    return csv_response(
        queries.por_cliente_plano(vend, orden),
        columnas=[
            ("tipo", "Candidato o improbable"),
            ("orden_en_la_hoja", "Orden en la hoja"),
            ("codigo", "Código"),
            ("nombre", "Cliente"),
            ("provincia", "Provincia"),
            ("vendedor", "Vendedor"),
            ("kg_potencial", "Kg en saldo de telas que compra"),
            ("subcategoria", "Tela"),
            ("colores_parados", "Colores en saldo"),
            ("puntos", "Puntos por kilo"),
            ("kg_parado", "Kg en saldo de esa tela"),
            ("kg_cliente", "Kg que le vendimos"),
            ("ultima_compra", "Última compra"),
            ("anio", "Año"),
        ],
        filename=f"a_quien_ofrecerle_que{'_' + vend if vend else ''}.csv",
    )


@analisis_bp.route("/analisis/competencia")
@requiere_login
def competencia():
    """
    El tablero de la competencia. Dueña 17/08/2026: "aca tienen acceso todos,
    vendedores sobre todo incluidos".

    ⭐ SIN `@requiere_permiso` a propósito: "todos" acá quiere decir cualquiera
    que entre al programa. Es la única ruta de la sección sin gate, y es
    deliberado — no un descuido. Sólo lee y no muestra plata: kilos, metas y
    puestos.

    ⚠ No alcanza con sacar el gate: un usuario VENDEDOR está encerrado en
    /mi-cartera por `scope_vendedor.py` y cualquier otra ruta le da 404. La
    apertura de verdad está allá, en PREFIJOS_PERMITIDOS.
    """
    # ⭐ El bloque "lo tuyo" sale del vendedor del USUARIO LOGUEADO, nunca del
    # querystring: si viniera de la URL, cualquiera vería la cartera de
    # cualquiera cambiando tres letras. Mismo criterio que /mi-cartera.
    vend = _vend_actual()
    datos = queries.competencia()
    return render_template(
        "analisis/competencia.html",
        vend=vend,
        telas=queries.telas_a_sacar(queries.items(), queries.puntos_por_tela()),
        mis_clientes=queries.mis_clientes_parado(vend) if vend else [],
        **datos)


@analisis_bp.route("/analisis/competencia/telas")
@requiere_login
def mis_telas():
    """
    "Lo parado" para el vendedor: LA MISMA pantalla, con sus clientes adentro.

    Dueña 17/08/2026: "la tab de que hay que sacar copiala para ellos. y que
    vean con sus clientes… estaba linda diseñada".

    ⭐ Es el mismo template que /analisis/parado. Lo único que cambia es de
    dónde salen los candidatos —su cartera en vez de todas— y la columna
    "clientes", que pasa a contar los SUYOS: dejarla con el total de la fábrica
    diría "137 clientes" y al abrir la fila aparecerían tres.
    """
    vend = _vend_actual()
    base = queries.con_puntos(queries.items())
    llamados = queries.llamados_por_tela(cartera_de=vend)
    for f in base:
        f["clientes"] = len(llamados.get(f["subcategoria"], []))
    filas = queries.abrir_en_lineas(base)
    # ⭐ LO QUE YA NO TIENE UN KILO NO SE DIBUJA (dueña 25/08/2026: "los que hay
    # 0 no tienen que estar"). El ítem que se vendió entero se queda en la
    # cohorte —"si empezamos a venderlas, que no se nos vayan de la lista"— y
    # sigue contando en el resumen y en la competencia, que salen de `base`.
    # Pero en la LISTA era un renglón de 0 kg, 0 puntos y grupo "—": no hay
    # nada que ofrecer ahí. Lo que se vendió tiene su propia tabla en el pie.
    filas = [f for f in filas if float(f.get("stock_kg") or 0) > 0]
    filas.sort(key=lambda f: -float(f.get("puntos_fila") or 0))
    grupos = sorted({f["categoria"] for f in filas if f["categoria"]})
    subgrupos = sorted({(f["subcategoria"], f["categoria"] or "") for f in filas},
                       key=lambda x: x[0])
    return render_template(
        "analisis/parado.html",
        filas=filas, llamados=llamados, mia=True, vend=vend,
        grupos=grupos,
        subgrupos=[{"sub": x, "cat": c} for x, c in subgrupos],
        resumen=queries.resumen(base, queries.kg_al_arrancar()),
        bolsa=queries.bolsa_congelada(),
        grupos_resumen=queries.por_grupo(base),
        estado=queries.estado(),
        # ⭐ La tabla de abajo: qué se vendió, qué día y quién.
        vendidos=queries.vendidos(queries.config("largada", "2026-08-25")),
        codigos_ambiguos=CODIGOS_AMBIGUOS,
        ahora_anio=today_ec().year,
    )


def _hoja_saldos() -> dict:
    """Los datos de la lista impresa: una fila por tela × color, con stock.

    ⭐ Dueña 25/08/2026: *"quiero una lista en pdf para imprimir todas las
    telas"*, tela y color.

    Va ordenada por TELA y adentro por kilos, y no por puntos como la pantalla:
    en papel no se filtra ni se busca — se busca con el dedo, y para eso el
    orden tiene que ser alfabético. La pantalla sigue ordenada por puntos, que
    es donde sí se decide a qué ir.
    """
    filas = [f for f in queries.con_puntos(queries.items())
             if float(f["stock_kg"] or 0) > 0]
    filas = queries.abrir_en_lineas(filas)
    filas.sort(key=lambda f: (f["subcategoria"].lower(), -float(f["stock_kg"] or 0)))
    bloques: list[tuple[str, list[dict]]] = []
    for f in filas:
        if not bloques or bloques[-1][0] != f["subcategoria"]:
            bloques.append((f["subcategoria"], []))
        bloques[-1][1].append(f)
    return {
        "filas": filas,
        "bloques": bloques,
        "kg_total": sum(float(f["stock_kg"] or 0) for f in filas),
        "puntos_total": sum(float(f.get("puntos_fila") or 0) for f in filas),
        "estado": queries.estado(),
    }


@analisis_bp.route("/analisis/competencia/telas/imprimir")
@requiere_login
def saldos_imprimir():
    """La lista entera, lista para imprimir. La misma hoja que sale en PDF.

    ⚠ Cuelga de /analisis/competencia a propósito: así la ven también los
    vendedores, que tienen ese prefijo abierto. No lleva un solo cliente
    adentro — son telas, colores y kilos de la fábrica.
    """
    return render_template("analisis/parado_impreso.html", **_hoja_saldos())


@analisis_bp.route("/analisis/competencia/telas/imprimir.pdf")
@requiere_login
def saldos_imprimir_pdf():
    """La misma hoja, en PDF, para mandarla o guardarla.

    Sale del MISMO HTML que la pantalla de arriba (ver `pdf_motor`): no hay una
    segunda plantilla que se despegue a la primera corrección.
    """
    html = render_template("analisis/parado_impreso.html", **_hoja_saldos())
    try:
        blob = pdf_motor.desde_html(html)
    except pdf_motor.SinMotor as e:
        current_app.logger.error("PDF de saldos: %s", e)
        return Response(
            "No se puede generar el PDF: el servidor no tiene un navegador "
            "instalado para imprimirlo. La pantalla de impresión sigue "
            "funcionando normalmente.",
            status=503, mimetype="text/plain; charset=utf-8")
    nombre = f"Saldos {today_ec().strftime('%d-%m-%Y')}.pdf"
    return Response(blob, mimetype="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{nombre}"',
        "Cache-Control": "no-store"})


@analisis_bp.route("/analisis/competencia/mi-hoja")
@requiere_login
def mi_hoja():
    """
    La hoja del vendedor: sus clientes y las telas paradas que puede ofrecerles.

    ⭐ Es LA MISMA plantilla que usa la oficina en /analisis/parado/clientes,
    con los datos recortados a su cartera. Dos plantillas para el mismo papel
    divergen a la primera corrección que se le hace a una sola — ya pasó en
    /mi-cartera, donde la oficina y el portal imprimieron órdenes distintos ocho
    días sin que nadie lo notara.

    ⚠ Cuelga de /analisis/competencia/ para que entre en el mismo permiso del
    allowlist el día que se les habilite. La ruta general de la hoja
    (/analisis/parado/clientes) NO se abre nunca: tiene los clientes de todos.
    """
    vend = _vend_actual()
    orden = request.args.get("orden") or "codigo"
    if orden not in queries.ORDENES:
        orden = "oportunidad"
    return render_template(
        "analisis/parado_clientes.html",
        vend=vend, mia=True, ordenes=queries.ORDENES,
        estado=queries.estado(),
        codigos_ambiguos=CODIGOS_AMBIGUOS,
        **queries.por_cliente(None, orden, cartera_de=vend))


def _vend_actual() -> str | None:
    """
    El código de vendedor del usuario logueado.

    ⭐ Nunca del querystring: si viniera de la URL, cualquiera vería la cartera
    de cualquiera cambiando tres letras. La ÚNICA excepción es un usuario
    wildcard (la dueña, Andrés), que puede pasar `?vend=` para previsualizar lo
    que ve un vendedor sin pedirle la contraseña a nadie — mismo mecanismo que
    /mi-cartera. Si el que lo manda ES vendedor, gana el suyo.
    """
    propio = (g.user or {}).get("vend")
    if propio:
        return propio
    if "*" in (g.get("permisos") or set()):
        return (request.args.get("vend") or "").strip().upper() or None
    return None


def _numero(texto: str | None, malos: list[str], donde: str) -> float | None:
    """Un % escrito a mano. Devuelve None si está vacío o no se entiende, y en
    el segundo caso lo anota en `malos` para que la pantalla lo diga."""
    t = (texto or "").strip().replace(",", ".")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        malos.append(donde)
        return None


@analisis_bp.route("/analisis/metas", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("analisis.ver")
def competencia_metas():
    """
    Pisar a mano la meta automática de un grupo. Sólo la dueña y Andrés.

    ⭐ Cuelga de `/analisis/metas` y NO de `/analisis/competencia/metas` a
    propósito. El allowlist de vendedores matchea por segmento
    (`path == p or path.startswith(p + "/")`), así que cualquier cosa colgada de
    `/analisis/competencia/` les quedaría abierta y la única defensa sería el
    permiso. Con la ruta afuera son dos cierres en vez de uno.
    """
    if request.method == "POST":
        quien = (g.user or {}).get("username")
        # ⚠ Lo que no se entiende NO se ignora en silencio: se junta y se avisa.
        # Un "Metas guardadas" sobre un campo que se descartó es la peor de las
        # respuestas — la dueña se va convencida de que cambió algo.
        malos: list[str] = []

        total = _numero(request.form.get("meta_total_pct"), malos, "el total")
        if total is not None:
            db.execute(
                """INSERT INTO scintela.parado_config (clave, valor, quien)
                   VALUES ('meta_total_pct', %s, %s)
                   ON CONFLICT (clave) DO UPDATE
                      SET valor = excluded.valor, actualizado = NOW(),
                          quien = excluded.quien""",
                (str(total), quien))

        for clave, valor in request.form.items():
            if not clave.startswith("meta_") or clave == "meta_total_pct":
                continue
            grupo = clave[5:]
            if not (valor or "").strip():
                # vacío = volver al automático. Guardar un 0 sería otra cosa:
                # "este grupo no tiene meta", que no es lo que quiso decir.
                db.execute("DELETE FROM scintela.parado_meta WHERE categoria = %s",
                           (grupo,))
                continue
            pct = _numero(valor, malos, grupo)
            if pct is None:
                continue
            db.execute(
                """INSERT INTO scintela.parado_meta (categoria, pct, quien)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (categoria) DO UPDATE
                      SET pct = excluded.pct, actualizado = NOW(),
                          quien = excluded.quien""",
                (grupo, pct, quien))

        if malos:
            flash("Guardé lo demás, pero no entendí lo que pusiste en: "
                  + ", ".join(malos) + ".", "error")
        else:
            flash("Metas guardadas.", "success")
        return redirect(url_for("analisis.competencia"))
    return render_template("analisis/competencia_metas.html", **queries.competencia())


@analisis_bp.route("/analisis/parado/actualizar", methods=["POST"])
@requiere_login
@requiere_permiso("analisis.ver")
def parado_actualizar():
    """Trae todo de Asinfo. Tarda ~10 s: por eso es un botón y no pasa al abrir."""
    try:
        r = queries.actualizar()
        flash(f"Actualizado: {r['items']} telas en la lista, "
              f"{r['llamados']} candidatos.", "success")
    except Exception as e:                       # noqa: BLE001
        # Fail-closed: si Asinfo no contesta, la pantalla sigue mostrando la
        # foto vieja CON su fecha, que es información. Vaciarla no lo es.
        _LOG.warning("Análisis/parado: no se pudo actualizar: %s", e)
        flash(f"No se pudo actualizar: {e}. Se sigue viendo la foto anterior.",
              "error")
    return redirect(url_for("analisis.parado"))
