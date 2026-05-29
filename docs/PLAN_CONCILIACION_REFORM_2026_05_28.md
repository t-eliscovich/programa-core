# Reforma absoluta de Conciliación bancaria — 2026-05-28

**Autora del pedido:** Tamara (dueña).
**Frase de origen:** "Reform absoluto de conciliaciones. […] Todo el resto necesito que borres, esta muy cargado, muy dificil."

## Objetivo

La pantalla post-procesar de `/conciliacion/banco` (`hub()` → `banco_resultado.html`) acumuló 3.367 líneas de template, ~10 endpoints distintos y mil cards/CTAs. La dueña no la puede usar. Reemplazarla por una pantalla **mínima, de 3 tabs, con un saldo en vivo arriba que se actualiza con cada conciliación**.

Lo que NO se toca:

- La pantalla inicial de carga (`/conciliacion/` → `index()` → sube el extracto). Funciona perfecto.
- El bloque "Conciliación bancaria · Pichincha" con el balance Saldo a conciliar → Saldo banco esperado (lo que ya queda lindo). Se copia tal cual y se reusa.
- El matcher (`matcher_banco.py`). Las pasadas P0–P4 quedan iguales — solo cambia cómo se presenta el resultado.
- Los endpoints de escritura existentes (`confirmar_match`, `crear_transaccion_agrupada_desde_reals`, `romper_match`). Se reusan tal cual.

Lo que SÍ se elimina:

- `banco_resultado.html` entero — se reemplaza por una nueva pantalla con 3 tabs.
- Los cards/CTAs de "Aceptar BANCSIS only", "Aceptar real only", "Crear bancsis individual", "Marcar depósitos del día", "Diag", "Self-test" en la post-procesar.
- Endpoints derivados que ya no se usen post-reforma (se marcan deprecated, no se borran en este sprint).

## Flujo objetivo (pantalla post-procesar)

```
┌──────────────────────────────────────────────────────────────────┐
│ Conciliación bancaria · Pichincha               último mov · DD  │
│                                                                  │
│   SALDO A CONCILIAR                                              │
│   $ 2,559,169.47   ← se actualiza con cada conciliación          │
│                                                                  │
│   Saldo PC libros (live)            2,605,247.89  …              │
│   − Pendientes PC (56)              + 66,425.67                  │
│                                     − 20,347.25                  │
│                                     neto +46,078.42  ← grande    │
│   = Saldo conciliado live           2,559,169.47                 │
│   + Pendientes históricos banco     + 203,645.90                 │
│                                     − 4,995.56                   │
│                                     neto +198,650.34  ← grande   │
│   = Saldo banco esperado            2,757,819.81                 │
├──────────────────────────────────────────────────────────────────┤
│  [ Manual ]  [ Impuestos y comisiones ]  [ Transferencias x doc ]│
├──────────────────────────────────────────────────────────────────┤
│  (contenido del tab activo — ver abajo)                          │
└──────────────────────────────────────────────────────────────────┘
```

El balance arriba es el mismo widget que ya existe en `banco_upload.html` (ya rediseñado, con el neto grande tras el commit 35788d7). Se mueve a un partial `_balance_pichincha.html` con DOS modos de render:

- **Modo `full`** (default) → el bloque grande como hoy, hero + tabla completa. Va en la pantalla de carga inicial.
- **Modo `compact`** → versión "barra" achicada, sticky en la parte superior de cada tab post-procesar. Mismo número, mismas filas, pero en tipografía 11–12px y altura ~1/3 del original. Suficiente para chequear el saldo mientras se concilia sin comerse pantalla.

```
Modo compact (sticky arriba de cada tab):

┌─────────────────────────────────────────────────────────────────────┐
│ SALDO A CONCILIAR  $ 2,559,169.47   últ. mov 2026-05-28             │
│ libros 2,605,247.89  − pend PC neto +46,078.42  = concil. 2,559,169.47│
│ + pend banco neto +198,650.34   = banco esperado 2,757,819.81        │
└─────────────────────────────────────────────────────────────────────┘
```

