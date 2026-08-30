"""Bridge a Asinfo (ERP SQL Server) vía Metabase API.

Asinfo no es alcanzable directo desde Programa Core (firewall + dialecto
SQL Server). Pero Metabase corre en el mismo EC2 que Programa Core, ya
tiene la conexión configurada como Database 2, y tiene cards SQL escritas,
debugueadas y auditadas para los reportes principales del ERP.

Este módulo expone esas cards como funciones planas. Cada función:

    - Resuelve el card_id desde una env var con prefijo ASINFO_CARD_*.
      Eso permite rotar cards (corrigieron un bug → nueva versión) sin
      tocar código.
    - Llama metabase_client.fetch_card(card_id), que devuelve list[dict].
    - Devuelve los rows tal cual — sin parsear a dataclass, porque cada
      card tiene su propio shape de columnas que ya está definido en
      Metabase. Si más adelante una vista necesita estructura tipada,
      se agrega un dataclass específico encima.

Las funciones son fail-soft (lo que devuelve metabase_client): si Metabase
está caído o las env vars no están seteadas, retornan [].

Env vars que lee:
    METABASE_URL, METABASE_USERNAME, METABASE_PASSWORD  (vía metabase_client)
    ASINFO_CARD_VENDEDOR_USD     — card_id de "Vendedor Comparativo Interanual USD"
    ASINFO_CARD_VENDEDOR_KG      — card_id de "Vendedor Kg"
    ASINFO_CARD_CLIENTE_KG       — card_id de "Cliente Kg"

Las cards canónicas al día de hoy (ver `intela-aws-deploy` SKILL):
    116 — Vendedor - Comparativo Interanual (USD)
    163 — Vendedor Kg - Comparativo Interanual
    164 — Cliente Kg - Comparativo Interanual
"""

from __future__ import annotations

import logging
import os
import threading as _threading
import time as _time

from modules._lib import metabase_client

_LOG = logging.getLogger("programa_core.asinfo")


# ---------------------------------------------------------------------------
# Helper genérico
# ---------------------------------------------------------------------------


def fetch_card_from_env(env_var: str, params: list[dict] | None = None) -> list[dict]:
    """Lee la card cuyo ID está en la env var y la ejecuta vía Metabase.

    Args:
        env_var: nombre de la env var que contiene el card_id (ej.
                 "ASINFO_CARD_VENDEDOR_USD").
        params: parameters opcionales para template-tags de la card,
                en el formato Metabase ({"type", "target", "value"}).

    Returns:
        Lista de rows (dicts). [] si la env var está vacía o Metabase falla.
    """
    card_id = os.environ.get(env_var, "").strip()
    if not card_id:
        _LOG.info("%s vacío — devolviendo []", env_var)
        return []
    return metabase_client.fetch_card(card_id, params=params)


# ---------------------------------------------------------------------------
# Wrappers nominales — las cards canónicas de ERP
# ---------------------------------------------------------------------------


def ventas_vendedor_usd(vendedor: str | None = None) -> list[dict]:
    """Comparativo interanual de ventas en USD por vendedor.

    Si la card tiene un template-tag `{{vendedor}}` (opcional), se le pasa
    el filtro. Si no, devuelve todos los vendedores.
    """
    params = None
    if vendedor:
        params = [
            {
                "type": "category",
                "target": ["variable", ["template-tag", "vendedor"]],
                "value": vendedor,
            }
        ]
    return fetch_card_from_env("ASINFO_CARD_VENDEDOR_USD", params=params)


def ventas_vendedor_kg(vendedor: str | None = None) -> list[dict]:
    """Comparativo interanual de ventas en Kg por vendedor."""
    params = None
    if vendedor:
        params = [
            {
                "type": "category",
                "target": ["variable", ["template-tag", "vendedor"]],
                "value": vendedor,
            }
        ]
    return fetch_card_from_env("ASINFO_CARD_VENDEDOR_KG", params=params)


def ventas_cliente_kg(vendedor: str | None = None) -> list[dict]:
    """Comparativo interanual de ventas en Kg por cliente (con filtro
    opcional por vendedor — la card 164 tiene ese template-tag)."""
    params = None
    if vendedor:
        params = [
            {
                "type": "category",
                "target": ["variable", ["template-tag", "vendedor"]],
                "value": vendedor,
            }
        ]
    return fetch_card_from_env("ASINFO_CARD_CLIENTE_KG", params=params)


# ---------------------------------------------------------------------------
# Cache TTL para facturas_periodo
# ---------------------------------------------------------------------------
# Una vista de /facturas pide hasta 5 años de facturas a Metabase y eso
# tarda 10-30s. Cacheamos por rango de fechas durante 5 min — la cartera
# no cambia segundo a segundo y la diferencia entre "ahora" y "hace 5 min"
# es irrelevante para conciliación. Reiniciar el proceso lo invalida.

_FACTURAS_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_FACTURAS_TTL_SECS = 300  # 5 minutos

# ---------------------------------------------------------------------------
# SUCURSALES — cómo Asinfo distingue AJO de AJ2 (TMT 2026-07-30)
# ---------------------------------------------------------------------------
# Dueña: "como hoy cargan manualmente en dbase, en asinfo es automatico, como
# sabemos si es AJO o AJ2... tenes que encontrar en asinfo como diferenciarlas".
#
# EL DATO EXISTE Y ES EL RUC. Cada sucursal es una `empresa` propia en Asinfo
# cuyo `identificacion` es el RUC de la matriz MÁS un dígito, con su propio
# `nombre_comercial` y el mismo `nombre_fiscal`:
#
#     22656  1793217341001   AJO   ALMACENES JOSE PUEBLA   ← matriz
#     22657  1793217341001|1 AJ2   ALMACENES JOSE PUEBLA   ← sucursal
#
# Son 15 en Asinfo y son exactamente los códigos que se tipean a mano: AJ2,
# CL2, CL3, ST1, ST2, PA2, PA3, ED2, PG1, CP2, MM1, AA2, AU1, AI7, JEC.
#
# Y la factura de la sucursal se emite a la MATRIZ (`factura_cliente.id_empresa`
# = AJO) con la DIRECCIÓN DE ENTREGA apuntando a la empresa-sucursal
# (`direccion_empresa.id_empresa` = 22657 = AJ2). Eso es lo que la persona lee.
# Medido el 30/07 sobre 2026-04-01..2026-07-30: 488 facturas / US$431.030
# (AJO→AJ2 207, CLR→CL3 160, CLR→CL2 116, VGA→JEC 5).
#
# La card 199 exporta `cliente_codigo` = nombre_comercial del FACTURADO, así que
# AJ2 nunca llegaba a PC. Esto lo resuelve con una segunda query (no hace falta
# editar la card, que es de Asinfo y la usan otros).
#
# EL GUARD DEL RUC no es decorativo: hay facturas donde el vendedor eligió la
# dirección de OTRO cliente (DIS→dirección de AJ2, AJO→RRV, CJE→FEB, AJ2→CP2).
# Sin exigir que el RUC de la dirección empiece con el del facturado, esas irían
# al cliente equivocado. En la ventana medida son 0 casos; históricamente 5.
_SUCURSAL_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_SUCURSAL_TTL_SECS = 300
_FECHA_ISO_RE = r"^\d{4}-\d{2}-\d{2}$"

_SQL_SUCURSAL_POR_NUMERO = """
SELECT fc.numero AS numero, e2.nombre_comercial AS sucursal
  FROM factura_cliente fc
  JOIN empresa e1            ON e1.id_empresa = fc.id_empresa
  JOIN direccion_empresa de  ON de.id_direccion_empresa = fc.id_direccion_empresa
  JOIN empresa e2            ON e2.id_empresa = de.id_empresa
 WHERE fc.fecha >= '{desde}' AND fc.fecha <= '{hasta}'
   AND fc.estado <> 0
   AND de.id_empresa <> fc.id_empresa
   AND e2.identificacion LIKE e1.identificacion + '%'
   AND LEN(e2.identificacion) > LEN(e1.identificacion)
   AND e2.nombre_comercial IS NOT NULL
"""


#: Un documento ANULADO en Asinfo no desaparece de `factura_cliente`: se queda
#: con `estado = 0` (y casi siempre una `descripcion_anulacion`). Ese es el
#: ÚNICO estado que significa "lo anularon". Cualquier otra cosa —vivo, un
#: estado que no conocemos, la fila que no aparece, la consulta que falla— NO
#: es una anulación y no habilita a tocar nada. TMT 2026-08-24.
ESTADO_ANULADO = 0

_NUMERO_DOC_RE = r"^[A-Z0-9][A-Z0-9\-]{2,29}$"


def estado_de_documentos(numeros) -> dict[str, dict] | None:
    """Le pregunta a Asinfo, POR NÚMERO, en qué estado está cada documento.

    TMT 2026-08-24. El vigía de anuladas concluía "la anularon" por AUSENCIA:
    si el documento no venía en la card del período, se daba de baja en PC.
    Pero ausencia no es anulación — el 21/08 la card contestó incompleta y se
    llevó puestas dos facturas VIVAS (182254 KJG y 182327 VGA, las dos con
    `fc.estado = 4` y sin motivo de anulación). Esta función es la pregunta
    directa que faltaba, y sirve igual para las facturas SRI
    ('001-099-000182254') que para las notas de entrega ('NTEN-10879'): las
    dos son filas de `factura_cliente` y las dos guardan su número completo
    en `fc.numero`.

    Devuelve ``{numero: {"estado": int, "motivo": str, "id_documento": int}}``
    con las que ENCONTRÓ — un número que no está en el dict es un número que
    Asinfo no reconoce.

    Devuelve **None** si no se pudo preguntar (Metabase caído, sin token, sin
    números válidos). None significa "no sé", que es distinto de "no está", y
    el llamador tiene que tratarlo como "no toques nada".
    """
    import re as _re

    limpios = sorted({
        str(n).strip().upper() for n in (numeros or []) if str(n or "").strip()
    })
    limpios = [n for n in limpios if _re.fullmatch(_NUMERO_DOC_RE, n)]
    if not limpios:
        return None
    # La SQL se interpola (no hay binds acá), así que el filtro de arriba es
    # la única puerta: sólo letras, números y guiones, nada de comillas.
    in_list = ", ".join("'" + n + "'" for n in limpios)
    sql = f"""
SELECT fc.numero AS numero, fc.estado AS estado, fc.id_documento AS id_documento,
       fc.descripcion_anulacion AS motivo
  FROM factura_cliente fc
 WHERE fc.numero IN ({in_list})
"""
    try:
        rows, ok = metabase_client.fetch_dataset_estado(
            2, sql, max_results=max(100, len(limpios) * 2))
    except Exception as e:  # noqa: BLE001 — fail-soft, igual que las cards
        _LOG.warning("estado_de_documentos falló: %s", e)
        return None
    if not ok:
        return None
    out: dict[str, dict] = {}
    for r in rows or []:
        num = str(r.get("numero") or "").strip()
        if not num:
            continue
        try:
            estado = int(r.get("estado"))
        except (TypeError, ValueError):
            continue
        out[num] = {
            "estado": estado,
            "motivo": str(r.get("motivo") or "").strip(),
            "id_documento": r.get("id_documento"),
        }
    return out


def sucursal_por_numero(desde, hasta) -> dict[str, str]:
    """`{numero SRI: código de la SUCURSAL}` para las facturas del rango.

    Sólo devuelve las facturas donde la dirección de entrega pertenece a una
    empresa-sucursal del MISMO RUC (ver la nota de arriba). El resto no está en
    el dict → se queda con el código del facturado.

    Cache 5 min. Fail-soft: si Metabase no contesta devuelve `{}` y todo se
    comporta igual que antes (el código del facturado).
    """
    import re as _re
    if hasattr(desde, "isoformat"):
        desde = desde.isoformat()
    if hasattr(hasta, "isoformat"):
        hasta = hasta.isoformat()
    # Un ISO datetime ('2026-07-01T00:00:00') se recorta a la fecha; cualquier
    # otra cosa se RECHAZA, no se trunca — la SQL se interpola (fetch_dataset no
    # toma binds acá), así que nada que no sea una fecha ISO exacta entra.
    desde = str(desde).strip().split("T")[0]
    hasta = str(hasta).strip().split("T")[0]
    if not (_re.fullmatch(_FECHA_ISO_RE, desde) and _re.fullmatch(_FECHA_ISO_RE, hasta)):
        _LOG.warning("sucursal_por_numero: fechas inválidas (%s, %s)", desde, hasta)
        return {}

    key = (desde, hasta)
    now = _time.time()
    cached = _SUCURSAL_CACHE.get(key)
    if cached and (now - cached[0]) < _SUCURSAL_TTL_SECS:
        return cached[1]

    sql = _SQL_SUCURSAL_POR_NUMERO.format(desde=desde, hasta=hasta)
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=20000) or []
    except Exception as e:  # noqa: BLE001 — fail-soft, igual que las cards
        _LOG.warning("sucursal_por_numero falló: %s", e)
        return {}
    out = {}
    for r in rows:
        num = str(r.get("numero") or "").strip()
        suc = str(r.get("sucursal") or "").strip().upper()
        if num and suc:
            out[num] = suc
    # Igual que facturas_periodo: NO cachear el vacío (no distingue "sin filas"
    # de "Metabase se cayó"), así el próximo request reintenta.
    if out:
        _SUCURSAL_CACHE[key] = (now, out)
    return out


def reset_sucursal_cache() -> None:
    """Vaciar el cache de sucursales (tests / después de un deploy)."""
    _SUCURSAL_CACHE.clear()


# ── ¿Se parte o no se parte? Lo decide el USO, no la estructura ──
# TMT 2026-07-30 (dueña: *"como haga dbase, pero dejemos que a futuro sea
# automatico"*). El control contra FACTURAS.DBF del 29/07 mostró que Asinfo tiene
# 15 sucursales pero la persona **sólo usa algunas**:
#
#     AJ2  149 facturas en el dBase  → SE PARTE
#     CL2    0                        → todo va a CLR
#     CL3    0                        → todo va a CLR
#     JEC    0                        → todo va a VGA
#     CJE    0                        → todo va a STB
#
# (CL2/CL3/JEC/CJE existen en CLIENTES.DBF y nunca se usaron.) Así que la regla
# del RUC sola reproduce la ESTRUCTURA de Asinfo, no la PRÁCTICA. Se filtra por
# uso: la sucursal manda **sólo si ese código ya tiene facturas propias en PC**.
#
# Y por eso es automático a futuro: el día que alguien empiece a usar CL3, la
# regla lo parte sola, sin tocar código ni ninguna tabla de mapeos. Al revés
# también: una sucursal nueva de Asinfo que nadie usa se sigue cobrando a la
# matriz, que es lo que se hace hoy.
#
# El umbral son 3 facturas, no 1: una sola factura mal codeada no debería
# cambiar el criterio de cobranza de un cliente entero.
_MIN_FACTURAS_SUCURSAL = 3
_SUC_USO_CACHE: dict = {}
_SUC_USO_TTL_SECS = 300


def sucursales_en_uso() -> set:
    """Códigos que PC YA usa como cliente propio (≥3 facturas vivas).

    Fail-soft y CONSERVADOR: si la consulta falla devuelve `set()`, con lo cual
    no se parte nada y todo queda como hoy (a la matriz).
    """
    now = _time.time()
    cached = _SUC_USO_CACHE.get("v")
    if cached and (now - cached[0]) < _SUC_USO_TTL_SECS:
        return cached[1]
    try:
        import db as _db
        rows = _db.fetch_all(
            """
            SELECT UPPER(TRIM(codigo_cli)) AS cod, COUNT(*) AS n
              FROM scintela.factura
             WHERE TRIM(COALESCE(stat, '')) NOT IN ('X', 'Y')
               AND COALESCE(codigo_cli, '') <> ''
             GROUP BY UPPER(TRIM(codigo_cli))
            HAVING COUNT(*) >= %s
            """,
            (_MIN_FACTURAS_SUCURSAL,),
        ) or []
    except Exception as e:  # noqa: BLE001
        _LOG.warning("sucursales_en_uso falló (%s) — no se parte ninguna", e)
        return set()
    out = {str(r["cod"]).strip().upper() for r in rows if r.get("cod")}
    if out:
        _SUC_USO_CACHE["v"] = (now, out)
    return out


def reset_sucursales_en_uso_cache() -> None:
    """Vaciar el cache de "sucursales en uso" (tests / tras cargar facturas)."""
    _SUC_USO_CACHE.clear()


def _aplicar_sucursales(rows: list[dict], desde, hasta) -> list[dict]:
    """Reemplaza `cliente_codigo` por el de la sucursal donde corresponda.

    Sólo para las sucursales QUE SE USAN (ver `sucursales_en_uso`): así PC hace
    lo que hace el dBase — parte AJO/AJ2 y junta CLR/CL2/CL3 — y se adapta solo
    cuando la práctica cambia.

    Deja el original en `cliente_codigo_facturado` y marca `es_sucursal=True`,
    para que las pantallas puedan mostrar "AJ2 (facturado a AJO)" sin volver a
    consultar. Idempotente.
    """
    if not rows:
        return rows
    suc = sucursal_por_numero(desde, hasta)
    if not suc:
        return rows
    en_uso = sucursales_en_uso()
    if not en_uso:
        return rows
    for r in rows:
        s = suc.get(str(r.get("numero") or "").strip())
        if not s or s not in en_uso:
            continue
        actual = str(r.get("cliente_codigo") or "").strip().upper()
        if actual == s:
            continue
        r["cliente_codigo_facturado"] = actual
        r["cliente_codigo"] = s
        r["es_sucursal"] = True
    return rows


def facturas_periodo(desde, hasta, max_edad_secs: float | None = None) -> list[dict]:
    """Facturas + NC financieras + Devoluciones + NTEN en el rango [desde, hasta].

    Cada fila es un documento individual con:
        tipo            — 'FACTURA' | 'DEVOLUCION' | 'NC_FINANCIERA' | 'NTEN' | 'NCNT'
        fecha           — date
        numero          — TEXT ('001-099-000175661' o 'NTEN-10444')
        cliente_codigo  — TEXT (código user-facing de empresa)
        vendedor        — TEXT (nombre del agente comercial)
        kg              — DECIMAL. NC_FINANCIERA siempre 0. DEVOLUCION/NCNT negados.
        usd             — DECIMAL. NC_FINANCIERA, DEVOLUCION y NCNT negados.

    Lee de la card definida por la env var `ASINFO_CARD_FACTURAS` (ID 199 al 2026-05-21).

    Performance: cache TTL 5 min por (desde, hasta). La primera carga del día
    sobre un rango grande puede tardar 10-30s; las siguientes son instantáneas.
    Para invalidar el cache, llamar `reset_facturas_cache()` o restart del proceso.

    Args:
        desde, hasta: `date` o string 'YYYY-MM-DD'. Ambos inclusivos.

    Returns:
        Lista de dicts con las columnas arriba. [] si Metabase está caído
        o si la env var no está seteada (fail-soft).
    """
    if hasattr(desde, "isoformat"):
        desde = desde.isoformat()
    if hasattr(hasta, "isoformat"):
        hasta = hasta.isoformat()
    desde, hasta = str(desde), str(hasta)

    key = (desde, hasta)
    now = _time.time()
    # TMT 2026-08-24: el que carga las facturas del día puede pedir data
    # FRESCA. Con el TTL de 5 min a secas, un ciclo de carga cada 2 min se
    # comía dos de cada tres corridas contra el cache: la factura seguía en
    # ámbar aunque Asinfo ya la tuviera, y la demora nunca bajaba de 5 min.
    ttl = _FACTURAS_TTL_SECS if max_edad_secs is None else float(max_edad_secs)
    cached = _FACTURAS_CACHE.get(key)
    if cached and (now - cached[0]) < ttl:
        return cached[1]

    params = [
        {
            "type": "date/single",
            "target": ["variable", ["template-tag", "fecha_inicio"]],
            "value": desde,
        },
        {
            "type": "date/single",
            "target": ["variable", ["template-tag", "fecha_fin"]],
            "value": hasta,
        },
    ]
    rows = fetch_card_from_env("ASINFO_CARD_FACTURAS", params=params)
    # TMT 2026-06-11 — filtro defensivo "facturas fantasma" (estado=0):
    # en Asinfo, fc.estado=0 = emision NO autorizada por el SRI que se
    # re-emitio despues con otro numero (TJC 177714/177712/177711 →
    # 176534/176658/176659; AFC 177710 y CTE 177709 nunca autorizadas).
    # El dBase tipeo la version corregida; importar las dos duplica kg.
    # La card 199 ya excluye estado 0 en su WHERE (fix 2026-06-11 via
    # /admin/debug-asinfo-facturas/card-estado) — esto es la red de
    # seguridad por si la card se re-edita. Si la columna `estado` no
    # viene en la card, es no-op.
    rows = [
        r for r in rows
        if str(r.get("estado", "")).strip() not in ("0", "0.0")
    ]
    # SUCURSALES (TMT 2026-07-30): la card devuelve el código del FACTURADO
    # (AJO); si la dirección de entrega es de una empresa-sucursal del mismo RUC
    # (AJ2), el código correcto es el de la sucursal. Se aplica acá, ANTES del
    # cache, para que TODA la app vea el mismo código y nadie tenga que
    # acordarse de enriquecer. Fail-soft: sin Metabase queda el del facturado.
    rows = _aplicar_sucursales(rows, desde, hasta)
    # Solo cacheamos si trajo algo — si fue [] por error de red, no fijamos
    # el resultado vacío 5 min (mejor reintentar al próximo request).
    if rows:
        _FACTURAS_CACHE[key] = (now, rows)
    return rows


def reset_facturas_cache() -> None:
    """Vaciar el cache de facturas_periodo. Útil para tests o tras un deploy
    que invalidó la fuente."""
    _FACTURAS_CACHE.clear()


