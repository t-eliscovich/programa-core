#!/usr/bin/env bash
# debug_tintoreria_vs_excel.sh — comparar tinturado_resumen() del bridge
# Programa Core contra la query exacta del Excel /telas/export de
# formulas_app. Detectar qué órdenes están de más o de menos por día.
#
# Uso (en CloudShell):
#   curl -sL https://raw.githubusercontent.com/t-eliscovich/programa-core/main/scripts/debug_tintoreria_vs_excel.sh | bash
#
# Opcional:
#   DESDE=2026-05-01 HASTA=2026-05-26 curl -sL ... | bash

set -euo pipefail

INSTANCE_ID="${INSTANCE_ID:-i-0fcca4d7029f08489}"
REGION="${REGION:-us-east-2}"
DESDE="${DESDE:-2026-05-01}"
HASTA="${HASTA:-2026-05-26}"

echo "=== Debug tintorería: Programa Core vs Excel — $DESDE..$HASTA ==="

PY=$(cat <<EOF
import os, sys, io
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
sys.path.insert(0, r'C:\programa-core')
os.chdir(r'C:\programa-core')
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from datetime import date
from collections import defaultdict
from modules._lib import formulas_db
formulas_db.init_pool()
if not formulas_db.disponible():
    print("ERROR: formulas_db no disponible")
    sys.exit(1)

DESDE = "$DESDE"
HASTA = "$HASTA"

# ============================================================
# Query A — get_telas_report de formulas_app (FUENTE DEL EXCEL)
# Replica EXACTA del SQL de formulas_app/database.py:get_telas_report
# ============================================================
print()
print("=== A) get_telas_report (fuente del Excel) ===")
print("Filtra: SUBSTRING(o.fecha) BETWEEN desde Y hasta (fecha de CREACION)")
print()
rows_a = formulas_db.fetch_all("""
    SELECT o.id, o.numero, o.fecha, o.codigo, o.jet,
           o.tela_cruda_kg, o.tela_terminada_kg, o.fecha_terminado,
           SUBSTRING(o.fecha, 7, 4)||'-'||SUBSTRING(o.fecha, 4, 2)||'-'||SUBSTRING(o.fecha, 1, 2) AS fecha_iso
      FROM ordenes o
      LEFT JOIN formulas f ON f.cod = o.codigo
     WHERE SUBSTRING(o.fecha, 7, 4)||'-'||SUBSTRING(o.fecha, 4, 2)||'-'||SUBSTRING(o.fecha, 1, 2)
           BETWEEN %s AND %s
     ORDER BY fecha_iso ASC, o.id ASC
""", (DESDE, HASTA))
print(f"  Total ordenes (creadas en rango): {len(rows_a)}")

# Agrupar por fecha_terminado (como hace el Excel "TELAS - Totales por dia terminado")
by_ft_a = defaultdict(lambda: {"n": 0, "cruda": 0.0, "term": 0.0})
sin_ft_a = 0
for r in rows_a:
    ft = str(r.get("fecha_terminado") or "")[:10]
    if not ft:
        sin_ft_a += 1
        continue
    s = by_ft_a[ft]
    s["n"] += 1
    s["cruda"] += float(r.get("tela_cruda_kg") or 0)
    s["term"] += float(r.get("tela_terminada_kg") or 0)
print(f"  Sin fecha_terminado: {sin_ft_a}")
print(f"  Dias unicos en fecha_terminado: {len(by_ft_a)}")
print()
print("  Por fecha_terminado:")
print(f"    {'fecha':12s} {'n':>4s} {'crudo':>12s} {'term':>12s}")
total_a_cruda = total_a_term = 0.0
total_a_n = 0
for ft in sorted(by_ft_a):
    s = by_ft_a[ft]
    print(f"    {ft:12s} {s['n']:>4d} {s['cruda']:>12,.1f} {s['term']:>12,.1f}")
    total_a_cruda += s['cruda']
    total_a_term += s['term']
    total_a_n += s['n']
print(f"    {'TOTAL':12s} {total_a_n:>4d} {total_a_cruda:>12,.1f} {total_a_term:>12,.1f}")

# ============================================================
# Query B — tinturado_resumen filtrando por TERMINADO (lo que tiene la pantalla HOY)
# ============================================================
print()
print("=== B) tinturado_resumen(terminado_desde, terminado_hasta) — VISTA ACTUAL ===")
from modules.tintura import service as tintura_service
desde_d = date.fromisoformat(DESDE)
hasta_d = date.fromisoformat(HASTA)
res_b = tintura_service.tinturado_resumen(
    limite=10000,
    terminado_desde=desde_d,
    terminado_hasta=hasta_d,
)
print(f"  Total ordenes (terminadas en rango): {len(res_b)}")

by_ft_b = defaultdict(lambda: {"n": 0, "cruda": 0.0, "term": 0.0})
for o in res_b:
    ft = o.fecha_terminado.isoformat() if o.fecha_terminado else None
    if not ft:
        continue
    s = by_ft_b[ft]
    s["n"] += 1
    s["cruda"] += float(o.tela_cruda_kg or 0)
    s["term"] += float(o.tela_terminada_kg or 0)

print()
print("  Comparacion A (Excel) vs B (pantalla) por dia:")
print(f"    {'fecha':12s} {'A_n':>4s} {'A_crudo':>10s} {'A_term':>10s}  {'B_n':>4s} {'B_crudo':>10s} {'B_term':>10s}  {'diff_n':>6s} {'diff_crudo':>11s}")
total_b_cruda = total_b_term = 0.0
total_b_n = 0
todas_fechas = sorted(set(by_ft_a) | set(by_ft_b))
for ft in todas_fechas:
    a = by_ft_a.get(ft, {"n": 0, "cruda": 0.0, "term": 0.0})
    b = by_ft_b.get(ft, {"n": 0, "cruda": 0.0, "term": 0.0})
    diff_n = b["n"] - a["n"]
    diff_c = b["cruda"] - a["cruda"]
    marker = "  <<<" if abs(diff_c) > 50 else ""
    print(f"    {ft:12s} {a['n']:>4d} {a['cruda']:>10,.1f} {a['term']:>10,.1f}  {b['n']:>4d} {b['cruda']:>10,.1f} {b['term']:>10,.1f}  {diff_n:>+6d} {diff_c:>+11,.1f}{marker}")
    total_b_cruda += b['cruda']
    total_b_term += b['term']
    total_b_n += b['n']
print(f"    {'TOTAL A':12s} {total_a_n:>4d} {total_a_cruda:>10,.1f} {total_a_term:>10,.1f}")
print(f"    {'TOTAL B':12s} {total_b_n:>4d} {total_b_cruda:>10,.1f} {total_b_term:>10,.1f}")
print(f"    DELTA: n={total_b_n - total_a_n:+d}  crudo={total_b_cruda - total_a_cruda:+,.1f}  term={total_b_term - total_a_term:+,.1f}")

# ============================================================
# Detalle: identificar ordenes que difieren — esto te dice POR QUE
# ============================================================
print()
print("=== DETALLE: ordenes en A pero no en B (Excel tiene, pantalla NO) ===")
ids_a = {r['id'] for r in rows_a if r.get('fecha_terminado')}
ids_b = {o.numero for o in res_b}  # numero es string

# Re-fetch numeros para A
a_by_id = {r['id']: r for r in rows_a}
a_numeros = {a_by_id[i]['numero']: a_by_id[i] for i in ids_a}

solo_en_a = set(a_numeros.keys()) - ids_b
solo_en_b = ids_b - set(a_numeros.keys())
print(f"  Ordenes solo en A: {len(solo_en_a)}")
print(f"  Ordenes solo en B: {len(solo_en_b)}")

if solo_en_a:
    print("  Primeras 10 ordenes que el Excel tiene y la pantalla NO:")
    for n in list(solo_en_a)[:10]:
        r = a_numeros[n]
        print(f"    numero={n} fecha_creacion={r['fecha']} fecha_terminado={r['fecha_terminado']} crudo={r['tela_cruda_kg']} term={r['tela_terminada_kg']}")

if solo_en_b:
    print("  Primeras 10 ordenes que la pantalla tiene y el Excel NO:")
    by_numero_b = {o.numero: o for o in res_b}
    for n in list(solo_en_b)[:10]:
        o = by_numero_b[n]
        print(f"    numero={n} fecha_creacion={o.fecha} fecha_terminado={o.fecha_terminado} crudo={o.tela_cruda_kg} term={o.tela_terminada_kg}")

print()
print("=== fin debug ===")
EOF
)
B64=$(printf '%s' "$PY" | base64 | tr -d '\n')