Se actualiza con cada conciliación (el redirect post-confirmar recalcula live).

## Tab 1 — Manual

**Propósito.** Lo que el matcher dejó como "sin parear" en los dos lados, ordenado, con checkboxes para marcar manualmente de a varios.

**Layout.** Dos paneles lado a lado (el screenshot adjunto):

- Izquierda: **Banco sin parear** (los `real_only` del matcher) — fecha, ±, monto, doc banco, concepto. Checkbox al inicio.
- Derecha: **Programa sin parear** (los `bancsis_only`) — fecha, ±, monto, doc banco, cliente/concepto. Checkbox al inicio.

Orden default: **monto descendente** en ambos paneles (la dueña pidió "de mayor a menor"). Mantener el filtro de texto que ya existe (filtra concepto/cliente/doc/monto).

**Total acumulado arriba.** Sobre la cabecera de los paneles, mostrar:

```
Seleccionado banco: $ X,XXX.XX  (N items)
Seleccionado programa: $ Y,YYY.YY (M items)
Diferencia: $ Z,ZZZ.ZZ   ← verde si es 0, rosa si ≠ 0
```

Se actualiza con JS al toquetear los checkboxes. Sin form submit hasta el botón.

**Botón "Conciliar".** Habilitado solo cuando hay al menos 1 marcado de cada lado.
- Click → abre un modal/pantalla intermedia con el **resumen 1 a 1**:

  ```
  Vas a conciliar estos movimientos:
    Banco 28/05  + 235.22  TRANSFERENCIA DIRECTA AG  ↔  Programa 28/05  + 235.22  MONICA PONCE
    Banco 28/05  − 0.05    IVA COBRADO              ↔  Programa 28/05  − 0.05    [crear bancsis]
    ...
  Diferencia: $ 0.00 ✓
  [ Cancelar ]  [ Confirmar ]
  ```

- Si el pareo 1:1 no cuadra (monto/cantidad), el modal lo dice y bloquea Confirmar.
- Confirmar → para cada par marcado dispara `confirmar_match()` (atómico en una sola `db.tx`).
- Vuelve a la misma pantalla del tab Manual. El balance arriba refleja los nuevos matches; las filas ya conciliadas desaparecen.

**Implementación backend.**
- Endpoint `POST /conciliacion/banco/manual/preview` → recibe `real_ids[]` + `bancsis_ids[]` → devuelve JSON con el preview 1:1.
- Endpoint `POST /conciliacion/banco/manual/confirmar` → recibe pares ya validados → `for par in pares: confirmar_match(...)`. Flash + redirect al tab Manual.

**Lo que ya NO está en el tab Manual.**

- Las sugerencias auto-matched del matcher (P2/P3) → pasan al tab Manual con un toggle "incluir sugerencias", **off por default**. Que la dueña vea solo lo realmente sin parear; las sugerencias quedan a un click.
- El "Aceptar BANCSIS only" / "Aceptar real only" en bulk → se elimina como bulk action. Si necesita aceptarlos se selecciona y va al modal de Manual.

## Tab 2 — Impuestos y comisiones

**Propósito.** Los movs del extracto que el matcher etiquetó como `COMISION` por `_es_comision_real` (IVA, COMISION, PAGO SENAE, etc.). Se acoplan automáticamente: el flujo natural es ver el subset, confirmar, y crear UNA tx BANCSIS agrupada (N:1).

**Layout.** Una sola tabla:

```
☑ Todos        (toggle)
Día        Tipo      Monto    Doc        Concepto
05-28      −          0.05    16236420   IVA COBRADO 51469932
05-28      −          0.05    16236357   IVA COBRADO 51470492
05-27      −          0.05    43158726   IVA COBRADO 51436622
05-28      −          0.31    16241797   COMISION COBRADO 51469932
05-28      −          0.31    16241725   COMISION COBRADO 51470492
05-27      −          0.31    43158676   COMISION COBRADO 51436622
05-28      −     15,441.80    16241744   PAGO SENAE 51469932
05-28      −     12,580.39    16241656   PAGO SENAE 51470492
            ────────────────
  Total seleccionado:  − 28,023.22  (8 items)
```

