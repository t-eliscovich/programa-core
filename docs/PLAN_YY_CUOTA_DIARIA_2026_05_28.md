# Plan — YY cuota diaria display-time

**Fecha:** 2026-05-28
**Pedido dueña:** "necesito que cada día incrementen como corresponden... ayudame
a encontrar la cuota diaria que se le suma día a día a este importe hasta
terminar el mes, no hace falta que corramos un job creo solo con calcular el día
se puede"
**Estado al inicio:** importe actual (28/05) es CORRECTO. Mañana 29/05 tiene que
verse `importe_hoy + cuota_diaria` sin tocar la fila a mano ni depender de cron.

---

## Decisión de diseño

**Snapshot HOY + display-time + L-V.**

- `posdat.importe` (lo que está hoy en la DB) queda como **baseline congelado**.
- Cada vez que se renderiza `/posdat?tab=yy` (y los consumidores listados en
  §Impacto), el importe que ve la dueña se calcula al vuelo:

```
importe_display = importe_base
                + cuota_diaria × días_hábiles_pasados_desde(baseline_date, hoy)
```

- `baseline_date` arranca en **2026-05-28** para todas las filas YY existentes.
  Se guarda en la propia fila (columna nueva o reusando `usuario_modifica` /
  metadata, ver §Esquema).
- "Días hábiles" = lunes a viernes inclusive. Sábado y domingo no suman (igual
  que el cron `correr_provisiones_diarias` actual).
- **Cierre mensual lazy con reset a 0**: el primer hit del mes nuevo:
  1. Calcula el `importe_display` como si hoy fuera el último día del mes anterior.
  2. Registra `mov_doble tipo='posdat_yy_cierre_mes'` con ese valor (queda en /historial).
  3. **Resetea `importe = 0`** y `baseline_date = último_día_del_mes_anterior`
     (típicamente 31 — no necesariamente hábil; lo importante es que la fórmula
     `dias_habiles((baseline, hoy])` cuente el primer día hábil del mes nuevo
     como offset=1).
  4. La fila YY queda lista para empezar a acumular desde 0 el mes nuevo.
  Sin cron — se dispara en la primera lectura del mes nuevo.

### Por qué snapshot HOY y no "desde día 1 del mes"

Si la fórmula fuera `cuota_diaria × días_del_mes`, los valores que se ven hoy
(SUELDOS 86.100, ALQUILER 8.500, etc.) cambiarían de golpe a otra cosa
(SUELDOS 5.000 × 20 días hábiles ≈ 100.000) porque los importes históricos no
fueron generados linealmente por la cuota actual. Snapshot HOY respeta lo que
ya está en pantalla y SOLO toca el futuro.

---

## Cómo se ve cada día (ejemplo SUELDOS, cuota_diaria = 5.000)

| Día        | Hábil | importe_base persistido | baseline_date | offset hábiles | importe_display |
|------------|-------|------------------------:|--------------:|---------------:|----------------:|
| Jue 28/05  | sí    | 86.100                  | 28/05         | 0              | **86.100** ← hoy |
| Vie 29/05  | sí    | 86.100                  | 28/05         | 1              | **91.100**       |
| Sáb 30/05  | no    | 86.100                  | 28/05         | 1              | 91.100           |
| Dom 31/05  | no    | 86.100                  | 28/05         | 1              | 91.100           |
| **Lun 01/06** | sí | 0 ← reset             | 31/05         | 1              | **5.000**        |
| Mar 02/06  | sí    | 0                       | 31/05         | 2              | 10.000           |
| ...        |       |                         |               |                |                  |
| Mar 30/06  | sí    | 0                       | 31/05         | 22 aprox       | ~110.000         |
| Mié 01/07  | sí    | 0 ← reset               | 30/06         | 1              | 5.000            |

El cierre del 01/06 hace tres cosas en la misma tx:
1. Calcula el importe_display "como si hoy fuera 31/05" → 91.100 (= 86.100 + 1 hábil × 5.000).
2. Registra `mov_doble tipo='posdat_yy_cierre_mes'` con importe=91.100, fecha=31/05,
   concepto="Cierre YY mayo 2026 — SUELDOS". Esto queda en /historial: la dueña
   puede consultar a cuánto llegó cada YY a fin de cada mes.
