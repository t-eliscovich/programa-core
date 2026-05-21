# Deploy del bridge de integraciones (formulas_app + Asinfo)

Pasos exactos para activar el bridge en producción (EC2). Cada comando es
copy-paste directo a **CloudShell** (la consola web de AWS).

**Pre-requisitos** que asumimos ya están en EC2:
- `DATABASE_URL` machine env var apunta a la RDS de producción.
- Metabase corre en `http://localhost:3000` (mismo box).
- Programa Core deployado en `C:\programa-core\` con el Scheduled Task `ProgramaCore`.

> Si alguno no se cumple, ver `intela-aws-deploy` SKILL primero.

---

## Paso 1 — Push del código a EC2

Si todavía no pusheaste los cambios de esta tanda (modules/_lib, modules/tintura,
modules/asinfo, modules/healthz/integraciones, scripts/setup_formulas_reader.py):

```bash
# en tu Mac
cd /Users/tamaraeliscovich/Documents/Claude/Projects/Programa\ Core
git add -A
git commit -m "Bridge integraciones: formulas_app + Asinfo (read-only)"
git push origin main
```

GitHub Actions deploya a `C:\programa-core\` automáticamente. Esperá que termine
(check el Actions tab en GitHub) antes de seguir.

---

## Paso 2 — Crear el rol `programa_core_reader` en la RDS

RDS es privada, solo se llega desde el EC2. Corremos el script vía SSM:

```bash
# en CloudShell
export AWS_PAGER=""

CMD=$(aws ssm send-command \
  --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable(\"DATABASE_URL\",\"Machine\"); C:\\Python312\\python.exe C:\\programa-core\\scripts\\setup_formulas_reader.py"]' \
  --query 'Command.CommandId' --output text)

echo "Command ID: $CMD"
sleep 6

aws ssm get-command-invocation --region us-east-2 \
  --instance-id i-0fcca4d7029f08489 --command-id $CMD \
  --query '{Status:Status,Out:StandardOutputContent,Err:StandardErrorContent}'
```

Output esperado: `Status: Success` y en `Out` vas a ver:

```
==============================================================================
ROL programa_core_reader (re)creado. Connection string:

FORMULAS_DATABASE_URL=postgresql://programa_core_reader:<password-aleatoria>@intela-db.c988ucsko537.us-east-2.rds.amazonaws.com:5432/postgres?sslmode=require

Pasos siguientes:
  1) Setear esta var a nivel Machine en el EC2:
     ...
  2) Reiniciar el Scheduled Task ProgramaCore para que la lea.
==============================================================================
```

**Copiá la connection string completa** (la línea que arranca con `postgresql://`).
La vas a usar en el paso 3.

---

## Paso 3 — Setear las env vars a nivel Machine en EC2

Reemplazá `<connection-string-del-paso-2>`, `<metabase-user>`, `<metabase-password>`
en el comando de abajo:

```bash
# en CloudShell
FORMULAS_URL='<connection-string-del-paso-2>'
MB_USER='<metabase-user>'
MB_PASS='<metabase-password>'

aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters "commands=[\"[Environment]::SetEnvironmentVariable('FORMULAS_DATABASE_URL', '$FORMULAS_URL', 'Machine'); [Environment]::SetEnvironmentVariable('METABASE_URL', 'http://localhost:3000', 'Machine'); [Environment]::SetEnvironmentVariable('METABASE_USERNAME', '$MB_USER', 'Machine'); [Environment]::SetEnvironmentVariable('METABASE_PASSWORD', '$MB_PASS', 'Machine'); [Environment]::SetEnvironmentVariable('ASINFO_CARD_VENDEDOR_USD', '116', 'Machine'); [Environment]::SetEnvironmentVariable('ASINFO_CARD_VENDEDOR_KG', '163', 'Machine'); [Environment]::SetEnvironmentVariable('ASINFO_CARD_CLIENTE_KG', '164', 'Machine')\"]" \
  --query 'Command.CommandId' --output text
```

> **Nota**: dejamos `COSTOS_OT_ADAPTER` sin setear (default = `fake`). El
> `PostgresAdapter` de `costos_ot` está deshabilitado igualmente (formulas_app
> no tiene clientes — ver SKILL `programa-core-integraciones`).

Verificá que se setearon (sin mostrar passwords):

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["[Environment]::GetEnvironmentVariable(\"FORMULAS_DATABASE_URL\",\"Machine\") -ne $null; [Environment]::GetEnvironmentVariable(\"METABASE_URL\",\"Machine\"); [Environment]::GetEnvironmentVariable(\"ASINFO_CARD_VENDEDOR_USD\",\"Machine\")"]' \
  --query 'Command.CommandId' --output text
