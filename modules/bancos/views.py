"""Bancos — lista, movimientos y emisión de cheques propios (chequera)."""
from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from parsers import parse_date, parse_int, parse_monto

from . import queries

bancos_bp = Blueprint("bancos", __name__, template_folder="templates")


@bancos_bp.route("/bancos")
@requiere_login
@requiere_permiso("bancos.ver")
def lista():
    """Vista hub de Bancos — TMT 2026-05-19 v2 (pedido dueña).

    Default: muestra Pichincha con su saldo y los 4 botones de acción
    (Emitir cheque, Depositar, NC, ND) pre-llenados con `?no_banco=`.
    Botón "Cambiar banco" permite cambiar a Internacional u otro.
    El "banco actual" se elige con ?banco=<no_banco> o `?banco=PICHINCHA`/
    `?banco=INTERNACIONAL` (resuelto por NOMBRE — el no_banco hardcoded
    no nos sirve porque varía por instalación).
    """
    mostrar_todos = request.args.get("todos") == "1"
    try:
        filas_all = queries.lista_bancos()
        error = None
    except Exception as e:
        filas_all, error = [], str(e)

    if mostrar_todos:
        filas = filas_all
    else:
        filas = [
            b for b in filas_all
            if round(float(b["saldo_stored"] or 0), 2) != 0.0
            or round(float(b["saldo_derivado"] or 0), 2) != 0.0
        ]
    ocultos = len(filas_all) - len(filas)

    # --- Resolver el banco "actual" del hub ----------------------------
    # Prioridad: ?banco=<no_banco_int o nombre> → fallback PICHINCHA.
    banco_param = (request.args.get("banco") or "").strip().upper()

    def _busca_por_nombre(needle):
        for b in filas_all:
            if needle in (b.get("nombre") or "").upper():
                return b
        return None

    banco_actual = None
    if banco_param.isdigit():
        no = int(banco_param)
        banco_actual = next((b for b in filas_all if int(b["no_banco"]) == no), None)
    elif banco_param in ("INTERNACIONAL", "INTER", "INTERNAC"):
        banco_actual = _busca_por_nombre("INTER")
    elif banco_param == "PICHINCHA" or banco_param == "PICH":
        banco_actual = _busca_por_nombre("PICHINC")
    if banco_actual is None:
        # Default: Pichincha por nombre.
        banco_actual = _busca_por_nombre("PICHINC")
    if banco_actual is None and filas_all:
        # Fallback: primer banco con saldo > 0, o el primero.
        banco_actual = (filas[0] if filas else filas_all[0])

    # Lista de otros bancos para el selector "Cambiar banco" (excluye el actual).
    otros_bancos = [
        b for b in filas_all
        if not banco_actual or int(b["no_banco"]) != int(banco_actual["no_banco"])
    ]

    return render_template(
        "bancos/lista.html",
        filas=filas, error=error,
        mostrar_todos=mostrar_todos,
        ocultos=ocultos,
        banco_actual=banco_actual,
        otros_bancos=otros_bancos,
    )


