# Backlog — Programa Core

_Última actualización: 2026-09-04._

**Contexto:** el dBase/FoxPro se retiró el 05/08/2026. PC es la única fuente de
verdad. No hay más syncs ni compares.

Formato: `[tamaño] qué · por qué · dónde` (XS <1h · S 1-3h · M 3-8h · L >1 día).

---

## Portal del cliente

El plan vive en `docs/notas-de-sesion/PLAN_PORTAL_CLIENTE_2026_08_24.md` (fases,
tablas, frenos). Lo que falta de la v1:

### [S] Fase 4 por MAIL: lo que queda después de la pantalla
La pantalla /portal-aviso ya está (04/09): prueba a la casa, interruptor
apagado, lista de a quién le va, bitácora. Falta: (1) que la dueña vea la
prueba y ajuste el texto; (2) el "sale solo los lunes" —hoy se aprieta el
botón—, que es un cron sobre `envio.mandar` con el interruptor prendido;
(3) el correo de cada vendedor en `seguridad.usuario.email` para que el
Reply-To le llegue a él (hoy varios no lo tienen: la respuesta vuelve al
remitente).

### [S] Sus pagos y cheques en detalle
Ya se muestran los cheques en cartera; falta el detalle del pago aplicado.

### [XS] Cliente de prueba AJT: sacarle el correo de Tamara de la ficha
Antes del lanzamiento real. Se le cargó `teliscovich@gmail.com` en
`cliente.correo` (04/09) para probar el portal; la ficha no tenía correo.

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

### [S] El smoke de rutas recorre la app como ANÓNIMO
Destapado el 18/08 arreglando el CI flaky. `tests/test_routes_smoke.py` parece
recorrer las ~2.500 rutas con un usuario logueado y wildcard, pero el
`fake_loader` que arma se le pisa a `auth` y **no a la copia que `app.py` tiene
del nombre** (`from auth import load_logged_in_user`). O sea que las rutas
contestan 302 al login — que no es 500 y pasa el assert.

Medido: si se le pisan las DOS copias, el smoke entra de verdad… y
**`/admin/health/simulacro-cierre` devuelve 500**.

⚠ **Ese 500 es del ENTORNO DE TESTS, no de producción** (verificado 26/08/2026).
En producción la ruta devuelve 200 y su veredicto es OK. Local revienta con
`relation "scintela.iniciales" does not exist`: la tabla no está en
`tests/fixtures/legacy_minimal_dump.sql`. Real, pero de fixture — no hay que
correr a arreglar la pantalla del cierre.

Queda: (a) meter `iniciales` en el dump de prueba, (b) decidir si el smoke debe
entrar logueado —ojo que `/admin/health/all` ES el cron y hacer GET tiene
efectos—, y (c) recién ahí endurecerlo.
No se tocó en el mismo commit que el arreglo de flakiness a propósito.


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

### [M] La CARTERA de `informe_balance_as_of` no cierra

Encontrado el 26/08/2026 mirando el simulacro de cierre. `crear_snapshot_historia`
tiene dos caminos: mes en curso → balance VIVO; mes pasado → reconstruido con
`informe_balance_as_of`. **El segundo devuelve una cartera 3,5 M más baja.**

Contra el cierre de julio, el dry-run daba **4.090.093** cuando la foto guardada
dice **7.593.520**. La guardada es la buena: la serie no tiene un solo salto —
6.472.510 (31/03) · 6.902.089 · 7.055.192 · 7.249.666 · **7.593.520** (31/07) ·
7,77-7,79 M (20 al 26/08). Una cartera de 4 M en julio sería caerse a la mitad y
volver sola en agosto.

Y se desvía **sólo la cartera**: stock, químicos y retiros dan idénticos entre la
foto guardada y el dry-run. De la cartera se contagian patrimonio (−5,88 M) y
utilidad (−5,67 M).

Pista, sin confirmar: el número se parece a "lo que sigue abierto HOY" —
facturas de julio o antes todavía abiertas (3.035.886) + cheques en cartera
(688.296) = 3.724.182. Está cerca de 4.090.093 pero **no da igual**: sobran
366 mil. La dirección parece ésa; el mecanismo exacto no está probado.

⚠ Importa más allá del simulacro: `informe_balance_as_of` es lo que alimenta
**cualquier lectura de un mes pasado**.

