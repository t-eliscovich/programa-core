"""Lista de precios (réplica de PRECIOS.DBF) — Datos base.

Matriz de clases de color (filas) x 12 tipos de tela (columnas), valor =
precio USD/kg. TODOS pueden VER (sólo requiere login). EDITAR sólo lo pueden
los roles con permiso 'precios.editar' — que los roles wildcard '*'
(Accionista = Federico/Tamara, Administrador = Andres) satisfacen
automáticamente, sin tocar roles.py ni migrar seguridad.
"""
from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from filters import today_ec
from parsers import parse_monto

from . import queries

precios_bp = Blueprint("precios", __name__, template_folder="templates")


@precios_bp.route("/precios")
@requiere_login
def lista():
    """Ver la lista de precios. Sólo login — la ven todos los roles.

    Muestra SÓLO la matriz base (precio USD/kg por clase de color x tela),
    igual que el dBase. Por defecto es de sólo lectura; el botón "Editar"
    (visible sólo con permiso 'precios.editar') habilita la edición en la
    misma pantalla.
    """
    try:
        filas = queries.matriz()
        error = None
    except Exception as e:  # noqa: BLE001
        filas, error = [], str(e)

    # Telas de PRECIO ÚNICO (migración 0159): mismo precio para todas las
    # clases de color. La dueña las quiere SUMADAS a lo que ya hay, no en un
    # bloque aparte — así que entran al mismo selector de tela.
    try:
        planos = queries.precio_plano()
    except Exception:  # noqa: BLE001
        planos = []
    opciones = queries.columnas_hoja(filas, planos) if filas else []
    por_key = {c["key"]: c for c in opciones}

    # Seccion de descuentos (solo lectura): Basico + 5% / 5%+9% / 5%+14% (cascada).
    tela_sel = (request.args.get("tela") or "jersey").strip()
    if tela_sel.lower() in queries.COLUMNAS_TELA:
        tela_sel = tela_sel.lower()
    if tela_sel not in por_key:
        tela_sel = "jersey" if "jersey" in por_key else (
            opciones[0]["key"] if opciones else "jersey"
        )
    try:
        # Federico, 2026-08-07 (grupo Intela x 3): *"El prg muestra con IVA y
        # sin IVA. Eso está fuera de nuestra historia. Siempre es con IVA
        # incluido"*. La matriz de descuentos ahora sale con IVA — misma
        # escala que la hoja imprimible al pie. En BD sigue todo NETO; el
        # modo Editar también, por la regla del skill precios-lista.
        descuentos = (
            queries.tabla_descuentos_columna(
                filas, por_key[tela_sel], con_iva=True
            )
            if filas and tela_sel in por_key
            else []
        )
    except Exception:  # noqa: BLE001
        descuentos = []

    try:
        planos_ui = queries.resolver_plano(planos, filas)
    except Exception:  # noqa: BLE001
        planos_ui = []

    # LA HOJA PARA IMPRIMIR, al pie de esta misma pantalla. TMT 2026-08-04,
    # dueña: "puedes poner el imprimir abajo y asi no hay tantas pantallas".
    # Siempre CON IVA — "deja solo la opcion con IVA para imprimir, nadie
    # imprime sin iva" / "aca razonamos siempre con IVA".
    from parsers import parse_int

    tramo_idx = parse_int(request.args.get("tramo")) or 0
    if not 0 <= tramo_idx < len(queries.TRAMOS_DESCUENTO):
        tramo_idx = 0
    # Orientación del papel. Primero fue apaisada ("imprimir en horizontal que
    # va a quedar mejor") y después quiso probar la vertical ("perdon imprimi
    # vertical a ver si sale mejor") → en vez de elegir por ella, queda un
    # selector. Default: vertical, que es lo último que pidió y con las telas
    # de hoy entra cómoda.
    apaisado = request.args.get("papel") == "h"
    try:
        hoja = (
            queries.tabla_impresion(filas, tramo_idx, True, opciones)
            if filas
            else []
        )
    except Exception:  # noqa: BLE001
        hoja = []

    return render_template(
        "precios/lista.html",
        filas=filas,
        planos=planos_ui,
        telas=queries.TELAS,
        opciones=opciones,
        error=error,
        descuentos=descuentos,
        tramos=queries.TRAMOS_DESCUENTO,
        tela_sel=tela_sel,
        hoja=hoja,
        tramo_idx=tramo_idx,
        tramo_label=queries.TRAMOS_DESCUENTO[tramo_idx][0],
        apaisado=apaisado,
        iva_pct=queries.IVA_PCT,
        hoy=today_ec(),
    )