3. `UPDATE posdat SET importe = 0, baseline_date = '2026-05-31'` (último día calendario
   de mayo). Así el lunes 01/06, `dias_habiles((31/05, 01/06]) = 1` (cuenta el lunes)
   y el display arranca en 5.000.

**Por qué baseline_date = último día calendario del mes anterior (no del hábil):**
da igual si 30/05 o 31/05 — ninguno es hábil. Lo importante es que sea cualquier fecha
≤ último día del mes anterior y ≥ último hábil del mes anterior, así la fórmula
`dias_habiles((baseline, hoy])` cuenta correctamente desde el primer hábil del mes
nuevo. Usamos 31/05 (último calendario) por simplicidad.

---

## Esquema (mínimo)

Una sola columna nueva en `scintela.posdat`:

```sql
ALTER TABLE scintela.posdat
  ADD COLUMN baseline_date DATE;
```

Migración 0058 (siguiente disponible):

- Setea `baseline_date = CURRENT_DATE` para todas las filas con `prov='YY'` y
  `(banc IS NULL OR banc = 0)`.
- Resto queda NULL (no aplica el cálculo).

Alternativa SIN nueva columna: usar `usuario_modifica` con un marker
(`'baseline:2026-05-28'`) — más feo, pero evita migración. Descartado porque
ensucia la columna que ya se usa para audit.

---

## Cambios de código

### `modules/posdat/queries.py`

**Nuevo helper:**

```python
def _dias_habiles_entre(desde: date, hasta: date) -> int:
    """Cuenta lunes-viernes en (desde, hasta]. Excluye `desde`, incluye `hasta`."""
    if hasta <= desde:
        return 0
    d, n = desde, 0
    while d < hasta:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0-4 = L-V
            n += 1
    return n
```

**En `buscar()` (después del bloque que ya setea `cuota_diaria`):**

```python
from calendar import monthrange
hoy = date.today()
for r in rows:
    if (r.get("prov") or "").upper() != "YY":
        continue
    cd = float(r.get("cuota_diaria") or 0)
    base_date = r.get("baseline_date")
    if not cd or not base_date:
        continue
    # Cierre mensual lazy si cruzamos mes:
    if (base_date.year, base_date.month) != (hoy.year, hoy.month):
        # 1. Calcular el importe_display "como si hoy fuera fin del mes ant".
        ult_dia_mes_ant = date(
            base_date.year, base_date.month,
            monthrange(base_date.year, base_date.month)[1],
        )
        offset_cierre = _dias_habiles_entre(base_date, ult_dia_mes_ant)
        importe_cierre = round(float(r["importe"]) + cd * offset_cierre, 2)
        # 2. Registrar mov_doble del cierre mensual + UPDATE reset.
        _ejecutar_cierre_mensual(r, importe_cierre, ult_dia_mes_ant)
        # 3. Trabajar el resto del cálculo con el nuevo baseline.
        r["importe"] = 0
        base_date = ult_dia_mes_ant
    offset = _dias_habiles_entre(base_date, hoy)
    r["importe_base"] = float(r["importe"])  # para debug en template
    r["importe"] = round(r["importe_base"] + cd * offset, 2)
    r["dias_offset"] = offset
```

**`_ejecutar_cierre_mensual` (en `posdat.queries`):**

```python
def _ejecutar_cierre_mensual(row, importe_cierre, ult_dia_mes_ant):
    """Persiste el cierre del mes anterior + reset a 0 para YY.
    Idempotente — chequea que no exista ya un mov_doble del cierre."""
    id_posdat = row["id_posdat"]
    with db.tx() as conn:
        # Defensivo: si ya hay un cierre de ese mes, no re-registrar.
        ya = db.fetch_one(
            "SELECT 1 FROM scintela.mov_doble "
            " WHERE origen_table='posdat' AND origen_id=%s "
            "   AND tipo='posdat_yy_cierre_mes' AND fecha=%s",
            (id_posdat, ult_dia_mes_ant), conn=conn,
        )
        if not ya and importe_cierre > 0:
            import mov_doble as _md
            _md.registrar(
                conn=conn, tipo="posdat_yy_cierre_mes",
                origen_table="posdat", origen_id=id_posdat,
                destino_table="posdat", destino_id=id_posdat,
                importe=importe_cierre, fecha=ult_dia_mes_ant,
                concepto=f"Cierre YY {ult_dia_mes_ant:%Y-%m} — {row.get('concepto') or ''}"[:200],
                usuario="cierre_yy_lazy",
                metadata={"mes": ult_dia_mes_ant.strftime("%Y-%m")},
            )
        db.execute(
            "UPDATE scintela.posdat "
            "   SET importe = 0, baseline_date = %s, "
            "       usuario_modifica = 'cierre_yy_lazy' "
            " WHERE id_posdat = %s",
            (ult_dia_mes_ant, id_posdat), conn=conn,
        )
```

