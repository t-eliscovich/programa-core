"""Todo el historial del automático de importaciones, al buzón de novedades.

TMT 2026-07-30 (dueña): *"si ok las de hoy, faltan las del pasado"*.

La 0141 recuperó sólo las conversiones cuyo `mensaje` ya venía en el formato
corto de hoy ("IM-… · AC · $ … · Se cargó a compras"). Las del 29/07 tenían el
mensaje viejo, largo y con la palabra BAP adentro, así que quedaron afuera.

Esta migración las trae TODAS — conversiones, frenos y errores — armando el
texto desde las COLUMNAS del log con el mismo `autobap.resumen()` que usan la
campanita y la pantalla del automático (una sola fuente, nada re-escrito a mano
y sin plata formateada en SQL).

Se preservan `leido` y `creado_en` originales: lo que ya se había leído no
vuelve a encender el contador. Idempotente por `clave` (las mismas que escribe
`_al_buzon`), así que corre tantas veces como haga falta.

GUARD to_regclass OBLIGATORIO: no-op en CI/test donde scintela.* no existe.
Prints ASCII (consola Windows cp1252).
"""
from __future__ import annotations

_NIVEL = {"conversion": "ok", "freno": "alerta", "error": "error"}


def _clave(fila: dict) -> str | None:
    """La MISMA clave que escribe autobap._al_buzon (anti-repetido)."""
    tipo = fila["tipo"]
    dia = str(fila["creado_en"])[:10]
    if tipo == "conversion":
        return f"importaciones:compra:{fila['id_compra']}" if fila["id_compra"] else None
    if tipo == "freno":
        return f"importaciones:freno:{dia}:{fila['n_anticipos']}"
    return f"importaciones:error:{fila['im_numero']}:{dia}"


def run(conn) -> None:
    cur = conn.cursor()

    cur.execute("SELECT to_regclass('scintela.autobap_log'), to_regclass('scintela.aviso')")
    log_t, aviso_t = cur.fetchone()
    if log_t is None or aviso_t is None:
        print("0142: scintela.autobap_log / aviso ausentes (CI/test) - no-op")
        return

    from modules.importaciones.autobap import resumen

    cur.execute(
        """
        SELECT tipo, im_numero, codigo_prov, n_anticipos, importe, id_compra,
               comprobante, mensaje, leido, creado_en
          FROM scintela.autobap_log
         ORDER BY creado_en ASC, id_autobap_log ASC
        """
    )
    cols = [d[0] for d in cur.description]
    filas = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]

    puestos = 0
    for f in filas:
        clave = _clave(f)
        if not clave:
            continue
        r = resumen({**f, "importe": float(f["importe"] or 0)})
        cur.execute(
            """
            INSERT INTO scintela.aviso
                   (fuente, nivel, titulo, detalle, importe, cantidad, url,
                    clave, leido, creado_en)
            VALUES ('importaciones', %s, %s, %s, %s, %s,
                    '/importaciones/automatico', %s, %s, %s)
            ON CONFLICT (clave) DO NOTHING
            """,
            (_NIVEL.get(f["tipo"], "ok"), r["titulo"][:200], r["detalle"],
             f["importe"], f["n_anticipos"], clave, f["leido"], f["creado_en"]),
        )
        puestos += cur.rowcount or 0

    print(f"0142: {len(filas)} avisos en el historial, {puestos} nuevos al buzon")
