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
# (`ajuste_cierre`). *"¿Podés rearmar el PDF? (...) subilo como un segundo
# PDF"* — y al ver una hoja nueva: *"¿por qué hacés otro formato?"*.
#
# La hoja de Resultados del PDF es la pantalla de la noche del cierre, y esa
# pantalla hoy calcula el mes en curso: no se puede volver a pedir. Así que
# es LA MISMA página del original, y sólo se reemplazan los números que el
# ajuste cambia (utilidad, kilos y $ de las etapas ajustadas, stock, total
# activo, patrimonio, utilidades del año), en amarillo y recalculados a
# partir de los números de la propia página. Arriba, una línea dice qué se
# corrigió y cuándo.
#
# El Flujo de producción sí se vuelve a pedir: la pantalla del mes CERRADO se
# arma con los movimientos de Asinfo, que ya están corregidos. Ventas,
# Cartera, Deudas, Gastos, Activos y Anticipos no cambian con un ajuste de
# stock: van las páginas del original, tal cual.

#: Texto con el que empieza cada sección en el PDF original (para partirlo).
_MARCA_VENTAS = "VENTAS DEL MES"
_MARCA_FLUJO = "MOVIMIENTOS DEL MES"
_MARCA_ACTIVOS = "Activos fijos"

#: Rótulo de la fila en la hoja de Resultados por etapa del ajuste.
_FILA_ETAPA = {"hilado": "Hilado", "tejido": "Tejido", "terminado": "Terminado"}

#: Anchos de Helvetica (milésimas de em) — para tapar y escribir el número
#: nuevo del mismo ancho que el viejo.
_ANCHO_HELV = {c: 556 for c in "0123456789"} | {".": 278, ",": 278, "-": 333,
                                                 " ": 278}


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _num_pagina(t: str) -> float | None:
    """'2.231.232' → 2231232 · '2,328' → 2.328 · '-7.139' → -7139."""
    t = (t or "").strip().replace("\u2212", "-")
    if not t or not any(ch.isdigit() for ch in t):
        return None
    t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _fmt(v: float, dec: int = 0) -> str:
    s = f"{abs(v):,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if v < 0 and round(abs(v), dec) else "") + s


def _anchos_fuente(fuente) -> tuple[dict, float, int]:
    """(ancho por código, ancho por defecto, bytes por código) de una fuente
    del PDF, en milésimas de em. Type0 (lo que escribe Chromium): /W del
    descendiente, códigos de 2 bytes. Simple: /Widths desde /FirstChar."""
    f = fuente.get_object()
    if str(f.get("/Subtype")) == "/Type0":
        d = f["/DescendantFonts"][0].get_object()
        dw = float(d.get("/DW") or 1000)
        w: dict = {}
        arr = list(d.get("/W") or [])
        i = 0
        while i < len(arr):
            c0 = int(arr[i])
            nxt = arr[i + 1]
            if isinstance(nxt, list | tuple) or hasattr(nxt, "__iter__"):
                for k, v in enumerate(list(nxt)):
                    w[c0 + k] = float(v)
                i += 2
            else:
                for c in range(c0, int(nxt) + 1):
                    w[c] = float(arr[i + 2])
                i += 3
        return w, dw, 2
    primero = int(f.get("/FirstChar") or 0)
    w = {primero + k: float(v) for k, v in enumerate(list(f.get("/Widths") or []))}
    return w, 500.0, 1


def _ancho_tj(args, op, anchos) -> float:
    """Ancho en unidades de texto (× tamaño de letra) de un Tj/TJ."""
    if not anchos:
        return 0.0
    w, dw, n = anchos
    partes = args[0] if op == b"TJ" and args else (args[-1:] if args else [])
    total = 0.0
    for p in partes:
        if isinstance(p, bytes | str) or hasattr(p, "original_bytes"):
            b = getattr(p, "original_bytes", None) or (p if isinstance(p, bytes)
                                                        else str(p).encode("latin-1", "ignore"))
            for i in range(0, len(b) - n + 1, n):
                total += w.get(int.from_bytes(b[i:i + n], "big"), dw)
        else:
            try:
                total -= float(p)          # ajuste de TJ, en milésimas
            except (TypeError, ValueError) as e:
                _LOG.debug("TJ con un elemento raro (%r): %s", p, e)
    return total / 1000.0


