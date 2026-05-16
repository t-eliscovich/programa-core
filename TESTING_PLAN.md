# Plan de testeos — Programa Core (movimientos dobles + historial)

Probar cada flujo nuevo armado en la sesión 2026-05-12. Total estimado: **40-60 min** si todo va bien. Si algo falla, parar y reportar antes de seguir.

> **Filosofía**: cada test arranca anotando saldos, hace UN movimiento, verifica las dos puntas (origen + destino) **y** la fila en `/historial`. Los tests están pensados para que si reversás todo al final, los saldos vuelven a los iniciales.

---

## 0. Pre-requisitos (5 min)

### 0a. Correr la migración (sólo una vez)

- [ ] **T00.1** — Abrir una terminal en la carpeta `Programa Core` (en Mac: Finder → click derecho en la carpeta → "Servicios" → "Abrir Terminal en la carpeta", o desde Spotlight `cmd+space` → tipear `terminal` → `cd /Users/tamaraeliscovich/Documents/Claude/Projects/Programa\ Core`).
- [ ] **T00.2** — Si tenés un venv (probablemente sí), activalo: `source .venv/bin/activate`. Si no andan los comandos, salteá este paso.
- [ ] **T00.3** — Correr: `python scripts/migrate.py`. Debería imprimir algo tipo:
  ```
  ✓ 0023_mov_doble.sql aplicada
  ```
  o `nothing to do` si ya estaba aplicada. Si tira error, copiame el mensaje completo.

### 0b. Levantar la app y verificar

- [ ] **T00.4** — Levantar Flask como hacés siempre. Loguearte.
- [ ] **T00.5** — Verificar sidebar: bajo "Tablero" debe aparecer **Operaciones** con un icono de flechas. Click → llega a `/operaciones`.
- [ ] **T00.6** — En `/operaciones` ver las 5 secciones con cards: Movimientos entre cuentas / Cheques / Compras / Movimientos del dueño / Historial.
- [ ] **T00.7** — Ir a `/historial` (sidebar → Más → "Historial movimientos"). Debe mostrarse la página con KPIs en cero y un mensaje tipo "Sin movimientos en este filtro". Si en cambio ves un mensaje rojo diciendo **"Falta correr la migración 0023"**, volvé al paso T00.3.
- [ ] **T00.8** — **Anotar saldos iniciales** antes de tocar nada:
  - Pichincha: $ __________
  - Internacional: $ __________
  - Caja: $ __________
  - Capital actual: $ __________

---

## 1. Sanity check — caja sin concepto especial (3 min)

Verifica que las cosas básicas no se rompieron antes de probar lo nuevo.

- [ ] **T01.1** — Ir a `/caja/nuevo`. Tipo Entrada, importe **10**, concepto **"test sanity"**. Guardar.
- [ ] **T01.2** — `/caja` muestra la fila. Saldo caja subió 10.
- [ ] **T01.3** — `/historial` **no** muestra nada nuevo (los movimientos simples de caja **sin** side effect no se registran al crearse — sólo los dobles).
- [ ] **T01.4** — Reversar el movimiento (botón ↺). Saldo caja vuelve al original. **El reverso SÍ aparece en `/historial`** como tipo "Reverso de caja (sin side effect)" — convención: todo reverso queda en el historial aunque el original no fuera doble.

---

## 2. Movimientos dobles concept-driven (8 min)

El bug que dio origen a todo esto. Caja con concepto `PICH …` ahora **tiene que** mover el saldo del banco.

- [ ] **T02.1** — `/caja/nuevo`. Salida, importe **20**, concepto **`PICH test deposito`**. Guardar.
  - Verifica: caja bajó 20.
  - **Pichincha subió 20** (ir a `/bancos`).
  - `/historial` muestra una fila tipo "Caja → Banco", estado **Activo**.
- [ ] **T02.2** — `/caja/nuevo`. Salida, importe **15**, concepto **`RR TMT gastos`**. Guardar.
  - Verifica: caja bajó 15.
  - Aparece un retiro nuevo en `/capital?filtro=retiros`.
  - `/historial` muestra "Caja → Retiro socio".
- [ ] **T02.3** — `/caja/nuevo`. Entrada, importe **20**, concepto **`PICH retiro de banco`**. Guardar.
  - Verifica: caja subió 20.
  - **Pichincha bajó 20**.
  - `/historial` muestra "Banco → Caja (entrada)".

