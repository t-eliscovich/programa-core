"""Endpoint /admin/clientes-ficha-asinfo — completa fichas desde el ERP.

TMT 2026-06-10 ("en algún lado tenemos que encontrar esa data"): los clientes
auto-creados al cargar facturas Asinfo quedan sin nombre hasta que llegue un
CLIENTES.DBF fresco — pero la razón social y el RUC YA ESTÁN en Asinfo
(factura electrónica SRI). Este endpoint busca los clientes de PC con nombre
vacío, pide la ficha a Asinfo vía Metabase y la rellena (rellenar-solo, no
pisa nada). GET = dry-run; ?aplicar=1 escribe.
"""
from __future__ import annotations

from flask import Blueprint, Response, request, stream_with_context

from auth import requiere_login, requiere_permiso

bp = Blueprint("clientes_ficha_asinfo", __name__,
               url_prefix="/admin/clientes-ficha-asinfo")


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("usuarios.admin")
def run():
    aplicar = request.args.get("aplicar") in ("1", "true", "on")
    return Response(stream_with_context(_run(aplicar)), mimetype="text/plain")


def _run(aplicar: bool):
    import db
    from modules.asinfo import service as asinfo_svc

    def line(m=""):
        return m.rstrip("\n") + "\n"

    yield line(f"=== Ficha de clientes desde Asinfo — {'APLICAR' if aplicar else 'DRY-RUN'} ===")
    vacios = db.fetch_all(
        """
        SELECT id_cliente, codigo_cli, nombre, ruc
          FROM scintela.cliente
         WHERE COALESCE(NULLIF(TRIM(nombre), ''), 'None') = 'None'
         ORDER BY codigo_cli
        """,
    ) or []
    yield line(f"Clientes en PC sin nombre: {len(vacios)} "
               f"({', '.join(r['codigo_cli'] for r in vacios[:20])})")
    if not vacios:
        yield line("Nada que completar.")
        return

    fichas = {}
    try:
        fichas = asinfo_svc.cliente_ficha([r["codigo_cli"] for r in vacios]) or {}
    except Exception as exc:  # noqa: BLE001
        yield line(f"[ERROR] Asinfo/Metabase no respondió: {exc!r}")
        return
    yield line(f"Asinfo devolvió ficha para: {len(fichas)}")
    yield line("")

    n = 0
    for r in vacios:
        cod = (r["codigo_cli"] or "").strip().upper()
        f = fichas.get(cod)
        if not f:
            yield line(f"  {cod:6} — sin match en Asinfo (queda para el próximo CLIENTES.DBF)")
            continue
        yield line(f"  {cod:6} → {f['nombre'][:45]}  ruc={f.get('ruc') or '-'}")
        if aplicar:
            db.execute(
                """
                UPDATE scintela.cliente
                   SET nombre = %s,
                       ruc = COALESCE(NULLIF(TRIM(ruc), ''), NULLIF(%s, '')),
                       usuario_modifica = 'ficha-asinfo'
                 WHERE id_cliente = %s
                   AND COALESCE(NULLIF(TRIM(nombre), ''), 'None') = 'None'
                """,
                (f["nombre"], f.get("ruc") or "", r["id_cliente"]),
            )
            n += 1
    yield line("")
    if aplicar:
        yield line(f"APLICADO ✓ — {n} clientes actualizados.")
    else:
        yield line("DRY-RUN: no se tocó nada. Agregá ?aplicar=1 para escribir.")