def reset_flujo_caches() -> None:
    """Vaciar los caches del flujo de producción: fabricacion_flujo_mes,
    despacho_fisico_mes y movimiento_bodega_mes. Útil para tests o tras un
    deploy que invalidó la fuente. Ver /informes/flujo-produccion."""
    _FABRICACION_FLUJO_CACHE.clear()
    _DESPACHO_CACHE.clear()
    _DESPACHO_HOY_CACHE.clear()
    _MOVIMIENTO_BODEGA_CACHE.clear()


# ID de la card Metabase con la retención (IVA+Fuente) por factura. Se puede
# rotar por env ASINFO_CARD_RETENCIONES; default = 202 (creada 2026-07-09).
_ASINFO_CARD_RETENCIONES_DEFAULT = "202"


_RETENCIONES_TTL_SECS = 300  # 5 min — dueña 2026-07-18: era la única card sin caché
_RETENCIONES_CACHE: dict = {}


def reset_retenciones_cache() -> None:
    """Vaciar el caché de retenciones (tests / tras aplicar cambios)."""
    _RETENCIONES_CACHE.clear()


def retenciones_periodo(desde, hasta) -> dict:
    """Retención (fuente + IVA) por factura de Asinfo en el rango [desde, hasta].

    La retención que el cliente emite contra nuestra factura NO es una columna
    de `factura_cliente`; vive como líneas de forma_cobro de retención. La card
    (default 202) hace el join detalle_cobro→forma_cobro→forma_pago→factura y
    agrupa por `numero` (SRI '001-099-NNNNNNNNN'). Ver skill
    programa-core-integraciones / memoria reference_asinfo_retenciones_join.

    Returns:
        dict `{numero: {"ret_fuente","ret_iva","ret_total"}}` (todos float).
        {} si Metabase no está o la card no trae nada (fail-soft).
    """
    if hasattr(desde, "isoformat"):
        desde = desde.isoformat()
    if hasattr(hasta, "isoformat"):
        hasta = hasta.isoformat()
    import time as _time
    _ck = (str(desde), str(hasta))
    _now = _time.time()
    _cached = _RETENCIONES_CACHE.get(_ck)
    if _cached and (_now - _cached[0]) < _RETENCIONES_TTL_SECS:
        return _cached[1]
    params = [
        {
            "type": "date/single",
            "target": ["variable", ["template-tag", "fecha_inicio"]],
            "value": str(desde),
        },
        {
            "type": "date/single",
            "target": ["variable", ["template-tag", "fecha_fin"]],
            "value": str(hasta),
        },
    ]
    card_id = os.environ.get(
        "ASINFO_CARD_RETENCIONES", "").strip() or _ASINFO_CARD_RETENCIONES_DEFAULT
    rows = metabase_client.fetch_card(card_id, params=params) or []
    out: dict = {}
    for r in rows:
        numero = str(r.get("numero") or "").strip()
        if not numero:
            continue
        rf = float(r.get("ret_fuente") or 0)
        ri = float(r.get("ret_iva") or 0)
        rt = float(r.get("ret_total") if r.get("ret_total") is not None else rf + ri)
        out[numero] = {"ret_fuente": rf, "ret_iva": ri, "ret_total": rt}
    # Solo cachear si trajo algo (un fallo transitorio de Metabase no debe
    # dejar 5 min de "{}" pegado — mismo criterio fail-soft del resto).
    if out:
        _RETENCIONES_CACHE[_ck] = (_now, out)
    return out


def facturas_totales_por_tipo(desde, hasta) -> dict:
    """Agregados por tipo de documento en el período [desde, hasta].

    Wrapper sobre facturas_periodo() que suma kg y usd por tipo. Útil para
    el panel de "ventas del mes" y para conciliar contra Programa Core.

    Returns:
        dict tipo→{docs, kg, usd}. Vacío si no hay data o si el bridge cae.
    """
    rows = facturas_periodo(desde, hasta)
    out: dict = {}
    for r in rows:
        t = r.get("tipo") or "?"
        slot = out.setdefault(t, {"docs": 0, "kg": 0.0, "usd": 0.0})
        slot["docs"] += 1
        slot["kg"] += float(r.get("kg") or 0)
        slot["usd"] += float(r.get("usd") or 0)
    # Redondear
    for slot in out.values():
        slot["kg"] = round(slot["kg"], 3)
        slot["usd"] = round(slot["usd"], 2)
    return out


def ventas_facturado_kg(yy: int, mm: int) -> float:
    """Kg NETOS facturados (vendidos) en Asinfo dentro del mes (yy, mm).

    Directo de Asinfo (facturas_periodo vía Metabase), NO de scintela.factura.
    Suma los kg de todas las facturas del mes; las devoluciones y notas de
    crédito (DEVOLUCION/NCNT) ya vienen negadas en `kg`, así que la suma es la
    venta neta. Fail-soft: 0.0 si Asinfo no responde.
    """
    import calendar as _cal
    from datetime import date as _date
    try:
        yy = int(yy)
        mm = int(mm)
    except (TypeError, ValueError):
        return 0.0
    d1 = _date(yy, mm, 1)
    d2 = _date(yy, mm, _cal.monthrange(yy, mm)[1])
    try:
        tot = facturas_totales_por_tipo(d1, d2) or {}
        return round(sum(float(s.get("kg") or 0) for s in tot.values()), 3)
    except Exception:  # noqa: BLE001 -- fail-soft
        return 0.0


# ---------------------------------------------------------------------------
# Disponibilidad
# ---------------------------------------------------------------------------


def disponible() -> bool:
    """True si Metabase está configurado y al menos una card_id está seteada.

    No prueba conectividad (eso es trabajo del healthcheck). Solo dice si
    el bridge está armado a nivel configuración.
    """
    if not metabase_client.disponible():
        return False
    return any(
        os.environ.get(v, "").strip()
        for v in (
            "ASINFO_CARD_VENDEDOR_USD",
            "ASINFO_CARD_VENDEDOR_KG",
            "ASINFO_CARD_CLIENTE_KG",
            "ASINFO_CARD_FACTURAS",
        )
    )


# ---------------------------------------------------------------------------
# Stock — cantidad por producto (sin costo: Asinfo no tiene costos cargados)
# ---------------------------------------------------------------------------
# Confirmado 2026-05-22: ninguno de los 20.064 productos activos tiene
# `costo_estandar` ni `precio_referencial_compra` > 0. Solo `precio_ultima_venta`
# tiene data para ~24% de los productos. Por eso `stock_asinfo()` devuelve
# CANTIDAD de stock (saldo_producto.saldo) sin costo. Si en algún momento
# se cargan costos en el ERP, esta función puede ampliarse para incluirlos.

_STOCK_TTL_SECS = 300  # 5 min (antes 10 — dueña 2026-07-18)
_STOCK_CACHE: dict = {}


def stock_asinfo(min_saldo: float = 0.0, id_bodega: int | None = None) -> list[dict]:
    """Stock por producto desde Asinfo, usando la vista pre-calculada
    `v_saldo_producto_vista` que ya:
      - consolida el saldo más reciente por producto+bodega,
      - expone tejido (categoría), subcategoría, color (hex),
      - incluye nombre_producto y nombre_comercial.

    Cada fila:
        codigo            — código SKU del producto
        nombre            — nombre_producto (p.ej. "Jersey 3.5 BLA")
        nombre_comercial  — alternativo (puede estar vacío)
        tejido            — nombre_categoria_producto (Jersey / Fleece / Pique / Rib / etc.)
        subcategoria      — variante (3.5 / 1.2x2.3 / 24/1 / etc.)
        color             — hex Asinfo (#ffffff). El "nombre" del color va embebido
                            en codigo (BLA, NEG, MAR, ...) y nombre.
        cantidad_total    — SUM(saldo_acumulado) sobre todas las bodegas del producto
        n_bodegas         — cuántas bodegas distintas tienen stock
        precio_ultima     — precio_ultima_venta del producto (puede ser 0)

    Args:
        min_saldo: filtra a productos con cantidad_total > min_saldo. Default 0.

    Returns:
        Lista de dicts ordenada por cantidad_total DESC. [] si Metabase no
        está configurado o si falla.

    Nota perf 2026-05-22: la vista trae ~3500 productos con stock. Subimos
    el max-results de Metabase a 10000 para no truncar (default 2000).
    """
    import time as _time
    try:
        id_bodega = int(id_bodega) if id_bodega is not None else None
    except (TypeError, ValueError):
        id_bodega = None
    cache_key = f"min_{min_saldo}_b{id_bodega}"
    now = _time.time()
    cached = _STOCK_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _STOCK_TTL_SECS:
        return cached[1]

    # Filtro opcional por bodega (para las tabs Hilo / Tela Cruda / Tela
    # Terminada de la vista "Por producto"). Sin bodega = consolida todas.
    filtro_bodega = f"WHERE id_bodega = {id_bodega}" if id_bodega is not None else ""

    # CRITICAL 2026-05-22: la vista `v_saldo_producto_vista` agrega LOTES
    # adicionales que el reporte oficial del ERP filtra (no respeta
    # "indicador lote"). Usando esa vista, los kg vienen ~6% más altos
    # que el reporte oficial. La fuente confiable es la tabla raw
    # `saldo_producto`, tomando el último snapshot por (producto, bodega)
    # con ROW_NUMBER. Verificado: Bodega Hilo = 1.767.920,41 kg coincide
    # al centavo con el export Excel del ERP.
    sql = f"""
        WITH ult AS (
            SELECT id_producto, id_bodega, saldo,
                   ROW_NUMBER() OVER (
                       PARTITION BY id_producto, id_bodega
                       ORDER BY fecha DESC, id_saldo_producto DESC
                   ) AS rn
              FROM saldo_producto
              {filtro_bodega}
        )
        SELECT p.codigo                                                AS codigo,
               COALESCE(NULLIF(p.descripcion, ''), p.nombre, p.codigo) AS nombre,
               ''                                                      AS nombre_comercial,
               COALESCE(cp.nombre, '')                                 AS tejido,
               ''                                                      AS subcategoria,
               ''                                                      AS color,
               SUM(u.saldo)                                            AS cantidad_total,
               COUNT(DISTINCT u.id_bodega)                             AS n_bodegas,
               COALESCE(MAX(p.precio_ultima_venta), 0)                 AS precio_ultima
          FROM ult u
          INNER JOIN producto p ON p.id_producto = u.id_producto
          LEFT JOIN categoria_producto cp ON cp.id_categoria_producto = p.id_categoria_producto
         WHERE u.rn = 1 AND u.saldo > 0
         GROUP BY p.codigo, p.descripcion, p.nombre, cp.nombre
        HAVING SUM(u.saldo) > 0
         ORDER BY SUM(u.saldo) DESC
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=10000)
    if not rows:
        return []
    out = []
    for r in rows:
        try:
            qty = float(r.get("cantidad_total") or 0)
            if qty < min_saldo:
                continue
            nombre = str(r.get("nombre") or "").strip()
            codigo = str(r.get("codigo") or "").strip()
            # Extraer "código de color" desde el nombre o código.
            # El convenio en Asinfo es que el color va al final, separado por
            # espacio o guion (ej. "Jersey 3.5 BLA", "20/1-65:35-PEI-KW").
            # Tomamos el último token alfa-mayúsculas del nombre; fallback al
            # último token del código si el nombre no tiene uno claro.
            color_cod = ""
            for token in reversed(nombre.split()):
                t = token.strip().upper()
                if t.isalpha() and 2 <= len(t) <= 5:
                    color_cod = t
                    break
            if not color_cod:
                # Fallback: último token del código separado por '-'
                last = codigo.split("-")[-1].strip().upper()
                if last.isalpha() and 2 <= len(last) <= 5:
                    color_cod = last
            out.append({
                "codigo": codigo,
                "nombre": nombre,
                "nombre_comercial": str(r.get("nombre_comercial") or "").strip(),
                "tejido": str(r.get("tejido") or "").strip(),
                "subcategoria": str(r.get("subcategoria") or "").strip(),
                "color_hex": str(r.get("color") or "").strip(),  # hex Asinfo (a menudo no es real)
                "color": color_cod,                              # el "código de color" útil (BLA/NEG/etc.)
                "cantidad_total": qty,
                "n_bodegas": int(r.get("n_bodegas") or 0),
                "precio_ultima": float(r.get("precio_ultima") or 0),
                # Compat con código viejo que esperaba estos nombres:
                "descripcion": nombre,
                "bodegas_detalle": "",
            })
        except (TypeError, ValueError):
            continue
    _STOCK_CACHE[cache_key] = (now, out)
    return out


# ---------------------------------------------------------------------------
# Stock por LOTE (Asinfo) — réplica del reporte "Stock Valorado por Lote"
# ---------------------------------------------------------------------------
# A diferencia de stock_asinfo() (que consolida por producto sobre todas las
# bodegas), esta función baja al nivel de LOTE individual con sus atributos,
# que es lo que pide el reporte oficial del ERP. Fuente: `saldo_producto_lote`
# (snapshot diario por producto+bodega+lote) tomando el último snapshot por
# (producto, bodega, lote) con ROW_NUMBER. Verificado 2026-06-09 contra el
# reporte oficial:
#     Bodega Hilo        = 1.790.694,44 kg
#     Bodega Tela Cruda  =   255.795,25 kg   (reporte: 255.660 → ±0,05%)
#     Bodega Prod.Term.  =   347.389,93 kg
#
# Atributos del lote (tabla `lote`, slots EAV id_atributo_N/id_valor_atributo_N
# resueltos contra `valor_atributo`). Mapa de `atributo` (id → nombre):
#     1 = Acabado | 2 = Calidad (PRI/SEG) | 3 = Color | 51 = Estampado
#     101 = Titulo Hilo | 103 = Proveedor | 151 = Fallas PT | 152 = Fallas TC
# Bodegas: 1=Colorantes, 51=Hilo, 52=Tela Cruda, 53=Prod.Terminado,
#          151=Reproceso, 201=Cuarentena.
#
# Dólares: NO se traen de Asinfo (no confiables). Solo cantidad (kg).

_STOCK_LOTE_TTL_SECS = 300  # 5 min (antes 10 — dueña 2026-07-18)
_STOCK_LOTE_CACHE: dict = {}
_STOCK_LOTE_TOTALES_CACHE: dict = {}


def stock_asinfo_lote(
    id_bodega: int,
    q: str = "",
    tejido: str = "",
    titulo: str = "",
    proveedor: str = "",
    calidad: str = "",
    color: str = "",
    limit: int = 1500,
) -> list[dict]:
    """Stock por LOTE de una bodega, con atributos resueltos.

    TODOS los filtros se empujan al SQL. Cada fila incluye `_total_lotes` y
    `_total_kg` = COUNT/SUM OVER() del set filtrado COMPLETO (no del recorte),
    para que los KPIs sean correctos aunque la tabla muestre solo `limit` filas
    (Bodega Hilo sola tiene ~61k lotes; no se renderizan todas).

    Returns:
        Lista de dicts (≤ limit) ordenada por saldo DESC. [] si falla.
    """
    try:
        id_bodega = int(id_bodega)
    except (TypeError, ValueError):
        return []

    import time as _time

    def _esc(s: str) -> str:
        return (s or "").strip().replace("'", "''")

    q_norm = (q or "").strip().upper()
    cache_key = (
        f"b{id_bodega}_q{q_norm}_te{tejido}_ti{titulo}"
        f"_pr{proveedor}_ca{calidad}_co{color}_l{limit}"
    )
    now = _time.time()
    cached = _STOCK_LOTE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _STOCK_LOTE_TTL_SECS:
        return cached[1]

    # Filtros empujados al SQL (id_bodega ya es int sanitizado).
    filtro_q = ""
    if q_norm:
        s = _esc(q_norm)
        filtro_q += f" AND (UPPER(p.codigo) LIKE '%{s}%' OR UPPER(p.nombre) LIKE '%{s}%')"
    if tejido:
        filtro_q += f" AND cp.nombre = '{_esc(tejido)}'"
    if titulo:
        filtro_q += f" AND a.titulo_hilo = '{_esc(titulo)}'"
    if proveedor:
        filtro_q += f" AND a.proveedor = '{_esc(proveedor)}'"
    if calidad:
        filtro_q += f" AND UPPER(a.calidad) = '{_esc(calidad).upper()}'"
    if color:
        filtro_q += f" AND a.color = '{_esc(color)}'"

    sql = f"""
        WITH ult AS (
            SELECT id_producto, id_bodega, id_lote, saldo,
                   ROW_NUMBER() OVER (
                       PARTITION BY id_producto, id_bodega, id_lote
                       ORDER BY fecha DESC, id_saldo_producto_lote DESC
                   ) AS rn
              FROM saldo_producto_lote
             WHERE id_bodega = {id_bodega}
        ),
        attr AS (
            SELECT u.id_lote,
                MAX(CASE WHEN va.id_atributo = 2   THEN va.nombre END) AS calidad,
                MAX(CASE WHEN va.id_atributo = 3   THEN va.nombre END) AS color,
                MAX(CASE WHEN va.id_atributo = 1   THEN va.nombre END) AS acabado,
                MAX(CASE WHEN va.id_atributo = 51  THEN va.nombre END) AS estampado,
                MAX(CASE WHEN va.id_atributo = 101 THEN va.nombre END) AS titulo_hilo,
                MAX(CASE WHEN va.id_atributo = 103 THEN va.nombre END) AS proveedor
              FROM lote l
              UNPIVOT (idv FOR slot IN (
                  id_valor_atributo_1, id_valor_atributo_2, id_valor_atributo_3,
                  id_valor_atributo_4, id_valor_atributo_5, id_valor_atributo_6,
                  id_valor_atributo_7, id_valor_atributo_8, id_valor_atributo_9,
                  id_valor_atributo_10)) u
              JOIN valor_atributo va ON va.id_valor_atributo = u.idv
             GROUP BY u.id_lote
        )
        SELECT TOP {int(limit)}
               p.codigo                                                AS codigo,
               COALESCE(NULLIF(p.descripcion, ''), p.nombre, p.codigo) AS producto,
               l.codigo                                                AS lote,
               COALESCE(cp.nombre, '')                                 AS tejido,
               a.calidad, a.color, a.acabado, a.estampado,
               a.titulo_hilo, a.proveedor,
               COALESCE(NULLIF(u.codigo, ''), u.nombre, 'KG')          AS unidad,
               ult.saldo                                               AS saldo,
               COUNT(*)        OVER ()                                  AS _total_lotes,
               SUM(ult.saldo)  OVER ()                                  AS _total_kg
          FROM ult
          INNER JOIN producto p ON p.id_producto = ult.id_producto
          INNER JOIN lote l ON l.id_lote = ult.id_lote
          LEFT JOIN categoria_producto cp ON cp.id_categoria_producto = p.id_categoria_producto
          LEFT JOIN unidad u ON u.id_unidad = p.id_unidad
          LEFT JOIN attr a ON a.id_lote = ult.id_lote
         WHERE ult.rn = 1 AND ult.saldo > 0 {filtro_q}
         ORDER BY ult.saldo DESC
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=int(limit))
    out = []
    for r in rows:
        try:
            saldo = float(r.get("saldo") or 0)
            if saldo <= 0:
                continue
            out.append({
                "codigo": str(r.get("codigo") or "").strip(),
                "producto": str(r.get("producto") or "").strip(),
                "lote": str(r.get("lote") or "").strip(),
                "tejido": str(r.get("tejido") or "").strip(),
                "calidad": str(r.get("calidad") or "").strip(),
                "color": str(r.get("color") or "").strip(),
                "acabado": str(r.get("acabado") or "").strip(),
                "estampado": str(r.get("estampado") or "").strip(),
                "titulo_hilo": str(r.get("titulo_hilo") or "").strip(),
                "proveedor": str(r.get("proveedor") or "").strip(),
                "unidad": str(r.get("unidad") or "KG").strip() or "KG",
                "saldo": saldo,
                "_total_lotes": int(r.get("_total_lotes") or 0),
                "_total_kg": float(r.get("_total_kg") or 0),
            })
        except (TypeError, ValueError):
            continue
    _STOCK_LOTE_CACHE[cache_key] = (now, out)
    return out


def stock_asinfo_lote_totales() -> list[dict]:
    """Totales de stock por bodega a nivel lote (landing del reporte).

    Barato (un GROUP BY) — sirve de resumen y de ancla de reconciliación.
    Cada fila: id_bodega, bodega, lotes, total_kg.
    """
    import time as _time
    now = _time.time()
    cached = _STOCK_LOTE_TOTALES_CACHE.get("all")
    if cached and (now - cached[0]) < _STOCK_LOTE_TTL_SECS:
        return cached[1]

    sql = """
        WITH ult AS (
            SELECT id_producto, id_bodega, id_lote, saldo,
                   ROW_NUMBER() OVER (
                       PARTITION BY id_producto, id_bodega, id_lote
                       ORDER BY fecha DESC, id_saldo_producto_lote DESC
                   ) AS rn
              FROM saldo_producto_lote
        )
        SELECT b.id_bodega AS id_bodega,
               b.nombre    AS bodega,
               COUNT(*)    AS lotes,
               SUM(u.saldo) AS total_kg
          FROM ult u
          JOIN bodega b ON b.id_bodega = u.id_bodega
         WHERE u.rn = 1 AND u.saldo > 0
         GROUP BY b.id_bodega, b.nombre
         ORDER BY SUM(u.saldo) DESC
    """
    # TMT 2026-07-29: `_estado` para distinguir "Metabase no contestó" de "no
    # hay filas". Ésta es LA función que rompió el balance el 29/07 — su []
    # cacheado 5 min dejaba el stock en cero y la utilidad ~495k abajo.
    rows, ok = metabase_client.fetch_dataset_estado(2, sql, max_results=100)
    out = []
    for r in rows:
        try:
            out.append({
                "id_bodega": int(r.get("id_bodega")),
                "bodega": str(r.get("bodega") or "").strip(),
                "lotes": int(r.get("lotes") or 0),
                "total_kg": float(r.get("total_kg") or 0),
            })
        except (TypeError, ValueError):
            continue
    _cache_put(_STOCK_LOTE_TOTALES_CACHE, "all", out, ok, _STOCK_LOTE_TTL_SECS)
    return out


