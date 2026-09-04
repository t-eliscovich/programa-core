"""Las pantallas del portal del cliente.

Un solo camino, sin ramas (`PLAN_PORTAL_CLIENTE_2026_08_24.md`):

    /ingresar        RUC + clave — la primera vez, 6 números al correo
    /codigo          los 6 números que llegaron por correo
    /elegir-clave    elige su clave y confirma su correo
    /mis-cuentas     si su RUC cae en dos fichas, cuál está mirando
    /                lo que ve adentro
    /salir

⭐ **El usuario es el RUC** (TMT 26/08/2026: *"los clientes no se saben su
código de cliente"*). El código de 3 letras sigue entrando por el mismo campo
para el que lo sepa. El porqué, medido, está en `acceso.py`.

⚠ La sesión del cliente vive en una llave PROPIA (`portal_cliente`), separada
de la de los empleados (`user_id`). No se mezclan a propósito: en este proceso
el login de empleados ni siquiera existe.
"""
from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from markupsafe import Markup

from extensions import limiter
from filters import today_ec
from modules.portal import acceso

portal_bp = Blueprint("portal", __name__, url_prefix="",
                      template_folder="templates")

#: La llave de la sesión del cliente. Distinta de `user_id`, que es la de los
#: empleados: que un portal público comparta llave con el ERP es justo lo que
#: este proceso vino a evitar.
LLAVE = "portal_cliente"

#: Las cuentas que abrió el RUC con el que entró. Con una sola no se usa; con
#: dos es la lista contra la que se valida el cambio de cuenta, para que nadie
#: se pase a una que su RUC no abre escribiéndola en la URL.
CUENTAS = "portal_cuentas"


@portal_bp.app_context_processor
def _comunes():
    from flask_wtf.csrf import generate_csrf

    return {
        # ⚠ `hoy` es una FECHA, no un texto, y el nombre es el mismo que usa el
        # portal de vendedores a propósito: el parcial compartido
        # `mi_cartera/_movimientos.html` hace `vencimiento < hoy` para pintar
        # las vencidas. Con un string eso no da False — LEVANTA, y se cae la
        # pantalla entera. Para mostrarla, `{{ hoy|fecha_es }}`.
        "hoy": today_ec(),
        # Las cuentas que abrió su RUC. Con una sola nadie las mira; con dos,
        # el encabezado le ofrece cambiar. Ver `mis_cuentas`.
        "cuentas_del_cliente": session.get(CUENTAS) or [],
        "csrf_token_input": Markup(
            f'<input type="hidden" name="csrf_token" value="{generate_csrf()}">'),
    }


def cliente_actual() -> str:
    """El código del cliente logueado, o vacío."""
    return (session.get(LLAVE) or "").strip().upper()


def _pedir_entrar():
    return redirect(url_for("portal.ingresar"))


def _de_donde() -> tuple[str, str]:
    """IP y navegador, para la bitácora."""
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "")
            .split(",")[0].strip(),
            request.headers.get("User-Agent", ""))


# ---------------------------------------------------------------------------
# Entrar
# ---------------------------------------------------------------------------


@portal_bp.route("/ingresar", methods=["GET", "POST"])
@limiter.limit("20 per minute; 100 per hour", methods=["POST"])
def ingresar():
    if request.method == "GET":
        if cliente_actual():
            return redirect(url_for("portal.inicio"))
        return render_template("portal/ingreso.html", identificador="")

    quien = (request.form.get("identificador") or "").strip()
    clave = request.form.get("clave") or ""
    ip, navegador = _de_donde()
    r = acceso.entrar(quien, clave, ip=ip, navegador=navegador)

    if r["primera_vez"]:
        # Nunca eligió clave. No se le dice "clave mala": se le manda el código
        # al correo que ya teníamos y sigue por ahí. Es el mismo camino que
        # "olvidé mi clave", así que hay una sola puerta que cuidar.
        return _mandar_y_pedir_el_codigo(r["codigo_cli"], quien)

    if not r["ok"]:
        flash(r["mensaje"], "error")
        # El RUC se devuelve para no hacerlo escribir de nuevo; la clave no.
        return render_template("portal/ingreso.html", identificador=quien), 401

    return _adentro(r["codigo_cli"], r["cuentas"])


