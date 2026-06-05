"""Listado, detalle y altas de cheques."""

from datetime import date, datetime

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import db
from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from filters import today_ec
from parsers import parse_date, parse_int, parse_monto

from . import queries

cheques_bp = Blueprint("cheques", __name__, template_folder="templates")


def _bancos() -> list[dict]:
    try:
        return db.fetch_all("SELECT no_banco, nombre FROM scintela.banco ORDER BY no_banco")
    except Exception:
        return []


# TMT 2026-05-19 v8 — _handle_cobro_efectivo + crear_cobro_efectivo
# (queries) eliminados: la dueña aclaró que el cobro en efectivo YA
# entraba por /cheques/nuevo eligiendo banco=99·EFECTIVO. No requería
# pantalla ni handler extra.


@cheques_bp.route("/cheques/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("cheques.crear")
def nuevo():
    """Alta de cheque.

    - stat='Z' por defecto (cartera). Si fechad > fecha → 'P' (postdatado).
    - Si es postdatado, `queries.crear` se encarga de la contrapartida.
    """
    errores: list[str] = []
    form: dict = {}

    try:
        from modules.autocomplete.queries import clientes_para_datalist

        clientes_datalist = clientes_para_datalist()
    except Exception:
        clientes_datalist = []

    if request.method == "GET":
        hoy_es = datetime.now().date().strftime("%d/%m/%Y")
        hoy_iso = datetime.now().date().isoformat()
        form["fecha"] = hoy_es
        form["fecha_recibido"] = hoy_es
        form["fechad"] = hoy_es
        # ISO para inputs type="date" (browser nativo). Pedido TMT 2026-05-14
        # — el campo "A depositar" usa date picker que sólo acepta ISO.
        form["fechad_iso"] = hoy_iso
        # Restaurar campos via query string — si veníamos de crear un
        # cliente nuevo, /clientes/nuevo nos redirige con los datos del
        # form anterior. TMT 2026-05-13.
        for k in (
            "fecha",
            "fecha_recibido",
            "fechad",
            "codigo_cli",
            "no_cheque",
            "importe",
            "no_banco",
            "banco_texto",
            "prov",
        ):
            if request.args.get(k):
                form[k] = request.args.get(k)
        if request.args.get("es_anticipo"):
            form["es_anticipo"] = request.args.get("es_anticipo") in ("1", "true", "True", "on")
        return render_template(
            "cheques/nuevo.html",
            form=form,
            errores=errores,
            bancos=_bancos(),
            clientes_datalist=clientes_datalist,
        )

    # TMT 2026-05-27 dueña: 'Cuando pongo volver para atrás no me borres
    # lo pre cargado si avance en cobranza'. Si llega paso=editar, no
    # creamos nada — re-renderizamos el form con TODOS los valores ya
    # cargados, listos para editar.
    if request.form.get("paso") == "editar":
        form_back: dict = {}
        form_back["fecha_recibido"] = (request.form.get("fecha_recibido") or "")
        form_back["fechad_iso"] = (request.form.get("fecha_recibido") or "")
        form_back["codigo_cli"] = (request.form.get("codigo_cli") or "").upper()
        form_back["banco_texto"] = (request.form.get("banco_texto") or "")
        form_back["prov"] = (request.form.get("prov") or "")
        if request.form.get("es_anticipo"):
            form_back["es_anticipo"] = True
        # Arrays — los pasamos como listas para que el template los pueda
        # iterar y rellenar cada bloque.
        form_back["cheques"] = []
        nos = request.form.getlist("no_cheque[]")
        imps = request.form.getlist("importe[]")
        fchs = request.form.getlist("fechad[]")
        sts = request.form.getlist("stat[]")
        dbs = request.form.getlist("doc_banco[]")
        nbs = request.form.getlist("no_banco[]")
        n = max(len(nos), len(imps), len(fchs), len(sts), len(dbs), len(nbs))
        for i in range(n):
            form_back["cheques"].append({
                "no_cheque": nos[i] if i < len(nos) else "",
                "importe": imps[i] if i < len(imps) else "",
                "fechad": fchs[i] if i < len(fchs) else "",
                "stat": sts[i] if i < len(sts) else "Z",
                "doc_banco": dbs[i] if i < len(dbs) else "",
                "no_banco": nbs[i] if i < len(nbs) else "",
            })
        # Compat: el template usa form.importe/no_cheque/fechad/no_banco
        # (scalars del primer bloque) para precarga. Llenamos del primero.
        if form_back["cheques"]:
            _p = form_back["cheques"][0]
            form_back["no_cheque"] = _p["no_cheque"]
            form_back["importe"] = _p["importe"]
            form_back["fechad"] = _p["fechad"]
            form_back["fechad_iso"] = _p["fechad"]
            form_back["stat"] = _p["stat"]
            form_back["doc_banco"] = _p["doc_banco"]
            form_back["no_banco"] = _p["no_banco"]
        return render_template(
            "cheques/nuevo.html",
            form=form_back,
            errores=[],
            bancos=_bancos(),
            clientes_datalist=clientes_datalist,
        )

    # TMT 2026-05-15: simplificado — una sola fecha de cabecera. La
    # "fecha de emisión" del cheque NO interesa a la dueña; usamos
    # `fecha_recibido` (cuándo entró al sistema) tanto para `fecha` como
    # para `fecha_recibido` internamente.
    fecha_recibido = parse_date(request.form.get("fecha_recibido")) or today_ec()
    fecha = fecha_recibido
    codigo_cli = (request.form.get("codigo_cli") or "").strip().upper()
    # Multi-cheque: el form manda no_cheque[], importe[], fechad[] como
    # arrays alineados por índice. Backwards-compat con name sin [].
    nos_cheque_raw = request.form.getlist("no_cheque[]")
    if not nos_cheque_raw:
        v = request.form.get("no_cheque")
        nos_cheque_raw = [v] if v else []
    importes_raw = request.form.getlist("importe[]")
    if not importes_raw:
        v = request.form.get("importe")
        importes_raw = [v] if v else []
    fechads_raw = request.form.getlist("fechad[]")
    if not fechads_raw:
        v = request.form.get("fechad")
        fechads_raw = [v] if v else []
    # TMT 2026-05-20 — Estado por cheque desde el dropdown nuevo (Z, P, D,
    # B, X, 1, 2). Si no viene, default Z (cartera) — el comportamiento
    # legacy. Cuando stat=P, la fechad es obligatoria; para el resto, la
    # fechad se colapsa a la fecha de recibido (queries.crear lo hace
    # con `fechad or fecha`).
    stats_raw = request.form.getlist("stat[]")
    if not stats_raw:
        v = request.form.get("stat")
        stats_raw = [v] if v else []
    # TMT 2026-05-26 dueña: nuevo campo "Doc. banco" por cheque (N° de
    # comprobante / depósito / transferencia). Campo libre opcional.
    docs_banco_raw = request.form.getlist("doc_banco[]")
    if not docs_banco_raw:
        v = request.form.get("doc_banco")
        docs_banco_raw = [v] if v else []
    # TMT 2026-05-27 dueña: 'banco emisor está después de importe y entonces
    # si elijo otro cheque, me deja elegir otro banco emisor'. Banco por
    # cheque (no_banco[] array). Si no viene array, fallback a scalar legacy.
    nos_banco_raw = request.form.getlist("no_banco[]")
    if not nos_banco_raw:
        v = request.form.get("no_banco")
        nos_banco_raw = [v] if v else []
    # Bancos depósito directo: 90 DEP.PICH, 91 DEP.INTER, 95 CANCELA ANT,
    # 97 ANTICIPO, 99 EFECTIVO. La dueña pidió que para estos: fecha hoy
    # obligatoria, no_cheque no requerido.
    _BANCOS_DEPOSITO = {90, 91, 95, 97, 99}
    # Limpiar y alinear las listas — cada cheque puede tener su propia
    # fecha de depósito Y su propio banco emisor.
    cheques_in: list[dict] = []
    for i, n in enumerate(nos_cheque_raw):
        n_clean = (n or "").strip()
        i_clean = importes_raw[i] if i < len(importes_raw) else None
        fd_clean = fechads_raw[i] if i < len(fechads_raw) else None
        st_clean = (stats_raw[i] if i < len(stats_raw) else "") or "Z"
        st_clean = st_clean.strip().upper()[:1] or "Z"
        db_clean = (docs_banco_raw[i] if i < len(docs_banco_raw) else "") or ""
        db_clean = db_clean.strip()[:40] or None
        # banco por cheque — fallback al primero/legacy si index out of range.
        nb_raw = nos_banco_raw[i] if i < len(nos_banco_raw) else (nos_banco_raw[0] if nos_banco_raw else None)
        nb_clean = parse_int(nb_raw)
        # Para bancos depósito: si N° cheque vacío, lo dejamos vacío (no req).
        # Pero si NO es depósito, el N° cheque queda como vino.
        es_deposito = (nb_clean in _BANCOS_DEPOSITO)
        if not n_clean and not (i_clean and str(i_clean).strip()):
            continue  # bloque totalmente vacío → skip
        # fechad por defecto: si banco es depósito → fecha hoy obligatoria.
        # Si stat='P' (postdatado), fechad obligatoria explicita.
        # Para el resto, colapsa a fecha_recibido.
        fd_parsed = parse_date(fd_clean) if fd_clean else None
        if es_deposito:
            cheque_fechad = today_ec()  # dueña: 'obligatoriamente fecha de hoy'
        elif st_clean == "P":
            cheque_fechad = fd_parsed  # puede ser None → error abajo
        else:
            cheque_fechad = fd_parsed or fecha
        cheques_in.append(
            {
                "no_cheque": n_clean,
                "importe": parse_monto(i_clean),
                "fechad": cheque_fechad,
                "stat": st_clean,
                "doc_banco": db_clean,
                "raw_importe": i_clean,
                "no_banco": nb_clean,
                "es_deposito": es_deposito,
            }
        )
    # `fechad` general (compat con resto del view + restore-on-error).
    fechad = cheques_in[0]["fechad"] if cheques_in else fecha
    # Backwards-compat para el resto del view: el "primer cheque" es el
    # canónico — se usa para flash, restore-on-error, y aplicaciones.
    primero = cheques_in[0] if cheques_in else {}
    no_cheque = primero.get("no_cheque", "")
    primero.get("importe")
    # TMT 2026-05-27 dueña: banco por cheque. `no_banco` (cabecera) ahora
    # es derivado del primer cheque para compat con resto del view.
    no_banco = cheques_in[0].get("no_banco") if cheques_in else parse_int(request.form.get("no_banco"))
    banco_texto = (request.form.get("banco_texto") or "").strip()[:30] or None
    prov = (request.form.get("prov") or "").strip()[:5] or None
    es_anticipo = bool(request.form.get("es_anticipo"))
    # TMT 2026-05-19 v8 — banco=97 (ANTICIPO) implica es_anticipo=True
    # aunque la dueña no tilde el checkbox. Pedido literal: "Asegurate
    # que en cobranza funcionen las logicas de seleccionar opciones de
    # banco >90 ejemplo anticipos, efectivo etc". Sin esto, elegir 97
    # quedaba como cheque normal y no generaba el espejo negativo.
    if no_banco == 97 and not es_anticipo:
        es_anticipo = True

    if fecha is None:
        errores.append("Fecha inválida.")
    if not codigo_cli:
        errores.append("Código de cliente requerido.")
    elif not db.fetch_one("SELECT 1 AS x FROM scintela.cliente WHERE codigo_cli = %s", (codigo_cli,)):
        # Cliente no existe → flujo guiado a /clientes/nuevo, mismo patrón
        # que facturas.nueva. TMT 2026-05-13.
        _permisos = getattr(g, "permisos", set()) or set()
        if "clientes.crear" in _permisos or "*" in _permisos:
            from urllib.parse import urlencode

            # TMT 2026-05-15: el form ahora manda arrays (no_cheque[], importe[],
            # fechad[]). Para el restore-on-cliente-no-existe sólo conservamos
            # el PRIMER bloque — si la usuaria estaba multi-cargando, los
            # cheques 2+ se pierden al volver. Caso poco frecuente (cliente
            # nuevo + multi-cheque), aceptable. `fecha` viejo ya no se manda
            # — usamos `fecha_recibido` (cuándo entró al sistema).
            primer_no = nos_cheque_raw[0] if nos_cheque_raw else ""
            primer_imp = importes_raw[0] if importes_raw else ""
            primer_fechad = fechads_raw[0] if fechads_raw else ""
            restore_args = {
                "fecha_recibido": request.form.get("fecha_recibido") or "",
                "fechad": primer_fechad,
                "codigo_cli": codigo_cli,
                "no_cheque": primer_no,
                "importe": primer_imp,
                "no_banco": request.form.get("no_banco") or "",
                "banco_texto": banco_texto or "",
                "prov": prov or "",
                "es_anticipo": "1" if es_anticipo else "",
            }
            restore_args = {k: v for k, v in restore_args.items() if v}
            next_url = url_for("cheques.nuevo") + "?" + urlencode(restore_args)
            flash(
                f"El cliente {codigo_cli} no existe — completá los datos "
                "para crearlo y después seguís con el cheque.",
                "warning",
            )
            return redirect(url_for("clientes.nuevo", codigo=codigo_cli, next=next_url))
        errores.append(f"El cliente {codigo_cli!r} no existe.")
    # Validación multi-cheque: TODOS los bloques deben tener n° y importe>0.
    if not cheques_in:
        errores.append("Por lo menos un cheque (N° + importe) requerido.")
    else:
        for i, ch in enumerate(cheques_in, start=1):
            etq = f"Cheque #{i}" if len(cheques_in) > 1 else ""
            # Bancos de depósito directo (90/91/95/97/99 = DEP.PICH, EFECTIVO,
            # ANTICIPO, etc.) NO requieren N° de cheque: es una cobranza en
            # efectivo/depósito, no hay cheque físico. La dueña ya lo había
            # pedido (ver _BANCOS_DEPOSITO arriba); la validación quedó
            # inconsistente y lo exigía igual. TMT 2026-06-06.
            if not ch.get("no_cheque") and not ch.get("es_deposito"):
                errores.append(f"N° de cheque{(' (' + etq + ')') if etq else ''} requerido.")
            imp = ch.get("importe")
            # TMT 2026-06-06 dueña: permitir importes NEGATIVOS (correcciones,
            # créditos a favor, devoluciones). La aplicación a facturas ya los
            # soporta (ver `aplicaciones_pre`, |imp|<0.005). Sólo bloqueamos el
            # cero. OJO: el `abono` de la factura viene del DBF — una corrección
            # con negativo en PC se pisa en el próximo sync; sirve para créditos
            # genuinos / facturas creadas en PC, no para revertir abonos del dBase.
            if imp is None or abs(imp) < 0.005:
                errores.append(f"Importe{(' (' + etq + ')') if etq else ''} distinto de cero requerido.")
            # TMT 2026-05-20 — si stat='P' (postdatado) la fecha de
            # depósito es obligatoria. Pedido literal dueña: "agregar P
            # en el dropdown y pedir fecha".
            if (ch.get("stat") or "Z") == "P" and ch.get("fechad") is None:
                errores.append(
                    f"Fecha de depósito requerida{(' (' + etq + ')') if etq else ''} "
                    "cuando el estado es P (postdatado)."
                )
        # No permitir números duplicados dentro del mismo guardado.
        nos = [c["no_cheque"].upper() for c in cheques_in if c.get("no_cheque")]
        if len(nos) != len(set(nos)):
            errores.append("Hay N° de cheque repetidos en el formulario.")
    if no_banco is None and not banco_texto:
        errores.append("Banco requerido (elegir o escribir).")

    # TMT 2026-05-15: el form ahora manda no_cheque[], importe[], fechad[]
    # como arrays. Para el restore-on-error, usamos el PRIMER elemento.
    # `fecha` viejo ya no existe (es fecha_recibido). `fechad` toma el del
    # primer bloque si existe.
    primer_no_raw = nos_cheque_raw[0] if nos_cheque_raw else ""
    primer_imp_raw = importes_raw[0] if importes_raw else ""
    primer_fechad_raw = fechads_raw[0] if fechads_raw else ""
    form.update(
        {
            "fecha": request.form.get("fecha_recibido"),  # alias para compat
            "fecha_recibido": request.form.get("fecha_recibido"),
            "fechad": primer_fechad_raw,
            "fechad_iso": primer_fechad_raw,
            "codigo_cli": codigo_cli,
            "no_cheque": primer_no_raw or no_cheque,
            "importe": primer_imp_raw,
            "no_banco": request.form.get("no_banco"),
            "banco_texto": banco_texto or "",
            "prov": prov or "",
            "es_anticipo": es_anticipo,
        }
    )

    if errores:
        return render_template(
            "cheques/nuevo.html",
            form=form,
            errores=errores,
            bancos=_bancos(),
            clientes_datalist=clientes_datalist,
        ), 400

    # Aplicaciones inline (TMT 2026-05-11): la UI permite distribuir el
    # cheque a facturas abiertas del cliente desde el mismo form.
    # Inputs llegan como `aplicar[<id_factura>]`. Si está vacío o cero,
    # se ignora. Si no aplica nada, el cheque queda en cartera puro.
    # TMT 2026-05-15: en modo multi-cheque también aceptamos aplicaciones
    # — el backend distribuye FIFO entre los cheques creados (cubrir
    # primero con el cheque #1 hasta agotarlo, después #2, etc.).
    aplicaciones_pre: list[dict] = []
    if not es_anticipo:
        for k, v in request.form.items():
            if not k.startswith("aplicar[") or not k.endswith("]"):
                continue
            try:
                id_fact = int(k[len("aplicar[") : -1])
            except ValueError:
                continue
            imp = parse_monto(v)
            # TMT 2026-05-15: aceptamos importes NEGATIVOS para absorber
            # créditos a favor del cliente (devoluciones, sobre-aplicaciones).
            # Antes el filtro `imp <= 0.005` descartaba todo lo no-positivo;
            # ahora descartamos sólo lo cercano a cero (|imp| < 0.005).
            if imp is None or abs(imp) < 0.005:
                continue
            # `stat_final[id_fact]` viene del paso de confirmación cuando
            # la dueña eligió T o A explícitamente. Sin override, queries
            # decide automático.
            forzar_stat = (request.form.get(f"stat_final[{id_fact}]") or "").upper()
            if forzar_stat not in ("T", "A"):
                forzar_stat = ""
            aplicaciones_pre.append(
                {
                    "id_fact": id_fact,
                    "importe": float(imp),
                    "forzar_stat": forzar_stat,
                }
            )

    # Sobre-aplicación: si la suma > TOTAL de cheques, error. Con multi-cheque,
    # el total disponible es la suma de todos los importes.
    # TMT 2026-05-15: tolerancia de $50 — el JS pregunta al submit para
    # diferencias chicas; el backend solo bloquea si la diferencia es grande
    # (más de $50 = error real, no redondeo / "casi-exacto").
    if aplicaciones_pre:
        total_a_aplicar = sum(a["importe"] for a in aplicaciones_pre)
        total_cheques = sum(float(c.get("importe") or 0) for c in cheques_in)
        if total_a_aplicar > total_cheques + 50.00:
            errores.append(
                f"La suma de las aplicaciones ({total_a_aplicar:.2f}) "
                f"supera el total de cheques ({total_cheques:.2f}) por más de $50."
            )
            return render_template(
                "cheques/nuevo.html",
                form=form,
                errores=errores,
                bancos=_bancos(),
                clientes_datalist=clientes_datalist,
            ), 400

    # ─── Wizard paso 2: confirmación con resumen de cambios ──────────
    # TMT 2026-05-15: antes de ejecutar, calculamos qué facturas van a
    # quedar T (cancelada) vs A (parcial) y mostramos un resumen. La
    # dueña confirma con un toggle por factura cuando el saldo residual
    # supera $0.50. Para diferencias ≤ $0.50, T automático.
    paso = (request.form.get("paso") or "").strip()
    if paso != "ejecutar":
        # Pre-calcular impacto en cada factura.
        impacto_facturas: list[dict] = []
        for ap in aplicaciones_pre:
            id_fact = int(ap["id_fact"])
            imp = float(ap["importe"])
            f = db.fetch_one(
                "SELECT id_factura, numf, fecha, vencimiento, importe, "
                "abono, saldo, stat "
                "FROM scintela.factura WHERE id_factura = %s",
                (id_fact,),
            )
            if not f:
                continue
            saldo_actual = float(f.get("saldo") or 0)
            abono_actual = float(f.get("abono") or 0)
            nuevo_abono = abono_actual + imp
            nuevo_saldo = float(f.get("importe") or 0) - nuevo_abono
            # Sugerencia automática:
            #   saldo ≤ 0 o |saldo| ≤ $0.50 → T
            #   saldo > $0.50 con abono → A
            #   sin abono → preserva
            auto_t = abs(nuevo_saldo) <= 0.50 or nuevo_saldo <= 0.01
            if auto_t:
                stat_sugerido = "T"
            elif nuevo_abono > 0.01:
                stat_sugerido = "A"
            else:
                stat_sugerido = f.get("stat") or "Z"
            # Si la dueña ya eligió override en una iteración previa, lo
            # respetamos al re-renderizar.
            stat_override = (request.form.get(f"stat_final[{id_fact}]") or "").upper()
            if stat_override not in ("T", "A"):
                stat_override = ""
            stat_actual = stat_override or stat_sugerido
            # Decidible si tiene saldo restante real (positivo o negativo
            # > centavos). Si saldo == 0 exacto, no hay decisión (T forzada).
            decidible = abs(nuevo_saldo) > 0.005
            impacto_facturas.append(
                {
                    "id_factura": id_fact,
                    "numf": f.get("numf"),
                    "fecha": f.get("fecha"),
                    "vencimiento": f.get("vencimiento"),
                    "importe": float(f.get("importe") or 0),
                    "saldo_antes": saldo_actual,
                    "aplicacion": imp,
                    "saldo_despues": nuevo_saldo,
                    "stat_antes": f.get("stat") or "Z",
                    "stat_sugerido": stat_sugerido,  # lo que el sistema sugiere
                    "stat_despues": stat_actual,  # lo que va a quedar (con override)
                    "auto_t": auto_t,
                    "decidible": decidible,
                }
            )
        # Renderizar la pantalla de confirmación con todos los datos
        # serializados para mandar al segundo POST.
        return render_template(
            "cheques/nuevo_confirmar.html",
            codigo_cli=codigo_cli,
            no_banco=no_banco,
            banco_texto=banco_texto or "",
            prov=prov or "",
            fecha_recibido=fecha_recibido,
            es_anticipo=es_anticipo,
            cheques_in=cheques_in,
            aplicaciones_pre=aplicaciones_pre,
            impacto_facturas=impacto_facturas,
            total_cheques=sum(float(c.get("importe") or 0) for c in cheques_in),
        )

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:3].upper()
        # Multi-cheque: si vinieron varios, los creamos todos. Si solo
        # uno, mismo flujo de siempre. TMT 2026-05-15.
        #
        # TMT 2026-05-15 (batch atómico): si esta operación va a generar >1
        # mov_doble (multi-cheque o multi-factura), generamos un batch_id
        # UUID y lo propagamos a TODOS los crear()/aplicar_a_factura().
        # El reverso en /historial los revierte juntos. Además, abrimos UNA
        # transacción única — si cualquier paso falla, rollback total (no
        # quedan cheques colgados sin aplicaciones).
        import uuid as _uuid

        es_batch = len(cheques_in) > 1 or len(aplicaciones_pre or []) > 1
        batch_id = str(_uuid.uuid4()) if es_batch else None

        cheques_creados: list[dict] = []
        n_aplicaciones = 0

        with db.tx() as conn:
            for ch_in in cheques_in:
                # TMT 2026-05-27 — banco por cheque (no_banco from ch_in)
                _ch_no_banco = ch_in.get("no_banco") if ch_in.get("no_banco") is not None else no_banco
                _ch_es_anticipo = es_anticipo or (_ch_no_banco == 97)
                ch = queries.crear(
                    fecha=fecha,
                    fechad=ch_in.get("fechad") or fechad,  # por cheque
                    fecha_recibido=fecha_recibido,
                    codigo_cli=codigo_cli,
                    no_cheque=ch_in["no_cheque"],
                    importe=ch_in["importe"],
                    no_banco=_ch_no_banco,
                    banco_texto=banco_texto,
                    prov=prov,
                    # TMT 2026-05-20 — stat seleccionado en el dropdown
                    # (Z/P/D/B/X/1/2). queries.crear lo respeta si !=Z.
                    stat=ch_in.get("stat") or "Z",
                    clave=clave,
                    es_anticipo=es_anticipo,
                    # TMT 2026-05-26 — doc_banco por cheque (N° comprobante/depósito).
                    doc_banco=ch_in.get("doc_banco"),
                    usuario=usuario,
                    batch_id=batch_id,
                    conn=conn,
                )
                # TMT 2026-05-15: queries.crear devuelve {id_cheque, no_cheque}
                # pero NO el importe — lo agrego acá para usar después en el
                # FIFO de aplicaciones y el flash del total.
                if isinstance(ch, dict):
                    ch["importe"] = float(ch_in["importe"] or 0)
                cheques_creados.append(ch)

            # Aplicar a facturas si hubo distribución inline.
            # TMT 2026-05-15: con multi-cheque distribuimos FIFO — el primer
            # cheque cubre las primeras facturas hasta agotarse, después el
            # segundo, etc. Cada aplicación queda en chequesxfact con su
            # id_cheque correspondiente. Todo dentro de la MISMA tx — si
            # falla una, rollback total (los cheques tampoco se crean).
            if aplicaciones_pre:
                por_cheque: dict[int, list[dict]] = {int(c["id_cheque"]): [] for c in cheques_creados}
                cheques_restantes = [
                    {"id_cheque": int(c["id_cheque"]), "restante": float(c.get("importe") or 0)}
                    for c in cheques_creados
                ]
                for ap in aplicaciones_pre:
                    rest_factura = float(ap["importe"])
                    if abs(rest_factura) < 0.005:
                        continue
                    i = 0
                    while abs(rest_factura) > 0.005 and i < len(cheques_restantes):
                        c = cheques_restantes[i]
                        if rest_factura > 0:
                            # Positiva: saltar cheques agotados.
                            if c["restante"] <= 0.005:
                                i += 1
                                continue
                            aplicar = min(rest_factura, c["restante"])
                        else:
                            # Crédito: absorbemos entero en el cheque actual.
                            aplicar = rest_factura
                        por_cheque[c["id_cheque"]].append(
                            {
                                "id_fact": ap["id_fact"],
                                "importe": aplicar,
                                "forzar_stat": ap.get("forzar_stat") or "",
                            }
                        )
                        c["restante"] -= aplicar
                        rest_factura -= aplicar
                        if rest_factura > 0 and c["restante"] <= 0.005:
                            i += 1
                    # Tolerancia de rounding: data legacy del DBF deja sub-pesos
                    # raros (0.55 típicos por conversiones COP→USD del Clipper).
                    # Reglas (TMT 2026-05-16):
                    #   - Sin T usado: tolerancia de $1 (absorbe rounding, no más).
                    #   - Con T usado: el usuario marcó explícitamente "aplicá
                    #     todo lo que quede" → absorbemos CUALQUIER diferencia
                    #     en el último FIFO. La dueña pidió no preocuparse por
                    #     los centavos cuando usó T.
                    t_used = (request.form.get("aplicar_t_used") or "").strip() == "1"
                    TOLERANCIA_ROUNDING = 1e9 if t_used else 1.00
                    if abs(rest_factura) > TOLERANCIA_ROUNDING:
                        raise ValueError(
                            f"No pude distribuir {rest_factura:.2f} de la "
                            f"factura {ap['id_fact']} — los cheques no alcanzan."
                        )
                    elif abs(rest_factura) > 0.005:
                        # Absorber el delta en la ÚLTIMA aplicación FIFO de
                        # esta factura (la del último cheque que entró). El
                        # signo del ajuste sigue el signo del rest_factura:
                        # rest>0 (shortage) → bajamos el saldo de la factura,
                        # rest<0 (excess) → idem pero al revés. En la práctica
                        # significa "el último cheque cubre los 0.55 que
                        # faltaban / restamos los 0.55 que sobraban".
                        for c in reversed(cheques_restantes):
                            ult = next(
                                (
                                    x
                                    for x in por_cheque.get(c["id_cheque"], [])
                                    if x["id_fact"] == ap["id_fact"]
                                ),
                                None,
                            )
                            if ult is not None:
                                ult["importe"] += rest_factura
                                rest_factura = 0
                                break
                # Aplicar cada batch al cheque correspondiente, en LA MISMA tx.
                for id_ch, aps in por_cheque.items():
                    if not aps:
                        continue
                    r = queries.aplicar_a_factura(
                        id_cheque=id_ch,
                        aplicaciones=aps,
                        usuario=usuario,
                        batch_id=batch_id,
                        conn=conn,
                    )
                    n_aplicaciones += int(r.get("n") or 0)

        ch = cheques_creados[0]  # primero — usado abajo para redirect

        # Mensajes según cantidad creada
        if len(cheques_creados) > 1:
            total_creado = sum(float(c.get("importe") or 0) for c in cheques_creados)
            nums = ", ".join(f"N° {c.get('no_cheque')}" for c in cheques_creados)
            sufijo = ""
            if es_anticipo:
                # Multi-cheque + anticipo: cada cheque generó su propio espejo.
                # TMT 2026-05-15.
                n_espejos = sum(
                    1 for c in cheques_creados if isinstance(c, dict) and c.get("id_cheque_anticipo")
                )
                if n_espejos:
                    sufijo = (
                        f" Cada uno generó su espejo negativo de anticipo ({n_espejos} espejos en total)."
                    )
            elif n_aplicaciones > 0:
                # Multi-cheque con aplicaciones distribuidas FIFO.
                # TMT 2026-05-15: antes el flash sólo decía "X cheques creados"
                # y no mencionaba las aplicaciones, dejando dudas de si se
                # habían aplicado o no.
                sufijo = f" Se distribuyeron {n_aplicaciones} aplicación(es) FIFO entre los cheques."
            flash(
                f"{len(cheques_creados)} cheques creados en cartera "
                f"(total $ {total_creado:,.2f}): {nums}.{sufijo}",
                "ok",
            )
            return redirect(url_for("cheques.lista", q=codigo_cli))
        if es_anticipo and ch.get("id_cheque_anticipo"):
            flash(
                f"Cheque N° {ch.get('no_cheque')} creado como ANTICIPO. Se generó "
                f"un espejo negativo (id #{ch['id_cheque_anticipo']}) que se aplicará "
                "a futuras facturas del cliente.",
                "ok",
            )
        elif n_aplicaciones > 0:
            flash(
                f"Cheque N° {ch.get('no_cheque')} creado y aplicado a {n_aplicaciones} factura(s).",
                "ok",
            )
        else:
            flash(f"Cheque N° {ch.get('no_cheque')} creado en cartera.", "ok")
        return redirect(url_for("cheques.detalle", id_cheque=ch["id_cheque"]))
    except ValueError as e:
        errores.append(str(e))
        return render_template(
            "cheques/nuevo.html",
            form=form,
            errores=errores,
            bancos=_bancos(),
            clientes_datalist=clientes_datalist,
        ), 400
    except Exception as e:  # noqa: BLE001
        # TMT 2026-05-15: temporariamente mostramos el detalle crudo
        # para diagnosticar el bug de multi-cheque. Volver a humanize()
        # una vez que esté estable.
        import logging

        logging.getLogger(__name__).exception("cheques.nuevo falló")
        errores.append(f"[DEBUG] {type(e).__name__}: {e}")
        return render_template(
            "cheques/nuevo.html",
            form=form,
            errores=errores,
            bancos=_bancos(),
            clientes_datalist=clientes_datalist,
        ), 500