# ---------------------------------------------------------------------------
# Stock EN PROCESO (WIP entre pasos de producción)
# ---------------------------------------------------------------------------
# Material despachado a órdenes de fabricación ABIERTAS pero todavía no
# devuelto como el producto del siguiente paso → "stock entre pasos" que no
# está en ningún saldo de bodega. Definición live (verificada 2026-06-09):
#   issued = SUM(detalle_orden_salida_material.cantidad_despachada) por OFT,
#            vía la junction detalle_orden_salida_material_orden_fabricacion.
#            (OFT-000035309 → 4.950,72, coincide al centavo con el Excel.)
#   producido = orden_fabricacion.cantidad_fabricada (declarado, estado actual).
#   en_proceso = issued − producido.
# Pasos por bodega de salida de la OFT: 52 = Tejeduría (Hilo→Tela Cruda),
# 53 = Tintorería/Confección (Tela Cruda→Producto Terminado).

_EN_PROCESO_TTL_SECS = 300
_EN_PROCESO_CACHE: dict = {}

_PASOS_PROCESO = {52: "Tejeduría (Hilo → Tela Cruda)", 53: "Tintorería (Tela Cruda → PT)"}


def stock_en_proceso() -> dict:
    """WIP entre pasos. Devuelve {pasos: [...], ofts: [...]} (fail-soft).

    pasos: por bodega de salida — issued, producido, en_proceso, n_ofts.
    ofts:  detalle por OFT con en_proceso > 0 (material claramente en proceso).
    """
    import time as _time
    now = _time.time()
    cached = _EN_PROCESO_CACHE.get("all")
    if cached and (now - cached[0]) < _EN_PROCESO_TTL_SECS:
        return cached[1]

    sql = """
        WITH ofs AS (
            SELECT id_orden_fabricacion, numero, id_bodega, id_producto,
                   ISNULL(cantidad, 0) AS planif, ISNULL(cantidad_fabricada, 0) AS fab
              FROM orden_fabricacion
             WHERE id_bodega IN (52, 53) AND estado_produccion <> 5
        ),
        issued AS (
            SELECT j.id_orden_fabricacion,
                   SUM(ISNULL(d.cantidad_despachada, 0)) AS issued
              FROM detalle_orden_salida_material_orden_fabricacion j
              JOIN detalle_orden_salida_material d
                ON d.id_detalle_orden_salida_material = j.id_detalle_orden_salida_material
             WHERE j.id_orden_fabricacion IN (SELECT id_orden_fabricacion FROM ofs)
             GROUP BY j.id_orden_fabricacion
        )
        SELECT o.id_bodega                                      AS id_bodega,
               o.numero                                         AS oft,
               COALESCE(NULLIF(p.descripcion,''), p.nombre, p.codigo) AS producto,
               p.codigo                                         AS prod_codigo,
               o.planif                                         AS planif,
               o.fab                                            AS fab,
               ISNULL(i.issued, 0)                              AS issued,
               ISNULL(i.issued, 0) - o.fab                      AS en_proceso
          FROM ofs o
          LEFT JOIN issued i ON i.id_orden_fabricacion = o.id_orden_fabricacion
          LEFT JOIN producto p ON p.id_producto = o.id_producto
         ORDER BY ISNULL(i.issued, 0) - o.fab DESC
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=20000)
    pasos: dict[int, dict] = {}
    ofts = []
    for r in rows:
        try:
            b = int(r.get("id_bodega"))
            issued = float(r.get("issued") or 0)
            fab = float(r.get("fab") or 0)
            ep = float(r.get("en_proceso") or 0)
            # Sólo cuentan las órdenes con material realmente EN PROCESO
            # (despachado > producido). Las que produjeron más de lo despachado
            # (yield/timing, ep<0) NO restan stock — se ignoran.
            if ep <= 0.01:
                continue
            slot = pasos.setdefault(b, {"id_bodega": b, "paso": _PASOS_PROCESO.get(b, f"Bodega {b}"),
                                        "issued": 0.0, "producido": 0.0, "en_proceso": 0.0, "n_ofts": 0})
            slot["issued"] += issued
            slot["producido"] += fab
            slot["en_proceso"] += ep
            slot["n_ofts"] += 1
            ofts.append({
                "id_bodega": b,
                "paso": _PASOS_PROCESO.get(b, f"Bodega {b}"),
                "oft": str(r.get("oft") or "").strip(),
                "producto": str(r.get("producto") or "").strip(),
                "prod_codigo": str(r.get("prod_codigo") or "").strip(),
                "planif": float(r.get("planif") or 0),
                "fab": fab,
                "issued": issued,
                "en_proceso": ep,
            })
        except (TypeError, ValueError):
            continue
    out = {"pasos": sorted(pasos.values(), key=lambda x: x["id_bodega"]), "ofts": ofts}
    _EN_PROCESO_CACHE["all"] = (now, out)
    return out


# ---------------------------------------------------------------------------
# Fabricación por proceso (réplica Excel "Saldos Inventarios Proceso Produccion")
# ---------------------------------------------------------------------------
# TMT 2026-06-10 dueña: Stock pasa a 2 tabs — Fabricación TC (52) y
# Fabricación PT (53) — con la misma estructura del Excel de la nube:
#   - Inventario en Proceso: Total OSM | Total Ing. Fab. | Saldo
#   - Órdenes de Fabricación: Planificada | Fabricada | Por producir
#     (PT agrupado por tejido/categoría: Fleece, Jersey, Pique, ...)
#   - Detalle por OFT.
# A diferencia de stock_en_proceso(), acá se incluyen TODAS las OFTs abiertas
# (también las de saldo ≤ 0 por yield/timing) para que los totales cierren
# EXACTO contra el Excel: Saldo = ΣOSM − ΣIng.Fab. sobre todo el universo.

_FABRICACION_TTL_SECS = 300
_FABRICACION_CACHE: dict = {}

# Material de ENTRADA por bodega (el que se transforma): el bloque "Inventario
# en Proceso" del Excel cuenta solo este material (TELA CRUDA / HILO), no los
# auxiliares ni colorantes que tambien se despachan a la OFT.
_MATERIAL_PROCESO = {52: "HILO", 53: "TELA CRUDA"}


def fabricacion_proceso(id_bodega: int) -> dict:
    """Proceso de fabricación de una bodega (52 TC / 53 PT). Fail-soft.

    Devuelve:
        resumen:    {issued, fab, saldo, planif, por_producir, n_ofts}
        por_tejido: [{tejido, planif, fab, por_producir, n_ofts}, ...]
        ofts:       [{oft, producto, prod_codigo, tejido, planif, fab,
                      issued, saldo, por_producir}, ...]
    """
    import time as _time
    now = _time.time()
    cached = _FABRICACION_CACHE.get(id_bodega)
    if cached and (now - cached[0]) < _FABRICACION_TTL_SECS:
        return cached[1]

    mat_proc = _MATERIAL_PROCESO.get(int(id_bodega), "TELA CRUDA")
    sql = f"""
        WITH leaf AS (
            -- Replica EXACTA del Power Query del Excel "Saldos Inventarios
            -- Proceso Produccion Nube" (Sql.Database as2): OFTs HOJA (no
            -- padres) con estado_produccion = 2.
            SELECT ofr.id_orden_fabricacion, ofr.numero, ofr.id_producto,
                   ISNULL(ofr.cantidad, 0) AS planif,
                   ISNULL(ofr.cantidad_fabricada, 0) AS fab
              FROM orden_fabricacion ofr
             WHERE ofr.id_bodega = {int(id_bodega)} AND ofr.estado_produccion = 2
               AND ofr.id_orden_fabricacion NOT IN (
                   SELECT id_orden_fabricacion_padre FROM orden_fabricacion
                    WHERE id_orden_fabricacion_padre IS NOT NULL)
        ),
        issued AS (
            -- Despacho del MATERIAL DE ENTRADA ({mat_proc}) por el junction
            -- orden_fabricacion_orden_salida_material (el del Excel — NO el
            -- detalle-junction). Filtrado por categoria para excluir auxiliares
            -- y colorantes, igual que el bloque "Inventario en Proceso" del Excel.
            -- IMPORTANTE: se ARRANCA desde `leaf` (pocas filas) y se baja a las
            -- tablas grandes de OSM por JOIN — con IN (SELECT ... FROM leaf) el
            -- planner escaneaba todo el OSM y la tab TC tardaba 21s (>20s timeout
            -- de fetch_dataset -> devolvia [] y cacheaba 0). Asi: ~1,3s.
            SELECT ofosm.id_orden_fabricacion,
                   SUM(ISNULL(dosm.cantidad_despachada, 0)) AS issued
              FROM leaf l
              JOIN orden_fabricacion_orden_salida_material ofosm
                ON ofosm.id_orden_fabricacion = l.id_orden_fabricacion
              JOIN detalle_orden_salida_material dosm
                ON dosm.id_orden_salida_material = ofosm.id_orden_salida_material
              JOIN producto prm ON prm.id_producto = dosm.id_producto
             WHERE prm.nombre_categoria_producto = '{mat_proc}'
             GROUP BY ofosm.id_orden_fabricacion
        )
        SELECT o.numero                                         AS oft,
               COALESCE(NULLIF(p.descripcion,''), p.nombre, p.codigo) AS producto,
               p.codigo                                         AS prod_codigo,
               COALESCE(cp.nombre, '')                          AS tejido,
               o.planif                                         AS planif,
               o.fab                                            AS fab,
               ISNULL(i.issued, 0)                              AS issued
          FROM leaf o
          LEFT JOIN issued i ON i.id_orden_fabricacion = o.id_orden_fabricacion
          LEFT JOIN producto p ON p.id_producto = o.id_producto
          LEFT JOIN categoria_producto cp
            ON cp.id_categoria_producto = p.id_categoria_producto
         ORDER BY ISNULL(i.issued, 0) - o.fab DESC
    """
    rows, ok = metabase_client.fetch_dataset_estado(2, sql, max_results=20000)
    # TMT 2026-06-10 v2 — paridad con el Excel: la tabla "Órdenes de
    # Fabricación" del Excel sólo cuenta OFTs INICIADAS (con material
    # despachado). Las abiertas sin movimiento se reportan aparte como
    # "sin iniciar" para que Planificada no infle 2-3x. El bloque
    # Inventario en Proceso (OSM/IngFab/Saldo) no cambia: las no
    # iniciadas tienen issued=0 y fab=0, no aportan.
    resumen = {"issued": 0.0, "fab": 0.0, "saldo": 0.0,
               "planif": 0.0, "por_producir": 0.0, "n_ofts": 0,
               "planif_inic": 0.0, "fab_inic": 0.0, "por_producir_inic": 0.0,
               "n_inic": 0, "planif_sin": 0.0, "n_sin": 0,
               # saldo_produciendo = en proceso SOLO de órdenes que ya arrancaron
               # a producir (fab>0). El material despachado a órdenes que todavía
               # NO produjeron (staged al pie de máquina) NO cuenta como "en
               # máquinas" — así lo ve la fábrica (dueña 2026-07-10: su planilla
               # da 36.293 vs 48.166 nuestro; la diferencia son justo esas OFTs
               # con material despachado y fab=0).
               "saldo_produciendo": 0.0}
    por_tejido: dict[str, dict] = {}
    ofts = []
    for r in rows:
        try:
            planif = float(r.get("planif") or 0)
            fab = float(r.get("fab") or 0)
            issued = float(r.get("issued") or 0)
        except (TypeError, ValueError):
            continue
        saldo = issued - fab
        por_producir = planif - fab
        iniciada = issued > 0.005 or fab > 0.005
        tejido = str(r.get("tejido") or "").strip() or "(s/categoría)"
        resumen["issued"] += issued
        resumen["fab"] += fab
        resumen["saldo"] += saldo
        if fab > 0.005:                       # solo órdenes YA produciendo
            resumen["saldo_produciendo"] += saldo
        resumen["planif"] += planif
        resumen["por_producir"] += por_producir
        resumen["n_ofts"] += 1
        if iniciada:
            resumen["planif_inic"] += planif
            resumen["fab_inic"] += fab
            resumen["por_producir_inic"] += por_producir
            resumen["n_inic"] += 1
            # El pivot por tejido replica el del Excel: sólo iniciadas.
            slot = por_tejido.setdefault(tejido, {"tejido": tejido, "planif": 0.0,
                                                  "fab": 0.0, "por_producir": 0.0, "n_ofts": 0})
            slot["planif"] += planif
            slot["fab"] += fab
            slot["por_producir"] += por_producir
            slot["n_ofts"] += 1
        else:
            resumen["planif_sin"] += planif
            resumen["n_sin"] += 1
        ofts.append({
            "oft": str(r.get("oft") or "").strip(),
            "producto": str(r.get("producto") or "").strip(),
            "prod_codigo": str(r.get("prod_codigo") or "").strip(),
            "tejido": tejido if tejido != "(s/categoría)" else "",
            "planif": planif,
            "fab": fab,
            "issued": issued,
            "saldo": saldo,
            "por_producir": por_producir,
            "iniciada": iniciada,
        })
    out = {
        "resumen": resumen,
        "por_tejido": sorted(por_tejido.values(), key=lambda x: -x["planif"]),
        "ofts": ofts,
    }
    _cache_put(_FABRICACION_CACHE, id_bodega, out, ok, _FABRICACION_TTL_SECS)
    return out


# ---------------------------------------------------------------------------
# Inventario por ETAPA del flujo de producción (live, kg desde Asinfo)
# ---------------------------------------------------------------------------
# TMT 2026-07-08 — para /flujo-produccion la dueña pidió ver el inventario
# live de Asinfo desglosado por etapa (Hilo / Tela Cruda / Terminada) con los
# saldos EN PROCESO entre pasos. Consolida stock_asinfo_lote_totales() (saldos
# de bodega 51/52/53) con fabricacion_proceso(52/53) (WIP entre pasos), usando
# EXACTAMENTE la combinación de las tabs Fabricación (stock_asinfo/views.py):
#     Hilo total  = bodega 51 + En proceso TC (saldo de fabricacion_proceso 52)
#     Cruda total = bodega 52 + En proceso PT (saldo de fabricacion_proceso 53)
# Solo kg — los dólares de Asinfo no son confiables (ver módulo de stock/lote).

_INVENTARIO_ETAPA_TTL_SECS = 300  # 5 min (antes 10 — dueña 2026-07-18)
_INVENTARIO_ETAPA_CACHE: dict = {}


# ── Caché de FRACASO: ventana corta ────────────────────────────────────────
# TMT 2026-07-29. Varias funciones de acá cacheaban por 5 min el resultado de
# Metabase SIN mirar si Metabase había contestado. `fetch_dataset` devuelve []
# tanto si la query no tiene filas como si hubo timeout/401/500, así que UN
# hipo dejaba el stock vacío durante toda la ventana: el balance caía a los kg
# del dBase y la Utilidad Real aparecía ~495.000 más baja SIN AVISAR
# (29/07: 196.010 en vez de 687.519; se "arregló sola" al vencer el TTL).
#
# NO se saca la caché del fracaso: si Metabase está caído, no cachear nada
# haría que CADA request dispare la query pesada (hasta 20 s cada una) y la
# app se arrastraría. Se guarda igual, pero con ventana corta — reintenta a
# los 30 s en vez de a los 5 min, y mientras tanto no martilla Metabase.
#
# El camino feliz NO cambia: mismo TTL de siempre, misma velocidad.
_FALLO_TTL_SECS = 30


#: ¿El valor que está hoy en cada caché vino de una consulta que SALIÓ BIEN?
#: `_cache_put` lo anota acá para que quien COMBINA varias fuentes (el
#: inventario por etapa) pueda distinguir "contestaron todas" de "contestaron
#: dos de tres" — que es lo que produce un número plausible pero equivocado.
#: Va aparte y no dentro de la tupla porque los ~20 lectores de estas cachés
#: esperan exactamente `(ts, valor)`.
_OK_POR_CACHE: dict = {}


def _cache_put(cache: dict, clave, valor, ok: bool, ttl: int) -> None:
    """Guarda `valor` con TTL normal si `ok`, con ventana corta si no.

    Los lectores hacen `if cached and (now - cached[0]) < TTL`, así que para
    acortar la ventana se guarda el timestamp ENVEJECIDO: un fracaso entra
    como si ya tuviera (ttl − 30 s) de antigüedad y vence a los 30 s. Así no
    hay que tocar ninguno de los lectores.
    """
    import time as _t
    ahora = _t.time()
    cache[clave] = (ahora if ok else ahora - max(0, ttl - _FALLO_TTL_SECS), valor)
    _OK_POR_CACHE[(id(cache), clave)] = bool(ok)


def _cache_ok(cache: dict, clave) -> bool:
    """¿Lo último que se guardó en esa caché vino de una consulta buena?

    Ante la duda dice que sí: una caché que todavía no pasó por `_cache_put`
    (proceso recién arrancado) no tiene por qué disparar una falsa alarma.
    """
    return bool(_OK_POR_CACHE.get((id(cache), clave), True))


# ── Las dos patas del balance vencen JUNTAS ───────────────────────────────
# TMT 2026-07-31. Cuando llega una importación se mueven dos cosas a la vez:
# el ANTICIPO sale del activo (porque Asinfo le puso fecha de recepción) y el
# STOCK entra (porque los lotes aparecen en la bodega). Medido contra Asinfo el
# mismo día: las recepciones NO son parciales — se crean completas de una
# (fecha_creacion y fecha_modificacion a 1-3 segundos) y los lotes entran al
# saldo en el mismo instante. O sea: en el ERP las dos patas SÍ pasan juntas.
#
# El desfase lo poníamos NOSOTROS. Cada pata venía de su propia caché de 5
# minutos, y cada una vencía por su lado: había ventanas de hasta 5 minutos en
# las que una ya había visto la recepción y la otra no. Una importación típica
# son ~24.500 kg × ~3 US$/kg ≈ 74.000 de utilidad que aparecían y desaparecían
# solos — del tamaño exacto del salto que vio Alex el 31/07 (609 → 688 en
# minutos).
#
# Acá las dos vencen al mismo tiempo: cuando toca refrescar se invalidan las
# DOS y se leen una atrás de la otra. La ventana de desfase pasa de 5 minutos a
# los ~2 segundos que tardan las dos consultas. El costo contra Metabase no
# cambia: sigue siendo una vuelta cada 5 minutos por pata.
_PAR_BALANCE_TTL_SECS = 300
_PAR_BALANCE_TS = 0.0

#: Alinea UNO SOLO a la vez. Sin esto, los tres que abren Resultados en el
#: mismo segundo en que vence el TTL cruzan el puente los tres: cada uno ve el
#: reloj vencido, tira las cachés que el otro acaba de llenar y vuelve a
#: preguntar. Con el candado, el primero alinea y los demás esperan esos dos
#: segundos UNA vez y se encuentran la pantalla lista. TMT 2026-08-26.
_PAR_BALANCE_CANDADO = _threading.Lock()


def alinear_lecturas_del_balance() -> bool:
    """Deja el inventario y las importaciones leídos en la MISMA vuelta.

    La llama el CALENTADOR cada vuelta (`modules/_lib/warmup.py`), y también el
    balance por las dudas. Devuelve True si esta llamada disparó el refresco
    (útil para los tests). Nunca lanza: si Asinfo no está armado, no hace nada
    y el balance sigue su camino de siempre.

    ⭐ TMT 2026-08-26 (dueña): *"resultados tarda en cargar"*. Medido: en
    caliente la pantalla son 15 ms, pero cada 5 minutos el reloj de acá vence y
    el PRIMERO que la abre pagaba las dos consultas —1,3 s— adentro de su
    request. El calentador no podía evitarlo: él sólo leía las dos cachés (y le
    daban HIT), mientras que esto las TIRA a propósito para leerlas juntas. O
    sea que el trabajo del calentador se descartaba y lo terminaba haciendo el
    usuario. Ahora el calentador llama a esto, que es lo que refresca de
    verdad, y el que abre Resultados se encuentra el reloj en hora.
    """
    global _PAR_BALANCE_TS
    import time as _t

    ahora = _t.time()
    if (ahora - _PAR_BALANCE_TS) < _PAR_BALANCE_TTL_SECS:
        return False
    with _PAR_BALANCE_CANDADO:
        # El de al lado pudo alinear mientras se esperaba el candado: si lo
        # hizo, esto ya está en hora y volver a tirar las cachés sería pagar
        # el puente dos veces para llegar al mismo número.
        if (_t.time() - _PAR_BALANCE_TS) < _PAR_BALANCE_TTL_SECS:
            return False
        return _alinear_ahora(ahora)


def _alinear_ahora(ahora: float) -> bool:
    """El refresco propiamente dicho. Se entra con el candado tomado."""
    global _PAR_BALANCE_TS

    try:
        _INVENTARIO_ETAPA_CACHE.pop("all", None)
        _STOCK_LOTE_TOTALES_CACHE.pop("all", None)
        _IMPORT_CACHE.clear()
        inventario_por_etapa()
        importaciones_asinfo()
    except Exception:  # noqa: BLE001 -- alinear cachés no puede tumbar el balance
        _PAR_BALANCE_TS = ahora - max(0, _PAR_BALANCE_TTL_SECS - _FALLO_TTL_SECS)
        return True
    # Si alguna de las dos falló, que se reintente a los 30 s (mismo criterio
    # que `_cache_put`): quedan desalineadas y no queremos esperar 5 minutos.
    ok = (_cache_ok(_INVENTARIO_ETAPA_CACHE, "all")
          and _cache_ok(_STOCK_LOTE_TOTALES_CACHE, "all"))
    _PAR_BALANCE_TS = (
        ahora if ok
        else ahora - max(0, _PAR_BALANCE_TTL_SECS - _FALLO_TTL_SECS)
    )
    return True


#: El último inventario COMPLETO que contestó Asinfo. Sirve de red: si en la
#: próxima vuelta falla una de las tres consultas, se devuelve éste en vez de
#: uno incompleto. Se pierde al reiniciar la app, y está bien: recién ahí no
#: hay nada mejor que caer al dBase.
_ULTIMO_INVENTARIO_BUENO: dict | None = None


def _ultimo_bueno_o(vacio: dict) -> dict:
    """El último inventario bueno, marcado como viejo; si no hay, el vacío."""
    if _ULTIMO_INVENTARIO_BUENO:
        return {**_ULTIMO_INVENTARIO_BUENO, "de_cache_vieja": True}
    return vacio


def inventario_por_etapa() -> dict:
    """Kg de inventario live por etapa del flujo (fail-soft).

    Returns dict:
        disponible     — bool. False si el bridge Asinfo no está armado o las
                         queries no devolvieron nada.
        hilo           — kg en bodega 51 (Hilo).
        tela_cruda     — kg en bodega 52 (Tela Cruda).
        terminada      — kg en bodega 53 (Producto Terminado).
        en_proceso_tc  — kg despachados a tejer aún no devueltos como TC
                         (saldo de fabricacion_proceso(52)).
        en_proceso_pt  — kg de TC despachados a tinturar/confeccionar aún no
                         devueltos como PT (saldo de fabricacion_proceso(53)).
        esperando_orden_hilo — kg despachados HOY sin orden de fabricación que
                         el balance sostiene hasta las 18:00 EC (0 si el
                         placeholder está apagado o ya pasó el corte).
        esperando_orden_cruda — ídem para tela cruda.
        hilo_total     — hilo + en_proceso_tc + esperando_orden_hilo.
        cruda_total    — tela_cruda + en_proceso_pt + esperando_orden_cruda.
        total          — cadena completa (hilo + tc_wip + cruda + pt_wip + PT).

    Todos los valores son 0.0 cuando disponible es False. Nunca lanza.
    """
    import time as _time
    now = _time.time()
    cached = _INVENTARIO_ETAPA_CACHE.get("all")
    if cached and (now - cached[0]) < _INVENTARIO_ETAPA_TTL_SECS:
        return cached[1]

    vacio = {
        "disponible": False,
        "hilo": 0.0, "tela_cruda": 0.0, "terminada": 0.0,
        "en_proceso_tc": 0.0, "en_proceso_pt": 0.0,
        "hilo_total": 0.0, "cruda_total": 0.0, "total": 0.0,
        "esperando_orden_hilo": 0.0, "esperando_orden_cruda": 0.0,
    }
    if not disponible():
        return vacio

    try:
        totales = stock_asinfo_lote_totales() or []
        # ¿Contestaron las TRES fuentes? Si falla una sola, el inventario que
        # sale de acá es plausible pero está incompleto — ver el guard de abajo.
        _ok_bodegas = _cache_ok(_STOCK_LOTE_TOTALES_CACHE, "all")
        por_bodega = {int(r.get("id_bodega")): float(r.get("total_kg") or 0)
                      for r in totales}
        # WIP entre pasos = "en máquinas" = material despachado − producido.
        # HILO y CRUDA en máquinas = TODAS las órdenes abiertas (saldo = issued −
        # fab), no solo las que ya produjeron. El hilo/cruda despachado a una OFT
        # YA salió de su bodega (51/52); si no se cuenta como WIP, el total se
        # subvalúa. Dueña 2026-07-13: sacar el filtro fab>0 que tenía el HILO —
        # la planilla de la fábrica (SKK) cuenta ese staged (en máquinas hilo
        # ~45.880), y eran ~6.600 kg (4 OFTs con hilo despachado y fab=0) que no
        # los contaba nadie. (fabricacion_proceso sigue exponiendo
        # saldo_produciendo por si se quiere el corte fab>0.)
        wip_tc = float((fabricacion_proceso(52).get("resumen") or {}).get("saldo") or 0)
        _ok_52 = _cache_ok(_FABRICACION_CACHE, 52)
        wip_pt = float((fabricacion_proceso(53).get("resumen") or {}).get("saldo") or 0)
        _ok_53 = _cache_ok(_FABRICACION_CACHE, 53)
    except Exception:  # noqa: BLE001
        return _ultimo_bueno_o(vacio)

    hilo = por_bodega.get(51, 0.0)
    tela_cruda = por_bodega.get(52, 0.0)
    terminada = por_bodega.get(53, 0.0)
    # TMT 2026-08-11 (dueña): el material despachado HOY sin orden de
    # fabricación sigue siendo nuestro —está en el telar— pero no lo reclama
    # ninguna OFT, así que sin esto la bodega lo descuenta entero en el acto.
    # Lo sostiene hasta las 18:00 EC; ahí se da de baja, avisada. Apagado por
    # defecto (`HILO_PLACEHOLDER=1` lo enciende) porque mueve la utilidad.
    try:
        from modules.asinfo import hilo_sin_of as _hso
        _esperando = _hso.esperando_orden_kg()
    except Exception:  # noqa: BLE001 -- nunca romper el inventario
        _esperando = {}
    esperando_hilo = max(0.0, float(_esperando.get(51, 0.0)))
    esperando_cruda = max(0.0, float(_esperando.get(52, 0.0)))
    # Sólo saldos WIP positivos suman stock (ídem stock_en_proceso()).
    wip_tc = max(0.0, wip_tc)
    wip_pt = max(0.0, wip_pt)

    if not (hilo or tela_cruda or terminada or wip_tc or wip_pt):
        # Nada vino — tratar como no disponible (fail-soft).
        return _ultimo_bueno_o(vacio)

    # ── GUARD del inventario INCOMPLETO (TMT 2026-07-31) ────────────────────
    # El guard de arriba sólo ataja "no vino NADA". El caso caro es el otro:
    # contestan las bodegas y se cae `fabricacion_proceso(52)` (timeout de 20 s
    # contra una query que ya se midió en 21). Ahí el inventario sale sin el
    # material EN MÁQUINAS —~45.000 kg de hilo, ~135.000 US$— y como el número
    # es plausible, el guard viejo lo dejaba pasar Y lo cacheaba 5 minutos.
    # Eso es lo que hacía saltar la Utilidad de un F5 al otro (31/07: Alex la
    # vio en 576, 545, 609 y 688 en diez minutos).
    #
    # Ahora: si faltó alguna fuente, se devuelve el ÚLTIMO INVENTARIO BUENO.
    # Un número quieto y de hace tres minutos es infinitamente mejor que uno
    # fresco y equivocado — y si no hay ninguno bueno todavía, se devuelve
    # vacío, que hace caer el balance al dBase (que al menos es coherente).
    if not (_ok_bodegas and _ok_52 and _ok_53):
        _LOG.warning(
            "inventario_por_etapa INCOMPLETO (bodegas=%s wip52=%s wip53=%s) — "
            "devuelvo el último bueno", _ok_bodegas, _ok_52, _ok_53,
        )
        parcial = _ultimo_bueno_o(vacio)
        # Ventana corta: reintentar a los 30 s, no dentro de 5 minutos.
        _cache_put(_INVENTARIO_ETAPA_CACHE, "all", parcial, False,
                   _INVENTARIO_ETAPA_TTL_SECS)
        return parcial

    out = {
        "disponible": True,
        "hilo": hilo,
        "tela_cruda": tela_cruda,
        "terminada": terminada,
        "en_proceso_tc": wip_tc,
        "en_proceso_pt": wip_pt,
        "esperando_orden_hilo": esperando_hilo,
        "esperando_orden_cruda": esperando_cruda,
        "hilo_total": hilo + wip_tc + esperando_hilo,
        "cruda_total": tela_cruda + wip_pt + esperando_cruda,
        "total": (hilo + wip_tc + esperando_hilo
                  + tela_cruda + wip_pt + esperando_cruda + terminada),
    }
    _cache_put(_INVENTARIO_ETAPA_CACHE, "all", out, True,
               _INVENTARIO_ETAPA_TTL_SECS)
    global _ULTIMO_INVENTARIO_BUENO
    _ULTIMO_INVENTARIO_BUENO = out
    return out


# ---------------------------------------------------------------------------
# Inventario por ETAPA AS-OF una fecha (snapshot histórico de Asinfo)
# ---------------------------------------------------------------------------
# TMT 2026-07-08 — para /flujo-produccion la dueña pidió un segundo cuadro de
# "MOVIMIENTOS DEL MES" cuyo Stock INICIAL salga de Asinfo (stock al ARRANQUE
# del mes seleccionado) en vez de las iniciales del programa. `saldo_producto_lote`
# guarda snapshots FECHADOS por (producto, bodega, lote); el saldo a una fecha
# de corte = el último snapshot por (producto, bodega, lote) con fecha <= corte
# (mismo patrón ROW_NUMBER que inventario_por_etapa()/stock_asinfo_lote_totales,
# pero con el filtro de fecha DENTRO de la partición).
#
# El WIP entre pasos (en proceso) NO se puede reconstruir a una fecha pasada:
# orden_fabricacion sólo guarda el estado ACTUAL (cantidad_fabricada de hoy), no
# el histórico. Por eso las etapas "en proceso" quedan en 0 para la foto as-of y
# hilo_total/cruda_total = el saldo de bodega puro (documentado). Sólo kg.

_INVENTARIO_ASOF_TTL_SECS = 300  # 5 min (antes 10 — dueña 2026-07-18)
_INVENTARIO_ASOF_CACHE: dict = {}


def inventario_por_etapa_a_fecha(fecha_corte) -> dict:
    """Kg de inventario por etapa AS-OF `fecha_corte` (fail-soft).

    Igual shape que inventario_por_etapa() (keys: disponible, hilo, tela_cruda,
    terminada, en_proceso_tc, en_proceso_pt, hilo_total, cruda_total, total),
    pero los saldos de bodega 51/52/53 se toman al último snapshot con
    `fecha <= fecha_corte`. El WIP entre pasos no es reconstruible a fecha
    pasada → en_proceso_tc/pt = 0.0 (y por ende hilo_total = hilo,
    cruda_total = tela_cruda).

    Args:
        fecha_corte: `date`/`datetime` o string 'YYYY-MM-DD'. El saldo se toma
                     al último snapshot en o antes de esta fecha (inclusive).

    Todos los valores son 0.0 y disponible=False si Asinfo no está armado o
    cualquier query falla. Nunca lanza. Cache TTL 10 min por fecha_corte.
    """
    import time as _time

    if hasattr(fecha_corte, "isoformat"):
        fecha_corte = fecha_corte.isoformat()[:10]
    fecha_corte = str(fecha_corte)[:10]

    vacio = {
        "disponible": False,
        "hilo": 0.0, "tela_cruda": 0.0, "terminada": 0.0,
        "en_proceso_tc": 0.0, "en_proceso_pt": 0.0,
        "hilo_total": 0.0, "cruda_total": 0.0, "total": 0.0,
    }

    # Sanitizar la fecha: sólo dígitos y guiones (defensa SQL injection —
    # va inline en el SQL de Metabase que no soporta bind params acá).
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha_corte):
        return vacio

    now = _time.time()
    cached = _INVENTARIO_ASOF_CACHE.get(fecha_corte)
    if cached and (now - cached[0]) < _INVENTARIO_ASOF_TTL_SECS:
        return cached[1]

    if not disponible():
        return vacio

    # Último snapshot por (producto, bodega, lote) con fecha <= corte, sumado
    # por bodega. Réplica de stock_asinfo_lote_totales() con el filtro de fecha
    # empujado a la partición del ROW_NUMBER.
    sql = f"""
        WITH ult AS (
            SELECT id_producto, id_bodega, id_lote, saldo,
                   ROW_NUMBER() OVER (
                       PARTITION BY id_producto, id_bodega, id_lote
                       ORDER BY fecha DESC, id_saldo_producto_lote DESC
                   ) AS rn
              FROM saldo_producto_lote
             WHERE fecha <= '{fecha_corte}'
               AND id_bodega IN (51, 52, 53)
        )
        SELECT id_bodega    AS id_bodega,
               SUM(saldo)   AS total_kg
          FROM ult
         WHERE rn = 1 AND saldo > 0
         GROUP BY id_bodega
    """
    try:
        # TMT 2026-07-31: CON estado. Este inventario de APERTURA es el `hi0`
        # del promedio ponderado del hilado; si Metabase no contesta y el vacío
        # se cachea 5 minutos, la tarifa $/kg se corre y revalúa TODO el stock.
        rows, _ok_asof = metabase_client.fetch_dataset_estado(
            2, sql, max_results=100)
        rows = rows or []
        por_bodega = {int(r.get("id_bodega")): float(r.get("total_kg") or 0)
                      for r in rows}
    except Exception:  # noqa: BLE001
        return vacio

    hilo = por_bodega.get(51, 0.0)
    tela_cruda = por_bodega.get(52, 0.0)
    terminada = por_bodega.get(53, 0.0)

    if not (hilo or tela_cruda or terminada):
        # Nada vino — tratar como no disponible (fail-soft). Si además fue
        # porque no contestaron, el vacío vence a los 30 s en vez de a los 5 min.
        _cache_put(_INVENTARIO_ASOF_CACHE, fecha_corte, vacio, _ok_asof,
                   _INVENTARIO_ASOF_TTL_SECS)
        return vacio

    # WIP entre pasos no es reconstruible a fecha pasada (ver nota arriba).
    en_proceso_tc = 0.0
    en_proceso_pt = 0.0

    out = {
        "disponible": True,
        "hilo": hilo,
        "tela_cruda": tela_cruda,
        "terminada": terminada,
        "en_proceso_tc": en_proceso_tc,
        "en_proceso_pt": en_proceso_pt,
        "hilo_total": hilo + en_proceso_tc,
        "cruda_total": tela_cruda + en_proceso_pt,
        "total": hilo + tela_cruda + terminada + en_proceso_tc + en_proceso_pt,
    }
    _cache_put(_INVENTARIO_ASOF_CACHE, fecha_corte, out, _ok_asof,
               _INVENTARIO_ASOF_TTL_SECS)
    return out


# ── La tarifa del hilado NO se recalcula cuando la fuente no contestó ──────
# TMT 2026-08-12 (dueña, viendo la utilidad en 78.149: *"esto nunca pero nunca
# puede pasar"*). A las 08:30 Asinfo dejó de contestar y las compras de hilado
# del mes —243.546 kg / 787.554 US$— se fueron a CERO, porque la lista de
# importaciones que las cruza vive allá. Sin compras el promedio ponderado no
# se diluye y el $/kg vuelve al de apertura: 3,0443 → 3,0201. Esos 2,42
# centavos por kilo × 2.660.021 kg de stock = −64.393 de utilidad, inventados
# por una consulta que no contestó.
#
# El guard de asimetría del 31/07 no lo agarra: mira "kilos sin dólares" y
# "dólares sin kilos", y acá vinieron LOS DOS en cero, que desde adentro se ve
# igual que un mes sin compras. El vigía `/admin/health/hilado-ukg` tampoco:
# compara el $/kg mostrado contra el esperado CON compras=0 — valida la
# cuenta, no los insumos. Dijo ok:true todo el tiempo.
#
# Regla (dueña 2026-08-12): **el $/kg se queda en el de la foto anterior**. Si
# las compras vienen en cero pero en NUESTRA base hay dólares de hilo del mes,
# no se revalúa nada: se sostiene la última tarifa buena hasta que Asinfo
# vuelva, y el balance lo dice. Mismo patrón que `_ULTIMO_INVENTARIO_BUENO`
# para los kilos: un número quieto es mejor que uno fresco y equivocado.
_ULTIMA_TARIFA_HILADO: dict | None = None


def dolares_hilo_del_mes(yy: int, mm: int) -> float:
    """US$ de compras de hilo (tipo H) del mes según NUESTRA base.

    No pasa por Asinfo: es el testigo de que el mes SÍ tuvo compras aunque el
    cruce las devuelva vacías. Fail-soft: 0.0 si no se puede leer (sin testigo
    no se congela nada, que es el comportamiento de siempre).
    """
    try:
        import db as _db
        from modules.informes.queries import NO_BACKFILL_WHERE as _nb
        r = _db.fetch_one(
            f"""
            SELECT COALESCE(SUM(COALESCE(importe, 0)), 0) AS us
              FROM scintela.compra
             WHERE fecha >= make_date(%s, %s, 1)
               AND fecha <  make_date(%s, %s, 1) + INTERVAL '1 month'
               AND UPPER(TRIM(tipo)) = 'H'
               AND COALESCE(stat, '') NOT IN ('X', 'Y')
               AND {_nb}
            """,
            (int(yy), int(mm), int(yy), int(mm)),
        ) or {}
        return float(r.get("us") or 0)
    except Exception:  # noqa: BLE001 -- fail-soft
        return 0.0


def _tarifa_hilado_previa(yy: int, mm: int) -> float:
    """El último $/kg de hilado calculado CON las compras a la vista.

    Primero la memoria del proceso; si está vacía (recién arrancado), la
    última foto de `scintela.traza_utilidad` que tenía compras — la foto con
    `compras_us = 0` es justamente la enferma, así que no sirve de ancla.
    0.0 si no hay ninguna.
    """
    prev = _ULTIMA_TARIFA_HILADO
    if prev and prev.get("yy") == int(yy) and prev.get("mm") == int(mm):
        return float(prev.get("ukg") or 0)
    try:
        import db as _db
        r = _db.fetch_one(
            """
            SELECT hilado_ukg
              FROM scintela.traza_utilidad
             WHERE creado_en >= make_date(%s, %s, 1)
               AND COALESCE(compras_us, 0) > 0
               AND COALESCE(hilado_ukg, 0) > 0
             ORDER BY creado_en DESC, id_traza DESC
             LIMIT 1
            """,
            (int(yy), int(mm)),
        ) or {}
        return float(r.get("hilado_ukg") or 0)
    except Exception:  # noqa: BLE001 -- fail-soft
        return 0.0


def mov_hilado_valuacion(yy: int, mm: int, open_ukg: float) -> dict:
    """$/kg y $ del HILADO del cuadro FLUJO — promedio ponderado apertura+compras.

    UNA sola fuente para el flujo (`_build_mov_asinfo`) y el balance
    (`informe_balance`), para que el $/kg del hilado (≈ 2,954) sea IDÉNTICO en los
    dos y no se pueda desincronizar. Dueña 2026-07-13: "el 2954 directamente = a
    la variable del flujo".

    `open_ukg` = costo $/kg de APERTURA del stock (del cierre / mov). El ingreso
    (compras/importaciones del mes) al costo real diluye/sube el promedio.

    Devuelve dict:
      stock_act_kg  = hilo actual (bodega) + en máquinas (WIP)
      stock_act_us  = valuación de ese stock al promedio ponderado
      stock_act_ukg = stock_act_us / stock_act_kg  ← el $/kg del hilado (2,954)
      avg_ukg       = promedio ponderado apertura+compras (para egresos)
      hi0, hi1, maq, compras, compras_us  = insumos (para trazabilidad)

    Fail-soft: si Asinfo no está disponible devuelve stock_act_ukg = open_ukg
    (nunca lanza, nunca rompe el balance ni el flujo)."""
    from datetime import date as _date
    global _ULTIMA_TARIFA_HILADO
    _open = float(open_ukg or 0)
    _fallback = {
        "disponible": False,
        "stock_act_kg": 0.0, "stock_act_us": 0.0,
        "stock_act_ukg": _open, "avg_ukg": _open,
        "hi0": 0.0, "hi1": 0.0, "maq": 0.0, "compras": 0.0, "compras_us": 0.0,
    }
    try:
        inv_inic = inventario_por_etapa_a_fecha(_date(int(yy), int(mm), 1))
        inv_act = inventario_por_etapa()
    except Exception:  # noqa: BLE001 -- fail-soft
        return _fallback
    hi0 = float(inv_inic.get("hilo") or 0)
    hi1 = float(inv_act.get("hilo") or 0)
    maq = float(inv_act.get("en_proceso_tc") or 0)
    if (hi0 + hi1) <= 0:
        return _fallback
    # kg FÍSICOS recibidos en el mes (para trazabilidad y para el chequeo del
    # flujo). NO es el denominador del promedio — ver el bloque de abajo.
    try:
        compras_fisicas = float(hilado_recibido_mes(int(yy), int(mm)) or 0)
    except Exception:  # noqa: BLE001
        compras_fisicas = 0.0
    # ── Los kilos NO entran al promedio hasta que llega su plata ────────────
    # TMT 2026-07-31 (dueña: "el stock ahora subió 20k... fijate si a la mañana
    # estaba distinto"). Asinfo marca la recepción y los kilos aparecen en la
    # bodega al instante; la compra con su importe la crea la conversión
    # DESPUÉS — hoy 31/07 fueron 8, 27 y 76 minutos de diferencia. Mientras
    # dura ese hueco, esos kilos entrarían al promedio ponderado SIN dólares,
    # o sea gratis, y el promedio revalúa los ~1,85 millones de kg de hilo que
    # hay en stock: cada dólar de compra que entra mueve el stock ~0,85 US$, y
    # cada kilo que entra sin su dólar lo mueve ~2,55 US$ para abajo. De ahí
    # los escalones de ±60.000 en la utilidad durante la mañana, y el rebote
    # cuando la compra por fin se carga.
    #
    # La plata y los kilos tienen que entrar JUNTOS: el denominador usa sólo
    # los kg cuyo importe YA está cargado (`kg_con_costo`). Los kilos que
    # todavía no tienen plata están igual en el stock (hi1) y se valúan al
    # promedio vigente — que es exactamente lo que no sabemos todavía cuánto
    # costaron. Cuando la compra entra, el promedio se mueve sólo por lo que
    # esa importación se aparta del promedio: un pasito, no un escalón.
    compras = 0.0
    compras_us = 0.0
    kg_sin_costo = 0.0
    try:
        from modules.importaciones import service as _impsvc
        _rec = _impsvc.costo_hilado_recibido_mes(int(yy), int(mm)) or {}
        compras_us = float(_rec.get("us") or 0)
        compras = float(_rec.get("kg_con_costo") or 0)
        kg_sin_costo = max(0.0, float(_rec.get("kg") or 0) - compras)
    except Exception:  # noqa: BLE001
        compras = 0.0
        compras_us = 0.0
        kg_sin_costo = 0.0
    # + COMPRAS LOCALES de hilo (dueña 2026-07-30): entran al promedio ponderado
    # igual que las importaciones, porque son compra real de hilo (con su tarifa).
    # Así el $/kg del hilado (compartido con el balance/Materia Prima) refleja el
    # costo de TODO lo comprado, no solo lo importado. Fail-soft: si Asinfo cae,
    # quedan solo las importaciones (import-only, como antes).
    try:
        from modules.compras_locales import service as _locsvc
        _loc = _locsvc.hilado_local_recibido_mes(int(yy), int(mm)) or {}
        compras += float(_loc.get("kg") or 0)
        compras_us += float(_loc.get("us") or 0)
    except Exception:  # noqa: BLE001
        pass
    # ── GUARD de la ASIMETRÍA kg / dólares (TMT 2026-07-31) ────────────────
    # Los KILOS comprados salen de Asinfo y los DÓLARES de nuestra base, cada
    # uno con su propio `except → 0`. Si llegan los kilos y no los dólares, el
    # promedio se calcula como si ese hilo hubiese entrado GRATIS: con 300.000
    # kg de apertura y 100.000 comprados, la tarifa cae 25 % de un saque y
    # revalúa los ~800.000 kg de stock. Es el mismo tipo de trampa que la
    # compra H con importe y sin kg del 16/07 (+83k de utilidad falsa), al
    # revés. Ante la asimetría NO se diluye: queda la tarifa de apertura.
    _asimetrico = (compras > 0 and compras_us <= 0) or (compras_us > 0 and compras <= 0)
    # Y el caso que nos costó 64.393 el 12/08: cero kg Y cero dólares mientras
    # NUESTRA base tiene hilo comprado este mes. Desde adentro se ve igual que
    # un mes sin compras; el testigo de que no lo es está en scintela.compra.
    _sin_compras = (compras <= 0 and compras_us <= 0
                    and dolares_hilo_del_mes(yy, mm) > 0)
    _motivo = ""
    if _asimetrico:
        _motivo = (f"los kg y los dólares de las compras del mes no se "
                   f"corresponden ({compras:,.0f} kg / {compras_us:,.2f} US$)")
    elif _sin_compras:
        _motivo = ("Asinfo no contestó las compras de hilado del mes: vinieron "
                   "en cero y en el programa hay compras de hilo cargadas")

    act_kg = hi1 + maq
    _congelada = _tarifa_hilado_previa(yy, mm) if _motivo else 0.0
    if _motivo:
        _LOG.warning(
            "mov_hilado_valuacion %s-%s: %s — %s", yy, mm, _motivo,
            (f"sostengo el $/kg anterior ({_congelada:.4f})" if _congelada > 0
             else f"sin tarifa anterior: uso la apertura ({_open:.4f})"),
        )
    if _congelada > 0:
        # El $/kg se queda EXACTO donde estaba (dueña 2026-08-12). No se
        # remezcla con la apertura ni se rearma con insumos que no llegaron:
        # cualquier recálculo acá revalúa 2,6 millones de kilos.
        avg = act_ukg = _congelada
        act_us = act_kg * _congelada
    else:
        avg = (_open if _motivo else
               (((hi0 * _open + compras_us) / (hi0 + compras))
                if (hi0 + compras) else _open))
        act_us = hi1 * avg + maq * _open
        act_ukg = (act_us / act_kg) if act_kg else avg
        if not _motivo and act_ukg > 0:
            _ULTIMA_TARIFA_HILADO = {"yy": int(yy), "mm": int(mm), "ukg": act_ukg}
    return {
        "disponible": True,
        "stock_act_kg": act_kg, "stock_act_us": act_us, "stock_act_ukg": act_ukg,
        "avg_ukg": avg, "hi0": hi0, "hi1": hi1, "maq": maq,
        "compras": compras, "compras_us": compras_us,
        # kg FÍSICOS recibidos según Asinfo (lo que el flujo muestra como
        # Ingresos) y los que todavía esperan que se cargue su compra. Si
        # `kg_sin_costo` no es 0, hay plata en camino que aún no está en la
        # tarifa — y esos kilos NO están diluyendo el promedio.
        "compras_fisicas": compras_fisicas,
        "kg_sin_costo": kg_sin_costo,
        "asimetrico": _asimetrico,
        # Con qué cara sale este $/kg: si `tarifa_motivo` no está vacío, los
        # insumos no eran de fiar. `tarifa_congelada` dice si además hubo una
        # tarifa anterior con la que sostenerlo (si no, cayó a la apertura y
        # el stock SÍ se revaluó — eso hay que gritarlo).
        "tarifa_congelada": bool(_congelada > 0),
        "tarifa_motivo": _motivo,
    }


# ---------------------------------------------------------------------------
# Importaciones de Asinfo (para cruzar contra compras/anticipos del programa)
# ---------------------------------------------------------------------------
# La lista "Importación" del ERP. La `Nota` (factura_proveedor.descripcion)
# lleva el código de la compra/anticipo del programa al final — ver
# concepto_parser.parse_nota_importacion(). Los DÓLARES de Asinfo (total) son
# referenciales; los confiables vienen del programa (scintela.compra).

_IMPORT_TTL_SECS = 300  # 5 minutos
_IMPORT_CACHE: dict = {}


# ---------------------------------------------------------------------------
# Kg por importación (detalle de la factura proveedor)
# ---------------------------------------------------------------------------
# TMT 2026-06-10 dueña: "importaciones no dice kg". El total en kg sale del
# detalle de la factura_proveedor. El nombre físico de la tabla de detalle no
# está documentado → se descubre UNA vez vía INFORMATION_SCHEMA (cacheado) y
# si no se encuentra, kg queda en None (la vista muestra —). Fail-soft total.

_IMPORT_KG_TTL_SECS = 300
_IMPORT_KG_CACHE: dict = {}
_IMPORT_KG_DETALLE: dict = {}  # {"tabla": ..., "col": ...} descubierto
_IMPORT_COSTO_HILADO_CACHE: dict = {}  # {im_numero: {costo,kg,usd_kg}} (promedio por tipo de hilado)


# El nombre REAL del detalle, verificado contra el ERP el 30/08/2026. El
# descubrimiento por INFORMATION_SCHEMA queda como confirmación, pero si no
# contesta se usa este nombre en vez de dejar los kg sin fuente.
_DETALLE_FP_CONOCIDO = {"tabla": "detalle_factura_proveedor", "col": "cantidad"}


def _descubrir_detalle_fp() -> dict | None:
    """Tabla+columna de cantidad del detalle de factura_proveedor.

    TMT 2026-08-30 — un solo fracaso del descubrimiento (Metabase caído un
    instante, p.ej. al arrancar el proceso) quedaba cacheado PARA SIEMPRE
    (`done=True` con hit=None) y `importaciones_kg` devolvía {} hasta el
    siguiente deploy: la columna KG de /importaciones toda en «—», el
    balance gritando "22 compra(s) del mes con importe pero SIN kg" y la
    dueña mandada a completar a mano kilos que estaban sanos en Asinfo.
    Ahora solo el ÉXITO se cachea para siempre; si el descubrimiento no
    contesta se usa el nombre conocido del detalle y se reintenta después.
    """
    if _IMPORT_KG_DETALLE.get("done"):
        return _IMPORT_KG_DETALLE.get("hit")
    sql = """
        SELECT c.TABLE_NAME AS tabla, c.COLUMN_NAME AS col
          FROM INFORMATION_SCHEMA.COLUMNS c
         WHERE c.TABLE_NAME LIKE '%factura_proveedor%'
           AND c.COLUMN_NAME LIKE '%cantidad%'
           AND EXISTS (
               SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS c2
                WHERE c2.TABLE_NAME = c.TABLE_NAME
                  AND c2.COLUMN_NAME = 'id_factura_proveedor'
           )
         ORDER BY CASE WHEN c.TABLE_NAME = 'detalle_factura_proveedor' THEN 0 ELSE 1 END,
                  CASE WHEN c.COLUMN_NAME = 'cantidad' THEN 0 ELSE 1 END
    """
    rows = metabase_client.fetch_dataset(2, sql, max_results=10)
    hit = None
    for r in rows:
        t, col = str(r.get("tabla") or ""), str(r.get("col") or "")
        if t and col and "precio" not in col.lower():
            hit = {"tabla": t, "col": col}
            break
    if hit:
        _IMPORT_KG_DETALLE["done"] = True
        _IMPORT_KG_DETALLE["hit"] = hit
        return hit
    # Sin respuesta: NO se graba `done` (la próxima llamada reintenta el
    # descubrimiento) y mientras tanto los kg salen del nombre conocido.
    return dict(_DETALLE_FP_CONOCIDO)


def importaciones_kg(limite: int = 400) -> dict[str, float]:
    """{im_numero: kg total} sumando el detalle. {} si no se pudo (fail-soft)."""
    import time as _time
    now = _time.time()
    cached = _IMPORT_KG_CACHE.get("all")
    if cached and (now - cached[0]) < _IMPORT_KG_TTL_SECS:
        return cached[1]
    det = _descubrir_detalle_fp()
    if not det:
        return {}
    sql = f"""
        SELECT TOP {int(limite)}
               fp.numero            AS im_numero,
               SUM(ISNULL(d.{det["col"]}, 0)) AS kg
          FROM factura_proveedor_importacion fpi
          JOIN factura_proveedor fp ON fp.id_factura_proveedor = fpi.id_factura_proveedor
          JOIN {det["tabla"]} d ON d.id_factura_proveedor = fp.id_factura_proveedor
         GROUP BY fp.numero, fpi.id_factura_proveedor
         ORDER BY fpi.id_factura_proveedor DESC
    """
    rows, ok = metabase_client.fetch_dataset_estado(2, sql, max_results=int(limite))
    out: dict[str, float] = {}
    for r in rows:
        try:
            out[str(r.get("im_numero") or "").strip()] = float(r.get("kg") or 0)
        except (TypeError, ValueError):
            continue
    _cache_put(_IMPORT_KG_CACHE, "all", out, ok, _IMPORT_KG_TTL_SECS)
    return out


def importaciones_costo_estimado(limite: int = 400) -> dict[str, dict]:
    """{im_numero: {"costo","kg","usd_kg","kg_sin_precio","ventana"}} — costo
    ESTIMADO de cada importación armado por TIPO DE HILADO.

    Para cada importación, costo estimado = Σ(promedio US$/kg del producto × kg
    de ese producto en la importación). El promedio por producto (= tipo de
    hilado) usa una ventana temporal (decisión dueña 2026-06-29):
      1. Primero los últimos 3 meses.
      2. Si ese producto no tuvo importaciones en 3 meses → últimos 6 meses.
      3. Si tampoco hay en 6 meses → ese tipo de hilado queda SIN precio
         (kg_sin_precio) y la UI debe pedir el costo a mano (no se inventa).

    Campos por importación:
        costo         — Σ(precio_ventana × kg) sólo de los hilados CON histórico
        kg            — kg totales de la importación
        usd_kg        — costo / kg_con_precio
        kg_sin_precio — kg cuyos hilados no tienen histórico 3m/6m (→ preguntar)
        ventana       — '3m' si todo salió de 3 meses, '6m' si algún hilado
                        necesitó la ventana de 6 meses, 'parcial' si falta precio

    Es un ESTIMADO editable; los dólares de Asinfo no son la fuente contable.
    Fail-soft: {} si Metabase no responde o no hay detalle.
    """
    import time as _time
    now = _time.time()
    cached = _IMPORT_COSTO_HILADO_CACHE.get("all")
    if cached and (now - cached[0]) < _IMPORT_KG_TTL_SECS:
        return cached[1]
    det = _descubrir_detalle_fp()
    if not det:
        return {}
    qty = det["col"]
    tabla = det["tabla"]
    sql = f"""
        WITH px AS (
            SELECT d.id_producto,
                   SUM(CASE WHEN fp.fecha >= DATEADD(month, -3, GETDATE())
                            THEN d.precio * d.{qty} END)
                     / NULLIF(SUM(CASE WHEN fp.fecha >= DATEADD(month, -3, GETDATE())
                            THEN d.{qty} END), 0) AS p3,
                   SUM(CASE WHEN fp.fecha >= DATEADD(month, -6, GETDATE())
                            THEN d.precio * d.{qty} END)
                     / NULLIF(SUM(CASE WHEN fp.fecha >= DATEADD(month, -6, GETDATE())
                            THEN d.{qty} END), 0) AS p6
              FROM {tabla} d
              JOIN factura_proveedor fp ON fp.id_factura_proveedor = d.id_factura_proveedor
             WHERE d.{qty} > 0 AND d.precio > 0
             GROUP BY d.id_producto
        )
        SELECT TOP {int(limite)}
               fp.numero AS im_numero,
               SUM(COALESCE(px.p3, px.p6) * d.{qty})                          AS costo,
               SUM(d.{qty})                                                   AS kg,
               SUM(CASE WHEN COALESCE(px.p3, px.p6) IS NULL THEN d.{qty} ELSE 0 END) AS kg_sin_precio,
               SUM(CASE WHEN px.p3 IS NULL AND px.p6 IS NOT NULL THEN d.{qty} ELSE 0 END) AS kg_solo_6m
          FROM {tabla} d
          JOIN factura_proveedor_importacion fpi ON fpi.id_factura_proveedor = d.id_factura_proveedor
          JOIN factura_proveedor fp ON fp.id_factura_proveedor = d.id_factura_proveedor
          LEFT JOIN px ON px.id_producto = d.id_producto
         WHERE d.{qty} > 0
         GROUP BY fp.numero, fpi.id_factura_proveedor
         ORDER BY fpi.id_factura_proveedor DESC
    """
    try:
        rows, ok = metabase_client.fetch_dataset_estado(2, sql, max_results=int(limite))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for r in rows or []:
        try:
            im = str(r.get("im_numero") or "").strip()
            if not im:
                continue
            costo = float(r.get("costo") or 0)
            kg = float(r.get("kg") or 0)
            kg_sin = float(r.get("kg_sin_precio") or 0)
            kg_6m = float(r.get("kg_solo_6m") or 0)
            kg_con = max(0.0, kg - kg_sin)
            if kg_sin > 0:
                ventana = "parcial"
            elif kg_6m > 0:
                ventana = "6m"
            else:
                ventana = "3m"
            out[im] = {
                "costo": round(costo, 2),
                "kg": round(kg, 2),
                "usd_kg": round(costo / kg_con, 4) if kg_con > 0 else None,
                "kg_sin_precio": round(kg_sin, 2),
                "ventana": ventana,
            }
        except (TypeError, ValueError):
            continue
    _cache_put(_IMPORT_COSTO_HILADO_CACHE, "all", out, ok, _IMPORT_KG_TTL_SECS)
    return out


def cliente_ficha(codigos: list[str]) -> dict[str, dict]:
    """Ficha básica (nombre, RUC) de clientes por código, desde Asinfo.

    TMT 2026-06-10: los clientes nuevos del dBase llegan a PC sin ficha
    (CLIENTES.DBF viaja con semanas de atraso), pero Asinfo SIEMPRE tiene
    la razón social y el RUC (factura electrónica SRI). Este helper la
    trae vía Metabase para que el auto-create de cargar-desde-asinfo y el
    backfill /admin/clientes-ficha-asinfo no dejen clientes "(ficha
    pendiente)".

    Fail-soft: devuelve {} si Metabase está caído / sin config. Los
    códigos se sanitizan (solo A-Z0-9), el resto se descarta.

    Returns:
        {CODIGO: {"nombre": str, "ruc": str}} — solo los encontrados.
    """
    import re as _re
    cods = sorted({
        c for c in ((x or "").strip().upper() for x in codigos)
        if c and _re.fullmatch(r"[A-Z0-9]{1,10}", c)
    })
    if not cods or not metabase_client.disponible():
        return {}
    in_list = ", ".join(f"'{c}'" for c in cods)
    # `identificacion` puede no existir en todas las versiones del schema —
    # probamos con RUC y caemos a sin-RUC si la query falla.
    # TMT 2026-08-05: en Asinfo `empresa.codigo` ES el RUC — el código corto
    # de 3 letras vive en `nombre_comercial` (ver /admin/clientes-asinfo).
    # Buscar sólo por `codigo` hacía que esta ficha casi nunca matcheara y el
    # auto-create de facturas dejaba clientes sin nombre. Y el NOMBRE que se
    # devuelve es el FISCAL ("APELLIDO NOMBRE"), decisión dueña 05/08: "lo
    # nuevo vale más" — antes devolvía nombre_comercial, que es el código.
    sql_con_ruc = f"""
        SELECT UPPER(LTRIM(RTRIM(COALESCE(NULLIF(e.nombre_comercial, ''), e.codigo)))) AS codigo,
               COALESCE(NULLIF(e.nombre_fiscal, ''), e.nombre_comercial, '') AS nombre,
               COALESCE(e.identificacion, '') AS ruc
          FROM empresa e
         WHERE e.codigo IN ({in_list})
            OR UPPER(LTRIM(RTRIM(e.nombre_comercial))) IN ({in_list})
    """
    sql_sin_ruc = f"""
        SELECT UPPER(LTRIM(RTRIM(COALESCE(NULLIF(e.nombre_comercial, ''), e.codigo)))) AS codigo,
               COALESCE(NULLIF(e.nombre_fiscal, ''), e.nombre_comercial, '') AS nombre,
               '' AS ruc
          FROM empresa e
         WHERE e.codigo IN ({in_list})
            OR UPPER(LTRIM(RTRIM(e.nombre_comercial))) IN ({in_list})
    """
    rows = metabase_client.fetch_dataset(2, sql_con_ruc, max_results=len(cods) + 5)
    if not rows:
        rows = metabase_client.fetch_dataset(2, sql_sin_ruc, max_results=len(cods) + 5)
    out: dict[str, dict] = {}
    for r in rows or []:
        cod = str(r.get("codigo") or "").strip().upper()
        nombre = str(r.get("nombre") or "").strip()
        if cod and nombre:
            out[cod] = {"nombre": nombre[:200],
                        "ruc": str(r.get("ruc") or "").strip()[:16]}
    return out


def importaciones_asinfo(limite: int = 400) -> list[dict]:
    """Lista de importaciones del ERP, con su Nota (código de cruce).

    Cada fila:
        im_numero       — número de importación user-facing (IM-0000537)
        fecha           — fecha de la factura proveedor (YYYY-MM-DD)
        fecha_recepcion — fecha de recepción (YYYY-MM-DD o None)
        total_asinfo    — total del ERP (REFERENCIAL — no confiable)
        proveedor       — razón comercial/fiscal de la empresa
        prov_cod_asinfo — código de empresa en Asinfo (≠ código del programa)
        nota            — descripción libre; lleva el código del programa

    Returns:
        Lista de dicts ordenada por importación más reciente primero.
        [] si Metabase no está configurado o falla (fail-soft).
    """
    import time as _time
    cache_key = f"l{int(limite)}"
    now = _time.time()
    cached = _IMPORT_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _IMPORT_TTL_SECS:
        return cached[1]

    # La "Fecha Recepción" real es la del documento de recepción de mercadería
    # (recepcion_proveedor.fecha, doc BOD-…), NO fp.fecha_recepcion (siempre NULL).
    # Si la importación todavía no tiene recepción → en tránsito (no recibida).
    sql = f"""
        SELECT TOP {int(limite)}
               fp.numero                                       AS im_numero,
               CONVERT(varchar, fp.fecha, 23)                  AS fecha,
               CONVERT(varchar, rp.fecha, 23)                  AS fecha_recepcion,
               rp.numero                                       AS bod,
               fp.total                                        AS total_asinfo,
               COALESCE(e.nombre_comercial, e.nombre_fiscal, '') AS proveedor,
               e.codigo                                        AS prov_cod_asinfo,
               fp.descripcion                                  AS nota
          FROM factura_proveedor_importacion fpi
          JOIN factura_proveedor fp ON fp.id_factura_proveedor = fpi.id_factura_proveedor
          LEFT JOIN empresa e ON e.id_empresa = fp.id_empresa
          LEFT JOIN recepcion_proveedor rp ON rp.id_recepcion_proveedor = fp.id_recepcion_proveedor
         ORDER BY fpi.id_factura_proveedor DESC
    """
    # TMT 2026-07-31: `_estado` en vez de `fetch_dataset` a secas. Esta lista
    # alimenta el cruce anticipo↔importación y, por ahí, la tarifa $/kg del
    # hilado — que revalúa TODO el stock. Estaba cacheada 5 min sin mirar si
    # Metabase contestó: un fracaso movía la utilidad y quedaba pegado. Ahora
    # el fracaso vence a los 30 s, como el resto.
    rows, _ok_imp = metabase_client.fetch_dataset_estado(
        2, sql, max_results=int(limite))
    out = []
    for r in rows:
        try:
            frec = str(r.get("fecha_recepcion") or "").strip() or None
            out.append({
                "im_numero": str(r.get("im_numero") or "").strip(),
                "fecha": str(r.get("fecha") or "").strip() or None,
                "fecha_recepcion": frec,
                "recibida": frec is not None,
                "bod": str(r.get("bod") or "").strip(),
                "total_asinfo": float(r.get("total_asinfo") or 0),
                "proveedor": str(r.get("proveedor") or "").strip(),
                "prov_cod_asinfo": str(r.get("prov_cod_asinfo") or "").strip(),
                "nota": str(r.get("nota") or "").strip(),
            })
        except (TypeError, ValueError):
            continue
    _cache_put(_IMPORT_CACHE, cache_key, out, _ok_imp, _IMPORT_TTL_SECS)
    return out


def hilado_recibido_mes(yy: int, mm: int, limite: int = 1000) -> float:
    """Kg de hilado RECIBIDOS en Asinfo dentro del mes (yy, mm).

    "Recibido" = con documento de recepción (recepcion_proveedor.fecha) cuya
    fecha cae en el mes pedido. Es el ingreso de hilado REAL que ve el ERP —
    el equivalente Asinfo a las compras tipo='H' del dBase (que en PC recién
    se cargan al convertir a compra). Cruza importaciones_asinfo()
    (fecha_recepcion + recibida) con importaciones_kg() (kg por importación).

    Fail-soft: 0.0 si Metabase falla o no hay datos.
    """
    try:
        imps = importaciones_asinfo(limite=limite)
        kgmap = importaciones_kg(limite=limite)
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper la vista
        return 0.0
    pref = f"{int(yy):04d}-{int(mm):02d}-"
    total = 0.0
    for r in imps or []:
        if not r.get("recibida"):
            continue
        fr = r.get("fecha_recepcion") or ""
        if fr.startswith(pref):
            total += float(kgmap.get(r.get("im_numero")) or 0.0)
    return total


def hilado_egresos_mes(yy: int, mm: int, *, mov: dict | None = None,
                       importaciones_kg: float | None = None) -> dict:
    """EGRESOS de HILADO del mes — LA variable del cuadro "MOVIMIENTOS DEL MES
    (INICIAL ASINFO)" del Flujo de producción, compartida con el balance
    (fila Materia Prima). Dueña 2026-07-17: "tiene que salir del cuadro de
    movimientos asinfo, no de cualquier lado — el usuario tiene que poder ver
    de dónde viene". UNA sola fórmula, un solo lugar ([[coherencia]]).

        egresos = salidas reales de bodega 51 del mes
                  − reingresos de lote (ingreso bruto − importaciones recibidas)

    `mov` / `importaciones_kg`: valores ya calculados (el flujo los pasa para
    no repetir queries); si faltan se consultan acá. Fail-soft:
    disponible=False si Asinfo no da el movimiento de bodega.
    """
    from datetime import date as _date
    out = {"disponible": False, "egresos_kg": 0.0, "reingresos_kg": 0.0,
           "bruto_egr_kg": 0.0, "ingreso_bodega_kg": 0.0,
           "importaciones_kg": 0.0}
    try:
        if mov is None:
            mov = movimiento_bodega_mes(51, _date(int(yy), int(mm), 1)) or {}
        ing = float(mov.get("ingreso") or 0.0)
        egr = float(mov.get("egreso") or 0.0)
    except Exception:  # noqa: BLE001 -- fail-soft, nunca romper la vista
        return out
    if not ing and not egr:
        return out  # bodega sin movimiento visible → mejor fallback que un 0
    if importaciones_kg is None:
        try:
            importaciones_kg = float(hilado_recibido_mes(int(yy), int(mm)) or 0.0)
        except Exception:  # noqa: BLE001
            importaciones_kg = 0.0
        # + compras LOCALES de hilo (dueña 2026-07-30): el ingreso contra el que se
        # netea el egreso = importación + local, la MISMA suma que la fila Ingresos
        # del flujo. Así el BALANCE (que llama sin importaciones_kg) usa el mismo
        # neteo que el FLUJO (que la pasa ya sumada) y los dos egresos coinciden.
        try:
            from modules.compras_locales import service as _locsvc
            importaciones_kg += float(
                (_locsvc.hilado_local_recibido_mes(int(yy), int(mm)) or {}).get("kg") or 0.0
            )
        except Exception:  # noqa: BLE001 -- fail-soft
            pass
    # reingresos SIN clamp a 0 (dueña 2026-07-30 "la cuenta tiene que sumar"): si las
    # recepciones (import + local) superan el ingreso BRUTO de bodega, es hilo
    # recibido Y consumido dentro del MISMO mes → reingresos < 0 y el egreso lo
    # absorbe, así el egreso captura ese consumo y la columna del flujo CIERRA
    # (inicial + ingresos + máquinas − egresos = stock act.). Antes el max(,0) lo
    # escondía y esa diferencia sobraba en Stock act.
    rein = ing - float(importaciones_kg or 0.0)
    out.update({
        "disponible": True,
        "egresos_kg": max(egr - rein, 0.0),
        "reingresos_kg": rein,
        "bruto_egr_kg": egr,
        "ingreso_bodega_kg": ing,
        "importaciones_kg": float(importaciones_kg or 0.0),
    })
    return out


_FABRICACION_FLUJO_TTL_SECS = 300  # 5 min (antes 10 — dueña 2026-07-18)
_FABRICACION_FLUJO_CACHE: dict = {}


def fabricacion_flujo_mes(id_bodega: int, yy: int, mm: int) -> dict:
    """Consumo y producción de una bodega (52 Tejeduría → Tela Cruda /
    53 Tintorería → Terminado) para las órdenes CERRADAS en el mes.

    Devuelve `{"issued": kg_material_consumido, "fab": kg_producidos}`:
      · fab    = SUM(cantidad_fabricada) = kg que INGRESARON a la bodega
                 (tela cruda / terminado producido).
      · issued = SUM(cantidad_despachada) del material de entrada = kg que
                 se CONSUMIERON (hilo para tejer / crudo para tinturar).
      · issued − fab = DESPERDICIO (merma) del proceso.

    Se filtra por `fecha_cierre` (cuándo la orden terminó de producir = cuándo
    ingresó a la bodega), NO por `fecha` (creación de la orden). Sólo órdenes
    HOJA (no padres). Fail-soft: {"issued":0,"fab":0}.

    (Verificado en vivo 2026-07-09: b52 issued 46.540 / fab 46.126 → merma
    ~0,9%; b53 issued 88.212 / fab 83.943 → merma ~4,8%.)
    """
    zero = {"issued": 0.0, "fab": 0.0}
    try:
        yy = int(yy)
        mm = int(mm)
    except (TypeError, ValueError):
        return dict(zero)
    cache_key = (int(id_bodega), yy, mm)
    now = _time.time()
    cached = _FABRICACION_FLUJO_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _FABRICACION_FLUJO_TTL_SECS:
        return cached[1]
    d1 = f"{yy:04d}-{mm:02d}-01"
    ny, nm = (yy + 1, 1) if mm == 12 else (yy, mm + 1)
    d2 = f"{ny:04d}-{nm:02d}-01"
    mat = _MATERIAL_PROCESO.get(int(id_bodega), "")
    cat_filter = (
        f" AND prm.nombre_categoria_producto = '{mat}'" if mat else ""
    )
    sql = f"""
        WITH ofs AS (
            SELECT id_orden_fabricacion, ISNULL(cantidad_fabricada, 0) AS fab
              FROM orden_fabricacion
             WHERE id_bodega = {int(id_bodega)}
               AND fecha_cierre >= '{d1}' AND fecha_cierre < '{d2}'
               AND id_orden_fabricacion NOT IN (
                     SELECT id_orden_fabricacion_padre FROM orden_fabricacion
                      WHERE id_orden_fabricacion_padre IS NOT NULL)
        ),
        iss AS (
            SELECT j.id_orden_fabricacion,
                   SUM(ISNULL(d.cantidad_despachada, 0)) AS issued
              FROM detalle_orden_salida_material_orden_fabricacion j
              JOIN detalle_orden_salida_material d
                ON d.id_detalle_orden_salida_material = j.id_detalle_orden_salida_material
              JOIN producto prm ON prm.id_producto = d.id_producto
             WHERE j.id_orden_fabricacion IN (SELECT id_orden_fabricacion FROM ofs)
                   {cat_filter}
             GROUP BY j.id_orden_fabricacion
        )
        SELECT SUM(o.fab)                    AS fab,
               SUM(ISNULL(i.issued, 0))      AS issued
          FROM ofs o
          LEFT JOIN iss i ON i.id_orden_fabricacion = o.id_orden_fabricacion
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=10)
        r = (rows or [{}])[0]
        out = {
            "issued": float(r.get("issued") or 0.0),
            "fab": float(r.get("fab") or 0.0),
        }
        if rows:  # no congelar un [] por error de red (idéntico a facturas)
            _FABRICACION_FLUJO_CACHE[cache_key] = (now, out)
        return out
    except Exception:  # noqa: BLE001 -- fail-soft
        return dict(zero)


