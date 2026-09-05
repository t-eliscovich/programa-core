# Reinicio NOCTURNO de Metabase (tarea Metabase_ReinicioNocturno, 02:30 hora
# Ecuador, registrada por el deploy de Programa Core — paso 5.d).
#
# TMT 2026-09-05. Java toma memoria hasta su tope y no la devuelve: en cinco
# días de uso Metabase llegó dos veces a dejar el server sin memoria y con
# todo lento. Con los topes de la JVM (deploy.yml, paso 5.d) no debería pasar; esto es
# el cinturón además de los tiradores: un arranque limpio cada noche, cuando
# no lo usa nadie. Mata SÓLO java por PID (nunca por nombre de otro proceso:
# los python.exe son los programas y los powershell.exe sus lanzadores).
$ErrorActionPreference = "Continue"
$log = "C:\metabase\reinicios.log"
"$(Get-Date -Format s) reinicio nocturno: parando" | Out-File -Append $log
Stop-ScheduledTask -TaskName Metabase -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "name='java.exe'" | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep 5
Start-ScheduledTask -TaskName Metabase
Start-Sleep 90
$java = Get-CimInstance Win32_Process -Filter "name='java.exe'"
$mem = (Get-Counter '\Memory\Available MBytes').CounterSamples[0].CookedValue
"$(Get-Date -Format s) reinicio nocturno: java pid=$($java.ProcessId) libres=$([int]$mem) MB" | Out-File -Append $log
