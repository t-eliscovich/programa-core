# Backlog — Programa Core

_Última actualización: 2026-08-13._

**Contexto:** el dBase/FoxPro se retiró el 05/08/2026. PC es la única fuente de
verdad. No hay más syncs ni compares.

Formato: `[tamaño] qué · por qué · dónde` (XS <1h · S 1-3h · M 3-8h · L >1 día).

---

## Poner red donde no hay

### [L] Cobertura real de la conciliación
Medido el 14/08 sacando el `include` de `.coveragerc` (sólo para medir, no se
commiteó): **el repo está al 45,43% — 22.686 líneas sin ningún test que las
pase**. Y el "gate de cobertura 100%" del CI mide **14 archivos** de una
whitelist, no el repo: `banco_v2_view.py` (4.824 líneas) no está.

Decisión de la dueña (14/08): **arrancar por la conciliación**, que es donde
salió todo lo de estos dos días y es lo que Alex usa a diario.

| archivo | cubierto | sin cubrir |
|---|---|---|
| `conciliacion/banco_v2_view.py` | 21% | 1.518 |
| `conciliacion/views.py` | **7%** | 759 |
| `conciliacion/matcher_banco.py` | 24% | 639 |
| `conciliacion/sesion.py` | 41% | 301 |

Orden acordado, siempre **empezando por los caminos que ESCRIBEN** (un test
sobre una pantalla que sólo muestra vale mucho menos):

1. Subir extracto / abrir sesión: archivo vacío, archivo que no es un
   extracto, todo dedupeado, filas nuevas, merge en sesión abierta.
2. Conciliar y deshacer (`views.py`, el del 7%): confirmar match, deshacer,
   N:1, hacer prevalecer.
3. Borrar pendiente y papelera — el camino que el 13/08 estaba roto y **no
   tenía ni un test**.
4. Recién ahí, sumar los 4 archivos al `include` con un piso por archivo,
   para que no puedan volver a bajar. Sin este paso el trabajo se deshace
   solo con el tiempo.

Los otros grandes descubiertos, para cuando toque: `facturas/views.py` (24%,
1.282), `cheques/views.py` (37%, 1.001), `bancos/views.py` (20%, 744),
`informes/views.py` (31%, 1.138).

---

## Barrido de "cosas que nadie miraba"

### [M] Seguir buscando lo que falla en silencio
Pedido de la dueña el 13/08, después de una tarde en que casi todo lo que
apareció tenía la misma forma: **algo que no anda y no avisa**. En un día
salieron cinco así, ninguno reportado por nadie:

- dos migraciones que nunca corrieron porque su número ya estaba usado
  (`0062`, `0064`) — el migrador las daba por aplicadas;
- cinco archivos de migración fantasma en el server, de renumeraciones de
  junio/julio, porque el deploy nunca borra lo que el repo dejó de tener;
- el chip de cliente mostraba `prov` fuera lo que fuera, y en 161 filas eso
  no era ningún cliente (`EQUE` salía de "Cheque");
- la oficina faltaba en las 189 pendientes sin que nada lo dijera;
- el concepto se recorta a 50 caracteres sin avisar (6 movimientos cortados).

Los cinco están cerrados **con su candado** (test, guard o freno de deploy).
Lo que queda es el método, que rinde: buscar dónde el sistema *podría* estar
callándose. Pistas concretas todavía sin mirar:

- ~~Cruzar TODA columna que el código nombra en un INSERT/UPDATE contra el
  esquema~~ **HECHO 13/08**: 668 pares (tabla, columna) de INSERT/UPDATE en
  74 tablas, cruzados contra `information_schema`. Cero fantasmas más allá
  del `codigo` que ya se arregló (los 10 que marcó el barrido son SQL armado
  con f-strings, falsos positivos del regex). Esa superficie está limpia.
- **Los `except Exception: pass`: 132 en 54 archivos, y 17 de ellos envuelven
  una ESCRITURA a la base** (medido 13/08). Cada uno de esos 17 es un lugar
  donde un INSERT/UPDATE puede fallar y la pantalla sigue como si nada. La
  mayoría son best-effort a propósito (snapshots, auditoría), así que NO se
  tocan en masa: hay que mirarlos de a uno y decidir cuál merece al menos un
  `_LOG.warning`. Los más cargados:
  `modules/facturas/views.py` (16 en total), `conciliacion/banco_v2_view.py`
  (11), `conciliacion/matcher_banco.py` (9). Lista completa: regrabar el
  barrido, es un grep de 20 líneas.