# ---------------------------------------------------------------------------
# El MISMO flujo, pero abierto POR PERÍODO (día o mes).
#
# TMT 2026-08-04 (dueña, sobre la pantalla de Terminado: *"no quiero artículos,
# quiero cantidades por día, así entendemos por qué el balance o la duda de
# utilidad"*, y después *"desperdicio... inicial producido vendido final... para
# cada mes"*). El total del mes no explica un salto: hace falta ver en qué
# período entraron los kg. Y como la utilidad es Δ patrimonio, lo que la mueve
# no es sólo lo que SALIÓ terminado sino lo que se COMIÓ de tela cruda — de ahí
# el desperdicio (crudo consumido − terminado producido) en cada fila.
#
# Es la misma query que `fabricacion_flujo_mes`, con el mismo criterio de hoja
# (`NOT IN` los padres) y el mismo filtro de categoría del material de entrada,
# agregada por `fecha_cierre` en vez de en un solo total. Que sea LA MISMA es
# la gracia: la suma de los períodos da EXACTAMENTE el número del mes que
# muestra Flujo de producción. Si un día se hace una copia con otro criterio,
# las dos pantallas empiezan a discutir y nadie sabe cuál creer.
# ---------------------------------------------------------------------------
_FABRICACION_DIA_TTL_SECS = 300  # 5 min, igual que el mensual
_FABRICACION_DIA_CACHE: dict = {}