echo "$B64" > /tmp/ssm_debug_tin_b64.txt
INSTANCE_ID="$INSTANCE_ID" python3 <<'PYEOF' > /tmp/ssm_debug_tin_payload.json
import json, os
with open('/tmp/ssm_debug_tin_b64.txt') as f:
    b64 = f.read().strip()
cmd = (
    "cd C:\\programa-core; "
    "[System.IO.File]::WriteAllBytes('C:\\programa-core\\_tmp_dbg_tin.py', "
    f"[Convert]::FromBase64String('{b64}')); "
    "& 'C:\\Python312\\python.exe' 'C:\\programa-core\\_tmp_dbg_tin.py'; "
    "Remove-Item 'C:\\programa-core\\_tmp_dbg_tin.py'"
)
payload = {
    "InstanceIds": [os.environ['INSTANCE_ID']],
    "DocumentName": "AWS-RunPowerShellScript",
    "Parameters": {"commands": [cmd]},
}
print(json.dumps(payload))
PYEOF

ID=$(aws ssm send-command --region "$REGION" --cli-input-json file:///tmp/ssm_debug_tin_payload.json \
  --query 'Command.CommandId' --output text)
echo "Command ID: $ID"
echo "Esperando ~30s..."
sleep 25

for i in 1 2 3 4 5; do
  STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --instance-id "$INSTANCE_ID" --command-id "$ID" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  if [ "$STATUS" = "Success" ] || [ "$STATUS" = "Failed" ]; then
    break
  fi
  sleep 6
done

echo ""
echo "=== RESULTADO (status=$STATUS) ==="
aws ssm get-command-invocation --region "$REGION" \
  --instance-id "$INSTANCE_ID" --command-id "$ID" \
  --query 'StandardOutputContent' --output text
echo ""
echo "=== ERRORS (si hubo) ==="
aws ssm get-command-invocation --region "$REGION" \
  --instance-id "$INSTANCE_ID" --command-id "$ID" \
  --query 'StandardErrorContent' --output text
