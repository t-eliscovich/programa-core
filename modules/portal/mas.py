"""Lo que el cliente ve desde "Más": cómo pagar, sus datos, su año en kilos,
sus pedidos, avisar un pago, la actividad. Rediseño 04/09/2026.

Regla de la casa: el portal no calcula plata. Lo que suma sale de
`informes.queries`; acá se arma la pantalla y se guardan los AVISOS que deja
el cliente (que nunca escriben en la ficha ni en la cartera: los atiende la
oficina por sus pantallas).
"""
from __future__ import annotations

import logging

import db
from filters import today_ec

from . import presentacion

_LOG = logging.getLogger("programa_core.portal")

CLAVE_COMO_PAGAR = "portal_como_pagar"

TIPOS_DE_PAGO = {
    "transferencia": "Transferencia",
    "deposito": "Depósito",
    "cheque": "Cheque",
    "efectivo": "Efectivo",
}

TOPE_ARCHIVO = 5 * 1024 * 1024
TIPOS_ARCHIVO = ("image/jpeg", "image/png", "image/webp", "application/pdf")


# ---------------------------------------------------------------------------
# Cómo pagar
# ---------------------------------------------------------------------------

def como_pagar() -> str:
    try:
        r = db.fetch_one("SELECT valor FROM scintela.nota_config WHERE clave = %s",
                         (CLAVE_COMO_PAGAR,))
        return ((r or {}).get("valor") or "").strip()
    except Exception as e:  # noqa: BLE001 -- sin la fila, la pantalla lo dice
        _LOG.warning("portal: no pude leer cómo pagar (%s)", e)
        return ""


def guardar_como_pagar(texto: str) -> None:
    db.execute(
        "INSERT INTO scintela.nota_config (clave, valor) VALUES (%s, %s) "
        "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
        (CLAVE_COMO_PAGAR, (texto or "").strip()[:2000]))


# ---------------------------------------------------------------------------
# Su año en kilos
# ---------------------------------------------------------------------------

def anio_en_kilos(cod: str) -> dict:
    """``{"meses": [{"mes": date, "etiqueta": "Sep", "kg": float,
    "importe": float, "pct": 0..100}], "kg": total, "importe": total,
    "max_kg": float}`` — los últimos 12 meses, con los que no compró en 0."""
    from datetime import date

    from modules.informes import queries as q

    filas = {f["mes"]: f for f in q.compras_por_mes_cliente(cod, 12)}
    hoy = today_ec()
    meses = []
    y, m = hoy.year, hoy.month
    for _ in range(12):
        meses.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    meses.reverse()
    salida = []
    for d in meses:
        f = filas.get(d) or {}
        salida.append({"mes": d,
                       "etiqueta": presentacion.MESES[d.month - 1][:3].capitalize(),
                       "kg": presentacion.numero(f.get("kg")),
                       "importe": presentacion.numero(f.get("importe")),
                       "facturas": int(f.get("facturas") or 0)})
    max_kg = max((x["kg"] for x in salida), default=0.0)
    for x in salida:
        x["pct"] = round(100 * x["kg"] / max_kg) if max_kg > 0 else 0
    return {"meses": salida, "kg": sum(x["kg"] for x in salida),
            "importe": sum(x["importe"] for x in salida), "max_kg": max_kg}


# ---------------------------------------------------------------------------
# Sus pedidos (los mismos que ve su vendedor, filtrados por SU código)
# ---------------------------------------------------------------------------

def pedidos_de(cod: str) -> dict:
    """``{"ok": bool, "pedidos": [...], "etapas": {numero: ...}}``."""
    from modules.pedidos import service as pedidos_service

    try:
        todos, ok = pedidos_service.por_pedido()
    except Exception as e:  # noqa: BLE001 -- el puente no puede tumbar la pantalla
        _LOG.warning("portal: pedidos no disponibles (%s)", e)
        return {"ok": False, "pedidos": [], "etapas": {}}
    if not ok:
        return {"ok": False, "pedidos": [], "etapas": {}}
    cod = (cod or "").strip().upper()
    mios = [p for p in todos if (p.get("codigo_cliente") or "").strip().upper() == cod]
    etapas = {}
    if mios:
        try:
            from modules._lib import formulas_memos
            estados = formulas_memos.estados([p["numero"] for p in mios])
            activos = {n: v for n, v in estados.items() if v.get("estado") != "cancelado"}
            etapas = pedidos_service.etapas_por_pedido(mios, activos)
        except Exception as e:  # noqa: BLE001 -- sin etapas, la lista igual sirve
            _LOG.warning("portal: etapas de pedidos no disponibles (%s)", e)
    return {"ok": True, "pedidos": mios, "etapas": etapas}


# ---------------------------------------------------------------------------
# Avisar un pago
# ---------------------------------------------------------------------------

