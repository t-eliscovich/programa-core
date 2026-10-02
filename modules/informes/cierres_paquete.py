"""El paquete PDF del cierre de mes.

TMT 2026-08-31: en el dBase, al cerrar el mes, alguien pegaba capturas de
las pantallas de cierre en un Word -- un archivo por mes (ver FEBRERO.docx,
34 capturas: Resultados/Balance, Ventas del mes por cliente, Cartera,
Deudas, Gastos del mes + el detalle de cada rubro, Flujo de producción
(Movimientos hilado/tejido/tintorería), Activos fijos con su amortización,
Anticipos a proveedores). Ese rito se fue con el dBase (05/08) y no tenía
reemplazo. Pedido de la dueña: *"quiero que lo hagas vos... hagámoslo para
el cierre, que sea parte del proceso"*.

CÓMO se arma: cada sección es una RUTA VIVA de la propia app (la misma que
ve un usuario). Se le pide con el test client de Flask -- el mismo truco
que ya usa `scripts/vista_local.py`, pero acá con una sesión real (no un
usuario fantasma) tomada prestada de un usuario activo con permiso amplio,
para que la página renderice exactamente como en pantalla, links y todo.
El HTML de cada página se imprime a PDF con `pdf_motor.desde_html()` -- la
MISMA hoja de estilos `@media print` que ya usa cualquier Ctrl+P de la app
(ver `templates/base.html`) -- y las páginas se pegan en un solo archivo
con `pypdf`. No hay una plantilla nueva que mantener: si una pantalla
cambia, el paquete del mes que viene cambia solo.

CUÁNDO se genera: `generar_y_guardar()` la llama `crear_snapshot_historia()`
(ver ese docstring) SOLO en la rama LIVE -- el mismo día que se cierra el
mes. Un backfill/as-of no tiene de dónde sacar la cartera, los gastos o los
activos de un mes viejo (esas pantallas son "hoy", no aceptan un mes
pasado): mostrarían el estado de HOY con el rótulo de un mes que ya cerró,
peor que no tener el archivo. Ahí se salta, con la razón en el log.

Best-effort SIEMPRE: si el servidor no tiene el navegador de `pdf_motor`
(ver `disponible()`), o cualquier página falla, `generar_y_guardar()` no
revienta -- devuelve `{"aplicado": False, "razon": ...}` y quien la llama
(el cierre de mes) sigue su camino. La foto de `scintela.historia` nunca
depende de que este paquete salga bien.
"""

from __future__ import annotations

import io
import logging

import db
from filters import today_ec

_LOG = logging.getLogger("programa_core.cierres_paquete")

#: (título de la sección, ruta a pedirle a la app, fondo). El orden es el
#: mismo en el que se archivaban las capturas del dBase. `fondo=True` sólo
#: en Resultados -- TMT 2026-08-31, dueña: "pagina 1, no lo podemos mostrar
#: igual que la pantalla de resultados?" -- esa sección sale con colores
#: (media screen); el resto sigue con la hoja de impresión de siempre.
PAGINAS: tuple[tuple[str, str, bool], ...] = (
    ("Informe Resultados — Balance", "/informes/balance", True),
    ("Ventas del mes", "/informes/ventas", False),
    # TMT 2026-08-31: /cartera/aging es la pantalla OPERATIVA (buckets de
    # mora, botón "stop automático") -- no lo que se archiva cada mes.
    # /informes/cartera es el resumen simple (CLI/CHQ/FAC/TOT/%), réplica
    # del CARTERA del dBase, con su propia vista compacta de 3 columnas
    # para impresión (ver cartera.html).
    ("Cartera", "/informes/cartera", False),
    ("Deudas", "/informes/deudas", False),
    ("Gastos del mes", "/informes/gastos", False),
    ("Flujo de producción", "/informes/flujo-produccion", False),
    ("Activos fijos", "/activos", False),
    ("Anticipos", "/dolares", False),
)

