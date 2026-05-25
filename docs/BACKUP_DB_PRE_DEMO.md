# Backup DB pre-demo Intela (instrucciones)

**Cuándo:** la mañana de la demo, antes de empezar.

## Opción 1 — Snapshot manual de RDS (recomendado, 1 click)

1. Abrí AWS Console → RDS → Databases.
2. Seleccioná la instance Postgres del programa.
3. Click **"Actions" → "Take snapshot"**.
4. Nombre: `pre-demo-intela-2026-05-26`.
5. Click "Take snapshot". Tarda 1-3 min.

Listo. Si pasa algo malo en la demo, podés restaurar a este snapshot en
~10 min (Actions → Restore snapshot).

## Opción 2 — Dump SQL via CloudShell (más portable)

Desde AWS CloudShell (mismo region us-east-2):

```bash
# Configurar credenciales (ya están si ya hiciste deploys)
export PGHOST=<el-endpoint-de-RDS>
export PGUSER=programa_core
export PGPASSWORD=<la-password>
export PGDATABASE=programa_core

pg_dump --schema=scintela --no-owner --no-acl \
  --file=pre-demo-2026-05-26.sql \
  programa_core

# Subir a S3 para tenerlo a mano
aws s3 cp pre-demo-2026-05-26.sql s3://intela-deploys/backups/
```

## Opción 3 — Automatic RDS backups (ya activo)

RDS hace backups automáticos cada día. Verificalo en:
- AWS Console → RDS → tu instance → Backup tab
- Debería decir "Automated backups: Enabled" con retention ≥ 7 days

Esos backups quedan disponibles para "point-in-time restore" cualquier
segundo del últimos 7 días.

## En caso de emergencia durante la demo

Si algo se rompe y necesitás restaurar:
1. AWS Console → RDS → Actions → Restore snapshot.
2. Elegí "pre-demo-intela-2026-05-26".
3. Restaurar a una NEW instance (10 min).
4. Cambiar el connection string del Windows server al new endpoint.
5. Restartear el Waitress service.

Esto te toma ~20 min total. Para una demo, mejor tener el snapshot a mano.