@cheques_bp.route("/cheques/_api/facturas-pendientes/<codigo_cli>")
@requiere_login
@requiere_permiso("cheques.ver")
def api_facturas_pendientes(codigo_cli: str):
    """JSON con facturas abiertas del cliente — alimenta la tabla inline
    del form de nuevo cheque.

    Devuelve sólo lo que la UI necesita: id, numf, fecha, vencimiento,
    importe, saldo. Ordenado FIFO (vencimiento ascendente).
    """
    codigo_cli = (codigo_cli or "").strip().upper()
    if not codigo_cli:
        return {"facturas": []}, 400
    rows = queries.facturas_pendientes(codigo_cli, limite=500)
    # TMT 2026-05-15: banco más usado por este cliente — para precargar
    # el select "Banco emisor" en el form. Excluimos anulados y nos
    # quedamos con el no_banco con más cheques del cliente.
    banco_sugerido = None
    try:
        row_b = db.fetch_one(
            """
            SELECT no_banco, COUNT(*) AS n
              FROM scintela.cheque
             WHERE codigo_cli = %s
               AND COALESCE(stat, '') NOT IN ('X', 'Y')
               AND no_banco IS NOT NULL
             GROUP BY no_banco
             ORDER BY n DESC, MAX(fecha) DESC
             LIMIT 1
            """,
            (codigo_cli,),
        )
        if row_b and row_b.get("no_banco") is not None:
            banco_sugerido = int(row_b["no_banco"])
    except Exception:
        banco_sugerido = None
    return {
        "codigo_cli": codigo_cli,
        "banco_sugerido": banco_sugerido,
        "facturas": [
            {
                "id_factura": int(r["id_factura"]),
                "numf": r.get("numf"),
                "numf_completo": r.get("numf_completo") or "",
                "fecha": r["fecha"].isoformat() if r.get("fecha") else None,
                "vencimiento": r["vencimiento"].isoformat() if r.get("vencimiento") else None,
                "importe": float(r.get("importe") or 0),
                "abono": float(r.get("abono") or 0),
                "saldo": float(r.get("saldo") or 0),
                "stat": r.get("stat") or "",
            }
            for r in rows
        ],
    }