_MESES_ES = (
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def nombre_mes(mes: int) -> str:
    return _MESES_ES[mes] if 1 <= mes <= 12 else str(mes)


def _usuario_sistema_id() -> int | None:
    """Un usuario activo de un rol con permiso amplio ('*'), para pedirle
    las páginas a la propia app por dentro. No se le manda nada, no se le
    cambia nada -- sólo se toma prestada su sesión un instante para poder
    renderizar pantallas que están gateadas por permiso."""
    row = db.fetch_one(
        """
        SELECT u.id_usuario
          FROM seguridad.usuario u
          JOIN seguridad.permiso p ON p.id_rol = u.id_rol
         WHERE u.activo AND p.nombre_opcion = '*'
         ORDER BY u.id_usuario
         LIMIT 1
        """
    )
    return (row or {}).get("id_usuario")


def _pdf_de_pagina(client, ruta: str, *, fondo: bool = False) -> bytes:
    """Le pide `ruta` al test client (ya logueado) y devuelve el PDF de esa
    página. Levanta si la página no respondió 200 o si no hay navegador.

    `fondo=True`: pide la página con `?pdf_limpio=1` (esconde nav/sidebar/
    botones sin pasar por la hoja de impresión, ver `templates/base.html`)
    y la imprime en media `screen` con fondos (ver `pdf_motor.desde_html`)."""
    from modules._lib import pdf_motor

    if fondo:
        sep = "&" if "?" in ruta else "?"
        ruta = f"{ruta}{sep}pdf_limpio=1"
    resp = client.get(ruta, follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"{ruta} respondió {resp.status_code}")
    html = resp.get_data(as_text=True)
    return pdf_motor.desde_html(html, fondo=fondo)


def _agregar_paginas(writer, pdf_bytes: bytes) -> None:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    for page in reader.pages:
        writer.add_page(page)


def armar_pdf(anio: int, mes: int) -> tuple[bytes, int]:
    """Arma el PDF del paquete pidiéndole cada página de `PAGINAS` a la
    propia app. Devuelve (bytes del pdf combinado, cantidad de páginas que
    entraron). Levanta `RuntimeError` si no se pudo armar ni una sola
    página -- un paquete vacío no sirve de nada."""
    from flask import current_app
    from pypdf import PdfWriter

    from modules._lib import pdf_motor

    if not pdf_motor.disponible():
        raise RuntimeError(
            "el servidor no tiene navegador para imprimir (pdf_motor)"
        )

    uid = _usuario_sistema_id()
    if not uid:
        raise RuntimeError("no hay ningún usuario activo con permiso '*'")

    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["last_activity"] = today_ec().isoformat()

    writer = PdfWriter()
    ok = 0
    fallos: list[str] = []
    for titulo, ruta, fondo in PAGINAS:
        try:
            pdf_bytes = _pdf_de_pagina(client, ruta, fondo=fondo)
            _agregar_paginas(writer, pdf_bytes)
            ok += 1
        except Exception as e:  # noqa: BLE001 -- una sección mala no tira el resto
            fallos.append(f"{titulo} ({ruta}): {e}")
            _LOG.warning("cierre %04d-%02d: no se pudo armar %r: %s",
                         anio, mes, ruta, e)

    if ok == 0:
        raise RuntimeError(
            "ninguna sección se pudo renderizar: " + "; ".join(fallos)
        )
    if fallos:
        _LOG.warning("cierre %04d-%02d: %d/%d secciones fallaron: %s",
                     anio, mes, len(fallos), len(PAGINAS), "; ".join(fallos))

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), ok


def guardar(anio: int, mes: int, pdf_bytes: bytes, paginas: int,
            usuario: str, version: int = 1, nota: str | None = None) -> int:
    """UPSERT del paquete de (anio, mes, version) -- se puede regrabar, igual
    que la foto de `scintela.historia`: la fila vieja se pisa, no se acumula.

    Tamara 02/10/2026: el cierre ORIGINAL es la versión 1 y no se pisa nunca
    con una corrección; el PDF que se arma después de un ajuste al cierre es
    la versión 2 (ver `armar_corregido`)."""
    res = db.execute_returning(
        """
        INSERT INTO scintela.cierre_paquete
            (anio, mes, version, nota, pdf, tamano_bytes, paginas, generado_por)
        VALUES (%(anio)s, %(mes)s, %(version)s, %(nota)s, %(pdf)s, %(tam)s,
                %(paginas)s, %(usuario)s)
        ON CONFLICT (anio, mes, version) DO UPDATE
           SET pdf = EXCLUDED.pdf,
               nota = EXCLUDED.nota,
               tamano_bytes = EXCLUDED.tamano_bytes,
               paginas = EXCLUDED.paginas,
               generado_por = EXCLUDED.generado_por,
               generado_en = now()
         RETURNING id_paquete
        """,
        {
            "anio": anio, "mes": mes, "version": int(version or 1),
            "nota": nota, "pdf": pdf_bytes, "tam": len(pdf_bytes),
            "paginas": paginas, "usuario": (usuario or "")[:50],
        },
    )
    return (res or {}).get("id_paquete")


