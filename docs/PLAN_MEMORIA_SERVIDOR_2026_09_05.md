# Plan: que al servidor no le vuelva a faltar memoria

Tamara, 05/09/2026: *"necesito que investigues todo a fondo y quiero hacer
todo, pero hacé un plan detallado"*. Este es el plan. Cada fase dice qué se
hace, por qué, cuánto ahorra, qué riesgo tiene y cómo se verifica.

## 1. Qué pasó (dos veces) y qué hay hoy

El EC2 (`i-0fcca4d7029f08489`, t3.medium: 2 vCPU, **4.036 MB**) corre Windows
Server + Defender + Programa Core (oficina 5002 y portal 5004) + formulas_app
(5001) + máquinas (5003) + Metabase (3000) + Caddy (443).

El 31/08 y el 05/09 se quedó sin memoria (52 y 47 MB libres, 6.000+ páginas/seg
a disco) y todo tardó 10–80 s. **La causa: procesos huérfanos del navegador
headless que saca los PDFs e imágenes.** Chromium son ~8 procesos; el
programa apagaba el suyo cada 15 min sin uso con `terminate()`, que en Windows
mata sólo al principal, y lo volvía a prender 30 s después. 4 vueltas/hora × 7
huérfanos × 10 días = **1.525 chrome, 8.903 MB**. El 31/08 se culpó a
Metabase (java era el proceso más grande de a uno) y se le bajó el heap: el
síntoma volvió a los cinco días porque la fuga seguía.

Foto del 05/09 13:13, después de los arreglos (memoria privada, por nombre):

| Proceso | Cuántos | MB | Qué es |
|---|---|---|---|
| java.exe | 1 | 947 | Metabase (tope 768 MB de heap + el resto de la JVM) |
| chrome.exe | 18 | 418 | los DOS navegadores prendidos (oficina y portal) |
| MsMpEng.exe | 1 | 365 | Windows Defender |
| powershell.exe | 5 | 339 | los lanzadores de las 4 apps + el de Metabase (70 MB cada uno, sólo esperan) |
| python.exe | 4 | 297 | los cuatro programas |
| svchost.exe | 54 | 274 | Windows |
| caddy.exe | 1 | 68 | el proxy HTTPS |
| conhost.exe | 8 | 49 | las consolas de los powershell |
| AnyDesk / ssm / updater ×8 / openvpn | | ~130 | acceso remoto, agente AWS, el actualizador de Chrome, OpenVPN |

**Libres: 1.880 MB.** Sin la fuga la máquina alcanza; el plan es dejar
margen de sobra y que, si vuelve a faltar, se sepa en un minuto y no por
Andrés.

## 2. Lo que ya quedó hecho el 05/09 (fase 0)

- `navegador.py`: apagar mata el ÁRBOL (`taskkill /T`); `pdf_motor` e
  `imagen_motor` pasan por `correr_y_matar_el_arbol`; el latido barre cada
  30 s los procesos nuestros que sobran (los reconoce por `--user-data-dir`
  con `pc-nav-/pc-pdf-/pc-img-` Y nombre de navegador; cada arranque usa
  `perfil-<n>`; el dueño de una carpeta tiene que ser un python vivo).
- `/admin/pantallas`, bloque "El servidor": memoria libre, CPU, procesos
  **por nombre**, navegadores nuestros, el vigía.
- Health `servidor` en `/admin/health/all` (< 400 MB libres o > 40 navegadores).
- **El vigía** (`vigia_servidor.py`): cada minuto; si faltan → barre, reinicia
  Metabase si java > 1.300 MB (1 vez / 2 h), campanita + mail a los
  administradores (1 cada 3 h, y otro cuando vuelve).
- Metabase: `scripts/servidor/start-metabase.ps1` en el repo con tope a toda
  la JVM (heap 768m, metaspace 256m, código 96m, buffers 64m, stack 512k,
  16 hilos, sale si se queda sin memoria) y `Metabase_ReinicioNocturno`
  02:30 EC; el deploy los deja en el server (paso 5.d).

## 3. Las fases que faltan

### Fase 1 — El navegador no se apaga más por "falta de uso" · XS · sin riesgo

**Por qué.** Hoy el ciclo apagar/prender es ceremonia: se apaga a los 15 min
sin uso y el latido lo vuelve a prender a los 30 s. Ese ciclo es el que
fabricaba huérfanos; el barrido lo tapa, pero lo sano es no tener el ciclo.