def _adentro(codigo_cli: str, cuentas: list):
    """Abre la sesión del cliente y lo manda a lo que vino a ver.

    Sesión nueva y limpia en cada ingreso: si quedaba algo de antes, se va.
    """
    session.clear()
    session[LLAVE] = codigo_cli
    session[CUENTAS] = [{"codigo_cli": c["codigo_cli"], "nombre": c.get("nombre") or ""}
                        for c in (cuentas or [])]
    session.permanent = True
    if len(session[CUENTAS]) > 1:
        # Su RUC cae en dos fichas: elige él cuál mira. Elegir por él sería
        # mostrarle media deuda sin decírselo.
        return redirect(url_for("portal.mis_cuentas"))
    return redirect(url_for("portal.inicio"))


def _mandar_y_pedir_el_codigo(codigo_cli: str, escrito: str):
    """Le manda los 6 números y muestra la pantalla donde los escribe."""
    seis, mail = acceso.pedir_codigo(codigo_cli)
    if seis:
        _mandar_el_codigo(seis, mail)
    flash(acceso.MANDADO, "ok")
    return render_template("portal/codigo.html", identificador=escrito,
                           minutos=acceso.MINUTOS_CODIGO)


@portal_bp.route("/elegir-clave", methods=["GET", "POST"])
def elegir_clave():
    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()

    fic = acceso.cliente(cod) or {}
    acc = acceso.acceso(cod) or {}
    # Si todavía no cargó ninguno, se le muestra el que usamos para mandarle
    # los 6 números: es el que tenemos, y ahí mismo lo corrige si es viejo.
    mail_previo = (acc.get("mail") or "").strip() or acceso.ultimo_mail_usado(cod)

    if request.method == "GET":
        return render_template("portal/elegir_clave.html",
                               nombre=fic.get("nombre") or cod,
                               mail=mail_previo)

    clave = request.form.get("clave") or ""
    repetir = request.form.get("repetir") or ""
    mail = request.form.get("mail") or ""
    mail2 = request.form.get("mail2") or ""

    def _volver(problema):
        flash(problema, "error")
        return render_template("portal/elegir_clave.html",
                               nombre=fic.get("nombre") or cod,
                               mail=mail or mail_previo), 400

    if clave != repetir:
        return _volver("Las dos claves no son iguales.")

    # El correo se comprueba ANTES de guardar la clave: si algo está mal, el
    # cliente vuelve a una pantalla que se comporta igual que la primera vez.
    ok, msg = acceso.mail_aceptable(mail, mail2)
    if not ok:
        return _volver(msg)

    ok, msg = acceso.guardar_clave(cod, clave)
    if not ok:
        return _volver(msg)

    acceso.guardar_mail(cod, mail, mail_previo)
    flash(msg, "ok")
    return redirect(url_for("portal.inicio"))


# ---------------------------------------------------------------------------
# Olvidé mi clave
# ---------------------------------------------------------------------------


@portal_bp.route("/olvide-la-clave", methods=["GET", "POST"])
@limiter.limit("6 per minute; 30 per hour", methods=["POST"])
def olvide_la_clave():
    if request.method == "GET":
        return render_template("portal/olvide.html", identificador="")

    quien = (request.form.get("identificador") or "").strip()
    cuentas = acceso.cuentas_de(quien)
    if cuentas:
        seis, mail = acceso.pedir_codigo(acceso.cuenta_con_la_clave(cuentas))
        if seis:
            _mandar_el_codigo(seis, mail)

    # ⭐ La MISMA respuesta exista o no el cliente, y tenga o no correo. Si
    # dijera "ese cliente no tiene correo", esta pantalla sería un buscador de
    # clientes reales.
    flash(acceso.MANDADO, "ok")
    return render_template("portal/codigo.html", identificador=quien,
                           minutos=acceso.MINUTOS_CODIGO)