def generar_y_guardar(anio: int, mes: int, usuario: str = "auto") -> dict:
    """Arma y guarda el paquete de (anio, mes). Nunca levanta: cualquier
    error vuelve como `{"aplicado": False, "razon": ...}` -- quien la llama
    (el cierre de mes) no puede depender de que esto salga bien."""
    try:
        pdf_bytes, paginas = armar_pdf(anio, mes)
        id_paquete = guardar(anio, mes, pdf_bytes, paginas, usuario)
        return {
            "aplicado": True, "anio": anio, "mes": mes,
            "id_paquete": id_paquete, "paginas": paginas,
            "tamano_bytes": len(pdf_bytes),
            "razon": f"Paquete de cierre {anio:04d}-{mes:02d} armado "
                     f"({paginas}/{len(PAGINAS)} secciones, "
                     f"{len(pdf_bytes):,} bytes).",
        }
    except Exception as e:  # noqa: BLE001
        _LOG.warning("cierre %04d-%02d: paquete NO generado: %s", anio, mes, e)
        return {
            "aplicado": False, "anio": anio, "mes": mes,
            "razon": f"No se pudo armar el paquete: {e}",
        }


#: tamaño máximo de un PDF subido a mano -- generoso (el paquete armado
#: normalmente pesa <1MB), pero evita que alguien suba cualquier cosa.
_MAX_BYTES_SUBIDO = 20 * 1024 * 1024


def guardar_manual_subido(anio: int, mes: int, pdf_bytes: bytes,
                           usuario: str) -> dict:
    """Archiva un PDF que alguien subió A MANO como el paquete de (anio, mes).

    Tamara 2026-09-02: agosto cerró un día tarde (`crear_snapshot_historia`
    cayó en la rama `_as_of`), así que el disparo automático de
    `generar_y_guardar` se salteó -- un backfill no tiene de dónde sacar
    Cartera/Gastos/Activos de un mes que ya pasó (ver el docstring de este
    módulo). El botón manual "Generar y guardar" tampoco sirve pasados unos
    días: arma el PDF con el estado de HOY, y para el 02/09 Anticipos ya
    estaba $454k más alto que al cierre del 31/08 -- archivaría un número
    falso con el rótulo "Agosto 2026".

    La única fuente confiable en ese caso es un PDF que alguien haya bajado
    EL MISMO DÍA del cierre real (por ejemplo con "Vista previa (con los
    datos de hoy)", que si se usa el día del cierre captura exactamente eso)
    y guardado afuera del sistema. Esta función lo archiva en
    `scintela.cierre_paquete` para que quede en el mismo lugar que los
    generados automáticamente -- incluye 'subido' en `generado_por` para
    que se note en la lista que no salió del mecanismo automático.

    Valida que sea un PDF de verdad (encabezado %PDF- + `pypdf` lo puede
    abrir) antes de guardar -- un archivo cualquiera con extensión .pdf no
    debería poder pisar el archivo."""
    from pypdf import PdfReader

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-"):
        return {"aplicado": False, "anio": anio, "mes": mes,
                "razon": "el archivo no es un PDF (falta el encabezado %PDF-)."}
    if len(pdf_bytes) > _MAX_BYTES_SUBIDO:
        return {"aplicado": False, "anio": anio, "mes": mes,
                "razon": f"el archivo pesa más de "
                         f"{_MAX_BYTES_SUBIDO // (1024 * 1024)}MB."}
    try:
        paginas = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception as e:  # noqa: BLE001 -- pypdf tira varios tipos con contenido roto
        return {"aplicado": False, "anio": anio, "mes": mes,
                "razon": f"el PDF está corrupto o no se pudo leer: {e}"}
    if paginas == 0:
        return {"aplicado": False, "anio": anio, "mes": mes,
                "razon": "el PDF no tiene páginas."}

    id_paquete = guardar(anio, mes, pdf_bytes, paginas,
                          usuario=f"subido:{usuario}")
    return {
        "aplicado": True, "anio": anio, "mes": mes,
        "id_paquete": id_paquete, "paginas": paginas,
        "tamano_bytes": len(pdf_bytes),
        "razon": f"PDF subido a mano archivado como el cierre de "
                 f"{nombre_mes(mes)} {anio} ({paginas} páginas, "
                 f"{len(pdf_bytes):,} bytes).",
    }