🔒 Mientras tanto (26/08) el simulacro **no muestra el Δ** salvo el último día
del mes, que es cuando toma el camino vivo (`se_puede_comparar_la_foto`). El
freno está porque la pantalla decía *"el Δ es lo que se corrige al rehacer la
foto"*, y con ese número rehacerla rompe un cierre que estaba bien.

---

## Aprobado por la dueña, pendiente de ejecutar

### [XS] La traza no dice de qué FECHA es el movimiento bancario

⚠ **Reescrito el 26/08/2026** — el item anterior estaba mal en sus dos partes.
Decía que Alex había cargado el ND de *Comisiones e impuestos 17/06-05/08*
($64,73) por la pantalla de bancos con una fecha vieja, y pedía (a) que esa
pantalla propusiera HOY y (b) corregir la fila.

**Ninguna de las dos cosas aplica.** Verificado en
`scintela.banco_conciliacion_match`: la fila (tx 45428) nació de la
CONCILIACIÓN, con método `created_from_real_grouped` — el agrupador juntó OCHO
líneas del extracto (comisiones + IVA) en una sola fila del libro. La fecha del
05/08 no la eligió nadie: es la del extracto, o sea la fecha en que el banco
cobró, que es la correcta. No hay nada que corregir y la pantalla de bancos no
está en el medio.

Lo que queda del reclamo original de la dueña (07/08, mirando el +$7.340 de la
traza: *"debería ese movimiento armarse con la fecha de hoy, no con el 05/08"*)
es más chico y es de la TRAZA: **la utilidad se movió el 07/08 pero la fila vive
el 05/08**, así que quien busca el salto por la fecha del día no la encuentra.
Se arregla mostrando la fecha del movimiento en la traza, no tocando la fila.

### [S] Un cobro de $72,30 entra cada dos o tres semanas y nunca se concilia

⚠ **Reescrito el 26/08/2026.** El item decía que faltaban CINCO cobranzas del
05/08 y que esos clientes figuraban debiendo plata ya pagada. **No es así**:
Alex las cargó el 11 y el 12/08 (ADO, DYS, MMA, YGE), están conciliadas, los
cuatro depósitos están en `scintela.cheque` (banco 90, stat B) y los cuatro
clientes tienen **cero facturas abiertas**.

Lo que sí hay, y no es un caso suelto: **siete cobros de $72,30 sin conciliar**,
todos con el mismo formato de concepto `<fecha><código>-BANCO PI-PAG-<número>`
(los otros dicen "TRANSFERENCIA DIRECTA DE <nombre>"):

| fecha | monto |
|---|---|
| 01/06 · 03/06 · 06/07 · 29/07 · 05/08 · 20/08 | 72,30 cada uno |
| 17/06 + 22/06 | 7,43 + 64,87 = 72,30 partido en dos |

**Ninguno tiene factura ni cheque de ese importe detrás** — buscado en
`scintela.factura` y `scintela.cheque`, cero. Son ~$506 entrando cada dos o tres
semanas que nadie imputó nunca. La pregunta no es qué es el del 05/08: es qué es
este cobro recurrente. Ahí sí, preguntarle al banco.

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

### [S] `/stock/fabricacion-tc` arma el balance entero para un pie de página
Medido el 02/09 (Tamara: *"¿páginas lentas?"*): la pantalla tarda lo que el
balance (129 consultas) porque `stock_queries.resumen_stock()` llama a
`informe_balance()` sólo para mostrar "Valor del stock (programa): $ · kg" al
pie. Opciones: pedir el pie con un `fetch` aparte al abrir, o una caché corta
del balance (con cuidado: el balance se relee después de cada carga).
`modules/stock_asinfo/views.py:_fabricacion_page`, `modules/stock/queries.py`.

### [S] `/cheques`: los nombres se buscan ANTES del LIMIT
La consulta del listado (284 ms, `modules/cheques/queries.py` `WITH filtrados`)
resuelve 5 subconsultas correlacionadas (cliente, vendedor, banco, proveedor)
para TODOS los cheques filtrados y recién después corta la página. Mover los
nombres afuera del `filtrados` (después del `LIMIT`) los deja en 50 filas. El
`saldo_acumulado` (window) sí necesita el conjunto entero, pero sobre `c.*`.

### [XS] El termómetro no ve las idas al puente hechas en hilos
`medidor.anotar_puente` es thread-local: lo que `/facturas/dia` pide a Asinfo
desde su `ThreadPoolExecutor` no se cuenta (la pantalla muestra "—"). Pasarle
el contexto a los hilos, o sumar en el hilo principal lo que devuelven.

