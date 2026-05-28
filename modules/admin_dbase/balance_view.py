"""Drift audit: PC vs Banco Pichincha.

Sube el xlsx del extracto y obtiene:
  - Saldo banco (top del xlsx)
  - Saldo PC libros (último mov en transacciones_bancarias)
  - Pendientes históricos
  - Saldo banco esperado = PC + pendientes
  - DRIFT exacto
  - Drift DESAGREGADO por categoría: IVA/COMISION/SENAE/CHEQUES DEV/Transfer/Otros

TMT 2026-05-28 — dueña: "verificar si el banco nos da igual".
"""

from __future__ import annotations

import io
import logging
import re
from collections import defaultdict
from datetime import datetime

import openpyxl
from flask import Blueprint, Response, render_template, request

import db as _db
from auth import requiere_login, requiere_permiso

_LOG = logging.getLogger("programa_core.admin_balance")

bp = Blueprint(
    "admin_balance",
    __name__,
    url_prefix="/admin/balance-banco",
    template_folder="templates",
)


CATS = [
    ("IVA", re.compile(r"\bIVA\b", re.I)),
    ("COMISION", re.compile(r"COMISION", re.I)),
    ("CHEQUE DEVUELTO", re.compile(r"CHEQUE\s+DEVUELTO|COST\s+CHEQUE", re.I)),
    ("PAGO SENAE", re.compile(r"PAGO\s+SENAE", re.I)),
    ("DEPOSITO", re.compile(r"DEPOSITO", re.I)),
    ("TRANSFERENCIA", re.compile(r"TRANSFERENCIA", re.I)),
    ("INTERES", re.compile(r"INTERES", re.I)),
]


def _categorizar(concepto: str) -> str:
    for cat, rx in CATS:
        if rx.search(concepto or ""):
            return cat
    return "OTROS"


def _parse_fecha(s):
    if s is None:
        return None
    if hasattr(s, "date"):
        return s.date()
    if hasattr(s, "year"):
        return s
    s = str(s).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_xlsx(content: bytes) -> list[dict]:
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active
    headers = [str(ws.cell(1, c).value or "").strip() for c in range(1, ws.max_column + 1)]
    movs = []
    for r in range(2, ws.max_row + 1):
        row = {}
        for ci, h in enumerate(headers, 1):
            row[h] = ws.cell(r, ci).value
        if not row.get("Fecha"):
            continue
        movs.append(row)
    return movs