def listar() -> list[dict]:
    """Los paquetes ya generados, del más nuevo al más viejo (y dentro de un
    mes, el original primero)."""
    filas = db.fetch_all(
        """
        SELECT anio, mes, version, nota, tamano_bytes, paginas, generado_en,
               generado_por
          FROM scintela.cierre_paquete
         ORDER BY anio DESC, mes DESC, version
        """
    )
    for f in filas:
        f["mes_nombre"] = nombre_mes(f["mes"])
    return filas


def obtener(anio: int, mes: int, version: int = 1) -> bytes | None:
    row = db.fetch_one(
        "SELECT pdf FROM scintela.cierre_paquete "
        " WHERE anio = %s AND mes = %s AND version = %s",
        (anio, mes, int(version or 1)),
    )
    pdf = (row or {}).get("pdf")
    # psycopg2 devuelve bytea como memoryview -- Response/send_file quieren
    # bytes de verdad.
    return bytes(pdf) if pdf is not None else None


# ── El PDF del cierre CORREGIDO (versión 2) ──────────────────────────────────
#
# Tamara 02/10/2026: Asinfo corrigió el stock (−51.775 kg de hilo, −7.139 kg
# de tela cruda) y el ajuste se absorbió en el cierre de septiembre
# (`ajuste_cierre`). *"¿Podés rearmar el PDF? Si se puede recalculando todo
# (...) pero subilo como un segundo PDF."*
#
# El PDF original son las pantallas tal como estaban la noche del cierre, y
# esas pantallas hoy muestran el mes en curso: no se pueden volver a pedir.
# Lo que cambió con el ajuste son dos cosas, y esas dos se rehacen:
#
#   · Resultados y balance: se arman de nuevo desde la foto de cierre
#     (`scintela.historia`, ya con el ajuste), la foto de la traza de esa
#     misma hora (caja/bancos, cheques/facturas y los kilos y $/kg de cada
#     etapa) y los colorantes del mes. Nada sale de la pantalla de hoy.
#   · Flujo de producción: la pantalla del mes CERRADO se arma con los
#     movimientos de Asinfo, que ya están corregidos.
#
# Ventas, Cartera, Deudas, Gastos, Activos y Anticipos no cambian con un
# ajuste de stock: van las páginas del original, tal cual.