@precios_bp.route("/precios/imprimir")
@requiere_login
def imprimir():
    """La hoja ya no es una pantalla aparte: vive al pie de /precios.

    TMT 2026-08-04, dueña: "puedes poner el imprimir abajo y asi no hay
    tantas pantallas". La ruta queda redirigiendo para no romper links ni
    favoritos viejos (los links de este sistema son strings hardcodeados).
    """
    from parsers import parse_int

    tramo_idx = parse_int(request.args.get("tramo")) or 0
    return redirect(url_for("precios.lista", tramo=tramo_idx, _anchor="hoja"))


@precios_bp.route("/precios/actualizar", methods=["POST"])
@requiere_login
@requiere_permiso("precios.editar")
def actualizar():
    """Actualiza una celda de precio (clase, tela). Gated 'precios.editar'."""
    from parsers import parse_int

    clase = parse_int(request.form.get("clase"))
    columna = (request.form.get("columna") or "").strip().lower()
    valor_raw = request.form.get("valor")

    if clase is None or columna not in queries.COLUMNAS_TELA:
        flash("No pude identificar la celda a actualizar.", "error")
        return redirect(url_for("precios.lista"))

    valor = parse_monto(valor_raw)
    if valor_raw and valor_raw.strip() and valor is None:
        flash(f"No entendí el precio «{valor_raw}». Usá formato 9,12.", "error")
        return redirect(url_for("precios.lista"))

    usuario = (g.user or {}).get("username", "web")
    try:
        queries.actualizar_precio(clase, columna, valor, usuario)
        flash("Precio actualizado.", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude actualizar el precio", e)
    return redirect(url_for("precios.lista"))


@precios_bp.route("/precios/subir-porcentaje", methods=["POST"])
@requiere_login
@requiere_permiso("precios.editar")
def subir_porcentaje():
    """Sube TODOS los precios de la matriz (la forma normal de actualizar).

    Gated 'precios.editar'. Dos modos según el campo `modo`:
    - "pct" (default): sube todos un % (multiplica). Un % <= -100 dejaría los
      precios en cero/negativos → se rechaza.
    - "monto": suma un importe fijo (USD) a todos (p.ej. 0,10 = diez centavos).
    Ambos importes se parsean con el parser EU/es-EC (coma decimal).
    """
    usuario = (g.user or {}).get("username", "web")
    modo = (request.form.get("modo") or "pct").strip().lower()

    if modo == "monto":
        monto_raw = request.form.get("monto")
        monto = parse_monto(monto_raw)
        if monto is None:
            flash(
                f"No entendí el monto «{monto_raw}». Usá formato 0,10.", "error"
            )
            return redirect(url_for("precios.lista"))
        # El input viene EN LA ESCALA c/IVA (asi lo tipea el dueno, igual que
        # ve la matriz editable y la hoja imprimible). La BD guarda NETO, asi
        # que dividimos por 1,15 antes de mandar. Federico 2026-08-07.
        try:
            monto_neto = float(monto) / (1.0 + queries.IVA_PCT / 100.0)
            queries.sumar_monto(monto_neto, usuario)
            signo = "+" if float(monto) >= 0 else ""
            flash(f"Sumado {signo}{monto} (c/IVA) a todos los precios.", "ok")
        except Exception as e:  # noqa: BLE001
            flash_exc("No pude sumar el monto a los precios", e)
        return redirect(url_for("precios.lista"))

    pct_raw = request.form.get("pct")
    pct = parse_monto(pct_raw)
    if pct is None:
        flash(f"No entendí el porcentaje «{pct_raw}». Usá formato 5 ó 5,5.", "error")
        return redirect(url_for("precios.lista"))
    if float(pct) <= -100:
        flash("El porcentaje no puede dejar los precios en cero o negativos.", "error")
        return redirect(url_for("precios.lista"))

    try:
        queries.subir_porcentaje(float(pct), usuario)
        signo = "+" if float(pct) >= 0 else ""
        flash(f"Todos los precios actualizados {signo}{pct}%.", "ok")
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude subir los precios", e)
    return redirect(url_for("precios.lista"))


@precios_bp.route("/precios/guardar", methods=["POST"])
@requiere_login
@requiere_permiso("precios.editar")
def guardar():
    """Guarda TODA la matriz de una vez (modo edición → botón "Guardar").

    Recibe un valor por celda con nombre `p_<clase>_<columna>`. Sólo escribe
    las celdas cuyo valor cambió respecto de la matriz actual. Gated
    'precios.editar'.
    """
    usuario = (g.user or {}).get("username", "web")
    try:
        actuales = queries.matriz()
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude leer la lista de precios", e)
        return redirect(url_for("precios.lista"))

    # Los inputs vienen EN LA ESCALA c/IVA (asi los tipea el dueno). La BD
    # guarda NETO, asi que dividimos por 1,15 antes de comparar y escribir.
    # Federico 2026-08-07: "El prg muestra con IVA y sin IVA. Eso esta fuera
    # de nuestra historia. Siempre es con IVA incluido." Guardamos con 4
    # decimales para que el redondeo del round-trip c/IVA <-> NETO no genere
    # drift visible (si alguien abre y guarda sin tocar nada, no debe cambiar).
    IVA_FACTOR = 1.0 + queries.IVA_PCT / 100.0
    cambios = 0
    errores = []
    for fila in actuales:
        clase = int(fila["clase"])
        for col in (c for c, _ in queries.TELAS):
            campo = f"p_{clase}_{col}"
            if campo not in request.form:
                continue
            raw = (request.form.get(campo) or "").strip()
            nuevo_iva = parse_monto(raw)
            if raw and nuevo_iva is None:
                errores.append(f"«{raw}»")
                continue
            actual = fila.get(col)
            act_f = float(actual) if actual is not None else None
            # Convertir input c/IVA a NETO con 4 decimales (mismo criterio del
            # skill precios-lista: "guardar con 4 decimales para que al
            # re-multiplicar vuelva exacto el numero original").
            nue_neto = (
                round(float(nuevo_iva) / IVA_FACTOR, 4)
                if nuevo_iva is not None else None
            )
            # "Sin cambios" se juzga en la escala que ve el usuario (2 dec c/IVA).
            act_iva_2 = round(act_f * IVA_FACTOR, 2) if act_f is not None else None
            nue_iva_2 = round(float(nuevo_iva), 2) if nuevo_iva is not None else None
            if act_iva_2 == nue_iva_2:
                continue
            try:
                queries.actualizar_precio(clase, col, nue_neto, usuario)
                cambios += 1
            except Exception:  # noqa: BLE001
                errores.append(f"{clase}/{col}")

    # Telas de precio único: campo `pp_<id>`. El input viene c/IVA (igual que
    # la matriz de arriba y la hoja de abajo). Se convierte a NETO y se guarda
    # con 4 decimales para que el round-trip c/IVA <-> NETO no drifteé.
    try:
        planos = queries.precio_plano()
    except Exception:  # noqa: BLE001
        planos = []
    for p in planos:
        if p.get("ref_col"):
            continue  # sigue a la matriz, no tiene número propio
        campo = f"pp_{int(p['id'])}"
        if campo not in request.form:
            continue
        raw = (request.form.get(campo) or "").strip()
        nuevo_iva = parse_monto(raw)
        if raw and nuevo_iva is None:
            errores.append(f"«{raw}»")
            continue
        actual = float(p["precio"]) if p.get("precio") is not None else None
        nue_neto = (
            round(float(nuevo_iva) / IVA_FACTOR, 4)
            if nuevo_iva is not None else None
        )
        # "Sin cambios" se juzga en la escala que ve el usuario (2 dec c/IVA).
        act_iva_2 = round(actual * IVA_FACTOR, 2) if actual is not None else None
        nue_iva_2 = round(float(nuevo_iva), 2) if nuevo_iva is not None else None
        if act_iva_2 == nue_iva_2:
            continue
        try:
            queries.actualizar_precio_plano(int(p["id"]), nue_neto, usuario)
            cambios += 1
        except Exception:  # noqa: BLE001
            errores.append(str(p["tela"]))

    if errores:
        flash("No entendí algunos precios: " + ", ".join(errores[:8]), "error")
    if cambios:
        flash(f"{cambios} precio(s) actualizado(s).", "ok")
    elif not errores:
        flash("Sin cambios.", "ok")
    return redirect(url_for("precios.lista"))
