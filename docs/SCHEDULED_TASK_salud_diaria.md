# Scheduled Task — chequeo de salud diario

Corre `scripts/check_salud_dia.py` una vez por día en el server Windows EC2 y
guarda el reporte en un log. El script es **read-only** (no toca la base) y
revisa invariantes de caja, gastos, bancos, mov_doble, cheques, facturas,
posdat, provisiones, **chequesxfact** (sobre-aplicaciones/duplicados) y
**reversibilidad** (que todo movimiento tenga reverso).

Exit code: **0** si todo OK/WARN, **1** si hay `[ERR]`.

> Si ya tenés un Scheduled Task diario que corre scripts de mantenimiento,
> sumá la línea del paso 1 a ese `.ps1` en vez de registrar uno nuevo.

---

## 1. Script PowerShell

Guardar en el server como `C:\formulas_app\salud_diaria.ps1`
(ajustá las rutas a las reales de tu instalación):

```powershell
# Chequeo de salud diario de programa_core. Read-only.
$ErrorActionPreference = "Stop"
$ts   = Get-Date -Format "yyyy-MM-dd_HH-mm"
$repo = "C:\formulas_app\programa-core"        # ← ruta del repo
$venv = "$repo\.venv\Scripts\python.exe"        # ← python del venv
$logDir = "C:\formulas_app\logs\salud"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = "$logDir\salud_$ts.txt"

# Las credenciales de la DB salen del .env del repo (python-dotenv las lee).
Push-Location $repo
try {
    & $venv "scripts\check_salud_dia.py" *>&1 | Tee-Object -FilePath $log
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        # Hubo [ERR]: dejamos una marca fácil de ver.
        Copy-Item $log "$logDir\_ULTIMO_CON_ERRORES.txt" -Force
    }
} finally {
    Pop-Location
}

# Rotación simple: borrar logs de más de 30 días.
Get-ChildItem $logDir -Filter "salud_*.txt" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force
```

---

## 2. Registrar el Scheduled Task

Desde una PowerShell admin en el server:

```powershell
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\formulas_app\salud_diaria.ps1"

# Diario a las 07:00 hora Ecuador. El server corre en UTC → 12:00 UTC.
$Trigger = New-ScheduledTaskTrigger -Daily -At "12:00"

$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName "Intela-Salud-Diaria" `
    -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings `
    -Description "check_salud_dia.py — invariantes read-only, reporte diario"
```

---

## 3. Test manual (sin esperar)

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\formulas_app\salud_diaria.ps1
Get-Content C:\formulas_app\logs\salud\salud_*.txt | Select-Object -Last 30
```

Deberías ver el reporte semaforizado con el `Resumen: N OK · N WARN · N ERR`.

---

## 4. Cómo mirarlo cada día

- **Rápido**: si existe `C:\formulas_app\logs\salud\_ULTIMO_CON_ERRORES.txt`,
  hubo un `[ERR]` en la última corrida → abrilo y mirá qué sección.
- **Completo**: el log del día en `...\logs\salud\salud_<fecha>.txt`.

> Hoy no hay envío de mail (no está configurado SMTP). Si más adelante querés
> que te avise por mail cuando hay `[ERR]`, se agrega un bloque `Send-MailMessage`
> al final del `.ps1` condicionado a `$code -ne 0` — decime y lo armo.

---

## Qué vigila (secciones)

| # | Sección | Marca `[ERR]` si… |
|---|---|---|
| 1 | Caja | egresos S sin clasificar como gasto |
| 2 | Gastos | reparto V1..V9 raro |
| 3 | Bancos | saldo running no cuadra |
| 4 | Mov_doble | reversos huérfanos |
| 5 | Cheques | estados inconsistentes |
| 6 | Facturas | saldo ≠ importe − abono |
| 7 | Posdat | banc/anulada inconsistente |
| 8 | Provisiones | corredor diario no corrió |
| 9 | **Chequesxfact** | cheque sobre-aplicado / factura sobre-abonada |
| 10 | **Reversibilidad** | algún movimiento sin reverso posible |