Default: **todos seleccionados** (la dueña dice "asegurate de acoplar todos"). Sus checkboxes pueden destildar.

**Botón "Conciliar y armar movimiento".** → abre el modal de resumen:

```
Vas a crear UN movimiento agrupado en el programa:

  Documento:     ND  (nota débito, porque neto < 0)
  Importe:       $ 28,023.22
  Fecha:         28/05/2026   [editable]
  Concepto:      Comisiones e impuestos 27/05-28/05   [editable, 50 chars]
  Prov/Cliente:  [opcional, 5 chars]

Y vas a conciliar los siguientes 8 movs del extracto contra ese movimiento:
  05-28  − 0.05      IVA COBRADO 51469932
  05-28  − 0.05      IVA COBRADO 51470492
  ...

[ Cancelar ]  [ Confirmar ]
```

Confirmar → llama `crear_transaccion_agrupada_desde_reals(no_banco, reals, fecha, concepto, prov, usuario)` que ya existe en matcher_banco.py:1432. Es atómico.

Vuelve al tab Impuestos y comisiones (que probablemente quede vacío). El balance arriba refleja la nueva conciliación.

**Cómo se decide qué cae acá.** Misma regla del matcher actual: `_es_comision_real(m)` → `categorizar(concepto, tipo).grupo == 'COMISION'`. Eso ya cubre IVA / COMISION BANCARIA / PAGO SENAE / etc. (ver `modules/conciliacion/categorizar.py`). **No cambiamos esa regla** en este sprint — la dueña confirmó que el auto-acople actual está bien.

## Tab 3 — Transferencias por número de documento

**Propósito.** Los matches que la PASS 0 del matcher generó por **coincidencia exacta de documento** (`extracto.documento == bancsis.numreferencia / no_cheque / doc_banco`). Son los más seguros y los que más volumen mueven cuando la dueña cargó bien el N° de comprobante en /cheques.

**Layout.** Tabla 1:1 (cada fila es un par):

```
Banco                                              Programa
Día    +/−    Monto       Doc              ↔       Día    Cliente               Doc match
05-28   +    1,000.00     21828513         ↔       05-28  ANA GUACHAMIN C       21828513
05-28   +    1,588.54     14146083         ↔       05-28  DAVID PEDRO SANCHEZ   14146083
...
☑ Todos                                           Total seleccionado: $ X,XXX.XX (N pares)
```

Default: **todos seleccionados**.

**Botón "Conciliar seleccionados".** → modal de resumen 1:1 (igual al Manual pero pre-pareado) → Confirmar → `for par: confirmar_match(...)`.

**Cómo se decide qué cae acá.** Re-correr PASS 0 del matcher sobre las listas `data.matches` filtradas por método `match_pass='P0'` (el matcher ya guarda el pass en el resultado). Si hoy no se guarda → añadir `pass` al `MatchPropuesto` y propagarlo.

## Sesión persistente — "la página puede quedar abierta hasta que aprete Terminar y guardar"

**Pregunta original de la dueña.** Hoy, si subo el extracto a las 10am, hago la mitad de los matches, cierro la pestaña y vuelvo a las 4pm — pierdo todo el progreso. ¿Puede quedar la pantalla abierta? Sí, si persistimos el estado server-side.

**Modelo.**

Tabla nueva `scintela.banco_conciliacion_sesion`:

```sql
CREATE TABLE scintela.banco_conciliacion_sesion (
  id              SERIAL PRIMARY KEY,
  no_banco        INT NOT NULL,
  usuario         VARCHAR(50) NOT NULL,
  abierta_en      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  cerrada_en      TIMESTAMP,                -- NULL = abierta
  cerrada_por     VARCHAR(50),
  extracto_hash   VARCHAR(64),              -- sha256 del xlsx para detectar re-uploads
  extracto_payload JSONB NOT NULL,          -- los movs reales parseados (lista MovBanco)
  matches_hechos  INT NOT NULL DEFAULT 0,   -- contador para la stat al cierre
  pdf_path        TEXT                      -- ruta del PDF generado al cerrar
);

CREATE INDEX banco_conciliacion_sesion_abiertas
  ON scintela.banco_conciliacion_sesion (no_banco, usuario)
 WHERE cerrada_en IS NULL;
```

**Flujo de la sesión.**

1. **Pantalla inicial** (`/conciliacion/`): la dueña sube el extracto y aprieta Procesar.
2. **Backend al recibir el upload**:
   - Calcula sha256 del bytes del archivo.
   - Busca si hay sesión abierta para `(no_banco, usuario)`:
     - Sí + mismo hash → "retomar sesión abierta del DD/MM HH:MM, querés seguir o empezar nueva?" → si seguir, redirect a `/conciliacion/banco?sesion=<id>`.
     - Sí + hash distinto → "tenés una sesión abierta con otro extracto, ¿cerrarla sin imprimir o continuarla?" → modal de decisión.
     - No → `INSERT` nueva sesión con el payload, redirect a `/conciliacion/banco?sesion=<id>`.
3. **Mientras está abierta**: cada match que la dueña confirma (Manual / Impuestos / Transferencias) incrementa `matches_hechos`. El extracto_payload NO se modifica (la fuente de verdad de qué se concilió contra qué está en `banco_conciliacion_match`).
4. **La pantalla post-procesar** lee el extracto_payload de la sesión + corre el matcher sobre los movs todavía no conciliados (excluye los `id_transaccion` que ya tienen match activo en `banco_conciliacion_match` para esta sesión).
5. **Botón "Terminar y guardar"** abajo de la pantalla (footer sticky):
   - Confirma "¿Cerrar la conciliación? Se genera el PDF de pendientes y la sesión queda archivada".
   - Backend: `UPDATE … SET cerrada_en=NOW(), cerrada_por=<u>` + genera PDF + redirect a `/conciliacion/cerrada/<id>` con el link de descarga.
6. **Una sesión cerrada** se ve en `/conciliacion/historial` (lista de sesiones con fecha apertura, cierre, matches, link al PDF). Read-only.

**Concurrencia.** Un solo usuario por banco a la vez. Si dos personas suben extracto al mismo tiempo, la segunda ve "ya hay una sesión abierta de Tamara, esperala o pedile que termine".

## Botón "Terminar y guardar" y el PDF de pendientes

**Trigger.** Al pie de la pantalla, sticky:

```
[ ✓ Terminar y guardar conciliación ]
```

Confirma con modal. Ejecuta:
1. `UPDATE banco_conciliacion_sesion SET cerrada_en, cerrada_por, pdf_path`.
2. Genera PDF (ver formato abajo).
3. Snapshot de saldo (`saldo_snapshot.snapshot(banco, 'sesion_cerrada', evento_ref=sesion_id)`).
4. Redirect a `/conciliacion/cerrada/<sesion_id>` con flash "Conciliación cerrada. PDF guardado.".

**Formato del PDF — réplica de la hoja `FEB` del xlsx de muestra.** UNA sola tabla, sin segunda hoja, sin sumarios extra:

```
DEPÓSITOS PENDIENTES                            Pichincha · sesión 2026-05-28 16:42
────────────────────────────────────────────────────────────────────────────────
FECHA       DETALLE                                            CODIGO       VALOR    DETALLE
2026-05-28  TRANSFERENCIA DIRECTA DE LOPEZ CALDERON FELIPE     56379469     150.00
2026-05-28  DEPOSITO                                           41508270     590.27
...
────────────────────────────────────────────────────────────────────────────────
                                                       Total:  $ XX,XXX.XX
```