#: Texto con el que empieza cada sección en el PDF original (para partirlo).
_MARCA_VENTAS = "VENTAS DEL MES"
_MARCA_FLUJO = "MOVIMIENTOS DEL MES"
_MARCA_ACTIVOS = "Activos fijos"


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def datos_resultados_al_cierre(anio: int, mes: int) -> dict:
    """Todo lo que lleva la hoja de Resultados de (anio, mes) al cierre,
    recalculado desde lo guardado, con los ajustes al cierre ya adentro."""
    from calendar import monthrange
    from datetime import date

    from modules.informes import ajuste_cierre

    fin = date(anio, mes, monthrange(anio, mes)[1])
    h = db.fetch_one(
        "SELECT * FROM scintela.historia WHERE fecha = %s "
        " ORDER BY id_historia DESC LIMIT 1", (fin,))
    if not h:
        raise RuntimeError(f"no hay foto de cierre del {fin:%d/%m/%Y}")
    # La foto de la traza de la hora del cierre (la última antes de que se
    # grabara historia): de ahí salen las partes que historia guarda sumadas.
    t = db.fetch_one(
        """
        SELECT * FROM scintela.traza_utilidad
         WHERE creado_en <= (%s AT TIME ZONE 'UTC') + INTERVAL '1 minute'
           AND (creado_en AT TIME ZONE 'America/Guayaquil')::date = %s
         ORDER BY creado_en DESC, id_traza DESC LIMIT 1
        """, (h.get("fecha_crea"), fin)) or {}
    ajustes = ajuste_cierre.vivos_del_mes(anio, mes)
    kg_aj = {e: 0.0 for e in ajuste_cierre.ETAPAS}
    for a in ajustes:
        for ln in a.get("lineas") or []:
            if ln.get("etapa") in kg_aj:
                kg_aj[ln["etapa"]] += _f(ln.get("kg"))
    etapas = []
    for e, nom in (("hilado", "Hilado"), ("tejido", "Tejido"), ("terminado", "Terminado")):
        kg = _f(t.get(f"{e}_kg")) + kg_aj[e]
        ukg = _f(t.get(f"{e}_ukg"))
        etapas.append({"etapa": e, "nombre": nom, "kg": kg, "ukg": ukg,
                       "us": kg * ukg, "ajuste_kg": kg_aj[e]})
    stock_us = _f(h.get("ustock"))
    stock_kg = _f(h.get("stock"))
    caja = _f(t.get("caja"))
    banco_total = _f(h.get("banco"))
    cheques = _f(t.get("cheques"))
    cart = _f(h.get("cart"))
    act = {
        "caja": caja, "bancos": banco_total - caja,
        "cheques": cheques, "facturas": cart - cheques, "cartera": cart,
        "subtotal": banco_total + cart,
        "anticipos": _f(h.get("anticipos")),
        "stock_us": stock_us, "stock_kg": stock_kg,
        "stock_ukg": (stock_us / stock_kg) if stock_kg else 0.0,
        "quimicos": _f(h.get("uqui")),
        "maq": _f(h.get("maquinaria")), "terr": _f(h.get("realty")),
    }
    act["af"] = act["maq"] + act["terr"]
    act["total"] = (act["subtotal"] + act["anticipos"] + stock_us
                    + act["quimicos"] + act["af"])
    pasivo = _f(h.get("deuda"))
    patrimonio = _f(h.get("patrimonio"))

    kv = _f(h.get("kvent"))

    def fila(nombre, kg, us):
        return {"nombre": nombre, "kg": kg, "us": us,
                "ukg": (us / kv) if (kv and kg is None) else ((us / kg) if kg else None)}

    col_kg = col_us = None
    try:
        from modules.comparativa_tintoreria.views import tintoreria_mensual_cacheada
        # Mismo criterio que la fila Colorantes del balance: la primera fila
        # de COSTOS DE TINTORERÍA del mes pedido (kg y $ de la misma fila).
        filas = (tintoreria_mensual_cacheada(anio, mes) or {}).get("filas") or []
        if filas and filas[0].get("t_imp") is not None:
            col_us = _f(filas[0]["t_imp"])
            col_kg = _f(filas[0].get("t_kg")) or None
    except Exception as e:  # noqa: BLE001
        _LOG.warning("cierre corregido: colorantes de %s/%s: %s", mes, anio, e)
    costos = [
        fila("Materia Prima", _f(h.get("kcom")), _f(h.get("ucom"))),
        fila("Tejeduría", _f(h.get("ktej")), _f(h.get("utej"))),
        fila("Tintorería", _f(h.get("ktin")), _f(h.get("utin"))),
    ]
    if col_us is not None:
        costos.append(fila("Colorantes/Quím.", col_kg, col_us))
    costos.append(fila("Administración", None, _f(h.get("gasto"))))
    usuti = _f(h.get("usuti"))
    imp_aj = sum(_f(a.get("importe")) for a in ajustes)

    cierres = db.fetch_all(
        """
        SELECT DISTINCT ON (date_trunc('month', fecha)) fecha, uvent, usuti, usret
          FROM scintela.historia
         WHERE fecha >= %s AND fecha <= %s
         ORDER BY date_trunc('month', fecha), fecha DESC, id_historia DESC
        """, (date(anio, 1, 1), fin)) or []
    ret_anio = db.fetch_one(
        "SELECT COALESCE(SUM(ret), 0) AS t FROM scintela.retiros "
        " WHERE fecha >= %s AND fecha <= %s "
        "   AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'",
        (date(anio, 1, 1), fin)) or {}
    return {
        "anio": anio, "mes": mes, "mes_nombre": nombre_mes(mes), "fin": fin,
        "ventas": fila("Ventas", kv, _f(h.get("uvent"))),
        "costos": costos,
        "utilidad": {"us": usuti, "ukg": (usuti / kv) if kv else 0.0,
                     "antes": usuti - imp_aj,
                     "ukg_antes": ((usuti - imp_aj) / kv) if kv else 0.0},
        # Igual que `ventas_anio_en_curso` la noche del cierre: los meses
        # cerrados de historia + lo facturado en el mes (sólo positivos).
        "anio_ventas": (sum(_f(c.get("uvent")) for c in cierres
                            if c["fecha"].month < mes) + _f((db.fetch_one(
            """
            SELECT COALESCE(SUM(importe), 0) AS t FROM scintela.factura
             WHERE EXTRACT(YEAR FROM fecha) = %s AND EXTRACT(MONTH FROM fecha) = %s
               AND COALESCE(stat, '') <> 'X' AND COALESCE(importe, 0) > 0
               AND COALESCE(usuario_crea, '') <> 'asinfo-backfill'
            """, (anio, mes)) or {}).get("t"))),
        "anio_utilidades": sum(_f(c.get("usuti")) for c in cierres),
        "dividendos_mes": _f(h.get("usret")),
        "dividendos_anio": _f(ret_anio.get("t")),
        "etapas": etapas, "activo": act, "pasivo": pasivo,
        "patrimonio": patrimonio, "ajustes": ajustes, "ajuste_total": imp_aj,
        "id_traza": t.get("id_traza"),
    }


