# Backup diario rotativo — 7 días

Objetivo: backup automático cada día, mantener los últimos 7, ir borrando
los más viejos. Cero intervención manual.

## Opción A — RDS Automated Backups (recomendado, ya viene incluido)

**Una vez, en AWS Console:**

1. RDS → Databases → seleccionar la instance.
2. **Modify** → buscar sección "Backup":
   - **Backup retention period**: `7 days`
   - **Backup window**: `06:00-07:00 UTC` (= 01:00-02:00 hora Ecuador, app dormida)
   - **Enable automated backups**: ✅
3. Apply immediately → Continue.

**Listo.** RDS:
- Hace un snapshot full cada noche
- Borra automáticamente el 8º día atrás
- También hace **WAL streaming continuo** → podés restaurar a CUALQUIER segundo de los últimos 7 días (no solo a las 06:00).

**Ventajas:**
- Cero código, cero scripts.
- Restore por consola en 10-15 min.
- Lo hace AWS, no podés olvidarte ni romper el cron.

**Limitación:**
- Los snapshots son internos de RDS, no `.sql` portables. Si querés un archivo
  para llevarte en un USB o restaurar en otro motor, usá Opción B además.

---

## Opción B — pg_dump diario en el server Windows con rotación por día de semana

Aprovechá que el Windows server ya corre Scheduled Tasks. Agregamos uno más
que hace `pg_dump` y lo guarda con el nombre del día (`lunes.sql`, `martes.sql`,
etc.). El backup del próximo lunes pisa al `lunes.sql` viejo → rotación
automática en 7 días sin tener que borrar nada.

### 1. Crear el script PowerShell

Guardar en el server Windows como `C:\formulas_app\backup_db.ps1`:

```powershell
# Backup diario de programa_core con rotación por día de semana.
# Genera C:\formulas_app\backups\<día>.sql y pisa el de la semana pasada.

$ErrorActionPreference = "Stop"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Día de la semana en español (lunes/martes/...)
$dias = @{
    'Monday'='lunes'; 'Tuesday'='martes'; 'Wednesday'='miercoles';
    'Thursday'='jueves'; 'Friday'='viernes'; 'Saturday'='sabado'; 'Sunday'='domingo'
}
$dia = $dias[(Get-Date).DayOfWeek.ToString()]
$dest = "C:\formulas_app\backups\$dia.sql"
$logFile = "C:\formulas_app\backups\backup.log"

New-Item -ItemType Directory -Force -Path "C:\formulas_app\backups" | Out-Null

# Credenciales de RDS (mismas que la app)
$env:PGHOST = "<endpoint-RDS>.us-east-2.rds.amazonaws.com"
$env:PGPORT = "5432"
$env:PGUSER = "programa_core"
$env:PGPASSWORD = "<la-password>"
$env:PGDATABASE = "programa_core"

try {
    "$ts INICIO backup $dia" | Add-Content $logFile
    & "C:\Program Files\PostgreSQL\17\bin\pg_dump.exe" `
        --schema=scintela --no-owner --no-acl --format=custom `
        --file="$dest" programa_core

    $size = (Get-Item $dest).Length / 1MB
    "$ts OK backup $dia → $dest ({0:N1} MB)" -f $size | Add-Content $logFile
} catch {
    "$ts ERROR backup $dia: $_" | Add-Content $logFile
    throw
}
```

> **Reemplazar** `<endpoint-RDS>` y `<la-password>` con los reales (las del
> archivo `.env` de la app).

> Si `pg_dump.exe` está en otra ruta (versión 16, 15, etc.), ajustar.

### 2. Crear el Scheduled Task

Desde una PowerShell con permisos admin en el server:

```powershell
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\formulas_app\backup_db.ps1"

# Diario a las 02:00 (hora server = UTC; ajustar si está en otra zona)
$Trigger = New-ScheduledTaskTrigger -Daily -At "02:00"

$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "Intela-DB-Backup-Diario" `
    -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings `
    -Description "Backup pg_dump de programa_core, rotativo 7 días (lunes.sql, martes.sql, ...)"
```

### 3. Test manual (ejecutar ya, sin esperar 2 AM)

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\formulas_app\backup_db.ps1
Get-ChildItem C:\formulas_app\backups\
Get-Content C:\formulas_app\backups\backup.log -Tail 10
```

Deberías ver `<diadehoy>.sql` con varios MB.

### 4. Cómo restaurar desde un dump

```powershell
$env:PGHOST = "<endpoint-RDS>"
$env:PGUSER = "programa_core"
$env:PGPASSWORD = "<password>"

# OJO: esto DROPEA el schema scintela actual y lo reemplaza.
# Hacé en un DB de prueba primero si estás dudoso.
& "C:\Program Files\PostgreSQL\17\bin\pg_restore.exe" `
    --clean --if-exists --no-owner --no-acl `
    --dbname=programa_core C:\formulas_app\backups\lunes.sql
```

### Bonus: subir el dump a S3 también

Agregá al final del script PowerShell, antes del `} catch {`:

```powershell
# Subir copia a S3 (si AWS CLI está instalado y configurado)
try {
    aws s3 cp $dest "s3://intela-deploys/backups/$dia.sql" --quiet
    "$ts OK upload S3" | Add-Content $logFile
} catch {
    "$ts WARN S3 upload falló: $_" | Add-Content $logFile
}
```

Así tenés copia local (Windows) + copia remota (S3).

---

## Recomendación final

- **Activá Opción A YA** (3 clicks en AWS Console). Es la red de seguridad.
- **Implementá Opción B esta semana** si querés tener `.sql` portables o que
  el backup quede en el server (más rápido restaurar localmente si pasa algo
  con AWS).

Ambas opciones se complementan: A te da point-in-time restore (cualquier
segundo de los últimos 7 días, manejado por AWS); B te da un archivo concreto
que podés bajar, abrir en pgAdmin, o llevarte en un pen drive.