**Qué.** En `navegador.py`: sacar el apagado por `IDLE_S`; reciclarlo UNA vez
por día a las 02:35 EC (justo después de Metabase) por si Chromium crece, con
el mismo `matar()` que ya mata el árbol. Test: un latido con `ultimo_uso`
viejo NO lo apaga; el latido de las 02:35 sí, una vez.

**Verificar.** 48 h con chrome × ~16–18 y sin "huérfanos" en /admin/pantallas.
**Ahorra.** Nada de memoria; saca la causa raíz.

### Fase 2 — Historia de la memoria, para ver la tendencia · S · sin riesgo

**Por qué.** Hoy se ve el instante. Una fuga lenta (la de chrome tardó diez
días) se ve sólo en la curva.

**Qué.** El vigía guarda una lectura por hora (libres, java, chrome, python,
total de procesos) en `scintela.servidor_memoria` (migración; 7 días
rotativos, 168 filas). `/admin/pantallas` muestra una curva chica de 7 días
y las tres lecturas más bajas. El health `servidor` agrega una alerta de
TENDENCIA: si la memoria libre bajó > 500 MB en 3 días sin volver.

**Verificar.** La curva se pinta con datos reales después de un día.

### Fase 3 — Lanzadores sin PowerShell · M · riesgo medio (toca los 3 repos)

**Por qué.** Cinco `powershell.exe` de ~70 MB + ocho `conhost.exe` se llevan
**~390 MB** para hacer tres cosas: exportar variables de máquina, rotar logs
y redirigir la salida. Eso lo hace un `launch.py` de 40 líneas y la tarea
programada arranca `python.exe` directo.

**Qué.**
1. `launch.py` (uno por repo, mismo código): lee las variables de Machine
   del registro (`HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment`,
   igual que el fallback de `formulas_memos._url_configurada`), las pone en
   `os.environ`, rota `logs/*.log` de más de 14 días, abre
   `logs/<app>-YYYY-MM-DD.log` y redirige stdout/stderr, y llama a
   `waitress.serve(app, host=..., port=...)` EN el mismo proceso.
2. Los deploys (`deploy.yml` de PC —oficina y portal—, `deploy.sh` de
   formulas, `deploy.sh` de máquinas) registran la tarea con
   `-Execute C:\Python312\python.exe -Argument launch.py` en vez de
   `powershell.exe -File launch_*.ps1`. RestartCount 3 / ExecutionTimeLimit 0
   se mantienen: el reinicio lo hace el Task Scheduler, no el wrapper.
3. Metabase: la tarea `Metabase` arranca `java.exe` directo con los `$jargs`
   del script; `MB_DB_SSL` y `MB_DB_SSL_MODE` pasan a variables de Máquina
   (el deploy las setea, idempotente). El `*>> metabase.log` se reemplaza por
   `-Dmetabase.log...`? NO: Metabase loguea a stdout; la tarea no captura
   stdout. Alternativa sin PowerShell: `MB_EMOJI_IN_LOGS=false` y dejar que
   loguee a `C:\metabase\logs` con `log4j2` (config `log4j2.xml` propio,
   `-Dlog4j.configurationFile=`). Es la parte con más incógnitas: se hace
   ÚLTIMA y se prueba en horario sin uso.
4. Orden: máquinas (el menos usado) → PC portal → PC oficina → formulas →
   Metabase. Uno por día, mirando el log y `/admin/pantallas` al día
   siguiente.

**Riesgos y frenos.** Si `launch.py` no encuentra una variable, la app
arranca sin ella y falla distinto a hoy: `launch.py` valida las obligatorias
y sale con código 3 y una línea en el log. Rollback: volver a registrar la
tarea con el `.ps1` (los `.ps1` no se borran en esta fase).

**Ahorra.** ~390 MB. **Verificar.** `powershell.exe` = 0 en la lista de
/admin/pantallas (salvo los del deploy en curso), conhost ≤ 2.

### Fase 4 — Windows: lo que corre sin que nadie lo pidió · S · riesgo bajo

**Qué.**
- Defender: exclusiones para `C:\programa-core`, `C:\formulas_app`,
  `C:\maquinas_app`, `C:\metabase`, `C:\Python312` y `%TEMP%\pc-*`. No baja
  la memoria de MsMpEng pero saca los picos de CPU al 100% en cada deploy
  (hoy escanea cada archivo que extrae el tar y cada PDF que sale).
