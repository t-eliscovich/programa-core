#!/usr/bin/env python3
"""Backfill BULK de facturas 2025+ desde Asinfo a scintela.factura.

PROBLEMA QUE RESUELVE:
    El sync DBF (FACTURAS.DBF) trae solo las facturas "vivas" — el ERP
    legacy purga las T (canceladas total) y X (anuladas) después de un
    tiempo. Por eso /facturas en producción no muestra historia: BED
    solo aparece desde marzo 2026, las T globales empiezan 2025-11-24.

    Asinfo (ERP online) NUNCA purga. Tiene la historia completa.

QUÉ HACE:
    1. Trae todas las facturas (FACTURA + DEVOLUCION + NC_FINANCIERA +
       NTEN + NCNT) de Asinfo card 199 desde --desde (default 2025-01-01).
    2. Carga el universo de facturas PC ya existentes en ese rango,
       indexado por (a) numf_completo y (b) (numf, codigo_cli, fecha).
    3. Para cada factura de Asinfo, decide:
         - SI ya está en PC (match por numf_completo o por tripleta) -> SKIP
         - SI NO -> INSERT con usuario_crea='asinfo-backfill'.
    4. Las insertadas como 'asinfo-backfill' son intocables para el sync
       DBF (ver `import_dbf.py` delete_where para FACTURAS.DBF).

ASUNCIONES:
    - Son facturas históricas -> stat='T' (cobradas/canceladas total).
      Por eso saldo=0, abono=importe.
    - tipo PC: 'F' (factura), 'D' (devolución), 'C' (NC financiera),
      'N' (NTEN), 'X' (NCNT). Convención derivada del DBF.
    - condic='CC' default (cuenta corriente).
    - vencimiento = fecha + 30 días default.
    - numf_completo = Asinfo `numero` tal cual ('001-099-000175661' o
      'NTEN-10444').

USO standalone:
    python3 scripts/backfill_facturas_2025_asinfo.py
        [--dry-run]               # reporta, no INSERT
        [--desde=2025-01-01]
        [--hasta=YYYY-MM-DD]      # default hoy
        [--limit=0]               # 0 = sin límite
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from datetime import date, timedelta

# Windows EC2 default stdout es cp1252 — cualquier unicode (->, Δ, $, acentos
# en path, etc.) lo crashea con UnicodeEncodeError. Reconfigurar a UTF-8 para
# que los prints no rompan. Sin esto el script tiraba error a la primera flecha.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# .env loader (defensivo — sin dotenv funciona igual)
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
MARKER = "asinfo-backfill"


# Mapping Asinfo tipo -> PC tipo (1 char convención DBF)
TIPO_MAP = {
    "FACTURA":       "F",
    "DEVOLUCION":    "D",
    "NC_FINANCIERA": "C",
    "NTEN":          "N",
    "NCNT":          "X",  # nota crédito nota
}


def _extract_numf(asinfo_numero: str) -> int | None:
    """Sufijo numérico del numero de Asinfo (idéntico a backfill_numf_completo)."""
    if not asinfo_numero:
        return None
    m = re.findall(r"\d+", str(asinfo_numero))
    if not m:
        return None
    try:
        return int(m[-1])
    except (ValueError, TypeError):
        return None


def cargar_pc_existentes(desde: date, hasta: date) -> tuple[set, set, int]:
    """Indexa lo que ya está en PC en el rango.

    Returns:
        - by_numf_completo: set de numf_completo (strings normalizados upper)
        - by_tripla: set de (numf, codigo_cli_pc, fecha_iso)
        - max_numf: int — el MAX(numf) en PC, para no colisionar generando nuevos
    """
    rows = db.fetch_all(
        """
        SELECT numf, codigo_cli, fecha,
               COALESCE(NULLIF(TRIM(numf_completo), ''), '') AS nfc
          FROM scintela.factura
         WHERE fecha BETWEEN %s AND %s
        """,
        (desde, hasta),
    )
    by_nfc = set()
    by_tripla = set()
    max_numf = 0
    for r in rows:
        nfc = (r.get("nfc") or "").strip().upper()
        if nfc:
            by_nfc.add(nfc)
        numf = int(r.get("numf") or 0)
        cli = (r.get("codigo_cli") or "").strip().upper()
        f = r.get("fecha")
        if hasattr(f, "isoformat"):
            f = f.isoformat()
        if numf and cli:
            by_tripla.add((numf, cli, str(f)))
        if numf > max_numf:
            max_numf = numf

    # Además, MAX(numf) GLOBAL — no solo en rango — porque vamos a generar
    # numf para filas Asinfo que no tienen numf extraíble (NTEN puro etc).
    row = db.fetch_one("SELECT COALESCE(MAX(numf), 0) AS m FROM scintela.factura")
    if row and int(row.get("m") or 0) > max_numf:
        max_numf = int(row["m"])
    return by_nfc, by_tripla, max_numf


def _iter_month_chunks(desde: date, hasta: date):
    """Itera mes por mes inclusivo. Cada yield es (chunk_desde, chunk_hasta).

    La card 199 de Metabase contra Asinfo (SQL Server) puede tardar 30-60s
    para rangos largos — pasamos el rango entero y nos comimos timeouts de
    15s devolviendo []. Mejor: 17 calls chicas (~3-8s c/u) que 1 call grande.
    """
    from calendar import monthrange
    cur_y, cur_m = desde.year, desde.month
    end_y, end_m = hasta.year, hasta.month
    while (cur_y, cur_m) <= (end_y, end_m):
        first = date(cur_y, cur_m, 1)
        last_day = monthrange(cur_y, cur_m)[1]
        last = date(cur_y, cur_m, last_day)
        chunk_desde = max(first, desde)
        chunk_hasta = min(last, hasta)
        yield chunk_desde, chunk_hasta
        cur_m += 1
        if cur_m > 12:
            cur_m = 1
            cur_y += 1


def backfill(*, dry_run: bool, desde: date, hasta: date, limit: int) -> dict:
    print(f"-> Cargando facturas Asinfo {desde}..{hasta} (chunked mes a mes)", flush=True)
    asinfo_rows: list[dict] = []
    chunks = list(_iter_month_chunks(desde, hasta))
    for i, (cd, ch) in enumerate(chunks, 1):
        chunk_rows = asinfo_service.facturas_periodo(cd, ch)
        print(f"   [{i}/{len(chunks)}] {cd}..{ch} -> {len(chunk_rows)} filas", flush=True)
        asinfo_rows.extend(chunk_rows)
    print(f"   Asinfo trajo {len(asinfo_rows)} filas (suma de chunks)", flush=True)
    if not asinfo_rows:
        return {"asinfo": 0, "ya_estaban": 0, "insertadas": 0, "saltadas_sin_cli": 0,
                "saltadas_sin_importe": 0, "errores": 0}

    print(f"-> Cargando PC existentes (rango)", flush=True)
    by_nfc, by_tripla, max_numf_pc = cargar_pc_existentes(desde, hasta)
    print(f"   PC tiene {len(by_nfc)} numf_completo + {len(by_tripla)} triplas. MAX(numf)={max_numf_pc}", flush=True)

    siguiente_numf = max_numf_pc + 1

    ya_estaban = 0
    saltadas_sin_cli = 0
    saltadas_sin_importe = 0
    candidatas = []  # filas a insertar
    por_tipo = Counter()

    for ai in asinfo_rows:
        numero = str(ai.get("numero") or "").strip()
        nfc_key = numero.upper()
        numf = _extract_numf(numero)
        cli_asinfo = (ai.get("cliente_codigo") or "").strip().upper()
        cli_pc = cli_aliases.to_pc(cli_asinfo) if cli_asinfo else ""
        f = ai.get("fecha")
        if hasattr(f, "isoformat"):
            f_iso = f.isoformat()
            fecha_obj = f
        else:
            f_iso = str(f)[:10]
            try:
                fecha_obj = date.fromisoformat(f_iso)
            except ValueError:
                continue

        # Skip si ya está
        if nfc_key and nfc_key in by_nfc:
            ya_estaban += 1
            continue
        if numf and cli_pc and (numf, cli_pc, f_iso) in by_tripla:
            ya_estaban += 1
            continue

        if not cli_pc:
            saltadas_sin_cli += 1
            continue

        importe = float(ai.get("usd") or 0)
        kg = float(ai.get("kg") or 0)
        # NC_FINANCIERA y NTEN pueden tener importe=0 — los dejamos pasar igual.
        # Pero si TANTO kg como importe son 0 y no hay numf, no sirve.
        if importe == 0 and kg == 0 and not numf:
            saltadas_sin_importe += 1
            continue

        tipo_asinfo = str(ai.get("tipo") or "FACTURA").upper()
        tipo_pc = TIPO_MAP.get(tipo_asinfo, "F")
        por_tipo[tipo_asinfo] += 1

        # numf — si Asinfo no tiene número parseable, generamos uno
        if not numf:
            numf = siguiente_numf
            siguiente_numf += 1

        # stat='T' para histórico cobrado. abono=importe, saldo=0.
        candidatas.append({
            "numf": numf,
            "fecha": fecha_obj,
            "codigo_cli": cli_pc,
            "kg": kg,
            "importe": importe,
            "abono": importe,
            "saldo": 0,
            "stat": "T",
            "condic": "CC",
            "tipo": tipo_pc,
            "vencimiento": fecha_obj + timedelta(days=30),
            "numf_completo": numero or None,
            "clave": None,
            "usuario_crea": MARKER,
        })

        if limit and len(candidatas) >= limit:
            break

    print(f"-> Candidatas a INSERT: {len(candidatas)}", flush=True)
    print(f"   Ya estaban (skip): {ya_estaban}", flush=True)
    print(f"   Sin codigo_cli mapeable: {saltadas_sin_cli}", flush=True)
    print(f"   Sin importe/kg/numf útiles: {saltadas_sin_importe}", flush=True)
    print(f"   Distribución por tipo Asinfo:", flush=True)
    for t, n in por_tipo.most_common():
        print(f"     {t:18s} {n}", flush=True)

    insertadas = 0
    errores = 0
    if dry_run:
        print("\n[DRY-RUN] No se ejecuta INSERT. Muestra de 5 candidatas:", flush=True)
        for c in candidatas[:5]:
            print(f"     {c['fecha']} cli={c['codigo_cli']} numf={c['numf']} nfc={c['numf_completo']} "
                  f"tipo={c['tipo']} stat={c['stat']} imp={c['importe']}", flush=True)
        return {
            "asinfo": len(asinfo_rows), "candidatas": len(candidatas),
            "ya_estaban": ya_estaban, "saltadas_sin_cli": saltadas_sin_cli,
            "saltadas_sin_importe": saltadas_sin_importe,
            "insertadas": 0, "errores": 0, "dry_run": True,
        }

    # INSERT bulk — un commit por chunk de 500 para no perder todo si falla algo.
    CHUNK = 500
    sql = """
        INSERT INTO scintela.factura
            (numf, fecha, codigo_cli, kg, importe, abono, saldo,
             stat, condic, tipo, vencimiento, numf_completo, clave, usuario_crea)
        VALUES (%(numf)s, %(fecha)s, %(codigo_cli)s, %(kg)s, %(importe)s, %(abono)s, %(saldo)s,
                %(stat)s, %(condic)s, %(tipo)s, %(vencimiento)s, %(numf_completo)s, %(clave)s, %(usuario_crea)s)
    """
    for i in range(0, len(candidatas), CHUNK):
        chunk = candidatas[i:i+CHUNK]
        try:
            with db.tx() as conn, conn.cursor() as cur:
                for c in chunk:
                    cur.execute(sql, c)
                    insertadas += 1
            print(f"   ... {insertadas}/{len(candidatas)} insertadas", flush=True)
        except Exception as e:
            errores += len(chunk)
            print(f"   ERR chunk {i}-{i+len(chunk)}: {e!r}", flush=True)
            # Continuamos con el siguiente chunk — preferimos parcial a abortar todo

    return {
        "asinfo": len(asinfo_rows), "candidatas": len(candidatas),
        "ya_estaban": ya_estaban, "saltadas_sin_cli": saltadas_sin_cli,
        "saltadas_sin_importe": saltadas_sin_importe,
        "insertadas": insertadas, "errores": errores,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--desde", type=str, default="2025-01-01")
    ap.add_argument("--hasta", type=str, default=None)
    ap.add_argument("--limit", type=int, default=0, help="0 = sin límite")
    args = ap.parse_args()

    desde = date.fromisoformat(args.desde)
    hasta = date.fromisoformat(args.hasta) if args.hasta else date.today()

    print(f"==== Backfill facturas {desde}..{hasta}  (dry-run={args.dry_run}) ====", flush=True)
    stats = backfill(dry_run=args.dry_run, desde=desde, hasta=hasta, limit=args.limit)

    print("\n==== Resumen final ====", flush=True)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()
    if stats.get("insertadas"):
        print(f"OK {stats['insertadas']} facturas backfilleadas con usuario_crea='{MARKER}'.")
        print("    Estas filas son intocables para el próximo sync DBF.")
    if stats.get("errores"):
        print(f"!  {stats['errores']} fallaron — revisar logs arriba.")


if __name__ == "__main__":
    main()