def _textos_con_posicion(page) -> list[dict]:
    """Cada texto de la página con su x, y, tamaño de letra y ANCHO reales.

    🚨 pypdf le pasa a `visitor_text` una matriz de texto VIEJA cuando el
    PDF lo hizo Chromium (posiciona cada celda con Tm/Td y el texto se junta
    después): todos los números caían en el mismo punto. La posición buena es
    la de los Tj/TJ desde la última vez que se entregó texto, que sí llega
    bien a `visitor_operand_before`. El ancho sale de las métricas de la
    fuente del propio PDF (así el número nuevo termina donde terminaba el
    viejo, que es como están alineadas las columnas)."""
    out: list[dict] = []
    pend: dict = {"tjs": []}
    fuentes: dict = {}
    try:
        for k, v in (page["/Resources"]["/Font"] or {}).items():
            fo = v.get_object()
            try:
                an = _anchos_fuente(fo)
            except Exception:  # noqa: BLE001 -- sin anchos se estima
                an = None
            fuentes[str(k)] = (str(fo.get("/BaseFont") or ""), an)
    except (KeyError, TypeError, AttributeError) as e:
        _LOG.warning("hoja corregida: la página no tiene fuentes legibles (%s)", e)

    def _xy(cm, tm, dx=0.0):
        tx, ty = tm[4] + dx * tm[0], tm[5] + dx * tm[1]
        return (tx * cm[0] + ty * cm[2] + cm[4], tx * cm[1] + ty * cm[3] + cm[5])

    def _antes(op, args, cm, tm):
        if op == b"Tf" and args and len(args) > 1:
            pend["tf"] = float(args[1])
            pend["fuente"] = str(args[0])
        if op in (b"Tj", b"TJ", b"'", b'"'):
            tf = pend.get("tf") or 0.0
            base, an = fuentes.get(pend.get("fuente") or "", ("", None))
            ancho = _ancho_tj(args, op, an) * tf
            x0, y0 = _xy(cm, tm)
            x1, _ = _xy(cm, tm, ancho)
            esc = abs(tm[0] * cm[0] + tm[1] * cm[2]) or 1.0
            pend["tjs"].append({"x": x0, "y": y0, "x1": x1 if an else None,
                                "fs": abs(tf) * esc, "bold": "bold" in base.lower()})

    def _texto(text, cm, tm, font_dict, font_size):
        tjs, pend["tjs"] = pend["tjs"], []
        t = (text or "").strip()
        if not t or not tjs:
            return
        p0 = tjs[0]
        fin = [j["x1"] for j in tjs if j["x1"] is not None]
        out.append({"t": t, "x": p0["x"], "y": p0["y"], "fs": p0["fs"],
                    "bold": p0["bold"], "x_fin": max(fin) if fin else None})

    page.extract_text(visitor_operand_before=_antes, visitor_text=_texto)
    return out


def _fila(textos: list[dict], rotulo: str) -> list[dict]:
    """Los números de la fila que empieza con `rotulo`, de izquierda a derecha."""
    lab = next((t for t in textos if t["t"] == rotulo), None)
    if lab is None:
        return []
    nums = [t for t in textos if abs(t["y"] - lab["y"]) <= 1.6
            and t is not lab and _num_pagina(t["t"]) is not None
            and t["x"] > lab["x"]]
    return sorted(nums, key=lambda t: t["x"])


def _ancho_helv(t: str, fs: float) -> float:
    return sum(_ANCHO_HELV.get(ch, 556) for ch in t) / 1000.0 * fs


def _pdf_texto(t: str) -> str:
    b = t.encode("cp1252", "replace").decode("latin-1")
    return b.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def cambios_hoja_resultados(textos: list[dict], ajustes: list[dict],
                            anio: int) -> list[dict]:
    """Qué número de la hoja de Resultados se reemplaza por cuál. Todo sale de
    los números de la propia hoja más los ajustes, así el resto de la página
    sigue cerrando con lo nuevo. Levanta si no encuentra una fila."""
    imp = round(sum(_f(a.get("importe")) for a in ajustes), 2)
    por_etapa: dict = {}
    for a in ajustes:
        for ln in a.get("lineas") or []:
            if ln.get("etapa") == "revaluacion":
                # El $/kg del cierre rehecho: cambia el $/kg y el U$ de cada
                # etapa (el hilo arrastra al tejido y al terminado).
                for e, r in (ln.get("por_etapa") or {}).items():
                    d = por_etapa.setdefault(e, {"kg": 0.0, "us": 0.0})
                    d["us"] += _f(r.get("importe"))
                    d["ukg"] = _f(r.get("ukg_despues"))
                continue
            e = por_etapa.setdefault(ln.get("etapa"), {"kg": 0.0, "us": 0.0})
            e["kg"] += _f(ln.get("kg"))
            e["us"] += _f(ln.get("importe"))
    cambios: list[dict] = []

    def fila(rotulo, n):
        f = _fila(textos, rotulo)
        if len(f) < n:
            raise RuntimeError(f"no encuentro la fila «{rotulo}» en la hoja de Resultados")
        return f

    def cambio(run, nuevo):
        if run["t"] != nuevo:
            cambios.append(dict(run, nuevo=nuevo))

    ventas_kg = _num_pagina(fila("Ventas", 1)[0]["t"]) or 0
    ut = fila("Utilidad Real", 2)
    ut_us = _num_pagina(ut[-1]["t"]) + imp
    cambio(ut[-1], _fmt(ut_us))
    if ventas_kg:
        cambio(ut[-2], _fmt(ut_us / ventas_kg, 3))
    ua = fila(f"Utilidades {anio}", 1)
    cambio(ua[-1], _fmt(_num_pagina(ua[-1]["t"]) + imp))
    for e, d in por_etapa.items():
        if e not in _FILA_ETAPA:
            continue
        r = fila(_FILA_ETAPA[e], 3)
        cambio(r[0], _fmt(_num_pagina(r[0]["t"]) + d["kg"]))
        if d.get("ukg"):
            cambio(r[1], _fmt(d["ukg"], 3))
        cambio(r[-1], _fmt(_num_pagina(r[-1]["t"]) + d["us"]))
    st = fila("Stock MP+Prod.", 3)
    kg = _num_pagina(st[0]["t"]) + sum(d["kg"] for d in por_etapa.values())
    us = _num_pagina(st[-1]["t"]) + imp
    cambio(st[0], _fmt(kg))
    cambio(st[-1], _fmt(us))
    if kg:
        cambio(st[1], _fmt(us / kg, 3))
    for rot in ("Total activo", "Patrimonio neto"):
        r = fila(rot, 1)
        cambio(r[-1], _fmt(_num_pagina(r[-1]["t"]) + imp))
    return cambios