@cheques_bp.route("/cheques/<int:id_cheque>/aplicar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("cheques.aplicar")
def aplicar(id_cheque: int):
    """Aplicar un cheque a facturas abiertas del mismo cliente.

    GET: muestra facturas pendientes con importes editables (pre-FIFO).
    POST: recorre inputs `aplicar[id_fact]` y delega a queries.aplicar_a_factura.
    """
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    stat_ch = (ch.get("stat") or "").upper()
    # Guard del view: paridad con queries.STATS_APLICABLES. TMT 2026-05-14 (#26).
    if stat_ch not in queries.STATS_APLICABLES:
        flash(
            f"Este cheque está en stat='{stat_ch}' — no se puede aplicar a facturas. "
            f"Sólo aplicable desde {queries.STATS_APLICABLES} (cartera/postergado/Daniela).",
            "warn",
        )
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))

    pendientes = queries.facturas_pendientes(ch["codigo_cli"])

    if request.method == "GET":
        restante = float(ch["importe"] or 0)
        pre = {}
        for f in pendientes:
            s = float(f["saldo"] or 0)
            usar = min(s, restante)
            pre[f["id_factura"]] = usar if usar > 0 else 0
            restante -= usar
            if restante <= 0:
                break
        return render_template(
            "cheques/aplicar.html",
            ch=ch,
            pendientes=pendientes,
            pre=pre,
            errores=[],
        )

    errores: list[str] = []
    aplicaciones = []
    for f in pendientes:
        raw = request.form.get(f"aplicar[{f['id_factura']}]")
        imp = parse_monto(raw)
        if imp is None or imp <= 0:
            continue
        aplicaciones.append({"id_fact": f["id_factura"], "importe": float(imp)})

    if not aplicaciones:
        errores.append("No indicaste ningún importe a aplicar.")
        return render_template(
            "cheques/aplicar.html",
            ch=ch,
            pendientes=pendientes,
            pre={},
            errores=errores,
        ), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.aplicar_a_factura(
            id_cheque=id_cheque,
            aplicaciones=aplicaciones,
            usuario=usuario,
        )
        flash(
            f"Cheque aplicado a {r['n']} factura(s), total {r['total_aplicado']:.2f}.",
            "ok",
        )
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))
    except ValueError as e:
        errores.append(str(e))
        return render_template(
            "cheques/aplicar.html",
            ch=ch,
            pendientes=pendientes,
            pre={},
            errores=errores,
        ), 400
    except Exception as e:
        errores.append(f"No pude aplicar el cheque: {e}")
        return render_template(
            "cheques/aplicar.html",
            ch=ch,
            pendientes=pendientes,
            pre={},
            errores=errores,
        ), 500


