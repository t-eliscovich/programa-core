#!/usr/bin/env bash
# debug_asinfo_bed_historico.sh — ¿tiene Asinfo las facturas T/X de BED
# que faltan en el DBF?
#
# Corre desde CloudShell. Usa SSM para llamar al bridge Asinfo (Metabase
# card 199 = ASINFO_CARD_FACTURAS) y comparar contra scintela.factura.
#
# Uso:
#   curl -sL https://raw.githubusercontent.com/t-eliscovich/programa-core/main/scripts/debug_asinfo_bed_historico.sh | bash
#
# Output esperado:
#   - Conteo por mes y tipo de factura BED en Asinfo (2024-01 a hoy).
#   - Lo mismo en scintela.factura.
#   - Lista de facturas presentes en Asinfo pero no en PC (=backfill target).

set -euo pipefail

INSTANCE_ID="${INSTANCE_ID:-i-0fcca4d7029f08489}"
REGION="${REGION:-us-east-2}"

echo "=== Diag Asinfo BED histórico — corriendo en EC2 vía SSM ==="

PY=$(cat <<'EOF'
import os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, r'C:\programa-core')
os.chdir(r'C:\programa-core')

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from collections import Counter
from datetime import date

# 1) Asinfo — pedir facturas a Metabase card 199 sin filtro de cliente
#    (la card no acepta filtro cliente_codigo), filtramos en Python.
print('=== ASINFO (Metabase card 199) — BED desde 2024-01-01 ===')
from modules.asinfo import service as asinfo
desde = '2024-01-01'
hasta = date.today().isoformat()
try:
    rows_all = asinfo.facturas_periodo(desde, hasta)
except Exception as e:
    print(f'ERROR llamando asinfo.facturas_periodo: {e!r}')
    rows_all = []

print(f'  Total filas Asinfo en rango {desde}..{hasta}: {len(rows_all)}')
if rows_all:
    sample = rows_all[0]
    print(f'  Columnas: {sorted(sample.keys())}')

# Filtrar a BED — el campo en Asinfo es 'cliente_codigo'.
rows_bed = [r for r in rows_all if str(r.get('cliente_codigo') or '').strip().upper() == 'BED']
print(f'  Filas BED en Asinfo: {len(rows_bed)}')

if rows_bed:
    # Distribución por mes y tipo
    by_mes = Counter()
    by_tipo = Counter()
    by_mes_tipo = Counter()
    for r in rows_bed:
        f = r.get('fecha')
        if hasattr(f, 'isoformat'):
            f = f.isoformat()
        f = str(f)[:7]  # YYYY-MM
        t = str(r.get('tipo') or '?').upper()
        by_mes[f] += 1
        by_tipo[t] += 1
        by_mes_tipo[(f, t)] += 1

    print()
    print('  Por tipo:')
    for t, n in by_tipo.most_common():
        print(f'    {t:18s} n={n}')

    print()
    print('  Por mes (todos los tipos):')
    for m in sorted(by_mes):
        print(f'    {m}  n={by_mes[m]}')

    print()
    print('  Cross mes x tipo (BED, mostrando solo FACTURA y DEVOLUCION):')
    meses = sorted({m for (m, _) in by_mes_tipo})
    tipos_relevantes = ['FACTURA', 'DEVOLUCION', 'NC_FINANCIERA', 'NTEN', 'NCNT']
    hdr = f'    {"mes":8s} ' + ' '.join(f'{t[:8]:>8s}' for t in tipos_relevantes)
    print(hdr)
    for m in meses:
        row = f'    {m:8s} ' + ' '.join(
            f'{by_mes_tipo.get((m, t), 0):>8d}' for t in tipos_relevantes
        )
        print(row)

    # Sample de filas más viejas (las que probablemente faltan en PC)
    print()
    print('  10 facturas BED más antiguas en Asinfo:')
    sorted_rows = sorted(rows_bed, key=lambda r: str(r.get('fecha') or ''))
    for r in sorted_rows[:10]:
        f = r.get('fecha')
        if hasattr(f, 'isoformat'):
            f = f.isoformat()
        print(f"    fecha={f}  tipo={r.get('tipo'):16s}  num={r.get('numero')}  usd={r.get('usd')}")