@bancos_bp.route("/bancos/_api/preview-concepto")
@requiere_login
@requiere_permiso("bancos.ver")
def preview_concepto():
    """JSON: previsualiza el tipo de cheque desde el concepto.

    Para el form emitir_cheque (chequera): mientras la usuaria tipea el
    concepto, el JS llama acá para sugerir el radio adecuado (proveedor /
    retiro / caja / etc). Auto-detect estilo dBase legacy.

    Mapeo:
      - PR <prov_valido>  → proveedor (con beneficiario sugerido)
      - RR <socio>        → retiro (con de_socio sugerido)
      - INHB / caja / efectivo → caja (banco→caja física)
      - PICH / INTER (transfer entre bancos) → caja (genérico) o proveedor
      - cualquier otro    → otro (sólo movimiento banco)
    """
    import concepto_parser
    import db as _db
    concepto = (request.args.get("concepto") or "").strip()
    if not concepto:
        return {"tipo_sugerido": None, "descripcion": "", "extras": {}}

    provs_validos = {
        (r.get("codigo_prov") or "").strip().upper()
        for r in (_db.fetch_all(
            "SELECT codigo_prov FROM scintela.proveedor"
        ) or [])
    }
    bancos_map: dict = {}
    for b in _db.fetch_all(
        "SELECT no_banco, COALESCE(nombre, '') AS nombre FROM scintela.banco"
    ) or []:
        n = (b.get("nombre") or "").upper().strip()
        if "PICHINC" in n:
            bancos_map.setdefault("PICHINCHA", int(b["no_banco"]))
        if "INTER" in n:
            bancos_map.setdefault("INTERNACIONAL", int(b["no_banco"]))

    parsed = concepto_parser.parse_concepto(
        concepto,
        {"provs_validos": provs_validos, "bancos": bancos_map},
    )
    parsed_tipo = parsed.get("tipo")
    # Mapeo parser → form radio.
    if parsed_tipo == "compra_proveedor":
        tipo_sugerido = "proveedor"
        extras = {"beneficiario": parsed.get("prov")}
    elif parsed_tipo == "retiro_socio":
        tipo_sugerido = "retiro"
        extras = {"de_socio": parsed.get("socio")}
    elif parsed_tipo == "caja_inhb":
        tipo_sugerido = "caja"
        extras = {}
    elif parsed_tipo == "transfer_banco":
        # Transferencia entre bancos propios — no aplica al form de cheques.
        # El form de cheques es para egresos a terceros.
        tipo_sugerido = "otro"
        extras = {}
    elif parsed_tipo == "dolares":
        # TMT 2026-05-17: antes mapeaba a "otro" (no creaba el anticipo).
        # Ahora va a la card dedicada `anticipo_usd` y pasa la cuenta de
        # 2 letras como beneficiario para el INSERT en scintela.dolares.
        tipo_sugerido = "anticipo_usd"
        extras = {"beneficiario": parsed.get("cuenta")}
    else:
        tipo_sugerido = "gasto"  # default razonable: gasto pagado con cheque
        extras = {}

    return {
        "tipo_parser": parsed_tipo,
        "tipo_sugerido": tipo_sugerido,
        "descripcion": concepto_parser.descripcion_humana(parsed),
        "extras": extras,
    }


