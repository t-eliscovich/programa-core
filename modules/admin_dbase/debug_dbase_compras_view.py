"""/admin/debug-dbase-compras — buscar una compra en el COMPRAS.DBF del dBase.

TMT 2026-07-31 (dueña): *"fijate si dBase lo tiene, si no no podemos seguir
tocando"*.

Contexto: la importación AC 32 quedó cargada en el programa a 2,394 US$/kg
cuando la banda del mes es 2,7 – 3,4. La pregunta es si al dBase —que es la
fuente contable de verdad— le figura el costo COMPLETO de esa importación. Si el
dBase tiene más plata que el programa, falta cargarla; si tiene lo mismo, el
número es ése y no hay nada que corregir.

SOLO LECTURA sobre el tarball que ya usa /admin/dbase-compare (`_leer`), sin
importar ni tocar nada. Si el tarball no está subido, lo dice y no inventa.

    ?prov=AC&concepto=32     — las filas de COMPRAS.DBF de ese proveedor/concepto
    ?prov=AC&desde=2026-07-01 — todas las de un proveedor desde una fecha
    ?tipo=H                   — filtrar por tipo (H hilado, K tejido, Q químicos…)

El CONCEPTO del dBase es texto libre ("32 SALDO", "32 CAE"), así que el match es
por PREFIJO numérico, igual que el parser de anticipos del programa.
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, Response, request

from auth import requiere_login, requiere_permiso

bp = Blueprint(
    "admin_debug_dbase_compras",
    __name__,
    url_prefix="/admin/debug-dbase-compras",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROV_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")


def _json(payload, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, indent=2, default=str, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )


def _num_prefijo(txt: str):
    """'32 SALDO' → 32 · 'CAE 32' → None. Mismo criterio que el parser de
    anticipos: el número que IDENTIFICA la importación va adelante."""
    m = re.match(r"\s*(\d{1,6})", str(txt or ""))
    return int(m.group(1)) if m else None


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def run():
    from modules.admin_dbase.dbase_compare_view import EXTRACT_DIR, _f, _leer

    if not (EXTRACT_DIR / "COMPRAS.DBF").exists():
        return _json({
            "ok": False,
            "error": "No hay COMPRAS.DBF extraído. Subí el tarball en "
                     "/admin/dbase-compare y volvé.",
            "carpeta": str(EXTRACT_DIR),
        })

    prov = (request.args.get("prov") or "").strip().upper()
    if prov and not _PROV_RE.match(prov):
        return _json({"ok": False, "error": "prov invalido"}, 400)
    tipo = (request.args.get("tipo") or "").strip().upper()[:1]
    desde = (request.args.get("desde") or "").strip()
    if desde and not _DATE_RE.match(desde):
        return _json({"ok": False, "error": "desde invalido (YYYY-MM-DD)"}, 400)
    concepto = (request.args.get("concepto") or "").strip()
    ref = _num_prefijo(concepto) if concepto else None

    filas = []
    for r in _leer("COMPRAS.DBF"):
        _prov = (r.get("PROV") or r.get("CODIGO") or "").strip().upper()
        if prov and _prov != prov:
            continue
        _tipo = (r.get("TIPO") or "").strip().upper()
        if tipo and _tipo != tipo:
            continue
        _fecha = r.get("FECHA")
        if desde and (not _fecha or str(_fecha)[:10] < desde):
            continue
        _con = (r.get("CONCEPTO") or "").strip()
        if ref is not None and _num_prefijo(_con) != ref:
            continue
        kg = _f(r.get("KG"))
        imp = _f(r.get("IMPORTE"))
        filas.append({
            "fecha": str(_fecha)[:10] if _fecha else None,
            "prov": _prov,
            "tipo": _tipo,
            "concepto": _con,
            "ref": _num_prefijo(_con),
            "kg": round(kg, 2),
            "importe": round(imp, 2),
            "usd_kg": (round(imp / kg, 4) if kg else None),
            "numero": (r.get("NUMERO") or r.get("NUM") or None),
        })

    filas.sort(key=lambda f: (f["fecha"] or "", f["ref"] or 0))
    tot_kg = sum(f["kg"] for f in filas)
    tot_us = sum(f["importe"] for f in filas)
    return _json({
        "ok": True,
        "filtro": {"prov": prov or None, "concepto": concepto or None,
                   "ref": ref, "tipo": tipo or None, "desde": desde or None},
        "n": len(filas),
        "total": {
            "kg": round(tot_kg, 2),
            "importe": round(tot_us, 2),
            "usd_kg": (round(tot_us / tot_kg, 4) if tot_kg else None),
        },
        "filas": filas,
    })
