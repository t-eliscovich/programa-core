---
name: programa-core-datos
description: Cómo contestar RÁPIDO una pregunta de datos en Programa Core (el ERP Flask de Intela, https://programa.intela.com.ec, Postgres RDS con IP lock). Usar SIEMPRE que haya que investigar un número que no cierra — utilidad, saldo de bancos, conciliación, pendientes, patrimonio, cartera, anticipos, stock — o que la pregunta sea "¿por qué pasó esto?" sobre datos de producción. Trae el orden de ataque (consola SQL read-only primero, endpoint nuevo último), las consultas ya escritas, los invariantes de la data que muerden siempre, y los errores que ya cometimos. Complementa a `programa-core` (invariantes de código) y a `posdat-flujo-utilidad` (modelo financiero).
---

# Programa Core — cómo contestar una pregunta de datos sin perder el día

## El problema que este skill resuelve

El 03/08/2026 hubo una jornada entera de debugging por un bug de la cadena de
saldos. La investigación fue correcta, pero **lenta por una sola razón**: la
base es RDS con IP lock y no se llega desde el sandbox. Cada pregunta nueva
("¿cuántas filas tienen el saldo roto?", "¿qué fila entró backdateada?",
"¿cuánto suman los pendientes borrados?") costaba:

> escribir un endpoint → push → **~3 min de CI + deploy** → recién ahí el número

Doce preguntas = doce deploys. La respuesta a cada una tardaba 3 minutos;
pensarla, 20 segundos.

**La regla nueva: nunca deployes para ver un número.**

---

## Orden de ataque (bajar sólo si el escalón anterior no alcanza)

### 1. Consola SQL — `/admin/sql`

Contesta el ~90% de las preguntas, en segundos, sin tocar el repo.

- Sólo lectura **garantizada por Postgres**: la consulta corre dentro de
  `SET TRANSACTION READ ONLY` y termina en `ROLLBACK`. INSERT/UPDATE/DELETE/DDL
  fallan con error 25006. No es un filtro de texto, no se burla.
- `statement_timeout` 15 s (configurable), tope de filas, bitácora en
  `scintela.sql_console_log`, columnas tipo credencial tapadas.
- Trae **recetas guardadas** — las consultas de abajo ya están adentro.
- Salida HTML, `?formato=json` y `?formato=csv`.

Desde una sesión de Claude, con el puente de Chrome, una sola llamada:

```js
// javascript_tool sobre una pestaña logueada en programa.intela.com.ec
const r = await fetch('/admin/sql?formato=json&limite=5000&q=' + encodeURIComponent(`
  SELECT no_banco, COUNT(*) FROM scintela.transacciones_bancarias GROUP BY 1
`), {credentials:'include'});
const j = await r.json();
JSON.stringify({n:j.n, cols:j.columnas, filas:j.filas.slice(0,50), err:j.error})
```

⚠ **Traé el resultado ya agregado.** El puente devuelve texto truncado: pedile
a Postgres el `COUNT`/`SUM`/`GROUP BY`, no 3.000 filas para sumarlas afuera.
Si necesitás las filas crudas, `LIMIT` + paginar.

### 2. Exports CSV que ya existen

Cuando la pantalla ya resuelve el join, el export es más rápido que escribir
el SQL:

```js
await fetch('/bancos/10?export=csv&desde=2026-06-01&hasta=2026-08-31',
            {credentials:'include'}).then(r=>r.text())
```

Hay `csv_response` en bancos, caja, bitácora, provisiones y comparativa de
tintorería, y `/admin/posdat-reconcile/pc-dump`.

### 3. Los health endpoints

`/admin/health/all` (lo curlea un cron diario en la EC2) junta:

| endpoint | qué caza |
|---|---|
| `/admin/health/cadena-saldos` | filas donde `\|Δsaldo\| ≠ \|importe\|` |
| `/admin/health/saldo-derivado` | si el saldo del balance se puede reconstruir sumando |
| `/admin/health/pendientes-conciliacion` | rótulos de resumen cargados como movimientos |
| `/admin/health/usuario-crea-audit` | markers `usuario_crea` fuera de whitelist |
| `/admin/health/utilidad-watchdog` | utilidad live vs. snapshot previo |

Todos devuelven `{"ok": bool, "alerts": [...], "stats": {...}}`. **Leelos
antes de investigar a mano** — muchas veces la respuesta ya está ahí.

### 4. Recién ahora: endpoint nuevo + deploy

Sólo si hace falta **escribir** (un fix, una migración, un dry-run que
proponga cambios) o si la pantalla la va a usar Tamara sin vos.

---

## Recetas — las preguntas que ya costaron caro

### Cadena de saldos rota
`transacciones_bancarias.saldo` es un saldo corrido almacenado. Cada fila
tiene que mover el saldo exactamente por su importe.

```sql
SELECT id_transaccion, fecha, documento, concepto, importe, saldo,
       ROUND(firmado, 2) AS delta_saldo,
       ROUND(ABS(ABS(firmado) - ABS(importe)), 2) AS gap
  FROM (SELECT *, saldo - LAG(saldo) OVER (ORDER BY fecha, id_transaccion) AS firmado
          FROM scintela.transacciones_bancarias WHERE no_banco = 10) t
 WHERE firmado IS NOT NULL AND ABS(ABS(firmado) - ABS(importe)) > 0.02
 ORDER BY fecha;
```

### Filas cargadas con fecha vieja (la causa raíz del 03/08)
Una fila que entra HOY con fecha de HACE UN MES rompe todo lo que camina por
`id_transaccion`.

```sql
SELECT id_transaccion, fecha, documento, concepto, importe,
       max_fecha_previa, (max_fecha_previa - fecha) AS dias_hacia_atras
  FROM (SELECT *, MAX(fecha) OVER (ORDER BY id_transaccion
                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS max_fecha_previa
          FROM scintela.transacciones_bancarias WHERE no_banco = 10) t
 WHERE fecha < max_fecha_previa
 ORDER BY id_transaccion DESC LIMIT 100;
```

### Saldo guardado vs. saldo sumado (y el mismo bug en chiquito)
`saldo_bancos()` lee el saldo de la ÚLTIMA fila, no suma. Si "última por
fecha" y "última por id" no son la misma fila, el balance está mal.

```sql
SELECT no_banco,
       (SELECT saldo FROM scintela.transacciones_bancarias t2 WHERE t2.no_banco = t.no_banco
         ORDER BY fecha DESC, id_transaccion DESC LIMIT 1) AS saldo_por_fecha,
       (SELECT saldo FROM scintela.transacciones_bancarias t3 WHERE t3.no_banco = t.no_banco
         ORDER BY id_transaccion DESC LIMIT 1)             AS saldo_por_id
  FROM scintela.transacciones_bancarias t GROUP BY no_banco;
```

Así apareció que **DEP.PICH. (banco 90)** muestra −455,89 cuando por fecha
cierra en 0,00: dos filas del 23 y 25/06, un ND mal posteado y su reverso.

### Fotos repetidas del mismo día en `historia`
Desde a95c835b (01/08) cada visita a `/historico-12m` saca una foto nueva. El
último día de un mes puede tener varias filas y el legado lee cualquiera —
por eso el patrimonio de un mes CERRADO cambia solo.

```sql
SELECT fecha, COUNT(*) AS fotos,
       ROUND(MAX(patrimonio) - MIN(patrimonio), 2) AS spread_patrimonio,
       ROUND(MAX(utilidad)   - MIN(utilidad), 2)   AS spread_utilidad
  FROM scintela.historia GROUP BY fecha HAVING COUNT(*) > 1
 ORDER BY fecha DESC;
```

**Regla:** toda lectura de `historia` necesita `ORDER BY fecha DESC,
id_historia DESC`. Sin el desempate, el número no es determinista.

### Matches N:M que no cierran
Agrupar por `confirm_batch_id` (migración 0071), **NO** por
`tb.id_transaccion`. Agrupar por el lado del programa parte una conciliación
2-contra-14 en 14 pedazos y ninguno cierra: el 03/08 daba **36 matches ·
+754.963,54** de falso positivo con la conciliación cerrada en +2,00.

```sql
COALESCE(m.confirm_batch_id, 'tx:' || m.id_transaccion::text) AS batch
```
Y dedupear los dos lados antes de sumar: el banco por
`(batch, real_fecha, real_documento, real_monto, real_tipo)`, el programa por
`id_transaccion`.

### Pendientes que en realidad son rótulos
El resumen del Excel cargado como movimientos: 6 filas, **$8.304.132,19**.

```sql
SELECT id, fecha, concepto, documento, monto, tipo, fuente
  FROM scintela.banco_historicos_pendientes
 WHERE UPPER(TRIM(COALESCE(documento,''))) ~ '(TOTAL|AJUSTE|DIFERENCIA|SALDO|PENDIENTES BANCO)'
 ORDER BY monto DESC;
```

### ¿Qué columnas tiene esta tabla?
No adivines nombres — un `column does not exist` cuesta otro round-trip.

```sql
SELECT table_name, ordinal_position, column_name, data_type
  FROM information_schema.columns
 WHERE table_schema = 'scintela' AND table_name LIKE '%banco%'
 ORDER BY table_name, ordinal_position;
```

---

## Invariantes de la data que muerden siempre

- **`importe` se guarda SIN signo.** El signo lo infiere `_signed_delta` del
  `documento` + `usuario_crea`, y las reglas difieren entre filas importadas
  del dBase y filas cargadas por la web. Nunca sumes `importe` crudo.
- **`saldo` es un saldo corrido almacenado**, no derivado. Cualquier inserción
  fuera de orden lo rompe hacia adelante.
- **`stat='Y'`** en toda query que sume desde `compra` o `factura`.
- **Formato de números EC/EU**: punto = miles, coma = decimales. `1.234,56`.
- **Zona horaria**: el contenedor y RDS están en UTC, Ecuador es UTC−5. Usá
  `today_ec()`, nunca `date.today()` ni `CURRENT_DATE`.
- **Conciliación**: `Libros − pendientes programa + pendientes banco = saldo
  banco esperado`. La diferencia se compara contra el extracto.
- **Un pendiente sin fecha puede ser legítimo** (pedido expreso de la dueña,
  2026-06-04: *"quiero que los −15.835,60 prevalezcan aunque no tengan
  fecha"*). Nunca filtres por "no tiene fecha ⇒ no es un movimiento".
- **`saldo_bancos()` toma el último saldo stored *que no sea cero*** (filtro
  `ABS(t.saldo) > 0.5`). Confunde "saldo 0 porque nadie lo calculó" con
  "saldo 0 porque la cuenta está vacía".

## Cosas que ya se decidieron — NO re-abrir sin preguntar

Cada una de éstas ya se investigó y se cerró. Reabrirlas cuesta una jornada y
molesta a la dueña, que ya tuvo que decir *"los kg ya dijimos que no están
duplicados, fijate la otra sesión"*.

| Tema | Estado |
|---|---|
| Kg de compras duplicados | **No están duplicados.** Los recargos que grabó el dBase repetían 70.354 kg; ya resuelto (2026-07-31). |
| DEP.PICH. −455,89 (debería ser 0,00) | **Real, y la dueña dijo que se deja** (03/08). Corregirlo mete +455,89 en la utilidad de agosto siendo una corrección de junio. Muere con la migración a `SUM()`. |
| Los 7 quiebres viejos (29/06–03/07, gap 297.123,54) | **No se tocan.** Son cicatrices del bug del ancla. El nivel actual lo valida la conciliación cerrando en +2,00. |
| Los 15.527,85 de anticipos | **Legítimo**: conversión manual de Andrés. Anticipos concilian al centavo. |
| El cierre de julio (2.382.056,31) | **Correcto.** Validado contra la conciliación. |
| Los +2,00 que quedan en la conciliación | `PAGO SENAE 51775463`: banco 17.467,11 vs anticipo AI 21/26 17.465,11. Abierto, no urgente. |

---

## Reglas de método (todas se pagaron caras)

1. **El extracto del banco es el árbitro, no los libros.** El 03/08 concluí
   que el número nuevo era el correcto; la conciliación mostraba que los
   libros estaban 155k ALTOS. Cuando dos números pelean, gana el papel del
   banco.
2. **Leé el archivo ENTERO.** Dije "no hay ninguna repetición, verificado por
   cuatro caminos" — los cuatro caminos miraban filas de movimiento. Las 6
   filas repetidas eran de resumen, y **mi propio parser las había salteado
   porque arrancaba en la primera fila con fecha**: el mismo bug que la app.
   Si la dueña insiste, no está equivocada.
3. **Antes de tocar datos previos a un cierre, dry-run que muestre Δ = 0.**
   Regla textual de ella: *"si tocás algo de antes de agosto tenés que dejarlo
   sin que la utilidad se mueva en absoluto"* y *"acordate que la conciliación
   cerró por 2 dólares antes de inventar todo"*. Un "nivelado" que parece
   inofensivo se propaga hasta hoy.
4. **Un filtro que se come un dato bueno es peor que el bug que arregla.**
   El blacklist "contiene" mataba `AJUSTE AC97 SIN FECHA` (dato legítimo).
   Matching normalizado y EXACTO.
5. **Mutá cada fix.** Rompé a propósito la línea arreglada y confirmá que un
   test se pone rojo. Si ninguno se cae, el test no prueba nada.
6. **Revisá la memoria y las sesiones previas antes de reabrir algo.**
   Reabrí la duplicación de kg como bug cuando ya estaba cerrada; ella tuvo
   que decir *"los kg ya dijimos que no están duplicados, fijate la otra
   sesión"*.
7. **Un ⚠ diario por algo legítimo entrena a ignorar el panel.** Los health
   sólo alertan sobre lo que de verdad está mal.

---

## Realidades del deploy (para no perder tiempo)

- Clonar main FRESCO a `/tmp`; el workspace local está ~160 commits atrás.
- Push a `HEAD:main` con el PAT → GitHub Actions corre CI + deploy, **~3 min**.
- **El deploy NO corre migraciones.** Toda tabla nueva se bootstrapea en
  caliente con `CREATE TABLE IF NOT EXISTS` (patrón `saldo_snapshot.py`,
  `papelera_pendientes.py`, `sql_console_view.py`).
- **Tailwind es un build JIT congelado**: las clases nuevas no renderizan.
  Usá `style=` inline o clases que ya existan.
- La API de check-runs de GitHub no contesta desde el contenedor: verificá el
  deploy por Chrome.
- CI: `make PY=python3 ci` — ~2.100 tests en ~35 s, coverage 100% sobre la
  lista corta de `.coveragerc`.
- Sin Postgres en CI: lo que vive en SQL se testea inspeccionando el fuente
  (`inspect.getsource`), patrón `test_auditar_agrupa_por_batch.py`.
