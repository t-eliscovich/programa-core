"""Proformas (cotizaciones)."""
from flask import (
    Blueprint,
    abort,
    g,
    jsonify,
    render_template,
    request,
)

from auth import requiere_login, requiere_permiso
from exports import csv_response
from filters import today_ec
from parsers import parse_date

from . import queries

proformas_bp = Blueprint("proformas", __name__, template_folder="templates")


@proformas_bp.route("/proformas")
@requiere_login
@requiere_permiso("proformas.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    try:
        filas = queries.buscar(q, desde, hasta)
        error = None
    except Exception as e:
        filas, error = [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha_emision", "Fecha"),
                ("id_proforma", "N° proforma"),
                ("codigo_cli", "Cliente"),
                ("cliente", "Nombre"),
                ("subtotal", "Subtotal"),
                ("monto_descuento_volumen", "Desc. vol."),
                ("subtotal_con_descuento", "Subtotal c/desc"),
                ("monto_descuento_contado", "Desc. contado"),
                ("total_final", "Total"),
            ],
            filename="proformas.csv",
        )

    total = sum(float(r["total_final"] or 0) for r in filas)
    return render_template(
        "proformas/lista.html",
        filas=filas, q=q, desde=desde, hasta=hasta, total=total, error=error,
    )


def _render_form(es_pedido: bool, titulo: str, guardada: dict | None = None):
    """Render compartido de la pantalla campo-a-campo (nueva.html), usada por
    Cotización y por Pedido.

    Se GUARDA desde el 2026-08-11 (pedido de Alex): el mismo botón que abre la
    hoja graba primero. `guardada` es una proforma que se está reabriendo para
    cambiarle algo — sus líneas van al formulario y al grabar sale una VERSIÓN
    NUEVA que apunta a ella (decisión de la dueña: la anterior no se pisa).

    Diferencia: en PEDIDO se ingresan los KILOS EXACTOS (sin multiplicar
    piezas), porque ya sale el pedido; en COTIZACIÓN la cantidad se convierte a
    kg por tipo (piezas×22, cuellos÷33, puños÷45, rib directo).
    """
    try:
        from modules.autocomplete.queries import clientes_para_datalist
        clientes_datalist = clientes_para_datalist()
    except Exception:
        clientes_datalist = []

    try:
        matriz = queries.matriz_precios()
    except Exception:
        matriz = {"clases": [], "telas": [], "precios": {}}

    try:
        colores = queries.colores_catalogo()
    except Exception:
        colores = []

    form = {"fecha": today_ec().strftime("%d/%m/%Y")}
    lineas_guardadas: list[dict] = []
    id_origen = None
    if guardada:
        form = dict(guardada["form"])
        lineas_guardadas = guardada["lineas"]
        id_origen = guardada["id_origen"]
    for k in ("codigo_cli", "fecha"):
        if request.args.get(k):
            form[k] = request.args.get(k)
    return render_template(
        "proformas/nueva.html",
        form=form, errores=[], clientes_datalist=clientes_datalist,
        matriz=matriz, colores=colores,
        es_pedido=es_pedido, titulo=titulo,
        lineas_guardadas=lineas_guardadas, id_origen=id_origen,
    )


@proformas_bp.route("/proformas/nueva", methods=["GET"])
@requiere_login
@requiere_permiso("proformas.crear")
def nueva():
    """Cotización nueva (kilos por conversión de cantidad)."""
    return _render_form(es_pedido=False, titulo="Nueva cotización")


@proformas_bp.route("/pedidos/nuevo", methods=["GET"])
@requiere_login
@requiere_permiso("proformas.crear")
def pedido_nuevo():
    """Pedido nuevo — igual que la cotización pero se ingresan los KILOS
    EXACTOS (sin multiplicar), porque ya sale el pedido."""
    return _render_form(es_pedido=True, titulo="Factura Proforma")