- Los campos con `[:N]` que recortan texto del usuario sin decirlo (el
  `concepto` a 50 es uno; ver si hay otros).
- Las 135 filas con `prov` de 2 letras (AC, NC, OP…): códigos de concepto
  guardados en el campo de cliente. De dónde salen.
- 🟡 La diferencia de **$10,18** entre "Saldo banco esperado" (1.875.988,98) y
  el saldo del extracto (1.875.978,80).

---

## Urgente / destapado por el retiro del dBase

### [M] Tintura del mes en curso sin fuente
`scintela.tinto` se alimentaba de TINTO.DBF por el sync. Sin sync, agosto en
adelante debe salir del puente a formulas_app. Verificar que
`_build_tintoreria_mensual` cubra el mes corriente; si "COSTOS DE TINTORERÍA"
da $0 en el mes en curso, es esto. `modules/comparativa_tintoreria/`,
`modules/tintura/service.py`.

### [S] Primer cierre 100% PC (31/08/2026)
El FoxPro ya no corre su devengo POSDAT ni su cuota de amortización ni la fila
de INICIALE. PC tiene equivalentes (cron del EC2 día 1, procs SQL) — verificar
UNO POR UNO en `/admin/health/simulacro-cierre` antes del 31/08.

---

## Aprobado por la dueña, pendiente de ejecutar

### [S] `/retiros` quedó huérfana del menú
`/informes/retiros` ("Dividendos", TMT 2026-05-20) dice explícitamente que
*"reemplaza la antigua /capital + /retiros"*, y ninguna pantalla ni el menú
linkean a `/retiros` — pero el Historial sí, y es el único destino que puede
mostrar un retiro de CUALQUIER fecha: Dividendos sólo sabe mostrar el mes o el
año EN CURSO (`modules/informes/views.py`, sin filtros de fecha). Por eso el
link del historial siguió apuntando a `/retiros` (ahora con `?id=`).

Decisión pendiente de Tamara: o se la deja como pantalla de detalle a la que
sólo se llega por link (como `/compras/<id>`), o Dividendos aprende a mostrar
un retiro puntual y `/retiros` se borra ("no tenemos que tener basura",
2026-07-20).

### [S] Un movimiento de banco se carga con la fecha del día, no con una vieja
Dueña 2026-08-07, mirando el +$7.340 de la traza: *"debería ese movimiento
armarse con la fecha de hoy, no con el 05/08"*.

El ND de *Comisiones e impuestos 17/06-05/08* ($64,73) lo cargó Alex el **07/08
a las 17:00** con `fecha = 2026-08-05`. Mueve la utilidad de HOY pero vive en la
fila de anteayer, así que quien lo busca por la fecha del salto no lo encuentra
— le pasó a la dueña en la pantalla de PICHINCHA. Es la misma trampa que costó
el día del 03/08.

Dos pedazos, y conviene el segundo primero:

1. **Que no vuelva a pasar** — la pantalla de carga propone HOY por defecto y
   pide confirmación explícita si la fecha es anterior. `modules/bancos/`.
2. **Corregir esa fila** — por la pantalla de bancos, no por SQL.
   🚨 `transacciones_bancarias.saldo` es un saldo corrido ALMACENADO: cambiar
   la fecha reordena las filas y reescribe la cadena hacia adelante. Verificar
   con `/admin/health/cadena-saldos` antes y después.

⚠ El OTRO movimiento de ese par (la NC de $7.404,88, tx 45429) **ya no existe**:
el 11/08 se descubrió que no eran comisiones sino CINCO cobranzas de clientes
del 05/08 que el agrupado se tragó por un corrimiento de índices, y se anuló por
`/conciliacion/banco-v2/deshacer`. El bug de código está arreglado; lo que queda
es cargar esas cobranzas — ver abajo.

### [M] Cargar las 5 cobranzas del 05/08 que estaban adentro de la NC $7.404,88

Los cinco créditos del extracto del 05/08 volvieron a quedar **pendientes de
banco** en la sesión abierta (panel Banco). Ninguno está aplicado a facturas, así
que hoy esos clientes figuran debiendo plata que ya pagaron. Tamara (11/08): las
carga Alex por Cobranza, no por script.

