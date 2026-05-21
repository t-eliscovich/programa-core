# Pre-mortem: cargar DBFs actuales del dBase a Programa Core

**TMT 2026-05-20** — análisis previo, NO documenta lo que pasó, sino qué
podría romperse cuando Tamara corra `python scripts/import_dbf.py` con los
DBFs frescos del dBase.

El script ya existe (`scripts/import_dbf.py`) y maneja muchos casos.
Este documento lista lo que **podría seguir rompiéndose** y la mitigación
disponible o por construir.

---

## 1. Lo que YA está cubierto en `import_dbf.py`

| Problema | Cubierto |
|---|---|
| Encoding cp850 (Windows ES legacy) | ✓ `encoding="cp850"` + `char_decode_errors="replace"` |
| Tipos heterogéneos (string vs number) | ✓ Coerciones `_str` / `_date` / `_num` / `_int` |
| Mes en inglés (`Apr`, `Aug`) y español (`Abr`) | ✓ `_MES_NUM` con ambos |
| Typos históricos (`JÔL`, `JÂL`, `EDT`) | ✓ Mapeo explícito en `_MES_NUM` |
| Columnas con espacio raro (`ST T`, `YY R`) | ✓ Probamos `r.get("ST")` ∥ `r.get("ST_T")` ∥ `r.get("ST T")` |
| TRUNCATE+INSERT idempotente por tabla | ✓ Transacción por tabla, falla aislada |
| Multi-DBF a misma tabla (PICHINCH+INTER → transacciones_bancarias) | ✓ `delete_where = (no_banco=%s, lookup_fn)` |
| Falta uno de los DBFs | ✓ Se omite (el que no esté no se borra) |
| Modo dry-run / `--only=` | ✓ Para iterar sin riesgo |

---

## 2. Lo que PROBABLEMENTE rompe y por qué

### 2a. Stats legacy preservados sin filtro

El script copia `stat` tal cual del DBF. La app actual lee stats vivos
(`Z`, `A`, `B`, `D`, `R`, `P`, `''`, `null`). Los valores legacy que pueden
venir en el dump:

- `V` (Internacional) en cheques — antes era "depositado en banco V". Hoy se
  usa `B`. El cheque queda invisible en /cheques y NO suma a totc.
- `Y` (físicamente borrado) — el `_archive` de dBase. Hoy no existe.
- `*` (reemplazo / sentinel) — algunas tablas viejas.
- `X` (anulado) en factura/compra/xgast — la app lo excluye correctamente,
  pero asegurate de que el set de stats vivos del balance NO incluya `X`.

**Mitigación:**
- Script de "limpieza post-import" que mapea stats legacy → modernos:
  `UPDATE scintela.cheque SET stat='B' WHERE stat='V';` (existía un mapeo,
  hay que revisarlo) — no está en `import_dbf.py`, conviene agregarlo como
  `post_load_<tabla>`.
- O alternativa: meter el mapping dentro del propio mapper antes del insert.

**Recomendación:** agregar a `_map_cheque` un dict `_STAT_LEGACY_MAP =
{'V':'B', 'Y': None}` y aplicar al campo. Conservar `Y` significa la fila
NO se debería importar (skipear silenciosamente).

### 2b. FKs huérfanas

`_map_cheque` setea `codigo_cli` del DBF directamente. Si un cliente fue
"limpiado" del dump pero un cheque lo referencia, el INSERT puede fallar si
hay un FK constraint sobre `scintela.cheque.codigo_cli → scintela.cliente.codigo_cli`.

Estado actual: **NO hay validación de FKs antes del insert.** Si el DDL no
tiene el FK, no falla — pero queda data huérfana que rompe joins en `/cheques`,
`/cartera`, etc.

**Mitigación:**
- Agregar un post-import check: `SELECT COUNT(*) FROM scintela.cheque c
  LEFT JOIN scintela.cliente cli ON cli.codigo_cli=c.codigo_cli
  WHERE cli.codigo_cli IS NULL`. Reportar cuántos huérfanos hay.
- Si pasa el threshold (>0%), bloquear el deploy y pedir el CLIENTES.DBF.