---

## 3. Wizards — transferencias y movimientos del dueño (10 min)

- [ ] **T03.1** — `/operaciones` → **Transferir entre bancos**. Pichincha → Internacional, importe **50**. Guardar.
  - Pichincha bajó 50, Internacional subió 50.
  - `/historial` muestra "Transferencia banco ↔ banco".

- [ ] **T03.2** — `/operaciones` → **Transferir entre cuentas USD** (saltear si no hay cuentas USD con saldo). Si hay: cuenta A → cuenta B, US$ **10**.
  - Saldo cuenta A bajó 10, B subió 10.
  - `/historial` muestra "Transferencia USD ↔ USD".

- [ ] **T03.3** — `/operaciones` → **Aporte de capital**. Importe **100**, cuenta **Caja**, socio **TMT**, concepto "test aporte".
  - Capital subió 100 (ver `/capital`).
  - Caja subió 100.
  - `/historial` muestra "Aporte capital → Caja".

- [ ] **T03.4** — `/operaciones` → **Retiro de socio**. Socio TMT, importe **100**, cuenta **Caja**, concepto "test retiro".
  - Aparece fila de retiro.
  - Caja bajó 100.
  - `/historial` muestra "Retiro socio ← Caja".

- [ ] **T03.5** — Confirmar que **caja vuelve al saldo previo a T03.3** (aporté 100 y retiré 100).

---

## 4. Compras con pago parcial (5 min)

- [ ] **T04.1** — `/compras/nueva`. Proveedor cualquiera (ej. ACR), importe **200**, tipo **Hilado**, comprobante "test pago parcial".
- [ ] **T04.2** — Abrir la sección "¿Pago parcial al momento?". Poner monto **80**, cuenta **Pichincha**.
- [ ] **T04.3** — Guardar. Flash debe decir "registrada con pago parcial de $ 80 (saldo $ 120)".
- [ ] **T04.4** — Verificar:
  - **Pichincha bajó 80** (no 200).
  - `/posdat` o `/proveedores` muestra una posdat abierta por **120** con ese proveedor.
  - `/historial` muestra "Compra con pago parcial".

---

## 5. Endoso de cheque (5 min)

> Necesitás un cheque en cartera (stat `Z`). Si no hay, cargá uno con `/cheques/nuevo` (cliente cualquiera, importe **150**) y luego volvé acá.

- [ ] **T05.1** — Abrir el detalle del cheque en cartera.
- [ ] **T05.2** — Botón "Cambiar estado" → **Endosar a proveedor…**.
- [ ] **T05.3** — Wizard: proveedor (ej. ACR), tipo **Hilado**, concepto "test endoso". Guardar.
- [ ] **T05.4** — Verificar:
  - Cheque ahora dice **Endosado → ACR** (badge violeta).
  - `/compras` muestra una compra nueva con `cuenta_pagada='E'`.
  - Cheque ya no aparece en pestaña "En cartera", aparece en pestaña **"Endosados"**.
  - `/historial` muestra "Endoso cheque → Proveedor".

---

## 6. Cheque emitido (chequera) — concept-driven (8 min)

> Necesitás saldo en algún banco. Confirmá en `/bancos` antes de seguir.

- [ ] **T06.1** — `/operaciones` → **Emitir cheque (chequera)**.
- [ ] **T06.2** — Antes de elegir el tipo, **tipear el concepto primero**: `RR TMT test chequera`.
- [ ] **T06.3** — Después de ~½ segundo debe aparecer el banner **"🤖 Detecté: Retiro dueño — apply"**. Click "aplicar".
- [ ] **T06.4** — Radio "Retiro dueño" debe estar marcado y el socio "TMT" auto-rellenado.
- [ ] **T06.5** — Banco origen Pichincha, importe **50**, n° cheque cualquiera. Guardar.
- [ ] **T06.6** — Verificar:
  - **Pichincha bajó 50**.
  - Aparece un retiro nuevo en `/capital?filtro=retiros`.
  - `/historial` muestra "Cheque emitido → Retiro socio".

- [ ] **T06.7** — Repetir con concepto **`PR ACR test prov`** → tipo debería detectarse como "Proveedor" (sin posdat asociada).
- [ ] **T06.8** — Repetir con concepto **`a caja test`** → tipo "A caja" (no se detecta auto, marcalo manual).