### `modules/posdat/templates/posdat/lista.html`

- Mostrar `importe_base` chiquito en tooltip cuando hay offset > 0:
  `title="base 28/05 = 86.100 + 5.000 × 1 día"`.
- KPI "Subió hoy" (ya existe, rojo) sigue mostrando `total_cuota_diaria`
  pero la etiqueta cambia: en lugar de "creció de ayer a hoy" decir
  "se suma cada día hábil".

### Cron `correr_provisiones_diarias` (en `modules/informes/queries.py`)

**Pasa a no-op** salvo el bake-in mensual:

- Si la fecha del lock es del mes actual → return sin tocar nada (la fórmula
  display ya cubre).
- Si detecta cambio de mes → corre el bake-in para TODOS los YY que tengan
  `baseline_date` del mes anterior. Esto es defensa en profundidad: el lazy
  bake-in también cubre el caso, pero si nadie abre la app el 01/06 el cron
  igual lo deja consolidado.

Sigue siendo idempotente (ya tiene marker en `sistema_meta`).

### Migración 0058_posdat_baseline_date.sql

```sql
ALTER TABLE scintela.posdat ADD COLUMN baseline_date DATE;
UPDATE scintela.posdat
   SET baseline_date = CURRENT_DATE
 WHERE UPPER(COALESCE(prov, '')) = 'YY'
   AND COALESCE(banc, 0) = 0
   AND (anulada IS NOT TRUE OR anulada IS NULL);
COMMENT ON COLUMN scintela.posdat.baseline_date IS
  'YY only: fecha desde la cual cuota_diaria se acumula display-time.';
```

---

## Impacto / consumidores

Lugares que leen `posdat.importe` para YY y se ven afectados:

| Ruta / módulo                     | Acción                                                |
|-----------------------------------|-------------------------------------------------------|
| `/posdat?tab=yy`                  | Fórmula aplicada en `queries.buscar` ✓               |
| `/posdat?tab=yy` KPIs hero         | `resumen()` necesita la misma corrección              |
| CSV export                        | Hereda del `buscar()` patched ✓                      |
| `/informes/balance` (posdat YY total) | `posdat.queries.resumen(tab='yy')` → patchear igual |
| `/informes/deudas`                 | idem `resumen('yy')`                                  |
| `/informes/flujo`                  | Proyecta posdat banc=0 — verificar si toma importe directo |
| `/historial`                       | Sin cambio (lee `mov_doble`, no `posdat.importe`)    |
| Inline edit del importe en /posdat | Cuando dueña edita un YY a mano: persistir como nuevo `importe_base` Y resetear `baseline_date = today` |

---

## Edge cases

1. **Dueña edita importe a mano** (input inline). El nuevo valor se persiste como
   `importe_base` con `baseline_date = today`. El offset vuelve a 0 desde ese día.
2. **Cambio de cuota_diaria (vía /provisiones)**. No requiere reset del baseline;
   el incremento futuro usa el nuevo `cuota_diaria` desde ese día. (La dueña
   pidió esto explícitamente: pueden coexistir cuotas viejas y nuevas en el
   mismo período sin que se "rebake" todo.)
3. **Posdat YY sin provisión matcheada** (caso RT). Se respeta el cálculo
   ya existente `cuota_diaria = importe / días_total`, pero NO se aplica
   bake-in: RT tiene fechad explícito y la fila se cierra al pagarse.
   Filtro: solo aplicar la fórmula nueva si `prov='YY'` exacto (no 'RT').
4. **Posdat YY anulada o cerrada (banc≠0)** — no aplica.
5. **Domingo a la noche → lunes**: offset cuenta lunes como +1 (cae en `weekday<5`).

---

## Plan de implementación (orden)