### [XS] Despacho sin factura: las muestras chicas avisan igual
Un despacho de 0,5 kg (MUESTRAS ING ANDRES, 28/08) enciende la misma alerta
que uno de 988 kg. El de hilo sin orden tiene piso de 200 kg; éste no tiene.
Tamara (02/09): en espera, para ver qué más aparece — mientras tanto la × de
cada renglón lo archiva. Opciones: piso (menos de 5 kg no avisa) o saltear
los que dicen MUESTRA en la nota. `modules/asinfo/despacho_sin_factura.py`.

### [S] `/comisiones/debug` sigue preguntando el mes con EXTRACT

El 26/08 se pasaron a rango de fechas las 14 consultas de
`modules/comisiones/queries.py` (ver la migración 0231 y el comentario de
`_rango_mes`). Las 8 de `modules/comisiones/views.py` quedaron como estaban a
propósito: son el endpoint `/comisiones/debug`, que existe para comparar cuántos
cheques caen en el mes según `fechad`, `fecha`, `fechaing` y `fechaout` — cuatro
columnas de la misma tabla, con `COUNT(*) FILTER`. Eso recorre la tabla entera
por definición y ningún índice lo cambia. Si algún día ese endpoint molesta, lo
que hay que revisar es si sigue haciendo falta, no cómo filtra.

### [S] /facturas: medido, y la dueña lo deja como está (26/08/2026)

Medido en producción sobre el mes en curso: **731 ms** con las 500 filas de hoy,
**466** con 100, **444** con una sola. O sea ~444 ms de piso + ~290 de las filas.
La dueña vio los números y decidió **dejar las 500**: *"ya está, dejemos como
está"*. No volver a proponer bajarlas.

Descartados midiendo, para que nadie los vuelva a sospechar: **Asinfo no es**
(la vista que saltea el puente tarda lo mismo), **los totales del header** son
5 ms y **los conteos de los tabs** 36 ms.

Si algún día el piso molesta, quedan dos hilos ya investigados:

1. `buscar()` cuesta 120 ms aunque muestre UNA fila: arma el universo filtrado
   entero —con el LATERAL a `cliente` por fila— y le calcula el saldo corrido
   con una window, y recién ahí aplica LIMIT/OFFSET. Paginar no baja ese pedazo.
2. No hay índice PLANO sobre `scintela.cliente(codigo_cli)`; el único es
   funcional (`upper(trim(...))`) y ese predicado no puede usarlo. Hoy lo salva
   el Memoize de Postgres.
   ⚠ **Probado y NO sirve** reescribir el LATERAL con `upper(trim(...))` para
   usar el índice que ya existe: sale peor (896 ms contra 235). El camino es
   crear el índice plano y medir.

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

### [XS] Los pedazos de nota de débito que quedaron en /bancos/nuevo-movimiento
Desde el 2026-08-24 la nota de débito se emite en `/bancos/emitir-cheque?doc=ND`
(dueña: *"tiene que ser igual que emitir cheque, misma pantalla"*) y
`/bancos/nuevo-movimiento?doc=ND` sólo redirige. En esa pantalla quedaron sin
uso los bloques `{% if doc == 'ND' %}` del template y, en la vista, el parseo de
`mov_destino` / `anticipo_prov` / `gasto_num` / `cuentas_op`: `doc` ahí sólo
puede ser DE o NC. No molestan (no se renderizan), pero confunden al que lea.

⚠ El ruteo por concepto mágico de `_routear_mov_simple` (RR / CAJA / INOP /
anticipo) **NO se toca**: lo sigue usando `/bancos/cargar` (la carga
multi-línea) y la conciliación. Lo que se borra es la UI que ya nadie ve.
Va con la limpieza de septiembre.

### [S] `autobap_log` global → por persona
Las novedades de proveedores son por persona desde el 31/07 (mig 0147), pero
`autobap_log` sigue global: lo que uno vio, desaparece para todos.

### [S] Cupo de crédito cargado en el 11% de los clientes
La ficha muestra cupo y descuento (38f900bb) pero el dato está casi vacío:
**458 de 3.986 clientes** tienen cupo (medido 26/08/2026).

**Los carga Andrés, cuando pueda** (dueña, 26/08/2026). No hay nada que hacer
del lado del programa: la pantalla de carga masiva ya existe
(`/clientes/cupos-carga`, xlsx → preview → confirmar). Es esperar.