### 2c. Columnas NUEVAS de Programa Core que no existen en el DBF

Tablas que ahora tienen columnas que el DBF no trae — el INSERT las deja en
NULL o en el default del DDL. Lista de las que pueden romper:

| Columna | Tabla | Para qué se usa | Riesgo |
|---|---|---|---|
| `fecha_recibido` | cheque | aging de cartera | aging mal sin ella |
| `motivo_anulacion` | posdat, cheque | UI mostrar razón | quedan en NULL |
| `batch_id` | mov_doble (no aplica al DBF) | grupos lógicos | OK, no se importa |
| `num` (xgast V1..V9) | xgast | clasificación V1..V9 | `_map_xgast` SÍ lo importa |
| `anulada` | posdat | soft delete | NULL → contado como activo (puede ser OK) |
| `numreferencia` | transacciones_bancarias | matching conciliación | `_map_banco_trans` SÍ lo importa |
| `vendedor` (migration 0032) | factura | dashboards Metabase | NULL → cards vendedor sin data |
| `tipo_proveedor` | proveedor | clasificación | NULL → defaults |

**Mitigación:** documentar en CHANGELOG por cada migration nueva si el DBF
tiene o no el campo correspondiente. Si no lo tiene, qué default razonable
poner. Ya hay precedente en `_map_factura` (`"usuario_crea": "dbf-import"`).

### 2d. Tablas que el DBF NO tiene

- `mov_doble` (migration 0031) — no existe en dBase. /historial queda vacío
  para data legacy. **Esto es por diseño** — los movimientos pre-PC se
  reconstruyen de las tablas raw. La UI ya muestra "—" para esas filas.
- `gasto_forzado` (migration 0033) — flujo de fondos vive en PC ahora.
  Quedaba en localStorage del browser antes, ahora en DB. El DBF no lo trae.
- `conciliacion_manual_log` (migration 0039 — la nueva) — lo mismo.
- `iniciales` (proyecciones del año) — SÍ está en DBF (INICIALE.DBF).
- `activos` (terrenos/edificios/maquinaria) — SÍ (ACTIVOS.DBF).

### 2e. Encoding raro en concepto: Ñ, acentos, símbolos

El DBF viene en cp850. `dbfread` con `encoding="cp850"` y
`char_decode_errors="replace"` reemplaza con `?` los caracteres no
decodificables, pero algunas filas históricas pueden tener basura.

**Ejemplo conocido:** los mes typos (`JÔL`, `JÂL`) — ya mapeados.

**Riesgo extra:** un concepto como `"COBR Ñ FAR"` puede aparecer como
`"COBR ? FAR"`. NO rompe el insert, pero rompe la búsqueda por texto exacto.

**Mitigación:**
- Detectar `?` en campos texto post-import y loggear cuántos hay.
- Si pasa un threshold, el operador puede re-correr con `encoding="cp1252"`
  manualmente (param `--encoding=cp1252` no existe todavía — agregarlo).

### 2f. Fechas TEXT con formato inconsistente

`dbfread` decodifica las fechas `D` como `datetime.date` directo. Pero hay
casos donde el DBF las guardó como TEXT (campo `C`). Por ejemplo, en
`ordenes` del proyecto Intela formulas_app, `fecha` es TEXT `DD/MM/YYYY`.

**En el Programa Core dump:** revisar si POSDAT.DBF tiene FECHA como `D` o
`C`. `_map_posdat` usa `_date(r.get("FECHA"))` — si viene como string, retorna
None. La fila se inserta con fecha NULL → no aparece en aging.

**Mitigación:** en `_date()`, agregar fallback que intente parsear strings
DD/MM/YYYY / YYYY-MM-DD si la primera comprobación falla. (Ya está en
`_parse_fecha_celda` del parser_xlsx — copiar el patrón.)

### 2g. Importes con coma decimal o como string

`_num()` hace `float(v)`. Si `v = "1234,56"` (decimal con coma), `float()`
falla → retorna `default=None` → el INSERT pone NULL en importe → cartera
descuadra.