1. Migración 0058 (columna + backfill `baseline_date = CURRENT_DATE`).
2. Helper `_dias_habiles_entre` + tests unitarios (incluyendo cruces de mes,
   feriados se ignoran porque dBase tampoco los respetaba).
3. Patch en `posdat.queries.buscar` y `resumen` (mismo cálculo en ambos).
4. Patch en `template lista.html` (tooltip + label KPI).
5. Patch en inline-edit handler `api_editar` para resetear `baseline_date`
   cuando viene `importe` explícito en YY.
6. Tests de integración: rendear lista 28/05 → 29/05 → 01/06 con fechas mockeadas
   (`freezegun`) y verificar valores esperados.
7. Decisión sobre `correr_provisiones_diarias`: dejarlo activo SOLO para
   bake-in mensual (defensa en profundidad). Tests.
8. Deploy + smoke 29/05 mañana.

---

## Riesgos

- **Cron viejo todavía corre** mientras testeamos → puede duplicar incrementos
  si no lo neutralizamos primero. Mitigación: deploy en este orden — neutralizar
  cron FIRST, después aplicar migración + código.
- **Cuotas diarias erróneas** (ej. ALQUILER en pantalla muestra 14.000 vs dBase
  legacy 700). No es tarea de este plan corregir los valores — sólo aseguramos
  que lo que esté en `provisiones.importe` se sume cada día. Si la dueña ve un
  valor raro, lo edita inline.
- **Sin feriados**: si Ecuador cae en feriado L-V, el sistema igual suma. Igual
  que dBase hoy. No vale la pena agregar tabla de feriados ahora.

---

## Verificación mañana 29/05/2026

- Abrir `/posdat?tab=yy`.
- Cada fila YY debe mostrar exactamente `importe_28_05 + cuota_diaria × 1`.
- KPI "+ Suma cada día hábil" muestra suma de `cuota_diaria`.
- Tooltip del importe muestra "Base 28/05 = X + cuota × 1 día hábil".
- Sábado/domingo: mismo valor que viernes.

---

## Estado de implementación (2026-05-28)

**Hecho:**
- Migración `0061_posdat_yy_baseline_date.sql` (ALTER TABLE + backfill +
  marker del cron a HOY + comentario + índice parcial).
- `modules/posdat/queries.py`: helpers `_dias_habiles_entre`,
  `_ultimo_dia_del_mes`, `_ejecutar_cierre_mensual_yy`,
  `_aplicar_display_time_yy`. Patch en `por_id`, `crear`, `editar`,
  `buscar`, `resumen` (este último delega a `buscar` para tab='yy').
- `modules/informes/queries.py`: cron `correr_provisiones_diarias` filtra
  `baseline_date IS NULL` en el `first_match` — no doble-cuenta filas
  display-time.
- `modules/posdat/templates/posdat/lista.html`: tooltip que muestra
  "Base 28/05 = 86.100 + 5.000 × 1 día hábil" y labels del KPI ajustados.
- `tests/test_posdat_yy_display_time.py`: 29 tests unit (0 fallos),
  cubren la matemática + cierre lazy + edge cases.

**Pasos de deploy** (orden importa):

1. **Aplicar migración 0061 antes** de deployar el código nuevo. Si se
   deploya el código sin la columna, `buscar()` levanta error de SQL.
   ```cmd
   psql ...formulas_app < migrations/0061_posdat_yy_baseline_date.sql
   ```
   Pero en este proyecto las migraciones corren contra el DB de Programa
   Core (no formulas_app). Ver `intela-aws-deploy` skill / runbook.

2. **Deploy del código** (push to main → Federico también puede co-editar
   pero esta área es segura por la memoria `feedback_git_workflow_federico`).

3. **Smoke 5 minutos después**:
   ```
   /posdat?tab=yy  → cada fila igual que antes (offset=0).
   /informes/balance → bloque "Posdatados YY" cuadra con KPI hero.
   /informes/deudas → KPI Total Deudas no cambió.
   ```

4. **Mañana 29/05 09:00**: confirmar que cada YY subió su `cuota_diaria`.

**Rollback rápido** si algo se rompe:
- Revert del código (deja la columna baseline_date, no molesta).
- O bien: `UPDATE scintela.posdat SET baseline_date = NULL WHERE prov='YY'`
  → el cron viejo vuelve a operar normalmente (filtra IS NULL).