# 2) PC — distribución BED por mes y stat
print()
print('=== PC (scintela.factura) — BED desde 2024-01-01 ===')
import psycopg2
host = os.environ.get('DB_HOST', '')
port = os.environ.get('DB_PORT', '5432')
name = os.environ.get('DB_NAME', '')
user = os.environ.get('DB_USER', '')
pwd  = os.environ.get('DB_PASSWORD', '')
conn = psycopg2.connect(host=host, port=port, dbname=name, user=user, password=pwd)
conn.autocommit = True
cur = conn.cursor()
cur.execute("""
    SELECT TO_CHAR(fecha, 'YYYY-MM') AS mes,
           COALESCE(NULLIF(TRIM(stat), ''), '(blank)') AS s,
           COUNT(*) AS n
      FROM scintela.factura
     WHERE codigo_cli = 'BED' AND fecha >= '2024-01-01'
     GROUP BY 1, 2
     ORDER BY 1, 2
""")
pc_rows = cur.fetchall()
print(f'  Filas BED en PC desde 2024-01-01: {sum(r[2] for r in pc_rows)}')
print()
print('  Por mes y stat:')
print(f'    {"mes":8s} {"stat":8s} {"n":>6s}')
for mes, s, n in pc_rows:
    print(f'    {mes:8s} {s:8s} {n:>6d}')

# 3) Comparación: meses en Asinfo donde PC tiene 0 (= candidato a backfill)
print()
print('=== BACKFILL TARGET — meses con facturas en Asinfo BED pero no en PC ===')
pc_por_mes = Counter()
for mes, s, n in pc_rows:
    pc_por_mes[mes] += n
asinfo_por_mes = Counter()
for r in rows_bed:
    f = r.get('fecha')
    if hasattr(f, 'isoformat'):
        f = f.isoformat()
    asinfo_por_mes[str(f)[:7]] += 1

todos = sorted(set(asinfo_por_mes) | set(pc_por_mes))
print(f'    {"mes":8s} {"asinfo":>8s} {"pc":>8s} {"falta":>8s}')
total_falta = 0
for m in todos:
    a = asinfo_por_mes.get(m, 0)
    p = pc_por_mes.get(m, 0)
    falta = max(0, a - p)
    total_falta += falta
    flag = '  <-- BACKFILL' if falta > 0 else ''
    print(f'    {m:8s} {a:>8d} {p:>8d} {falta:>8d}{flag}')
print()
print(f'  TOTAL facturas BED a backfillear (estimado): {total_falta}')

conn.close()
print('\n=== fin diag ===')
EOF
)
B64=$(printf '%s' "$PY" | base64 | tr -d '\n')

echo "$B64" > /tmp/ssm_b64.txt
INSTANCE_ID="$INSTANCE_ID" python3 <<'PYEOF' > /tmp/ssm_payload.json
import json, os
with open('/tmp/ssm_b64.txt') as f:
    b64 = f.read().strip()
cmd = (
    "cd C:\\programa-core; "
    "[System.IO.File]::WriteAllBytes('C:\\programa-core\\_tmp_asinfo_diag.py', "
    f"[Convert]::FromBase64String('{b64}')); "
    "& 'C:\\Python312\\python.exe' 'C:\\programa-core\\_tmp_asinfo_diag.py'; "
    "Remove-Item 'C:\\programa-core\\_tmp_asinfo_diag.py'"
)
payload = {
    "InstanceIds": [os.environ['INSTANCE_ID']],
    "DocumentName": "AWS-RunPowerShellScript",
    "Parameters": {"commands": [cmd]},
}
print(json.dumps(payload))
PYEOF

ID=$(aws ssm send-command --region "$REGION" --cli-input-json file:///tmp/ssm_payload.json \
  --query 'Command.CommandId' --output text)

echo "Command ID: $ID"
echo "Esperando (Metabase puede tardar 20-40s en facturas 2024+)..."
sleep 25

for i in 1 2 3 4 5 6 7 8; do
  STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --instance-id "$INSTANCE_ID" --command-id "$ID" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  if [ "$STATUS" = "Success" ] || [ "$STATUS" = "Failed" ]; then
    break
  fi
  sleep 8
done

echo ""
echo "=== RESULTADO (status=$STATUS) ==="
aws ssm get-command-invocation --region "$REGION" \
  --instance-id "$INSTANCE_ID" --command-id "$ID" \
  --query 'StandardOutputContent' --output text
echo ""
echo "=== ERRORS ==="
aws ssm get-command-invocation --region "$REGION" \
  --instance-id "$INSTANCE_ID" --command-id "$ID" \
  --query 'StandardErrorContent' --output text