def _mandar_el_codigo(seis: str, mail: str) -> None:
    """Manda el código por correo. Nunca rompe la pantalla.

    Si el mail no sale, el cliente ve el mismo mensaje de siempre y puede
    pedir otro. Decírselo sería decirle que ese código existe.
    """
    try:
        from modules._lib import mailer

        texto = (f"Su código para entrar al portal de Intela es: {seis}\n\n"
                 f"Vale por {acceso.MINUTOS_CODIGO} minutos. "
                 f"Si no lo pidió usted, ignore este mensaje.")
        mailer.enviar("Intela · su código para entrar", texto, [mail])
    except Exception:  # noqa: BLE001 -- el mail nunca tumba la pantalla
        pass


@portal_bp.route("/codigo", methods=["POST"])
@limiter.limit("10 per minute; 40 per hour")
def codigo():
    """El código de 6 números que llegó por correo.

    ⚠ Lo que viaja en la pantalla es lo que el cliente ESCRIBIÓ (su RUC), no el
    código de 3 letras: si viajara el nuestro, quien tipeara un RUC cualquiera
    lo leería en el HTML y la pantalla sería una tabla de equivalencias.
    """
    quien = (request.form.get("identificador") or "").strip()
    seis = request.form.get("seis") or ""
    ip, navegador = _de_donde()
    cuentas = acceso.cuentas_de(quien)
    cod = acceso.cuenta_con_la_clave(cuentas)

    if not cod or not acceso.usar_codigo(cod, seis):
        acceso.anotar(cod or acceso.normalizar_codigo(quien)[:40],
                      "codigo_malo", "codigo", ip, navegador)
        flash("Ese código no sirve. Puede estar vencido o ya usado.", "error")
        return render_template("portal/codigo.html", identificador=quien,
                               minutos=acceso.MINUTOS_CODIGO), 401

    acceso.anotar(cod, "ok", "codigo", ip, navegador)
    session.clear()
    session[LLAVE] = cod
    session[CUENTAS] = [{"codigo_cli": c["codigo_cli"], "nombre": c.get("nombre") or ""}
                        for c in cuentas]
    session.permanent = True
    # Entró con el correo, no con la clave: lo que sigue es elegir una nueva.
    flash("Elegí una clave nueva.", "ok")
    return redirect(url_for("portal.elegir_clave"))


@portal_bp.route("/mis-cuentas", methods=["GET", "POST"])
def mis_cuentas():
    """Cuál de sus cuentas está mirando, cuando su RUC cae en dos fichas.

    Un caso real y uno solo: AJO y AJ2, Almacenes José Puebla, la misma empresa
    cargada dos veces. Mostrarle una sola sería mostrarle media deuda.

    ⚠ Sólo se puede saltar a una cuenta que esté EN LA SESIÓN, o sea que la
    abrió el mismo RUC con el que entró. Escribir otro código en el formulario
    no lleva a ningún lado.
    """
    if not cliente_actual():
        return _pedir_entrar()
    cuentas = session.get(CUENTAS) or []

    if request.method == "POST":
        elegida = acceso.normalizar_codigo(request.form.get("codigo_cli") or "")
        if elegida not in {c["codigo_cli"] for c in cuentas}:
            return _pedir_entrar()
        session[LLAVE] = elegida
        return redirect(url_for("portal.inicio"))

    return render_template("portal/cuentas.html", cuentas=cuentas,
                           actual=cliente_actual())


@portal_bp.route("/salir", methods=["GET", "POST"])
def salir():
    session.clear()
    return _pedir_entrar()


# ---------------------------------------------------------------------------
# Adentro
# ---------------------------------------------------------------------------


def _cargar_estado_cuenta(cod: str) -> dict:
    """El estado de cuenta del cliente.

    ⭐ Sale de `informes.queries.estado_cuenta_cliente`, la MISMA función que
    usan la oficina y el portal de vendedores. El portal no calcula ni un
    número: si el saldo que ve el cliente saliera de otra cuenta, tarde o
    temprano diría algo distinto que el que ve la oficina — y el que llama por
    teléfono es él.

    El blueprint de informes no se registra en este proceso, pero el módulo se
    importa igual: lo que no existe acá son sus PANTALLAS.
    """
    from modules.informes import queries as _q

    return _q.estado_cuenta_cliente(cod)


