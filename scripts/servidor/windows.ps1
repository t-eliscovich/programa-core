# Lo que el servidor Windows tiene que tener configurado — idempotente, lo
# corre el deploy de Programa Core en cada vuelta (paso 5.e de deploy.yml).
#
# Fase 4 del plan docs/PLAN_MEMORIA_SERVIDOR_2026_09_05.md. Regla del 27/08:
# la infra del EC2 va por el pipeline, no "andá a CloudShell". Cada bloque
# mira primero y escribe sólo si hace falta, así una corrida sin cambios
# imprime "ya estaba" y no toca nada.
#
# Qué hace:
#  1. Defender: excluye las carpetas de los programas y de Python. No baja la
#     memoria de MsMpEng; saca los picos de CPU al 100% de cada deploy (escanea
#     cada archivo que extrae el tar) y de cada PDF que sale del navegador.
#  2. Google Update (updater.exe ×8): Chrome está sólo para el navegador
#     headless; su actualizador corre 8 procesos para nada. Se deshabilitan
#     sus tareas y servicios. (Chrome se actualiza a mano el día que haga
#     falta.)
#  3. Pagefile fijo de 4 GB: hoy lo maneja Windows y crece a saltos mientras
#     pagina. No ahorra memoria; evita que un episodio termine en "no hay
#     memoria para el pagefile". Toma efecto en el próximo reinicio.
#  4. OpenVPN: SÓLO si $ApagarOpenVpn (decisión de Tamara pendiente).
param([switch]$ApagarOpenVpn)

$ErrorActionPreference = "Continue"

Write-Output "--- 1. Defender: exclusiones ---"
$carpetas = @('C:\programa-core', 'C:\formulas_app', 'C:\maquinas_app', 'C:\metabase', 'C:\Python312',
              (Join-Path $env:TEMP 'pc-nav'), 'C:\Windows\Temp')
try {
    $ya = (Get-MpPreference).ExclusionPath
    foreach ($c in $carpetas) {
        if ($ya -contains $c) { Write-Output "  $c ya estaba" }
        else { Add-MpPreference -ExclusionPath $c; Write-Output "  $c agregada" }
    }
    $procs = @('python.exe', 'java.exe', 'chrome.exe', 'msedge.exe')
    $yap = (Get-MpPreference).ExclusionProcess
    foreach ($p in $procs) {
        if ($yap -contains $p) { Write-Output "  proceso $p ya estaba" }
        else { Add-MpPreference -ExclusionProcess $p; Write-Output "  proceso $p agregado" }
    }
} catch { Write-Output "  AVISO Defender: $_" }

Write-Output "--- 2. Google Update apagado ---"
try {
    $tareas = Get-ScheduledTask -TaskName 'GoogleUpdate*' -ErrorAction SilentlyContinue
    foreach ($t in $tareas) {
        if ($t.State -eq 'Disabled') { Write-Output "  tarea $($t.TaskName) ya estaba apagada" }
        else { $t | Disable-ScheduledTask | Out-Null; Write-Output "  tarea $($t.TaskName) apagada" }
    }
    foreach ($s in (Get-Service -Name 'gupdate*', 'GoogleUpdater*' -ErrorAction SilentlyContinue)) {
        if ($s.StartType -eq 'Disabled') { Write-Output "  servicio $($s.Name) ya estaba apagado" }
        else { Stop-Service $s.Name -Force -ErrorAction SilentlyContinue; Set-Service $s.Name -StartupType Disabled; Write-Output "  servicio $($s.Name) apagado" }
    }
    Get-Process -Name 'updater' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
} catch { Write-Output "  AVISO Google Update: $_" }

Write-Output "--- 3. Pagefile fijo 4 GB ---"
try {
    $cs = Get-CimInstance Win32_ComputerSystem
    if ($cs.AutomaticManagedPagefile) {
        Set-CimInstance -InputObject $cs -Property @{AutomaticManagedPagefile = $false}
    }
    $pf = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pf -and $pf.InitialSize -eq 4096 -and $pf.MaximumSize -eq 4096) { Write-Output "  ya estaba en 4096/4096" }
    elseif ($pf) { Set-CimInstance -InputObject $pf -Property @{InitialSize = 4096; MaximumSize = 4096}; Write-Output "  puesto en 4096/4096 (toma efecto al reiniciar)" }
    else { New-CimInstance -ClassName Win32_PageFileSetting -Property @{Name = 'C:\pagefile.sys'; InitialSize = 4096; MaximumSize = 4096} | Out-Null; Write-Output "  creado 4096/4096 (toma efecto al reiniciar)" }
} catch { Write-Output "  AVISO pagefile: $_" }

if ($ApagarOpenVpn) {
    Write-Output "--- 4. OpenVPN apagado ---"
    foreach ($s in (Get-Service -Name 'OpenVPN*' -ErrorAction SilentlyContinue)) {
        if ($s.StartType -eq 'Disabled') { Write-Output "  $($s.Name) ya estaba apagado" }
        else { Stop-Service $s.Name -Force -ErrorAction SilentlyContinue; Set-Service $s.Name -StartupType Disabled; Write-Output "  $($s.Name) apagado" }
    }
} else {
    Write-Output "--- 4. OpenVPN: sin tocar (falta la decision) ---"
}
