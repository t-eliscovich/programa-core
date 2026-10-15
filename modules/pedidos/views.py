"""/pedidos — qué pidieron los clientes y todavía no se despachó.

Cinco cortes del mismo dato, en el orden del Excel "Detalle de Pedidos" de la
dueña: Color · Tipo de tela · Acabado · Cliente · Categoría. El acabado no es
un corte aparte: desde el 06/10/2026 es parte del RENGLÓN (tela + color +
acabado), sacado de la LÍNEA del pedido (`service.acabado_de_linea`).

  Color      → una fila por color (código grande, nombre chico), con pedido,
               stock, producción y faltante; se abre a sus telas y clientes.
  Tipo tela  → una pestaña por familia, un bloque por tela, una fila por color.
  Cliente    → total pedido por cliente (mezcla unidades, como el Excel).
  Categoría  → total pedido por familia de tela.

Todo se lee en la unidad en que se pide: rollos ENTEROS para las telas,
unidades para cuellos y puños.

Rutas:
  GET /pedidos                  (facturas.ver)
  GET /pedidos/color/<codigo>   (facturas.ver)

Va en el menú debajo de Factura Proforma (dueña 2026-08-17).
"""
from __future__ import annotations

import io

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso, tiene_permiso
from modules._lib import formulas_fabricar, formulas_memos

from . import service

pedidos_bp = Blueprint("pedidos", __name__, template_folder="templates")

#: Los cortes disponibles. El primero es el default: desde el 15/10/2026 la
#: pantalla abre en Tipo de tela (Tamara: "hacé que esta sea la primera
#: pantalla en vez de color").
CORTES = ("tela", "color", "cliente", "pedido", "categoria")
CORTES_LBL = {
    "color": "Color",
    "tela": "Tipo de tela",
    "cliente": "Cliente",
    "pedido": "Pedido",
    "categoria": "Categoría",
}