@cheques_bp.route("/cheques/<int:id_cheque>/confirmar-reverso", methods=["GET"])
@requiere_login
@requiere_permiso("cheques.anular")
def confirmar_reverso(id_cheque: int):
    """Paso 1 del 2-step: confirmar 'Sin fondos' o 'Reversar (me confundí)'.

    TMT 2026-05-24 — Dueña: 'no es lo mismo reversar que rebote'. La
    distinción es por stat actual:
      - B/A/1/2 → SIN FONDOS (el cheque ya estuvo en circulación bancaria
                  y rebotó). Evento malo, queda anotado en cliente.
      - Z/D/P/V → REVERSAR (te confundiste al cargar). Admin undo, sin
                  afectar al cliente.
    """
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    if ch.get("stat") == "R":
        flash("El cheque ya está reversado.", "warn")
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))
    stat_prev = ch.get("stat") or ""
    es_rebote = stat_prev in queries.STATS_REBOTE_REAL
    no_ch = ch.get("no_cheque") or f"#{id_cheque}"
    importe = ch.get("importe") or 0
    cliente = ch.get("codigo_cli", "")
    detalle = {
        "N° cheque": no_ch,
        "Fecha": (ch.get("fecha").strftime("%d/%m/%Y") if ch.get("fecha") else "—"),
        "Cliente": cliente,
        "Importe": f"$ {importe}",
        "Estado actual": stat_prev,
    }
    if es_rebote:
        stat_destino = "3" if stat_prev in ("1", "2") else "1"
        titulo = f"Marcar SIN FONDOS — cheque {no_ch}"
        mensaje = (
            f"El cheque N° {no_ch} por $ {importe} REBOTÓ — el cliente no "
            "tenía fondos. Esto es un EVENTO MALO."
        )
        detalle["Qué va a pasar"] = (
            f"(1) Cheque pasa stat '{stat_prev}' → '{stat_destino}' (rebotado). "
            "(2) Se restauran las facturas que cubría — vuelven a cartera. "
            f"(3) Se anota [REBOTE] en la observación del cliente {cliente} "
            "(el STOP lo decidís vos)."
        )
        confirm_label = "Confirmar SIN FONDOS"
    else:
        titulo = f"Reversar (me confundí) — cheque {no_ch}"
        mensaje = (
            f"Te equivocaste cargando este cheque N° {no_ch} por $ {importe}. "
            "Esto es un UNDO administrativo — no afecta al cliente."
        )
        detalle["Qué va a pasar"] = (
            f"(1) Cheque pasa stat '{stat_prev}' → 'X' (eliminado por error). "
            "(2) Se restauran las facturas que cubría — vuelven a cartera. "
            f"(3) NO se toca al cliente {cliente} — no es un rebote."
        )
        confirm_label = "Confirmar REVERSAR"
    return render_template(
        "_confirmar_accion.html",
        titulo=titulo,
        mensaje=mensaje,
        detalle_registro=detalle,
        accion_url=url_for("cheques.reversar", id_cheque=id_cheque),
        volver_url=url_for("cheques.detalle", id_cheque=id_cheque),
        motivo_requerido=True,
        confirm_label=confirm_label,
    )


@cheques_bp.route("/cheques/<int:id_cheque>/reversar", methods=["POST"])
@requiere_login
@requiere_permiso("cheques.anular")
def reversar(id_cheque: int):
    motivo = (request.form.get("motivo") or "").strip()
    # TMT 2026-05-21 dueña: motivo opcional sin requerir.
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.reversar(id_cheque=id_cheque, motivo=motivo, usuario=usuario)
        # TMT 2026-05-24 — vocabulario claro: "sin fondos" si fue rebote
        # real (banco rechazó), "reversado" si fue undo administrativo.
        n_aplic = r["reversadas"]
        if r.get("es_rebote_real"):
            base = (
                f"Cheque marcado como SIN FONDOS. Se anotó el rebote en "
                f"la observación del cliente {r['codigo_cli']}. "
                f"Se restauraron {n_aplic} factura(s) a cartera."
            )
        else:
            base = (
                f"Cheque REVERSADO (undo administrativo). "
                f"Se restauraron {n_aplic} factura(s) a cartera. "
                "No se tocó al cliente."
            )
        flash(base, "ok")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash_exc("No pude reversar el cheque", e)
    return redirect(url_for("cheques.detalle", id_cheque=id_cheque))


@cheques_bp.route("/cheques/<int:id_cheque>/postergar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("cheques.crear")
def postergar(id_cheque: int):
    """Postergar un cheque: cambia fechad y deja traza de cuándo y por qué.

    Sólo cheques en stat='Z' (cartera) o ya 'P' (re-postergación) se pueden
    postergar. La query levanta ValueError si el origen es otro.

    GET: muestra form con la fechad actual y un input para la nueva fecha.
    POST: aplica el cambio y redirige al detalle.
    """
    # TMT 2026-05-19 v8 — wrap en try/except defensivo para evitar 502
    # cuando algo inesperado revienta el worker. Cualquier excepción no
    # atrapada antes (queries.por_id, parse_date, period guard) cae acá
    # con flash + redirect en lugar de matar el process.
    import logging

    log = logging.getLogger("cheques.postergar")

    try:
        next_url = (request.form.get("next") or "").strip()
    except Exception:
        next_url = ""
    es_next_local = next_url.startswith("/") and not next_url.startswith("//") and "://" not in next_url

    def _fallback_redirect():
        if es_next_local:
            return redirect(next_url)
        return redirect(url_for("cheques.lista"))

    try:
        ch = queries.por_id(id_cheque)
    except Exception as e:
        log.exception("por_id falló para cheque %s", id_cheque)
        flash_exc("No pude cargar el cheque", e)
        return _fallback_redirect()

    if not ch:
        flash(f"Cheque {id_cheque} no existe.", "warn")
        return _fallback_redirect()

    errores: list[str] = []
    form: dict = {}

    if request.method == "GET":
        try:
            return render_template("cheques/postergar.html", ch=ch, errores=errores, form=form)
        except Exception as e:
            log.exception("render postergar.html GET falló")
            flash_exc("No pude mostrar el form", e)
            return _fallback_redirect()

    try:
        nueva_fechad = parse_date(request.form.get("nueva_fechad"))
    except Exception:
        nueva_fechad = None
    motivo = (request.form.get("motivo") or "").strip()

    if nueva_fechad is None:
        errores.append("Nueva fecha de depósito inválida.")

    form.update(
        {
            "nueva_fechad": request.form.get("nueva_fechad"),
            "motivo": motivo,
        }
    )

    if errores:
        # Inline (popover): no tirar wizard, flash + back al listado.
        if es_next_local:
            for err in errores:
                flash(err, "warn")
            return redirect(next_url)
        try:
            return render_template("cheques/postergar.html", ch=ch, errores=errores, form=form), 400
        except Exception as e:
            log.exception("render postergar.html POST/errores falló")
            flash_exc("No pude mostrar el form", e)
            return _fallback_redirect()

    try:
        usuario = (g.user or {}).get("username", "web") if hasattr(g, "user") else "web"
        queries.postergar(
            id_cheque=id_cheque,
            nueva_fechad=nueva_fechad,
            motivo=motivo,
            usuario=usuario,
        )
        flash(
            f"Cheque postergado al {nueva_fechad.strftime('%d/%m/%Y')}.",
            "ok",
        )
    except ValueError as e:
        if es_next_local:
            flash(str(e), "warn")
            return redirect(next_url)
        errores.append(str(e))
        try:
            return render_template("cheques/postergar.html", ch=ch, errores=errores, form=form), 400
        except Exception as e2:
            log.exception("render postergar.html POST/ValueError falló")
            flash_exc("No pude mostrar el form", e2)
            return _fallback_redirect()
    except Exception as e:
        log.exception("queries.postergar falló para cheque %s", id_cheque)
        flash_exc("No pude postergar el cheque", e)
        return _fallback_redirect()

    # Éxito → redirect.
    if es_next_local:
        return redirect(next_url)
    return redirect(url_for("cheques.detalle", id_cheque=id_cheque))


@cheques_bp.route("/cheques/<int:id_cheque>/desaplicar/<int:id_factura>", methods=["GET"])
@requiere_login
@requiere_permiso("cheques.aplicar")
def confirmar_desaplicar(id_cheque: int, id_factura: int):
    """Wizard para deshacer la aplicación de un cheque a una factura específica.

    Diferente al "reversar cheque entero": solo deshace ESA aplicación,
    deja el cheque en cartera para aplicarse a otra factura. TMT 2026-05-13.
    """
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    f = db.fetch_one(
        "SELECT id_factura, numf, importe, saldo, abono, codigo_cli "
        "FROM scintela.factura WHERE id_factura = %s",
        (id_factura,),
    )
    if not f:
        abort(404)
    aplicaciones = (
        db.fetch_all(
            """
        SELECT importe FROM scintela.chequesxfact
         WHERE id_cheque = %s AND id_fact = %s
        """,
            (id_cheque, id_factura),
        )
        or []
    )
    if not aplicaciones:
        flash(
            f"No hay aplicaciones de cheque #{id_cheque} a factura #{id_factura}.",
            "warn",
        )
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))
    total = sum(float(a.get("importe") or 0) for a in aplicaciones)
    detalle = {
        "Cheque": f"#{id_cheque} (N° {ch.get('no_cheque') or '—'})",
        "Factura": f"#{f.get('numf') or id_factura} ({f.get('codigo_cli') or '—'})",
        "Importe a desaplicar": f"$ {total:,.2f}",
        "Saldo factura actual": f"$ {f.get('saldo') or 0:,.2f}",
        "Abono factura actual": f"$ {f.get('abono') or 0:,.2f}",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Desaplicar cheque {ch.get('no_cheque') or '#' + str(id_cheque)} de factura #{f.get('numf') or id_factura}",
        mensaje=(
            "Vas a deshacer SOLO esta aplicación. El cheque queda en su estado actual "
            "y la factura se reabre por el monto desaplicado."
        ),
        detalle_registro=detalle,
        accion_url=url_for("cheques.desaplicar", id_cheque=id_cheque, id_factura=id_factura),
        volver_url=url_for("cheques.detalle", id_cheque=id_cheque),
        motivo_requerido=True,
        motivo_obligatorio=False,
        confirm_label="Confirmar desaplicación",
    )