@proformas_bp.route("/proformas/cliente-defaults")
@requiere_login
@requiere_permiso("proformas.crear")
def cliente_defaults_api():
    """Prefill de descuentos al elegir cliente (JSON)."""
    data = queries.cliente_defaults(request.args.get("codigo_cli", ""))
    if not data:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, **data})


@proformas_bp.route("/proformas/<int:id_proforma>")
@requiere_login
@requiere_permiso("proformas.ver")
def detalle(id_proforma: int):
    data = queries.detalle(id_proforma)
    if not data:
        abort(404)
    return render_template("proformas/detalle.html", **data)


@proformas_bp.route("/proformas/<int:id_proforma>/editar")
@requiere_login
@requiere_permiso("proformas.crear")
def editar(id_proforma: int):
    """Reabrir una proforma guardada para cambiarle algo.

    Alex 2026-08-11: *"en ocasiones los clientes piden aumentar o quitar algo y
    lo que ahora toca es repetir la proforma en cada petición"*. Al grabar sale
    una versión NUEVA con número nuevo; la que el cliente ya tiene en la mano
    queda intacta.

    El precio que se rellena es el GUARDADO, no el de la lista de hoy: es el
    que el cliente tiene en el papel. Si la lista cambió, la pantalla lo avisa.
    """
    data = queries.para_formulario(id_proforma)
    if not data:
        abort(404)
    es_pedido = data["es_pedido"]
    titulo = "Factura Proforma" if es_pedido else "Nueva cotización"
    return _render_form(es_pedido=es_pedido, titulo=titulo, guardada=data)


@proformas_bp.route("/proformas/guardar", methods=["POST"])
@requiere_login
@requiere_permiso("proformas.crear")
def guardar():
    """Graba lo que hay en el formulario y devuelve el N° (JSON).

    La pantalla llama acá ANTES de abrir la hoja: la app no puede enterarse de
    lo que pasa después de abrirla (el navegador ofrece imprimir en papel o
    guardar en PDF, y ninguna de las dos vuelve a avisar). Lo que se guarda es
    el acto de generar la proforma, no el de imprimirla.

    Los totales se recalculan acá con `calcular_totales`: lo que mande el
    browser se usa para los datos, nunca para la plata.
    """
    payload = request.get_json(silent=True) or {}
    fecha = parse_date(payload.get("fecha")) or today_ec()
    # `id_proforma` = la que ESTA pantalla ya grabó. Viene cuando Alex corrige
    # algo y vuelve a apretar Imprimir: se regraba esa en vez de dejar dos
    # proformas casi iguales. El número nuevo sale al reabrir desde la lista.
    ya_grabada = payload.get("id_proforma")
    if ya_grabada:
        try:
            res = queries.actualizar(
                int(ya_grabada),
                codigo_cli=payload.get("codigo_cli") or "",
                fecha=fecha,
                lineas=payload.get("lineas") or [],
                pct_volumen=payload.get("descuento_volumen") or 0,
                aplica_contado=True,
                pct_contado=5.0,
                flete=payload.get("flete") or 0,
                observaciones=payload.get("observaciones"),
            )
            return jsonify({"ok": True, **res})
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except LookupError:
            # Se la llevó el barrido mientras la pantalla estaba abierta:
            # cae a grabar una nueva, que es lo que el usuario espera.
            pass
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500
    try:
        res = queries.crear(
            codigo_cli=payload.get("codigo_cli") or "",
            fecha=fecha,
            lineas=payload.get("lineas") or [],
            pct_volumen=payload.get("descuento_volumen") or 0,
            aplica_contado=True,   # 5% siempre (dueña 2026-07-09)
            pct_contado=5.0,
            flete=payload.get("flete") or 0,
            es_pedido=bool(payload.get("es_pedido")),
            id_origen=payload.get("id_origen") or None,
            observaciones=payload.get("observaciones"),
            usuario=(g.user or {}).get("username", "web"),
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, **res})
