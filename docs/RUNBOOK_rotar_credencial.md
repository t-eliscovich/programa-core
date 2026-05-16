# Runbook — rotar credencial `1n7el4Pyth0n`

Procedimiento para rotar la contraseña del usuario `postgres` (o del usuario de
la app) en RDS antes de que Programa Core llegue a producción.

**Estado:** bloqueado de ejecución por falta de acceso AWS desde el sandbox.
Este runbook está listo para correr manualmente (o en una sesión con acceso a
la AWS console) en menos de 30 minutos.

## Por qué urgente

La vieja app PyQt trae `password=1n7el4Pyth0n` en `config.ini` plaintext. Si
ese archivo circula (repo, email, backup mal configurado), cualquiera con la
IP del RDS tiene acceso completo a la base de producción. **No se puede ir a
producción con esa credencial.**

## Precondiciones

1. Acceso a AWS console (rol IAM con permisos `rds:ModifyDBInstance` y
   `secretsmanager:PutSecretValue` sobre el secret del app).
2. Acceso SSM o CloudShell para conectar a EC2.
3. Un terminal con VPN al RDS para verificar el cambio.
4. Ventana de mantenimiento de ~5 min (la app va a restartear).

## Paso 0 — inventariar dónde está la credencial vieja

Antes de rotar, descubrir todos los lugares que la tienen. Usar el reemplazo
cuando rotemos:

```bash
# En el repo local
cd "/Users/tamaraeliscovich/Documents/Claude/Projects/Programa Core"
grep -rnI "1n7el4Pyth0n" . 2>/dev/null | grep -v .venv | grep -v __pycache__

# En la app PyQt (el origen del leak)
grep -rnI "1n7el4Pyth0n" "/Users/tamaraeliscovich/Documents/INTELA copy/" 2>/dev/null
```

Esperado: aparecen en
- `INTELA.rar` (comprimido; el `config.ini` adentro).
- Posiblemente `INTELA copy/sistema/config.ini` o similar.
- Documentación histórica del proyecto.

El repo de Programa Core **no** debería tenerla — usamos env vars. Si aparece
en código de `Programa Core`, es un bug: reemplazar inmediatamente por
`os.environ["DB_PASSWORD"]`.

## Paso 1 — generar la nueva contraseña

Contraseña fuerte, 32+ chars, sin caracteres que puedan romper escaping en
PowerShell/bash/.env:

```bash
openssl rand -base64 36 | tr -d '/+=' | cut -c1-32
# ejemplo: aZ8kXmN2pQR9vWeY4tUj6cDfBhGkLnMs
```

Guardarla en el clipboard. **No loggearla ni hacer echo en un script.**

## Paso 2 — cambiar en RDS

Opción A (preferida, AWS console):

1. RDS → Databases → `intela-rds` (o el nombre que corresponda).
2. Actions → Modify.
3. New master password → pegar la nueva.
4. Continue → Apply immediately.
5. Esperar ~2 min hasta que el status vuelva a "Available".

Opción B (CLI):

```bash
aws rds modify-db-instance \
    --db-instance-identifier intela-rds \
    --master-user-password "${NEW_PASSWORD}" \
    --apply-immediately \
    --region us-east-2
```

## Paso 3 — actualizar AWS Secrets Manager

El app lee la pwd desde un secret (si aún no, ver paso 3b). Encontrarlo:

```bash
aws secretsmanager list-secrets --region us-east-2 \
    --query "SecretList[?contains(Name, 'intela') || contains(Name, 'programa-core')].Name"
```

Actualizar:

```bash
aws secretsmanager put-secret-value \
    --secret-id intela/programa-core/db \
    --secret-string "${NEW_PASSWORD}" \
    --region us-east-2
```

Si no existe el secret (deploy todavía no lo usa):

```bash
aws secretsmanager create-secret \
    --name intela/programa-core/db \
    --secret-string "${NEW_PASSWORD}" \
    --region us-east-2
```

### Paso 3b — si el app todavía lee `.env` directo en EC2