@portal_bp.route("/estado-de-cuenta", methods=["GET"])
def estado_cuenta():
    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()
    data = _cargar_estado_cuenta(cod)
    # La pestaña. Sin esto el link "Cheques" de la tarjeta no hacía nada:
    # el parcial compartido lee `tab` y el portal no se lo pasaba
    # (04/09/2026, con AJT).
    tab = request.args.get("tab") or "facturas"
    return render_template("portal/estado_cuenta.html",
                           data=data, t=data.get("totales") or {},
                           codigo=cod, tab=tab, qv="",
                           # A dónde lleva el número (ver _movimientos.html).
                           factura_endpoint="portal.factura",
                           factura_args={})



def _elegir_factura(data: dict, numf: int) -> dict | None:
    """La factura `numf` DENTRO de la cuenta del cliente, con el desempate.

    ⭐ Primero por el NÚMERO COMPLETO (`?doc=`), que no se repite nunca; después
    por la PK interna (`?id=`, 04/09/2026: los documentos sin número completo
    pierden el `doc=` y el corto puede dar para dos en la misma cuenta); y al
    final por el número corto, que en 288 documentos vale CERO. La ficha y el
    papel eligen por la MISMA puerta: si no, el cliente imprime otro documento
    que el que está mirando.
    """
    facturas = data.get("facturas") or []
    doc = (request.args.get("doc") or "").strip()
    if doc:
        for f in facturas:
            if (f.get("numf_completo") or "").strip() == doc:
                return f
    _id = (request.args.get("id") or "").strip()
    if _id.isdigit():
        for f in facturas:
            if int(f.get("id_factura") or 0) == int(_id):
                return f
    for f in facturas:
        try:
            if int(f.get("numf") or 0) == int(numf):
                return f
        except (TypeError, ValueError):
            continue
    return None


@portal_bp.route("/factura/<int:numf>", methods=["GET"])
def factura(numf: int):
    """Qué se llevó el cliente en una de SUS facturas.

    TMT 2026-08-25 (dueña). El cuerpo es el mismo parcial que ve su vendedor:
    mismos números y mismas columnas. Si el cliente y el vendedor vieran dos
    detalles distintos de la misma factura, la discusión no se puede tener.

    El scope es el de siempre en el portal: la factura se busca DENTRO del
    estado de cuenta del cliente que está adentro. Un número ajeno da 404.
    """
    from flask import abort

    from modules.asinfo import factura_lineas

    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()
    data = _cargar_estado_cuenta(cod)
    # ⭐ Primero por el NÚMERO COMPLETO, que no se repite nunca. `numf` es el
    # corto: se repite y en 288 documentos vale CERO (casi todos notas de
    # entrega). Buscando sólo por él, el cliente abría el PRIMER documento de
    # su cuenta con ese número — y podía ser otro. El vendedor mira la MISMA
    # factura por la misma puerta (26/08/2026).
    elegida = _elegir_factura(data, numf)
    if elegida is None:
        abort(404)
    numero = (elegida.get("numf_completo") or "").split("-")[-1].lstrip("0") or str(numf)
    return render_template(
        "portal/factura.html", f=elegida, numero=numero, codigo=cod,
        det=factura_lineas.que_se_llevo(elegida.get("numf_completo")))


@portal_bp.route("/factura/<int:numf>/papel", methods=["GET"])
def factura_papel_cliente(numf: int):
    """La factura en papel, la misma que tiene su vendedor y la oficina.

    El scope es el de siempre en el portal: la factura se busca DENTRO del
    estado de cuenta del cliente que está adentro. Un número ajeno da 404.
    """
    from flask import abort

    from modules.asinfo import factura_papel

    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()
    data = _cargar_estado_cuenta(cod)
    f = _elegir_factura(data, numf)
    if f is None:
        abort(404)
    return render_template(
        "informes/factura_papel.html",
        **factura_papel.hoja(f.get("numf_completo")), numero=numf)


