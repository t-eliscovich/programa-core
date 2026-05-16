"""Sanity check: simulación rápida del flujo con el filtro actual."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

if __name__ == "__main__":
    # Saldo inicial hoy
    saldo_caja = float((db.fetch_one(
        "SELECT saldo FROM scintela.caja WHERE saldo IS NOT NULL ORDER BY fecha DESC, id_caja DESC LIMIT 1"
    ) or {}).get("saldo") or 0)
    saldo_bancos = float((db.fetch_one(
        """SELECT COALESCE(SUM(s), 0) AS s FROM (
              SELECT (SELECT t.saldo FROM scintela.transacciones_bancarias t
                       WHERE t.no_banco = b.no_banco
                       ORDER BY t.fecha DESC, t.id_transaccion DESC LIMIT 1) AS s
                FROM scintela.banco b) sub"""
    ) or {}).get("s") or 0)
    saldo_hoy = saldo_caja + saldo_bancos

    # Cheques cobrables 60d
    cheq_60 = float((db.fetch_one(
        """SELECT COALESCE(SUM(importe), 0) AS t FROM scintela.cheque
            WHERE stat IN ('Z','1','2','3','P','D')
              AND fechad BETWEEN CURRENT_DATE AND CURRENT_DATE + 60"""
    ) or {}).get("t") or 0)
    cheq_70 = float((db.fetch_one(
        """SELECT COALESCE(SUM(importe), 0) AS t FROM scintela.cheque
            WHERE stat IN ('Z','1','2','3','P','D')
              AND fechad BETWEEN CURRENT_DATE AND CURRENT_DATE + 70"""
    ) or {}).get("t") or 0)

    # Egresos posdat 60d con filtro NUEVO (banc IN 0,9, vencidos a hoy)
    egr_60 = float((db.fetch_one(
        """SELECT COALESCE(SUM(importe), 0) AS t FROM scintela.posdat
            WHERE fechad IS NOT NULL
              AND fechad <= CURRENT_DATE + 60
              AND COALESCE(banc, 0) IN (0, 9)"""
    ) or {}).get("t") or 0)
    egr_70 = float((db.fetch_one(
        """SELECT COALESCE(SUM(importe), 0) AS t FROM scintela.posdat
            WHERE fechad IS NOT NULL
              AND fechad <= CURRENT_DATE + 70
              AND COALESCE(banc, 0) IN (0, 9)"""
    ) or {}).get("t") or 0)

    print(f"Saldo inicial (bancos + caja) : ${saldo_hoy:>15,.0f}")
    print()
    print(f"+ Cheques cobrables 60d       : ${cheq_60:>15,.0f}")
    print(f"− Egresos posdat 60d (0,9)    : ${egr_60:>15,.0f}")
    saldo_60 = saldo_hoy + cheq_60 - egr_60
    print(f"= Saldo proyectado a 60d      : ${saldo_60:>15,.0f}")
    print()
    print(f"+ Cheques cobrables 70d       : ${cheq_70:>15,.0f}")
    print(f"− Egresos posdat 70d (0,9)    : ${egr_70:>15,.0f}")
    saldo_70 = saldo_hoy + cheq_70 - egr_70
    print(f"= Saldo proyectado a 70d      : ${saldo_70:>15,.0f}")
    print()
    print(f"dBase chart MIN (20-jul ≈ 68d): $    -2,276,000")

    out = ROOT / "scripts" / "_diag" / "flujo_check.json"
    out.write_text(json.dumps({
        "saldo_hoy": saldo_hoy,
        "saldo_caja": saldo_caja,
        "saldo_bancos": saldo_bancos,
        "cheq_60": cheq_60,
        "egr_60": egr_60,
        "saldo_60": saldo_60,
        "cheq_70": cheq_70,
        "egr_70": egr_70,
        "saldo_70": saldo_70,
    }, indent=2))
    print(f"\n→ {out}")