- `updater.exe` ×8 (Google Update de Chrome): Chrome está sólo para el
  headless; se desactivan las tareas `GoogleUpdateTaskMachine*` y el servicio.
  ~21 MB, pero también son 8 procesos menos. (Alternativa mejor: usar Edge,
  que ya viene con Windows, y desinstalar Chrome — `pdf_motor` ya prefiere
  Edge si está; verificar cuál es el que corre hoy.)
- OpenVPN (`openvpnserv2`, 19 MB): ¿se usa? Si es de la instalación
  original y no conecta a nada, deshabilitar el servicio.
- AnyDesk (43 MB): se queda (es el acceso remoto de Tamara).
- Pagefile: dejar fijo 4 GB (hoy lo maneja Windows y crece a saltos mientras
  pagina). No ahorra memoria; hace que un episodio no se convierta en
  "no hay memoria para el pagefile".

Todo esto va como paso idempotente del `deploy.yml` de PC (un
`scripts/servidor/windows.ps1` que revisa y aplica), no a mano — regla del
27/08: la infra del EC2 va por el pipeline.

**Ahorra.** ~40 MB y CPU. **Verificar.** El deploy no pone la CPU al 100% por
dos minutos; `updater` desaparece de la lista.

### Fase 5 — Un solo navegador para oficina y portal · M · riesgo medio

**Por qué.** Dos navegadores prendidos = 2 × ~210 MB. El portal saca un PDF
cada tanto; no necesita el suyo.

**Qué.** El portal le pide el PDF/imagen a la oficina por HTTP interno
(`http://127.0.0.1:5002/_interno/hoja`, POST con el HTML, firmado con un
secreto de máquina, sólo desde 127.0.0.1). Si la oficina no contesta, el
portal cae al camino de siempre (un navegador por archivo, que ahora mata
el árbol). En el portal `arrancar_en_segundo_plano()` no se llama.

**Ahorra.** ~210 MB. **Verificar.** chrome × ~8–9; el PDF del portal sigue
saliendo (test con AJT) y tarda lo mismo.

### Fase 6 — Metabase más chico, con datos · S · riesgo medio

**Por qué.** 947 MB para ~5 usuarios y unas pocas consultas por minuto es
mucho, pero bajarlo a ciegas es como el 31/08.

**Qué.** Primero MEDIR: `-Xlog:gc*:file=C:\metabase\gc.log:time` una semana
y leer el heap después de cada GC (el "live set"). Si el live set está
consistentemente < 400 MB, bajar a `-Xmx512m` (ahorra ~250 MB); si no, se
queda. Con `-XX:+ExitOnOutOfMemoryError` un tope corto no cuelga el server:
sale y la tarea lo levanta.

### Fase 7 — La decisión de la máquina

Después de las fases 1–5 el uso en régimen debería ser ~1,5 GB de 4 (hoy
2,15). La regla, ya hablada el 05/09: **si el vigía manda "le falta memoria"
dos veces en un mes sin que sea una fuga nueva, se pasa a t3.large (8 GB,
~US$35/mes más)**, y ahí se sube `-Xmx` de Metabase. Cómo: de noche, bajar
el TTL del DNS en cPanel a 5 min el día anterior, stop/start cambia la IP
pública → actualizar los A de programa/portal/maquinas/metabase, y
aprovechar para poner una IP elástica y no volver a hacer esto.

Aparte, y no tiene que ver con la memoria del EC2: la RDS es `db.t3.micro`
(1 GB). Mirar una vez en la consola (RDS → Monitoring) `FreeableMemory` y
`CPUCreditBalance` del último mes; si los créditos tocan cero, es otra
conversación.

## 4. Orden y tiempos

| # | Fase | Tamaño | Cuándo |
|---|---|---|---|
| 1 | Navegador sin apagado por idle | XS | ya, con el próximo push |
| 2 | Historia de la memoria | S | esta semana |
| 4 | Windows (Defender, updater, OpenVPN, pagefile) | S | esta semana, por el deploy |
| 3 | Lanzadores sin PowerShell | M | uno por día, empezando por máquinas |
| 5 | Un navegador para los dos procesos | M | cuando el portal tenga uso real |
| 6 | Metabase medido y más chico | S | una semana de gc.log, después decidir |
| 7 | t3.large | — | sólo si el vigía avisa dos veces en un mes |

Cada fase se cierra con la misma foto de `/admin/pantallas` (memoria libre y
la tabla por nombre) anotada acá abajo.

## 5. Bitácora

- 05/09 13:13 — fase 0 hecha. Libres 1.880 MB. java 947 · chrome ×18 418 ·
  MsMpEng 365 · powershell ×5 339 · python ×4 297 · svchost ×54 274.