@cheques_bp.route("/cheques/<int:id_cheque>/desaplicar/<int:id_factura>", methods=["POST"])
@requiere_login
@requiere_permiso("cheques.aplicar")
def desaplicar(id_cheque: int, id_factura: int):
    motivo = (request.form.get("motivo") or "").strip()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.desaplicar_factura(
            id_cheque=id_cheque,
            id_factura=id_factura,
            motivo=motivo,
            usuario=usuario,
        )
        flash(
            f"Cheque #{id_cheque} desaplicado de factura #{id_factura} "
            f"(- $ {r['importe_desaplicado']:.2f}). "
            f"Factura ahora: saldo $ {r['saldo_factura_post']:.2f}, stat='{r['stat_factura_post']}'.",
            "ok",
        )
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude desaplicar", e)
    return redirect(url_for("cheques.detalle", id_cheque=id_cheque))


@cheques_bp.route("/cheques/<int:id_cheque>/confirmar-reverso-endoso", methods=["GET"])
@requiere_login
@requiere_permiso("cheques.aplicar")
def confirmar_reverso_endoso(id_cheque: int):
    """Wizard de 2 pasos para reversar un endoso de cheque a proveedor.

    El reverso:
      - Anula la compra creada al endosar (stat='Y').
      - Restaura el cheque a stat='Z' (cartera).
      - Limpia prov, fechaout.
      - Registra mov_doble del reverso linkeado al endoso original.
    TMT 2026-05-13.
    """
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    if (ch.get("stat") or "").upper() != "E":
        flash(
            f"Cheque {id_cheque} no está endosado (stat='{ch.get('stat')}'). "
            "Sólo se puede reversar el endoso desde stat='E'.",
            "warn",
        )
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))
    detalle = {
        "N°": ch.get("no_cheque") or f"#{id_cheque}",
        "Importe": f"$ {ch.get('importe') or 0:,.2f}",
        "Cliente original": f"{ch.get('codigo_cli') or '—'} — {ch.get('cliente') or ''}",
        "Endosado a": ch.get("prov") or "—",
        "Fecha endoso": (ch.get("fechaout").strftime("%d/%m/%Y") if ch.get("fechaout") else "—"),
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Reversar endoso del cheque {ch.get('no_cheque') or '#' + str(id_cheque)}",
        mensaje=(
            f"Vas a deshacer el endoso del cheque a {ch.get('prov') or '—'}. "
            "Se va a anular la compra creada al endosar y el cheque vuelve a estar "
            "EN CARTERA (stat='Z'). Todo se hace en una sola transacción."
        ),
        detalle_registro=detalle,
        accion_url=url_for("cheques.reversar_endoso", id_cheque=id_cheque),
        volver_url=url_for("cheques.detalle", id_cheque=id_cheque),
        motivo_requerido=True,
        motivo_obligatorio=False,  # opcional — la dueña puede dejarlo vacío
        confirm_label="Confirmar reverso del endoso",
    )


@cheques_bp.route("/cheques/<int:id_cheque>/reversar-endoso", methods=["POST"])
@requiere_login
@requiere_permiso("cheques.aplicar")
def reversar_endoso(id_cheque: int):
    """Ejecuta el reverso del endoso. Ver `queries.reversar_endoso`."""
    motivo = (request.form.get("motivo") or "").strip()
    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.reversar_endoso(
            id_cheque=id_cheque,
            motivo=motivo,
            usuario=usuario,
        )
        msg = f"Endoso del cheque {id_cheque} reversado. Cheque vuelve a CARTERA (stat='{r['stat_nuevo']}'). "
        if r.get("id_compra_anulada"):
            msg += f"Compra #{r['id_compra_anulada']} anulada."
        else:
            msg += "(No se encontró compra hermana para anular — revisar manualmente)."
        flash(msg, "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude reversar el endoso", e)
    return redirect(url_for("cheques.detalle", id_cheque=id_cheque))


@cheques_bp.route("/cheques/<int:id_cheque>/endosar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("cheques.aplicar")
def endosar(id_cheque: int):
    """Endosar un cheque a un proveedor.

    GET: muestra wizard con cheque + selector de proveedor + concepto.
    POST: ejecuta queries.endosar en una sola transacción (cheque pasa a
    stat='E', se crea compra al proveedor pagada por endoso).
    """
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    stat = (ch.get("stat") or "").upper()
    if stat not in queries.STATS_ENDOSABLES:
        flash(
            f"Cheque en stat='{stat}' no se puede endosar. "
            f"Sólo desde {queries.STATS_ENDOSABLES} (cartera, postergado, Daniela).",
            "warn",
        )
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))

    # Cargar proveedores activos para el datalist + select.
    try:
        proveedores = (
            db.fetch_all(
                "SELECT codigo_prov, COALESCE(nombre,'') AS nombre, "
                "       COALESCE(tipo,'') AS tipo "
                "FROM scintela.proveedor "
                "WHERE COALESCE(activo, '1') NOT IN ('0', 'N') "
                "ORDER BY codigo_prov"
            )
            or []
        )
    except Exception:
        proveedores = []

    errores: list[str] = []
    form: dict = {
        "codigo_prov": "",
        "concepto": "",
        "tipo_compra": "C",
        "fecha": today_ec().isoformat(),
    }
    # Restaurar campos via query string — si veníamos de crear un proveedor
    # nuevo, /proveedores/nuevo nos redirige con los datos del form
    # anterior en el query. TMT 2026-05-13.
    if request.method == "GET":
        for k in ("codigo_prov", "concepto", "tipo_compra", "fecha"):
            if request.args.get(k):
                form[k] = request.args.get(k)

    if request.method == "POST":
        codigo_prov = (request.form.get("codigo_prov") or "").strip().upper()
        concepto = (request.form.get("concepto") or "").strip()
        tipo_compra = (request.form.get("tipo_compra") or "C").strip().upper()[:1]
        fecha = parse_date(request.form.get("fecha")) or today_ec()

        form.update(
            {
                "codigo_prov": codigo_prov,
                "concepto": concepto,
                "tipo_compra": tipo_compra,
                "fecha": request.form.get("fecha") or fecha.isoformat(),
            }
        )

        if not codigo_prov:
            errores.append("Proveedor requerido.")
        # Si el proveedor no existe → flujo guiado a /proveedores/nuevo,
        # mismo patrón que compras.nueva. TMT 2026-05-13.
        elif not db.fetch_one(
            "SELECT 1 AS x FROM scintela.proveedor WHERE codigo_prov = %s",
            (codigo_prov,),
        ):
            _permisos = getattr(g, "permisos", set()) or set()
            if "proveedores.crear" in _permisos or "*" in _permisos:
                from urllib.parse import urlencode

                restore_args = {
                    "codigo_prov": codigo_prov,
                    "concepto": concepto or "",
                    "tipo_compra": tipo_compra or "",
                    "fecha": request.form.get("fecha") or "",
                }
                restore_args = {k: v for k, v in restore_args.items() if v}
                next_url = url_for("cheques.endosar", id_cheque=id_cheque) + "?" + urlencode(restore_args)
                flash(
                    f"El proveedor {codigo_prov} no existe — completá los datos "
                    "para crearlo y después seguís con el endoso.",
                    "warning",
                )
                return redirect(url_for("proveedores.nuevo", codigo=codigo_prov, next=next_url))
            errores.append(f"El proveedor {codigo_prov!r} no existe.")
        if errores:
            return render_template(
                "cheques/endosar.html",
                ch=ch,
                proveedores=proveedores,
                form=form,
                errores=errores,
            ), 400

        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.endosar(
                id_cheque=id_cheque,
                codigo_prov=codigo_prov,
                concepto=concepto,
                tipo_compra=tipo_compra,
                fecha=fecha,
                usuario=usuario,
            )
            flash(
                f"Cheque endosado a {r['codigo_prov']} ({r['proveedor_nombre']}). "
                f"Se creó la compra N° {r['numero_compra']} por $ {r['importe']:.2f}.",
                "ok",
            )
            return redirect(url_for("cheques.detalle", id_cheque=id_cheque))
        except ValueError as e:
            errores.append(str(e))
            return render_template(
                "cheques/endosar.html",
                ch=ch,
                proveedores=proveedores,
                form=form,
                errores=errores,
            ), 400
        except Exception as e:
            flash_exc("No pude endosar el cheque", e)
            return redirect(url_for("cheques.detalle", id_cheque=id_cheque))

    return render_template(
        "cheques/endosar.html",
        ch=ch,
        proveedores=proveedores,
        form=form,
        errores=errores,
    )


@cheques_bp.route("/cheques/boleta")
@requiere_login
@requiere_permiso("cheques.ver")
def boleta_deposito():
    """Boleta de depósito impresa — replica BANCOS.PRG:1250-1359 (BOLEPICH/BOLEIN).

    Levanta el depósito que se hizo a `no_banco` en `fecha`, agrupando todos
    los cheques que fueron al banco ese día. Si no se pasan params, default
    a (hoy, banco 1 = Pichincha).

    Query params:
      - fecha=YYYY-MM-DD  (default: hoy)
      - no_banco=N        (default: 1 = Pichincha)
    """
    from datetime import datetime as _dt

    fecha_str = (request.args.get("fecha") or "").strip()
    try:
        fecha = _dt.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else today_ec()
    except ValueError:
        fecha = today_ec()
    # TMT 2026-05-15 (re-audit H5): NO hardcodear no_banco=1 — en data 2026
    # Pichincha es no_banco=10. Resolvemos dinámicamente igual que
    # depositar_lote (matching por nombre).
    no_banco = parse_int(request.args.get("no_banco"))
    if not no_banco:
        import contextlib as _ctx

        all_bancos = []
        with _ctx.suppress(Exception):
            all_bancos = (
                db.fetch_all(
                    "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco ORDER BY no_banco"
                )
                or []
            )
        pichincha = [b for b in all_bancos if "PICHINC" in (b.get("nombre") or "").upper()]
        if pichincha:
            no_banco = int(pichincha[0]["no_banco"])
        elif all_bancos:
            # Fallback: primer banco operativo no-legacy
            fallback = [
                b
                for b in all_bancos
                if "INTER" not in (b.get("nombre") or "").upper()
                and "EFECTIVO" not in (b.get("nombre") or "").upper()
                and "UKN" not in (b.get("nombre") or "").upper()
                and "ANTIC" not in (b.get("nombre") or "").upper()
            ][:1]
            if fallback:
                no_banco = int(fallback[0]["no_banco"])
        no_banco = no_banco or 1  # último recurso, mejor que crash
    try:
        boleta = queries.boleta_deposito(fecha=fecha, no_banco=no_banco)
        error = None
    except ValueError as e:
        boleta = None
        error = str(e)
    except Exception as e:  # noqa: BLE001
        boleta = None
        error = f"Error inesperado: {e}"
    # Lista dinámica de bancos para el dropdown — antes el template tenía
    # <option value="1"> hardcoded que no existe en la data real (Pichincha
    # es no_banco=10, Internacional 32). TMT 2026-05-16.
    bancos_dropdown = (
        db.fetch_all(
            "SELECT no_banco, COALESCE(nombre,'') AS nombre "
            "FROM scintela.banco WHERE EXISTS ("
            "  SELECT 1 FROM scintela.transacciones_bancarias t WHERE t.no_banco = scintela.banco.no_banco"
            ") ORDER BY no_banco"
        )
        or []
    )
    return render_template(
        "cheques/boleta_deposito.html",
        boleta=boleta,
        fecha=fecha,
        no_banco=no_banco,
        bancos_dropdown=bancos_dropdown,
        error=error,
    )


