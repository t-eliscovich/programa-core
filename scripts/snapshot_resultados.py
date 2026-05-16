"""Snapshot del Informe de Resultados — para validar deltas entre runs.

Uso típico:
  1. Antes de cualquier cambio: `python scripts/snapshot_resultados.py --label antes`
  2. Hacer operaciones (alta + reverso, por ejemplo).
  3. Después: `python scripts/snapshot_resultados.py --label despues`
  4. `python scripts/snapshot_resultados.py --diff antes despues`

Cada snapshot se guarda en `scripts/_snapshots/<label>.json` con todos los
valores clave del balance: utilidad, PATR, total activo, stock breakdown,
costos por categoría, saldos bancos+caja+cheques+facturas.

Si la operación fue forward+reverse perfecta, el diff debería ser TODO 0.
Cualquier delta != 0 marca un saldo que no se restauró correctamente.

TMT 2026-05-13.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SNAP_DIR = ROOT / "scripts" / "_snapshots"
SNAP_DIR.mkdir(exist_ok=True)


def _num(x) -> float:
    """Cast seguro a float."""
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def tomar_snapshot() -> dict:
    """Lee informe_balance() y pliega los valores claves en un dict plano."""
    import db
    from modules.informes import queries as inf

    bal = inf.informe_balance() or {}

    # Resultados.stock — dict {hilado/tejido/terminado/total: {kg, ukg, us}}
    res = bal.get("resultados") or {}
    stock = res.get("stock") or {}
    hilado    = stock.get("hilado")    or {}
    tejido    = stock.get("tejido")    or {}
    terminado = stock.get("terminado") or {}
    total_st  = stock.get("total")     or {}

    # Costos del mes — list of {label, us, kg, ukg, proy}
    costos = {c.get("label"): c for c in (res.get("costos") or [])}

    # Utilidad (live)
    util = res.get("utilidad") or {}

    snap = {
        "fecha_snapshot": datetime.now().isoformat(),
        # Utilidad / patrimonio (top-level de bal)
        "utilidad_us":         _num(bal.get("utilidad")),
        "patr":                _num(bal.get("patr")),
        "patant":              _num(bal.get("patant")),
        "patr_para_utilidad":  _num(bal.get("patr_para_utilidad")),
        # Componentes del activo (todos en USD)
        "salcaj":              _num(bal.get("salcaj")),
        "salbanc":             _num(bal.get("salbanc")),
        "salbanc1":            _num(bal.get("salbanc1")),
        "salbanc2":            _num(bal.get("salbanc2")),
        "pos1":                _num(bal.get("pos1")),
        "pos2":                _num(bal.get("pos2")),
        "antic":               _num(bal.get("antic")),
        "uret":                _num(bal.get("uret")),
        "umaq":                _num(bal.get("umaq")),
        "uact":                _num(bal.get("uact")),
        "vsto":                _num(bal.get("vsto")),
        "vqx":                 _num(bal.get("vqx")),
        "cart":                _num(bal.get("cart")),
        "subt":                _num(bal.get("subt")),
        "totl":                _num(bal.get("totl")),
        "totp":                _num(bal.get("totp")),
        # Stock breakdown
        "stock_hilado_kg":     _num(hilado.get("kg")),
        "stock_hilado_us":     _num(hilado.get("us")),
        "stock_hilado_ukg":    _num(hilado.get("ukg")),
        "stock_tejido_kg":     _num(tejido.get("kg")),
        "stock_tejido_us":     _num(tejido.get("us")),
        "stock_tejido_ukg":    _num(tejido.get("ukg")),
        "stock_terminado_kg":  _num(terminado.get("kg")),
        "stock_terminado_us":  _num(terminado.get("us")),
        "stock_terminado_ukg": _num(terminado.get("ukg")),
        "stock_total_kg":      _num(total_st.get("kg")),
        "stock_total_us":      _num(total_st.get("us")),
        "stock_total_ukg":     _num(total_st.get("ukg")),
        # Costos del mes (panel RESULTADOS)
        "venta_us":            _num((res.get("venta") or {}).get("us")),
        "matpr_us":            _num((costos.get("MAT.PR.") or {}).get("us")),
        "tejido_costo_us":     _num((costos.get("TEJIDO")   or {}).get("us")),
        "colqui_us":           _num((costos.get("COL.QUI.") or {}).get("us")),
        "gsproc_us":           _num((costos.get("GS.PROC.") or {}).get("us")),
        "gastos_us":           _num((costos.get("GASTOS")   or {}).get("us")),
        # Utilidad live
        "utilidad_live_us":    _num(util.get("us")),
        "utilidad_live_pct":   _num(util.get("pct")),
    }

    # Saldos exactos por banco (independiente del display)
    bancos_saldo = db.fetch_all(
        """
        SELECT b.no_banco, b.nombre,
               COALESCE((SELECT t.saldo FROM scintela.transacciones_bancarias t
                          WHERE t.no_banco = b.no_banco
                          ORDER BY t.fecha DESC, t.id_transaccion DESC
                          LIMIT 1), 0) AS saldo
          FROM scintela.banco b
         WHERE COALESCE(UPPER(b.nombre),'') LIKE '%%PICHINC%%'
            OR COALESCE(UPPER(b.nombre),'') LIKE '%%INTERNAC%%'
         ORDER BY b.no_banco
        """
    ) or []
    snap["bancos_saldos_por_id"] = {
        str(b["no_banco"]): {
            "nombre": (b.get("nombre") or "").strip(),
            "saldo": _num(b.get("saldo")),
        }
        for b in bancos_saldo
    }

    # Conteos de mov_doble por estado
    mov_estados = db.fetch_all(
        """
        SELECT estado, COUNT(*) AS n, COALESCE(SUM(importe), 0) AS total
          FROM scintela.mov_doble
         GROUP BY estado
         ORDER BY estado
        """
    ) or []
    snap["mov_doble_por_estado"] = {
        r["estado"]: {"n": int(r["n"]), "total": _num(r.get("total"))}
        for r in mov_estados
    }

    return snap


def guardar(label: str, snap: dict) -> Path:
    p = SNAP_DIR / f"{label}.json"
    p.write_text(json.dumps(snap, indent=2, ensure_ascii=False, default=str))
    return p


def cargar(label: str) -> dict:
    p = SNAP_DIR / f"{label}.json"
    if not p.exists():
        raise FileNotFoundError(f"No existe snapshot '{label}' en {p}")
    return json.loads(p.read_text())


def _es_numero(x) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def diff(snap_a: dict, snap_b: dict, *, tol: float = 0.01) -> dict:
    """Devuelve dict {clave: (a, b, delta)} para cambios > tol."""
    cambios: dict = {}
    todas = set(snap_a) | set(snap_b)
    for k in sorted(todas):
        if k == "fecha_snapshot":
            continue
        a, b = snap_a.get(k), snap_b.get(k)
        if isinstance(a, dict) and isinstance(b, dict):
            sub = diff(a, b, tol=tol)
            if sub:
                cambios[k] = sub
            continue
        if _es_numero(a) and _es_numero(b):
            delta = (b or 0) - (a or 0)
            if abs(delta) > tol:
                cambios[k] = {"antes": a, "despues": b, "delta": delta}
        elif a != b:
            cambios[k] = {"antes": a, "despues": b}
    return cambios


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", help="Tomar snapshot con este label.")
    p.add_argument("--diff", nargs=2, metavar=("LABEL_A", "LABEL_B"),
                   help="Comparar dos snapshots.")
    p.add_argument("--list", action="store_true",
                   help="Listar snapshots existentes.")
    p.add_argument("--tol", type=float, default=0.01,
                   help="Tolerancia de delta (default 0.01).")
    args = p.parse_args()

    if args.list:
        for f in sorted(SNAP_DIR.glob("*.json")):
            print(f.stem)
        return 0

    if args.label:
        snap = tomar_snapshot()
        path = guardar(args.label, snap)
        print(f"Snapshot '{args.label}' guardado en {path}\n")
        print(f"  Utilidad (PATR-PATANT):  $ {snap['utilidad_us']:>14,.2f}")
        print(f"  Utilidad live:           $ {snap['utilidad_live_us']:>14,.2f}  ({snap['utilidad_live_pct']:.2f}%)")
        print(f"  Patrimonio neto (PATR):  $ {snap['patr']:>14,.2f}")
        print(f"  Patrimonio anterior:     $ {snap['patant']:>14,.2f}")
        print(f"  Total activos (TOTL):    $ {snap['totl']:>14,.2f}")
        print(f"  Total pasivos (TOTP):    $ {snap['totp']:>14,.2f}")
        print()
        print(f"  Caja:                    $ {snap['salcaj']:>14,.2f}")
        print(f"  Bancos total:            $ {snap['salbanc']:>14,.2f}")
        print(f"  Cartera (cheques+fact):  $ {snap['cart']:>14,.2f}")
        print(f"  Stock MP+Prod (vsto):    $ {snap['vsto']:>14,.2f}")
        print(f"  Stock Quím (vqx):        $ {snap['vqx']:>14,.2f}")
        print()
        print(f"  Stock Hilado:    {snap['stock_hilado_kg']:>9,.0f} kg · "
              f"{snap['stock_hilado_ukg']:>6,.3f} U$/kg · "
              f"$ {snap['stock_hilado_us']:>12,.2f}")
        print(f"  Stock Tejido:    {snap['stock_tejido_kg']:>9,.0f} kg · "
              f"{snap['stock_tejido_ukg']:>6,.3f} U$/kg · "
              f"$ {snap['stock_tejido_us']:>12,.2f}")
        print(f"  Stock Terminado: {snap['stock_terminado_kg']:>9,.0f} kg · "
              f"{snap['stock_terminado_ukg']:>6,.3f} U$/kg · "
              f"$ {snap['stock_terminado_us']:>12,.2f}")
        print(f"  Stock TOTAL:     {snap['stock_total_kg']:>9,.0f} kg · "
              f"{snap['stock_total_ukg']:>6,.3f} U$/kg · "
              f"$ {snap['stock_total_us']:>12,.2f}")
        print()
        print(f"  MAT.PR. (mes):           $ {snap['matpr_us']:>14,.2f}")
        print(f"  TEJIDO (mes):            $ {snap['tejido_costo_us']:>14,.2f}")
        print(f"  COL.QUI. (mes):          $ {snap['colqui_us']:>14,.2f}")
        print(f"  GS.PROC. (mes):          $ {snap['gsproc_us']:>14,.2f}")
        print(f"  GASTOS (mes):            $ {snap['gastos_us']:>14,.2f}")
        print()
        for nb, b in snap.get("bancos_saldos_por_id", {}).items():
            print(f"  Banco #{nb} {b['nombre']:<14}: $ {b['saldo']:>14,.2f}")
        print()
        for estado, d in snap.get("mov_doble_por_estado", {}).items():
            print(f"  mov_doble {estado:<10}: {d['n']:>6} filas · $ {d['total']:>14,.2f}")
        return 0

    if args.diff:
        a, b = args.diff
        snap_a = cargar(a)
        snap_b = cargar(b)
        cambios = diff(snap_a, snap_b, tol=args.tol)
        if not cambios:
            print(f"✓ '{a}' vs '{b}': SIN CAMBIOS (tol ±{args.tol}).")
            return 0
        print(f"Diferencias '{a}' → '{b}':\n")

        def _print(d, indent=""):
            for k, v in d.items():
                if isinstance(v, dict) and "antes" not in v:
                    print(f"{indent}{k}:")
                    _print(v, indent + "  ")
                else:
                    a_, b_ = v.get("antes"), v.get("despues")
                    delta = v.get("delta")
                    if delta is not None:
                        signo = "+" if delta > 0 else ""
                        print(f"{indent}{k:<32}: {a_:>14,.2f} → {b_:>14,.2f}  ({signo}{delta:,.2f})")
                    else:
                        print(f"{indent}{k:<32}: {a_!r} → {b_!r}")

        _print(cambios)
        return 1

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