def _rango_mes(yy: int, mm: int) -> tuple[str, str]:
    """(primero del mes, primero del siguiente) en ISO."""
    d1 = f"{yy:04d}-{mm:02d}-01"
    ny, nm = (yy + 1, 1) if mm == 12 else (yy, mm + 1)
    return d1, f"{ny:04d}-{nm:02d}-01"


# `CONVERT(..., 23)` da 'YYYY-MM-DD'; recortado a varchar(7) da 'YYYY-MM'. Es
# la clave de agrupación de cada granularidad, y también la clave del dict que
# devuelven las funciones de despacho — así las dos fuentes se cruzan sin
# parsear fechas.
_PERIODO_SQL = {"dia": "CONVERT(varchar(10), %s, 23)",
                "mes": "CONVERT(varchar(7),  %s, 23)"}


def _fabricacion_flujo_agrupado(id_bodega: int, d1: str, d2: str,
                                por: str) -> list[dict]:
    """Motor de `fabricacion_flujo_por_dia` / `_por_mes`. Ver el comentario de
    arriba: es la query de `fabricacion_flujo_mes` con un GROUP BY."""
    periodo = _PERIODO_SQL[por] % "fecha_cierre"
    mat = _MATERIAL_PROCESO.get(id_bodega, "")
    cat_filter = (
        f" AND prm.nombre_categoria_producto = '{mat}'" if mat else ""
    )
    sql = f"""
        WITH ofs AS (
            SELECT id_orden_fabricacion,
                   {periodo}                     AS periodo,
                   ISNULL(cantidad_fabricada, 0) AS fab
              FROM orden_fabricacion
             WHERE id_bodega = {id_bodega}
               AND fecha_cierre >= '{d1}' AND fecha_cierre < '{d2}'
               AND id_orden_fabricacion NOT IN (
                     SELECT id_orden_fabricacion_padre FROM orden_fabricacion
                      WHERE id_orden_fabricacion_padre IS NOT NULL)
        ),
        iss AS (
            SELECT j.id_orden_fabricacion,
                   SUM(ISNULL(d.cantidad_despachada, 0)) AS issued
              FROM detalle_orden_salida_material_orden_fabricacion j
              JOIN detalle_orden_salida_material d
                ON d.id_detalle_orden_salida_material = j.id_detalle_orden_salida_material
              JOIN producto prm ON prm.id_producto = d.id_producto
             WHERE j.id_orden_fabricacion IN (SELECT id_orden_fabricacion FROM ofs)
                   {cat_filter}
             GROUP BY j.id_orden_fabricacion
        )
        SELECT o.periodo,
               COUNT(*)                      AS n_ofs,
               SUM(o.fab)                    AS fab,
               SUM(ISNULL(i.issued, 0))      AS issued
          FROM ofs o
          LEFT JOIN iss i ON i.id_orden_fabricacion = o.id_orden_fabricacion
         GROUP BY o.periodo
         ORDER BY o.periodo
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=400)
    except Exception:  # noqa: BLE001 -- fail-soft
        return []
    return [
        {
            "periodo": str(r.get("periodo") or ""),
            "n_ofs": int(r.get("n_ofs") or 0),
            "issued": round(float(r.get("issued") or 0.0), 2),
            "fab": round(float(r.get("fab") or 0.0), 2),
        }
        for r in (rows or [])
    ]


def fabricacion_flujo_por_dia(id_bodega: int, yy: int, mm: int) -> list[dict]:
    """`fabricacion_flujo_mes` abierto por DÍA de cierre.

    Devuelve (fail-soft, `[]` si Asinfo no contesta):
        [{periodo 'YYYY-MM-DD', n_ofs, issued, fab}]  ordenado por período.

    · fab    = kg que INGRESARON a la bodega ese día (terminado producido).
    · issued = kg de material de entrada que se CONSUMIERON en esas órdenes
               (tela cruda para bodega 53, hilo para la 52).
    · issued − fab = DESPERDICIO del día.
    """
    try:
        yy, mm, id_bodega = int(yy), int(mm), int(id_bodega)
    except (TypeError, ValueError):
        return []
    cache_key = (id_bodega, yy, mm, "dia")
    now = _time.time()
    cached = _FABRICACION_DIA_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _FABRICACION_DIA_TTL_SECS:
        return cached[1]
    d1, d2 = _rango_mes(yy, mm)
    out = _fabricacion_flujo_agrupado(id_bodega, d1, d2, "dia")
    if out:  # no congelar un [] por error de red (mismo criterio que el mensual)
        _FABRICACION_DIA_CACHE[cache_key] = (now, out)
    return out


def fabricacion_flujo_por_mes(id_bodega: int, anio: int) -> list[dict]:
    """Igual, pero una fila por MES del año `anio` ('YYYY-MM' en `periodo`).

    TMT 2026-08-04 (dueña: *"para cada mes"*). UNA query para los 12 meses, no
    doce llamadas a `fabricacion_flujo_mes`.
    """
    try:
        anio, id_bodega = int(anio), int(id_bodega)
    except (TypeError, ValueError):
        return []
    cache_key = (id_bodega, anio, 0, "mes")
    now = _time.time()
    cached = _FABRICACION_DIA_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _FABRICACION_DIA_TTL_SECS:
        return cached[1]
    out = _fabricacion_flujo_agrupado(
        id_bodega, f"{anio:04d}-01-01", f"{anio + 1:04d}-01-01", "mes")
    if out:
        _FABRICACION_DIA_CACHE[cache_key] = (now, out)
    return out


def reset_fabricacion_dia_cache() -> None:
    """Vaciar el cache de fabricacion_flujo_por_dia/_por_mes (tests/deploy)."""
    _FABRICACION_DIA_CACHE.clear()


_DESPACHO_TTL_SECS = 300  # 5 min (antes 10 — dueña 2026-07-18)
_DESPACHO_CACHE: dict = {}


def despacho_fisico_mes(yy: int, mm: int, id_bodega: int = 53) -> float:
    """Kg FÍSICOS despachados a cliente en el mes (yy, mm) desde una bodega.

    Fuente = `despacho_cliente` + `detalle_despacho_cliente` de Asinfo: el
    despacho REAL que salió por la puerta al cliente (con lote y liberación de
    calidad), NO el saldo por lote (foto derivada) ni la factura (documento).
    Por defecto bodega 53 = Producto Terminado. Excluye despachos anulados.

    Este es el EGRESO físico real del terminado. Verificado en vivo 2026-07-10:
    julio 107.026 kg = FACTURA 97.528 + NTEN 9.520 (dif 22 kg); 6 meses
    1.758.611 = FACTURA+NTEN 1.757.915 (dif 696 kg, 0,04%). O sea el despacho
    físico coincide con lo facturado (FACTURA+NTEN) casi al kilo — la NTEN es
    justo el pedazo despachado cuya factura va aparte. Fail-soft: 0.0.
    """
    try:
        yy = int(yy)
        mm = int(mm)
    except (TypeError, ValueError):
        return 0.0
    cache_key = (yy, mm, int(id_bodega))
    now = _time.time()
    cached = _DESPACHO_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _DESPACHO_TTL_SECS:
        return cached[1]
    d1 = f"{yy:04d}-{mm:02d}-01"
    ny, nm = (yy + 1, 1) if mm == 12 else (yy, mm + 1)
    d2 = f"{ny:04d}-{nm:02d}-01"
    sql = f"""
        SELECT SUM(ISNULL(dd.cantidad, 0)) AS kg
          FROM despacho_cliente dc
          JOIN detalle_despacho_cliente dd
            ON dd.id_despacho_cliente = dc.id_despacho_cliente
         WHERE dc.fecha >= '{d1}' AND dc.fecha < '{d2}'
           AND dc.fecha_anulacion IS NULL
           AND dd.id_bodega = {int(id_bodega)}
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=10)
        r = (rows or [{}])[0]
        out = round(float(r.get("kg") or 0.0), 3)
        if rows:  # no congelar un [] por error de red
            _DESPACHO_CACHE[cache_key] = (now, out)
        return out
    except Exception:  # noqa: BLE001 -- fail-soft
        return 0.0