@cheques_bp.route("/cheques/<int:id_cheque>/reemplazar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("cheques.crear")
def reemplazar(id_cheque: int):
    """Cheque XX reemplazo — replica BANCOS.PRG:266-305.

    El cliente trae un cheque nuevo para reemplazar uno vivo en cartera.
    Sólo se admite desde stat Z/P/D. Migra aplicaciones a facturas vivas
    al cheque nuevo.

    GET: muestra detalle del viejo + form (nuevo N° / nuevo importe / motivo).
    POST: ejecuta queries.reemplazar() y redirige al detalle del nuevo cheque.
    """
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    stat = (ch.get("stat") or "").upper()
    if stat not in queries.STATS_APLICABLES:
        flash(
            f"Cheque en stat='{stat}' no se puede reemplazar. "
            f"Sólo desde {queries.STATS_APLICABLES} (cartera/postergado/Daniela). "
            "Si rebotó, usá 'Reversar (rebote)'. Si fue endosado, primero "
            "reversá el endoso.",
            "warn",
        )
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))

    errores: list[str] = []
    form: dict = {
        "nuevo_no_cheque": "",
        "nuevo_importe": str(ch.get("importe") or ""),
        "motivo": "",
    }

    if request.method == "GET":
        return render_template(
            "cheques/reemplazar.html",
            ch=ch,
            form=form,
            errores=errores,
        )

    nuevo_no_cheque = (request.form.get("nuevo_no_cheque") or "").strip()
    nuevo_importe_raw = request.form.get("nuevo_importe")
    nuevo_importe = parse_monto(nuevo_importe_raw)
    motivo = (request.form.get("motivo") or "").strip()

    form.update(
        {
            "nuevo_no_cheque": nuevo_no_cheque,
            "nuevo_importe": nuevo_importe_raw or "",
            "motivo": motivo,
        }
    )

    if not nuevo_no_cheque:
        errores.append("Número de cheque nuevo requerido.")
    if nuevo_importe is None or nuevo_importe <= 0:
        errores.append("Importe inválido (debe ser positivo).")

    if errores:
        return render_template(
            "cheques/reemplazar.html",
            ch=ch,
            form=form,
            errores=errores,
        ), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.reemplazar(
            id_cheque_viejo=id_cheque,
            nuevo_no_cheque=nuevo_no_cheque,
            nuevo_importe=float(nuevo_importe),
            motivo=motivo,
            usuario=usuario,
        )
        flash(
            f"Cheque #{id_cheque} reemplazado. Nuevo cheque N° {r['no_cheque_nuevo']} "
            f"(id #{r['id_cheque_nuevo']}) por $ {r['importe_nuevo']:.2f}. "
            f"{r['aplicaciones_migradas']} aplicacion(es) migradas.",
            "ok",
        )
        return redirect(url_for("cheques.detalle", id_cheque=r["id_cheque_nuevo"]))
    except ValueError as e:
        errores.append(str(e))
        return render_template(
            "cheques/reemplazar.html",
            ch=ch,
            form=form,
            errores=errores,
        ), 400
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude reemplazar el cheque", e)
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))


@cheques_bp.route("/cheques/<int:id_cheque>")
@requiere_login
@requiere_permiso("cheques.ver")
def detalle(id_cheque: int):
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    aplicaciones = queries.aplicaciones(id_cheque)
    depositos = queries.depositos(id_cheque)
    total_aplicado = sum(float(a["aplicado"] or 0) for a in aplicaciones)
    # Cheques hijo (espejos de anticipo) — TMT 2026-05-14 (#28).
    hijos = queries.hijos(id_cheque)
    try:
        from modules.recientes import queries as rec

        rec.registrar(
            "cheque",
            id_cheque,
            etiqueta=f"Cheque {ch.get('no_cheque') or id_cheque} · {ch.get('codigo_cli', '')}",
        )
    except Exception:  # noqa: BLE001
        # TMT 2026-05-15 (re-audit M2): no rompemos el detalle si "recientes"
        # falla — es UX puro — pero LOGEAMOS el stack para no perder bugs.
        import logging as _lg

        _lg.getLogger(__name__).exception(
            "recientes.registrar(cheque, %s) falló",
            id_cheque,
        )
    return render_template(
        "cheques/detalle.html",
        ch=ch,
        aplicaciones=aplicaciones,
        depositos=depositos,
        total_aplicado=total_aplicado,
        hijos=hijos,
    )


