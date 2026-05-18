# Deploy en producción — Programa Core (AWS EC2)

> Fecha de instalación inicial: **2026-05-17**.

Programa Core corre como webapp HTTPS en `https://programa.intela.com.ec`, accesible públicamente con login restringido a 2 emails via Google OAuth. Comparte el mismo EC2 con `formulas_app` (port 5001) y Metabase (port 3000) sin colisión.

## Arquitectura

| Pieza | Valor |
|---|---|
| Dominio | `programa.intela.com.ec` |
| TLS | Caddy (`C:\caddy\`) + Let's Encrypt automático |
| Reverse proxy | Caddy escucha 443 → forwardea a `localhost:5002` |
| App | Flask + Waitress en port **5002** |
| EC2 | `i-0fcca4d7029f08489` Windows Server 2019, us-east-2 |
| Python | `C:\Python312\python.exe` (sin venv, deps globales) |
| RDS | PostgreSQL 16, database **`intela`** (separada de `postgres`/`metabase` del mismo cluster) |
| Scheduled Task | `ProgramaCoreApp` corre como SYSTEM |
| Deploy | GitHub Actions OIDC → S3 → SSM (push a `main` deploya en ~1 min) |
| Auth | Google OAuth allowlist hardcodeada de 2 emails (`teliscovich@gmail.com`, `feliscovich@gmail.com`) |
| Costo extra | **$0/mes** (reuso de infra de formulas) |

## Cómo hacer un cambio y verlo en prod

1. Hacés tu cambio local en código.
2. `git push origin main` desde tu Mac.
3. GitHub Actions corre dos workflows en paralelo:
   - **CI** — lint + tests + docker-build (~1 min)
   - **Deploy to EC2** — empaqueta, sube a S3, manda SSM al EC2 que descarga + extrae + `pip install` + restart task + healthcheck (~1 min)
4. En ~2 min total, el cambio está en `https://programa.intela.com.ec`.

Si el deploy falla, la versión vieja sigue corriendo (el SSM falla antes de tocar nada, o el restart deja la versión vieja). Ver logs del run en https://github.com/t-eliscovich/programa-core/actions.

## Estructura en el server

```
C:\
├── programa-core\          ← código de la app (sincronizado con main)
│   ├── .env                ← env vars de producción (NO está en el repo)
│   ├── app.py, run.py, ...
│   └── ...
├── caddy\                  ← TLS reverse proxy
│   ├── caddy.exe
│   ├── Caddyfile
│   └── data\               ← certificados Let's Encrypt
├── Python312\              ← Python global (compartido con formulas + provisiones)
├── pg17\                   ← PostgreSQL 17 client tools (pg_restore para dumps de pg17)
├── pg16\                   ← PostgreSQL 16 client tools (legacy)
└── tmp\                    ← scratch para dumps, scripts SSM, etc.
```

Coexistencia con otros servicios en el mismo EC2:
```
C:\formulas_app\     ← formulas, port 5001
C:\metabase\         ← Metabase, port 3000
C:\kilos-proxy\      ← proxy interno para Metabase, port 8080
```

## Environment variables — `C:\programa-core\.env`

```
FLASK_ENV=production
FLASK_DEBUG=0

# DB
DB_HOST=intela-db.c988ucsko537.us-east-2.rds.amazonaws.com
DB_PORT=5432
DB_NAME=intela
DB_USER=postgres
DB_PASSWORD=...
DB_POOL_MIN=1
DB_POOL_MAX=10

# Seguridad
SECRET_KEY=...                                  # backup en Machine env: PROGRAMA_CORE_SECRET_KEY
SESSION_COOKIE_SECURE=1
APP_BASE_URL=https://programa.intela.com.ec

# Google OAuth
GOOGLE_CLIENT_ID=171015031963-qfpbpq49q8b1t7gp40jrfei6pe1s6eiu.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...                 # backup en SSM Parameter Store SecureString
```

El `.env` lo escribe el script de bootstrap (`set_env_vars.ps1`). Cambiarlo a mano via SSM es OK pero hay que `Restart-ScheduledTask -TaskName ProgramaCoreApp` para que tome efecto.

## Google OAuth

- Proyecto GCP: `programa-core` (Project number `171015031963`)
- Modo **Testing** (no requiere verificación de Google)
- Test users: `teliscovich@gmail.com`, `feliscovich@gmail.com` (alineado con el allowlist hardcodeado)
- Authorized JavaScript origins: `https://programa.intela.com.ec`
- Authorized redirect URI: `https://programa.intela.com.ec/auth/google/callback`

**Defense in depth:** la allowlist está hardcodeada en `modules/auth_google/views.py` (`_DEFAULT_ALLOWLIST`). Aunque alguien arregle el GCP para dejar entrar más usuarios, el callback rebota con 403 si el email no está en esa constante.

Primera vez que un test user entra Google muestra "This app isn't verified" — click **Advanced** → **Go to Programa Core (unsafe)**. Esto es esperable en modo Testing. Pasarse a "In production" requiere verificación de Google (no hace falta para 2 usuarios).

Para agregar un 3er usuario: editar `_DEFAULT_ALLOWLIST` en el código **Y** agregar el email en GCP → Audience → Test users.

## Caddyfile

`C:\caddy\Caddyfile`:

```
{
    email teliscovich@gmail.com
    storage file_system C:/caddy/data
}

metabase.intela.com.ec {
    @kilos path /kilos-hoy
    reverse_proxy @kilos localhost:8080
    reverse_proxy localhost:3000
}

programa.intela.com.ec {
    reverse_proxy localhost:5002
    encode gzip
}
```

Después de editar: `cd C:\caddy && .\caddy.exe reload --config Caddyfile`. Caddy pide el cert Let's Encrypt automáticamente la primera vez que llega tráfico HTTPS al hostname.

## GitHub Actions deploy pipeline

- Workflow: `.github/workflows/deploy.yml`
- IAM role en AWS: `arn:aws:iam::743978811761:role/programa-core-deploy`
- Trust policy: permite asumir el role a cualquier workflow del repo `t-eliscovich/programa-core` via OIDC
- Permisos del role: `s3:PutObject` al folder `programa_core_deploy/` del bucket de deploy + `ssm:SendCommand` al instance EC2 (mínimos)
- Secret en GitHub: `AWS_DEPLOY_ROLE_ARN` (el ARN de arriba)
- **No PATs ni access keys** — todo via OIDC

Disparar manualmente: `gh workflow run "Deploy to EC2" --ref main`, o desde la UI de Actions click "Run workflow".

## Base de datos — restore desde dump local

El dump que usamos en producción es el local de Tamara (no el `intela12042026.sql` viejo). Pattern para refrescar prod con el estado actual de tu DB local:

```bash
# En la Mac de Tamara
PGPASSWORD=postgres pg_dump -h localhost -U postgres -F c \
    --exclude-table='public.catalogo' \
    -f ~/intela_actual.sql intela
# --exclude-table porque postgres NO es owner de catalogo (la creó otro user).
```

Subir a CloudShell via Actions → Upload file. Después en CloudShell:

```bash
aws s3 cp ~/intela_actual.sql s3://intela-deploy-743978811761/intela_actual.sql
DUMP_URL=$(aws s3 presign s3://intela-deploy-743978811761/intela_actual.sql --expires-in 3600 --region us-east-2)
```

En el EC2 via SSM, restore con pg17:

```powershell
$env:PGPASSWORD = $dbPass
& C:\pg17\bin\pg_restore.exe -h $dbHost -p $dbPort -U $dbUser -d intela `
    --no-owner --no-privileges --no-acl C:\tmp\dump.sql
```

**Importante:** usar `C:\pg17\bin\pg_restore.exe` (no el de pg16) porque el header v1.16 de dumps generados con pg17 NO lo entiende pg_restore 16.

Si la DB ya tiene tablas previas, dropear primero los schemas:

```sql
DROP SCHEMA IF EXISTS scintela CASCADE;
DROP SCHEMA IF EXISTS seguridad CASCADE;
-- NO dropear public (donde vive catalogo del dump viejo)
```

## Migrations

`scripts/migrate.py` en el repo. Aplica `migrations/*.sql` en orden, rastreando aplicadas en `seguridad.migraciones_aplicadas`. Idempotente — re-correr no rompe.

**Importante:** el deploy de GitHub Actions NO corre migrations automáticamente (decisión consciente — evitar accidental DDL en prod). Después de un cambio de schema, correr a mano:

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name AWS-RunPowerShellScript \
  --parameters 'commands=["$env:PYTHONIOENCODING=\"utf-8\"; Set-Location C:\\programa-core; C:\\Python312\\python.exe scripts\\migrate.py"]'
```

`PYTHONIOENCODING=utf-8` es necesario porque `migrate.py` printea caracteres unicode (`→`) que crashean el cp1252 default de Windows console.

## Smoke test después de un deploy

Desde CloudShell:

```bash
aws ssm send-command --region us-east-2 --instance-ids i-0fcca4d7029f08489 \
  --document-name AWS-RunPowerShellScript \
  --parameters 'commands=["(Get-ScheduledTask -TaskName ProgramaCoreApp).State; (Invoke-WebRequest -UseBasicParsing http://localhost:5002/login -TimeoutSec 10).StatusCode"]'
```

Espera `Running` y `200`.

Externamente:

```bash
curl -sI https://programa.intela.com.ec/login | head -3
```

Espera `HTTP/2 200`.

## Cómo NO confundir Programa Core con formulas_app

Comparten EC2 + RDS pero todo lo user-facing está separado a propósito:

| Dimensión | formulas | Programa Core |
|---|---|---|
| URL | `http://3.19.22.125:5001` (solo desde fábrica, sin TLS) | `https://programa.intela.com.ec` |
| Login | user/pass + 2FA propio | Google OAuth (1 botón) |
| DB | `postgres` | `intela` |
| Folder | `C:\formulas_app\` | `C:\programa-core\` |
| Task | `FormulasApp` | `ProgramaCoreApp` |
| Port | 5001 | 5002 |
| Repo | otro | `t-eliscovich/programa-core` |
| Env vars | `DATABASE_URL` (URL completa, Machine) | `DB_HOST` + `DB_NAME` + `DB_USER` + `DB_PASSWORD` separados (en `.env`) |

Los nombres de env vars no se solapan — un deploy de Programa Core nunca puede pisar la config de formulas y viceversa.

## Gotchas que ya nos pegaron (read antes de tocar algo)

- **Dump SQL formato custom:** un dump `pg_dump -F c` (binario) tiene header "PGDMP" + version. NO se puede restaurar con `psycopg2.execute(sql)` — necesita `pg_restore`.
- **Versión de pg_restore:** dump generado con pg_dump 17 tiene header v1.16. pg_restore 16 NO lo entiende. Por eso `C:\pg17\` existe.
- **Encoding del dump del 12-Abril:** el `intela12042026.sql` legacy estaba en cp1252/latin-1, no UTF-8. Al leerlo desde Python, hay que tratar de varios encodings.
- **`public.catalogo`:** Tamara no es owner de esa tabla en local — la creó otro user. Por eso el dump usa `--exclude-table=public.catalogo`. La tabla en producción quedó del dump viejo del 12-Abril.
- **Drop schemas antes de restore:** si la DB tiene tablas viejas, `pg_restore` choca con constraints/triggers. Drop `scintela` + `seguridad` CASCADE antes.
- **`.env` NO va en el tarball del deploy** — el workflow excluye varios patrones; deliberadamente el `.env` vive solo en el server. Si se pierde, regenerar con el script de bootstrap.
- **Zoom de body en `templates/base.html`** — está en `1.0`. Inicialmente era `1.18` y causaba scroll horizontal. Si en el futuro pedís ajustar el tamaño general, ese es el único número a tocar.
- **`scripts/migrate.py` con encoding:** el script printea `→` (flecha unicode); en Windows console default (cp1252) crashea. Setear `PYTHONIOENCODING=utf-8` antes de correrlo via SSM.

## Cómo agregar un 3er usuario al sistema

1. En el repo: editar `modules/auth_google/views.py`, agregar email al set `_DEFAULT_ALLOWLIST`. Commit + push (deploya solo).
2. En Google Cloud Console: `programa-core` → Audience → Test users → Add users → agregar el email.
3. Pedirle al usuario que entre a `https://programa.intela.com.ec` y se loguee con Google. La primera vez el callback hace upsert en `seguridad.usuario` con role Dueño/Administrador automáticamente.

## Rollback

Si un deploy rompe algo y necesitás volver atrás:

```bash
# Desde tu Mac
git revert HEAD --no-edit
git push origin main
# Espera ~2 min; GitHub Actions deploya la versión anterior.
```

O manualmente reset a un commit específico:

```bash
git reset --hard <commit-sha>
git push --force origin main
```

(Cuidado con `--force` si estás colaborando con alguien; no problema si trabajás sola.)