_DESPACHO_HOY_TTL_SECS = 120  # el recuadro del inicio se refresca cada minuto
_DESPACHO_HOY_CACHE: dict = {}


def despacho_fisico_dia_info(fecha, id_bodega: int = 53) -> dict:
    """{kg, medido_ts, fresco} de lo despachado a cliente EN UN DÍA.

    TMT 2026-08-13 (dueña): *"podemos poner otro número que sea el despachado,
    a veces se despacha más que facturado"* — el recuadro del inicio muestra
    los dos, porque la mercadería puede salir hoy y facturarse mañana.

    Mismo WHERE que `despacho_fisico_mes` (misma fuente, anulados afuera,
    bodega 53 = Producto Terminado), así que el día suma dentro del mes sin
    sorpresas. La fecha va como literal calculado en Python a propósito: el
    `GETDATE()` de Asinfo está en UTC y correría el día cinco horas (ver
    `despacho_sin_factura`).

    ⭐ **Si Asinfo no contesta se devuelve el ÚLTIMO NÚMERO BUENO** (decisión de
    la dueña, 13/08), con `fresco=False` y la hora en que se midió, para que la
    pantalla pueda decir "11:24" en vez de mostrar un cero que se lee como "no
    se despachó nada". Un fracaso NUNCA se guarda como valor (lección del
    29/07: cachear el fracaso de Metabase dejó la utilidad rota por horas).
    `kg=None` sólo cuando todavía no hubo ninguna lectura buena del día.
    """
    dia = str(fecha)[:10]
    cache_key = (dia, int(id_bodega))
    now = _time.time()
    cached = _DESPACHO_HOY_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _DESPACHO_HOY_TTL_SECS:
        return {"kg": cached[1], "medido_ts": cached[0], "fresco": True}

    def _viejo() -> dict:
        if cached:
            return {"kg": cached[1], "medido_ts": cached[0], "fresco": False}
        return {"kg": None, "medido_ts": None, "fresco": False}

    sql = f"""
        SELECT SUM(ISNULL(dd.cantidad, 0)) AS kg
          FROM despacho_cliente dc
          JOIN detalle_despacho_cliente dd
            ON dd.id_despacho_cliente = dc.id_despacho_cliente
         WHERE dc.fecha = '{dia}'
           AND dc.fecha_anulacion IS NULL
           AND dd.id_bodega = {int(id_bodega)}
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=10)
    except Exception:  # noqa: BLE001 -- fail-soft: la pantalla no se cae
        return _viejo()
    if not rows:
        return _viejo()
    out = round(float((rows[0] or {}).get("kg") or 0.0), 2)
    _DESPACHO_HOY_CACHE[cache_key] = (now, out)
    return {"kg": out, "medido_ts": now, "fresco": True}


def despacho_fisico_dia(fecha, id_bodega: int = 53) -> float:
    """Sólo los kilos de `despacho_fisico_dia_info`. 0.0 si nunca se pudo leer."""
    kg = despacho_fisico_dia_info(fecha, id_bodega).get("kg")
    return float(kg) if kg is not None else 0.0


# ---------------------------------------------------------------------------
# El MISMO despacho físico, abierto por período (día o mes).
#
# TMT 2026-08-04 (dueña, sobre Terminado: *"poné inicial producido vendido
# final"*). El "vendido" de esa tabla es ESTO: el despacho físico que salió por
# la puerta, no la factura (documento) ni el delta del saldo por lote. Misma
# fuente y mismo WHERE que `despacho_fisico_mes` — con un GROUP BY, para que el
# total de la tabla dé exactamente lo que da esa función.
# ---------------------------------------------------------------------------
_DESPACHO_PERIODO_CACHE: dict = {}


def _despacho_fisico_agrupado(id_bodega: int, d1: str, d2: str,
                              por: str) -> dict:
    """{periodo: kg}. Fail-soft: {} si Asinfo no contesta."""
    periodo = _PERIODO_SQL[por] % "dc.fecha"
    sql = f"""
        SELECT {periodo}                   AS periodo,
               SUM(ISNULL(dd.cantidad, 0)) AS kg
          FROM despacho_cliente dc
          JOIN detalle_despacho_cliente dd
            ON dd.id_despacho_cliente = dc.id_despacho_cliente
         WHERE dc.fecha >= '{d1}' AND dc.fecha < '{d2}'
           AND dc.fecha_anulacion IS NULL
           AND dd.id_bodega = {id_bodega}
         GROUP BY {periodo}
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=400)
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    return {str(r.get("periodo") or ""): round(float(r.get("kg") or 0.0), 2)
            for r in (rows or [])}


