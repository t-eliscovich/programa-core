<#
    Pone el PORTAL DEL CLIENTE en el aire. Se corre UNA VEZ, en el servidor.

    Qué hace, en este orden:

      1. Crea la tarea programada `PortalClienteApp`, copiando la definición de
         `ProgramaCoreApp` —el programa de la oficina, que ya funciona— y
         cambiándole sólo la puerta (`run_portal:app`) y el puerto (5004).
      2. Le agrega `portal.intela.com.ec` al Caddyfile, que pide el certificado
         de Let's Encrypt solo la primera vez que llega tráfico.
      3. Chequea que conteste.

    Es IDEMPOTENTE: si la tarea ya existe la vuelve a definir igual, y si el
    Caddyfile ya tiene el bloque no lo agrega de nuevo. Correrlo dos veces no
    rompe nada.

    Cómo se corre (SSM Run Command, o una consola en el server):

        powershell -ExecutionPolicy Bypass -File C:\programa-core\scripts\crear_servicio_portal.ps1

    ⚠ NO toca el bloque de `programa.intela.com.ec` ni la tarea de la oficina.
    Antes de escribir el Caddyfile deja una copia con fecha al lado, y si Caddy
    rechaza la configuración nueva, la restaura y deja todo como estaba.

    Ver `modo.py`, `run_portal.py` y PLAN_PORTAL_CLIENTE_2026_08_24.md.
#>

$ErrorActionPreference = 'Stop'

$TAREA_NUEVA  = 'PortalClienteApp'
$TAREA_BASE   = 'ProgramaCoreApp'
$PUERTO       = 5004
$CADDYFILE    = 'C:\caddy\Caddyfile'
$CADDY_EXE    = 'C:\caddy\caddy.exe'
$HOSTNAME_WEB = 'portal.intela.com.ec'

Write-Output "=== Portal del cliente: puesta en marcha ==="

# ---------------------------------------------------------------------------
# 1. La tarea programada
# ---------------------------------------------------------------------------

$base = Get-ScheduledTask -TaskName $TAREA_BASE -ErrorAction SilentlyContinue
if (-not $base) {
    throw "No existe la tarea $TAREA_BASE. Sin ella no sé con qué arrancar el portal."
}

$accionBase = $base.Actions[0]
Write-Output "La oficina arranca con:"
Write-Output "  Programa : $($accionBase.Execute)"
Write-Output "  Argumentos: $($accionBase.Arguments)"
Write-Output "  Carpeta  : $($accionBase.WorkingDirectory)"

# La MISMA línea, con la puerta y el puerto del portal. Se deriva de la que ya
# funciona en vez de escribirla a mano: si mañana cambia la forma de arrancar
# (otra versión de Waitress, otro python), el portal la hereda sola.
$argumentos = $accionBase.Arguments -replace 'run:app', 'run_portal:app' -replace '5002', "$PUERTO"

if ($argumentos -eq $accionBase.Arguments) {
    throw "No pude derivar los argumentos del portal (no encontré 'run:app' ni '5002' en: $($accionBase.Arguments)). Frenar acá es mejor que arrancar un segundo proceso de la OFICINA en otro puerto."
}
if ($argumentos -notmatch 'run_portal:app') {
    throw "Los argumentos derivados no apuntan a run_portal:app: $argumentos"
}

Write-Output ""
Write-Output "El portal va a arrancar con:"
Write-Output "  Argumentos: $argumentos"

$carpeta = $accionBase.WorkingDirectory
if (-not $carpeta) { $carpeta = 'C:\programa-core' }
if (-not (Test-Path (Join-Path $carpeta 'run_portal.py'))) {
    throw "No encuentro run_portal.py en $carpeta. Falta deployar el codigo nuevo."
}

$accion = New-ScheduledTaskAction -Execute $accionBase.Execute `
                                  -Argument $argumentos `
                                  -WorkingDirectory $carpeta

# Mismo disparador, mismo usuario y mismas opciones que la de la oficina: lo
# que ya sobrevive a los reinicios del server.
$disparadores = $base.Triggers
if (-not $disparadores) { $disparadores = New-ScheduledTaskTrigger -AtStartup }

Register-ScheduledTask -TaskName $TAREA_NUEVA `
                       -Action $accion `
                       -Trigger $disparadores `
                       -Principal $base.Principal `
                       -Settings $base.Settings `
                       -Force | Out-Null

Write-Output "Tarea $TAREA_NUEVA registrada."

Stop-ScheduledTask -TaskName $TAREA_NUEVA -ErrorAction SilentlyContinue
Start-Sleep 2
Start-ScheduledTask -TaskName $TAREA_NUEVA

$codigo = 0
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep 1
    try { $codigo = (Invoke-WebRequest -UseBasicParsing "http://localhost:$PUERTO/" -TimeoutSec 5).StatusCode } catch { $codigo = 0 }
    if ($codigo -eq 200) { break }
}
Write-Output "El portal en localhost:$PUERTO contesta HTTP $codigo"
if ($codigo -ne 200) {
    throw "El portal no levanto. NO toco el Caddyfile: pedirle un certificado a Let's Encrypt para un sitio caido gasta uno de los pocos intentos que da por semana."
}

# ---------------------------------------------------------------------------
# 2. El Caddyfile — el certificado sale solo
# ---------------------------------------------------------------------------

if (-not (Test-Path $CADDYFILE)) {
    throw "No encuentro $CADDYFILE."
}

$texto = Get-Content $CADDYFILE -Raw
if ($texto -match [regex]::Escape($HOSTNAME_WEB)) {
    Write-Output "El Caddyfile ya tiene $HOSTNAME_WEB — no lo toco."
} else {
    $copia = "$CADDYFILE.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $CADDYFILE $copia -Force
    Write-Output "Copia del Caddyfile en $copia"

    # OJO: nada de here-strings (@" ... "@). El cierre tiene que ir pegado al
    # margen izquierdo, y adentro de un bloque indentado eso no se ve venir:
    # el 24/08 el script entero no compilo por eso. Un array de lineas no
    # tiene esa trampa.
    $bloque = @(
        "",
        "$HOSTNAME_WEB {",
        "    reverse_proxy localhost:$PUERTO",
        "    encode gzip",
        "}"
    ) -join "`r`n"
    Add-Content -Path $CADDYFILE -Value $bloque

    Push-Location (Split-Path $CADDY_EXE)
    try {
        & $CADDY_EXE reload --config Caddyfile
        if ($LASTEXITCODE -ne 0) { throw "caddy reload salio $LASTEXITCODE" }
        Write-Output "Caddy recargado con $HOSTNAME_WEB."
    } catch {
        Copy-Item $copia $CADDYFILE -Force
        & $CADDY_EXE reload --config Caddyfile
        Pop-Location
        throw "Caddy rechazo la configuracion nueva. Restaure la anterior y la recargue: el server quedo como estaba. Error: $_"
    }
    Pop-Location
}

# ---------------------------------------------------------------------------
# 3. Cómo quedó
# ---------------------------------------------------------------------------

Write-Output ""
Write-Output "=== Como quedo ==="
Write-Output "Oficina : $((Get-ScheduledTask -TaskName $TAREA_BASE).State) (puerto 5002)"
Write-Output "Portal  : $((Get-ScheduledTask -TaskName $TAREA_NUEVA).State) (puerto $PUERTO)"
Write-Output ""
Write-Output "Probar desde afuera: https://$HOSTNAME_WEB/"
Write-Output "El certificado puede tardar unos segundos la PRIMERA vez que alguien entra."
