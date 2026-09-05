# El arranque de Metabase en el EC2 — el deploy de Programa Core copia este
# archivo a C:\metabase\start-metabase.ps1 (paso 5.d de deploy.yml) si cambió.
#
# TMT 2026-09-05 (Andrés: "está super lento el sistema, se queda pensando").
# Segunda vez en cinco días que la máquina (t3.medium, 4 GB para cuatro
# programas + Windows + Defender) se queda sin memoria por Metabase: el 31/08
# se le puso -Xmx1g y hoy java estaba igual en 1.619 MB privados con 52 MB
# libres y 6.239 páginas/seg a disco. El heap es UNA parte de lo que come la
# JVM: metaspace, código compilado, hilos y buffers directos van aparte y no
# tenían tope. Acá se les pone tope a todos y se bajan los hilos de Jetty.
# Si igual se queda sin memoria, sale (ExitOnOutOfMemoryError) y la tarea
# programada lo vuelve a levantar limpio, en vez de quedarse colgado
# arrastrando a todo el server.
#
# Tres cosas que ya quemaron tiempo y no hay que "simplificar":
#  · $jargs como ARRAY: PowerShell rompe los -D/-X inline.
#  · $ErrorActionPreference = "Continue": java escribe en stderr al arrancar y
#    con Stop la tarea aborta antes de que Jetty bindee.
#  · No subir -Xmx en este server: sólo si pasa a 8 GB.
$ErrorActionPreference = "Continue"
$env:MB_DB_TYPE = [System.Environment]::GetEnvironmentVariable("MB_DB_TYPE","Machine")
$env:MB_DB_HOST = [System.Environment]::GetEnvironmentVariable("MB_DB_HOST","Machine")
$env:MB_DB_PORT = [System.Environment]::GetEnvironmentVariable("MB_DB_PORT","Machine")
$env:MB_DB_DBNAME = [System.Environment]::GetEnvironmentVariable("MB_DB_DBNAME","Machine")
$env:MB_DB_USER = [System.Environment]::GetEnvironmentVariable("MB_DB_USER","Machine")
$env:MB_DB_PASS = [System.Environment]::GetEnvironmentVariable("MB_DB_PASS","Machine")
$env:MB_JETTY_HOST = [System.Environment]::GetEnvironmentVariable("MB_JETTY_HOST","Machine")
$env:MB_JETTY_PORT = [System.Environment]::GetEnvironmentVariable("MB_JETTY_PORT","Machine")
$env:MB_DB_SSL = "true"
$env:MB_DB_SSL_MODE = "require"
$env:MB_JETTY_MAXTHREADS = "16"
$java = "C:\Program Files\Eclipse Adoptium\jdk-21.0.5.11-hotspot\bin\java.exe"
Set-Location C:\metabase
$jargs = @("-Xmx768m","-XX:MaxMetaspaceSize=256m","-XX:ReservedCodeCacheSize=96m","-XX:MaxDirectMemorySize=64m","-Xss512k","-XX:+UseSerialGC","-XX:+ExitOnOutOfMemoryError","-jar","C:\metabase\metabase.jar")
& $java $jargs *>> C:\metabase\metabase.log