@pedidos_bp.route("/pedidos")
@requiere_login
@requiere_permiso("facturas.ver")
def lista():
    filas, disponible = service.pendientes()
    categorias = service.por_categoria(filas)

    corte = (request.args.get("corte") or "").strip()
    if corte not in CORTES:
        corte = CORTES[0]

    ctx = dict(
        disponible=disponible,
        categorias=categorias,
        corte=corte,
        cortes=CORTES,
        cortes_lbl=CORTES_LBL,
        dias_pedido_max=service.DIAS_PEDIDO_MAX,
        dias_produccion_viva=service.DIAS_PRODUCCION_VIVA,
        kg_por_rollo=service.KG_POR_ROLLO,
        # se completan según el corte
        colores=[], telas=[], activa="", resumen=None,
        clientes=[], solo_faltan=False, cubiertas=[],
        pedidos_por_color={}, pedidos_lista=[], etapas={}, memo_lineas={},
        imp_hojas=[], imprimir_todas=False, nombres_color={},
        hacer_cat={}, hacer_total=0, hacer_colores=0,
        enviados={}, mandados={}, mandar_a_hacer_activo=False,
    )

    if not disponible or not categorias:
        return render_template("pedidos/lista.html", **ctx)

    if corte == "color":
        ctx["colores"] = service.por_color(filas)
        ctx["pedidos_por_color"] = service.pedidos_por_color()
        ctx["memo_lineas"] = _memo_de_lineas(ctx["pedidos_por_color"])
    elif corte == "cliente":
        ctx["clientes"] = service.por_cliente(filas)
        ctx["nombres_color"] = service.nombres_color()
        ctx["pedidos_por_color"] = service.pedidos_por_color()
        ctx["memo_lineas"] = _memo_de_lineas(ctx["pedidos_por_color"])
    elif corte == "pedido":
        pedidos_lista, disp_ped = service.por_pedido()
        ctx["pedidos_lista"] = pedidos_lista
        ctx["disponible"] = disponible and disp_ped
        # Qué pedidos ya tienen memo en la fábrica (fail-soft: {} si el
        # bridge no está — los botones se muestran y el UNIQUE de formulas
        # frena un doble envío igual).
        numeros = [p["numero"] for p in pedidos_lista]
        memo_estados = formulas_memos.estados(numeros)
        # Un memo cancelado no cuenta como "tiene memo" para la pantalla: el
        # pedido tiene que volver a mostrar "Enviar memo" como si nunca se
        # hubiera mandado.
        activos = {n: v for n, v in memo_estados.items() if v.get("estado") != "cancelado"}
        ctx["memo_estados"] = activos
        # La ETAPA de cada pedido (enviado → en tintura → terminado), armada
        # con las órdenes de formulas y la OFT en Asinfo. Sin porcentajes
        # (dueña 27/08).
        ctx["etapas"] = service.etapas_por_pedido(pedidos_lista, activos)
    elif corte == "tela":
        # La pestaña por defecto es la que MÁS falta, no la primera alfabética.
        pedida = (request.args.get("cat") or "").strip()
        nombres = [c["categoria"] for c in categorias]
        activa = pedida if pedida in nombres else (nombres[0] if nombres else "")
        solo_faltan = request.args.get("falta") == "1"
        pedidos_por_color = service.pedidos_por_color()
        memo_lineas = _memo_de_lineas(pedidos_por_color)
        # Lo ya mandado a producción desde esta pantalla (formulas_app, tabla
        # a_fabricar): sus pedidos no son memo pero tampoco "faltan".
        af_filas, _ = formulas_fabricar.envios()
        enviados = formulas_fabricar.pedidos_enviados(af_filas)
        cubiertos = set(memo_lineas) | set(enviados)
        # Falta (sin memo) y Mandar a hacer (Tamara 15/10/2026), para TODAS
        # las familias: las pestañas muestran lo que falta.
        service.en_unidad(filas, "alt")
        service.sin_memo_y_a_hacer(filas, pedidos_por_color, cubiertos)
        hacer_cat: dict[str, int] = {}
        for f in filas:
            hacer_cat[f["categoria"]] = hacer_cat.get(f["categoria"], 0) + f["hacer_d"]
        telas = service.por_tela(filas, activa) if activa else []
        cubiertas = []
        if solo_faltan:
            for t in telas:
                t["filas"] = [f for f in t["filas"] if f["hacer_d"] > 0]
            cubiertas = [t["tela"] for t in telas if not t["filas"]]
            telas = [t for t in telas if t["filas"]]
        for t in telas:
            t["u"] = next((f["u"] for f in t["filas"]), "roll")
            t["sinmemo_d"] = sum(f["sinmemo_d"] for f in t["filas"])
            t["hacer_d"] = sum(f["hacer_d"] for f in t["filas"])
            # El renglón TOTAL de cada tabla (Tamara 15/10: "los totales como
            # parte de la tabla en vez de como buckets aparte").
            t["tot_pedido"] = sum(f["pedido_d"] for f in t["filas"])
            t["tot_conmemo"] = sum(f.get("conmemo_d", 0) for f in t["filas"])
            t["tot_stock"] = sum(f["inventario_d"] for f in t["filas"])
            t["tot_prod"] = sum(f["produccion_d"] for f in t["filas"])
        # La hoja impresa: la familia elegida, o TODAS con ?todas=1 (Tamara
        # 15/10: "quiero imprimir todas juntas"), cada familia arrancando hoja.
        todas = request.args.get("todas") == "1"
        if todas:
            fams = [(c["categoria"], service.por_tela(filas, c["categoria"]))
                    for c in categorias]
            for _, ts in fams:
                for t in ts:
                    t["u"] = next((f["u"] for f in t["filas"]), "roll")
                    t["hacer_d"] = sum(f["hacer_d"] for f in t["filas"])
                    t["tot_pedido"] = sum(f["pedido_d"] for f in t["filas"])
                    t["tot_conmemo"] = sum(f.get("conmemo_d", 0) for f in t["filas"])
                    t["tot_stock"] = sum(f["inventario_d"] for f in t["filas"])
                    t["tot_prod"] = sum(f["produccion_d"] for f in t["filas"])
        else:
            fams = [(activa, telas)]
        ctx["imp_hojas"] = service.hojas_impresion(fams)
        # El NOMBRE del color al lado del código (Tamara 15/10/2026: "poné
        # también el nombre del color", en pantalla y en la impresión).
        ctx["nombres_color"] = service.nombres_color()
        ctx["imprimir_todas"] = todas
        ctx.update(
            activa=activa, telas=telas, solo_faltan=solo_faltan,
            cubiertas=cubiertas,
            resumen=next((c for c in categorias if c["categoria"] == activa), None),
            pedidos_por_color=pedidos_por_color, memo_lineas=memo_lineas,
            hacer_cat=hacer_cat,
            enviados=enviados,
            mandados=formulas_fabricar.vivos_por_clave(af_filas, service.clave_renglon),
            mandar_a_hacer_activo=(formulas_fabricar.disponible()
                                   and tiene_permiso("pedidos.enviar_memo")),
            hacer_total=sum(t["hacer_d"] for t in telas),
            hacer_colores=sum(1 for t in telas for f in t["filas"] if f["hacer_d"] > 0),
        )
    # corte == "categoria" usa `categorias`, ya calculado.

    return render_template("pedidos/lista.html", **ctx)


