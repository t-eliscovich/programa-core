# Backlog — Programa Core

_Última actualización: 2026-08-05 (reescrito entero; la versión anterior era del 18/05 y listaba como faltante cosas hechas hace meses)._

**Contexto:** el dBase/FoxPro se retiró el 05/08/2026. PC es la única fuente de
verdad. No hay más syncs ni compares.

Formato: `[tamaño] qué · por qué · dónde` (XS <1h · S 1-3h · M 3-8h · L >1 día).

---

## Traza de la utilidad

### [S] Depositar desde la pantalla de Bancos no deja `mov_doble`
El depósito en LOTE (`/cheques` → depositar) registra un `cheque_depositado`
por cheque, con `no_banco` y `id_transaccion` en la metadata. El depósito que
se carga a mano desde Bancos no registra nada: el cheque desaparece de cartera
sin hecho que lo explique, y el único `mov_doble` que le queda pegado es el
`cheque_aplicado_a_factura` de cuando entró. La traza lo tapa
(`HECHO_NO_EXPLICA_BAJA` + `_unir_las_dos_patas`, 07/08), pero el hecho sigue
sin existir: en `/historial` ese depósito no aparece y no se puede revertir con
el ↺. `modules/bancos/queries.py`.

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

### [S] Un movimiento de banco se carga con la fecha del día, no con una vieja
Dueña 2026-08-07, mirando el +$7.340 de la traza: *"debería ese movimiento
armarse con la fecha de hoy, no con el 05/08"*.

Los dos movimientos de *Comisiones e impuestos 17/06-05/08* (NC $7.404,88 y ND
$64,73) los cargó Alex el **07/08 a las 12:00** con `fecha = 2026-08-05`. Mueven
la utilidad de HOY pero viven en la fila de anteayer, así que quien los busca
por la fecha del salto no los encuentra — le pasó a la dueña en la pantalla de
PICHINCHA. Es la misma trampa que costó el día del 03/08.

Dos pedazos, y conviene el segundo primero:

1. **Que no vuelva a pasar** — la pantalla de carga propone HOY por defecto y
   pide confirmación explícita si la fecha es anterior. `modules/bancos/`.
2. **Corregir esas dos filas** — por la pantalla de bancos, no por SQL.
   🚨 `transacciones_bancarias.saldo` es un saldo corrido ALMACENADO: cambiar
   la fecha reordena las filas y reescribe la cadena hacia adelante. Verificar
   con `/admin/health/cadena-saldos` antes y después.

Mientras tanto, la traza debería decir de qué FECHA es el movimiento bancario y
no sólo el importe.


### [M] Rebuild de static/tailwind.css (aprobado 05/08)
El build está congelado: 745 clases definidas vs 1.581 usadas → **~488 clases
fantasma** que no renderizan sin avisar (`leading-none` ×21, `w-16` ×18,
`text-[9px]` ×27, casi todos los `dark:`/`file:`/`focus:`). Regenerar con la
CLI de Tailwind escaneando `templates/**` + `modules/**/templates/**`, agregar
paso de CI o make target, y **smoke visual pantalla por pantalla** después
(488 clases activándose de golpe pueden mover layouts).

### [L] Limpieza del código dBase — SEPTIEMBRE 2026, no antes
~15k líneas: `modules/admin_dbase/` (35 views, mayoría debug de la migración),
`scripts/sync_dbase_actual.py` (919), el boot-sync de PICHINCH.xlsx en `app.py`
(~L172) que corre en cada arranque, `/admin/dbase-sync`, `/admin/dbase-compare/*`.
**OJO**: `sql_console_view`, `salud_view`, `migraciones_view`, `deploy_view`,
`health_audit_view` viven en ese módulo pero NO son dbase — separarlos primero.

---

## Deuda conocida

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

## Decisiones registradas (NO reabrir)

- Las 26 rutas sin control de permisos (8 de escritura) quedan ABIERTAS a
  propósito — "está bien que puedan" (05/08).
- Compras con kg=0 desde Asinfo: aceptado. Activos sin tipo (~$655k): aceptado.
- No se cargan más aliases cliente Asinfo↔PC: sucursales por dirección.

---

## Proceso

### [S] Checks de drift en /admin/health/all
Cada par de fuentes que debe coincidir, con check automático:
`config/roles.py` ↔ `seguridad.permiso` (drift ya visto: `cupos.editar`),
clases de templates ↔ tailwind.css, links hardcodeados ↔ url_map
(generalizar `test_historial_links_resuelven` a TODOS los templates).

---

_Mantener al día: al cerrar un item, borrarlo de acá en el MISMO commit._