def despacho_fisico_por_dia(yy: int, mm: int, id_bodega: int = 53) -> dict:
    """{'YYYY-MM-DD': kg despachados} del mes. Fail-soft: {}."""
    try:
        yy, mm, id_bodega = int(yy), int(mm), int(id_bodega)
    except (TypeError, ValueError):
        return {}
    cache_key = (id_bodega, yy, mm, "dia")
    now = _time.time()
    cached = _DESPACHO_PERIODO_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _DESPACHO_TTL_SECS:
        return cached[1]
    d1, d2 = _rango_mes(yy, mm)
    out = _despacho_fisico_agrupado(id_bodega, d1, d2, "dia")
    if out:
        _DESPACHO_PERIODO_CACHE[cache_key] = (now, out)
    return out


def despacho_fisico_por_mes(anio: int, id_bodega: int = 53) -> dict:
    """{'YYYY-MM': kg despachados} del año. UNA query para los 12 meses."""
    try:
        anio, id_bodega = int(anio), int(id_bodega)
    except (TypeError, ValueError):
        return {}
    cache_key = (id_bodega, anio, 0, "mes")
    now = _time.time()
    cached = _DESPACHO_PERIODO_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _DESPACHO_TTL_SECS:
        return cached[1]
    out = _despacho_fisico_agrupado(
        id_bodega, f"{anio:04d}-01-01", f"{anio + 1:04d}-01-01", "mes")
    if out:
        _DESPACHO_PERIODO_CACHE[cache_key] = (now, out)
    return out


def reset_despacho_periodo_cache() -> None:
    """Vaciar el cache de despacho_fisico_por_dia/_por_mes (tests / deploy)."""
    _DESPACHO_PERIODO_CACHE.clear()


_MOVIMIENTO_BODEGA_TTL_SECS = 300  # 5 min (antes 10 — dueña 2026-07-18)
_MOVIMIENTO_BODEGA_CACHE: dict = {}


def movimiento_bodega_mes(id_bodega: int, corte) -> dict:
    """Ingreso/egreso FÍSICO de una bodega DESDE `corte` (exclusivo) hasta hoy,
    medido de los deltas del saldo por lote (`saldo_producto_lote`).

    CIERRA POR CONSTRUCCIÓN con el stock: para cada lote la suma de deltas =
    saldo_hoy − saldo_as_of(corte) (identidad telescópica), así que
        stock_as_of(corte) + ingreso − egreso = stock_hoy
    exactamente, siempre que `corte` sea el MISMO que usa el stock inicial.

    · ingreso = Σ subidas de saldo (+ altas de lote nuevas post-corte).
    · egreso  = Σ bajas de saldo.

    Es el movimiento REAL de la bodega (producción + devoluciones/reingresos del
    lado de ingreso; despachos/salidas del lado de egreso), NO la producción
    declarada (que subcuenta el ingreso) ni el despacho documento (que corre por
    encima del saldo por timing). Fail-soft: {"ingreso":0.0,"egreso":0.0}.
    """
    zero = {"ingreso": 0.0, "egreso": 0.0}
    if hasattr(corte, "isoformat"):
        corte = corte.isoformat()[:10]
    corte = str(corte)[:10]
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", corte):
        return dict(zero)
    cache_key = (int(id_bodega), corte)
    now = _time.time()
    cached = _MOVIMIENTO_BODEGA_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _MOVIMIENTO_BODEGA_TTL_SECS:
        return cached[1]
    sql = f"""
        WITH d AS (
            SELECT saldo,
                   LAG(saldo) OVER (
                       PARTITION BY id_producto, id_lote
                       ORDER BY fecha, id_saldo_producto_lote
                   ) AS prev,
                   fecha
              FROM saldo_producto_lote
             WHERE id_bodega = {int(id_bodega)}
        )
        SELECT
            SUM(CASE WHEN prev IS NULL AND saldo > 0 THEN saldo
                     WHEN prev IS NOT NULL AND saldo - prev > 0 THEN saldo - prev
                     ELSE 0 END) AS ingreso,
            SUM(CASE WHEN prev IS NOT NULL AND saldo - prev < 0 THEN prev - saldo
                     ELSE 0 END) AS egreso
          FROM d
         WHERE fecha > '{corte}'
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=10)
        r = (rows or [{}])[0]
        out = {
            "ingreso": round(float(r.get("ingreso") or 0.0), 3),
            "egreso": round(float(r.get("egreso") or 0.0), 3),
        }
        if rows:  # no congelar un [] por error de red
            _MOVIMIENTO_BODEGA_CACHE[cache_key] = (now, out)
        return out
    except Exception:  # noqa: BLE001 -- fail-soft
        return dict(zero)


# ---------------------------------------------------------------------------
# El MISMO movimiento de bodega, abierto por período (día o mes).
#
# TMT 2026-08-04 (dueña, sobre el descuadre que mostró Terminado Asinfo: *"sí,
# esto me suena raro"*). En 8 meses el encadenado producción−despacho daba
# 295.106 kg y Asinfo tenía 328.541: **33.435 kg que no entraron por producción
# ni salieron por venta**. Para saber de dónde salen hace falta el movimiento
# REAL de la bodega —el que cierra por construcción con el stock— puesto al
# lado de la producción declarada y del despacho a cliente:
#
#     ingreso_real − producido = lo que entró SIN ser producción
#                                (devoluciones de cliente, reingresos)
#     egreso_real  − vendido   = lo que salió SIN ser venta
#                                (bajas, reprocesos que vuelven a tintura)
#
# Misma query que `movimiento_bodega_mes` (LAG sobre saldo_producto_lote), con
# la ventana acotada y un GROUP BY. ⚠ La partición del LAG NO se filtra por
# fecha: para que el primer movimiento de la ventana sepa contra qué comparar,
# tiene que ver toda la historia del lote. El filtro va en el WHERE de afuera.
# ---------------------------------------------------------------------------
_MOVIMIENTO_PERIODO_CACHE: dict = {}


def _movimiento_bodega_agrupado(id_bodega: int, d1: str, d2: str,
                                por: str) -> dict:
    """{periodo: {ingreso, egreso}}. Fail-soft: {}."""
    periodo = _PERIODO_SQL[por] % "fecha"
    sql = f"""
        WITH d AS (
            SELECT saldo,
                   LAG(saldo) OVER (
                       PARTITION BY id_producto, id_lote
                       ORDER BY fecha, id_saldo_producto_lote
                   ) AS prev,
                   fecha
              FROM saldo_producto_lote
             WHERE id_bodega = {id_bodega}
        )
        SELECT {periodo} AS periodo,
            SUM(CASE WHEN prev IS NULL AND saldo > 0 THEN saldo
                     WHEN prev IS NOT NULL AND saldo - prev > 0 THEN saldo - prev
                     ELSE 0 END) AS ingreso,
            SUM(CASE WHEN prev IS NOT NULL AND saldo - prev < 0 THEN prev - saldo
                     ELSE 0 END) AS egreso
          FROM d
         WHERE fecha >= '{d1}' AND fecha < '{d2}'
         GROUP BY {periodo}
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=400)
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    return {
        str(r.get("periodo") or ""): {
            "ingreso": round(float(r.get("ingreso") or 0.0), 2),
            "egreso": round(float(r.get("egreso") or 0.0), 2),
        }
        for r in (rows or [])
    }


def movimiento_bodega_por_dia(id_bodega: int, yy: int, mm: int) -> dict:
    """{'YYYY-MM-DD': {ingreso, egreso}} del mes. Fail-soft: {}."""
    try:
        yy, mm, id_bodega = int(yy), int(mm), int(id_bodega)
    except (TypeError, ValueError):
        return {}
    cache_key = (id_bodega, yy, mm, "dia")
    now = _time.time()
    cached = _MOVIMIENTO_PERIODO_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _MOVIMIENTO_BODEGA_TTL_SECS:
        return cached[1]
    d1, d2 = _rango_mes(yy, mm)
    out = _movimiento_bodega_agrupado(id_bodega, d1, d2, "dia")
    if out:
        _MOVIMIENTO_PERIODO_CACHE[cache_key] = (now, out)
    return out


def movimiento_bodega_por_mes(id_bodega: int, anio: int) -> dict:
    """{'YYYY-MM': {ingreso, egreso}} del año. UNA query para los 12 meses."""
    try:
        anio, id_bodega = int(anio), int(id_bodega)
    except (TypeError, ValueError):
        return {}
    cache_key = (id_bodega, anio, 0, "mes")
    now = _time.time()
    cached = _MOVIMIENTO_PERIODO_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _MOVIMIENTO_BODEGA_TTL_SECS:
        return cached[1]
    out = _movimiento_bodega_agrupado(
        id_bodega, f"{anio:04d}-01-01", f"{anio + 1:04d}-01-01", "mes")
    if out:
        _MOVIMIENTO_PERIODO_CACHE[cache_key] = (now, out)
    return out


def reset_movimiento_periodo_cache() -> None:
    """Vaciar el cache de movimiento_bodega_por_dia/_por_mes (tests/deploy)."""
    _MOVIMIENTO_PERIODO_CACHE.clear()


_INGRESO_DIA_TTL_SECS = 300
_INGRESO_DIA_CACHE: dict = {}


def ingreso_bodega_por_dia(id_bodega: int, corte) -> list[dict]:
    """INGRESO físico de una bodega POR DÍA desde `corte` (exclusivo) — la
    versión diaria de `movimiento_bodega_mes().ingreso` (misma CTE de deltas
    por lote, mismo criterio de altas): la suma de los días = el total del
    mes EXACTO. Dueña 2026-07-20: el DIARIO de Tejeduría Asinfo tiene que
    sumar el ingreso a bodega (179.704), no las OFs cerradas (207.623).

    Devuelve [{"dia": "YYYY-MM-DD", "kg": float}] ordenado del más nuevo al
    más viejo. Fail-soft: [].
    """
    if hasattr(corte, "isoformat"):
        corte = corte.isoformat()[:10]
    corte = str(corte)[:10]
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", corte):
        return []
    cache_key = (int(id_bodega), corte)
    now = _time.time()
    cached = _INGRESO_DIA_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _INGRESO_DIA_TTL_SECS:
        return cached[1]
    sql = f"""
        WITH d AS (
            SELECT saldo,
                   LAG(saldo) OVER (
                       PARTITION BY id_producto, id_lote
                       ORDER BY fecha, id_saldo_producto_lote
                   ) AS prev,
                   fecha
              FROM saldo_producto_lote
             WHERE id_bodega = {int(id_bodega)}
        )
        SELECT CONVERT(varchar, fecha, 23) AS dia,
               SUM(CASE WHEN prev IS NULL AND saldo > 0 THEN saldo
                        WHEN prev IS NOT NULL AND saldo - prev > 0 THEN saldo - prev
                        ELSE 0 END) AS ingreso
          FROM d
         WHERE fecha > '{corte}'
         GROUP BY fecha
         ORDER BY fecha DESC
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=100) or []
        out = [
            {"dia": str(r.get("dia") or "")[:10],
             "kg": round(float(r.get("ingreso") or 0.0), 2)}
            for r in rows
            if float(r.get("ingreso") or 0.0) > 0.005
        ]
        if rows:
            _INGRESO_DIA_CACHE[cache_key] = (now, out)
        return out
    except Exception:  # noqa: BLE001 -- fail-soft
        return []


# ---------------------------------------------------------------------------
# Producción de tejeduría (bodega 52 = TELA CRUDA) por Asinfo — reemplaza la
# carga MANUAL del dBase (alguien tipeaba las compras tipo K a mano).
#
# Cada `orden_fabricacion` HOJA CERRADA de bodega 52 produce tela cruda:
# `cantidad_fabricada` = kg producidos, `fecha_cierre` = día. El TEJEDOR sale
# del inicio de `descripcion`:
#   · "MQ<nn> ..."  → INTELA (máquina propia, autoproducción) → cod KK
#   · "M REYES ..." → tercerizado (maquila) → cod RY
#   · "A PONCE ..." → tercerizado → cod AP
# Verificado en vivo 2026-07-14: julio = INTELA 96.786 / AP 2.644 / RY 1.633 kg.
# Consumido por la tab /produccion-tejeduria-asinfo (match contra compras K).
# ---------------------------------------------------------------------------
_PROD_TEJ_TTL_SECS = 300  # 5 min (antes 10 — dueña 2026-07-18)
_PROD_TEJ_CACHE: dict = {}
_PROD_TEJ_RANGO_CACHE: dict = {}
# Guard anti-inyección de las fechas que se interpolan en el SQL de Metabase.
# (el resto del módulo importa `re as _re` dentro de cada función; acá hace
#  falta a nivel módulo porque la constante se compila una sola vez)
import re as _re_mod  # noqa: E402

_FECHA_ISO_RE = _re_mod.compile(r"\d{4}-\d{2}-\d{2}")

INTELA_COD = "KK"
# Nombre en la descripción de la OF → codigo_prov de scintela.compra. Extender
# acá cuando aparezca un tejedor tercerizado nuevo (la tab marca los
# "desconocido" con cod vacío para que se agreguen).
TEJEDOR_TERCERIZADO_MAP = {
    "PONCE": "AP",
    "REYES": "RY",
}

#: Tejedores cuyo apellido aparece DENTRO de otras palabras o de notas al pie,
#: así que sólo valen anclados al ARRANQUE de la descripción.
#:
#: TMT 2026-07-30 (dueña: *"la factura de UN N.-131 no está en tus compras?"*).
#: **UN = RODRIGO UNDA** es tercerizado y Asinfo SÍ registra su producción como
#: `"R UNDA …"`, pero no estaba en el mapa ⇒ `_clasificar_tejedor` devolvía
#: cod='' y `_a_intela_si_desconocido` lo pasaba a INTELA. Efecto doble: su
#: producción se contaba como propia, y el motor de tejeduría (que sólo carga
#: los de `TERCERIZADOS_VALIDOS`) **nunca creó una compra de UN**. La tarifa
#: UN 1,1500 ya estaba cargada — el filtro la frenaba tres pasos antes.
#:
#: ⚠ Por qué ANCLADO y no `"UNDA" in desc` como PONCE/REYES: "UNDA" aparece
#: dentro de "SEGUNDA" y "FUNDAS", y además hay 6 OFs de FF 96CM que dicen
#: `"… NUEVO TEJIDO SR UNDA"` en una nota y NO son de él. De las 22 OFs de 2026
#: que contienen "R UNDA", sólo las **16 que ARRANCAN** con eso son suyas.
TEJEDOR_TERCERIZADO_PREFIJO = {
    r"^R\s+UNDA\b": ("UN", "Unda"),
}

#: (inicial, apellido) → (codigo_prov, label) para tolerar ERRORES DE TIPEO.
#:
#: TMT 2026-07-30 (dueña, mirando dos avisos de "tejedor sin reconocer": *"me
#: parece que esto es un error de tipeo y en verdad es Reyes"*). Tenía razón.
#: En dos años y medio de OFs hay **7 con el apellido mal tipeado**, y las 7
#: se contaron como producción PROPIA — o sea, nunca se les creó la compra:
#:
#:   M RTEYES ×2 · M RERYES · M RYES ×2 · M REYRS  (Reyes, ~2.030 kg)
#:   A PONGE                                        (Ponce,  1.929 kg)
#:
#: Todas están a **UN solo error** del apellido correcto, así que se reconocen
#: con distancia de edición ≤ 1. Las guardas que lo hacen seguro y no un
#: adivinador: (1) la descripción tiene que arrancar con la INICIAL exacta del
#: tejedor (`M` Reyes · `A` Ponce · `R` Unda) — verificado contra Asinfo: TODA
#: descripción que arranca con una letra sola y un espacio es de uno de los
#: tres; (2) la primera letra del apellido tiene que coincidir; (3) mínimo 4
#: caracteres. Sin (1) y (2), "FUNDA" entraría como "UNDA".
TEJEDOR_TIPEO = {
    ("M", "REYES"): ("RY", "Reyes"),
    ("A", "PONCE"): ("AP", "Ponce"),
    ("R", "UNDA"): ("UN", "Unda"),
}


def _a_un_error_de(palabra: str, canonico: str) -> bool:
    """True si `palabra` está a **una sola** edición de `canonico`.

    Una inserción, un borrado o una sustitución. Sin librerías: son palabras de
    6 letras y se corre por cada OF.
    """
    if len(canonico) < 4 or len(palabra) < 4:
        return False
    if palabra == canonico:
        return True
    if abs(len(palabra) - len(canonico)) > 1 or palabra[0] != canonico[0]:
        return False
    if len(palabra) == len(canonico):  # sustitución
        return sum(1 for a, b in zip(palabra, canonico) if a != b) == 1
    corta, larga = ((palabra, canonico) if len(palabra) < len(canonico)
                    else (canonico, palabra))
    i = 0
    while i < len(corta) and corta[i] == larga[i]:
        i += 1
    return corta[i:] == larga[i + 1:]        # inserción / borrado


def _clasificar_tejedor(descripcion: str) -> tuple[str, str, bool]:
    """(codigo_prov, label, es_intela) desde orden_fabricacion.descripcion.

    INTELA si arranca con máquina (`MQ02`, `MQ 11`, `MAQ 21`, `M07`…). Si no,
    tercerizado: se mapea el nombre → codigo_prov, tolerando UN error de tipeo.
    Un tejedor no mapeado devuelve cod='' (la tab lo muestra como 'desconocido'
    y la campanita avisa que hay que darlo de alta).

    ⚠ **Asinfo NO tiene un código de tejedor.** Se verificó columna por columna
    (2026-07-30): `id_entidad_origen`, `nombre_responsable`, `id_sucursal`,
    `id_ruta_fabricacion`, atributos — todas NULL o idénticas entre INTELA,
    Ponce, Reyes y Unda. El texto de `descripcion` es el ÚNICO lugar donde vive
    la identidad del tejedor, y por eso hay que ser tolerante con cómo se tipea.
    """
    import re as _re
    d = (descripcion or "").strip()
    du = d.upper()
    # Máquina propia. Cubre las formas que aparecen en Asinfo — MQ02 · MQ 11 ·
    # MAQ 21 · M07 · M15 — sin tocar a los tejedores, que después de la inicial
    # llevan una LETRA, no un dígito.
    #
    # ⭐ `Q02` (sin la M) es un TIPEO, no una forma: aparece UNA sola vez en
    # toda la historia desde 2025 (OFT-000040738, 450,18 kg) y en agosto salió
    # por la campanita como "tejedor sin reconocer", contándose como producción
    # propia igual pero con un aviso al pedo. Dueña 2026-08-19: confirmado que
    # es máquina. Es seguro aceptarlo: ningún tejedor puede colisionar, porque
    # después de la inicial suelta va un APELLIDO, nunca un dígito.
    if _re.match(r"^(?:M(?:A?Q)?|Q)\s*\d", du):
        return (INTELA_COD, "INTELA", True)
    # Los anclados primero: son los que necesitan estar al arranque para no
    # confundirse con otra palabra o con una nota al pie.
    for patron, (cod, label) in TEJEDOR_TERCERIZADO_PREFIJO.items():
        if _re.match(patron, du):
            return (cod, label, False)
    for nombre, cod in TEJEDOR_TERCERIZADO_MAP.items():
        if nombre in du:
            return (cod, nombre.capitalize(), False)
    # Apellido MAL TIPEADO — ver TEJEDOR_TIPEO. Sólo si la descripción arranca
    # con la inicial exacta del tejedor y el apellido está a una sola edición.
    palabras = du.split()
    if len(palabras) >= 2 and len(palabras[0]) == 1:
        for (inicial, apellido), (cod, label) in TEJEDOR_TIPEO.items():
            if palabras[0] == inicial and _a_un_error_de(palabras[1], apellido):
                return (cod, label, False)
    label = " ".join(d.split()[:2]) or "?"
    return ("", label, False)


def produccion_tejeduria_mes(anio: int, mes: int) -> dict:
    """Producción de tela cruda (bodega 52) del mes, por OF, día y tejedor,
    desde Asinfo. Reemplaza la carga manual del dBase.

    Solo OFs HOJA CERRADAS (estado_produccion=5) con `fecha_cierre` en el mes.

    Devuelve (fail-soft):
        {disponible, anio, mes,
         ofs:[{numero, dia 'YYYY-MM-DD', kg, descripcion, cod, label, es_intela}],
         por_tejedor:[{cod, label, es_intela, ofs, kg}],
         total_kg}
    Si Asinfo cae o no hay datos → disponible=False, listas vacías.
    """
    vacio = {"disponible": False, "anio": anio, "mes": mes,
             "ofs": [], "por_tejedor": [], "total_kg": 0.0}
    try:
        anio = int(anio)
        mes = int(mes)
    except (TypeError, ValueError):
        return dict(vacio)
    cache_key = (anio, mes)
    now = _time.time()
    cached = _PROD_TEJ_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _PROD_TEJ_TTL_SECS:
        return cached[1]
    d1 = f"{anio:04d}-{mes:02d}-01"
    ny, nm = (anio + 1, 1) if mes == 12 else (anio, mes + 1)
    d2 = f"{ny:04d}-{nm:02d}-01"
    sql = f"""
        SELECT numero,
               CONVERT(varchar(10), fecha_cierre, 23) AS dia,
               ISNULL(cantidad_fabricada, 0)          AS kg,
               descripcion
          FROM orden_fabricacion
         WHERE id_bodega = 52
           AND indicador_hoja = 1
           AND estado_produccion = 5
           AND fecha_cierre >= '{d1}' AND fecha_cierre < '{d2}'
         ORDER BY fecha_cierre
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=2000)
    except Exception:  # noqa: BLE001 -- fail-soft
        return dict(vacio)
    if not rows:
        return dict(vacio)
    ofs = []
    agg: dict = {}
    total = 0.0
    for r in rows:
        kg = float(r.get("kg") or 0.0)
        desc = str(r.get("descripcion") or "")
        cod, label, es_intela = _clasificar_tejedor(desc)
        ofs.append({
            "numero": str(r.get("numero") or "").strip(),
            "dia": str(r.get("dia") or "")[:10],
            "kg": round(kg, 2),
            "descripcion": desc.strip(),
            "cod": cod,
            "label": label,
            "es_intela": es_intela,
        })
        k = cod or ("?" + label)
        a = agg.setdefault(k, {"cod": cod, "label": label,
                               "es_intela": es_intela, "ofs": 0, "kg": 0.0})
        a["ofs"] += 1
        a["kg"] += kg
        total += kg
    por_tejedor = sorted(agg.values(), key=lambda x: -x["kg"])
    for a in por_tejedor:
        a["kg"] = round(a["kg"], 2)
    out = {"disponible": True, "anio": anio, "mes": mes,
           "ofs": ofs, "por_tejedor": por_tejedor, "total_kg": round(total, 2)}
    _PROD_TEJ_CACHE[cache_key] = (now, out)
    return out