@bancos_bp.route("/bancos/transferir", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def transferir():
    """Mueve plata de un banco al otro. Atómico: insert CH origen + DE destino.

    Flujo en 2 pasos (TMT 2026-05-14 #19 audit):
      1. GET form → user llena.
      2. POST sin `confirmado=1` → muestra wizard con saldo_preview.
      3. POST con `confirmado=1` → ejecuta la transferencia.
    """
    # Sólo bancos operativos (Pichincha + Internacional) — la tabla scintela.banco
    # tiene 30 filas pero la mayoría son rubros contables del PRG legacy. TMT 2026-05-12.
    bancos = queries.bancos_operativos() or []
    if request.method == "POST":
        no_origen  = parse_int(request.form.get("no_banco_origen"))
        no_destino = parse_int(request.form.get("no_banco_destino"))
        importe    = parse_monto(request.form.get("importe"))
        fecha      = parse_date(request.form.get("fecha")) or date.today()
        concepto   = (request.form.get("concepto") or "").strip()
        confirmado = request.form.get("confirmado") in ("1", "true", "yes")

        # Validaciones básicas comunes (al paso 2 y al paso 3).
        errores: list[str] = []
        if not no_origen:
            errores.append("Elegí el banco de origen.")
        if not no_destino:
            errores.append("Elegí el banco de destino.")
        if no_origen and no_destino and no_origen == no_destino:
            errores.append("Origen y destino no pueden ser el mismo banco.")
        if importe is None or float(importe or 0) <= 0:
            errores.append("Importe debe ser mayor a cero.")
        if errores:
            for e in errores:
                flash(e, "warn")
            return render_template(
                "bancos/transferir.html",
                bancos=bancos, hoy=date.today().isoformat(),
                form={"no_banco_origen": no_origen, "no_banco_destino": no_destino,
                      "importe": request.form.get("importe"),
                      "fecha": request.form.get("fecha"),
                      "concepto": concepto},
            )

        # Paso 2: si NO confirmó, mostrar wizard con saldo_preview.
        if not confirmado:
            try:
                # Buscar nombres + saldos actuales de los dos bancos.
                bo = next((b for b in bancos if int(b.get("no_banco")) == int(no_origen)), None)
                bd = next((b for b in bancos if int(b.get("no_banco")) == int(no_destino)), None)
                imp_f = float(importe or 0)
                saldo_preview = [
                    {
                        "label": f"{(bo or {}).get('nombre') or no_origen} (origen)",
                        "antes":   float((bo or {}).get("saldo") or 0),
                        "despues": float((bo or {}).get("saldo") or 0) - imp_f,
                    },
                    {
                        "label": f"{(bd or {}).get('nombre') or no_destino} (destino)",
                        "antes":   float((bd or {}).get("saldo") or 0),
                        "despues": float((bd or {}).get("saldo") or 0) + imp_f,
                    },
                ]
            except Exception:
                saldo_preview = None
            # Bug C fix (TMT 2026-05-16): el template _confirmar_accion.html
            # espera `extras_hidden` (list of {name,value}) y `accion_url`,
            # no `hidden_fields` + `form_action`. Antes los hidden inputs no
            # se renderizaban → el POST de confirm llegaba sin datos y volvía
            # al form vacío con errores "Elegí banco origen", etc.
            return render_template(
                "_confirmar_accion.html",
                titulo="Confirmar transferencia entre bancos",
                resumen=(f"Vas a transferir <strong>$ {float(importe or 0):,.2f}</strong> "
                         f"de {(bo or {}).get('nombre') or 'origen'} a "
                         f"{(bd or {}).get('nombre') or 'destino'} "
                         f"el {fecha.strftime('%d/%m/%Y')}."),
                concepto=concepto,
                saldo_preview=saldo_preview,
                accion_url=url_for("bancos.transferir"),
                cancel_url=url_for("bancos.transferir"),
                extras_hidden=[
                    {"name": "no_banco_origen",  "value": no_origen},
                    {"name": "no_banco_destino", "value": no_destino},
                    {"name": "importe",          "value": request.form.get("importe") or ""},
                    {"name": "fecha",            "value": fecha.isoformat()},
                    {"name": "concepto",         "value": concepto},
                    {"name": "confirmado",       "value": "1"},
                ],
                motivo_obligatorio=False,
                motivo_requerido=False,
                confirm_label="Sí, transferir",
            )

        # Paso 3: confirmado → ejecutar.
        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.transferir_entre_bancos(
                no_banco_origen=no_origen,
                no_banco_destino=no_destino,
                importe=importe,
                fecha=fecha,
                concepto=concepto,
                usuario=usuario,
            )
            flash(
                f"Transferencia OK: {r['origen']['nombre']} → {r['destino']['nombre']} "
                f"por $ {r['importe']:.2f}. Saldos: origen ${r['origen']['saldo_nuevo']:.2f} · "
                f"destino ${r['destino']['saldo_nuevo']:.2f}.",
                "ok",
            )
            return redirect(url_for("bancos.lista"))
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:  # noqa: BLE001
            flash_exc("No pude completar la transferencia", e)
    return render_template(
        "bancos/transferir.html",
        bancos=bancos,
        hoy=date.today().isoformat(),
    )


@bancos_bp.route("/bancos/emitir-cheque", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def emitir_cheque():
    """Wizard para emitir un cheque propio (chequera).

    El legacy `BANCOS.PRG::CHEQUERA` infería el tipo de movimiento del
    proveedor + concepto. Este wizard pide el tipo EXPLÍCITO (proveedor /
    retiro / caja / gasto / otro) y aplica el side-effect correspondiente
    en una sola transacción.
    """
    bancos = queries.bancos_operativos() or []

    if request.method == "POST":
        tipo = (request.form.get("tipo") or "").strip().lower()
        no_banco = parse_int(request.form.get("no_banco"))
        importe = parse_monto(request.form.get("importe"))
        fecha = parse_date(request.form.get("fecha")) or date.today()
        no_cheque = (request.form.get("no_cheque") or "").strip()
        beneficiario = (request.form.get("beneficiario") or "").strip().upper()
        concepto = (request.form.get("concepto") or "").strip()
        id_posdat = parse_int(request.form.get("id_posdat"))
        de_socio = (request.form.get("de_socio") or "").strip().upper()
        es_postdatado = bool(request.form.get("es_postdatado"))
        fechad = parse_date(request.form.get("fechad"))

        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.emitir_cheque(
                tipo=tipo, no_banco=no_banco, importe=importe, fecha=fecha,
                no_cheque=no_cheque, beneficiario=beneficiario, concepto=concepto,
                id_posdat=id_posdat, de_socio=de_socio,
                es_postdatado=es_postdatado, fechad=fechad,
                usuario=usuario,
            )
            flash(
                f"Cheque emitido OK desde {r['banco_nombre']} por $ {r['importe']:.2f}. "
                f"Side-effect: {r['side_effect']}.",
                "ok",
            )
            return redirect(url_for("bancos.movimientos", no_banco=r["no_banco"]))
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("No pude emitir el cheque", e)

    # GET o POST con error
    import contextlib
    posdats = []
    prov_filter = (request.args.get("prov") or "").strip().upper() or None
    with contextlib.suppress(Exception):
        posdats = queries.posdat_abiertas_de(prov_filter)

    # Autocomplete: conceptos históricos + proveedores activos.
    import contextlib as _ctx
    conceptos = []
    proveedores = []
    with _ctx.suppress(Exception):
        conceptos = queries.conceptos_frecuentes_egresos(limite=50)
    with _ctx.suppress(Exception):
        proveedores = queries.proveedores_activos(limite=500)

    # TMT 2026-05-17: si la URL trae ?tipo=anticipo_usd (acceso directo
    # desde sidebar), el template pre-selecciona esa card. Aceptamos los
    # 6 tipos válidos; cualquier otro valor se ignora.
    tipo_inicial = (request.args.get("tipo") or "").strip().lower()
    if tipo_inicial not in ("proveedor", "retiro", "caja", "gasto",
                            "anticipo_usd", "otro"):
        tipo_inicial = ""

    # TMT 2026-05-19 — item 19 (pedido dueña): si vienen con ?id_posdat=N
    # desde el botón "Pagar" de /posdat, levantamos esa posdat y la pasamos
    # al template para mostrar un banner de contexto + pre-seleccionar la
    # fila + pre-llenar importe/concepto. Antes el wizard llegaba "vacío"
    # y la dueña tenía que recordar todos los datos.
    import db as _db
    posdat_target = None
    id_posdat_param = (request.args.get("id_posdat") or "").strip()
    if id_posdat_param:
        try:
            posdat_target = _db.fetch_one(
                """
                SELECT pd.id_posdat, pd.num, pd.prov, pd.fechad,
                       pd.importe, pd.concepto, pd.banc,
                       COALESCE(p.nombre, '') AS proveedor_nombre
                  FROM scintela.posdat pd
                  LEFT JOIN scintela.proveedor p ON p.codigo_prov = pd.prov
                 WHERE pd.id_posdat = %s
                   AND (pd.anulada IS NOT TRUE OR pd.anulada IS NULL)
                """,
                (int(id_posdat_param),),
            )
            if posdat_target:
                # Default tipo = "proveedor" cuando viene desde el botón Pagar.
                if not tipo_inicial:
                    tipo_inicial = "proveedor"
                # Si tenemos prov, filtramos las posdats al mismo proveedor
                # para que la lista no abrume.
                if posdat_target.get("prov") and not prov_filter:
                    prov_filter = (posdat_target["prov"] or "").strip().upper() or None
                    with contextlib.suppress(Exception):
                        posdats = queries.posdat_abiertas_de(prov_filter)
        except (ValueError, TypeError):
            posdat_target = None

    # TMT 2026-05-19 v2 — banco pre-seleccionado vía ?no_banco= cuando se
    # entra desde la action bar de /bancos. La dueña trabaja siempre desde
    # Pichincha por defecto, esto evita el "elegir banco" extra.
    no_banco_inicial = parse_int(request.args.get("no_banco"))

    return render_template(
        "bancos/emitir_cheque.html",
        bancos=bancos,
        posdats=posdats,
        prov_filter=prov_filter,
        tipo_inicial=tipo_inicial,
        no_banco_inicial=no_banco_inicial,
        hoy=date.today().isoformat(),
        conceptos=conceptos,
        proveedores=proveedores,
        posdat_target=posdat_target,
    )


@bancos_bp.route("/bancos/cheque-emitido/<int:id_transaccion>/reversar",
                 methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def reversar_cheque_emitido(id_transaccion: int):
    """Wizard de 2 pasos para reversar un cheque emitido.

    GET: muestra detalles + input de motivo.
    POST: ejecuta queries.reversar_cheque_emitido (atómico, registra mov_doble).
    """
    import db as _db
    tx = _db.fetch_one(
        """
        SELECT t.id_transaccion, t.fecha, t.no_banco, t.documento,
               t.importe, t.concepto, t.prov, t.numreferencia,
               COALESCE(b.nombre, '') AS banco_nombre,
               COALESCE(p.nombre, '') AS prov_nombre
          FROM scintela.transacciones_bancarias t
          LEFT JOIN scintela.banco b ON b.no_banco = t.no_banco
          LEFT JOIN scintela.proveedor p ON p.codigo_prov = t.prov
         WHERE t.id_transaccion = %s
        """,
        (id_transaccion,),
    )
    if not tx:
        abort(404)
    if (tx.get("documento") or "").strip().upper() != "CH":
        flash(
            f"La transacción #{id_transaccion} no es un cheque emitido "
            f"(documento={tx.get('documento')!r}).", "warn",
        )
        return redirect(url_for("bancos.movimientos", no_banco=tx["no_banco"]))

    if request.method == "POST":
        motivo = (request.form.get("motivo") or "").strip()
        if len(motivo) < 5:
            flash("Motivo requerido (mín. 5 caracteres).", "warn")
            return render_template(
                "bancos/reversar_cheque_emitido.html", tx=tx, motivo=motivo,
            ), 400
        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.reversar_cheque_emitido(
                id_transaccion=id_transaccion,
                motivo=motivo,
                usuario=usuario,
            )
            msg = (
                f"Cheque emitido reversado. Compensación bancaria "
                f"#{r['id_transaccion_compensacion']} (ND $ {r['importe']:.2f})."
            )
            if r.get("side_effect_revertido"):
                se = r["side_effect_revertido"]
                msg += f" Side effect revertido: {se.get('tipo')}."
            flash(msg, "ok")
            return redirect(url_for("bancos.movimientos", no_banco=tx["no_banco"]))
        except ValueError as e:
            flash(str(e), "warn")
            return render_template(
                "bancos/reversar_cheque_emitido.html", tx=tx, motivo=motivo,
            ), 400
        except Exception as e:
            flash_exc("No pude reversar el cheque emitido", e)
            return redirect(url_for("bancos.movimientos", no_banco=tx["no_banco"]))

    return render_template(
        "bancos/reversar_cheque_emitido.html", tx=tx, motivo="",
    )


@bancos_bp.route("/bancos/transferencia/<int:id_mov_doble>/reversar",
                 methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def reversar_transferencia(id_mov_doble: int):
    """Wizard de 2 pasos para reversar una transferencia banco↔banco.

    GET: muestra detalles + input de motivo (opcional).
    POST: ejecuta queries.reversar_transferencia. NC en origen + CH en
    destino, atómico, registra mov_doble del reverso linkeado.
    TMT 2026-05-13.
    """
    import db as _db
    md = _db.fetch_one(
        """
        SELECT m.id_mov_doble, m.tipo, m.fecha_operacion, m.importe,
               m.concepto, m.estado,
               m.origen_id, m.destino_id, m.metadata,
               tx_o.no_banco AS no_banco_origen,
               tx_d.no_banco AS no_banco_destino,
               COALESCE(b_o.nombre, '') AS banco_origen,
               COALESCE(b_d.nombre, '') AS banco_destino
          FROM scintela.mov_doble m
          LEFT JOIN scintela.transacciones_bancarias tx_o
                 ON tx_o.id_transaccion = m.origen_id
          LEFT JOIN scintela.transacciones_bancarias tx_d
                 ON tx_d.id_transaccion = m.destino_id
          LEFT JOIN scintela.banco b_o ON b_o.no_banco = tx_o.no_banco
          LEFT JOIN scintela.banco b_d ON b_d.no_banco = tx_d.no_banco
         WHERE m.id_mov_doble = %s
        """,
        (id_mov_doble,),
    )
    if not md:
        abort(404)
    if md.get("tipo") != "transfer_banco_banco":
        flash(
            f"mov_doble #{id_mov_doble} no es una transferencia banco↔banco.",
            "warn",
        )
        return redirect(url_for("historial.lista"))
    if md.get("estado") != "activo":
        flash(
            f"La transferencia ya está en estado {md.get('estado')!r} — "
            "no se puede reversar otra vez.", "warn",
        )
        return redirect(url_for("historial.lista"))

    if request.method == "POST":
        motivo = (request.form.get("motivo") or "").strip()
        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.reversar_transferencia(
                id_mov_doble=id_mov_doble, motivo=motivo, usuario=usuario,
            )
            flash(
                f"Transferencia reversada. NC en banco origen "
                f"(tx #{r['compensacion_origen']}) + CH en destino "
                f"(tx #{r['compensacion_destino']}). Importe $ {r['importe']:.2f}.",
                "ok",
            )
            return redirect(url_for("historial.lista"))
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("No pude reversar la transferencia", e)
        return redirect(url_for("historial.lista"))

    detalle = {
        "Importe": f"$ {float(md.get('importe') or 0):,.2f}",
        "Fecha": md.get("fecha_operacion"),
        "Banco origen":  f"{md.get('banco_origen') or ''} (#{md.get('no_banco_origen') or '?'})",
        "Banco destino": f"{md.get('banco_destino') or ''} (#{md.get('no_banco_destino') or '?'})",
        "Concepto": md.get("concepto") or "—",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Reversar transferencia bancaria #{id_mov_doble}",
        mensaje=(
            "Vas a reversar la transferencia banco↔banco. Se va a insertar "
            "una NC (ingreso) en el banco origen y una CH (egreso) en el "
            "destino — ambas compensan los movimientos originales. Atómico."
        ),
        detalle_registro=detalle,
        accion_url=url_for("bancos.reversar_transferencia", id_mov_doble=id_mov_doble),
        volver_url=url_for("historial.lista"),
        motivo_requerido=True,
        motivo_obligatorio=False,
        confirm_label="Confirmar reverso de transferencia",
    )


@bancos_bp.route("/bancos/<int:no_banco>")
@requiere_login
@requiere_permiso("bancos.ver")
def movimientos(no_banco):
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    try:
        banco = queries.banco_info(no_banco)
        if not banco:
            abort(404)
        filas = queries.movimientos(no_banco, desde, hasta)
        error = None
    except Exception as e:
        filas, banco, error = [], None, str(e)

    if request.args.get("export") == "csv" and filas:
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"),
                ("tipo", "Tipo"),
                ("referencia", "Referencia"),
                ("descripcion", "Descripción"),
                ("debito", "Débito"),
                ("credito", "Crédito"),
                ("saldo", "Saldo"),
            ],
            filename=f"banco_{no_banco}_movimientos.csv",
        )
    return render_template(
        "bancos/movimientos.html",
        banco=banco, filas=filas, desde=desde, hasta=hasta, error=error,
    )


@bancos_bp.route("/bancos/recompute-saldos", methods=["POST"])
@requiere_login
@requiere_permiso("bancos.editar")
def recompute_saldos():
    """Recalcula el running `saldo` de TODAS las filas de
    `transacciones_bancarias` para todos los bancos. Necesario una sola vez
    para repara los depósitos que se hicieron con código viejo (saldo=NULL).

    El walk-forward es seguro de correr: si todo está bien, devuelve los
    mismos números. Si hay filas con saldo=NULL o desfasadas, las arregla.

    Reportado por TMT 2026-05-11.
    """
    import bank_helpers
    import db as _db
    try:
        bancos = _db.fetch_all(
            "SELECT no_banco, COALESCE(nombre,'') AS nombre FROM scintela.banco ORDER BY no_banco"
        ) or []
        total_filas = 0
        bancos_sin_filas = 0
        # TX SEPARADA POR BANCO (TMT 2026-05-14 #20 audit):
        # Antes había una sola tx envolvente para todos los bancos. Si un
        # banco tenía 10k+ filas el lock bloqueaba INSERTs concurrentes en
        # transacciones_bancarias por minutos. Una tx por banco da pasos
        # cortos y permite a otros usuarios escribir entre medio.
        for b in bancos:
            no_banco = int(b["no_banco"])
            # ANCLA = SEGUNDA fila más vieja (NOT primera). bug #R1 audit
            # 2026-05-14: usar MIN(id_transaccion) es equivalente a
            # ancla=None porque NO hay filas con id < MIN(id), entonces
            # `saldo_previo` queda en 0 y el walk arranca de cero —
            # reintroduce el bug histórico que destruyó Pichincha el
            # 2026-05-12. El ancla debe respetar el saldo de la primera
            # fila como opening implícito y walkear desde la segunda.
            anc = _db.fetch_one(
                """
                SELECT id_transaccion AS ancla
                  FROM scintela.transacciones_bancarias
                 WHERE no_banco = %s
                 ORDER BY fecha ASC, id_transaccion ASC
                 OFFSET 1 LIMIT 1
                """,
                (no_banco,),
            )
            ancla_id = anc.get("ancla") if anc else None
            if not ancla_id:
                # Banco con 0 o 1 fila — nada que recomputar (sin segunda
                # fila no hay walk; la única fila es ya el opening).
                bancos_sin_filas += 1
                continue
            with _db.tx() as conn:
                n = bank_helpers.recompute_saldos_desde(
                    conn,
                    no_banco=no_banco,
                    no_cta=None,
                    ancla_id=int(ancla_id),
                )
            total_filas += int(n or 0)
        msg = (f"Saldos recalculados: {total_filas} fila(s) tocadas en "
               f"{len(bancos) - bancos_sin_filas} banco(s).")
        if bancos_sin_filas:
            msg += f" {bancos_sin_filas} banco(s) sin transacciones."
        flash(msg, "ok")
    except Exception as e:
        flash_exc("No pude recalcular saldos bancarios", e)
    return redirect(url_for("bancos.lista"))


# ---------------------------------------------------------------------------
# Nuevo movimiento simple (DE / NC / ND) — TMT 2026-05-19 (pedido dueña).
# Una vista genérica con un parámetro `doc` que decide el tipo. Reutiliza
# el mismo template.
# ---------------------------------------------------------------------------
_LABELS_DOC = {
    "DE": ("Depósito",      "Suma al saldo del banco"),
    "NC": ("Nota de crédito","Suma al saldo (devolución, intereses, reverso de cargo)"),
    "ND": ("Nota de débito", "Resta del saldo (cargo del banco, comisión, ISI)"),
}


@bancos_bp.route("/bancos/nuevo-movimiento", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def nuevo_movimiento():
    """Form genérico para crear DE / NC / ND.

    El tipo de documento llega vía `?doc=DE|NC|ND` (GET) o el campo hidden
    `documento` (POST). Comparte template `bancos/nuevo_movimiento.html`.
    """
    doc = (request.args.get("doc") or request.form.get("documento") or "").upper().strip()
    if doc not in _LABELS_DOC:
        flash("Documento inválido (debe ser DE, NC o ND).", "warn")
        return redirect(url_for("bancos.lista"))
    label, ayuda = _LABELS_DOC[doc]

    try:
        bancos = queries.lista_bancos()
    except Exception as e:
        bancos, _err = [], str(e)
        flash_exc("No pude listar bancos", e)

    if request.method == "POST":
        no_banco = parse_int(request.form.get("no_banco"))
        importe = parse_monto(request.form.get("importe"))
        fecha = parse_date(request.form.get("fecha")) or date.today()
        concepto = (request.form.get("concepto") or "").strip()
        prov = (request.form.get("beneficiario") or "").strip().upper() or None
        usuario = (g.user or {}).get("username", "web")
        try:
            r = queries.crear_movimiento_simple(
                no_banco=no_banco, documento=doc,
                importe=importe, fecha=fecha,
                concepto=concepto, prov=prov,
                usuario=usuario,
            )
            flash(
                f"{label} registrada por $ {r['importe']:.2f}. "
                f"Nuevo saldo: $ {r['saldo_nuevo']:.2f}.",
                "ok",
            )
            return redirect(url_for("bancos.movimientos",
                                    no_banco=r["no_banco"]))
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc(f"No pude registrar la {label.lower()}", e)

    # GET o POST con error
    import contextlib as _ctx
    proveedores = []
    with _ctx.suppress(Exception):
        proveedores = queries.proveedores_activos(limite=500)

    # TMT 2026-05-19 v2 — banco pre-seleccionado vía ?no_banco= (default
    # Pichincha cuando se entra desde /bancos).
    no_banco_inicial = parse_int(request.args.get("no_banco"))

    return render_template(
        "bancos/nuevo_movimiento.html",
        doc=doc, label=label, ayuda=ayuda,
        bancos=bancos,
        no_banco_inicial=no_banco_inicial,
        proveedores=proveedores,
        hoy=date.today().isoformat(),
    )


@bancos_bp.route("/bancos/mov-simple/<int:id_mov_doble>/reversar",
                 methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def confirmar_reverso_movimiento_simple(id_mov_doble: int):
    """Wizard de reverso para DE / NC / ND creados vía /bancos/nuevo-movimiento.

    GET: muestra detalles + input de motivo.
    POST: ejecuta queries.reversar_movimiento_simple (atómico).
    """
    import db as _db
    md = _db.fetch_one(
        """
        SELECT id_mov_doble, tipo, origen_id, importe, fecha, concepto, estado
          FROM scintela.mov_doble
         WHERE id_mov_doble = %s
        """,
        (id_mov_doble,),
    )
    if not md:
        flash(f"mov_doble #{id_mov_doble} no existe.", "warn")
        return redirect(url_for("historial.lista"))
    if (md.get("estado") or "") != "activo":
        flash(f"Este mov_doble ya está {md.get('estado')}.", "warn")
        return redirect(url_for("historial.lista"))

    # Info del movimiento original para mostrar.
    tx = _db.fetch_one(
        """
        SELECT t.id_transaccion, t.no_banco, t.documento, t.importe, t.fecha,
               t.saldo, COALESCE(b.nombre, '') AS banco_nombre
          FROM scintela.transacciones_bancarias t
          LEFT JOIN scintela.banco b ON b.no_banco = t.no_banco
         WHERE t.id_transaccion = %s
        """,
        (md.get("origen_id"),),
    )

    if request.method == "POST":
        motivo = (request.form.get("motivo") or "").strip()
        usuario = (g.user or {}).get("username", "web")
        try:
            r = queries.reversar_movimiento_simple(
                id_mov_doble=id_mov_doble,
                motivo=motivo,
                usuario=usuario,
            )
            flash(
                f"Reverso OK: {r['doc_orig']} compensado con {r['doc_reverso']}. "
                f"Nuevo saldo: $ {r['saldo_nuevo']:.2f}.",
                "ok",
            )
            return redirect(url_for("historial.lista"))
        except ValueError as e:
            flash(str(e), "warn")
        except Exception as e:
            flash_exc("No pude reversar el movimiento", e)

    # Render wizard de confirmación.
    tipo_legible = (md.get("tipo") or "").replace("_", " ")
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Reversar {tipo_legible}",
        mensaje=(
            "Esta acción compensa el movimiento original con un documento de "
            "signo opuesto (DE/NC → CH; ND → NC). El saldo del banco queda como "
            "estaba antes del alta."
        ),
        detalle_registro={
            "Tipo":     tipo_legible,
            "Banco":    tx.get("banco_nombre", "") if tx else "",
            "Importe":  f"$ {md.get('importe', 0):.2f}",
            "Concepto": md.get("concepto") or "(sin concepto)",
            "Fecha":    md.get("fecha"),
        },
        accion_url=url_for("bancos.confirmar_reverso_movimiento_simple",
                          id_mov_doble=id_mov_doble),
        volver_url=url_for("historial.lista"),
        motivo_obligatorio=True,
        confirm_label="Reversar",
    )
