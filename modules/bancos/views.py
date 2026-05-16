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
    # Por defecto escondemos los bancos con saldo 0 (la mayoría son cuentas
    # cerradas o históricas que ensucian la vista). ?todos=1 los muestra.
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
    return render_template(
        "bancos/lista.html",
        filas=filas, error=error,
        mostrar_todos=mostrar_todos,
        ocultos=ocultos,
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
    import concepto_parser, db as _db
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
        tipo_sugerido = "otro"
        extras = {}
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
            return render_template(
                "_confirmar_accion.html",
                titulo="Confirmar transferencia entre bancos",
                resumen=(f"Vas a transferir <strong>$ {float(importe or 0):,.2f}</strong> "
                         f"de {(bo or {}).get('nombre') or 'origen'} a "
                         f"{(bd or {}).get('nombre') or 'destino'} "
                         f"el {fecha.strftime('%d/%m/%Y')}."),
                concepto=concepto,
                saldo_preview=saldo_preview,
                form_action=url_for("bancos.transferir"),
                cancel_url=url_for("bancos.transferir"),
                hidden_fields={
                    "no_banco_origen":  no_origen,
                    "no_banco_destino": no_destino,
                    "importe":          request.form.get("importe") or "",
                    "fecha":            fecha.isoformat(),
                    "concepto":         concepto,
                    "confirmado":       "1",
                },
                motivo_obligatorio=False,
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

    return render_template(
        "bancos/emitir_cheque.html",
        bancos=bancos,
        posdats=posdats,
        prov_filter=prov_filter,
        hoy=date.today().isoformat(),
        conceptos=conceptos,
        proveedores=proveedores,
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