def ingresos_fabricacion_mes(anio: int, mes: int, id_bodega: int = 52) -> dict:
    """INGRESOS DE FABRICACIÓN (documento IFT) del mes a una bodega.

    ⭐ TMT 2026-08-19 (Andrés, vía la dueña: *"existe una IFT, ingreso de orden
    de fabricación, y ese es un mejor documento"*). Tenía razón, y el dato lo
    probó: la orden NO es el hecho económico, la ENTREGA sí.

    Mirando el acumulado de la orden (`cantidad_fabricada`) pasaban dos cosas
    malas: (a) no hay fecha del ingreso, sólo la de la orden, así que todo lo
    que un maquilero entrega este mes contra una orden abierta el mes pasado se
    imputaba al mes pasado; (b) el número cambia hasta que la orden cierra. Con
    eso, agosto/2026 cargaba 3.411,35 kg cuando por IFT habían entrado
    17.416,85 — Ponce entregó 8 veces en agosto, TODAS contra órdenes de julio,
    y quedaba en cero.

    Cada IFT es un ingreso consumado: fecha propia, kilos propios, ligado a la
    orden (de ahí sale el tejedor) y a la bodega destino. Verificado sobre los
    112.965 IFT de Asinfo: ninguno sin orden, ninguno con cantidad negativa.
    Los estados son 0 (anulado), 4 y 5; se toma **sólo el 5** (procesado) — los
    44 en estado 4 son todos de máquinas propias, nunca de tercerizados.

    Devuelve (fail-soft, misma forma que `produccion_tejeduria_mes` para que la
    tab los trate igual):
        {disponible, anio, mes,
         ofs:[{numero (IFT), oft, dia, kg, descripcion, cod, label, es_intela}],
         por_tejedor, total_kg}
    """
    vacio = {"disponible": False, "anio": anio, "mes": mes,
             "ofs": [], "por_tejedor": [], "total_kg": 0.0}
    try:
        anio = int(anio)
        mes = int(mes)
        id_bodega = int(id_bodega)
    except (TypeError, ValueError):
        return dict(vacio)
    cache_key = ("ift", anio, mes, id_bodega)
    now = _time.time()
    cached = _PROD_TEJ_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _PROD_TEJ_TTL_SECS:
        return cached[1]
    d1 = f"{anio:04d}-{mes:02d}-01"
    ny, nm = (anio + 1, 1) if mes == 12 else (anio, mes + 1)
    d2 = f"{ny:04d}-{nm:02d}-01"
    sql = f"""
        SELECT m.numero                              AS ift,
               CONVERT(varchar(10), m.fecha, 23)     AS dia,
               o.numero                              AS oft,
               SUM(d.cantidad)                       AS kg,
               MAX(o.descripcion)                    AS descripcion
          FROM movimiento_inventario m
          JOIN orden_fabricacion o
            ON o.id_orden_fabricacion = m.id_orden_fabricacion
          JOIN detalle_movimiento_inventario d
            ON d.id_movimiento_inventario = m.id_movimiento_inventario
         WHERE m.numero LIKE 'IFT-%'
           AND m.estado = 5
           AND d.id_bodega_destino = {id_bodega}
           AND m.fecha >= '{d1}' AND m.fecha < '{d2}'
         GROUP BY m.numero, m.fecha, o.numero
        HAVING SUM(d.cantidad) > 0
         ORDER BY m.fecha
    """
    # TMT 2026-08-30 — `fetch_dataset` pelado devuelve [] tanto para "Metabase
    # caído" como para "no hubo IFT", y acá se cacheaba `disponible=True` con
    # total 0 los 5 minutos completos: los tercerizados desaparecían de la
    # pantalla de Tejeduría, INTELA absorbía el ingreso de bodega 52 entero y
    # el barrido no creaba el pasivo del maquilero. El comentario de abajo
    # prometía distinguir los dos casos; ahora el código lo hace de verdad.
    try:
        rows, _ok_ift = metabase_client.fetch_dataset_estado(
            2, sql, max_results=5000)
    except Exception:  # noqa: BLE001 -- fail-soft
        return dict(vacio)
    if not _ok_ift:
        # Fracaso ≠ dato: se cachea el vacío (disponible=False) con la
        # ventana corta de 30 s, como todo lo demás.
        _cache_put(_PROD_TEJ_CACHE, cache_key, dict(vacio), False,
                   _PROD_TEJ_TTL_SECS)
        return dict(vacio)
    ofs = []
    agg: dict = {}
    total = 0.0
    for r in rows or []:
        kg = float(r.get("kg") or 0.0)
        desc = str(r.get("descripcion") or "")
        cod, label, es_intela = _clasificar_tejedor(desc)
        ofs.append({
            "numero": str(r.get("ift") or "").strip(),
            "oft": str(r.get("oft") or "").strip(),
            "dia": str(r.get("dia") or "")[:10],
            "kg": round(kg, 2),
            "descripcion": desc.strip(),
            "cod": cod,
            "label": label,
            "es_intela": es_intela,
        })
        k = cod or ("?" + label)
        a = agg.setdefault(k, {"cod": cod, "label": label,
                               "es_intela": es_intela, "ofs": 0, "kg": 0.0})
        a["ofs"] += 1
        a["kg"] += kg
        total += kg
    por_tejedor = sorted(agg.values(), key=lambda x: -x["kg"])
    for a in por_tejedor:
        a["kg"] = round(a["kg"], 2)
    # `disponible=True` aunque no haya filas: "Asinfo contestó y no hubo
    # ingresos" es distinto de "Asinfo no contestó" (el gotcha del caché del
    # 29/07 — ver la skill). Distinguirlo de verdad es el `_ok_ift` de arriba.
    out = {"disponible": True, "anio": anio, "mes": mes,
           "ofs": ofs, "por_tejedor": por_tejedor, "total_kg": round(total, 2)}
    _cache_put(_PROD_TEJ_CACHE, cache_key, out, True, _PROD_TEJ_TTL_SECS)
    return out


def produccion_tejeduria_rango(d1: str, d2: str) -> dict:
    """Igual que `produccion_tejeduria_mes` pero sobre un RANGO de fechas de
    cierre [d1, d2), agregado por tejedor (sin el detalle de OFs).

    TMT 2026-07-26 (pedido dueña: "sí, por proveedor"): el tope de la carga
    automática pasó de mensual a ACUMULADO por proveedor. El mes solo no sirve
    porque el maquilero factura con desfase y a caballo de dos meses: en julio
    el dBase le facturó a Ponce 11.415,95 kg contra 8.817,00 que Asinfo cerró,
    y a Reyes 5.210,78 contra 3.082,53.

    UNA sola query para todo el rango (no N llamadas mes a mes). Fail-soft
    igual que el resto del módulo. Cache propio con el mismo TTL.
    """
    vacio = {"disponible": False, "desde": d1, "hasta": d2,
             "por_tejedor": [], "total_kg": 0.0}
    if not (_FECHA_ISO_RE.fullmatch(str(d1) or "")
            and _FECHA_ISO_RE.fullmatch(str(d2) or "")):
        return dict(vacio)
    cache_key = (str(d1), str(d2))
    now = _time.time()
    cached = _PROD_TEJ_RANGO_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _PROD_TEJ_TTL_SECS:
        return cached[1]
    sql = f"""
        SELECT ISNULL(cantidad_fabricada, 0) AS kg, descripcion
          FROM orden_fabricacion
         WHERE id_bodega = 52
           AND indicador_hoja = 1
           AND estado_produccion = 5
           AND fecha_cierre >= '{d1}' AND fecha_cierre < '{d2}'
    """
    try:
        rows = metabase_client.fetch_dataset(2, sql, max_results=20000)
    except Exception:  # noqa: BLE001 -- fail-soft
        return dict(vacio)
    if not rows:
        return dict(vacio)
    agg: dict = {}
    total = 0.0
    for r in rows:
        kg = float(r.get("kg") or 0.0)
        cod, label, es_intela = _clasificar_tejedor(str(r.get("descripcion") or ""))
        k = cod or ("?" + label)
        a = agg.setdefault(k, {"cod": cod, "label": label,
                               "es_intela": es_intela, "ofs": 0, "kg": 0.0})
        a["ofs"] += 1
        a["kg"] += kg
        total += kg
    por_tejedor = sorted(agg.values(), key=lambda x: -x["kg"])
    for a in por_tejedor:
        a["kg"] = round(a["kg"], 2)
    out = {"disponible": True, "desde": d1, "hasta": d2,
           "por_tejedor": por_tejedor, "total_kg": round(total, 2)}
    _PROD_TEJ_RANGO_CACHE[cache_key] = (now, out)
    return out


def reset_prod_tejeduria_cache() -> None:
    """Vaciar el cache de produccion_tejeduria_mes/_rango (tests / deploy)."""
    _PROD_TEJ_CACHE.clear()
    _PROD_TEJ_RANGO_CACHE.clear()


# ---------------------------------------------------------------------------
# COMPRAS LOCALES de hilo (TMT 2026-07-30, dueña: "la fábrica importa hilo pero
# también hace compras locales; en Ingreso de hilado hay que sumarlas").
#
# En Asinfo una compra local es una `factura_proveedor` que NO tiene fila en
# `factura_proveedor_importacion` (numero CMTR-… en vez de IM-…) y cuya
# recepción entra a la BODEGA 51, igual que una importación. Los proveedores
# reales son HY (Hiltexpoy) y EP (El Peral), los dos con RUC.
#
# ⚠ Hay que partir de la FACTURA, no de la recepción: una factura puede tener
#   N recepciones parciales, así que mirando `recepcion_proveedor` sola aparecen
#   recepciones de AC/AART "sin importación" que SÍ son importaciones.
#
# El TOTAL de Asinfo viene SIN IVA y es sólo referencial (dueña: "asinfo nunca
# nos importa en plata") — la plata sale del tarifario, ver modules/compras_locales.
# ---------------------------------------------------------------------------
_LOCALES_TTL_SECS = 300  # 5 minutos
_LOCALES_CACHE: dict = {}

BODEGA_HILO = 51


def compras_locales_asinfo(limite: int = 200) -> list[dict]:
    """Compras LOCALES de hilo del ERP (factura sin importación, bodega 51).

    Cada fila:
        fp_numero       — número de la factura de proveedor en Asinfo (CMTR-…)
        fecha           — fecha de la factura (YYYY-MM-DD)
        fecha_recepcion — fecha de la 1ª recepción (YYYY-MM-DD o None)
        recibida        — bool
        bod             — documento de recepción (BOD-…)
        numero_factura  — nº SRI completo del proveedor ('001-002-000037649')
        fact_num        — int con el nº de factura sin ceros ni guiones (37649)
        total_asinfo    — total del ERP, SIN IVA (REFERENCIAL)
        proveedor       — razón comercial/fiscal
        ruc             — empresa.codigo (para locales es el RUC)
        producto        — código de producto de la recepción
        n_productos     — cuántos productos distintos trae la factura
        kg              — kg ingresados a la bodega 51
        nota            — descripción libre de la factura

    Returns:
        Lista ordenada por factura más reciente primero. [] si Metabase no está
        configurado o falla (fail-soft — la pantalla nunca se rompe por esto).
    """
    import time as _time
    cache_key = f"l{int(limite)}"
    now = _time.time()
    cached = _LOCALES_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _LOCALES_TTL_SECS:
        return cached[1]

    # JOIN + GROUP BY, NO un OUTER APPLY correlacionado: la primera versión
    # usaba `OUTER APPLY` y con TOP 200 se pasaba del timeout de Metabase (20 s)
    # → `fetch_dataset` devuelve [] fail-soft y la pantalla quedaba SIN las
    # locales, sin un solo error a la vista. Con TOP 3 andaba, así que el bug se
    # escondía en cualquier prueba chica. Así tarda ~1,1 s.
    #
    # El piso de 24 meses es parte del arreglo: sin él, SQL Server tiene que
    # recorrer la tabla entera de facturas para juntar TOP N.
    sql = f"""
        SELECT TOP {int(limite)}
               fp.numero                                        AS fp_numero,
               CONVERT(varchar, fp.fecha, 23)                   AS fecha,
               fp.numero_factura                                AS numero_factura,
               fp.total                                         AS total_asinfo,
               COALESCE(e.nombre_comercial, e.nombre_fiscal, '') AS proveedor,
               e.codigo                                         AS ruc,
               fp.descripcion                                   AS nota,
               CONVERT(varchar, MIN(rp.fecha), 23)              AS fecha_recepcion,
               MIN(rp.numero)                                   AS bod,
               SUM(d.cantidad)                                  AS kg,
               MIN(p.codigo)                                    AS producto,
               COUNT(DISTINCT p.codigo)                         AS n_productos
          FROM factura_proveedor fp
          LEFT JOIN factura_proveedor_importacion fpi
                 ON fpi.id_factura_proveedor = fp.id_factura_proveedor
          LEFT JOIN empresa e ON e.id_empresa = fp.id_empresa
          JOIN detalle_factura_proveedor dfp
               ON dfp.id_factura_proveedor = fp.id_factura_proveedor
          JOIN detalle_recepcion_proveedor d
               ON d.id_detalle_factura_proveedor = dfp.id_detalle_factura_proveedor
          JOIN recepcion_proveedor rp
               ON rp.id_recepcion_proveedor = d.id_recepcion_proveedor
          LEFT JOIN producto p ON p.id_producto = d.id_producto
         WHERE fpi.id_factura_proveedor IS NULL
           AND d.id_bodega = {int(BODEGA_HILO)}
           AND fp.fecha >= DATEADD(month, -24, GETDATE())
         GROUP BY fp.id_factura_proveedor, fp.numero, fp.fecha, fp.numero_factura,
                  fp.total, e.nombre_comercial, e.nombre_fiscal, e.codigo,
                  fp.descripcion
         ORDER BY fp.id_factura_proveedor DESC
    """
    # TMT 2026-08-30 — mismo gotcha que ingresos_fabricacion_mes: un timeout
    # dejaba [] cacheado 5 min como si "no hubiera locales". Con eso el
    # promedio ponderado del hilado perdía las dos patas juntas (kg y US$),
    # ningún guard saltaba, y el $/kg se recalculaba import-only revaluando
    # todo el stock en silencio. El fracaso ahora vence a los 30 s.
    rows, _ok_loc = metabase_client.fetch_dataset_estado(
        2, sql, max_results=int(limite))
    out = []
    for r in rows:
        try:
            frec = str(r.get("fecha_recepcion") or "").strip() or None
            nf = str(r.get("numero_factura") or "").strip()
            out.append({
                "fp_numero": str(r.get("fp_numero") or "").strip(),
                "fecha": str(r.get("fecha") or "").strip() or None,
                "fecha_recepcion": frec,
                "recibida": frec is not None,
                "bod": str(r.get("bod") or "").strip(),
                "numero_factura": nf,
                "fact_num": numero_de_factura(nf),
                "total_asinfo": float(r.get("total_asinfo") or 0),
                "proveedor": str(r.get("proveedor") or "").strip(),
                "ruc": str(r.get("ruc") or "").strip(),
                "producto": str(r.get("producto") or "").strip(),
                "n_productos": int(r.get("n_productos") or 0),
                "kg": float(r.get("kg") or 0),
                "nota": str(r.get("nota") or "").strip(),
            })
        except (TypeError, ValueError):
            continue
    _cache_put(_LOCALES_CACHE, cache_key, out, _ok_loc, _LOCALES_TTL_SECS)
    return out


def numero_de_factura(numero_factura: str) -> int | None:
    """'001-002-000037649' → 37649. El nº que el dBase escribe en el concepto.

    Toma el ÚLTIMO segmento (el secuencial) y le saca los ceros a la izquierda.
    Si no viene con guiones, usa todos los dígitos. None si no hay ninguno.
    """
    s = str(numero_factura or "").strip()
    if not s:
        return None
    ult = s.split("-")[-1]
    digitos = "".join(c for c in ult if c.isdigit())
    if not digitos:
        digitos = "".join(c for c in s if c.isdigit())
    if not digitos:
        return None
    try:
        return int(digitos)
    except ValueError:
        return None


def reset_locales_cache() -> None:
    """Vaciar el cache de compras_locales_asinfo (tests / deploy)."""
    _LOCALES_CACHE.clear()


# ---------------------------------------------------------------------------
# Consumo de hilado — réplica del Excel "Consumos de Material - Resumen"
# ---------------------------------------------------------------------------
# El Excel de la dueña (Power Query directo al SQL Server de Asinfo) arma su
# consumo con SUM(detalle_orden_salida_material.cantidad_despachada) por mes y
# material, filtrado a la categoría HILO. Acá se replica esa consulta por el
# puente Metabase (DB 2). Decisiones tomadas con la dueña el 27/08/2026:
#   - Igual al Excel: consumo BRUTO (no se resta cantidad_devuelta) y sin
#     filtrar estado del OSM.
#   - Sólo categoría HILO (el slicer del Excel estaba en HILO).
#   - "Material" = producto.nombre_subcategoria_producto (22/1, 20/1 JAS 8%,
#     100/36 POLIESTER, …), el mismo nivel de fila que usa el pivot del Excel.
# El saldo actual NO copia la subconsulta del Excel (max(fecha_creacion) por
# producto, que pisa bodegas): usa el patrón verificado de stock_asinfo() —
# último snapshot por (producto, bodega) con ROW_NUMBER y saldo > 0.


def consumo_hilado_mensual(anio: int) -> tuple[list[dict], bool]:
    """kg de hilado despachados (OSM) por mes y material, del año pedido.

    Devuelve `(filas, ok)`:
        filas — [{mes, material, kg, productos}], mes 1-12.
        ok    — False si Metabase no contestó (distinto de "sin filas").

    `productos` (COUNT DISTINCT de productos con despacho ese mes) sirve
    para reproducir el "Promedio Mensual" del pivot del Excel, que es
    AVG(cantidad_mes) sobre las filas producto×mes — o sea
    total ÷ Σ productos-con-despacho-por-mes (verificado en vivo:
    22/1 = 733.954 ÷ 13 = 56.458, igual a la hoja).
    """
    try:
        anio = int(anio)
    except (TypeError, ValueError):
        return [], False
    if not (2000 <= anio <= 2100):
        return [], False
    sql = f"""
SELECT MONTH(osm.fecha)                    AS mes,
       pr.nombre_subcategoria_producto     AS material,
       SUM(dosm.cantidad_despachada)       AS kg,
       COUNT(DISTINCT dosm.id_producto)    AS productos
  FROM orden_salida_material osm
  JOIN detalle_orden_salida_material dosm
    ON dosm.id_orden_salida_material = osm.id_orden_salida_material
  JOIN producto pr ON pr.id_producto = dosm.id_producto
 WHERE pr.nombre_categoria_producto = 'HILO'
   AND YEAR(osm.fecha) = {anio}
 GROUP BY MONTH(osm.fecha), pr.nombre_subcategoria_producto
"""
    try:
        rows, ok = metabase_client.fetch_dataset_estado(2, sql, max_results=5000)
    except Exception as e:  # noqa: BLE001 — fail-soft como todo el bridge
        _LOG.warning("consumo_hilado_mensual falló: %s", e)
        return [], False
    out = []
    for r in rows or []:
        try:
            out.append({
                "mes": int(r.get("mes")),
                "material": str(r.get("material") or "(sin subcategoría)").strip(),
                "kg": float(r.get("kg") or 0),
                "productos": int(r.get("productos") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out, bool(ok)


def consumo_hilado_saldos() -> tuple[list[dict], bool]:
    """Saldo actual de HILO en inventario por material (todas las bodegas).

    Último snapshot por (producto, bodega) de `saldo_producto`, sólo saldos
    positivos — mismo patrón verificado al centavo que usa stock_asinfo().
    Devuelve `(filas, ok)` con filas = [{material, kg}] orden kg DESC.
    """
    sql = """
WITH ult AS (
    SELECT id_producto, id_bodega, saldo,
           ROW_NUMBER() OVER (
               PARTITION BY id_producto, id_bodega
               ORDER BY fecha DESC, id_saldo_producto DESC
           ) AS rn
      FROM saldo_producto
)
SELECT pr.nombre_subcategoria_producto AS material,
       SUM(u.saldo)                    AS kg
  FROM ult u
  JOIN producto pr ON pr.id_producto = u.id_producto
 WHERE u.rn = 1 AND u.saldo > 0
   AND pr.nombre_categoria_producto = 'HILO'
 GROUP BY pr.nombre_subcategoria_producto
 ORDER BY SUM(u.saldo) DESC
"""
    try:
        rows, ok = metabase_client.fetch_dataset_estado(2, sql, max_results=2000)
    except Exception as e:  # noqa: BLE001 — fail-soft como todo el bridge
        _LOG.warning("consumo_hilado_saldos falló: %s", e)
        return [], False
    out = []
    for r in rows or []:
        try:
            out.append({
                "material": str(r.get("material") or "(sin subcategoría)").strip(),
                "kg": float(r.get("kg") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out, bool(ok)
