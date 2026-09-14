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


def lista() -> list[dict]:
    """Una fila por cliente con saldo a favor nuestro (saldo > 0) Y que
    compró (factura no anulada) en los últimos `MESES_ULTIMA_COMPRA` meses.

    Cada fila trae: codigo_cli, nombre, vend, saldo, vencido, correo (el
    resuelto), de_donde ('portal' | 'ficha' | 'asinfo' | ''), entro (bool: ya
    eligió clave en el portal), ultimo_aviso (timestamptz | None),
    ultimo_aviso_ok (bool | None), ultima_compra (date).
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
                   UPPER(TRIM(codigo_cli)) AS codigo_cli, enviado_en, ok
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
               u.ok                             AS ultimo_aviso_ok
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
           id_ses: str, quien: str) -> None:
    db.execute(
        "INSERT INTO scintela.portal_aviso "
        "  (codigo_cli, correo, tipo, ok, motivo, id_ses, enviado_por) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ((codigo_cli or "")[:20], (correo or "")[:200], tipo, bool(ok),
         (motivo or "")[:200] or None, (id_ses or "")[:120] or None,
         (quien or "")[:40] or None))


def historial(limite: int = 200) -> list[dict]:
    """Los últimos avisos que salieron, del más nuevo al más viejo."""
    return db.fetch_all(
        """
        SELECT a.codigo_cli, COALESCE(c.nombre, '') AS nombre, a.correo, a.tipo,
               a.ok, a.motivo, a.enviado_por, a.enviado_en
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