Columnas tal cual el xlsx. Lista de **lo que quedó pendiente** después de cerrar la sesión:
- Movs del banco (real_only) que no se conciliaron en esta sesión.
- Excluye comisiones/impuestos (ya van a la cuenta del banco automáticamente, no son "depósitos pendientes" en el sentido del reporte).
- Tipo C primero, después D si la dueña confirma que también lo quiere (open question abajo).

**Implementación PDF.** Skill `pdf` ya disponible. Plantilla simple con `reportlab` o `weasyprint` desde HTML. Storage local en `data/conciliacion_pdfs/<sesion_id>.pdf` (mismo patrón que `data/dbase_snapshots/`). El campo `pdf_path` guarda la ruta relativa.

## Lo que SE BORRA del template post-procesar

| Bloque actual de `banco_resultado.html` | Decisión |
|---|---|
| Card "Sugerencias del matcher (PASS 1-4)" | Mover a toggle dentro del tab Manual ("incluir sugerencias", off por default). |
| Card "Bancsis only — bulk aceptar como conciliados" | Eliminar bulk. Se concilia desde el tab Manual. |
| Card "Real only — bulk aceptar / crear bancsis individuales" | Eliminar bulk. Se concilia desde el tab Manual / Impuestos. |
| Card "Marcar depósitos del día" | Eliminar de esta pantalla — vive ya en `/conciliacion/depositos`, link en el footer. |
| Cards "Diag", "Self-test", "KPI debug" | Mover a `/conciliacion/hub/diag` (ya existen) — sin link visible desde la pantalla principal. |
| Resumen "Movimientos del banco no identificados en PC" + "Movimientos del programa sin contrapartida" (tabla larga separada) | Reemplazada por los tabs. |
| "Conciliados del día" (tabla informativa al pie) | Mover al log `/conciliacion/cambios` (ya existe). Footer-link nada más. |

## Endpoints

### Nuevos

- `GET /conciliacion/banco` (renombrar de `hub`) → renderiza la pantalla nueva con los 3 tabs. El query param `?tab=manual|impuestos|transferencias` define cuál arranca; default `manual`.
- `POST /conciliacion/banco/manual/preview` → JSON con el preview 1:1 para el modal.
- `POST /conciliacion/banco/manual/confirmar` → ejecuta los matches seleccionados.
- `POST /conciliacion/banco/impuestos/confirmar` → wrapper sobre `crear_transaccion_agrupada_desde_reals` + matches.
- `POST /conciliacion/banco/transferencias/confirmar` → wrapper sobre `confirmar_match` × N para los pares de PASS 0.

### Existentes que se reusan

- `confirmar_match()` en matcher_banco.py
- `crear_transaccion_agrupada_desde_reals()` en matcher_banco.py
- `romper_match()` (ya con UI: el botón desmatch en /conciliacion/cambios — commit 35788d7).

### Existentes que se marcan deprecated (no se borran en este sprint)

`hub_aceptar_bancsis_only`, `hub_aceptar_real_only`, `hub_crear_bancsis`, `hub_crear_bancsis_agrupado` (queda como interno del wrapper de impuestos), `marcar_depositos_dia_conciliados`, `hub_match_click`. Mantener para no romper bookmarks/tests, pero quitar links visibles.

## Backend — cambios

### `matcher_banco.MatchPropuesto`

Agregar campo `pass_origen: str` para que el handler sepa si vino de PASS 0 (transferencias por doc) o de PASS 1-4 (sugerencias fuzzy). Hoy se calcula pero no se persiste en la estructura — se pierde en el render.

### `views.hub()`

Renombrar a `views.banco_post_procesar()`, simplificar drásticamente:

