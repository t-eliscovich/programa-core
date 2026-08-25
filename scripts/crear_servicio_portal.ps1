<#
    Pone el PORTAL DEL CLIENTE en el aire. Se corre UNA VEZ, en el servidor.

    Que hace, en este orden:

      1. Crea la tarea programada PortalClienteApp, copiando la definicion de
         ProgramaCoreApp -el programa de la oficina, que ya funciona- y
         cambiandole solo la puerta (run_portal:app) y el puerto (5004).
      2. Le agrega portal.intela.com.ec al Caddyfile, que pide el certificado
         de Let's Encrypt solo la primera vez que llega trafico.
      3. Chequea que conteste.

    Es IDEMPOTENTE: si la tarea ya existe la vuelve a definir igual, y si el
    Caddyfile ya tiene el bloque no lo agrega de nuevo. Correrlo dos veces no
    rompe nada.

    Como se corre (SSM Run Command, o una consola en el server):

        powershell -ExecutionPolicy Bypass -File C:\programa-core\scripts\crear_servicio_portal.ps1

    NO toca el bloque de la oficina ni su tarea. Antes de escribir el Caddyfile
    deja una copia con fecha al lado, y si Caddy rechaza la configuracion nueva
    la restaura y deja todo como estaba.

    ---------------------------------------------------------------------------
    DOS REGLAS DE ESTE ARCHIVO, las dos pagadas caro el 24/08/2026:

    1. TODO EN ASCII. Sin acentos, sin enies, sin comillas tipograficas, sin
       flechas ni simbolos. Windows PowerShell 5.1 lee un .ps1 SIN BOM como
       Windows-1252, no como UTF-8: cada caracter acentuado se decodifica mal y
       el parser termina cortando una cadena por la mitad. El sintoma no dice
       "encoding": dice "Unexpected token" en un renglon que esta perfecto.

    2. NADA DE HERE-STRINGS (@" ... "@). El cierre tiene que ir pegado al margen
       izquierdo, y adentro de un bloque indentado eso no se ve venir.

    En este entorno no hay forma de correr PowerShell antes de mandarlo al
    server, asi que lo que no se puede probar se evita.
    ---------------------------------------------------------------------------

    Ver modo.py, run_portal.py y PLAN_PORTAL_CLIENTE_2026_08_24.md.
#>

$ErrorActionPreference = 'Stop'

$TAREA_NUEVA  = 'PortalClienteApp'
$TAREA_BASE   = 'ProgramaCoreApp'
$PUERTO       = 5004
$CADDYFILE    = 'C:\caddy\Caddyfile'
$CADDY_EXE    = 'C:\caddy\caddy.exe'
$SITIO        = 'portal.intela.com.ec'

Write-Output '=== Portal del cliente: puesta en marcha ==='

# ---------------------------------------------------------------------------
# 1. La tarea programada
# ---------------------------------------------------------------------------

$base = Get-ScheduledTask -TaskName $TAREA_BASE -ErrorAction SilentlyContinue
if (-not $base) {
    throw "No existe la tarea $TAREA_BASE. Sin ella no se con que arrancar el portal."
}

$accionBase = $base.Actions[0]
Write-Output 'La oficina arranca con:'
Write-Output ('  Programa   : ' + $accionBase.Execute)
Write-Output ('  Argumentos : ' + $accionBase.Arguments)
Write-Output ('  Carpeta    : ' + $accionBase.WorkingDirectory)

# ---------------------------------------------------------------------------
# El launcher del portal se DERIVA del de la oficina
# ---------------------------------------------------------------------------
#
# La tarea de la oficina no llama a waitress directo: llama a un
# launch_core.ps1 que exporta las variables de Machine (DATABASE_URL,
# METABASE_*, las cards de Asinfo), arma la carpeta de logs y recien ahi
# arranca waitress. Ese archivo NO esta en el repo: vive solo en el server.
#
# Por eso el del portal se genera A PARTIR de el en vez de escribirlo a mano:
# la lista de variables de entorno es larga y se desactualiza sola. Si manana
# alguien le agrega una variable al de la oficina, el del portal la hereda en
# la proxima corrida de este script.

$m = [regex]::Match($accionBase.Arguments, '-File\s+"?([^"]+\.ps1)"?')
if (-not $m.Success) {
    throw "No pude encontrar el launcher (.ps1) en los argumentos de $TAREA_BASE : $($accionBase.Arguments)"
}
$launcherOficina = $m.Groups[1].Value
if (-not (Test-Path $launcherOficina)) {
    throw "El launcher de la oficina no existe: $launcherOficina"
}

$launcherPortal = Join-Path (Split-Path $launcherOficina) 'launch_portal.ps1'
$textoOficina = Get-Content $launcherOficina -Raw

if ($textoOficina -notmatch 'run:app' -or $textoOficina -notmatch '5002') {
    throw "El launcher de la oficina no tiene 'run:app' ni '5002'. Frenar aca es mejor que arrancar un segundo proceso de la OFICINA en otro puerto."
}

$textoPortal = $textoOficina -replace 'run:app', 'run_portal:app' -replace '5002', "$PUERTO" -replace 'core-', 'portal-'

