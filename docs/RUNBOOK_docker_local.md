# Runbook — levantar Programa Core local con Docker

Procedimiento para arrancar el app end-to-end en tu Mac, sin tocar Postgres
local ni Homebrew. Útil para:
- Probar que un deploy está sano antes de shippear a EC2.
- Onboardear a alguien nuevo sin configurar su máquina.
- Reproducir un bug visto en producción.

## Prerequisitos

- Docker Desktop instalado y corriendo. (Pruebalo con `docker ps` — si responde
  con una tabla vacía, todo bien.)
- El repo clonado en `~/Documents/Claude/Projects/Programa Core`.

No hace falta Postgres instalado, ni Python, ni nada más.

## Paso 1 — build y up

```bash
cd "~/Documents/Claude/Projects/Programa Core"

# Build de la imagen + arranque de db + app. La primera vez tarda 3-5 min
# (descarga postgres:16-alpine y python:3.11-slim + instala deps).
docker compose up -d
```

Esperá a que los healthchecks pasen:

```bash
docker compose ps
# Buscá "healthy" en la columna STATUS de ambos servicios.
```

## Paso 2 — aplicar migraciones

```bash
docker compose exec app python scripts/migrate.py
```

Salida esperada: lista de migraciones `0001..0009` aplicadas + "OK".

## Paso 3 — crear primer admin

```bash
docker compose exec app python scripts/seed_roles.py
```

Va a pedir username y password interactivamente. Setear algo con al menos 10
chars + una letra + un número (la política de password del app).

Alternativa no interactiva:

```bash
docker compose exec -e INTELA_ADMIN_USER=admin \
                    -e INTELA_ADMIN_PASSWORD='DevLocal2026!' \
                    app python scripts/seed_roles.py
```

## Paso 4 — verificar

```bash
# Liveness
curl -fsS http://localhost:5050/healthz
# expected: {"status":"ok","ts":"..."}

# Readiness (toca DB)
curl -fsS http://localhost:5050/healthz/ready
# expected: {"status":"ok","db":"connected","latency_ms":...,"ts":"..."}
```

Abrir `http://localhost:5050/login` en el browser. Loguearse con las credenciales
que seteaste en el paso 3. Debe redirigir al dashboard del Dueño.

## Paso 5 — correr los tests dentro del container

```bash
docker compose run --rm test
# expected: pytest + coverage report. Los tests @pytest.mark.db usan el
#           Postgres del compose porque DB_HOST=db ya viene configurado.
```

Para correr sólo los tests `@pytest.mark.db`:

```bash
docker compose run --rm \
    -e DB_HOST=db \
    -e DB_NAME=programa_core \
    -e DB_USER=app \
    -e DB_PASSWORD=app_local_only \
    test python -m pytest -m db -q
```

## Paso 6 — logs

```bash
docker compose logs -f app          # seguir logs del app
docker compose logs -f db           # logs del Postgres
docker compose logs --tail=50 app   # últimas 50 líneas
```

## Parar todo

```bash
# Sin borrar datos
docker compose down

# Borrando el volumen de la DB (empezar limpio la próxima vez)
docker compose down -v
```

## Troubleshooting

**"port 5050 already in use":** ya tenés el launcher.sh corriendo. Pará con
`pkill -f waitress` o cambiá el puerto en docker-compose.yml (`5051:5050`).

**"pg_isready timeout":** el container de db tardó más que el healthcheck.
Subí `retries` a 20 en `docker-compose.yml` o esperá unos segundos y re-levantá.

**"SECRET_KEY no está configurada":** el compose tiene uno de dev hardcodeado.
Si lo borraste, poné uno en env:

```bash
SECRET_KEY=$(openssl rand -base64 48) docker compose up -d
```

**Querés entrar al container:**

```bash
docker compose exec app bash
# Ya estás dentro, podés correr psql, python, etc.
```

## Diferencias con producción

- En prod, la DB es RDS en us-east-2, no el container local.
- En prod, `SECRET_KEY` viene de AWS Secrets Manager, no de env var en plano.
- En prod, el app corre bajo Waitress como Scheduled Task de Windows, no docker
  (ver skill `intela-aws-deploy`).

Ese compose es para dev local — no es la topología de prod.
