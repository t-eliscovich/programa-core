#!/usr/bin/env bash
# debug_facturas_stat.sh — diag de facturas por stat (especialmente BED).
# Corre desde CloudShell. Usa SSM para hacer queries en RDS via EC2.
#
# Uso:
#   curl -sL https://raw.githubusercontent.com/t-eliscovich/programa-core/main/scripts/debug_facturas_stat.sh | bash
#
# Output: distribución de stats globalmente + para cliente BED + fechas
# min/max por stat. Detecta si faltan T/X y de qué rango de fechas.

set -euo pipefail

INSTANCE_ID="${INSTANCE_ID:-i-0fcca4d7029f08489}"
REGION="${REGION:-us-east-2}"

echo "=== Diag facturas por stat — corriendo en EC2 vía SSM ==="

PY=$(cat <<'EOF'
import os, sys
url = os.environ.get('DATABASE_URL', '')
if (not url) or url.startswith('postgresql://localhost'):
    print('REFUSING — DATABASE_URL no apunta a RDS')
    sys.exit(1)
import psycopg2
conn = psycopg2.connect(url)
cur = conn.cursor()

print('=== Distribución global stats en scintela.factura ===')
cur.execute("""
    SELECT COALESCE(NULLIF(TRIM(stat), ''), '(blank)') AS s,
           COUNT(*) AS n,
           COALESCE(SUM(importe), 0)::float AS imp,
           MIN(fecha), MAX(fecha)
      FROM scintela.factura
     GROUP BY 1 ORDER BY 2 DESC
""")
for r in cur.fetchall():
    print(f'  stat={r[0]:10s} n={r[1]:>6d}  $ {r[2]:>14,.2f}  fechas: {r[3]} → {r[4]}')

print()
print('=== BED por stat ===')
cur.execute("""
    SELECT COALESCE(NULLIF(TRIM(stat), ''), '(blank)') AS s,
           COUNT(*) AS n,
           COALESCE(SUM(importe), 0)::float AS imp,
           MIN(fecha), MAX(fecha)
      FROM scintela.factura
     WHERE codigo_cli = 'BED'
     GROUP BY 1 ORDER BY 2 DESC
""")
for r in cur.fetchall():
    print(f'  stat={r[0]:10s} n={r[1]:>6d}  $ {r[2]:>14,.2f}  fechas: {r[3]} → {r[4]}')

print()
print('=== BED ÚLTIMAS 10 facturas (cualquier stat) ===')
cur.execute("""
    SELECT id_factura, numf, fecha, stat, importe, saldo
      FROM scintela.factura
     WHERE codigo_cli = 'BED'
     ORDER BY fecha DESC, id_factura DESC
     LIMIT 10
""")
for r in cur.fetchall():
    print(f'  id={r[0]:>6d} numf={r[1]:>8} fecha={r[2]} stat={(r[3] or "(blank)"):>5}  imp $ {float(r[4]):>10,.2f}  saldo $ {float(r[5] or 0):>10,.2f}')

print()
print('=== Cuenta de stats T y X por cliente top ===')
cur.execute("""
    SELECT codigo_cli, stat, COUNT(*) AS n
      FROM scintela.factura
     WHERE stat IN ('T', 'X')
     GROUP BY codigo_cli, stat
     ORDER BY n DESC
     LIMIT 20
""")
print('  (top 20 clientes con facturas T o X)')
for r in cur.fetchall():
    print(f'  cli={r[0]:5s} stat={r[1]} n={r[2]}')

print()
print('=== Historial — ¿hubo facturas BED stat=T alguna vez? ===')
cur.execute("""
    SELECT COUNT(*) FROM scintela.historia WHERE codigo_cli = 'BED'
""")
n_hist = cur.fetchone()[0]
print(f'  scintela.historia (snapshots) tiene {n_hist} filas para BED')

conn.close()
print('\n=== fin diag ===')
EOF
)
B64=$(printf '%s' "$PY" | base64 | tr -d '\n')

# Escribir el comando PowerShell a un tmp y armar payload JSON robusto via Python.
echo "$B64" > /tmp/ssm_b64.txt
INSTANCE_ID="$INSTANCE_ID" python3 <<'PYEOF' > /tmp/ssm_payload.json
import json, os
with open('/tmp/ssm_b64.txt') as f:
    b64 = f.read().strip()
cmd = (
    "cd C:\\programa-core; "
    "$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable('DATABASE_URL', 'Machine'); "
    f"[System.IO.File]::WriteAllBytes('C:\\programa-core\\_tmp_diag.py', [Convert]::FromBase64String('{b64}')); "
    "& 'C:\\Python312\\python.exe' 'C:\\programa-core\\_tmp_diag.py'; "
    "Remove-Item 'C:\\programa-core\\_tmp_diag.py'"
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
echo "Esperando..."
sleep 12

for i in 1 2 3 4 5; do
  STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --instance-id "$INSTANCE_ID" --command-id "$ID" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  if [ "$STATUS" = "Success" ] || [ "$STATUS" = "Failed" ]; then
    break
  fi
  sleep 5
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