def _capa(ancho: float, alto: float, cambios: list[dict], leyenda: str):
    """Una página transparente con los números nuevos (sobre un fondo amarillo
    que tapa el viejo) y la leyenda arriba, para estampar sobre la original."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    ops = []
    for c in cambios:
        fs = c["fs"] or 9.0
        fuente = "HB" if c["bold"] else "HR"
        # El número nuevo termina donde terminaba el viejo (las columnas están
        # alineadas a la derecha). Sin el ancho real, se estima con Helvetica.
        x_der = c.get("x_fin") or (c["x"] + _ancho_helv(c["t"], fs) * 0.95)
        w_viejo = x_der - c["x"]
        w_nuevo = _ancho_helv(c["nuevo"], fs)
        tz = max(80.0, min(110.0, 100.0 * w_viejo / w_nuevo)) if (
            w_nuevo and len(c["nuevo"]) == len(c["t"])) else 100.0
        x = x_der - w_nuevo * tz / 100.0
        izq = min(x, c["x"]) - 1.2
        ops.append(f"q 0.996 0.953 0.78 rg {izq:.2f} {c['y'] - 2.4:.2f} "
                   f"{x_der - izq + 1.2:.2f} {fs * 0.98 + 2.8:.2f} re f Q")
        ops.append(f"BT /{fuente} {fs:.2f} Tf {tz:.1f} Tz 0.06 0.09 0.16 rg "
                   f"1 0 0 1 {x:.2f} {c['y']:.2f} Tm ({_pdf_texto(c['nuevo'])}) Tj ET")
    if leyenda:
        ops.append(f"BT /HB 8.5 Tf 0.57 0.25 0.05 rg 1 0 0 1 38 {alto - 22:.2f} Tm "
                   f"({_pdf_texto(leyenda)}) Tj ET")
    w = PdfWriter()
    pag = w.add_blank_page(width=ancho, height=alto)

    def _helv(nombre):
        return DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject(nombre),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding")})
    pag[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/HR"): _helv("/Helvetica"),
            NameObject("/HB"): _helv("/Helvetica-Bold")})})
    st = DecodedStreamObject()
    st.set_data("\n".join(ops).encode("latin-1"))
    pag[NameObject("/Contents")] = w._add_object(st)
    return pag


def hoja_resultados_corregida(anio: int, mes: int) -> tuple[bytes, list[dict]]:
    """La página de Resultados del PDF original con los números del ajuste
    reemplazados. Devuelve (pdf de una página, lista de cambios)."""
    from pypdf import PdfReader, PdfWriter

    from modules.informes import ajuste_cierre

    original = obtener(anio, mes, 1)
    if not original:
        raise RuntimeError(f"no hay PDF original del cierre de {nombre_mes(mes)} {anio}")
    ajustes = ajuste_cierre.vivos_del_mes(anio, mes)
    if not ajustes:
        raise RuntimeError(f"el cierre de {nombre_mes(mes)} {anio} no tiene ajustes")
    page = PdfReader(io.BytesIO(original)).pages[0]
    textos = _textos_con_posicion(page)
    cambios = cambios_hoja_resultados(textos, ajustes, anio)
    imp = sum(_f(a.get("importe")) for a in ajustes)
    leyenda = (f"Corregido el {today_ec():%d/%m/%Y} · ajuste al cierre {_fmt(imp)}: "
               + "; ".join(str(a.get("motivo") or "") for a in ajustes))[:150]
    ancho, alto = float(page.mediabox.width), float(page.mediabox.height)
    page.merge_page(_capa(ancho, alto, cambios, leyenda))
    w = PdfWriter()
    w.add_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue(), cambios


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

    resultados, _cambios = hoja_resultados_corregida(anio, mes)
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
    for i in range(1, partes["flujo"]):   # el resto de Resultados, si hay, y Ventas..Gastos
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