def guardar_aviso_pago(cod: str, tipo: str, importe, fecha, referencia: str,
                       nota: str, archivo=None) -> tuple[bool, str]:
    """Deja el aviso. `archivo` es el FileStorage del form o None.
    Devuelve (ok, mensaje para el cliente)."""
    tipo = (tipo or "").strip().lower()
    if tipo not in TIPOS_DE_PAGO:
        return False, "Elija qué tipo de pago hizo."
    try:
        imp = float(str(importe or "").replace(".", "").replace(",", ".")) if importe else None
    except ValueError:
        return False, "El importe no se entiende. Escríbalo como 1234,56."
    if imp is not None and imp <= 0:
        return False, "El importe tiene que ser mayor que cero."
    datos = None
    nombre = ctype = None
    if archivo is not None and getattr(archivo, "filename", ""):
        ctype = (archivo.mimetype or "").lower()
        if ctype not in TIPOS_ARCHIVO:
            return False, "El comprobante tiene que ser una foto (JPG, PNG) o un PDF."
        datos = archivo.read()
        if len(datos) > TOPE_ARCHIVO:
            return False, "El archivo es muy pesado (más de 5 MB). Pruebe con una foto más chica."
        nombre = (archivo.filename or "comprobante")[:120]
    db.execute(
        """
        INSERT INTO scintela.portal_aviso_pago
               (codigo_cli, tipo, importe, fecha, referencia, nota,
                archivo, archivo_nombre, archivo_tipo)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        ((cod or "")[:20], tipo, imp, fecha or None, (referencia or "").strip()[:60] or None,
         (nota or "").strip()[:400] or None, datos, nombre, ctype))
    return True, "Recibimos su aviso. La oficina lo revisa y lo aplica a su cuenta."


def avisos_de_pago_de(cod: str, limite: int = 20) -> list[dict]:
    return db.fetch_all(
        """
        SELECT id_aviso_pago, tipo, importe, fecha, referencia, nota,
               archivo_nombre, creado_en, atendido_en
          FROM scintela.portal_aviso_pago
         WHERE UPPER(TRIM(codigo_cli)) = %s
         ORDER BY creado_en DESC
         LIMIT %s
        """,
        ((cod or "").strip().upper(), limite)) or []


# ---------------------------------------------------------------------------
# Pedir que corrijan sus datos
# ---------------------------------------------------------------------------

def pedir_correccion(cod: str, nombre: str, vend: str, texto: str) -> bool:
    """Un aviso a la campanita de la oficina (y a la del vendedor, que es la
    misma campanita), con lo que el cliente escribió. NO toca la ficha."""
    from modules.avisos import queries as avisos

    texto = (texto or "").strip()[:600]
    if not texto:
        return False
    return avisos.avisar(
        fuente="portal", nivel="ok",
        titulo=f"{cod} pide corregir sus datos",
        detalle=f"{presentacion.nombre_lindo(nombre)} (vendedor {vend or '—'}) escribió desde el portal:\n{texto}",
        url=f"/clientes/{cod}/editar")


# ---------------------------------------------------------------------------
# Actividad: todo lo que pasó en su cuenta, mezclado y por fecha
# ---------------------------------------------------------------------------

def actividad(facturas: list[dict], pagos: list[dict], despachos: list | None,
              pedidos: list[dict]) -> list[dict]:
    """Una sola lista ``{"fecha", "tipo", "titulo", "detalle", "importe",
    "url"}`` del más nuevo al más viejo."""
    items = []
    for f in facturas:
        neg = presentacion.es_negativa(f)
        items.append({
            "fecha": f.get("fecha"), "tipo": "credito" if neg else "factura",
            "titulo": f"{'Devolución' if neg else 'Factura'} {f.get('numf')}",
            "detalle": (f.get("estado_cliente") or {}).get("texto", ""),
            "importe": abs(presentacion.numero(f.get("importe"))),
            "url": f"/factura/{f.get('numf')}?doc={f.get('numf_completo') or ''}&id={f.get('id_factura') or ''}",
        })
    for p in pagos:
        num = (p.get("no_cheque") or "").strip()
        items.append({
            "fecha": p.get("dia_ingreso") or p.get("fecha_recibido") or p.get("fecha"),
            "tipo": "pago",
            "titulo": f"Recibimos su {p.get('que_es', 'pago').lower()}{(' ' + num) if num else ''}",
            "detalle": (p.get("nombre_banco") or "").title(),
            "importe": presentacion.numero(p.get("importe")), "url": "/mis-pagos",
        })
    for g in (despachos or []):
        partes = []
        if g.get("rollos"):
            partes.append(f"{g['rollos']} rollo{'s' if g['rollos'] != 1 else ''}")
        if g.get("unidades"):
            partes.append(f"{g['unidades']} un.")
        items.append({
            "fecha": g.get("dia"), "tipo": "despacho",
            "titulo": f"Despacho {g.get('corto')}", "detalle": " · ".join(partes),
            "importe": None, "url": f"/despacho/{g.get('numero')}",
        })
    for p in pedidos:
        items.append({
            "fecha": p.get("fecha"), "tipo": "pedido",
            "titulo": f"Pedido {p.get('numero')}",
            "detalle": f"{p.get('n_lineas', 0)} línea{'s' if p.get('n_lineas', 0) != 1 else ''}",
            "importe": None, "url": "/pedidos",
        })
    return presentacion.ordenar_por_fecha(items, "fecha")