### [XS] Comisiones: la rama muerta de `scintela.cobro`
⚠ Este item estuvo MAL REDACTADO hasta el 24/08 ("la rama de cobros no-cheque
suma contra una tabla que nadie escribe"), y se leía como si los cobros que no
son cheque no se estuvieran contando. **No falta nada.** La cobranza del mes
suma tres ramas y los cobros no-cheque entran por las otras dos:

- depósitos y efectivo viven en `scintela.cheque` con `no_banco` 90/91/99
  (en agosto: 639 filas de DEP.PICH. y 85 de efectivo) y entran por la rama de
  cheques acreditados. Que el 24/08 hayan salido de la PANTALLA `/cheques` no
  los sacó de la tabla;
- los cobros que entraron a caja sin fila de cheque entran por la rama de
  `scintela.caja` (`_CAJA_COBRO_FROM`; en agosto 95 entradas, $140.473,95).

Lo que queda es sólo código muerto: `scintela.cobro` tiene **una** fila, del
14/03/2024, así que su rama aporta $0 a cualquier mes. El propio comentario de
`modules/comisiones/queries.py` lo dice: *"la rama que `scintela.cobro`
prometía y nunca cumplió"*. Sacar la rama de los cuatro queries que la
nombran (`cobranza_periodo`, la del detalle, la de `views.py` y la de
`mi_cartera`) y la tabla. Va con la limpieza de septiembre.

### [S] Comisiones: cheque que rebota en el mes siguiente (PENDIENTE, dueña 05/08)
La comisión se pagó sobre plata que no entró. Inclinación de la dueña
(05/08, preliminar): **descontar el día/mes en que rebota** — confirmar con
ella el detalle antes de implementar (¿resta de la cobranza del mes del
rebote? ¿y si vuelve a cobrarse después?).

### [XS] Volver a medir los tests contra el orden, de vez en cuando

⚠ **Re-medido el 26/08/2026: los 50 de la lista del 13/08 YA NO FALLAN.** Cinco
semillas, en serie y en paralelo con el reparto más agresivo
(`-n 4 --dist worksteal`): cero. Se arreglaron en el camino, casi seguro con el
snapshot/restore de hooks que hoy hace la fixture `app` del conftest. El item
quedó abierto meses sin que nadie volviera a medir.

Lo que sí apareció al re-medir —y se arregló el mismo día— fueron tres tests de
`test_conciliacion_sesion_orfana_c1379abc.py`: `sesion.tabla_existe()` cachea
por PROCESO y el primero que preguntaba contestaba por toda la suite. Con la
base falsa daba `False` y desde ahí banco-v2 redirigía al hub. Ahora hay
`olvidar_si_existe_la_tabla()` y una fixture autouse que fija la verdad de
producción.

Lo único que queda es la costumbre: correr la suite barajada cada tanto. Cómo,
y el atajo para bisecar (dentro del archivo, no entre archivos), en
`docs/tests_dependientes_del_orden.md`.

✅ **El hallazgo de producción que salió con esto ya está arreglado**
(26/08/2026): `tabla_existe()` cacheaba también el `False` del `except`, así que
un segundo de base inaccesible al arrancar dejaba la conciliación v2
redirigiendo al hub hasta el próximo reinicio, sin avisar. Ahora el error no se
cachea —se loguea y se vuelve a preguntar—; la respuesta buena, incluso el «no
está», se sigue cacheando. Tres tests en
`tests/test_tabla_existe_no_cachea_el_error.py`.
### [S] La fila no lleva a ningún lado
En /pedidos el color se abre y muestra quién pidió y qué se está tinturando.
Acá, cuando algo está en rojo, no hay adónde ir a ver el detalle. El destino
natural es `/pedidos/color/<codigo>`, que ya existe.

### [M] Nadie mira la pantalla sola
Hoy hay que acordarse de entrar. Lo que entra en rojo podría avisar por la
campanita o por la nota diaria. Ojo con el ruido: 62 en rojo hoy es
demasiado para un aviso — tendría que avisar sólo lo que CAMBIÓ de estado.

### [L] El percentil 90 es una convención, no un cálculo de costos
Anotado por el propio análisis (`Predicción de ventas/PLAN_ML.md`, etapa 4):
*"el P90 que uso hoy lo puse yo a ojo"*. Si sobrar cuesta 3× lo que faltar, el
cuantil correcto sería más bajo y todo el plan de teñido cambiaría. Sale de
medir el costo real de un sobrante contra el de un faltante.

---

## Cachés que tratan un fracaso como dato (audit 30/08/2026)

El 30/08 se arreglaron los cuatro peores (`_descubrir_detalle_fp`,
`ingresos_fabricacion_mes`, `compras_locales_asinfo`, `codigo_por_direccion`).
El resto del audit, ordenado — el patrón correcto ya existe en el repo:
`fetch_dataset_estado` + `_cache_put(..., ok, ttl)` (fracaso vence a 30 s).

### [M] quimicos_flujo asume que formulas_db devuelve None y devuelve []
`modules/informes/quimicos_flujo.py:102/339/405` — `_lib/formulas_db.fetch_all`
devuelve `[]` al fallar, así que los `if rows is None` son código muerto:
con formulas caído se cachean 240 s de ceros (desglose químico, ajustes,
físico de colorantes) y la columna QUÍM.$ del flujo sale en cero con un
ajuste falso. El balance NO se toca (`_VQX_ULTIMO_BUENO` lo protege).

### [M] El caché de la pantalla Flujo producción congela el contexto degradado
`modules/informes/views.py:2956` — el guard mira `error`/`data` (Postgres)
pero cachea también `inv_asinfo`/`mov_asinfo`/`prod_tej_asinfo`: con Asinfo o
formulas caídos y Postgres sano, la pantalla queda 300 s con los ceros aunque
las fuentes vuelvan. Sumar `disponible`/`_cache_ok` al guard.

### [S] Dos pantallas de stock cachean el [] de un timeout
`modules/asinfo/service.py` `stock_en_proceso` (~1085) y `stock_asinfo_lote`
(~936): 300 s de "no hay nada" en Stock en proceso y Stock por lote (pantalla
y CSV; el balance no — sus anclas ya usan `_estado`). La función de al lado
(`stock_asinfo_lote_totales`) ya tiene el patrón bueno: copiarlo.

### [S] El mapa de aliases cae al fallback hardcodeado por 5 min
`modules/asinfo/aliases.py:~100` (`_refrescar`): ante un hipo de la DB queda
el mapa de 3 aliases hardcodeados el TTL entero; los agregados por pantalla
dejan de aplicarse. Mismo arreglo que `codigo_por_direccion` (ya hecho).

### [S] Flags set-once que un fallo deja mal hasta reiniciar
`modules/activos/queries.py:120` (`_HAS_ORDEN_MANUAL = False` para siempre —
la de al lado, `_tiene_borrado`, ya lo hace bien) y los `_bootstrapped` de
saldo_snapshot / papelera_pendientes / concepto_cobro / nota_usuario /
mail_asinfo / primera_compra_asinfo / bancos/apertura: si el CREATE falla una
vez, no se reintenta. Mover la bandera adentro del try.

### [S] fetch_card no marca el estado del bridge
`modules/_lib/metabase_client.py` — `fetch_card` no llama `_marcar()`: el que
quiera cachear un card con `ultimo_fetch_ok()` va a leer un ok ajeno. Hoy no
muerde (los usuarios de cards guardan con `if rows:`), pero es una trampa
armada para el próximo.

### [L] periodo_guard se apaga si el import falla al arrancar
`periodo_guard.py:30-44` — con `_periodos_q = None` el guard de meses
cerrados no bloquea nada el resto de la vida del proceso (salvo
`PERIODO_GUARD_STRICT=1`). Determinístico, no transitorio, pero vale saberlo.

### [M] amortizaciones_mensuales() capaz se come el DEPRACT de activos tipo 'T'
`queries.py:2366-2417` -- `depract = by.get("I", 0.0)` después de
`GROUP BY UPPER(TRIM(tipo))`, sin sumar `'T'`. Mismo patrón que el bug real
que ya se arregló hoy en `balance_components_as_of` (le faltaba 'T'), pero acá
en el costo de tejeduría/tintorería (DCC/DTJ), no en el balance. Severidad
baja porque un terreno normalmente no amortiza (`cuota` debería ser 0 para
tipo 'T'), pero `activos_totales()` documenta que SÍ hay reclasificaciones
I→T en la práctica -- no se pudo confirmar con datos reales (RDS con IP-lock).
No tocar sin decisión de Tamara: si hay algún activo 'T' con cuota > 0, sumar
ese bucket movería la utilidad en vivo. Agregar también un test de
inspección de fuente (como `test_balance_components_as_of_terrenos.py`) que
blindee `activos_totales()` contra esta regresión.

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