# Después: aws ssm get-command-invocation ... como en el paso 2
```

Esperás:
```
True
http://localhost:3000
116
```

---

## Paso 4 — Restart del Scheduled Task ProgramaCore

Para que los nuevos `os.environ` se carguen, el proceso Python tiene que reiniciarse:

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["Stop-ScheduledTask -TaskName ProgramaCore; Start-Sleep 3; Start-ScheduledTask -TaskName ProgramaCore; Start-Sleep 5; (Get-ScheduledTask -TaskName ProgramaCore).State"]' \
  --query 'Command.CommandId' --output text
# Después: get-command-invocation. Esperá `Running` en la última línea.
```

---

## Paso 5 — Smoke test: `/healthz/integraciones`

Desde una IP allowlisted (tu IP en la fábrica, o agregada temporalmente a la SG):

```bash
# Reemplazá <ec2-public-ip-o-dns> con el endpoint real de Programa Core.
curl -s http://<ec2-public-ip-o-dns>:5050/healthz/integraciones | python3 -m json.tool
```

Output esperado:

```json
{
    "ts": "2026-05-21T...",
    "formulas_app": {
        "configured": true,
        "reachable": true,
        "latency_ms": 8.4
    },
    "metabase": {
        "configured": true,
        "reachable": true,
        "latency_ms": 142.7
    }
}
```

Diagnóstico si algo está mal:

| Caso | Significa |
|---|---|
| `formulas_app.configured: false` | `FORMULAS_DATABASE_URL` no se cargó. Reiniciá el Scheduled Task (paso 4). |
| `formulas_app.reachable: false` | El rol existe pero hay un problema de red o de SSL. Re-correr el setup (paso 2) y verificar que `sslmode=require` esté en la URL. |
| `metabase.configured: false` | Falta alguna de las 3 env vars de Metabase. |
| `metabase.reachable: false` | Credentials inválidos. Probá `curl http://localhost:3000/api/session -d '{"username":"...","password":"..."}'` directo en el server. |

---

## Paso 6 — Smoke test funcional (opcional, recomendado)

Una vez que el healthcheck está verde, podés disparar las funciones del bridge
desde un Python shell en EC2 para confirmar que la data sale:

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["cd C:\\programa-core; $env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable(\"DATABASE_URL\",\"Machine\"); $env:FORMULAS_DATABASE_URL = [System.Environment]::GetEnvironmentVariable(\"FORMULAS_DATABASE_URL\",\"Machine\"); C:\\Python312\\python.exe -c \"from modules._lib import formulas_db; formulas_db.init_pool(); from modules.tintura import service; rows = service.tinturado_resumen(limite=3); print(len(rows), \\\"órdenes traídas\\\"); [print(r.numero, r.fecha, r.tela_cruda_kg, r.tela_terminada_kg) for r in rows]\""]'
```

Esperás 3 órdenes con sus números, fechas y kilos. Si devuelve `0 órdenes traídas`
y `healthcheck` es verde, la tabla `ordenes` está vacía o el rol no tiene SELECT
sobre ella — agregar al `EXPOSED_TABLES` en `scripts/setup_formulas_reader.py` y re-correr.

---

## Rollback (si algo se rompe)

El bridge degrada solo (`disponible()=False`, retorna `[]`) cuando las env vars no
están. Para desactivarlo:

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["[Environment]::SetEnvironmentVariable(\"FORMULAS_DATABASE_URL\", $null, \"Machine\"); Stop-ScheduledTask -TaskName ProgramaCore; Start-Sleep 2; Start-ScheduledTask -TaskName ProgramaCore"]'
```

Después podés borrar el rol de la RDS si querés (no es necesario — está limitado
a SELECT en tablas específicas):

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name "AWS-RunPowerShellScript" \
  --parameters 'commands=["$env:DATABASE_URL = [System.Environment]::GetEnvironmentVariable(\"DATABASE_URL\",\"Machine\"); C:\\Python312\\python.exe -c \"import os, psycopg2; from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT; from urllib.parse import urlparse; p = urlparse(os.environ[\\\"DATABASE_URL\\\"]); c = psycopg2.connect(host=p.hostname, port=p.port, user=p.username, password=p.password, dbname=\\\"postgres\\\"); c.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT); cur=c.cursor(); cur.execute(\\\"DROP OWNED BY programa_core_reader CASCADE\\\"); cur.execute(\\\"DROP ROLE IF EXISTS programa_core_reader\\\"); print(\\\"rol borrado\\\")\""]'
```

---

## Rotación de credenciales (cada cierto tiempo)

**Password del rol Postgres**: re-correr el paso 2 (es idempotente: `DROP ROLE` +
`CREATE ROLE` con nueva password). Después re-hacer paso 3 con la nueva URL y
paso 4 para que el restart la tome.

**Password de Metabase**: Admin → People → editar al usuario en la UI de Metabase.
Después actualizar `METABASE_PASSWORD` Machine var (paso 3 con el resto vacío) +
restart (paso 4).
