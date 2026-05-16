"""Persistencia de scintela.factura_electronica.

Capa de datos para el ciclo de vida del comprobante SRI. Este módulo NO
genera XML ni firma ni envía — sólo lee/escribe la tabla.

Reglas:
    - `crear_borrador()` inserta con estado='borrador' y guarda el XML
      generado. Es el único método que acepta una factura "virgen". Si ya
      existe un registro para el id_factura que no esté en estado final
      (autorizado/anulado), se considera regeneración: el viejo se pisa.
    - Cualquier cambio de estado usa audit (`usuario_modifica`,
      `fecha_modifica`). La migración 0009 tiene trigger sólo para
      fecha_modifica; el usuario lo seteamos explícito.
    - No hay DELETE. Un comprobante rechazado queda como histórico; un
      autorizado que hay que revertir exige nota de crédito.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import db
from periodo_guard import asegurar_fecha_abierta

# Estados válidos — espejo del CHECK constraint en la migración 0009.
ESTADO_BORRADOR   = "borrador"
ESTADO_FIRMADO    = "firmado"
ESTADO_ENVIADO    = "enviado"
ESTADO_AUTORIZADO = "autorizado"
ESTADO_RECHAZADO  = "rechazado"
ESTADO_ANULADO    = "anulado"

# Estados "terminales" — ya no se pueden regenerar desde borrador.
_ESTADOS_TERMINALES = (ESTADO_AUTORIZADO, ESTADO_ANULADO)


def por_id(id_fe: int) -> dict | None:
    """Un registro por id_factura_electronica."""
    return db.fetch_one(
        """
        SELECT *
          FROM scintela.factura_electronica
         WHERE id_factura_electronica = %s
        """,
        (id_fe,),
    )


def por_id_factura(id_factura: int) -> dict | None:
    """El registro SRI vigente (más reciente) de una factura de venta.

    Devuelve NULL si la factura todavía no fue emitida electrónicamente.
    En caso de múltiples intentos (un rechazo + una regeneración), trae
    el último.
    """
    return db.fetch_one(
        """
        SELECT *
          FROM scintela.factura_electronica
         WHERE id_factura = %s
         ORDER BY fecha_crea DESC, id_factura_electronica DESC
         LIMIT 1
        """,
        (id_factura,),
    )


def obtener_xml(id_fe: int) -> str | None:
    """Devuelve el XML generado (o firmado si existe) para mostrar al usuario."""
    row = db.fetch_one(
        """
        SELECT xml_firmado, xml_generado
          FROM scintela.factura_electronica
         WHERE id_factura_electronica = %s
        """,
        (id_fe,),
    )
    if not row:
        return None
    # Preferimos el firmado si ya está firmado; si no, el generado.
    return row.get("xml_firmado") or row.get("xml_generado")


def crear_borrador(
    *,
    id_factura: int,
    clave_acceso: str,
    ambiente: str,
    estab: str,
    pto_emi: str,
    secuencial: str,
    fecha_emision: date,
    totales: dict[str, Any],
    xml_generado: str,
    tipo_comprobante: str = "01",
    tipo_emision: str = "1",
    usuario: str = "web",
) -> dict:
    """Inserta (o regenera) el registro SRI en estado borrador.

    Si ya existe un registro para el id_factura y NO está en estado terminal
    (autorizado/anulado), lo regenera (UPDATE con nueva clave_acceso +
    xml). Si está en estado terminal, levanta ValueError — no se puede
    "volver a emitir" una factura que ya fue autorizada.

    Args:
        totales: el dict que devuelve `calcular_totales()` en modules/sri/xml.py.
            Debe tener: subtotal_sin_impuestos, total_iva, importe_total,
            y opcionalmente `por_iva` (lista con una entrada por porcentaje).
        xml_generado: XML ya construido (string UTF-8 con declaración).

    Raises:
        ValueError: si la factura está en estado terminal o si el período
                    contable de `fecha_emision` está cerrado.
    """
    asegurar_fecha_abierta(fecha_emision)

    existente = por_id_factura(id_factura)
    if existente and existente.get("estado") in _ESTADOS_TERMINALES:
        raise ValueError(
            f"La factura {id_factura} ya tiene un comprobante electrónico "
            f"en estado '{existente['estado']}' — no se puede regenerar. "
            "Si hay que revertir, emitir una nota de crédito."
        )

    # Desglose de IVA para el snapshot de totales. Tomamos el bloque con
    # mayor porcentaje no-cero como "gravado"; el 0% va a iva_0.
    iva_0 = 0.0
    iva_grav = 0.0
    iva_pct = 15.0
    for bloque in (totales.get("por_iva") or []):
        pct = float(bloque.get("porcentaje") or 0)
        base = float(bloque.get("base_imponible") or 0)
        if pct == 0:
            iva_0 += base
        else:
            iva_grav += base
            iva_pct = pct  # si hay varios, nos quedamos con el último — caso raro

    params = {
        "id_factura": id_factura,
        "clave_acceso": clave_acceso,
        "ambiente": ambiente,
        "tipo_emision": tipo_emision,
        "tipo_comprobante": tipo_comprobante,
        "estab": estab,
        "pto_emi": pto_emi,
        "secuencial": secuencial,
        "fecha_emision": fecha_emision,
        "subtotal_sin_impuestos": float(totales.get("subtotal_sin_impuestos") or 0),
        "subtotal_iva_0":         iva_0,
        "subtotal_iva_grav":      iva_grav,
        "iva_porcentaje":         iva_pct,
        "total_iva":              float(totales.get("total_iva") or 0),
        "importe_total":          float(totales.get("importe_total") or 0),
        "xml_generado": xml_generado,
        "usuario":     usuario,
    }

    if existente:
        # Regeneración — pisamos el borrador/rechazado anterior. Mantenemos
        # el id_factura_electronica y fecha_crea para trazabilidad.
        with db.tx() as conn:
            row = db.fetch_one(
                """
                UPDATE scintela.factura_electronica
                   SET clave_acceso = %(clave_acceso)s,
                       ambiente = %(ambiente)s,
                       tipo_emision = %(tipo_emision)s,
                       tipo_comprobante = %(tipo_comprobante)s,
                       estab = %(estab)s,
                       pto_emi = %(pto_emi)s,
                       secuencial = %(secuencial)s,
                       fecha_emision = %(fecha_emision)s,
                       subtotal_sin_impuestos = %(subtotal_sin_impuestos)s,
                       subtotal_iva_0 = %(subtotal_iva_0)s,
                       subtotal_iva_grav = %(subtotal_iva_grav)s,
                       iva_porcentaje = %(iva_porcentaje)s,
                       total_iva = %(total_iva)s,
                       importe_total = %(importe_total)s,
                       xml_generado = %(xml_generado)s,
                       xml_firmado = NULL,
                       respuesta_sri = NULL,
                       mensaje_error = NULL,
                       numero_autorizacion = NULL,
                       fecha_autorizacion = NULL,
                       intentos = 0,
                       ultimo_intento_en = NULL,
                       estado = 'borrador',
                       usuario_modifica = %(usuario)s,
                       fecha_modifica = CURRENT_TIMESTAMP
                 WHERE id_factura_electronica = %(id_fe)s
                 RETURNING *
                """,
                {**params, "id_fe": existente["id_factura_electronica"]},
                conn=conn,
            )
            return dict(row) if row else {}

    # Inserción nueva.
    with db.tx() as conn:
        row = db.fetch_one(
            """
            INSERT INTO scintela.factura_electronica (
                id_factura, clave_acceso, ambiente, tipo_emision,
                tipo_comprobante, estab, pto_emi, secuencial,
                fecha_emision,
                subtotal_sin_impuestos, subtotal_iva_0, subtotal_iva_grav,
                iva_porcentaje, total_iva, importe_total,
                xml_generado, estado, usuario_crea
            ) VALUES (
                %(id_factura)s, %(clave_acceso)s, %(ambiente)s, %(tipo_emision)s,
                %(tipo_comprobante)s, %(estab)s, %(pto_emi)s, %(secuencial)s,
                %(fecha_emision)s,
                %(subtotal_sin_impuestos)s, %(subtotal_iva_0)s, %(subtotal_iva_grav)s,
                %(iva_porcentaje)s, %(total_iva)s, %(importe_total)s,
                %(xml_generado)s, 'borrador', %(usuario)s
            )
            RETURNING *
            """,
            params,
            conn=conn,
        )
        return dict(row) if row else {}


def proximo_secuencial(estab: str, pto_emi: str, tipo_comprobante: str = "01") -> int:
    """Devuelve el siguiente número secuencial disponible para este establecimiento.

    Se calcula como MAX(secuencial) + 1 sobre los comprobantes ya emitidos
    electrónicamente para el mismo estab/pto_emi/tipo. Arranca en 1.

    En producción este número debería provenir de una secuencia Postgres
    para ser concurrente-seguro — el MAX+1 tiene race con dos emisiones
    simultáneas. Por ahora alcanza: la fábrica factura serialmente y un
    UNIQUE index sobre clave_acceso atrapa duplicados.
    """
    row = db.fetch_one(
        """
        SELECT COALESCE(MAX(CAST(secuencial AS integer)), 0) + 1 AS siguiente
          FROM scintela.factura_electronica
         WHERE estab = %s AND pto_emi = %s AND tipo_comprobante = %s
        """,
        (estab, pto_emi, tipo_comprobante),
    )
    return int(row["siguiente"]) if row else 1


# =====================================================================
# Ciclo de vida — firma, envío, autorización
# =====================================================================

def guardar_firma(
    *, id_fe: int, xml_firmado: str, usuario: str = "web",
) -> None:
    """Persiste el XML firmado + cambia estado a 'firmado'.

    Sólo válido desde estado 'borrador' o 'rechazado' (se puede re-firmar un
    XML rechazado después de corregirlo y regenerarlo).
    """
    db.execute(
        """
        UPDATE scintela.factura_electronica
           SET xml_firmado = %s,
               estado = 'firmado',
               fecha_modifica = CURRENT_TIMESTAMP,
               usuario_modifica = %s
         WHERE id_factura_electronica = %s
           AND estado IN ('borrador', 'rechazado')
        """,
        (xml_firmado, usuario, id_fe),
    )


def marcar_enviado(*, id_fe: int, usuario: str = "web") -> None:
    """Estado fugaz — se marca al hacer POST al SRI antes de recibir respuesta.

    Si el flujo es `enviar + esperar + consultar_autorizacion` en la misma
    request, este estado dura segundos. Útil para auditar que un comprobante
    efectivamente llegó al SRI, aun si la autorización posterior falla o el
    app crashea antes de recibirla.
    """
    db.execute(
        """
        UPDATE scintela.factura_electronica
           SET estado = 'enviado',
               ultimo_intento_en = CURRENT_TIMESTAMP,
               intentos = intentos + 1,
               fecha_modifica = CURRENT_TIMESTAMP,
               usuario_modifica = %s
         WHERE id_factura_electronica = %s
        """,
        (usuario, id_fe),
    )


def actualizar_respuesta_recepcion(
    *, id_fe: int, respuesta: dict, usuario: str = "web",
) -> None:
    """Post-Recepción. `respuesta` viene de `envio.enviar_a_recepcion()`.

    Si RECIBIDA, queda en 'enviado' (esperando autorización).
    Si DEVUELTA, pasa a 'rechazado' con el primer mensaje como error legible.
    """
    import json
    estado_sri = (respuesta.get("estado") or "").upper()
    mensajes = respuesta.get("mensajes") or []
    if estado_sri == "RECIBIDA":
        nuevo_estado = "enviado"
        mensaje_error = None
    elif estado_sri == "DEVUELTA":
        nuevo_estado = "rechazado"
        # Armar un resumen legible para mostrar al usuario.
        if mensajes:
            m0 = mensajes[0]
            mensaje_error = (
                f"{m0.get('identificador', '')} — {m0.get('mensaje', '')}. "
                f"{m0.get('informacionAdicional', '')}".strip()
            )
        else:
            mensaje_error = "SRI devolvió el comprobante sin mensaje legible."
    else:
        nuevo_estado = "rechazado"
        mensaje_error = f"Respuesta SRI desconocida: {estado_sri}"

    db.execute(
        """
        UPDATE scintela.factura_electronica
           SET estado = %s,
               respuesta_sri = %s::jsonb,
               mensaje_error = %s,
               fecha_modifica = CURRENT_TIMESTAMP,
               usuario_modifica = %s
         WHERE id_factura_electronica = %s
        """,
        (nuevo_estado, json.dumps(respuesta, default=str), mensaje_error, usuario, id_fe),
    )


def crear_borrador_nota_credito(
    *,
    id_factura_origen: int,
    clave_acceso: str,
    ambiente: str,
    estab: str,
    pto_emi: str,
    secuencial: str,
    fecha_emision: date,
    totales: dict[str, Any],
    xml_generado: str,
    num_doc_modificado: str,
    fecha_emision_doc_sustento: date,
    motivo: str,
    tipo_emision: str = "1",
    usuario: str = "web",
) -> dict:
    """Inserta un registro SRI como NC (tipo_comprobante='04') en estado borrador.

    A diferencia de `crear_borrador()` (que es para facturas 1:1), una factura
    puede tener MÚLTIPLES notas de crédito contra ella. Cada NC es su propio
    registro independiente con su propia clave_acceso única.

    Guardamos el motivo + el doc modificado en el `respuesta_sri` JSONB como
    metadata, y en `mensaje_error` dejamos un resumen legible (que NO es un
    error — pero el campo sirve como "nota del operador"; se re-escribe si
    hay un error SRI real después).
    """
    import json

    from periodo_guard import asegurar_fecha_abierta
    asegurar_fecha_abierta(fecha_emision)

    # Metadata en JSONB — sobrescribe cualquier intento anterior.
    meta = {
        "doc_modificado": {
            "numero": num_doc_modificado,
            "fecha_emision": fecha_emision_doc_sustento.isoformat(),
        },
        "motivo": motivo,
        "id_factura_origen": id_factura_origen,
    }

    row = db.fetch_one(
        """
        INSERT INTO scintela.factura_electronica (
            id_factura, clave_acceso, ambiente, tipo_emision, tipo_comprobante,
            estab, pto_emi, secuencial, fecha_emision,
            subtotal_sin_impuestos, subtotal_iva_0, subtotal_iva_grav,
            iva_porcentaje, total_iva, importe_total,
            estado, xml_generado, respuesta_sri, mensaje_error, usuario_crea
        ) VALUES (
            %s, %s, %s, %s, '04',
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            'borrador', %s, %s::jsonb, %s, %s
        )
        RETURNING id_factura_electronica
        """,
        (
            id_factura_origen, clave_acceso, ambiente, tipo_emision,
            estab, pto_emi, secuencial, fecha_emision,
            totales.get("subtotal_sin_impuestos", 0),
            0, totales.get("subtotal_sin_impuestos", 0),  # todo lo gravado en la NC
            totales.get("iva_porcentaje", 15),
            totales.get("total_iva", 0),
            totales.get("valor_modificacion", 0),
            xml_generado, json.dumps(meta, default=str),
            f"NC contra {num_doc_modificado}: {motivo}",
            usuario,
        ),
    )
    return {"id_factura_electronica": int(row["id_factura_electronica"])} if row else {}


def actualizar_respuesta_autorizacion(
    *, id_fe: int, respuesta: dict, usuario: str = "web",
) -> None:
    """Post-Autorización. Transiciones terminales (autorizado | rechazado) o
    sigue en 'enviado' si el SRI contesta EN PROCESO.
    """
    import json
    estado_sri = (respuesta.get("estado") or "").upper()
    mensajes = respuesta.get("mensajes") or []
    num = respuesta.get("numero_autorizacion")
    fecha_aut = respuesta.get("fecha_autorizacion")

    if estado_sri == "AUTORIZADO":
        nuevo_estado = "autorizado"
        mensaje_error = None
    elif estado_sri == "NO AUTORIZADO":
        nuevo_estado = "rechazado"
        if mensajes:
            m0 = mensajes[0]
            mensaje_error = (
                f"{m0.get('identificador', '')} — {m0.get('mensaje', '')}. "
                f"{m0.get('informacionAdicional', '')}".strip()
            )
        else:
            mensaje_error = "SRI no autorizó el comprobante (sin detalle)."
    elif estado_sri == "EN PROCESO":
        # No transicionamos — reintentamos el poll más tarde.
        return
    else:
        nuevo_estado = "rechazado"
        mensaje_error = f"Respuesta SRI desconocida en autorización: {estado_sri}"

    db.execute(
        """
        UPDATE scintela.factura_electronica
           SET estado = %s,
               numero_autorizacion = %s,
               fecha_autorizacion = %s,
               respuesta_sri = %s::jsonb,
               mensaje_error = %s,
               fecha_modifica = CURRENT_TIMESTAMP,
               usuario_modifica = %s
         WHERE id_factura_electronica = %s
        """,
        (nuevo_estado, num, fecha_aut,
         json.dumps(respuesta, default=str), mensaje_error, usuario, id_fe),
    )
