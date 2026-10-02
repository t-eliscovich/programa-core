"""Las consultas del aviso del portal: a quién le va, y qué pasó con cada uno.

La lista de clientes con saldo sale de `informes.queries.
estado_cuenta_clientes_saldos` —la MISMA que imprime los estados de cuenta por
grupos— para que "a todos los que tienen saldo" signifique lo mismo acá que en
el resto del programa.

TMT 14/09/2026: además de tener saldo, tiene que haber comprado en los
últimos `MESES_ULTIMA_COMPRA` meses (`_ultima_compra`) — a un cliente que
dejó de comprar hace años no le mandamos un aviso de un portal nuevo, le
mandaríamos un cobro disfrazado.

El correo de cada uno se resuelve en el MISMO orden que el portal cuando manda
el código de 6 números (`modules/portal/acceso.pedir_codigo`): el que el
cliente confirmó en el portal → el cargado a mano en la ficha → el del
catálogo de Asinfo. Si se le manda el aviso a un correo y el código de entrada
a otro, el cliente no entiende nada.
"""
from __future__ import annotations

from datetime import date

import db

#: La clave de `scintela.nota_config` que dice si el envío a clientes está
#: prendido. Nace en '0' (mig 0242): "hasta no testear no mandamos nada".
CLAVE_INTERRUPTOR = "portal_aviso_a_clientes"

#: Sólo entran al aviso los clientes que compraron (facturas no anuladas)
#: dentro de esta cantidad de meses. TMT 14/09/2026.
MESES_ULTIMA_COMPRA = 6


def a_clientes_encendido() -> bool:
    try:
        r = db.fetch_one("SELECT valor FROM scintela.nota_config WHERE clave = %s",
                         (CLAVE_INTERRUPTOR,))
        return bool(r) and (r.get("valor") or "").strip() == "1"
    except Exception:  # noqa: BLE001 -- sin la fila, apagado
        return False


def encender_a_clientes(prendido: bool) -> None:
    db.execute(
        "INSERT INTO scintela.nota_config (clave, valor) VALUES (%s, %s) "
        "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
        (CLAVE_INTERRUPTOR, "1" if prendido else "0"))


#: La clave que prende/apaga el RECORDATORIO automático (cada 2 semanas,
#: TMT 14/09/2026). Nace apagada — mismo patrón que CLAVE_INTERRUPTOR.
CLAVE_INTERRUPTOR_EC = "portal_aviso_ec_auto"

#: La fecha (ISO, YYYY-MM-DD) de la PRÓXIMA corrida del recordatorio. Se
#: guarda como un puntero en vez de calcularse por aritmética de semana
#: par/impar: cada corrida (mande o se salte por feriado) empuja este
#: puntero 14 días, y como 14 es múltiplo de 7 siempre cae lunes.
CLAVE_PROXIMA_EC = "portal_aviso_ec_proxima"


def ec_auto_encendido() -> bool:
    try:
        r = db.fetch_one("SELECT valor FROM scintela.nota_config WHERE clave = %s",
                         (CLAVE_INTERRUPTOR_EC,))
        return bool(r) and (r.get("valor") or "").strip() == "1"
    except Exception:  # noqa: BLE001 -- sin la fila, apagado
        return False


def encender_ec_auto(prendido: bool) -> None:
    db.execute(
        "INSERT INTO scintela.nota_config (clave, valor) VALUES (%s, %s) "
        "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
        (CLAVE_INTERRUPTOR_EC, "1" if prendido else "0"))


def proxima_corrida_ec() -> date | None:
    try:
        r = db.fetch_one("SELECT valor FROM scintela.nota_config WHERE clave = %s",
                         (CLAVE_PROXIMA_EC,))
        v = ((r or {}).get("valor") or "").strip()
        return date.fromisoformat(v) if v else None
    except Exception:  # noqa: BLE001 -- sin fecha programada
        return None


def fijar_proxima_corrida_ec(d: date) -> None:
    db.execute(
        "INSERT INTO scintela.nota_config (clave, valor) VALUES (%s, %s) "
        "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
        (CLAVE_PROXIMA_EC, d.isoformat()))


