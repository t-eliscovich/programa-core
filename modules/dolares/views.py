"""Anticipos en USD — listado y vista agrupada de scintela.dolares."""
from datetime import date as _date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import db
from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from parsers import parse_date, parse_monto

from . import queries

dolares_bp = Blueprint("dolares", __name__, template_folder="templates")


def _safe(fn, default):
    try:
        return fn(), None
    except Exception as e:
        return default, str(e)


def _nombres_clientes(codigos: list[str]) -> dict[str, str]:
    """Mapeo cta (= codigo_cli, 3 chars) → nombre cliente.

    Una sola consulta para todas las cuentas, evita N+1.
    """
    codigos = [c for c in codigos if c]
    if not codigos:
        return {}
    rows = db.fetch_all(
        """
        SELECT UPPER(TRIM(codigo_cli)) AS cta, nombre
        FROM scintela.cliente
        WHERE UPPER(TRIM(codigo_cli)) = ANY(%s)
        """,
        (codigos,),
    )
    return {r["cta"]: r["nombre"] for r in rows or []}


@dolares_bp.route("/dolares")
@requiere_login
@requiere_permiso("informes.ver")
def lista():
    """Anticipos en USD — vista moderna agrupada por cuenta.

    El total de los anticipos vivos (st vacío) coincide con el campo
    ANTICIPOS del balance. La vista trae:
      · 4 KPIs hero (total vivo, partidas, cuentas, aplicados acumulados)
      · cards por cuenta con saldo vivo + ranking de mayor a menor
      · tabla detallada con filtros (cuenta/fecha/solo vivos)
    """
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    cta   = (request.args.get("cta") or "").strip() or None
    # Federico 2026-05-22 — filtro "Pedido Num.": texto/número incluido en
    # el concepto. Una cuenta puede tener varios pedidos/anticipos en curso.
    pedido = (request.args.get("pedido") or "").strip() or None
    solo_vivos = request.args.get("solo_vivos", "1") != "0"
    filas, error = _safe(
        lambda: queries.lista(
            desde=desde, hasta=hasta, cta=cta, solo_vivos=solo_vivos,
            pedido=pedido,
        ),
        [],
    )
    cuentas, _ = _safe(lambda: queries.por_cuenta(solo_vivos=True), [])
    res, _ = _safe(queries.resumen, {})

    # Enriquecer cuentas con nombre del cliente (una sola query, no N+1).
    nombres = _nombres_clientes([c["cta"] for c in cuentas])
    for c in cuentas:
        c["nombre"] = nombres.get(c["cta"]) or ""

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha", "Fecha"), ("cta", "Cuenta"),
                ("concepto", "Concepto"), ("importe", "Importe"),
                ("st", "Estado"), ("clave", "Clave"),
            ],
            filename="anticipos.csv",
        )
    return render_template(
        "dolares/lista.html",
        filas=filas, cuentas=cuentas, resumen=res,
        desde=desde, hasta=hasta, cta=cta, pedido=pedido,
        solo_vivos=solo_vivos, error=error,
    )


@dolares_bp.route("/dolares/convertir-lote", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("compras.crear")
def convertir_lote():
    """BAP — conversión lote de anticipos USD a compra (BANCOS.PRG:733-819).

    GET sin proveedor: muestra agrupación por proveedor con totales.
    GET con `?prov=XX`: muestra anticipos del proveedor con checkboxes.
    POST: ejecuta `queries.convertir_a_compra()`.

    Permisos: `compras.crear` (estamos creando una compra).
    """
    if request.method == "POST":
        codigo_prov = (request.form.get("codigo_prov") or "").strip().upper()
        ids_raw = request.form.getlist("id_dolares")
        try:
            ids = [int(x) for x in ids_raw if x and str(x).strip()]
        except ValueError:
            flash("IDs de anticipos inválidos.", "warn")
            return redirect(url_for("dolares.convertir_lote",
                                    prov=codigo_prov))
        concepto = (request.form.get("concepto") or "").strip()
        tipo_compra = (request.form.get("tipo_compra") or "H").strip().upper()
        fecha = parse_date(request.form.get("fecha")) or _date.today()
        kg = parse_monto(request.form.get("kg"))
        motivo = (request.form.get("motivo") or "").strip()

        if not codigo_prov:
            flash("Proveedor requerido.", "warn")
            return redirect(url_for("dolares.convertir_lote"))
        if not ids:
            flash("Seleccioná al menos un anticipo para convertir.", "warn")
            return redirect(url_for("dolares.convertir_lote",
                                    prov=codigo_prov))

        try:
            usuario = (g.user or {}).get("username", "web")
            r = queries.convertir_a_compra(
                codigo_prov=codigo_prov,
                ids_anticipos=ids,
                fecha=fecha,
                concepto=concepto,
                tipo_compra=tipo_compra,
                kg=kg,
                motivo=motivo,
                usuario=usuario,
            )
            flash(
                f"BAP: {r['n_anticipos']} anticipo(s) de {codigo_prov} "
                f"convertidos a compra N° {r['numero_compra']} "
                f"({r['comprobante']}) por $ {r['importe_total']:.2f}.",
                "ok",
            )
            # Compras no tiene endpoint de detalle por id — redirigimos al
            # listado de compras filtrado por proveedor.
            return redirect(url_for("compras.lista", q=codigo_prov))
        except ValueError as e:
            flash(str(e), "warn")
            return redirect(url_for("dolares.convertir_lote",
                                    prov=codigo_prov))
        except Exception as e:  # noqa: BLE001
            flash_exc("No pude convertir los anticipos", e)
            return redirect(url_for("dolares.convertir_lote",
                                    prov=codigo_prov))

    # GET
    prov_sel = (request.args.get("prov") or "").strip().upper() or None
    # TMT 2026-05-20 — pedido dueña: "Lo mismo para hilo (H)". Filtramos
    # proveedores tipo='H' en el wizard de convertir-lote.
    grupos, _ = _safe(
        lambda: queries.anticipos_pendientes_por_proveedor(tipos_filter=["H"]),
        [],
    )

    # Enriquecer con nombre del proveedor (no es N+1 grande — pocos proveedores).
    nombres: dict[str, str] = {}
    try:
        rows = db.fetch_all(
            "SELECT codigo_prov, COALESCE(nombre,'') AS nombre "
            "FROM scintela.proveedor"
        ) or []
        nombres = {r["codigo_prov"]: r["nombre"] for r in rows}
    except Exception:
        pass
    for g_row in grupos:
        g_row["nombre"] = nombres.get(g_row["codigo_prov"]) or ""

    anticipos: list[dict] = []
    if prov_sel:
        anticipos, _ = _safe(
            lambda: queries.anticipos_pendientes_de_proveedor(prov_sel),
            [],
        )

    return render_template(
        "dolares/convertir_lote.html",
        grupos=grupos,
        prov_sel=prov_sel,
        anticipos=anticipos,
        nombres=nombres,
        hoy=_date.today().isoformat(),
    )