@portal_bp.route("/mis-pagos", methods=["GET"])
def mis_pagos():
    """Lo que le recibimos: un recibo, no la máquina de estados.

    ⭐ TMT 26/08: *"no mostremos tanto detalle, sólo fecha y recibido"*. El
    recorrido del cheque —postergado, depositado, endosado, devuelto— es
    trabajo nuestro; contarlo abre preguntas que él no hizo. Lo que le importa
    de la plata está en su estado de cuenta, que es donde se discute el saldo.

    En el estado de cuenta los pagos aparecen sólo mientras siguen EN CARTERA:
    una vez depositados desaparecen de la pestaña. Acá están todos, que es lo
    que contesta *"¿les llegó lo que les dejé?"*.

    No hay una consulta nueva: sale de `estado_cuenta_cliente`, la misma
    función que usa la oficina.
    """
    from modules.cheques import estados

    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()

    data = _cargar_estado_cuenta(cod)
    pagos = [{**c, "que_es": _que_es(c)}
             for c in (data.get("cheques") or [])
             if estados.se_le_muestra_al_cliente(c.get("stat"))]
    # Lo último primero, POR LA FECHA QUE SE MUESTRA. Antes era un
    # `reverse()` del orden de la consulta (fecha del cheque / fecha de
    # salida), y en pantalla la columna "Recibido" salía desordenada:
    # 01/09, 01/09, 21/07, 01/09… (04/09/2026, con AJT).
    pagos.sort(key=_dia_recibido, reverse=True)

    return render_template("portal/pagos.html", codigo=cod, pagos=pagos,
                           cliente=data.get("cliente") or {})


def _dia_recibido(c: dict):
    """La misma fecha que muestra la columna "Recibido" de `pagos.html`."""
    from datetime import date, datetime
    v = c.get("dia_ingreso") or c.get("fecha_recibido") or c.get("fecha")
    if isinstance(v, datetime):
        v = v.date()
    if not isinstance(v, date):
        v = date.min
    # Mismo día: el cargado más tarde, primero.
    return (v, c.get("id_cheque") or 0)


#: Los "bancos" que no son bancos: 99 es efectivo y 90/91 son los depósitos
#: directos (DEP.PICH.). Ver el skill de cobranza — en la tabla de cheques
#: viven las tres cosas.
BANCO_EFECTIVO = 99
BANCOS_DEPOSITO = (90, 91)


def _que_es(c: dict) -> str:
    """Cheque, depósito o efectivo. Los tres viven en la misma tabla."""
    try:
        no_banco = int(c.get("no_banco") or 0)
    except (TypeError, ValueError):
        no_banco = 0
    if no_banco == BANCO_EFECTIVO:
        return "Efectivo"
    if no_banco in BANCOS_DEPOSITO:
        return "Depósito"
    return "Cheque"


@portal_bp.route("/despachos", methods=["GET"])
def despachos():
    """Qué le mandamos y cuándo. La mercadería, al lado de su saldo.

    Los datos salen de Asinfo por el puente de siempre — Programa Core guarda
    la plata de la factura, no la mercadería. Si Asinfo no contesta, la
    pantalla lo dice y no se cae: ver `despachos_cliente`.
    """
    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()

    from modules.asinfo import despachos_cliente

    try:
        meses = int(request.args.get("meses") or despachos_cliente.MESES_DEFAULT)
    except (TypeError, ValueError):
        meses = despachos_cliente.MESES_DEFAULT
    # ⭐ El RUC va con el código: hay `nombre_comercial` repetidos en dos
    # empresas que son contribuyentes DISTINTOS (PRE, MCS). Ver la auditoría
    # del 26/08 en `despachos_cliente`.
    ruc = (acceso.cliente(cod) or {}).get("ruc") or ""
    return render_template("portal/despachos.html", codigo=cod,
                           d=despachos_cliente.de_cliente(cod, ruc, meses))


@portal_bp.route("/despacho/<numero>", methods=["GET"])
def despacho(numero: str):
    """Los rollos de UNA guía suya, agrupados por tela.

    ⚠ El código del cliente va adentro de la consulta, no se chequea después:
    los números de guía van uno atrás del otro, así que cambiarle un dígito a
    la URL tiene que dar 404 y no el despacho del vecino.
    """
    from flask import abort

    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()

    from modules.asinfo import despachos_cliente

    ruc = (acceso.cliente(cod) or {}).get("ruc") or ""
    g = despachos_cliente.guia(cod, ruc, numero)
    if g["ok"] and not g["existe"]:
        abort(404)
    return render_template("portal/despacho.html", g=g, codigo=cod)


