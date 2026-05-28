#!/usr/bin/env python3
"""Backfill PC.numf_completo cruzando contra Asinfo por sufijo numérico.

PROBLEMA QUE RESUELVE:
    El DBF tiene `numf` (175763) pero NO `numf_completo` (NTEN-10462 o
    001-099-000175763). Después de cada sync DBF, las facturas nuevas
    quedan "huérfanas" en /facturas?solo_huerfanas=1 hasta que se las
    matchee contra Asinfo.

QUÉ HACE:
    1. Lee todas las facturas PC con numf>0 y numf_completo IS NULL/''
       desde 2025-01-01 (cutoff Asinfo).
    2. Trae todas las facturas de Asinfo en el rango cubierto.
    3. Para cada PC factura, busca un Asinfo numero cuyo sufijo numérico
       coincida con numf. Si la fecha y el codigo_cli también coinciden,
       UPDATE numf_completo = asinfo.numero.
    4. Imprime resumen: cuántas se matchearon, cuántas siguen huérfanas.

USO (standalone):
    cd /Users/tamaraeliscovich/Documents/Claude/Projects/Programa Core
    .venv/bin/python scripts/backfill_numf_completo_from_asinfo.py
        [--dry-run]               # solo reporta, no UPDATE
        [--limit=500]             # max facturas a procesar
        [--desde=2025-01-01]      # cutoff custom

USO (post-sync DBF, vía SSM en EC2):
    Lo llama automáticamente sync_dbase_actual.py después del import.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Cargar .env si está
for _env in (".env.prod", ".env"):
    _p = os.path.join(_ROOT, _env)
    if os.path.isfile(_p):
        try:
            from dotenv import load_dotenv
            load_dotenv(_p, override=False)
        except ImportError:
            with open(_p) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() and k.strip() not in os.environ:
                        os.environ[k.strip()] = v.strip().strip('"').strip("'")
        break


import db
from modules.asinfo import service as asinfo_service
from modules.asinfo import aliases as cli_aliases


ASINFO_CUTOFF = date(2025, 1, 1)


def _extract_numf(asinfo_numero: str) -> int | None:
    """Extrae el sufijo numérico de un numero de Asinfo.

    Ejemplos:
        '001-099-000175763' -> 175763
        'NTEN-10462'        -> 10462
        'NCNT-00123'        -> 123
        ''                  -> None
    """
    if not asinfo_numero:
        return None
    # Toma el último grupo de dígitos del string
    m = re.findall(r"\d+", str(asinfo_numero))
    if not m:
        return None
    try:
        return int(m[-1])
    except (ValueError, TypeError):
        return None


def cargar_huerfanas_pc(desde: date, limite: int) -> list[dict]:
    """Facturas PC sin numf_completo asignado, candidatas a match con Asinfo.

    TMT 2026-05-28: ANTES filtrábamos `numf > 0`, lo que dejaba afuera las
    facturas que la dueña cargó a mano sin tipear el número (numf=0/NULL).
    Esas iban directo a /facturas?solo_huerfanas=1 y nunca se intentaban
    matchear automáticamente — había que hacerlo a mano. Ahora las
    incluimos: pase 1 las matchea por (numf, cli) si numf>0, pase 2 las
    matchea por (cli, fecha, importe) si numf=0/NULL.

    Excluye marcas '#DUP_*', '#SIN_ASINFO_*' (resoluciones humanas previas).
    """
    return db.fetch_all(
        """
        SELECT id_factura, numf, fecha, codigo_cli, kg, importe
          FROM scintela.factura
         WHERE fecha >= %s
           AND (numf_completo IS NULL OR numf_completo = '')
           AND (stat IS NULL OR stat IN ('Z','A','T','X','N','',' '))
           AND kg IS NOT NULL AND kg <> 0
         ORDER BY fecha DESC
         LIMIT %s
        """,
        (desde, limite),
    )


def _ai_ya_asignados() -> set[str]:
    """Set de numero_asinfo ya usados como numf_completo en otra factura PC.

    No deben re-asignarse — el UNIQUE constraint `uq_factura_numf_completo`
    levantaría error igual, pero filtrar acá nos deja contar ambiguos
    correctamente y darle el match al primer claim sin trabarse.
    """
    rows = db.fetch_all(
        "SELECT DISTINCT numf_completo "
        "  FROM scintela.factura "
        " WHERE numf_completo IS NOT NULL AND numf_completo <> '' "
        "   AND NOT (numf_completo LIKE '#%%')"
    ) or []
    return {(r.get("numf_completo") or "").strip() for r in rows}


def _match_pase_2_por_fecha_importe(
    huerfanas: list[dict],
    asinfo_facts: list[dict],
    ai_ya_usados: set[str],
    *,
    tolerancia_dias: int = 1,
    tolerancia_importe: float = 0.01,
) -> tuple[list[tuple[str, int]], int, int]:
    """Pase 2: match para facturas con numf=0/NULL.

    Estrategia: indexar Asinfo por (cli_pc, fecha_iso), buscar candidato
    con mismo cliente + fecha ±N días + |usd - importe_pc| < tolerancia +
    mismo signo de kg. Solo asigna si el match es ÚNICO (un único AI libre
    dentro de la ventana, no claim'd por otra factura PC en este mismo
    pase). Returns (updates, ambig, no_match).

    TMT 2026-05-28: nuevo pase para resolver las "huérfanas con numf=0"
    que el matcher original (solo numf+cli) saltaba completo.
    """
    # Indexar AI por (cli_pc, fecha_iso) — solo los que NO están ya usados.
    ai_idx: dict[tuple[str, str], list[dict]] = {}
    for ai in asinfo_facts:
        numero = (ai.get("numero") or "").strip()
        if not numero or numero in ai_ya_usados:
            continue
        cli_pc = cli_aliases.to_pc((ai.get("cliente_codigo") or "").upper())
        f = ai.get("fecha")
        if hasattr(f, "isoformat"):
            f_iso = f.isoformat()[:10]
        elif f:
            f_iso = str(f)[:10]
        else:
            continue
        ai_idx.setdefault((cli_pc, f_iso), []).append(ai)

    updates: list[tuple[str, int]] = []
    ai_claimed: set[str] = set()  # primer claim gana — evita doble-asignar
    ambig = 0
    no_match = 0
    from datetime import timedelta as _td

    for h in huerfanas:
        # Solo pase 2 si numf vacío.
        numf_pc = h.get("numf")
        if numf_pc and int(numf_pc) > 0:
            continue
        cli_pc = (h.get("codigo_cli") or "").strip().upper()
        imp_pc = float(h.get("importe") or 0)
        kg_pc = float(h.get("kg") or 0)
        f_pc = h.get("fecha")
        if not (cli_pc and f_pc):
            no_match += 1
            continue

        # Buscar candidatos en ventana fecha ±N
        cands: list[dict] = []
        for d in range(-tolerancia_dias, tolerancia_dias + 1):
            f_check = (f_pc + _td(days=d)).isoformat()
            for ai in ai_idx.get((cli_pc, f_check), []):
                numero = (ai.get("numero") or "").strip()
                if numero in ai_claimed:
                    continue
                usd_ai = float(ai.get("usd") or 0)
                kg_ai = float(ai.get("kg") or 0)
                # Filtros: importe ±tolerancia, signo kg.
                if abs(imp_pc - usd_ai) > max(
                    abs(imp_pc) * 0.005, tolerancia_importe
                ):
                    continue
                if (kg_pc > 0) != (kg_ai > 0):
                    continue
                # kg ±5% (drift de redondeo).
                if abs(kg_pc - kg_ai) > max(abs(kg_pc) * 0.05, 1.0):
                    continue
                cands.append(ai)

        if not cands:
            no_match += 1
            continue
        # Único match (preferir fecha exacta).
        cands.sort(
            key=lambda ai: abs(
                (
                    f_pc - (
                        ai["fecha"]
                        if hasattr(ai.get("fecha"), "isoformat")
                        else date.fromisoformat(str(ai.get("fecha"))[:10])
                    )
                ).days
            )
        )
        if len(cands) > 1:
            # Si hay varios pero el primero es claramente mejor en fecha
            # exacta, lo tomamos; sino ambiguo.
            mejor = cands[0]
            segundo = cands[1]
            f_mejor = (
                mejor["fecha"]
                if hasattr(mejor.get("fecha"), "isoformat")
                else date.fromisoformat(str(mejor.get("fecha"))[:10])
            )
            f_seg = (
                segundo["fecha"]
                if hasattr(segundo.get("fecha"), "isoformat")
                else date.fromisoformat(str(segundo.get("fecha"))[:10])
            )
            if abs((f_pc - f_mejor).days) >= abs((f_pc - f_seg).days):
                ambig += 1
                continue
        ai = cands[0]
        numero = (ai.get("numero") or "").strip()
        ai_claimed.add(numero)
        updates.append((numero, int(h["id_factura"])))
    return updates, ambig, no_match


def backfill(dry_run: bool = False, limite: int = 5000,
             desde: date | None = None) -> dict:
    """Run the backfill. Returns stats dict.

    TMT 2026-05-28: dos pases secuenciales —
      PASE 1: facturas con numf>0 → match por (numf, cli_alias).
      PASE 2: facturas con numf=0 → match por (cli_alias, fecha ±1d, importe).
    El segundo pase se agregó porque PC venía dejando ~465 facturas
    huérfanas (cargadas a mano sin tipear el número Asinfo), que el filtro
    `WHERE numf > 0` excluía del backfill principal. Ver task #25.
    """
    from datetime import timedelta as _td

    desde = desde or ASINFO_CUTOFF
    huerfanas = cargar_huerfanas_pc(desde, limite)
    n_huerf = len(huerfanas)
    if n_huerf == 0:
        return {
            "huerfanas_pc": 0,
            "pase1_matched": 0,
            "pase2_matched": 0,
            "updated": 0,
            "ambig": 0,
            "no_match": 0,
        }

    fechas = [h["fecha"] for h in huerfanas if h.get("fecha")]
    if not fechas:
        return {
            "huerfanas_pc": n_huerf,
            "pase1_matched": 0,
            "pase2_matched": 0,
            "updated": 0,
            "ambig": 0,
            "no_match": n_huerf,
        }

    # Ampliar 1 día el rango Asinfo — pase 2 mira ±1 día.
    mn = (min(fechas) - _td(days=1)).isoformat()
    mx = (max(fechas) + _td(days=1)).isoformat()

    try:
        asinfo_facts = asinfo_service.facturas_periodo(mn, mx)
    except Exception as e:
        print(f"ERR: Asinfo falló: {e}", file=sys.stderr)
        return {
            "huerfanas_pc": n_huerf,
            "pase1_matched": 0,
            "pase2_matched": 0,
            "updated": 0,
            "ambig": 0,
            "no_match": n_huerf,
            "error": str(e),
        }

    # Pre-cargar set de AI ya asignados — evita re-claim por UNIQUE constraint.
    ai_ya_usados = _ai_ya_asignados()

    # ===== PASE 1: numf > 0 → match por (numf, cli_alias) =====
    # TMT 2026-05-26: aplicamos alias map ANTES de indexar — el código de
    # Asinfo se traduce a PC para que (numf, cli) matchee directo. Ej:
    # Asinfo cliente="CL2" se indexa como "CLR" (su alias en PC).
    asinfo_by_key: dict[tuple[int, str], list[dict]] = {}
    for ai in asinfo_facts:
        numero = (ai.get("numero") or "").strip()
        if numero in ai_ya_usados:
            continue
        numf_ai = _extract_numf(numero)
        if numf_ai is None:
            continue
        cli_asinfo = (ai.get("cliente_codigo") or "").strip().upper()
        cli_pc = cli_aliases.to_pc(cli_asinfo)
        asinfo_by_key.setdefault((numf_ai, cli_pc), []).append(ai)

    pase1_matched = 0
    ambig = 0
    no_match = 0
    updates: list[tuple[str, int]] = []
    ai_claimed_pase1: set[str] = set()

    huerf_con_numf = [
        h for h in huerfanas if h.get("numf") and int(h["numf"]) > 0
    ]
    huerf_sin_numf = [
        h for h in huerfanas if not h.get("numf") or int(h["numf"]) == 0
    ]

    for h in huerf_con_numf:
        numf = int(h["numf"])
        cli = (h.get("codigo_cli") or "").strip().upper()
        candidatos = [
            ai for ai in asinfo_by_key.get((numf, cli), [])
            if (ai.get("numero") or "").strip() not in ai_claimed_pase1
        ]
        if not candidatos:
            # Fallback: sólo por numf, sin filtrar cliente.
            candidatos_loose = [
                ai
                for key, lst in asinfo_by_key.items()
                if key[0] == numf
                for ai in lst
                if (ai.get("numero") or "").strip() not in ai_claimed_pase1
            ]
            if len(candidatos_loose) == 1:
                candidatos = candidatos_loose
        if not candidatos:
            # Va al pase 2 — quizás encuentra por fecha/importe.
            huerf_sin_numf.append(h)
            continue
        if len(candidatos) > 1:
            ambig += 1
            continue
        ai = candidatos[0]
        numero = (ai.get("numero") or "").strip()
        ai_claimed_pase1.add(numero)
        updates.append((numero, int(h["id_factura"])))
        pase1_matched += 1

    # ===== PASE 2: numf=0 (o no matcheado en pase 1) → (cli, fecha, importe) =====
    # Excluir los AI ya claim'd en pase 1 para no doblar.
    ai_usados_total = ai_ya_usados | ai_claimed_pase1
    updates_pase2, ambig_p2, no_match_p2 = _match_pase_2_por_fecha_importe(
        huerf_sin_numf, asinfo_facts, ai_usados_total
    )
    pase2_matched = len(updates_pase2)
    updates.extend(updates_pase2)
    ambig += ambig_p2
    no_match += no_match_p2

    # ===== Aplicar UPDATEs =====
    import psycopg2

    updated = 0
    uniq_conflict = 0
    if updates and not dry_run:
        for numero, id_factura in updates:
            try:
                db.execute(
                    "UPDATE scintela.factura SET numf_completo = %s "
                    "WHERE id_factura = %s",
                    (numero, id_factura),
                )
                updated += 1
            except psycopg2.errors.UniqueViolation:
                # Otra factura PC ya tiene este numero asignado — race condition
                # entre el snapshot ya_usados y el UPDATE. Ignorar.
                uniq_conflict += 1
            except Exception as e:
                print(f"ERR: UPDATE {id_factura} falló: {e}", file=sys.stderr)

    return {
        "huerfanas_pc": n_huerf,
        "huerf_con_numf": len(huerf_con_numf),
        "huerf_sin_numf_inicial": n_huerf - len(huerf_con_numf),
        "asinfo_facts": len(asinfo_facts),
        "pase1_matched": pase1_matched,
        "pase2_matched": pase2_matched,
        "matched": pase1_matched + pase2_matched,
        "updated": updated,
        "uniq_conflict": uniq_conflict,
        "ambig": ambig,
        "no_match": no_match,
        "dry_run": dry_run,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="No UPDATE, solo reportar")
    ap.add_argument("--limit", type=int, default=5000, help="Max facturas a procesar")
    ap.add_argument("--desde", type=str, default=None, help="YYYY-MM-DD")
    args = ap.parse_args()

    desde = None
    if args.desde:
        from datetime import datetime
        desde = datetime.fromisoformat(args.desde).date()

    print(f"-> Backfill numf_completo (dry-run={args.dry_run}, limit={args.limit})")
    stats = backfill(dry_run=args.dry_run, limite=args.limit, desde=desde)

    print()
    print("==== Resumen ====")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()
    if stats["matched"] and not args.dry_run:
        print(f"OK {stats['updated']} facturas actualizadas con numf_completo.")
    if stats.get("no_match"):
        print(f"! {stats['no_match']} siguen huérfanas (no encontradas en Asinfo).")
    if stats.get("ambig"):
        print(f"! {stats['ambig']} ambiguas (múltiples Asinfo con mismo numf+cli) — review manual.")


if __name__ == "__main__":
    main()