def _partir_original(pdf_bytes: bytes) -> dict:
    """Índices de las secciones del PDF original: dónde empiezan Ventas, el
    Flujo de producción y Activos. Levanta si no encuentra alguna."""
    from pypdf import PdfReader

    r = PdfReader(io.BytesIO(pdf_bytes))
    textos = [(p.extract_text() or "") for p in r.pages]
    def primera(marca, desde=0):
        for i in range(desde, len(textos)):
            if marca.lower() in textos[i].lower():
                return i
        return None
    i_v = primera(_MARCA_VENTAS)
    i_f = primera(_MARCA_FLUJO, (i_v or 0) + 1)
    i_a = primera(_MARCA_ACTIVOS, (i_f or 0) + 1)
    if i_v is None or i_f is None or i_a is None:
        raise RuntimeError("no encuentro las secciones del PDF original "
                           f"(ventas={i_v}, flujo={i_f}, activos={i_a})")
    return {"reader": r, "ventas": i_v, "flujo": i_f, "activos": i_a,
            "n": len(textos)}


def pagina_resultados_corregida(anio: int, mes: int) -> str:
    """El HTML de la hoja de Resultados recalculada (para el PDF y para verla
    en pantalla antes de guardarla)."""
    from flask import render_template

    d = datos_resultados_al_cierre(anio, mes)
    return render_template("informes/cierre_resultados_corregido.html", d=d,
                           hoy=today_ec())


def armar_corregido(anio: int, mes: int) -> tuple[bytes, int]:
    """El paquete de (anio, mes) con Resultados y Flujo de producción
    recalculados y el resto de las páginas del original. Devuelve (bytes,
    páginas). Levanta si falta el original o el navegador."""
    from flask import current_app
    from pypdf import PdfWriter

    from modules._lib import pdf_motor

    original = obtener(anio, mes, 1)
    if not original:
        raise RuntimeError(f"no hay PDF original del cierre de {nombre_mes(mes)} {anio}")
    if not pdf_motor.disponible():
        raise RuntimeError("el servidor no tiene navegador para imprimir (pdf_motor)")
    partes = _partir_original(original)
    r = partes["reader"]

    resultados = pdf_motor.desde_html(pagina_resultados_corregida(anio, mes),
                                      fondo=True)
    uid = _usuario_sistema_id()
    if not uid:
        raise RuntimeError("no hay ningún usuario activo con permiso '*'")
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["last_activity"] = today_ec().isoformat()
    try:
        from modules.informes.views import reset_flujo_produccion_cache
        reset_flujo_produccion_cache()
    except Exception:  # noqa: BLE001
        pass
    flujo = _pdf_de_pagina(client, f"/informes/flujo-produccion?anio={anio}&mes={mes}")

    w = PdfWriter()
    _agregar_paginas(w, resultados)
    for i in range(partes["ventas"], partes["flujo"]):
        w.add_page(r.pages[i])
    _agregar_paginas(w, flujo)
    for i in range(partes["activos"], partes["n"]):
        w.add_page(r.pages[i])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue(), len(w.pages)


def generar_corregido(anio: int, mes: int, usuario: str = "web") -> dict:
    """Arma y guarda la versión 2 del paquete de (anio, mes). Nunca levanta."""
    try:
        from modules.informes import ajuste_cierre
        aj = ajuste_cierre.vivos_del_mes(anio, mes)
        pdf_bytes, paginas = armar_corregido(anio, mes)
        nota = "; ".join(f"{a['motivo']} ({_f(a['importe']):,.0f})".replace(",", ".")
                         for a in aj) or None
        id_paquete = guardar(anio, mes, pdf_bytes, paginas, usuario,
                             version=2, nota=nota)
        return {"aplicado": True, "id_paquete": id_paquete, "paginas": paginas,
                "razon": f"PDF corregido de {nombre_mes(mes)} {anio} guardado "
                         f"({paginas} páginas)."}
    except Exception as e:  # noqa: BLE001
        _LOG.warning("cierre %04d-%02d corregido: %s", anio, mes, e)
        return {"aplicado": False, "razon": f"No se pudo armar: {e}"}
