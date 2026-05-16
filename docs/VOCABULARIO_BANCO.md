# Vocabulario canónico — Banco / Cuenta

Glosario para la convención de naming sobre referencias a cuentas bancarias. Lo escribe TMT 2026-05-14 después del audit del bloque C (#41): hay inconsistencia entre `no_banco` / `banco` / `cuenta` / `nb` en el código y los templates. Este doc fija el contrato; el código NO se refactoriza retroactivamente (riesgo alto) — sólo se aplica al código nuevo.

## Modelo de datos

```
scintela.banco
    no_banco       INT  PRIMARY KEY    -- el identificador
    nombre         VARCHAR              -- "Pichincha", "Internacional", etc.
```

`no_banco` es el ID. Los valores conocidos hoy:
- `10` — Pichincha (Banco principal).
- `32` — Internacional.

En el código legacy dBase también aparece `nb` (alias del mismo). Y en algunas vistas hay `id_banco` o `banco`. Todos refieren al mismo `scintela.banco.no_banco`.

## Convenciones canónicas

### Python (parámetros de función, kwargs)

**`no_banco`** — siempre que se pase el ID a un helper Python. Coincide con el nombre de la columna. Ejemplos:

```python
bank_helpers.insert_movimiento_bancario(conn, no_banco=10, ...)
bank_helpers.recompute_saldos_desde(conn, no_banco=10, ancla_id=12345)
queries.transferir(no_banco_origen=10, no_banco_destino=32, ...)
```

### SQL (columnas y joins)

**`no_banco`** — siempre. Como en el schema. Nunca aliasear a `banco` o `id`.

```sql
SELECT b.no_banco, b.nombre, t.importe
  FROM scintela.banco b
  JOIN scintela.transacciones_bancarias t ON t.no_banco = b.no_banco
 WHERE b.no_banco = 10;
```

### Templates Jinja (variables expuestas a la vista)

**`banco`** (string legible: nombre) — cuando es para mostrar al usuario.
**`no_banco`** (int) — cuando es para construir URLs o `<option value>`.

```jinja
<a href="{{ url_for('bancos.detalle', no_banco=b.no_banco) }}">{{ b.nombre }}</a>
```

Si el template necesita el dict completo: usar `b` o `banco`. Acceder por atributo `b.nombre`, `b.no_banco`. Nunca aliasear a `cuenta`.

### URLs / query params

**`no_banco`** — siempre el parámetro de query. `?no_banco=10`. No usar `banco_id` ni `cuenta`.

### Forms (HTML)

**`no_banco`** — el name del input/select cuando se trata de elegir el banco. No usar `banco` ni `cuenta`.

```html
<select name="no_banco">
  <option value="10">Pichincha</option>
  <option value="32">Internacional</option>
</select>
```

## Qué NO usar (legacy / inconsistencias)

- **`nb`** — alias dBase. Aparece en MENU.PRG y en algunas migraciones. En código nuevo: nunca. En código viejo: dejar igual, no refactorizar.
- **`cuenta`** — ambiguo (cuenta corriente vs cuenta contable). Si querés referirte al banco, usar `no_banco`/`banco`. Si te referís a otra cosa (ej. cuenta contable de un asiento), nombrarlo distinto.
- **`banco_id`** — no. La columna se llama `no_banco`.
- **`id_banco`** — tampoco. Aunque la convención de otros IDs es `id_*`, en `banco` la PK histórica es `no_banco` (legacy dBase) y se respeta para no romper joins.

## Excepciones documentadas

En `scintela.posdat.banc` la columna se llama `banc` (no `no_banco`), con semántica especial (0=abierta, 9=pagada, 1/2/...=banco). NO confundir con `no_banco`. La convención está documentada en `modules/posdat/__init__.py` y SKILL.md.

En `scintela.retiros.nb` y `scintela.flujo.banco1/banco2` quedaron nombres legacy. Las queries son read-only para esos casos. No refactorizar.

## Cuando agregar código nuevo

1. Si necesitás referirte al banco: `no_banco` (int) en Python/SQL/URL, `banco` (nombre legible) sólo en templates de presentación.
2. Si dudás, mirar `modules/bancos/queries.py` — es el módulo canónico.
3. NO uses `id`, `banco_id`, `cuenta`, `nb`, ni `id_banco` en código nuevo.