def reclamar_corrida_ec(prevista: date, siguiente: date) -> bool:
    """Toma la corrida del recordatorio de una sola vez: mueve el puntero de
    `prevista` a `siguiente` SÓLO si todavía dice `prevista`.

    TMT 02/10/2026: el recordatorio salió DOS veces a todos los clientes
    (09:33 y 09:36 EC). El servidor corre más de un proceso, cada uno con su
    propio freno en memoria; los dos vieron la fecha vencida y arrancaron,
    porque el puntero recién se corría al TERMINAR de mandar. Ahora se corre
    ANTES, con un UPDATE condicional: la base deja ganar a uno solo, y el
    que pierde no manda nada.
    """
    n = db.execute(
        "UPDATE scintela.nota_config SET valor = %s "
        " WHERE clave = %s AND TRIM(valor) = %s",
        (siguiente.isoformat(), CLAVE_PROXIMA_EC, prevista.isoformat()))
    return n == 1


#: Un cliente no recibe dos avisos (de ningún tipo) en esta cantidad de días.
#: TMT 02/10/2026: *"asegurate sí o sí y restringí mails"*. El recordatorio
#: es cada 14 días, así que 7 no frena nada legítimo.
DIAS_SIN_REPETIR = 7

#: El candado del envío a clientes: en `nota_config`, el momento (UTC ISO)
#: en que alguien lo tomó, o '' si está libre. Uno solo manda a la vez.
CLAVE_ENVIO_EN_CURSO = "portal_aviso_envio_en_curso"

#: Si un envío se muere a la mitad sin soltar el candado, a los 30 min se
#: puede volver a tomar (435 mails tardan ~4 min).
_CANDADO_VENCE_MIN = 30


def tomar_envio() -> bool:
    """True si este proceso tomó el candado del envío a clientes."""
    from datetime import UTC, datetime, timedelta
    ahora = datetime.now(UTC)
    vencido = (ahora - timedelta(minutes=_CANDADO_VENCE_MIN)).strftime("%Y-%m-%dT%H:%M:%S")
    r = db.execute_returning(
        "INSERT INTO scintela.nota_config (clave, valor) VALUES (%s, %s) "
        "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor "
        " WHERE TRIM(COALESCE(scintela.nota_config.valor, '')) = '' "
        "    OR scintela.nota_config.valor < %s "
        "RETURNING clave",
        (CLAVE_ENVIO_EN_CURSO, ahora.strftime("%Y-%m-%dT%H:%M:%S"), vencido))
    return bool(r)


def soltar_envio() -> None:
    db.execute("UPDATE scintela.nota_config SET valor = '' WHERE clave = %s",
               (CLAVE_ENVIO_EN_CURSO,))


def ya_avisado(codigo_cli: str) -> bool:
    """¿Este cliente ya recibió un aviso de verdad en los últimos
    `DIAS_SIN_REPETIR` días? Se pregunta justo antes de cada mail."""
    r = db.fetch_one(
        "SELECT 1 AS si FROM scintela.portal_aviso "
        " WHERE UPPER(TRIM(codigo_cli)) = UPPER(TRIM(%s)) "
        "   AND tipo = 'cliente' AND ok "
        "   AND enviado_en > now() - make_interval(days => %s) "
        " LIMIT 1",
        (codigo_cli or "", DIAS_SIN_REPETIR))
    return bool(r)


def lista() -> list[dict]:
    """Una fila por cliente con saldo a favor nuestro (saldo > 0) Y que
    compró (factura no anulada) en los últimos `MESES_ULTIMA_COMPRA` meses.

    Cada fila trae: codigo_cli, nombre, vend, saldo, vencido, correo (el
    resuelto), de_donde ('portal' | 'ficha' | 'asinfo' | ''), entro (bool: ya
    eligió clave en el portal), ultimo_aviso (timestamptz | None),
    ultimo_aviso_ok (bool | None), ultima_compra (date), y del último aviso:
    con_marca (salió con seguimiento), abierto_en, clic_en (mig 0257).
    """
    from modules.informes.queries import estado_cuenta_clientes_saldos

    con_saldo = [f for f in estado_cuenta_clientes_saldos()
                 if (f.get("saldo") or 0) > 0]
    if not con_saldo:
        return []
    codigos = sorted({(f["codigo_cli"] or "").strip().upper() for f in con_saldo})
    extra = {r["codigo_cli"]: r for r in _correos_y_portal(codigos)}
    compraron = _ultima_compra(codigos)
    filas = []
    for f in con_saldo:
        cod = (f["codigo_cli"] or "").strip().upper()
        ultima_compra = compraron.get(cod)
        if not ultima_compra:
            continue  # no compró en los últimos MESES_ULTIMA_COMPRA meses
        e = extra.get(cod) or {}
        correo, de_donde = _resolver(e)
        filas.append({
            "codigo_cli": cod,
            "nombre": f.get("nombre") or cod,
            "vend": f.get("vend") or "",
            "saldo": f.get("saldo") or 0,
            "vencido": f.get("vencido") or 0,
            "correo": correo,
            "de_donde": de_donde,
            "entro": bool(e.get("eligio_clave")),
            "ultimo_aviso": e.get("ultimo_aviso"),
            "ultimo_aviso_ok": e.get("ultimo_aviso_ok"),
            "con_marca": bool(e.get("con_marca")),
            "abierto_en": e.get("abierto_en"),
            "clic_en": e.get("clic_en"),
            "ultima_compra": ultima_compra,
        })
    filas.sort(key=lambda x: x["codigo_cli"])
    return filas


