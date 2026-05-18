# Backlog — Programa Core

_Ultima actualización: 2026-05-18_

## Pedido dueña 2026-05-18 — UI cleanup pendiente

Cosas que la dueña marcó después del primer batch y todavía no están
hechas. Cada item debe pasar las 3 reglas canónicas del skill antes de
mergear (R1 vocabulario · R2 no scroll en altas · R3 cero redundancia).

### [S] /cartera/aging: sacar los chips "Con email / Con teléfono / Sin contacto"
- **Qué**: en la barra de filtros chip ("Todos · Críticos 90+ · Con email
  · Con teléfono · En STOP · Sin contacto") tres chips no aportan nada
  útil para auditar cartera: la dueña no filtra por presencia de email
  para tomar decisiones de cobranza.
- **Dejar**: "Todos", "Críticos 90+", "En STOP".
- **Sacar**: "Con email", "Con teléfono", "Sin contacto".
- **Dónde**: `modules/cartera/templates/cartera/aging.html`, bloque
  `#quick-filters` (línea ~96). También sacar los handlers JS asociados
  en el `<script>` del mismo archivo.
- **Por qué**: violación de Regla 3 (info que no aporta para la tarea
  principal de la pantalla).

### [M] Consistencia de UI entre formularios de alta
- **Qué**: Compras (Nueva Compra), Ventas (Nueva Factura) y Cobranza
  (Nuevo Cheque) tienen layouts visualmente distintos — distinto header
  (algunos `page_header`, otros `page_hero`, otros `<h1>` plano), distinta
  altura de inputs, distinto patrón de subtítulo, distinto patrón de
  botones de "Cancelar / Guardar".
- **Por qué**: la dueña los usa todos los días; saltar entre ellos no
  debería requerir reorientarse visualmente.
- **Plan**:
  1. Definir patrón canónico (probablemente `.page-shell` + `page-header`
     sin subtítulo + grid 2 cols + footer con Cancelar a la izquierda y
     Guardar a la derecha).
  2. Aplicar a los 3 forms: `compras/nueva.html`, `facturas/nueva.html`,
     `cheques/nuevo.html`.
  3. Considerar bonus: `posdat/form.html`, `gastos/nuevo.html`, `caja/nuevo.html`,
     `capital/aportar.html`, `capital/retirar.html` también deberían
     adoptar el patrón.
- **Riesgo**: tocar `cheques/nuevo.html` (970 líneas con JS complejo)
  puede romper el flujo de aplicación inline a facturas. Testear el
  smoke completo + un alta real antes de mergear.

### [XS] Confirmar atribución de "Me sobran los 2 cuadros de la derecha"
- **Qué**: la frase en la docx aparece entre el párrafo de provisiones y
  el de stock, sin atribución clara. Yo la apliqué a **provisiones** (3
  KPI cards → 1). Pero pudo haber sido **stock** (4 KPI cards: Hilado,
  Tejido, Terminado, Químicos).
- **Plan**: preguntarle a la dueña a cuál se refería. Si era stock,
  reducir las 4 cards a 1 (probablemente "Valor total US$" con
  desglose en tooltip).

### [S] Auditoría completa de botones "Volver"
- **Qué**: en la docx la dueña dijo "así debería haber en todas las
  pantallas, volver a la anterior". El batch inicial cubrió las que
  reportó explícitamente (posdat, compras, estado_cuenta, deudas →
  posdat). Pero quedan muchas detail pages sin "← Volver" explícito:
  factura detalle, cheque detalle, compra editar, posdat editar, cliente
  cuenta corriente, activo editar, etc. La mayoría sólo tienen el
  breadcrumb del header — funciona pero no es el patrón explícito.
- **Plan**: auditoría 1× sobre todos los templates `templates/**/detalle.html`
  y `templates/**/editar.html`, agregar el botón "← Volver" canónico
  cuando falte.

---

## Pedido dueña 2026-05-18 — Features faltantes vs PRG viejo

Estos 4 items fueron pedidos en la docx "Para Claude". Los UI fixes ya
están aplicados; estos quedan en backlog porque cada uno requiere diseño
de datos + decisión de negocio.

### [M] Comisión de vendedores
- **Qué**: módulo que calcule la comisión mensual de cada vendedor según
  las facturas cobradas y un % por vendedor (o por cliente).
- **Por qué**: existe en el PRG viejo, se usa para liquidar a los vendedores.
- **Dónde**: nuevo blueprint `modules/comisiones/`. Lee `scintela.factura`
  (con stat='T' / saldo<importe) + `scintela.cliente.vend` (campo que ya
  existe). El % por vendedor probablemente vive en una tabla nueva
  `scintela.vendedor` con `(codigo, nombre, pct_comision)`.
- **Dependencia**: confirmar con dueña cómo se calcula hoy (qué % aplica,
  si se cobra al facturar o al cobrar, si hay base de exclusiones).

### [S] Efectivo / cheques semana actual + últimas 3 semanas
- **Qué**: pantalla mostrando ingresos (cheques + efectivo) de la semana
  en curso + las 3 anteriores, agregadas por día/cliente.
- **Estado actual**: ya existe `cobranzas.matriz_3_semanas`
  (`modules/cobranzas/templates/cobranzas/matriz_3_semanas.html`) —
  verificar con dueña si cumple, y si no, qué falta.

### [M] Historia: cuadros de resultados/balances de meses anteriores
- **Qué**: ver los balances y P&L de cada mes pasado (drill-down desde
  la lista mensual).
- **Estado actual**: existen `informes/historia.html` y
  `historia_multianual.html`. Verificar si renderizan los cuadros como
  los espera la dueña; si no, agregar la vista "cuadro mensual" (mismo
  layout que /informes/balance pero contra un snapshot histórico).

### [L] Cuadro de Fuentes y Usos
- **Qué**: estado de flujo de fondos clásico — fuentes (utilidad,
  amortización, aumentos de pasivos, disminuciones de activos) vs usos
  (dividendos, disminución de pasivos, aumentos de activos). Período
  configurable (mes/año).
- **Por qué**: dueña usa esto en el PRG viejo para presentar a banco /
  socios.
- **Dónde**: nueva ruta `/informes/fuentes_usos`. Necesita 2 snapshots
  de balance (inicio y fin del período) + el P&L del período. Replica
  INFORMES.PRG sección de fuentes y usos.

---

Esta es la lista completa de lo que falta, ordenada por impacto.  Cada item tiene: **qué**, **por qué**, **dónde**, y un **estimado grueso** (XS <1h · S 1-3h · M 3-8h · L >1 día).

---

## Done en esta sesión

- Auditoría completa del código (views + queries + templates de los 12 módulos). Reporte en la conversación; los bugs reales ya quedaron arreglados, los gaps están abajo.
- Fix `STATS` dict en `modules/cheques/queries.py` — typo `carteta` → `cartera`, y consolidación a una única fuente de verdad (el elif-chain inline era el que contaba, el dict era dead code).
- Fix N+1 en `modules/dashboard/queries.py::actividad_reciente()` — subconsultas correlacionadas en el SELECT reemplazadas por `LEFT JOIN scintela.cliente`.
- Fix `flujo_ultimos_dias()` — reemplazo de `(%s || ' days')::interval` por `make_interval(days => %s)` con coerción defensiva a int.
- `templates/base.html` — print stylesheet completo (sidebar/topbar/botones ocultos, tablas compactadas, `@page` margins, saldo pills degradan a texto plano).
- `scripts/add_indexes.sql` — migración idempotente de índices para todas las consultas frecuentes (fecha DESC, codigo_cli, no_banco, stat, etc.). Aplicar con `psql $DATABASE_URL -f scripts/add_indexes.sql`.
- `tests/test_routes_smoke.py` — smoke test end-to-end que camina las 25 rutas GET registradas con DB stub. Pasa en verde. Correr después de cada cambio: `python3 tests/test_routes_smoke.py`.
- `docs/SCHEMA.txt` — dump en texto plano de las 40 tablas del backup (`intela12042026.sql` es formato pg_custom binario, no grep-able).
- Skill `programa-core` actualizada con los hallazgos de esta sesión (dated note, no rewrite).

---

## v1 — Lo que falta antes de salir a producción (read-only)

### BUGS / RIESGOS

Ninguno conocido. El smoke test camina todas las rutas GET y devuelven <500 con el DB stubeado. Las únicas dudas vivas son:

1. **[S] Verificar que `scintela.transacciones_bancarias` tiene datos en el dump cargado.** Si está vacío, las vistas de bancos/movimientos y el cálculo de saldo bancario quedan sin datos aunque la query sea correcta. Check: `SELECT COUNT(*) FROM scintela.transacciones_bancarias;` debería devolver >0.
2. **[S] Confirmar el mapping `scintela.banco.no_banco` → nombre.** El dashboard y los informes asumen 1=Pichincha, 2=Internacional, 9=pasivos/cerrada. Confirmar con la factoría que sigue siendo así.
3. **[XS] Rotar `SECRET_KEY` en `.env` antes del primer login real.** El launcher actual pone un default de desarrollo.

### UX / POLISH restante

4. **[S] Leyenda de códigos `stat` en la lista de cheques.** Los usuarios nuevos no saben que Z=cartera, D=depositado, R=rebotado, A=acreditado, P=postdatado. Tooltip sobre el header de la columna o un modal "¿Qué significan los códigos?" bajo la tabla.
5. **[XS] Columna `activo` visible en listas de clientes y proveedores.** El schema tiene el campo, pero hoy no se muestra — el usuario no ve quién está bloqueado.
6. **[S] Estilo del row de totales.** Muchos reports calculan `total_importe`, `total_saldo` en `views.py` y los pasan al template, pero el row de total se renderea como una fila más. Debería ser `bg-slate-100 font-bold`, sticky-bottom en reports largos.
7. **[XS] `num_es` / `money_es` / `kg_es` consistencia en CSV.** Algunos CSVs exportan `importe` con `num_es` en vez de `money_es` (2 decimales fijos) o `kg` sin `kg_es`. Revisar columna por columna en `exports.csv_response()` callers.

### Performance

8. **[XS] Aplicar `scripts/add_indexes.sql` a la BD productiva** antes del primer día con tráfico real. Todo es `CREATE INDEX IF NOT EXISTS`, seguro de correr.
9. **[M] Perfilar las 3 queries más lentas del dashboard Dueño** con `EXPLAIN ANALYZE` — tend_cartera, tend_saldo, actividad_reciente — y confirmar que usen los índices nuevos.

### Tests

10. **[M] Extender smoke test para rutas con path params.** Hoy se skipean `<int:id>` routes. Agregar un pase que haga INSERT de una fixture mínima (1 cliente, 1 factura, 1 cheque) y visite `/facturas/<id>`, `/cheques/<id>`, etc.
11. **[S] Test unitario para `_tendencia()` y `saldo_pill`.** Lógica aislada, fácil de probar.

---

## v2 — Funcionalidad transaccional (falta legado)

El app es read-only hoy. La parity audit contra el programa dBase identifica estos huecos, en orden de impacto:

### A. Crítico para reemplazar el dBase

12. **[L] `facturas.anular`** — ruta `POST /facturas/<id>/anular` que fija `factura.stat` a código de cancelado, crea row en `bitacora_migracion` o tabla audit. Requiere CSRF.
13. **[L] `cheques.depositar` + `cheques.reversar`** — flujo Z→D (depositado) y D→R (rebotado). Si estaba aplicado a facturas, re-abrir saldos en `chequesxfact` dentro de la misma transacción.
14. **[L] `retenciones.emitir`** — crear `scintela.retencion` vinculado a una factura. Auto-cálculo basado en `proveedor.retbase` y `proveedor.retiva` con override manual.
15. **[L] `compras.crear`** — nuevo `scintela.compra` + row espejo en `scintela.posdat` (obligación de pago).
16. **[L] `cheques.aplicar_a_factura`** — escribir rows en `chequesxfact` en una sola transacción. UI: pantalla con el cheque en el header y una tabla de facturas pendientes del mismo cliente con checkboxes + importe.

### B. Importante pero no bloqueante

17. **[M] Modulo `posdatos`** — CRUD de `scintela.posdat`, obligaciones de pago a proveedores. Filtro por `banc` y `fechad`.
18. **[M] `caja.crear`** — registro de movimientos de caja (ingreso/egreso).
19. **[M] `provisiones` CRUD** — provisiones recurrentes (alquiler, servicios).
20. **[M] `capital` CRUD** — movimientos de patrimonio (aportes, retiros del dueño).
21. **[L] `proformas.crear_detalle` + `proformas.editar_detalle`** — edición línea-a-línea de `proforma_detalle`.

### C. Calidad de datos a mediano plazo

22. **[L] Valuación de stock en vivo.** Hoy `balance()` lee `scintela.historia.stock` (snapshot mensual). Reemplazar por sum live desde tabla de inventario (fuera de scope v1).
23. **[M] Tarea mensual scheduleada.** Llamar `scintela.procesa_provisiones(CURRENT_DATE)` y `scintela.actualizar_amortizacion()` el día 1 de cada mes vía cron o launch daemon. Checar `ult_mes_amortizado` para idempotencia.

### D. Seguridad para fase transaccional

24. **[S] Flask-WTF + CSRF tokens** en todas las rutas POST/PUT/DELETE. No aplicable hoy (read-only), obligatorio el día 1 de v2.
25. **[S] Validación de inputs en filtros.** Fechas hoy se pasan sin parsear — `datetime.fromisoformat(desde)` con fallback a None.
26. **[M] Audit log (`seguridad.bitacora`) de cada escritura** — username, módulo, acción, row_id, before/after JSON. Usable también para rollback manual.
27. **[M] Rate limit en `/login`** — bloqueo temporal después de N intentos fallidos.

### E. Operación

28. **[S] Scheduled task de backup diario** de la BD a S3 us-east-2. Ver `intela-aws-deploy` para el patrón.
29. **[S] Endpoint `/salud`** que devuelva `{"db": "ok", "version": "...", "rev": "..."}` para el monitor del launcher/load-balancer.
30. **[M] Despliegue a AWS.** Cuando el v1 esté pulido, migrar de `launcher.sh` local → EC2 + RDS siguiendo el skill `intela-aws-deploy`.

---

## Chrome MCP file:// — problema conocido

El tool `mcp__Claude_in_Chrome__navigate` antepone `https://` a cualquier URL, incluyendo `file://`. Resultado: `https://file:///...` no carga. Workaround que probé esta sesión (asignar `window.location.href` vía `javascript_tool`) también falla porque Chrome bloquea redirects cross-protocol a `file://`.

Recomendación: abrir los archivos estáticos de `preview/` con doble click en Finder (Chrome es el default para `.html` en tu Mac). O reportar el bug a Anthropic desde el botón 👎 en cualquier respuesta donde intente usar esa herramienta.

---

## Cómo priorizar cuando vuelvas

1. **Si quieres salir a producción con el app read-only ya:** aplica `scripts/add_indexes.sql` (item 8), rotá `SECRET_KEY` (3), confirmá `transacciones_bancarias` tiene datos (1), y desplegá. El resto de v1 es cosmético.
2. **Si vas a seguir construyendo para reemplazar el dBase:** empezá por items 12-16 en orden (ellos son el 80% del uso diario del programa viejo).
3. **Si el foco es auditoría / compliance:** items 26 + 28 primero.

---

_Mantén este archivo al día. Cada vez que cierres un item, movelo a "Done en esta sesión" con la fecha. Si agregás uno nuevo, respeta el formato `[tamaño] descripción` y agrupalo por sección._