Conectar por SSM a la EC2 y editar `.env`:

```bash
aws ssm start-session --target i-xxxxxxxxxx --region us-east-2

# en el server
cd C:\programa-core   # o donde esté deployado
notepad .env
# cambiar DB_PASSWORD=1n7el4Pyth0n por DB_PASSWORD=<NEW_PASSWORD>
```

Restartear el Scheduled Task que mantiene Waitress vivo (o el service de
Windows si así está orquestado):

```powershell
Stop-ScheduledTask -TaskName "ProgramaCore"
Start-ScheduledTask -TaskName "ProgramaCore"
```

Ver logs: `tail -f` sobre `C:\programa-core\logs\app.log` y verificar que no
haya líneas con "password authentication failed".

## Paso 4 — rotar también el usuario de `formulas_app` si aplica

`formulas_app` corre en la misma EC2 y comparte RDS. Si usa el mismo usuario
`postgres` (típico en el setup actual), el mismo cambio ya lo afectó. Si tiene
un usuario propio (`formulas_user`), rotarlo en paralelo con el mismo
procedimiento.

Ver el skill `intela-aws-deploy` para los detalles de deploy de `formulas_app`.

## Paso 5 — verificar

Desde un terminal con VPN al RDS:

```bash
# Con la nueva pwd
psql -h intela-rds.xxx.us-east-2.rds.amazonaws.com -U postgres -d postgres \
     -c "SELECT current_user, now();"
# (ingresar NEW_PASSWORD cuando pida)

# Con la vieja — debe FALLAR
PGPASSWORD="1n7el4Pyth0n" psql -h intela-rds.xxx.us-east-2.rds.amazonaws.com \
    -U postgres -d postgres -c "SELECT 1;"
# expected: "password authentication failed for user postgres"
```

Y contra el app:

```bash
curl -I https://programa-core.intela.com.ec/healthz
# expected: 200 OK
```

## Paso 6 — checklist final

- [ ] `psql` con pwd vieja rechaza conexión.
- [ ] `psql` con pwd nueva conecta.
- [ ] `/healthz` del app devuelve 200.
- [ ] `/login` renderiza (el app se levantó bien).
- [ ] Un login de prueba completa el flujo.
- [ ] Logs del app no tienen "password authentication failed" en los últimos
      10 min.
- [ ] Actualizar `docs/CREDENCIALES.md` (crear si no existe) con el ts del
      cambio. Sin la pwd real — sólo "rotado en YYYY-MM-DD por <usuario>".
- [ ] `grep -rnI "1n7el4Pyth0n" .` en el repo devuelve 0 matches.

## Rollback

Si algo sale mal:

```bash
aws rds modify-db-instance \
    --db-instance-identifier intela-rds \
    --master-user-password "1n7el4Pyth0n" \
    --apply-immediately \
    --region us-east-2
```

Pero si necesitaste rollback: investigar *por qué* y hacerlo de nuevo. La pwd
vieja está quemada — sigue circulando.

## Invariantes post-rotación

1. La contraseña nueva vive en Secrets Manager, no en `.env` ni en código.
2. El deploy pipeline lee la pwd del secret al arranque — el restart es lo
   que recoge cambios futuros.
3. Cualquier doc que diga "la pwd es 1n7el4Pyth0n" se actualiza o borra.
4. Próxima rotación: cada 180 días. Agendarla en el cron mensual ampliado
   o en el calendario del equipo.

## Cuándo NO rotar

- Durante el horario operativo de la fábrica (9-17 ET). Esperar a la noche o
  al fin de semana.
- Con una ventana de deploy pendiente — terminar el deploy primero.
- Si no podés probar el app post-rotación. Rollback sin verificar = downtime.

## Registro de rotaciones

| Fecha       | Quién        | Motivo                                   | Pwd rotada a (hint) |
|-------------|--------------|------------------------------------------|---------------------|
| 2026-04-17  | Claude       | bloqueo: pwd histórica en PyQt plain-text | —                   |
| (ejecutar)  | …            | rotación inicial                          | (no se registra)    |