```python
@conciliacion_bp.route("/banco", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def banco_post_procesar():
    # 1) Reusa la carga del extracto desde session (igual que hoy).
    # 2) Corre matchear_extracto_banco una vez.
    # 3) Separa el resultado:
    #    - manual_banco = real_only que NO son COMISION
    #    - manual_programa = bancsis_only
    #    - impuestos = real_only que SÍ son COMISION
    #    - transferencias = matches con pass_origen='P0'
    #    - sugerencias = matches con pass_origen in ('P1','P2','P3','P4')
    # 4) Calcula el balance Pichincha (helper saldo_pc_actual ya existe).
    # 5) Renderiza el nuevo template con todos los buckets.
    tab = (request.args.get("tab") or "manual").lower()
    return render_template(
        "conciliacion/banco.html",
        tab_activo=tab,
        balance=saldo_pc_actual,
        manual_banco=...,
        manual_programa=...,
        impuestos=...,
        transferencias=...,
        sugerencias=...,
    )
```

### Templates

- Nuevo partial `modules/conciliacion/templates/conciliacion/_balance_pichincha.html` con el bloque hero + tabla referencial. Recibe `balance` como contexto.
- Nuevo template `modules/conciliacion/templates/conciliacion/banco.html`:
  - Incluye `_balance_pichincha.html`.
  - Tabs como `<nav>` con `<a href="?tab=X">` (server-side, no JS — más simple y compartible).
  - Renderiza el tab activo (manual/impuestos/transferencias).
  - JS mínimo para los checkboxes + totalizador en cada tab.
- Nuevo partial `_modal_resumen.html` para el modal de confirmación (es el mismo patrón en los 3 tabs).
- `banco_resultado.html` queda como archivo huérfano hasta el sprint siguiente — borrarlo.

### Frontend

JS plano (sin frameworks, alineado con el resto de Programa Core):

```js
// Por tab, dos paneles con checkbox.cb. Actualizar contadores onclick.
document.querySelectorAll('.cb').forEach(cb => cb.addEventListener('change', () => {
  recalc(cb.closest('section').dataset.lado);
}));

function recalc(lado) {
  // lado in ('banco','programa')
  // ... suma de inputs marcados, escribe en .total-lado
  // habilita boton conciliar si hay >=1 de cada lado
}
```

El modal de resumen se renderiza server-side (fetch al endpoint preview, devuelve HTML del modal, inyecta en `<dialog>`). Más simple que armar JSON + render en cliente y reusa el mismo Jinja partial.

## Edge cases que el plan tiene que manejar

- **Monto seleccionado banco != programa** en el tab Manual → modal bloquea Confirmar. Diferencia se ve en rojo. Caso típico: depósito de 3 cheques juntos del lado banco vs 3 movs PC del lado programa — suman lo mismo, se concilian.
- **Filas que cambian entre la carga del extracto y la confirmación** (alguien concilió en otra pestaña). El endpoint confirmar debe validar `id_transaccion` sigue libre antes de matchear — si ya fue conciliado en otra parte, skipear con flash + warning.
- **Impuestos tab vacío** (no detectó comisiones) → mostrar empty state con explicación.
- **Transferencias tab vacío** (PASS 0 no encontró matches) → mismo empty state.
- **El balance arriba se cachea por request, no se recalcula con JS**. Después de cada `confirmar` viene un redirect → recarga server-side → balance live.
- **Sync con dBase en paralelo**: si el sync de fondo marca un mov `stat='*'` mientras la dueña está en el tab Manual, ese mov debería desaparecer al refresh. Confirmar que la lista de pendientes se calcula con el filtro `stat IN (...) AND saldo <> 0` (ya documentado en memoria `project_cartera_signo` — aplica el mismo principio).

## Riesgos

1. **Rompemos un atajo que Federico está usando.** Antes de borrar `banco_resultado.html` chequear `git log -p modules/conciliacion/templates/conciliacion/banco_resultado.html` y ver si Federico tocó algo reciente. Coordinar.
2. **El matcher devuelve más sugerencias falsas que reales** y el toggle "incluir sugerencias" termina siendo necesario. Si la dueña reporta esto, suben las sugerencias a su propio mini-tab "Sugerencias" en lugar de toggle.
3. **JS de totalizador rompe en Safari** (Federico usa Chrome). Probar en ambos.

## Plan de ejecución (sprints sugeridos)