---

## 7. Reversos (8 min)

Esta es la parte crítica. Si algo se rompió en los pasos 2-6, acá deberían volver a foja cero.

- [ ] **T07.1** — `/caja` → encontrar el movimiento del T02.1 (`PICH test deposito`). Click "↺ Reversar", motivo "test".
  - Caja vuelve a su saldo previo a T02.1.
  - **Pichincha vuelve a su saldo previo a T02.1**.
  - `/historial` muestra **dos filas relacionadas**: la original con badge **Reversado → #N**, y un **Reverso** nuevo con badge ← link al original.

- [ ] **T07.2** — Reversar también T02.2 (retiro) y T02.3 (entrada caja). Verificar que caja y retiros vuelven al estado previo.

- [ ] **T07.3** — Reversar T06.1 (cheque emitido): ir a `/bancos/<no_banco>` → buscar la fila CH del T06.5 → botón "↺ reversar" (rojo). Motivo "test reverso". Confirmar.
  - Aparece una fila **ND** positiva por el mismo importe en el banco.
  - Pichincha vuelve al saldo previo a T06.5.
  - Aparece un retiro **negativo** (compensación) en `/capital?filtro=retiros`.
  - `/historial` muestra el reverso enlazado.

- [ ] **T07.4** — **Doble reverso debe fallar**. Intentar reversar de nuevo el mismo `id_caja` o la misma `tx CH`. Debería dar error claro tipo "ya fue reversado".

---

## 8. Historial — filtros y export (3 min)

- [ ] **T08.1** — `/historial` debe mostrar todas las operaciones de la sesión, **incluso las reversadas** (con badge ámbar) y los **reversos** (badge gris).
- [ ] **T08.2** — Filtro **Estado = "Reversado"**: solo aparecen las originales que fueron reversadas. Cada una con link `→ #N` al reverso.
- [ ] **T08.3** — Filtro **Estado = "Reverso"**: solo aparecen los reversos. Cada uno con link `← #N` al original.
- [ ] **T08.4** — Filtro **Tipo = "transfer_banco_banco"**: solo la fila de la T03.1.
- [ ] **T08.5** — Botón **CSV** descarga un archivo con todas las filas filtradas.

---

## 9. Vocabulario consistente (2 min)

- [ ] **T09.1** — `/compras/nueva`: dropdown "Tipo" debe decir **"K — Tejido"**, **"H — Hilado"** (singular), **"Q — Químicos"**, **"C — Otros"**, **"A — Anticipo"**.
- [ ] **T09.2** — `/capital/aportar`: dropdown "Cuenta" debe decir "Caja / Banco Pichincha / Banco Internacional".
- [ ] **T09.3** — `/informes/flujo` o donde sea que aparezcan las dos columnas de bancos: deben decir **"Pichincha"** y **"Internacional"** completos (no "Pich" ni "Inter").

---

## 10. Cierre — verificar saldos finales (3 min)

Si reversaste todos los movimientos de la prueba, deberían volver a los iniciales:

- [ ] **T10.1** — Pichincha = el de T00.6.
- [ ] **T10.2** — Internacional = el de T00.6.
- [ ] **T10.3** — Caja = el de T00.6.
- [ ] **T10.4** — Capital = el de T00.6.

> Si **no coinciden**, mirá `/historial` con filtro estado=Activo y revisar qué operaciones quedaron sin reversar.

---

## Edge cases para revisar después (opcional, ~10 min)

- [ ] **T11.1** — Período cerrado: setear `fecha_cierre` en `parametros` a hoy y tratar de hacer una operación con fecha anterior. Debe dar error claro.
- [ ] **T11.2** — Pago parcial > importe de la compra: debe rechazar con "excede el importe".
- [ ] **T11.3** — Endosar cheque ya depositado: debe rechazar "stat='B' no se puede endosar".
- [ ] **T11.4** — Reversar una transferencia entre bancos: hoy **no hay UI para esto** (Fase pendiente). Verificar que al menos podés reversar manualmente borrando las dos filas si te equivocaste — y reportar como "feature faltante".

---

## Si algo falla

Apuntá el ID del test y un mini-resumen: "T02.1: caja bajó pero Pichincha no se movió, el saldo quedó en X cuando debería ser Y". Eso es suficiente para que en la próxima sesión arranque a debuggear con contexto.