# TMT 2026-05-27 dueña: 'Cuando ponga editar un cheque me deje desde las
# lineas. No hace falta ir a una nueva pantalla. demasiado tramite.'
# La pantalla /cheques/<id>/editar fue ELIMINADA. Reemplazada por inline
# edit en detalle + lista, que postean a /cheques/<id>/actualizar.
@cheques_bp.route("/cheques/<int:id_cheque>/actualizar", methods=["POST"])
@requiere_login
@requiere_permiso("cheques.editar")
def actualizar(id_cheque: int):
    """Endpoint POST único para inline edit (detalle + lista).

    Campos blandos editables: concepto, observacion, fechad, importe,
    no_cheque. Cualquier campo que el form NO mande NO se toca.
    Errores van como flash y redirect al `next` o al detalle.
    """
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    stat = (ch.get("stat") or "").upper()
    if stat in queries.STATS_TERMINALES_EDIT:
        flash(f"Cheque en stat='{stat}' es terminal — no se puede editar.", "warn")
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))

    next_url = (request.form.get("next") or "").strip() or url_for(
        "cheques.detalle", id_cheque=id_cheque
    )
    errores: list[str] = []

    # Concepto / observación — sólo si el form los manda explícitos.
    concepto = None
    if "concepto" in request.form:
        concepto = (request.form.get("concepto") or "").strip()[:50] or None
    observacion = None
    if "observacion" in request.form:
        observacion = (request.form.get("observacion") or "").strip() or None

    # Fechad — parseo y validación; si vino vacío, no se cambia.
    fechad = None
    if "fechad" in request.form:
        fechad_str = (request.form.get("fechad") or "").strip()
        if fechad_str:
            fechad = parse_date(fechad_str)
            if fechad is None:
                errores.append("Fecha de depósito inválida.")

    # Importe — Decimal para evitar TypeError (Decimal - float = 500).
    # Si el nuevo == actual, no se manda al query.
    from decimal import Decimal as _Dec
    importe_nuevo = None
    if "importe" in request.form:
        importe_str = (request.form.get("importe") or "").strip()
        if importe_str:
            importe_nuevo = parse_monto(importe_str)
            if importe_nuevo is None:
                errores.append("Importe inválido.")
            elif importe_nuevo <= 0:
                errores.append("Importe debe ser mayor a 0.")
            else:
                importe_actual = _Dec(str(ch.get("importe") or 0))
                if abs(importe_nuevo - importe_actual) < _Dec("0.01"):
                    importe_nuevo = None  # sin cambio → no UPDATE

    # N° cheque — sólo si vino explícito en el form. Validación en query.
    no_cheque_nuevo = None
    if "no_cheque" in request.form:
        nc = (request.form.get("no_cheque") or "").strip()
        if nc:
            actual = (ch.get("no_cheque") or "").strip()
            if nc != actual:
                no_cheque_nuevo = nc

    # Doc. banco — TMT 2026-05-27 dueña: 'doc banco no es igual a cheque'.
    # Campo separado (varchar(40) N° comprobante/depósito). Vacío es válido
    # (NULL en DB). Solo se procesa si el form lo manda explícito.
    doc_banco_nuevo = None
    doc_banco_changed = False
    if "doc_banco" in request.form:
        db_v = (request.form.get("doc_banco") or "").strip()
        actual_db = (ch.get("doc_banco") or "").strip()
        if db_v != actual_db:
            doc_banco_nuevo = db_v  # puede ser "" → en query se mapea a NULL
            doc_banco_changed = True

    if errores:
        for e in errores:
            flash(e, "error")
        return redirect(next_url)

    try:
        usuario = (g.user or {}).get("username", "web")
        res = queries.editar(
            id_cheque,
            concepto=concepto,
            observacion=observacion,
            fechad=fechad,
            importe=importe_nuevo,
            no_cheque=no_cheque_nuevo,
            doc_banco=doc_banco_nuevo if doc_banco_changed else None,
            usuario=usuario,
        )
        msg = "Cheque editado."
        if res.get("fechad_shifted_lunes"):
            msg += f" Fecha movida al lunes ({res['fechad_nueva']:%d/%m/%Y})."
        flash(msg, "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(next_url)


@cheques_bp.route("/cheques/<int:id_cheque>/confirmar-rebote", methods=["GET"])
@requiere_login
@requiere_permiso("cheques.transicionar")
def confirmar_rebote(id_cheque: int):
    """Wizard de 2 pasos para marcar rebote: muestra detalle + pide motivo.

    El rebote pone al cliente en STOP, por eso requiere motivo escrito
    (paridad con otras acciones críticas). TMT 2026-05-13.
    """
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    detalle = {
        "N°": ch.get("no_cheque") or f"#{id_cheque}",
        "Cliente": f"{ch.get('codigo_cli') or '—'} — {ch.get('cliente') or ''}",
        "Importe": f"$ {ch.get('importe') or 0:,.2f}",
        "F. depósito": (ch.get("fechad").strftime("%d/%m/%Y") if ch.get("fechad") else "—"),
        "Stat actual": ch.get("stat") or "—",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Marcar como rebotado — cheque {ch.get('no_cheque') or '#' + str(id_cheque)}",
        mensaje=(
            f"Vas a marcar el cheque como rebotado. "
            f"Se anota en la observación del cliente {ch.get('codigo_cli') or ''} — "
            "el STOP lo decidís manualmente desde la pantalla del cliente."
        ),
        detalle_registro=detalle,
        accion_url=url_for("cheques.transicionar", id_cheque=id_cheque),
        volver_url=url_for("cheques.detalle", id_cheque=id_cheque),
        motivo_requerido=True,
        motivo_obligatorio=True,
        confirm_label="Confirmar rebote",
        # Hidden inputs extras para que el POST a transicionar reciba stat_destino.
        extras_hidden=[{"name": "stat_destino", "value": "9"}],
    )


@cheques_bp.route("/cheques/<int:id_cheque>/transicionar", methods=["POST"])
@requiere_login
@requiere_permiso("cheques.transicionar")
def transicionar(id_cheque: int):
    """Cambia el stat del cheque, aplicando los side-effects automáticamente.

    POST `stat_destino`: B (deposito Pichincha) / I (deposito Inter) /
    C (cobrado caja) / 9 (rebotado) / X (anulado) / P (postergado) / D (Daniela).
    """
    stat_destino = (request.form.get("stat_destino") or "").strip().upper()
    no_banco = parse_int(request.form.get("no_banco"))
    motivo = (request.form.get("motivo") or "").strip()
    fecha_str = (request.form.get("fecha") or "").strip()
    fecha = parse_date(fecha_str) if fecha_str else None

    # TMT 2026-05-21 dueña: motivo opcional. Si está vacío, usa default.
    if stat_destino == "9" and not motivo:
        motivo = "sin motivo"

    # Resolver no_banco por NOMBRE cuando el front mandó un placeholder
    # (legacy: B/I tenían 1/2 hardcodeados pero la DB del usuario tiene
    # no_banco distintos — Pichincha=10 en data 2026). TMT 2026-05-11.
    # Match en Python — el LIKE de Postgres se comportaba raro acá.
    if stat_destino in ("B", "I", "V"):
        needle = "PICHINC" if stat_destino == "B" else "INTER"
        all_b = (
            db.fetch_all(
                "SELECT no_banco, COALESCE(nombre,'') AS nombre FROM scintela.banco ORDER BY no_banco"
            )
            or []
        )
        match = next(
            (b for b in all_b if needle in (b.get("nombre") or "").upper()),
            None,
        )
        if match:
            no_banco = int(match["no_banco"])

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.transicionar_stat(
            id_cheque,
            stat_destino=stat_destino,
            no_banco=no_banco,
            fecha=fecha,
            motivo=motivo,
            usuario=usuario,
        )
        nombres = {
            "B": "Depositado en Pichincha",
            "I": "Depositado en Internacional",
            "V": "Depositado en Internacional (legacy)",
            "C": "Cobrado en caja",
            "9": "Marcado como rebotado",
            "X": "Anulado",
            "P": "Postergado",
            "D": "Pasado a Daniela",
        }
        flash(f"{nombres.get(stat_destino, stat_destino)}.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("Error al transicionar", e)
    return redirect(url_for("cheques.detalle", id_cheque=id_cheque))


@cheques_bp.route("/cheques/<int:id_cheque>/anular-error-carga", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("cheques.anular")
def anular_error_carga(id_cheque: int):
    """Anular un cheque mal cargado, con compensaciones automáticas.

    Decisión 2026-04-30: para corregir importe/cliente/banco mal cargados
    se anula el cheque viejo y se crea uno nuevo. Más limpio que reversar.
    """
    ch = queries.por_id(id_cheque)
    if not ch:
        abort(404)
    if (ch.get("stat") or "").upper() in ("X", "T", "R"):
        flash("Cheque ya cerrado — no se puede anular por error de carga.", "warn")
        return redirect(url_for("cheques.detalle", id_cheque=id_cheque))

    if request.method == "POST":
        motivo = (request.form.get("motivo") or "").strip()
        id_reemp_str = (request.form.get("id_reemplazo") or "").strip()
        id_reemplazo = int(id_reemp_str) if id_reemp_str.isdigit() else None
        # TMT 2026-05-21 dueña: motivo opcional sin minlen.
        try:
            usuario = (g.user or {}).get("username", "web")
            res = queries.anular_por_error_de_carga(
                id_cheque,
                motivo=motivo,
                id_reemplazo=id_reemplazo,
                usuario=usuario,
            )
            msg = f"Cheque anulado por error de carga. {res['aplicaciones_reversadas']} aplicación(es) revertida(s)."
            if res.get("compensacion"):
                comp = res["compensacion"]
                msg += f" Compensación en {comp['tipo']} #{comp['id']}."
            flash(msg, "ok")
            return redirect(url_for("cheques.detalle", id_cheque=id_cheque))
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("Error al anular", e)
            return redirect(url_for("cheques.detalle", id_cheque=id_cheque))

    return render_template("cheques/anular_error_carga.html", ch=ch)


@cheques_bp.route("/cheques/_api/depositar-lote", methods=["POST"])
@requiere_login
@requiere_permiso("cheques.aplicar")
def api_depositar_lote():
    """Depósito inline de N cheques desde la pantalla /cheques.

    Pedido dueña 2026-05-20: "Necesitamos agilizar el proceso de los
    depósitos. Si estoy parado en cheques de clientes. Tener un filtro
    que dia hoy. Después poder seleccionar, y una vez que pongo
    depositar lote, se depositan los seleccionados en la pantalla
    principal. No hace falta una segunda pantalla. (...) Cuando el
    cheque se deposita, tengo que ver que el saldo subió en el banco".

    Acepta JSON `{ids: [int], no_banco?: int, fecha?: 'YYYY-MM-DD'}`. Si
    no_banco no viene, default a Pichincha (mismo fallback que el
    wizard clásico). Reusa `queries.depositar_lote` para no duplicar la
    lógica transaccional.

    Devuelve JSON con `n_depositados`, `total`, `banco_nombre`,
    `saldo_antes`, `saldo_despues`. La UI muestra una notificación con
    el delta del saldo para que la dueña vea que efectivamente subió.
    """
    import contextlib
    from datetime import datetime as _dt

    import bank_helpers

    data = request.get_json(silent=True) or request.form
    ids_raw = data.get("ids") or data.get("id_cheque") or []
    if isinstance(ids_raw, str):
        # Form submit envía CSV "1,2,3" — soportamos ambos.
        ids_raw = [x for x in ids_raw.split(",") if x.strip()]
    try:
        ids = [int(x) for x in ids_raw]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "IDs de cheques inválidos."}), 400
    if not ids:
        return jsonify({"ok": False, "error": "Seleccioná al menos un cheque."}), 400

    # Fallback de banco — replica el match por nombre del wizard.
    no_banco = data.get("no_banco")
    try:
        no_banco = int(no_banco) if no_banco not in (None, "") else None
    except (TypeError, ValueError):
        no_banco = None
    if not no_banco:
        all_bancos = []
        with contextlib.suppress(Exception):
            all_bancos = (
                db.fetch_all(
                    "SELECT no_banco, COALESCE(nombre,'') AS nombre FROM scintela.banco ORDER BY no_banco"
                )
                or []
            )
        pichincha = [b for b in all_bancos if "PICHINC" in (b.get("nombre") or "").upper()]
        if pichincha:
            no_banco = int(pichincha[0]["no_banco"])
    if not no_banco:
        return jsonify(
            {
                "ok": False,
                "error": "Banco destino requerido (no encontré Pichincha por default).",
            }
        ), 400

    fecha_raw = (data.get("fecha") or "").strip()
    try:
        fecha_dep = _dt.strptime(fecha_raw, "%Y-%m-%d").date() if fecha_raw else None
    except ValueError:
        return jsonify({"ok": False, "error": f"Fecha inválida: {fecha_raw!r}."}), 400

    # Saldo ANTES (para mostrar el delta).
    try:
        saldo_antes = bank_helpers.saldo_actual(no_banco=no_banco)
    except Exception:  # noqa: BLE001
        saldo_antes = None

    try:
        usuario = (g.user or {}).get("username", "web")
        r = queries.depositar_lote(
            ids_cheques=ids,
            no_banco=no_banco,
            fecha_deposito=fecha_dep,
            usuario=usuario,
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"No pude depositar: {e}"}), 500

    # Saldo DESPUÉS — leído después del commit de queries.depositar_lote.
    try:
        saldo_despues = bank_helpers.saldo_actual(no_banco=no_banco)
    except Exception:  # noqa: BLE001
        saldo_despues = None

    return jsonify(
        {
            "ok": True,
            "n_depositados": r["n_depositados"],
            "total": r["total"],
            "no_banco": r["no_banco"],
            "banco_nombre": r["banco_nombre"],
            "fecha_deposito": r["fecha_deposito"].isoformat(),
            "saldo_antes": saldo_antes,
            "saldo_despues": saldo_despues,
            # URL de la boleta imprimible — el JS puede ofrecerla como link.
            "boleta_url": url_for(
                "cheques.boleta_deposito",
                fecha=r["fecha_deposito"].isoformat(),
                no_banco=r["no_banco"],
            ),
        }
    )


