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

#: (título de la sección, ruta a pedirle a la app). El orden es el mismo
#: en el que se archivaban las capturas del dBase.
PAGINAS: tuple[tuple[str, str], ...] = (
    ("Informe Resultados — Balance", "/informes/balance"),
    ("Ventas del mes", "/informes/ventas"),
    # TMT 2026-08-31: /cartera/aging es la pantalla OPERATIVA (buckets de
    # mora, botón "stop automático") -- no lo que se archiva cada mes.
    # /informes/cartera es el resumen simple (CLI/CHQ/FAC/TOT/%), réplica
    # del CARTERA del dBase, con su propia vista compacta de 3 columnas
    # para impresión (ver cartera.html).
    ("Cartera", "/informes/cartera"),
    ("Deudas", "/informes/deudas"),
    ("Gastos del mes", "/informes/gastos"),
    ("Flujo de producción", "/informes/flujo-produccion"),
    ("Activos fijos", "/activos"),
    ("Anticipos", "/dolares"),
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


def _pdf_de_pagina(client, ruta: str) -> bytes:
    """Le pide `ruta` al test client (ya logueado) y devuelve el PDF de esa
    página. Levanta si la página no respondió 200 o si no hay navegador."""
    from modules._lib import pdf_motor

    resp = client.get(ruta, follow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"{ruta} respondió {resp.status_code}")
    html = resp.get_data(as_text=True)
    return pdf_motor.desde_html(html)


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
    for titulo, ruta in PAGINAS:
        try:
            pdf_bytes = _pdf_de_pagina(client, ruta)
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
            usuario: str) -> int:
    """UPSERT del paquete de (anio, mes) -- se puede regrabar, igual que la
    foto de `scintela.historia`: la fila vieja se pisa, no se acumula."""
    res = db.execute_returning(
        """
        INSERT INTO scintela.cierre_paquete
            (anio, mes, pdf, tamano_bytes, paginas, generado_por)
        VALUES (%(anio)s, %(mes)s, %(pdf)s, %(tam)s, %(paginas)s, %(usuario)s)
        ON CONFLICT (anio, mes) DO UPDATE
           SET pdf = EXCLUDED.pdf,
               tamano_bytes = EXCLUDED.tamano_bytes,
               paginas = EXCLUDED.paginas,
               generado_por = EXCLUDED.generado_por,
               generado_en = now()
         RETURNING id_paquete
        """,
        {
            "anio": anio, "mes": mes,
            "pdf": pdf_bytes, "tam": len(pdf_bytes),
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


def listar() -> list[dict]:
    """Los paquetes ya generados, del más nuevo al más viejo."""
    filas = db.fetch_all(
        """
        SELECT anio, mes, tamano_bytes, paginas, generado_en, generado_por
          FROM scintela.cierre_paquete
         ORDER BY anio DESC, mes DESC
        """
    )
    for f in filas:
        f["mes_nombre"] = nombre_mes(f["mes"])
    return filas


def obtener(anio: int, mes: int) -> bytes | None:
    row = db.fetch_one(
        "SELECT pdf FROM scintela.cierre_paquete WHERE anio = %s AND mes = %s",
        (anio, mes),
    )
    pdf = (row or {}).get("pdf")
    # psycopg2 devuelve bytea como memoryview -- Response/send_file quieren
    # bytes de verdad.
    return bytes(pdf) if pdf is not None else None