| Extracto (doc) | Monto | Cliente | Contra qué |
|---|---|---|---|
| 53443956 | 1.142,96 | MMA · Marroquín Espinosa | FIFO (debe 38.066,95) |
| 56804542 | 72,30 | ❓ sin identificar | — |
| 71519723 | 3.099,52 | ADO · Oñate Oñate | factura 180981, importe idéntico |
| 55685078 | 2.568,54 | DYS · Dayío Sports | sus 4 facturas abiertas, suman exacto |
| 59356148 | 521,56 | YGE · Erazo Melendrez | **ya cargada** el 11/08 (cheque 102153) |

Dos notas: el crédito de YGE **no se vuelve a cargar** — su depósito ya está en
el libro con fecha 11/08 y lo que falta es cruzarlo contra el crédito del 05/08
en el panel de conciliación. Y los $72,30 no tienen cliente reconocible en el
concepto: hay que preguntarle al banco o buscar un saldo de ese importe.

Mientras tanto, la traza debería decir de qué FECHA es el movimiento bancario y
no sólo el importe.

### [L] Limpieza del código dBase — SEPTIEMBRE 2026, no antes

Reconfirmado el 2026-08-09: **se hace en septiembre**, no antes. Lo que sigue
es el inventario ya hecho (se ensayó entero ese día sobre un clon y la suite
quedó en verde: −9.128 líneas), para que en septiembre sea ejecutar y no
volver a investigar.

**Borrar** (`modules/admin_dbase/`, ~7.500 líneas; ninguno tiene fuente desde
el 05/08): `dbase_compare_view` · `views` (`/admin/dbase-sync`) ·
`debug_dbase_compras_view` · `cheques_feching_view` · `clientes_import_view` ·
`proveedores_import_view` · `facturas_reconcile_view` · `posdat_reconcile_view` ·
`totf_1a1_view` · `abonos_historicos_view` · `debug_ustock_view` ·
`debug_yy_view` · `marcar_asinfo_view`. Más `scripts/sync_dbase_actual.py`,
`scripts/sync_stat_from_xlsx*.py`, `data/dbase_snapshots/` y el boot-sync de
`app.py` (~L172) que corre en CADA arranque contra un xlsx congelado.
Tests que se van con ellos: `test_dbase_compare_signos`,
`test_cheques_feching_backfill`, `test_clientes_import`,
`test_facturas_reconcile`, `test_posdat_reconcile`, `test_sync_regresion_guard`,
y los seis `test_dbase_compare_*` de `test_stock_vivo_prg.py`.

**NO borrar** — viven en ese módulo y no tienen nada de dBase: `sql_console_view`,
`salud_view`, `health_audit_view` (el cron diario), `migraciones_view`,
`deploy_view`, `clientes_asinfo_view` + `_detalle`, `ficha_asinfo_view`,
`import_sin_plata_view`, `auto_match_view`, `balance_view`,
`regen_snapshot_view`, y los ocho `debug_*` de Asinfo/producción (la dueña los
deja: 2026-08-09).

🚨 **La trampa**: `tinto_costos_sync.py` tiene DOS funciones y sólo una es del
dBase. `refresh_from_dir()` lee `TINTO.DBF`/`COSTOS.DBF` de un tarball y se va;
`refresh_from_formulas_app()` lee la base VIVA de tintorería y es la que se
usa. Su ruta cuelga de la pantalla que hay que borrar
(`/admin/dbase-sync/tinto-costos/from-formulas`), así que hay que **mudarla a
su propia URL antes** de borrar la pantalla. Lo mismo con su template
(`tinto_costos.html` tiene los dos formularios en la misma página).

**Después**: el módulo deja de tener nada de dBase → renombrarlo
(`admin_dbase` → `admin_tools`, también `templates/admin_dbase/`). Un módulo
llamado `admin_dbase` que contiene la consola SQL y el deploy es la razón por
la que nadie lo limpió antes.

**Guardas que avisan solas si algo se rompe**: `test_ninguna_aceptada_sobra`
(falla si borrás una ruta que está en `accesos.ACEPTADAS` — hay que sacar de
ahí `/admin/dbase-compare/` y `/vivo`), `test_routes_smoke` (renderiza todo
GET) y `test_scope_vendedor`, que nombra `/admin/dbase-sync` como ejemplo de
pantalla cerrada y hay que cambiarle el ejemplo.

---

## Deuda conocida

