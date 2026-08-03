# Depósito de cheques — auditoría del 31/07/2026

Escrito porque la dueña preguntó *"¿estás seguro que depósito de cheques
funciona bien?"*. **No se cambió ni una línea de código ni un solo dato.** Esto
es el informe y el dry run.

---

## 0. Lo primero: el depósito de HOY está bien

Antes de buscar defectos había que descartar el síntoma reportado (*"me dijeron
que sí habían depositado y no funcionó"*):

| verificación | resultado |
|---|---|
| Depósitos hechos desde PC hoy | **uno**: `dep.23 ch.` |
| Suma de los 23 cheques vs. el movimiento del banco | **72.580,99 = 72.580,99**, al centavo |
| ¿Se pisa con los 49 depósitos individuales del dBase de hoy? | **No** — clientes distintos, cero duplicación |
| POSTs a depositar-lote hoy (bitácora) | 1, a las 21:25. **Ninguno de Alex ni de Andrés** |
| Actividad de Alex / Andrés hoy | 93 y 54 escrituras — estuvieron trabajando, pero en `/cheques/nuevo`, conciliación, caja, emitir cheque |

**Conclusión:** hoy no hubo ningún intento fallido de depósito. El "no funcionó"
es de otro día o se quedó antes de mandarlo.

Falsa pista que casi me lleva puesto: en el detalle de un depósito, cada cheque
muestra **"⚠ rebotó"**. **No es un estado, es un botón** (marcar que el banco lo
rechazó — `bancos/movimientos.html:534`). Los 23 lo tienen porque los 23 tienen
el botón.

---

## 1. El defecto real: los anticipos negativos entran al depósito

### Qué es un cheque negativo

Cuando un cliente adelanta plata o paga de más, eso no es un papel: es una
**anotación**. El sistema la guarda como un cheque con importe negativo (espejo,
NB=98 / NB=97) para que la cuenta corriente del cliente cierre. Los 87 que hay
hoy dicen **ANTICIPO** en el concepto.

### Qué hace el código

`modules/cheques/queries.py`, `depositar_lote`. Dos listas distintas para la
misma operación:

```python
# 1) marca como depositados TODOS los seleccionados
UPDATE scintela.cheque
   SET stat = CASE WHEN stat IN ('1','2','3') THEN 'V' ELSE 'B' END, …
 WHERE id_cheque IN (…todos…)

# 2) el movimiento del banco, SOLO los positivos
positivos = [r for r in rows if float(r.get("importe") or 0) > 0]
total_pos = round(sum(…positivos…), 2)
if positivos:
    …insert_movimiento_bancario(importe=total_pos)…
```

Y el total que se informa es el de **todas** las filas:

```python
total = sum(float(r.get("importe") or 0) for r in rows)   # incluye negativos
…
return {"n_depositados": len(rows), "total": total, …}
```

que la vista muestra tal cual: *"N cheque(s) depositado(s) … por $ {total}"*.

**Resultado:** el anticipo sale de cartera para siempre, al banco no va nada que
lo compense, y el número que se muestra no es el que entra al banco. No salta
ningún error.

Caso extremo: si el lote fuera **sólo** negativos, `positivos` queda vacío → **no
se crea ningún movimiento bancario** y los cheques igual quedan en `'B'`.

### Dry run — números reales del 31/07/2026, 21:40

Leídos de `/cheques/depositar-lote` sin tocar nada:

| | |
|---|---|
| Cheques listados con casilla | **1.348** |
| Positivos | 1.261 · **2.560.860,77** |
| Anticipos negativos | **87** · **−105.657,97** |
| Lo que diría el mensaje al confirmar | 1.348 cheques por **2.455.202,80** |
| Lo que realmente entraría al banco | **2.560.860,77** |
| Diferencia | **105.657,97** |

Ejemplos: `#99939 DIIGAR ALEXANDER POTOSI · ANTICIPO · −2.000,00` ·
`#100195 MARIA MARCALLA · ANTICIPO · −250,00` ·
`#99209 PALLO CHIPANTAS · ANTICIPO · −900,00`.

### Ya pasó — tres veces

`/cheques/diag/depositados-sin-movimiento` marca 640 cheques. **637 son del
`dbf-import`** (llegaron ya depositados desde el dBase; es arrastre conocido, no
este bug). Los **3 restantes son de Alex y son negativos**:

| cheque | importe | fecha |
|---|---|---|
| #100599 | −150,00 | 15/07/2026 |
| #100689 | −1.000,00 | 16/07/2026 |
| #100948 | −730,00 | 21/07/2026 |

Verificado en pantalla: #100599 figura *"Depositado (Pichincha)"*, sin
contrapartida en el banco.

---

## 2. Cómo lo hacía el dBase — la regla ya existe

La dueña: *"FIJATE COMO HACIA EL DBASE"*. Estaba escrito en el repo:

**a. Nunca deposita un negativo.** `ALTAS.PRG L170-186`: el movimiento `DOC='DE'`
se appendea **sólo con importe > 0**. PC lo replica literal en el alta
(`queries.py:3000`):

```python
if no_banco in (90, 91) and (stat or "").upper() == "Z" and float(importe or 0) > 0:
    stat = "B"
```

**b. Un anticipo no se deposita: se CANCELA.** `ALTAS.PRG NB=95 (CANCELA
ANTIC.)`: cuando el cliente paga, el dBase busca el espejo negativo NB=98 del
mismo cliente y marca a **los dos** con `'X'`. Si no lo encuentra, avisa *"NO SE
ENCUENTRA EL ANTICIPO"* y lo deja a mano. Ése es el final de un anticipo — el
depósito nunca fue parte de su vida.

**c. Y PC ya tiene el guard… en el otro camino.** El 22/07, por el cheque 100410
de Alex, se agregó en `crear()` (`queries.py:3013-3033`) un guard anti-orphan que
nombra **`importe<=0`** explícitamente y tira `ValueError`.

> **Es el mismo patrón que apareció esta mañana con las importaciones:** un
> arreglo correcto aplicado a un camino y no al otro. Ahí fue `_nearest_import`
> con año en anticipos y sin año en compras (`service.py:302` vs `:320`). Acá es
> el guard de `importe > 0` en el alta y no en el lote.
>
> **Regla que queda:** cuando se arregla un camino, buscar los hermanos. Un
> `grep` de la condición nueva cuesta un minuto.

---

## 3. Qué cambiaría el arreglo (y qué NO)

Pregunta textual de la dueña: *"no cambiaría nada entonces, ¿no?"*.

**NO cambia:**

- ningún número del balance, la utilidad, el patrimonio ni el stock;
- ningún saldo de banco;
- ninguna fila existente de `cheque`, `transacciones_bancarias` ni
  `chequextransaccion`;
- ningún depósito ya hecho, incluido el `dep.23 ch.` de hoy.

**SÍ cambia:**

- la pantalla de depositar en lote pasa a listar **1.261** en vez de 1.348 (los
  87 anticipos dejan de tener casilla);
- el total que muestra pasa de 2.455.202,80 a **2.560.860,77** — que es el que
  realmente entra al banco.

**NO arregla** los 3 cheques de julio (−1.880 en total). El arreglo es
preventivo, no curativo. Repararlos es una decisión aparte: hay que devolverlos a
cartera para que el saldo a favor del cliente vuelva a existir, y eso toca datos.

---

## 4. Lo demás que apareció (no urgente, sin verificar en vivo)

Ordenado por gravedad. Los dos primeros los leí en el código; el resto viene de
la auditoría y **no están medidos contra producción**.

1. **`deshacer_deposito` + rebote, en cierto orden, borra el depósito entero.**
   `compensar_deposito_devuelto` borra el link del cheque rebotado y deja el `DE`
   intacto (por diseño). Después, "Volver a cartera" sobre otro cheque del lote
   cuenta `n = COUNT(*) chequextransaccion` → si quedaba 1, **borra el `DE`
   completo**. Lote A=100 + B=200: A rebota (ND −100, banco +200), se deshace B →
   se borra el `DE` de 300 → **banco en −100 cuando debería quedar en 0**. En el
   orden inverso cierra bien.
2. **El banco emisor se pisa.** `depositar_lote` hace `no_banco = <destino>`,
   `banco = <destino>`, y `deshacer` no lo restaura. La boleta imprimible resuelve
   la columna "banco emisor" con `LEFT JOIN banco ON no_banco = c.no_banco`
   (`queries.py:2330`) → **todas las filas dicen PICHINCHA**. Es el papel que va
   al banco.
3. **`numreferencia` se lee y se descarta.** El SELECT trae `doc_banco` con un
   comentario que dice que es para propagarlo, y el INSERT pasa
   `numreferencia=None` (`queries.py:2199`). La regla #1 del matcher de
   conciliación nunca puede disparar para un depósito en lote.
4. **Sin idempotencia bajo concurrencia real.** La validación de estado se lee
   fuera de la transacción y sin `FOR UPDATE`. El doble submit **secuencial** sí
   está cubierto (el cheque ya está en `'B'`).
5. **`deshacer_deposito` no valida período contable** (`asegurar_fecha_abierta`)
   y **no tiene un solo test**, siendo la función que edita y borra movimientos
   bancarios ya asentados.

## 5. Lo que está BIEN (verificado, no supuesto)

- **Atomicidad**: el UPDATE de cheques, el movimiento bancario y los links van en
  la **misma** `db.tx()`. Si falla el banco, se revierte todo.
- **Validación de estados**: fail-closed y en bloque — un cheque no depositable
  **aborta el lote entero** con el detalle de cuáles.
- **Suma del consolidado** (positivos): exacta, sin IVA, comisión ni redondeo.
- **Guard de conciliación en `deshacer`**: fail-closed, dentro de la tx; si la
  tabla no existe, explota y hace rollback.
- **Rebote de un cheque de un lote**: no rompe el consolidado, compensa con ND
  por el importe del cheque, e es idempotente por dos vías.