Estado: dBase normalmente entrega numérico como float, no string. Pero hay
campos calculados que pueden venir mal.

**Mitigación:** ya existe en `_parse_monto` de `parser.py` (módulo
conciliación). Copiar a `_num()` de `import_dbf.py`:

```python
if isinstance(v, str):
    v = v.strip().replace(" ", "")
    if "," in v and "." in v: v = v.replace(".","").replace(",",".")
    elif "," in v: v = v.replace(",",".")
```

### 2h. Migraciones aplicadas previamente

Si la DB destino corre con un schema viejo (pre-0027), `import_dbf.py`
puede fallar al INSERT en `posdat` por columna `anulada` inexistente. El
script no chequea el schema antes.

**Mitigación:** correr siempre `python scripts/migrate.py` antes del import.
Si está documentado en el README, hacerlo más visible. Agregar una
verificación al inicio de `import_dbf.py`: `SELECT max(num) FROM
scintela.migrations` y abortar si menor a la última que el código espera.

### 2i. Códigos 3-char (BED, CLR, etc.) que ya no están en el catálogo

`scintela.factura.codigo_cli` viene del DBF. La app actual usa estos como
FK a `scintela.cliente`. Si el cliente fue borrado, las cuentas viejas
quedan huérfanas.

Estado: la app NO falla — usa LEFT JOIN. Pero el cuadro de cuenta del
cliente muestra "cliente desconocido".

**Mitigación:** post-import script que detecte códigos en facturas/cheques
que NO existan en `scintela.cliente`. Listarlos y proponer crear stubs.

### 2j. Dump de historia con columnas en 0

Este YA pasó (es el bug que vimos hoy con `/fuentes-y-usos`). El DBF de
HISTORIA puede tener algunas columnas en 0 si el dueño nunca cerró el
balance ese mes. `_map_historia` los importa tal cual → balance_components_as_of
hereda los 0 → fuentes-y-usos muestra Δ espureos.

**Mitigación ya implementada (v5c/v7):**
- `_es_snap_vacio()` detecta y avisa con un cartel amarillo.
- `_delta()` evita Δ espureo cuando una columna está en 0 en h_ini.

**Próximo paso:** script que regenere snapshots de los últimos N meses
usando `balance_components_as_of(last_day_mes)` para llenar las columnas
faltantes con valores derivados de las tablas raw. Idempotente: solo
sobreescribir si la columna está en 0.

---

## 3. Checklist pre-import recomendado

Antes de correr `python scripts/import_dbf.py`:

1. ✅ Backup snapshot de la DB actual (RDS toma snapshots cada noche; igual
   manual no hace daño).
2. ✅ Correr `python scripts/migrate.py` para asegurar schema actualizado.
3. ✅ Copiar los DBFs frescos a `/Users/tamaraeliscovich/Documents/INTELA copy/Files/`.
4. ✅ `python scripts/import_dbf.py --dry-run` — verificar conteos esperados.
5. ✅ `python scripts/import_dbf.py` — corre transacción por tabla.
6. ✅ Verificar /informes/balance — TOTAL ACTIVO, PASIVOS, PATR.NET dentro
   del rango esperado.
7. ✅ Verificar /informes/fuentes-y-usos — sin el cartel amarillo de
   columnas vacías.
8. ✅ `python scripts/smoke_test_dbase_port.py` (si existe ese smoke test).

---

## 4. Próximas mejoras al `import_dbf.py` que reducirían riesgo

Por orden de impacto:

1. **Detectar y mapear stats legacy** (V→B, Y→skip) en el mapper.
2. **Validar FKs post-import** y abortar/avisar si hay huérfanos.
3. **Soporte `--encoding=`** flag para forzar cp1252 cuando cp850 falla.
4. **Verificar schema version** al inicio (compara con un número que vive en
   `scintela.migrations`).
5. **`_num()` robusto** con parsing de strings es-EC.
6. **Post-import sanity checks** automáticos: contar huérfanos, contar `?`
   en concepto, contar fechas NULL en tablas críticas.

Ninguno bloquea el import actual, pero cada uno cierra un agujero que ya
nos picó al menos una vez.