if ($textoPortal -notmatch 'run_portal:app' -or $textoPortal -notmatch "$PUERTO") {
    throw 'El launcher derivado no quedo apuntando al portal.'
}

$cabecera = @()
$cabecera += '# GENERADO por scripts/crear_servicio_portal.ps1 a partir de'
$cabecera += ("# " + $launcherOficina + " -- NO editar a mano: se regenera.")
$cabecera += '# Lo unico que cambia es la puerta (run_portal:app), el puerto y el log.'
$cabecera += ''
Set-Content -Path $launcherPortal -Value (($cabecera -join "`r`n") + $textoPortal) -Encoding ASCII

Write-Output ''
Write-Output ('Launcher del portal escrito en ' + $launcherPortal)

$argumentos = $accionBase.Arguments.Replace($launcherOficina, $launcherPortal)
if ($argumentos -eq $accionBase.Arguments) {
    throw 'No pude cambiarle el launcher a los argumentos de la tarea.'
}

Write-Output 'El portal va a arrancar con:'
Write-Output ('  Argumentos : ' + $argumentos)

$carpeta = $accionBase.WorkingDirectory
if (-not $carpeta) { $carpeta = 'C:\programa-core' }
if (-not (Test-Path (Join-Path $carpeta 'run_portal.py'))) {
    throw "No encuentro run_portal.py en $carpeta. Falta deployar el codigo nuevo."
}

$accion = New-ScheduledTaskAction -Execute $accionBase.Execute -Argument $argumentos -WorkingDirectory $carpeta

# Mismo disparador, mismo usuario y mismas opciones que la de la oficina: lo
# que ya sobrevive a los reinicios del server.
$disparadores = $base.Triggers
if (-not $disparadores) { $disparadores = New-ScheduledTaskTrigger -AtStartup }

Register-ScheduledTask -TaskName $TAREA_NUEVA -Action $accion -Trigger $disparadores -Principal $base.Principal -Settings $base.Settings -Force | Out-Null

Write-Output "Tarea $TAREA_NUEVA registrada."

Stop-ScheduledTask -TaskName $TAREA_NUEVA -ErrorAction SilentlyContinue
Start-Sleep 2
Start-ScheduledTask -TaskName $TAREA_NUEVA

$codigo = 0
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep 1
    try { $codigo = (Invoke-WebRequest -UseBasicParsing "http://localhost:$PUERTO/ingresar" -TimeoutSec 5).StatusCode } catch { $codigo = 0 }
    if ($codigo -eq 200) { break }
}
Write-Output "El portal en localhost:$PUERTO contesta HTTP $codigo"
if ($codigo -ne 200) {
    throw "El portal no levanto. NO toco el Caddyfile: pedirle un certificado a Let's Encrypt para un sitio caido gasta uno de los pocos intentos que da por semana."
}

# ---------------------------------------------------------------------------
# 2. El Caddyfile - el certificado sale solo
# ---------------------------------------------------------------------------

if (-not (Test-Path $CADDYFILE)) {
    throw "No encuentro $CADDYFILE."
}

$texto = Get-Content $CADDYFILE -Raw
if ($texto -match [regex]::Escape($SITIO)) {
    Write-Output "El Caddyfile ya tiene $SITIO - no lo toco."
} else {
    $copia = ($CADDYFILE + '.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Copy-Item $CADDYFILE $copia -Force
    Write-Output "Copia del Caddyfile en $copia"

    # Comillas SIMPLES y concatenacion: sin interpolacion no hay forma de que
    # una llave o un dos puntos confundan al parser.
    Add-Content -Path $CADDYFILE -Value ''
    Add-Content -Path $CADDYFILE -Value ($SITIO + ' {')
    Add-Content -Path $CADDYFILE -Value ('    reverse_proxy localhost:' + $PUERTO)
    Add-Content -Path $CADDYFILE -Value '    encode gzip'
    Add-Content -Path $CADDYFILE -Value '}'

    Push-Location (Split-Path $CADDY_EXE)
    try {
        & $CADDY_EXE reload --config Caddyfile
        if ($LASTEXITCODE -ne 0) { throw "caddy reload salio $LASTEXITCODE" }
        Write-Output "Caddy recargado con $SITIO."
        Pop-Location
    } catch {
        Copy-Item $copia $CADDYFILE -Force
        & $CADDY_EXE reload --config Caddyfile
        Pop-Location
        throw "Caddy rechazo la configuracion nueva. Restaure la anterior y la recargue: el server quedo como estaba. Error: $_"
    }
}

# ---------------------------------------------------------------------------
# 3. Como quedo
# ---------------------------------------------------------------------------

Write-Output ''
Write-Output '=== Como quedo ==='
Write-Output ('Oficina : ' + (Get-ScheduledTask -TaskName $TAREA_BASE).State + ' (puerto 5002)')
Write-Output ('Portal  : ' + (Get-ScheduledTask -TaskName $TAREA_NUEVA).State + " (puerto $PUERTO)")
Write-Output ''
Write-Output "Probar desde afuera: https://$SITIO/"
Write-Output 'El certificado puede tardar unos segundos la PRIMERA vez que alguien entra.'