@cheques_bp.route("/cheques/depositar-lote", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("cheques.aplicar")
def depositar_lote():
    """Pantalla para depositar varios cheques en cartera al mismo banco.

    GET: muestra los cheques en cartera (estado='Z'/'P') con checkboxes y
    selector de banco destino.
    POST: ejecuta el depósito en bloque vía queries.depositar_lote.
    """
    import contextlib
    from datetime import datetime as _dt

    # TMT 2026-05-11: los depósitos van SIEMPRES a Pichincha. Buscamos
    # por nombre, filtrando en Python para evitar quirks de LIKE/collation
    # de Postgres que en algún punto hicieron desaparecer el match.
    #
    # TMT 2026-05-14 (#27): match por nombre es frágil — si renombran el
    # banco a "PICHINCHA C.A." o lo abrevian, deja de matchear. Fallback:
    # si no encontramos Pichincha por nombre, devolvemos el primer banco
    # no-internacional/no-contable como destino default. La idea NO es que
    # haya múltiples opciones (la usuaria SIEMPRE deposita en Pichincha),
    # sino que la pantalla nunca quede sin destino válido.
    # Si en el futuro este match falla, agregar config.BANCO_DEPOSITO_DEFAULT
    # o flag bool en scintela.banco.
    all_bancos = []
    with contextlib.suppress(Exception):
        all_bancos = (
            db.fetch_all(
                "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco ORDER BY no_banco"
            )
            or []
        )
    bancos = [b for b in all_bancos if "PICHINC" in (b.get("nombre") or "").upper()]
    if not bancos and all_bancos:
        # Fallback: primer banco no-internacional / no-contable.
        # Filtra los rubros legacy comunes para no devolver "UKN" o "EFECTIVO".
        bancos = [
            b
            for b in all_bancos
            if "INTER" not in (b.get("nombre") or "").upper()
            and "EFECTIVO" not in (b.get("nombre") or "").upper()
            and "UKN" not in (b.get("nombre") or "").upper()
            and "DEP" not in (b.get("nombre") or "").upper()[:3]
            and "ANTIC" not in (b.get("nombre") or "").upper()
        ][:1]

    if request.method == "POST":
        ids_raw = request.form.getlist("id_cheque")
        try:
            ids = [int(x) for x in ids_raw if x.strip()]
        except ValueError:
            flash("IDs de cheques inválidos.", "warn")
            return redirect(url_for("cheques.depositar_lote"))
        no_banco = parse_int(request.form.get("no_banco"))
        # Fallback: si el form no trajo no_banco (template sin hidden,
        # config rara, etc.), buscamos Pichincha por nombre. Pichincha es
        # el único destino válido — no hay razón para fallar acá.
        if not no_banco and bancos:
            no_banco = int(bancos[0]["no_banco"])
        fecha_dep_raw = request.form.get("fecha_deposito") or ""
        try:
            fecha_dep = _dt.strptime(fecha_dep_raw, "%Y-%m-%d").date() if fecha_dep_raw else None
        except ValueError:
            fecha_dep = None
        concepto = (request.form.get("concepto") or "").strip() or None
        if not ids:
            flash("Seleccioná al menos un cheque para depositar.", "warn")
            return redirect(url_for("cheques.depositar_lote"))
        if not no_banco:
            # Listar bancos disponibles para que sea más fácil debugear.
            todos = (
                db.fetch_all(
                    "SELECT no_banco, COALESCE(nombre,'') AS nombre FROM scintela.banco ORDER BY no_banco"
                )
                or []
            )
            opciones = ", ".join(f"{b['no_banco']}={b['nombre']}" for b in todos[:10]) or "(ninguno)"
            flash(
                f"No encontré Pichincha en scintela.banco. Bancos existentes: {opciones}. "
                "Si Pichincha tiene otro nombre (ej. 'PICHINCH' truncado), avisame.",
                "warn",
            )
            return redirect(url_for("cheques.depositar_lote"))
        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.depositar_lote(
                ids_cheques=ids,
                no_banco=no_banco,
                fecha_deposito=fecha_dep,
                concepto=concepto,
                usuario=usuario,
            )
            flash(
                f"{r['n_depositados']} cheque(s) depositado(s) en {r['banco_nombre']} "
                f"por $ {r['total']:.2f}.",
                "ok",
            )
            # TMT 2026-05-15 (#6): tras depositar, redirigir directo a la
            # boleta imprimible (BOLEPICH / BOLEIN del legacy).
            return redirect(
                url_for(
                    "cheques.boleta_deposito",
                    fecha=r["fecha_deposito"].isoformat(),
                    no_banco=r["no_banco"],
                )
            )
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("No pude depositar el lote", e)
        return redirect(url_for("cheques.lista", estado="cartera"))

    # GET — listado de cheques DEPOSITABLES (Z + P). TMT 2026-05-11:
    # antes filtraba sólo "cartera" (Z) y se quedaba sin ver los postergados.
    # `limite=10000` para que entren todos los cheques abiertos — esta
    # pantalla es operativa, la contadora necesita ver el universo completo
    # (filtros cliente-side abajo). Default era 500, le faltaban filas.
    try:
        cheques_cartera = queries.buscar(q="", estado="cartera", desde=None, hasta=None, limite=10000)
        cheques_posterg = queries.buscar(q="", estado="postergados", desde=None, hasta=None, limite=10000)
        # Unir y deduplicar por id_cheque (defensivo).
        seen = set()
        cheques_lote: list = []
        for c in list(cheques_cartera) + list(cheques_posterg):
            cid = c.get("id_cheque")
            if cid in seen:
                continue
            seen.add(cid)
            cheques_lote.append(c)
        # Ordenar por fechad (los más urgentes de depositar primero) — los
        # que tienen fechad < hoy ya vencieron y son prioridad.
        cheques_lote.sort(
            key=lambda c: (
                c.get("fechad") or c.get("fecha") or date.max,
                c.get("id_cheque") or 0,
            )
        )
    except Exception as e:
        cheques_lote = []
        flash_exc("No pude cargar los cheques", e)

    return render_template(
        "cheques/depositar_lote.html",
        cheques=cheques_lote,
        bancos=bancos,
        hoy=today_ec().isoformat(),
    )


@cheques_bp.route("/cheques")
@requiere_login
@requiere_permiso("cheques.ver")
def lista():
    q = request.args.get("q", "").strip()
    # TMT 2026-05-19 v8 (pedido dueña): default = 'cartera_total' para que
    # el hero/listado matchee con b.totc de /informes/balance. Antes era
    # 'cartera' (solo Z) y el número del hero no coincidía con Resultados.
    # Fórmula canónica TOTC (PRG L24): stat ∈ Z+1+2+3+P+D.
    estado = request.args.get("estado", "cartera_total")
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    cliente = request.args.get("cliente", "").strip()

    # TMT 2026-05-29 (pedido dueña): "el filtro del cheque no funciona si no
    # esta en la pagina, me tiene que buscar todos". Cuando hay búsqueda
    # libre (q), expandimos el scope a TODOS los stats — incluyendo
    # depositados, endosados y reversados — para que el cheque aparezca
    # esté donde esté. La pestaña activa sigue mostrándose para contexto,
    # pero el query corre sobre el universo completo.
    estado_efectivo = "todos" if q else estado
    ver_eliminados_arg = request.args.get("ver_eliminados") in ("1", "true", "yes")
    ver_eliminados = True if q else ver_eliminados_arg

    def _parse_num(s: str | None) -> float | None:
        if not s:
            return None
        try:
            return float(str(s).replace(",", "."))
        except ValueError:
            return None

    monto_min = _parse_num(request.args.get("monto_min"))
    monto_max = _parse_num(request.args.get("monto_max"))
    # Show all (default 100k) — antes era 2000. Pedido TMT 2026-05-14.
    try:
        limite = int(request.args.get("limite") or 100000)
    except (TypeError, ValueError):
        limite = 100000
    # TMT 2026-05-27 dueña: 'necesito que despues en cheques pongas
    # flechitas para ver los siguientes 500 cheques'. Pagination de 500
    # por página. ?page=N (1-indexed). Si la URL trae ?limite= explícito
    # > 500, se respeta (export CSV / scripts).
    POR_PAGINA = 500
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    es_export = request.args.get("export") in ("csv", "xlsx")
    if not es_export and limite >= 100000:
        # Default → paginar 500/pag. Si pidió explícito otro limite, respetar.
        page_limite = POR_PAGINA
        page_offset = (page - 1) * POR_PAGINA
    else:
        page_limite = limite
        page_offset = 0
    # ?ver_eliminados=1 → incluye cheques stat='X' (reversados) en tab "Todos".
    # Default: ocultos para no saturar. Pedido TMT 2026-05-14 (#40 audit).
    # `ver_eliminados` y `estado_efectivo` ya quedaron resueltos arriba: si
    # hay búsqueda libre (q), pisamos estado='todos' y ver_eliminados=True
    # para que el cheque buscado aparezca esté en el bucket que esté.
    try:
        filas = queries.buscar(
            q,
            estado_efectivo,
            desde,
            hasta,
            limite=page_limite,
            cliente=cliente,
            monto_min=monto_min,
            monto_max=monto_max,
            ver_eliminados=ver_eliminados,
            offset=page_offset,
        )
        error = None
    except Exception as e:
        filas, error = [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("no_cheque", "N° Cheque"),
                ("fecha", "Fecha"),
                ("fechad", "F. depósito"),
                ("codigo_cli", "Cliente"),
                ("cliente", "Nombre"),
                ("banco", "Banco"),
                ("importe", "Importe"),
                ("stat", "Stat"),
            ],
            filename=f"cheques_{estado}.csv",
        )

    # Conteos por pestaña — un solo query agrupando por el bucket de stat.
    # Pestañas (2026-04-29): cartera (Z) / depositados (B+A) / devueltos (1+2+3+R) /
    # daniela (D) / postergados (P) / endosados (E, TMT 2026-05-12).
    # Bucket extra `devueltos_en_gestion` = (1+2+3) sin 'R' — el que suma
    # a TOTC del balance (PRG línea 24: STAT $ "Z123PD" no incluye R).
    # TMT 2026-05-14 (#16).
    try:
        conteos = (
            db.fetch_all(
                """
            SELECT
              CASE
                WHEN stat = 'Z'                       THEN 'cartera'
                WHEN stat IN ('B','A')                THEN 'depositados'
                WHEN stat IN ('1','2','3','R')        THEN 'devueltos'
                WHEN stat = 'D'                       THEN 'daniela'
                WHEN stat = 'P'                       THEN 'postergados'
                WHEN stat = 'E'                       THEN 'endosados'
                WHEN stat = 'X'                       THEN 'eliminados'
                ELSE 'otros'
              END                            AS bucket,
              COUNT(*)                       AS n,
              COALESCE(SUM(importe), 0)      AS total
            FROM scintela.cheque
            GROUP BY 1
            """
            )
            or []
        )
        conteos_por_bucket = {c["bucket"]: dict(c) for c in conteos}
        # Sub-bucket: devueltos EN GESTION (1+2+3) — los rebotados que
        # todavía cuentan en TOTC (excluye 'R' = rebote terminal incobrable).
        try:
            row_eg = db.fetch_one(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
                  FROM scintela.cheque
                 WHERE stat IN ('1', '2', '3')
                """
            )
            if row_eg:
                conteos_por_bucket["devueltos_en_gestion"] = dict(row_eg)
        except Exception:
            pass
        # TMT 2026-05-19 v2 — Cartera total = Z + P + 1/2/3 + D (los 4
        # buckets visibles arriba). Sin B (depositados ya están en el
        # banco). Pedido Tamara — antes incluía B, fue revertido.
        try:
            row_tot = db.fetch_one(
                """
                SELECT COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
                  FROM scintela.cheque
                 WHERE stat IN ('Z', 'P', '1', '2', '3', 'D')
                """
            )
            if row_tot:
                conteos_por_bucket["cartera_total"] = dict(row_tot)
        except Exception:
            pass
    except Exception:
        conteos_por_bucket = {}

    # Total REAL del filtro — sin LIMIT. Si las filas visibles == total
    # del filtro, no hay diferencia. Si están limitadas (truncado), el
    # template muestra "Mostrando X de N · Total $T".
    try:
        # TMT 2026-05-20 PASADA 6 Federico #8 — pasar cliente/monto al
        # total_buscar para que el hero KPI refleje el filtro real.
        agg = queries.total_buscar(
            q,
            estado_efectivo,
            desde,
            hasta,
            cliente=cliente,
            monto_min=monto_min,
            monto_max=monto_max,
        )
        total = agg["total"]
        n_total = agg["n"]
    except Exception:
        total = sum(float(r["importe"] or 0) for r in filas)
        n_total = len(filas)
    return render_template(
        "cheques/lista.html",
        filas=filas,
        q=q,
        estado=estado,
        desde=desde,
        hasta=hasta,
        cliente=cliente,
        monto_min=monto_min,
        monto_max=monto_max,
        total=total,
        n_total=n_total,
        error=error,
        conteos=conteos_por_bucket,
        # TMT 2026-05-19 — pasamos el mapping de transiciones para que el
        # template arme el dropdown de "Editar estado" por fila.
        transiciones_legales=queries.TRANSICIONES_LEGALES,
        # TMT 2026-05-20 — fecha hoy ISO para el date input de la barra
        # flotante "Depositar lote" (depósito inline sin segunda pantalla).
        hoy_iso=today_ec().isoformat(),
        # TMT 2026-05-27 dueña: paginación 500/pag.
        page=page,
        por_pagina=POR_PAGINA,
        tiene_mas_pag=(len(filas) == POR_PAGINA),
    )


# =====================================================================
# Carga masiva CSV — batch 13. Mismos campos que crear() / ALTAS.PRG.
# =====================================================================

CHEQUES_CSV_COLS = [
    ("fecha", "Fecha", True),
    ("codigo_cli", "Código cliente", True),
    ("no_cheque", "N° cheque", True),
    ("importe", "Importe", True),
    ("no_banco", "N° banco", False),
    ("banco_texto", "Banco", False),
    ("fechad", "Fecha depósito", False),
    ("stat", "Estado", False),
    ("prov", "Proveedor", False),
    ("clave", "Clave", False),
]


@cheques_bp.route("/cheques/cargar-csv", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("cheques.crear")
def cargar_csv():
    from csv_upload import plantilla_csv, procesar_csv

    if request.args.get("plantilla") == "1":
        from flask import Response

        csv_str = plantilla_csv(CHEQUES_CSV_COLS)
        resp = Response("\ufeff" + csv_str, mimetype="text/csv; charset=utf-8")
        resp.headers["Content-Disposition"] = 'attachment; filename="plantilla_cheques.csv"'
        return resp

    if request.method == "POST":
        f = request.files.get("archivo")
        if not f or not f.filename:
            flash("Subí un archivo CSV.", "warn")
            return redirect(url_for("cheques.cargar_csv"))
        raw = f.read()
        result = procesar_csv(
            raw,
            CHEQUES_CSV_COLS,
            queries.crear,
            usuario=(g.user or {}).get("username", "web"),
        )
        tono = "ok" if result.error == 0 else "warn"
        flash(f"Procesadas {result.total} filas — {result.ok} ok, {result.error} con error.", tono)
        return render_template(
            "cheques/cargar_csv_resultado.html",
            result=result,
            cols=CHEQUES_CSV_COLS,
        )
    return render_template("cheques/cargar_csv.html", cols=CHEQUES_CSV_COLS)