def _memo_de_lineas(pedidos_por_color: dict) -> dict[str, dict]:
    """Qué pedidos de los cortes Color / Tipo de tela ya tienen memo, y en
    qué etapa está cada línea (dueña 01/10: "una celda donde refleje los
    pedidos que ya estén enviados con memo").

    `{numero: {"pedido": etapa, "lineas": {producto: etapa}}}` sólo de los
    pedidos con memo activo (el cancelado no cuenta). Fail-soft: sin bridge
    a formulas → {} y la celda dice "Sin memo"; sin Asinfo para las etapas
    → todos quedan en "enviado".
    """
    numeros = sorted({p["numero"] for ps in pedidos_por_color.values() for p in ps})
    activos = {n: v for n, v in formulas_memos.estados(numeros).items()
               if v.get("estado") != "cancelado"}
    if not activos:
        return {}
    pedidos, ok = service.por_pedido()
    etapas = service.etapas_por_pedido(pedidos, activos) if ok else {}
    return {n: etapas.get(n) or {"pedido": "enviado", "lineas": {}} for n in activos}


@pedidos_bp.route("/pedidos/excel")
@requiere_login
@requiere_permiso("facturas.ver")
def excel():
    """Todo lo que se ve en pantalla, en un xlsx con una hoja por corte.

    Color (a nivel color + tela), Tipo de tela, Cliente y Categoría — el mismo
    dato de las cuatro pestañas, para ordenar y filtrar en Excel.
    """
    filas, disponible = service.pendientes()
    if not disponible:
        flash("No pude consultar Asinfo, así que no armé el Excel. "
              "Probá de nuevo en un minuto.", "error")
        return redirect(url_for("pedidos.lista"))
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        flash("openpyxl no instalado en el server — pedí al admin que lo instale.", "error")
        return redirect(url_for("pedidos.lista"))

    from filters import today_ec

    colores = service.por_color(filas)          # variants ya traen pedido_d, etc.
    categorias = service.por_categoria(filas)
    clientes = service.por_cliente()

    wb = Workbook()
    bold = Font(bold=True)
    relleno = PatternFill("solid", fgColor="E2E8F0")

    def encabezado(ws, cols):
        for i, h in enumerate(cols, 1):
            c = ws.cell(row=1, column=i, value=h)
            c.font = bold
            c.fill = relleno
            c.alignment = Alignment(horizontal="left")

    def cerrar(ws, anchos, nfilas):
        for i, ancho in enumerate(anchos, 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = ancho
        ws.freeze_panes = "A2"
        ncol = ws.cell(row=1, column=len(anchos)).column_letter
        ws.auto_filter.ref = f"A1:{ncol}{max(nfilas + 1, 2)}"

    def falta(v):
        return v if v > 0 else 0

    # ── Hoja Color (color + tela) ──
    ws = wb.active
    ws.title = "Color"
    encabezado(ws, ["Código", "Color", "Tela", "Acabado", "Unidad",
                    "Pedido", "Stock", "En produc.", "Falta"])
    r = 1
    for g in colores:
        nombre = f"{g['codigo']} {g['nombre']}".strip()
        for f in g["variants"]:
            r += 1
            for i, v in enumerate([f["codigo"], nombre, f["tela"], f.get("acabado", ""),
                                   f["u"], f["pedido_d"], f["inventario_d"],
                                   f["produccion_d"], falta(f["faltan_d"])], 1):
                ws.cell(row=r, column=i, value=v)
    cerrar(ws, (12, 24, 22, 8, 8, 10, 10, 12, 10), r)

    # ── Hoja Tipo de tela (familia · tela · color) ──
    ws = wb.create_sheet("Tipo de tela")
    encabezado(ws, ["Familia", "Tela", "Color", "Código", "Acabado", "Unidad",
                    "Pedido", "Stock", "En produc.", "Falta"])
    r = 1
    for f in sorted(filas, key=lambda x: (x["categoria"], x["tela"], x["color"])):
        r += 1
        for i, v in enumerate([f["categoria"], f["tela"], f["color"], f["codigo"],
                               f.get("acabado", ""), f["u"], f["pedido_d"],
                               f["inventario_d"], f["produccion_d"], falta(f["faltan_d"])], 1):
            ws.cell(row=r, column=i, value=v)
    cerrar(ws, (12, 22, 10, 12, 8, 8, 10, 10, 12, 10), r)

    # ── Hoja Cliente ──
    ws = wb.create_sheet("Cliente")
    encabezado(ws, ["Cliente", "Pedido"])
    for r, cl in enumerate(clientes, 2):
        ws.cell(row=r, column=1, value=cl["cliente"])
        ws.cell(row=r, column=2, value=cl["pedido"])
    cerrar(ws, (14, 12), len(clientes))

    # ── Hoja Categoría ──
    ws = wb.create_sheet("Categoría")
    encabezado(ws, ["Familia", "Pedido", "Unidad"])
    cats = sorted(categorias, key=lambda c: -c["pedido_d"])
    for r, c in enumerate(cats, 2):
        ws.cell(row=r, column=1, value=c["categoria"])
        ws.cell(row=r, column=2, value=c["pedido_d"])
        ws.cell(row=r, column=3, value=c["u"])
    cerrar(ws, (16, 12, 8), len(cats))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    nombre = f"pedidos_pendientes_{today_ec():%Y_%m_%d}.xlsx"
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@pedidos_bp.route("/pedidos/ordenes")
@requiere_login
@requiere_permiso("facturas.ver")
def ordenes():
    """Las órdenes de tintura con producto, cliente, pedido y avance en kg
    (Tamara 2026-10-01). Ver modules/pedidos/ordenes.py."""
    from . import ordenes as _ord

    res = _ord.ordenes()
    return render_template(
        "pedidos/ordenes.html", res=res, filas=res["filas"],
        estados=_ord.ESTADOS, dias=_ord.DIAS_DEFAULT,
    )


@pedidos_bp.route("/pedidos/color/<codigo>")
@requiere_login
@requiere_permiso("facturas.ver")
def color(codigo: str):
    """Detalle de un color: quién lo pidió y qué se está tinturando.

    No devuelve 404 cuando el código no existe: si Asinfo no contesta no
    podemos distinguir "no existe" de "no pude preguntar", y un 404 ahí
    mentiría. Muestra la ficha vacía con el aviso.
    """
    aca = request.args.get("aca")
    ficha, pedidos, ordenes, disponible = service.detalle_color(codigo, aca)
    return render_template(
        "pedidos/color.html",
        codigo=codigo.strip().upper(),
        ficha=ficha,
        pedidos=pedidos,
        ordenes=ordenes,
        repetidos=service.repetidos(pedidos),
        disponible=disponible,
        kg_por_rollo=service.KG_POR_ROLLO,
    )


@pedidos_bp.route("/pedidos/enviar-memo", methods=["POST"])
@requiere_login
@requiere_permiso("pedidos.enviar_memo")
def enviar_memo():
    """Manda UN pedido como memo al tab Memos de formulas_app.

    El memo es la foto del pedido al momento de enviar. Si ya estaba enviado
    (por quien sea), avisa y no duplica — lo garantiza el UNIQUE de
    `pedido_numero` del lado de formulas_app, no esta vista.
    """
    from flask import g

    numero = (request.form.get("numero") or "").strip()
    detalle = service.armar_memo(numero) if numero else None
    if detalle is None:
        flash("No encontré ese pedido entre los pendientes, así que no mandé "
              "el memo. Recargá la pantalla y probá de nuevo.", "error")
        return redirect(url_for("pedidos.lista", corte="pedido"))

    usuario = g.user["username"] if getattr(g, "user", None) else ""
    ok, motivo = formulas_memos.enviar(
        numero=numero,
        cliente=detalle["cliente"]["nombre"],
        vendedor=service.etiqueta_dueno(detalle["vendedor"]),
        enviado_por=usuario,
        detalle=detalle,
    )
    if ok:
        flash(f"Memo del pedido {numero} enviado a la fábrica.", "success")
    elif motivo == "ya_enviado":
        flash(f"El pedido {numero} ya tenía memo enviado — no se mandó otro.",
              "warning")
    else:
        flash("No pude hablar con formulas_app — el memo NO se envió. "
              "Probá de nuevo en un rato.", "error")
    return redirect(url_for("pedidos.lista", corte="pedido"))


@pedidos_bp.route("/pedidos/cancelar-memo", methods=["POST"])
@requiere_login
@requiere_permiso("pedidos.enviar_memo")
def cancelar_memo():
    """Cancela un memo PENDIENTE para poder mandar uno nuevo (el pedido
    cambió). Si la fábrica ya lo tomó, no hace nada — avisa y no rompe."""
    from flask import g

    numero = (request.form.get("numero") or "").strip()
    if not numero:
        flash("Falta el número de pedido.", "error")
        return redirect(url_for("pedidos.lista", corte="pedido"))

    usuario = g.user["username"] if getattr(g, "user", None) else ""
    ok, motivo = formulas_memos.cancelar(numero, usuario)
    if ok:
        flash(f"Memo del pedido {numero} cancelado. Ya lo podés mandar de nuevo.",
              "success")
    elif motivo == "no_pendiente":
        flash(f"El memo del pedido {numero} ya no está pendiente — no se "
              "pudo cancelar.", "warning")
    else:
        flash("No pude hablar con formulas_app — el memo NO se canceló. "
              "Probá de nuevo en un rato.", "error")
    return redirect(url_for("pedidos.lista", corte="pedido"))


# ── Mandar a hacer → formulas_app «Fabricación» (Tamara 15/10/2026) ───────────

@pedidos_bp.route("/pedidos/mandar-a-hacer", methods=["POST"])
@requiere_login
@requiere_permiso("pedidos.enviar_memo")
def mandar_a_hacer():
    """Recibe `c_<clave del renglón>` = rollos y lo manda a formulas_app.

    El navegador sólo dice CUÁNTO de qué renglón: tela, color, acabado y los
    pedidos que cubre se arman acá de nuevo, con los pendientes de Asinfo de
    este momento — así nadie puede mandar un renglón que no existe ni colgarle
    pedidos ajenos. Un renglón que ya no está pendiente se saltea y se avisa.
    """
    from flask import g

    cat = (request.form.get("cat") or "").strip()
    volver = url_for("pedidos.lista", corte="tela", cat=cat) if cat else url_for(
        "pedidos.lista", corte="tela")
    def _num(v) -> float:
        try:
            return max(0.0, float(str(v or "0").replace(",", ".")))
        except ValueError:
            return 0.0

    # c_<clave> = rollos de la tela, r_<clave> = rib en kilos (Tamara 15/10).
    pedidas: dict[str, tuple[float, float]] = {}
    for k in request.form:
        if k[:2] in ("c_", "r_"):
            clave = k[2:]
            rollos, rib = _num(request.form.get("c_" + clave)), _num(request.form.get("r_" + clave))
            if rollos > 0 or rib > 0:
                pedidas[clave] = (rollos, rib)
    if not pedidas:
        flash("No cargaste ninguna cantidad para producir.", "warning")
        return redirect(volver)

    filas, ok = service.pendientes()
    if not ok:
        flash("No pude consultar Asinfo — no se mandó nada. Probá de nuevo en un rato.",
              "error")
        return redirect(volver)
    service.en_unidad(filas, "alt")
    por_clave = {f["clave"]: f for f in filas}
    pedidos_por_color = service.pedidos_por_color()
    memo_lineas = _memo_de_lineas(pedidos_por_color)
    af_filas, _ = formulas_fabricar.envios()
    ya = set(memo_lineas) | set(formulas_fabricar.pedidos_enviados(af_filas))

    lineas, salteadas = [], []
    for clave, (cant, rib) in pedidas.items():
        f = por_clave.get(clave)
        if f is None:
            salteadas.append(clave.split("_")[0])
            continue
        peds = [{"numero": p["numero"], "cliente": p["codigo_cliente"] or p["cliente"],
                 "cantidad": p["cantidad"]}
                for p in pedidos_por_color.get(clave, []) if p["numero"] not in ya]
        lineas.append({
            "categoria": f["categoria"], "tela": f["tela"],
            "acabado": f.get("acabado") or "", "color": f["color"],
            "codigo": f["codigo"], "cantidad": cant, "unidad": f["u"], "rib_kg": rib,
            "pedidos": peds,
        })
    usuario = g.user["username"] if getattr(g, "user", None) else ""
    ok, motivo, _envio = formulas_fabricar.mandar(lineas, usuario)
    if ok:
        total = sum(ln["cantidad"] for ln in lineas)
        rib = sum(ln["rib_kg"] for ln in lineas)
        partes = []
        if total:
            partes.append(f"{total:g} {'rollos' if lineas[0]['unidad'] == 'roll' else lineas[0]['unidad']}")
        if rib:
            partes.append(f"{rib:g} kg de rib")
        flash(f"Listo: {len(lineas)} {'color' if len(lineas) == 1 else 'colores'} · "
              + " + ".join(partes)
              + " mandados a producción. A Jonathan le llega en «Fabricación».", "success")
    elif motivo == "vacio":
        flash("Ninguno de los colores cargados sigue pendiente — no se mandó nada.",
              "warning")
    else:
        flash("No pude hablar con formulas_app — NO se mandó nada. Probá de nuevo en un rato.",
              "error")
    if salteadas:
        flash("Ya no estaban pendientes, no se mandaron: " + ", ".join(salteadas) + ".",
              "warning")
    return redirect(volver)


@pedidos_bp.route("/pedidos/mandados")
@requiere_login
@requiere_permiso("facturas.ver")
def mandados():
    """Historial de lo mandado a producción: cada envío con fecha, quién lo
    mandó, sus colores y en qué está cada uno en la fábrica."""
    filas, ok = formulas_fabricar.envios(dias=120)
    envios, por = [], {}
    for f in filas:
        e = por.get(f["envio"])
        if e is None:
            e = {"envio": f["envio"], "enviado_en": f["enviado_en"],
                 "enviado_por": f["enviado_por"], "filas": []}
            por[f["envio"]] = e
            envios.append(e)
        e["filas"].append(f)
    for e in envios:
        e["filas"].sort(key=service.orden_tipo_de_tela)
        e["total"] = sum(f["cantidad"] for f in e["filas"])
        e["total_rib"] = sum(f.get("rib_kg") or 0 for f in e["filas"])
        est = {f["estado"] for f in e["filas"]} - {"cancelado"}
        e["estado"] = ("pendiente" if "pendiente" in est else
                       "en_proceso" if "en_proceso" in est else
                       "terminado" if est else "cancelado")
        e["cancelable"] = any(f["estado"] == "pendiente" for f in e["filas"])
    return render_template("pedidos/mandados.html", envios=envios, disponible=ok,
                           nombres_color=service.nombres_color(),
                           puede_cancelar=tiene_permiso("pedidos.enviar_memo"))


@pedidos_bp.route("/pedidos/mandados/cancelar", methods=["POST"])
@requiere_login
@requiere_permiso("pedidos.enviar_memo")
def cancelar_mandado():
    """Cancela un envío entero (`envio`) o un renglón (`id`) que la fábrica
    todavía no tomó. Lo que ya está en proceso o hecho no se toca."""
    from flask import g

    usuario = g.user["username"] if getattr(g, "user", None) else ""
    envio = (request.form.get("envio") or "").strip()
    try:
        id_ = int(request.form.get("id") or "")
    except ValueError:
        id_ = None
    if not envio and id_ is None:
        flash("No encontré qué cancelar.", "warning")
        return redirect(url_for("pedidos.mandados"))
    n, motivo = formulas_fabricar.cancelar(usuario, envio=envio, id_=id_)
    if n:
        flash(f"Cancelado: {n} {'color' if n == 1 else 'colores'}. A la fábrica le aparece como cancelado.",
              "success")
    elif motivo == "no_pendiente":
        flash("No se pudo cancelar: la fábrica ya lo tomó (está en proceso o hecho).", "warning")
    else:
        flash("No pude hablar con formulas_app — NO se canceló. Probá de nuevo en un rato.", "error")
    return redirect(url_for("pedidos.mandados"))
