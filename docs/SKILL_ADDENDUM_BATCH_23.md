# Batch 23 — Caja/Bancos: opening balance + signed sum (audit-bug fix)

**Fecha:** 2026-04-30
**Trigger:** Usuario detectó "diferencia de auditoría" de $664K en /caja y dijo "pero acá encontrá la diferencia!!! de dónde sacaste el saldo de 80.000 si no? que pésimo trabajo".

## El bug

`modules/caja/queries.py::resumen()` calculaba `saldo_derivado` con `SUM(importe)` plano. Pero en `scintela.caja`:

- `importe` es siempre **POSITIVO** (valor absoluto).
- El signo viene del campo **`tipo`**: `'E'` = entrada (+), `'S'` = salida (−).

Resultado: `SUM(importe)` daba la suma de magnitudes (ingresos + |egresos|), que no significa nada. La UI mostraba un drift falso de $664K que asustaba al usuario y escondía el invariante real.

## Invariante real

```
saldo_running_última_fila = opening + Σ(importe WHERE tipo='E') − Σ(importe WHERE tipo='S')
```

donde `opening` = `saldo_primera_fila − signo×importe_primera_fila` (saldo que existía en caja antes del primer movimiento cargado).

Verificado con DBF (CAJA.DBF Files/, 30-abr-2026):

```
opening      = $16.161,16
Σ entradas   = $403.891,67  (266 filas tipo='E')
Σ salidas    = $339.998,84  (456 filas tipo='S')
saldo neto   = $63.892,83
opening + neto = $80.054,—  ✓ = saldo running última fila
```

Drift real = $0 → no warning (caso normal).

## Cambios

### 1. `modules/caja/queries.py::resumen()`

`saldo_derivado` ahora aplica el signo:
```sql
SUM(CASE WHEN tipo = 'E' THEN importe
         WHEN tipo = 'S' THEN -importe
         ELSE importe END) AS saldo_derivado
```

`ingresos` / `egresos` ahora son la suma sólo de su lado (no plano):
```sql
SUM(CASE WHEN tipo = 'E' THEN importe ELSE 0 END) AS ingresos,
SUM(CASE WHEN tipo = 'S' THEN importe ELSE 0 END) AS egresos,
```

Nuevo campo `opening` (saldo antes del primer movimiento):
```sql
(SELECT saldo - CASE WHEN tipo='E' THEN importe
                     WHEN tipo='S' THEN -importe
                     ELSE importe END
    FROM scintela.caja
    WHERE saldo IS NOT NULL
    ORDER BY fecha ASC, id_caja ASC LIMIT 1) AS opening
```

### 2. `modules/caja/templates/caja/lista.html`

Drift detection corregido:
```jinja
{% set saldo_esperado = opening + ingresos - egresos_abs %}
{% set drift = saldo_actual - saldo_esperado %}
```

Strip de contexto ahora muestra la suma educativa: `Saldo inicial + Entradas − Salidas = Saldo`. Warning de auditoría sólo si `drift>$0.01` (caso normal: no aparece).

### 3. `modules/bancos/queries.py::lista_bancos()`

Mismo patrón: agrega `opening` por banco. Bancos también arrancaron con saldo previo a la primera transacción cargada — sin opening, el drift indicator del template gritaba en cada chequera con histórico.

### 4. `modules/bancos/templates/bancos/lista.html`

Drift = `saldo_stored − (opening + saldo_derivado)`. Tooltip actualizado para reflejar la fórmula correcta.

## Lección para el futuro

**Antes de dibujar un "audit warning" en la UI, verificar que la fórmula del lado derecho efectivamente reconstruye el lado izquierdo cuando todo está sano.** Si el "esperado" no incluye el opening balance, va a fallar siempre que la chequera/caja tenga histórico previo al primer movimiento cargado, y el warning se vuelve ruido permanente que esconde drifts reales.

**Convención del campo `importe` en scintela.caja**: VALOR ABSOLUTO + signo en `tipo`. Cualquier query que sume `importe` sin diferenciar por tipo está bugueada por construcción. Documentado al tope del query `resumen()` para que no se repita.

## Archivos tocados

```
modules/caja/queries.py            (resumen() reescrita + docstring)
modules/caja/templates/caja/lista.html  (drift = saldo − (opening + neto))
modules/bancos/queries.py          (lista_bancos: + opening por banco)
modules/bancos/templates/bancos/lista.html  (drift indicator usa opening)
```

## Validación

- `python -m ruff check modules/caja/queries.py modules/bancos/queries.py` → All checks passed.
- `ast.parse` ambos archivos → ok.
- Verificación numérica end-to-end vs DBF en sandbox: bloqueada (no hay PG en el sandbox); el usuario debe refrescar `/caja` y `/bancos` en la app local para confirmar que el warning de $664K desapareció y que el strip ahora muestra el desglose correcto.