**Sprint 1 — Reforma estructural (este sprint).**
- (a) Sacar `_balance_pichincha.html` como partial reusable (modos `full` y `compact`). Reemplazar en `banco_upload.html`. Smoke.
- (b) Migración `0058_banco_conciliacion_sesion.sql` (tabla + índice).
- (c) Nuevo template `banco.html` con balance compact sticky, tabs, footer con "Terminar y guardar".
- (d) Refactor `views.hub` → `banco_post_procesar` que: crea/retoma sesión, clasifica buckets, renderiza.
- (e) Tab Manual con paneles, checkboxes, total acumulado, modal preview, endpoint confirmar.
- (f) Tab Impuestos: tabla + total + modal + endpoint confirmar (wrapping de `crear_transaccion_agrupada_desde_reals`).
- (g) Tab Transferencias: tabla 1:1 + checkboxes + modal + endpoint confirmar.
- (h) Endpoint `terminar_y_guardar` + generador PDF formato hoja FEB + `/conciliacion/cerrada/<id>`.
- (i) Lista `/conciliacion/historial` con todas las sesiones cerradas + link al PDF.
- (j) Borrar links visibles a Diag/Self-test/Bulk-aceptar. Mover esos endpoints a deprecated.
- (k) Tests: cada endpoint nuevo con FakeDB + Flask test_client. Smoke 80/80 URLs.
- (l) Deploy + dueña confirma.

**Sprint 2 — Limpieza.**
- Borrar `banco_resultado.html`.
- Borrar endpoints deprecated tras 2 semanas sin uso (chequear logs).
- Mover Diag a `/admin/conciliacion-diag` con permiso admin.

## Open questions (para resolver antes de empezar Sprint 1)

1. ¿Federico necesita que algún CTA viejo siga existiendo? (Antes de borrar bulk-aceptar.)
2. ¿El tab Manual incluye también las filas con `stat='*'` que vinieron del dBase? Hoy `bancsis_only` ya las excluye — confirmar que sigue así.
3. ¿El modal de resumen permite editar la fecha/concepto/prov para el tab Impuestos? (Sí, lo dice el plan, pero confirmar antes de implementar.)
4. El PDF de pendientes — ¿solo movs del banco tipo C (entradas, "depósitos pendientes") o también tipo D (salidas pendientes)? El xlsx de muestra es solo entradas pero ofrecer ambos por si los necesita.
5. ¿La sesión persistente expira sola? Propongo: una sesión abierta sin actividad > 30 días se autocierra con flag "cerrada por timeout" y sin PDF. Avisar al usuario si entra después.
6. ¿"Terminar y guardar" puede ejecutarse incluso si quedan pendientes? Sí — la dueña sabe lo que hace, el PDF lista lo que queda. No es un bloqueo, es un cierre.

## Anexo — qué clases CSS y qué patrones reusamos

- Hero + tabla referencial → ya rediseñado en `banco_upload.html` (commit 35788d7). Copy-paste a partial.
- Cards de tabs: `rounded-xl border border-slate-200 shadow-sm`. Tab activo: `border-b-2 border-emerald-500`.
- Modal: `<dialog>` HTML nativo. No bootstrap modal, no headlessui. Reusamos lo de `/cobranza` (busca `<dialog id="modal-`).
- Tabular nums: `tabular-nums` en todos los montos.
- Checkboxes: `accent-emerald-600`.
- Botón primario verde: `bg-emerald-600 hover:bg-emerald-700 text-white text-sm rounded-md px-3 py-1.5`.

## Definición de hecho

Para considerar el Sprint 1 cerrado:

- La pantalla `/conciliacion/banco` se ve como el mock de arriba.
- Los 3 tabs funcionan, cada uno con su modal de resumen.
- El balance arriba se actualiza visiblemente tras cada conciliación (porque el redirect lo recalcula live).
- `banco_resultado.html` NO se borra todavía (Sprint 2), pero NINGÚN link de la app lo apunta.
- Tests pasan, smoke 80/80, demo a la dueña.