def _ultima_compra(codigos: list[str]) -> dict:
    """`{codigo_cli: fecha}` de los que tienen al menos una factura NO
    anulada (stat fuera de X/T, igual criterio que `facturas.queries.
    STATS_ANULADAS`) con fecha dentro de `MESES_ULTIMA_COMPRA` meses.

    El filtro de fecha va en el HAVING, en Postgres: evita reinventar la
    aritmética de "hace 6 meses" en Python."""
    return {
        r["codigo_cli"]: r["ultima_compra"]
        for r in db.fetch_all(
            """
            SELECT UPPER(TRIM(codigo_cli)) AS codigo_cli, MAX(fecha) AS ultima_compra
              FROM scintela.factura
             WHERE UPPER(TRIM(codigo_cli)) = ANY(%(codigos)s)
               AND (stat IS NULL OR stat NOT IN ('X', 'T'))
             GROUP BY UPPER(TRIM(codigo_cli))
            HAVING MAX(fecha) >= CURRENT_DATE - (%(meses)s || ' months')::interval
            """,
            {"codigos": codigos, "meses": MESES_ULTIMA_COMPRA},
        )
    }


def _resolver(e: dict) -> tuple[str, str]:
    for campo, de_donde in (("mail_portal", "portal"), ("correo_ficha", "ficha"),
                            ("mail_asinfo", "asinfo")):
        v = (e.get(campo) or "").strip()
        if v:
            return v, de_donde
    return "", ""


def _correos_y_portal(codigos: list[str]) -> list[dict]:
    """Los tres correos posibles, el estado en el portal y el último aviso,
    para TODOS los códigos en una consulta."""
    return db.fetch_all(
        """
        WITH c AS (
            SELECT UPPER(TRIM(codigo_cli)) AS codigo_cli,
                   TRIM(correo)            AS correo_ficha,
                   LEFT(regexp_replace(COALESCE(ruc, ''), '\\D', '', 'g'), 10) AS ruc10
              FROM scintela.cliente
             WHERE UPPER(TRIM(codigo_cli)) = ANY(%(codigos)s)
        ),
        ultimo AS (
            SELECT DISTINCT ON (UPPER(TRIM(codigo_cli)))
                   UPPER(TRIM(codigo_cli)) AS codigo_cli, enviado_en, ok,
                   token, abierto_en, clic_en
              FROM scintela.portal_aviso
             WHERE tipo = 'cliente'
             ORDER BY UPPER(TRIM(codigo_cli)), enviado_en DESC
        )
        SELECT c.codigo_cli,
               c.correo_ficha,
               pa.mail                          AS mail_portal,
               pa.clave_hash IS NOT NULL        AS eligio_clave,
               ma.email                         AS mail_asinfo,
               u.enviado_en                     AS ultimo_aviso,
               u.ok                             AS ultimo_aviso_ok,
               u.token IS NOT NULL              AS con_marca,
               u.abierto_en                     AS abierto_en,
               u.clic_en                        AS clic_en
          FROM c
          LEFT JOIN scintela.portal_acceso pa
                 ON UPPER(TRIM(pa.codigo_cli)) = c.codigo_cli
          LEFT JOIN scintela.cliente_mail_asinfo ma
                 ON ma.ruc10 = c.ruc10 AND c.ruc10 <> ''
          LEFT JOIN ultimo u ON u.codigo_cli = c.codigo_cli
        """,
        {"codigos": codigos},
    )


def correo_del_vendedor(vend: str) -> str:
    """El correo del usuario vendedor, para que la respuesta le llegue a él."""
    vend = (vend or "").strip().upper()
    if not vend:
        return ""
    try:
        r = db.fetch_one(
            "SELECT email FROM seguridad.usuario "
            " WHERE UPPER(TRIM(vend)) = %s AND activo ORDER BY id_usuario LIMIT 1",
            (vend,))
        return ((r or {}).get("email") or "").strip()
    except Exception:  # noqa: BLE001 -- sin correo, contesta la casa
        return ""


