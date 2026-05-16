"""Diagnóstico read-only del flujo de caja.

Corre un set de queries contra la DB local y guarda el resultado en
`scripts/_diag/flujo_<timestamp>.json` Y en `scripts/_diag/flujo_latest.json`
(para que Claude pueda leer la última versión sin saber el timestamp).

Uso:
    python scripts/diag_flujo.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

OUT_DIR = ROOT / "scripts" / "_diag"
OUT_DIR.mkdir(exist_ok=True)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _date_str(x):
    if x is None:
        return None
    try:
        return x.isoformat()
    except AttributeError:
        return str(x)


def _normalize(rows):
    out = []
    for r in rows or []:
        rr = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                rr[k] = v.isoformat()
            elif isinstance(v, (int, float, str, bool)) or v is None:
                rr[k] = v
            else:
                rr[k] = str(v)
        out.append(rr)
    return out


def main() -> int:
    out = {"fecha": datetime.now().isoformat(), "queries": {}}

    # 1) Posdat por banc
    out["queries"]["posdat_por_banc"] = _normalize(db.fetch_all(
        """
        SELECT banc,
               COUNT(*) AS n,
               COALESCE(SUM(importe), 0) AS total,
               COUNT(*) FILTER (WHERE fechad >= CURRENT_DATE) AS futuros,
               COALESCE(SUM(importe) FILTER (WHERE fechad >= CURRENT_DATE), 0) AS total_futuros,
               COUNT(*) FILTER (WHERE fechad < CURRENT_DATE) AS vencidos,
               COALESCE(SUM(importe) FILTER (WHERE fechad < CURRENT_DATE), 0) AS total_vencidos,
               MIN(fechad) AS fechad_min,
               MAX(fechad) AS fechad_max
          FROM scintela.posdat
         GROUP BY banc
         ORDER BY banc
        """
    ))

    # 2) Plazos forward-looking (fechad - hoy) — todas
    out["queries"]["plazos_forward"] = _normalize(db.fetch_all(
        """
        SELECT 'DSO_factura_todas' AS metric,
               ROUND(SUM(saldo * (vencimiento - CURRENT_DATE)) / NULLIF(SUM(saldo), 0), 1) AS dias,
               COUNT(*) AS n,
               ROUND(SUM(saldo)::numeric, 2) AS base
          FROM scintela.factura
         WHERE COALESCE(saldo, 0) > 0
           AND COALESCE(stat, '') IN ('Z', 'A', '', ' ')
           AND vencimiento IS NOT NULL
        UNION ALL
        SELECT 'DSO_factura_solo_futuras',
               ROUND(SUM(saldo * (vencimiento - CURRENT_DATE)) / NULLIF(SUM(saldo), 0), 1),
               COUNT(*),
               ROUND(SUM(saldo)::numeric, 2)
          FROM scintela.factura
         WHERE COALESCE(saldo, 0) > 0
           AND COALESCE(stat, '') IN ('Z', 'A', '', ' ')
           AND vencimiento >= CURRENT_DATE
        UNION ALL
        SELECT 'DPO_posdat_banc0_todas',
               ROUND(SUM(importe * (fechad - CURRENT_DATE)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fechad IS NOT NULL
        UNION ALL
        SELECT 'DPO_posdat_banc0_solo_futuras',
               ROUND(SUM(importe * (fechad - CURRENT_DATE)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fechad >= CURRENT_DATE
        """
    ))

    # 3) Plazos backward-looking (hoy - fecha_emisión) — DSO/DPO clásico
    out["queries"]["plazos_backward"] = _normalize(db.fetch_all(
        """
        SELECT 'DSO_clasico (hoy-fecha emision)' AS metric,
               ROUND(SUM(saldo * (CURRENT_DATE - fecha)) / NULLIF(SUM(saldo), 0), 1) AS dias
          FROM scintela.factura
         WHERE COALESCE(saldo, 0) > 0
           AND COALESCE(stat, '') IN ('Z', 'A', '', ' ')
        UNION ALL
        SELECT 'DPO_clasico_banc0',
               ROUND(SUM(importe * (CURRENT_DATE - fecha)) / NULLIF(SUM(importe), 0), 1)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
        """
    ))

    # 4) Plazos ventana 60 días (igual a la del chart actual)
    out["queries"]["plazos_60dias"] = _normalize(db.fetch_all(
        """
        SELECT 'DSO_60d_vencimiento_entre_hoy_y_60' AS metric,
               ROUND(SUM(saldo * (vencimiento - CURRENT_DATE)) / NULLIF(SUM(saldo), 0), 1) AS dias,
               COUNT(*) AS n,
               ROUND(SUM(saldo)::numeric, 2) AS base
          FROM scintela.factura
         WHERE COALESCE(saldo, 0) > 0
           AND COALESCE(stat, '') IN ('Z', 'A', '', ' ')
           AND vencimiento BETWEEN CURRENT_DATE AND CURRENT_DATE + 60
        UNION ALL
        SELECT 'DPO_60d_banc0',
               ROUND(SUM(importe * (fechad - CURRENT_DATE)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fechad BETWEEN CURRENT_DATE AND CURRENT_DATE + 60
        UNION ALL
        SELECT 'DPO_60d_banc_no_9 (incluye banc=9 futuros — BUG actual)',
               ROUND(SUM(importe * (fechad - CURRENT_DATE)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE fechad BETWEEN CURRENT_DATE AND CURRENT_DATE + 60
           AND NOT (COALESCE(banc, 0) = 9 AND fechad < CURRENT_DATE)
        """
    ))

    # 4b) PLAZO OTORGADO = vencimiento - fecha (no hoy - fecha, no fechad - hoy)
    #     Esta es la hipótesis más fuerte para PLAZ.COBR / PLAZ.DEUDA de dBase.
    out["queries"]["plazo_otorgado_factura"] = _normalize(db.fetch_all(
        """
        SELECT 'plazo_otorgado_facturas_abiertas' AS metric,
               ROUND(SUM(saldo * (vencimiento - fecha)) / NULLIF(SUM(saldo), 0), 1) AS dias_ponderado,
               ROUND(AVG(vencimiento - fecha)::numeric, 1) AS dias_simple,
               COUNT(*) AS n,
               ROUND(SUM(saldo)::numeric, 2) AS base
          FROM scintela.factura
         WHERE COALESCE(saldo, 0) > 0
           AND COALESCE(stat, '') IN ('Z', 'A', '', ' ')
           AND vencimiento IS NOT NULL
           AND fecha IS NOT NULL
        UNION ALL
        SELECT 'plazo_otorgado_facturas_todas',
               ROUND(SUM(importe * (vencimiento - fecha)) / NULLIF(SUM(importe), 0), 1),
               ROUND(AVG(vencimiento - fecha)::numeric, 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.factura
         WHERE vencimiento IS NOT NULL
           AND fecha IS NOT NULL
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        UNION ALL
        SELECT 'plazo_otorgado_factura_ult_12m',
               ROUND(SUM(importe * (vencimiento - fecha)) / NULLIF(SUM(importe), 0), 1),
               ROUND(AVG(vencimiento - fecha)::numeric, 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.factura
         WHERE vencimiento IS NOT NULL
           AND fecha IS NOT NULL
           AND fecha >= CURRENT_DATE - 365
           AND COALESCE(stat, '') NOT IN ('X', 'Y')
        """
    ))

    # 4c) PLAZO OTORGADO posdat = fechad - fecha. Misma hipótesis para PLAZ.DEUDA.
    out["queries"]["plazo_otorgado_posdat"] = _normalize(db.fetch_all(
        """
        SELECT 'plazo_otorgado_posdat_banc0_todos' AS metric,
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1) AS dias_ponderado,
               ROUND(AVG(fechad - fecha)::numeric, 1) AS dias_simple,
               COUNT(*) AS n,
               ROUND(SUM(importe)::numeric, 2) AS base
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fechad IS NOT NULL
           AND fecha IS NOT NULL
        UNION ALL
        SELECT 'plazo_otorgado_posdat_banc0_ult_12m',
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1),
               ROUND(AVG(fechad - fecha)::numeric, 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fechad IS NOT NULL
           AND fecha IS NOT NULL
           AND fecha >= CURRENT_DATE - 365
        UNION ALL
        SELECT 'plazo_otorgado_posdat_todos',
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1),
               ROUND(AVG(fechad - fecha)::numeric, 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE fechad IS NOT NULL
           AND fecha IS NOT NULL
        UNION ALL
        SELECT 'plazo_otorgado_posdat_banc0_sin_outliers (fechad-fecha entre 0 y 365)',
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1),
               ROUND(AVG(fechad - fecha)::numeric, 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fechad IS NOT NULL
           AND fecha IS NOT NULL
           AND (fechad - fecha) BETWEEN 0 AND 365
        """
    ))

    # 4c2) Más variantes de PLAZ.DEUDA — recortes temporales y exclusión de refinanciamientos
    out["queries"]["plazo_deuda_variantes"] = _normalize(db.fetch_all(
        """
        SELECT 'banc0_fechad_ult_180d' AS metric,
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1) AS dias,
               COUNT(*) AS n,
               ROUND(SUM(importe)::numeric, 2) AS base
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fechad IS NOT NULL AND fecha IS NOT NULL
           AND (fechad - fecha) BETWEEN 0 AND 180
        UNION ALL
        SELECT 'banc0_fechad_ult_120d',
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fechad IS NOT NULL AND fecha IS NOT NULL
           AND (fechad - fecha) BETWEEN 0 AND 120
        UNION ALL
        SELECT 'banc0_fecha_ult_6m',
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fecha >= CURRENT_DATE - 180
           AND fechad IS NOT NULL AND fecha IS NOT NULL
        UNION ALL
        SELECT 'banc0_fecha_ult_3m',
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fecha >= CURRENT_DATE - 90
           AND fechad IS NOT NULL AND fecha IS NOT NULL
        UNION ALL
        SELECT 'banc0_sin_YY_BP',
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND COALESCE(prov, '') NOT IN ('YY', 'BP')
           AND fechad IS NOT NULL AND fecha IS NOT NULL
        UNION ALL
        SELECT 'banc0+banc9_fechad_ult_180d',
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) IN (0, 9)
           AND fechad IS NOT NULL AND fecha IS NOT NULL
           AND (fechad - fecha) BETWEEN 0 AND 180
        UNION ALL
        SELECT 'banc9_solo_futuros_fechad-hoy',
               ROUND(SUM(importe * (fechad - CURRENT_DATE)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 9
           AND fechad >= CURRENT_DATE
        UNION ALL
        SELECT 'banc0_fechad-hoy_futuros_sin_outliers',
               ROUND(SUM(importe * (fechad - CURRENT_DATE)) / NULLIF(SUM(importe), 0), 1),
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND fechad >= CURRENT_DATE
           AND (fechad - CURRENT_DATE) BETWEEN 0 AND 365
        """
    ))

    # 4d) Cheque cartera: plazo otorgado por cliente = fechad - fecha
    out["queries"]["plazo_otorgado_cheque"] = _normalize(db.fetch_all(
        """
        SELECT 'plazo_otorgado_cheque_cartera' AS metric,
               ROUND(SUM(importe * (fechad - fecha)) / NULLIF(SUM(importe), 0), 1) AS dias_ponderado,
               ROUND(AVG(fechad - fecha)::numeric, 1) AS dias_simple,
               COUNT(*) AS n,
               ROUND(SUM(importe)::numeric, 2) AS base
          FROM scintela.cheque
         WHERE COALESCE(stat, '') IN ('Z', 'P', 'D')
           AND fechad IS NOT NULL
           AND fecha IS NOT NULL
        """
    ))

    # 5) Cheques cobrables próximos 60 días (lo que entra al flujo como ingreso)
    out["queries"]["cheques_cobrables"] = _normalize(db.fetch_all(
        """
        SELECT 'cheques_cartera_proximos_60d' AS metric,
               COUNT(*) AS n,
               ROUND(SUM(importe)::numeric, 2) AS total
          FROM scintela.cheque
         WHERE COALESCE(stat, '') IN ('Z', 'P', 'D')
           AND COALESCE(fechad, fecha) BETWEEN CURRENT_DATE AND CURRENT_DATE + 60
        UNION ALL
        SELECT 'cheques_cartera_todos',
               COUNT(*),
               ROUND(SUM(importe)::numeric, 2)
          FROM scintela.cheque
         WHERE COALESCE(stat, '') IN ('Z', 'P', 'D')
        """
    ))

    # 6) Egresos del flujo según código actual (qué SQL corre realmente)
    out["queries"]["egresos_flujo_actual"] = _normalize(db.fetch_all(
        """
        SELECT
            COUNT(*) AS n,
            ROUND(SUM(importe)::numeric, 2) AS total,
            COUNT(*) FILTER (WHERE COALESCE(banc, 0) = 0) AS n_banc_0,
            COUNT(*) FILTER (WHERE COALESCE(banc, 0) = 9) AS n_banc_9,
            ROUND(SUM(importe) FILTER (WHERE COALESCE(banc, 0) = 0)::numeric, 2) AS total_banc_0,
            ROUND(SUM(importe) FILTER (WHERE COALESCE(banc, 0) = 9)::numeric, 2) AS total_banc_9
          FROM scintela.posdat
         WHERE fechad IS NOT NULL
           AND fechad <= CURRENT_DATE + 60
           AND NOT (COALESCE(banc, 0) = 9 AND fechad < CURRENT_DATE)
        """
    ))

    # 7) Egresos del flujo si filtraramos banc<>9 (propuesta de fix)
    out["queries"]["egresos_flujo_fix_propuesto"] = _normalize(db.fetch_all(
        """
        SELECT
            COUNT(*) AS n,
            ROUND(SUM(importe)::numeric, 2) AS total
          FROM scintela.posdat
         WHERE fechad IS NOT NULL
           AND fechad <= CURRENT_DATE + 60
           AND COALESCE(banc, 0) <> 9
        """
    ))

    # 8) Mira columna `pago` en cliente — quizás dBase usa pago promedio de cliente.
    #    pago es varchar; cast defensivo via regex.
    try:
        out["queries"]["cliente_pago_medio"] = _normalize(db.fetch_all(
            """
            SELECT ROUND(AVG(NULLIF(regexp_replace(COALESCE(pago::text, ''), '[^0-9.\\-]', '', 'g'), '')::numeric), 1) AS pago_promedio_dias,
                   COUNT(*) FILTER (WHERE NULLIF(regexp_replace(COALESCE(pago::text, ''), '[^0-9.\\-]', '', 'g'), '') IS NOT NULL) AS n,
                   MIN(NULLIF(regexp_replace(COALESCE(pago::text, ''), '[^0-9.\\-]', '', 'g'), '')::numeric) AS min_pago,
                   MAX(NULLIF(regexp_replace(COALESCE(pago::text, ''), '[^0-9.\\-]', '', 'g'), '')::numeric) AS max_pago
              FROM scintela.cliente
            """
        ))
    except Exception as e:
        out["queries"]["cliente_pago_medio"] = [{"error": str(e)}]

    # 8b) Muestra de valores crudos de cliente.pago — top 20
    try:
        out["queries"]["cliente_pago_muestra"] = _normalize(db.fetch_all(
            """
            SELECT pago, COUNT(*) AS n
              FROM scintela.cliente
             WHERE pago IS NOT NULL
             GROUP BY pago
             ORDER BY COUNT(*) DESC
             LIMIT 20
            """
        ))
    except Exception as e:
        out["queries"]["cliente_pago_muestra"] = [{"error": str(e)}]

    # 9) Mira columna `plazo` en proveedor (si existe)
    try:
        out["queries"]["proveedor_plazo_medio"] = _normalize(db.fetch_all(
            """
            SELECT ROUND(AVG(NULLIF(regexp_replace(COALESCE(plazo::text, ''), '[^0-9.\\-]', '', 'g'), '')::numeric), 1) AS plazo_promedio_dias,
                   COUNT(*) FILTER (WHERE NULLIF(regexp_replace(COALESCE(plazo::text, ''), '[^0-9.\\-]', '', 'g'), '') IS NOT NULL) AS n,
                   MIN(NULLIF(regexp_replace(COALESCE(plazo::text, ''), '[^0-9.\\-]', '', 'g'), '')::numeric) AS min_plazo,
                   MAX(NULLIF(regexp_replace(COALESCE(plazo::text, ''), '[^0-9.\\-]', '', 'g'), '')::numeric) AS max_plazo
              FROM scintela.proveedor
            """
        ))
    except Exception as e:
        out["queries"]["proveedor_plazo_medio"] = [{"error": str(e)}]

    # 9b) Muestra cruda de proveedor.plazo — top 20
    try:
        out["queries"]["proveedor_plazo_muestra"] = _normalize(db.fetch_all(
            """
            SELECT plazo, COUNT(*) AS n
              FROM scintela.proveedor
             WHERE plazo IS NOT NULL
             GROUP BY plazo
             ORDER BY COUNT(*) DESC
             LIMIT 20
            """
        ))
    except Exception as e:
        out["queries"]["proveedor_plazo_muestra"] = [{"error": str(e)}]

    # 10) Anatomía completa de posdats banc=0 — top 10 más viejos
    out["queries"]["posdat_banc0_top10_mas_viejos"] = _normalize(db.fetch_all(
        """
        SELECT id_posdat, fecha, fechad, prov, importe,
               (CURRENT_DATE - fecha) AS dias_desde_emision,
               (fechad - CURRENT_DATE) AS dias_hasta_vencimiento
          FROM scintela.posdat
         WHERE COALESCE(banc, 0) = 0
           AND importe > 0
         ORDER BY fecha ASC
         LIMIT 10
        """
    ))

    # Guardar
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    f_ts = OUT_DIR / f"flujo_{ts}.json"
    f_latest = OUT_DIR / "flujo_latest.json"
    f_ts.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    f_latest.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))

    # Print resumen al stdout
    print(f"✓ Guardado en {f_ts}")
    print(f"✓ Symlink: {f_latest}\n")
    for key, rows in out["queries"].items():
        print(f"--- {key} ---")
        if isinstance(rows, dict):
            print(f"  {rows}")
        else:
            for r in rows:
                print(f"  {r}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