@bp.route("/", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def balance():
    if request.method == "GET":
        return render_template("admin_dbase/balance.html", resultado=None)

    f = request.files.get("xlsx")
    if not f:
        return render_template("admin_dbase/balance.html",
                               resultado={"error": "Subí el xlsx."})
    no_banco = 10
    try:
        no_banco = int(request.form.get("no_banco") or 10)
    except (TypeError, ValueError):
        pass

    content = f.read()
    if len(content) > 10 * 1024 * 1024:
        return render_template("admin_dbase/balance.html",
                               resultado={"error": "Archivo >10MB."})

    try:
        movs = _parse_xlsx(content)
    except Exception as e:
        _LOG.exception("parse xlsx fail")
        return render_template("admin_dbase/balance.html",
                               resultado={"error": f"Parse: {e}"})

    # Saldo banco top del xlsx (fila con mayor fecha, primer registro)
    movs_con_saldo = [m for m in movs if m.get("Saldo") is not None and m.get("Fecha")]
    if not movs_con_saldo:
        return render_template("admin_dbase/balance.html",
                               resultado={"error": "El xlsx no tiene columna Saldo o Fecha."})
    # Ordenamos por fecha desc, tomamos el primero (running balance más reciente)
    top = sorted(
        movs_con_saldo,
        key=lambda m: (_parse_fecha(m.get("Fecha")) or datetime.min.date(),),
        reverse=True,
    )[0]
    saldo_banco_top = float(top["Saldo"])
    fecha_banco_top = _parse_fecha(top["Fecha"])

    # Saldo PC libros
    row = _db.fetch_one(
        """
        SELECT t.id_transaccion, t.fecha, t.documento, t.importe, t.saldo
        FROM scintela.transacciones_bancarias t
        WHERE t.no_banco=%s ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1
        """,
        (no_banco,),
    )
    saldo_pc = float((row or {}).get("saldo") or 0)
    fecha_pc = (row or {}).get("fecha")

    # Pendientes históricos (banco confirmó, PC no tiene)
    h = _db.fetch_one(
        """
        SELECT COUNT(*) AS n,
          COALESCE(SUM(CASE WHEN COALESCE(tipo,'C')='C' THEN monto ELSE 0 END),0) AS cred,
          COALESCE(SUM(CASE WHEN COALESCE(tipo,'D')='D' THEN monto ELSE 0 END),0) AS deb
        FROM scintela.banco_historicos_pendientes
        WHERE no_banco=%s AND conciliado_en IS NULL
        """,
        (no_banco,),
    ) or {}
    hist_cred = float(h.get("cred") or 0)
    hist_deb = float(h.get("deb") or 0)
    hist_neto = hist_cred - hist_deb

    saldo_esperado = saldo_pc + hist_neto
    drift = round(saldo_banco_top - saldo_esperado, 2)

    # Categorización del extracto
    sum_por_cat: dict[str, dict[str, float]] = defaultdict(lambda: {"cred": 0.0, "deb": 0.0, "n": 0})
    for m in movs:
        try:
            monto = float(m.get("Monto") or 0)
        except (TypeError, ValueError):
            continue
        tipo = str(m.get("Tipo") or "C").upper()
        cat = _categorizar(str(m.get("Concepto") or ""))
        sum_por_cat[cat]["n"] += 1
        if tipo == "C":
            sum_por_cat[cat]["cred"] += monto
        else:
            sum_por_cat[cat]["deb"] += monto

    # Sumar por cat: cuáles PC NO tiene (chequeo simple: cuántas filas del xlsx tienen
    # candidato PC con monto exacto, fecha ±3d, mismo tipo, no conciliado).
    en_pc_por_cat: dict[str, int] = defaultdict(int)
    falta_en_pc_por_cat_monto: dict[str, float] = defaultdict(float)
    for m in movs:
        try:
            monto = float(m.get("Monto") or 0)
        except (TypeError, ValueError):
            continue
        tipo = str(m.get("Tipo") or "C").upper()[:1]
        f_b = _parse_fecha(m.get("Fecha"))
        cat = _categorizar(str(m.get("Concepto") or ""))
        if not f_b:
            continue
        docs_compat = ("DE", "TR", "XX", "NC", "IN", "AC") if tipo == "C" else ("CH", "ND", "DB", "GS", "PA")
        r = _db.fetch_one(
            """
            SELECT 1 FROM scintela.transacciones_bancarias t
            WHERE t.no_banco=%s AND t.fecha BETWEEN %s AND %s
              AND ABS(t.importe - %s) < 0.005
              AND UPPER(TRIM(t.documento)) IN %s
            LIMIT 1
            """,
            (no_banco, f_b, f_b, monto, docs_compat),
        )
        if r:
            en_pc_por_cat[cat] += 1
        else:
            falta_en_pc_por_cat_monto[cat] += monto

    cats_resumen = []
    for cat, vals in sorted(sum_por_cat.items(), key=lambda kv: -abs(kv[1]["cred"] - kv[1]["deb"])):
        n_total = vals["n"]
        n_en_pc = en_pc_por_cat.get(cat, 0)
        n_falta = n_total - n_en_pc
        cats_resumen.append({
            "categoria": cat,
            "n_total": n_total,
            "n_en_pc": n_en_pc,
            "n_falta": n_falta,
            "sum_cred_banco": round(vals["cred"], 2),
            "sum_deb_banco": round(vals["deb"], 2),
            "sum_neto_banco": round(vals["cred"] - vals["deb"], 2),
            "monto_falta_pc": round(falta_en_pc_por_cat_monto.get(cat, 0.0), 2),
        })

    resultado = {
        "saldo_banco_top": round(saldo_banco_top, 2),
        "fecha_banco_top": fecha_banco_top,
        "saldo_pc": round(saldo_pc, 2),
        "fecha_pc": fecha_pc,
        "hist_cred": round(hist_cred, 2),
        "hist_deb": round(hist_deb, 2),
        "hist_neto": round(hist_neto, 2),
        "saldo_esperado": round(saldo_esperado, 2),
        "drift": drift,
        "cuadra": abs(drift) < 1.0,
        "n_movs_xlsx": len(movs),
        "categorias": cats_resumen,
        "no_banco": no_banco,
    }
    return render_template("admin_dbase/balance.html", resultado=resultado)