### [S] `STATS_VIVOS` está definido dos veces, con miembros distintos
`modules/cheques/queries.py` L2818 (`Z,B,1,2,3,D,P,A`) y L4092
(`Z,1,2,3,P,D`): gana el de abajo, que es el que quisieron los dos únicos usos
(cancelar un cheque por anticipo 97). O sea que hoy el comportamiento es el
correcto — pero el de arriba, con su comentario explicando por qué incluye la
`A` legacy, **miente**, y ruff no avisa de una constante redefinida. Con
`STATS_EN_CARTERA` (10/08) ya son cuatro tuplas del mismo concepto en el mismo
archivo. Consolidar en UNA con nombre que diga qué pregunta responde, o dejar
las dos con nombres distintos. Detectado 10/08 auditando `00d11d9`; no es
urgente porque no cambia ningún número hoy.

### [S] `autobap_log` global → por persona
Las novedades de proveedores son por persona desde el 31/07 (mig 0147), pero
`autobap_log` sigue global: lo que uno vio, desaparece para todos.

### [M] 7 pares de códigos de cliente duplicados
BLP BRC JQS LEC YMO JRP MTE — la mig 0155 prohíbe nuevos; estos quedaron.
Cada par necesita decisión (mismo cliente ×2 vs dos empresas reales).
Pantallas: `/admin/clientes-asinfo`, `cambiar-codigo`.

### [S] Cupo de crédito cargado en ~10% de los clientes
La ficha muestra cupo y descuento (38f900bb) pero el dato está casi vacío.
Cargar los cupos reales o la columna es decoración.

### [S] Comisiones: `scintela.cobro` vacía
La rama de "cobros no-cheque" suma contra una tabla que nadie escribe
(1 fila, de 2024). O se llena o se saca.

### [S] Comisiones: cheque que rebota en el mes siguiente (PENDIENTE, dueña 05/08)
La comisión se pagó sobre plata que no entró. Inclinación de la dueña
(05/08, preliminar): **descontar el día/mes en que rebota** — confirmar con
ella el detalle antes de implementar (¿resta de la cobranza del mes del
rebote? ¿y si vuelve a cobrarse después?).

### [S] Capturas 07:00/19:00 de /informes/dia
Verificar en unos días que las capturas programadas corren solas en producción
(la tabla arrancó el 04/08).

### [M] 50 tests dependen del orden en que corren
Medido el 13/08 con la suite en orden aleatorio (4 semillas): 50 tests pasan o
fallan según qué corrió antes, **37 de ellos en `test_mi_cartera.py`** y casi
todos de permisos/login ("sin permiso → 404"). En el orden de siempre pasan, así
que hoy están apoyados en algo que les dejó otro test, no en su propio setup.
Hoy lo tapa `--dist loadfile` (mantiene junto cada archivo); por eso el Makefile
NO usa `worksteal` aunque sea ~6 s más rápido. Atacarlo por archivo: 37 de 50
son uno solo, casi seguro es una sola causa. Lista completa y cómo reproducir:
`docs/tests_dependientes_del_orden.md`.

---

## Decisiones registradas (NO reabrir)

- Las rutas sin control de permisos quedan ABIERTAS a propósito — "está bien
  que puedan" (05/08, reconfirmado el 09/08). Auditadas una por una el 09/08:
  de las 26 que se contaban, **doce no eran un agujero** (redirects, el flow
  de Google que corre antes del login, y las que chequean con un helper). Las
  demás viven en `modules/usuarios/accesos.py::ACEPTADAS` con el motivo
  escrito, y un test impide que esa lista se pudra. La lista roja de
  `/usuarios/accesos` ahora significa "esto apareció y nadie lo miró".
- Compras con kg=0 desde Asinfo: aceptado. Activos sin tipo (~$655k): aceptado.
- No se cargan más aliases cliente Asinfo↔PC: sucursales por dirección.

---

## Proceso

### [S] Checks de drift en /admin/health/all
Cada par de fuentes que debe coincidir, con check automático:
`config/roles.py` ↔ `seguridad.permiso` (drift ya visto: `cupos.editar`).

✅ Hechos: clases de templates ↔ tailwind.css · links hardcodeados ↔ url_map
(`test_drift_estatico`, ya generalizado a TODOS los templates) · valores de
filtro ↔ vocabulario de la pantalla destino (12/08: la traza mandaba
`/historial?tipo=banco_cargado`, que no existe, y abría una pantalla vacía —
`test_links_de_la_traza_no_caen_en_vacio_2026_08_12`).

---

_Mantener al día: al cerrar un item, borrarlo de acá en el MISMO commit._