def anotar(codigo_cli: str, correo: str, tipo: str, ok: bool, motivo: str,
           id_ses: str, quien: str, token: str = "") -> None:
    db.execute(
        "INSERT INTO scintela.portal_aviso "
        "  (codigo_cli, correo, tipo, ok, motivo, id_ses, enviado_por, token) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        ((codigo_cli or "")[:20], (correo or "")[:200], tipo, bool(ok),
         (motivo or "")[:200] or None, (id_ses or "")[:120] or None,
         (quien or "")[:40] or None, (token or "")[:40] or None))


def marcar_apertura(token: str) -> int:
    """La imagen invisible del mail se descargó: el cliente lo abrió (o su
    correo la bajó solo — ver mig 0257). Devuelve las filas tocadas."""
    return db.execute(
        "UPDATE scintela.portal_aviso "
        "   SET abierto_en = COALESCE(abierto_en, now()), aperturas = aperturas + 1 "
        " WHERE token = %s",
        (token,))


def marcar_clic(token: str) -> int:
    """El cliente apretó el botón del mail. Un clic también es una apertura
    (aunque su correo haya bloqueado la imagen)."""
    return db.execute(
        "UPDATE scintela.portal_aviso "
        "   SET clic_en = COALESCE(clic_en, now()), clics = clics + 1, "
        "       abierto_en = COALESCE(abierto_en, now()) "
        " WHERE token = %s",
        (token,))


def resumen_envios(limite: int = 6) -> list[dict]:
    """Cómo le fue a cada envío a clientes, por día (Ecuador): cuántos
    salieron, cuántos se abrieron y cuántos hicieron clic. Sólo cuentan los
    que salieron con marca (desde el 02/10/2026)."""
    return db.fetch_all(
        """
        SELECT (enviado_en AT TIME ZONE 'America/Guayaquil')::date AS dia,
               COUNT(*)                                   AS enviados,
               COUNT(*) FILTER (WHERE token IS NOT NULL)  AS con_marca,
               COUNT(abierto_en)                          AS abrieron,
               COUNT(clic_en)                             AS clic
          FROM scintela.portal_aviso
         WHERE tipo = 'cliente' AND ok
         GROUP BY 1
         ORDER BY 1 DESC
         LIMIT %s
        """,
        (limite,))


def historial(limite: int = 200) -> list[dict]:
    """Los últimos avisos que salieron, del más nuevo al más viejo."""
    return db.fetch_all(
        """
        SELECT a.codigo_cli, COALESCE(c.nombre, '') AS nombre, a.correo, a.tipo,
               a.ok, a.motivo, a.enviado_por, a.enviado_en,
               a.token IS NOT NULL AS con_marca, a.abierto_en, a.clic_en
          FROM scintela.portal_aviso a
          LEFT JOIN scintela.cliente c ON UPPER(TRIM(c.codigo_cli)) = UPPER(TRIM(a.codigo_cli))
         ORDER BY a.enviado_en DESC
         LIMIT %s
        """,
        (limite,))


def vendedores() -> list[dict]:
    """Los vendedores como los ve el cliente en su portal: código, nombre,
    el WhatsApp cargado (mig 0246) y el correo de su usuario (el Reply-To
    del aviso). Sirve para ver de un vistazo a quién le falta qué."""
    return db.fetch_all(
        """
        SELECT v.codigo, COALESCE(v.nombre, '') AS nombre,
               COALESCE(v.whatsapp, '')         AS whatsapp,
               COALESCE((SELECT u.email FROM seguridad.usuario u
                          WHERE UPPER(TRIM(u.vend)) = UPPER(TRIM(v.codigo)) AND u.activo
                          ORDER BY u.id_usuario LIMIT 1), '') AS correo,
               (SELECT COUNT(*) FROM scintela.cliente c
                 WHERE UPPER(TRIM(c.vend)) = UPPER(TRIM(v.codigo))) AS clientes
          FROM scintela.vendedor v
         WHERE v.activo
         ORDER BY v.codigo
        """
    )


def guardar_whatsapp(codigo: str, numero: str, usuario: str) -> int:
    return db.execute(
        """
        UPDATE scintela.vendedor
           SET whatsapp          = NULLIF(%s, ''),
               fecha_actualiza   = CURRENT_TIMESTAMP,
               usuario_actualiza = %s
         WHERE UPPER(TRIM(codigo)) = UPPER(TRIM(%s))
        """,
        ((numero or "").strip()[:20], (usuario or "")[:30], codigo),
    )