@portal_bp.route("/estado-de-cuenta/imprimir", methods=["GET"])
def estado_cuenta_imprimir():
    """La hoja para imprimir — la MISMA que sale de la oficina.

    Incluye `informes/_estado_cuenta_impreso.html`, el parcial compartido. El
    envoltorio sí es propio: el de la oficina extiende el chrome del ERP, con
    su menú y su breadcrumb, que acá no existen.
    """
    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()
    data = _cargar_estado_cuenta(cod)
    return render_template("portal/estado_cuenta_impreso.html",
                           data=data, t=data.get("totales") or {},
                           facturas=data.get("facturas") or [],
                           cheques=data.get("cheques") or [],
                           codigo=cod)


@portal_bp.route("/estado-de-cuenta.pdf", methods=["GET"])
def estado_cuenta_pdf_():
    """El estado de cuenta como archivo, para guardarlo o mandarlo.

    Es el MISMO documento que descargan la oficina y el vendedor: el cuerpo
    sale del parcial compartido y el archivo se llama igual —
    `Estado de cuenta ATE 24-08-2026.pdf`, con el código y el día, para que
    cinco estados de cuenta en una carpeta se distingan sin abrirlos.

    ⚠ Lo único que NO se reusa es `estado_cuenta_pdf.generar()`, y por una
    razón concreta: esa función renderiza `informes/estado_cuenta_lote_print.html`,
    que extiende el chrome del ERP y llama a `url_for('informes.…')` — en este
    proceso esas rutas no existen y el PDF moriría con un BuildError. Igual que
    la página de 404. Así que se arma con el envoltorio del portal, que incluye
    el mismo parcial y copia la misma CSS de impresión (hay un test que compara
    las dos).
    """
    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()

    from flask import Response, current_app

    from modules._lib import pdf_motor
    from modules.informes import estado_cuenta_pdf

    data = _cargar_estado_cuenta(cod)
    html = render_template("portal/estado_cuenta_impreso.html",
                           data=data, t=data.get("totales") or {},
                           facturas=data.get("facturas") or [],
                           cheques=data.get("cheques") or [],
                           codigo=cod, para_pdf=True)
    try:
        blob = pdf_motor.desde_html(html)
    except pdf_motor.SinMotor as e:
        # El botón se esconde cuando no hay motor, pero alguien puede llegar
        # por la URL. Un mensaje que dice qué falta evita el "no anda el botón".
        current_app.logger.error("PDF del portal (%s): %s", cod, e)
        return Response(
            "No se puede generar el archivo en este momento. La hoja para "
            "imprimir sigue funcionando.",
            status=503, mimetype="text/plain; charset=utf-8")

    nombre = estado_cuenta_pdf.nombre_archivo(
        (data.get("cliente") or {}).get("nombre") or "", cod)
    return Response(blob, mimetype="application/pdf", headers={
        # `inline`: en el celular el que lo abre lo pasa al menú de compartir;
        # si alguien entra por la URL, que lo vea en vez de bajarlo a ciegas.
        "Content-Disposition": f'inline; filename="{nombre}"',
        "Cache-Control": "no-store",
    })


@portal_bp.route("/", methods=["GET"])
def inicio():
    cod = cliente_actual()
    if not cod:
        return _pedir_entrar()

    acc = acceso.acceso(cod)
    # Le cortaron el acceso mientras estaba adentro: se va en el próximo click.
    if acc and not acc.get("activo", True):
        session.clear()
        flash(acceso.CORTADO, "error")
        return _pedir_entrar()
    # El cliente se borró o le cambiaron el código: no se inventa una pantalla.
    fic = acceso.cliente(cod)
    if not fic:
        session.clear()
        return _pedir_entrar()

    # Todavía no eligió clave: no puede quedarse a mitad de camino.
    if acc is not None and not acc.get("clave_hash"):
        return redirect(url_for("portal.elegir_clave"))

    # Lo que el cliente vino a ver es su cuenta: no se le pone una portada en
    # el medio para que tenga que dar otro click.
    return redirect(url_for("portal.estado_cuenta"))
