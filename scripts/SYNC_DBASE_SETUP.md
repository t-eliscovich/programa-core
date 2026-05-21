# sync_dbase_actual — sync de DBFs del dBase a Programa Core

**Cuándo usar:** cada vez que llega un dump fresco de los `.DBF` del dBase
(típicamente: cierre de mes, conciliación post-fábrica, recovery).

**Script principal:** `scripts/sync_dbase_actual.py`
**Wrappea:** `scripts/import_dbf.py` (motor de import)
**Pre-mortem:** `PREMORTEM_IMPORT_DBASE_2026_05_20.md` (raíz del repo)

## Workflow estándar

```bash
# 1. Copiar los .DBF desde la fábrica
#    (puede ser scp, copia USB, o lo que sea — al final tienen que estar
#    en una carpeta local con todos los .DBF juntos).
mkdir -p ~/Downloads/dbase_dump_$(date +%Y_%m_%d)
cp /path/from/factory/*.DBF ~/Downloads/dbase_dump_$(date +%Y_%m_%d)/

# 2. Dry-run — NO toca la DB. Verifica encoding, parseo, conteos.
python scripts/sync_dbase_actual.py \
    --source ~/Downloads/dbase_dump_$(date +%Y_%m_%d) \
    --dry-run

# Si el dry-run muestra errores de encoding o pocas filas leídas, podés
# forzar encoding antes del run real:
python scripts/sync_dbase_actual.py \
    --source ~/Downloads/dbase_dump_2026_05_21 \
    --dry-run --encoding cp1252

# 3. Sync real — TRUNCATE+INSERT por tabla, en transacción.
python scripts/sync_dbase_actual.py \
    --source ~/Downloads/dbase_dump_$(date +%Y_%m_%d)

# 4. Verificar en la app
#    Abrir https://programa.intela.com.ec/informes/balance
#    El cuadro tiene que mostrar números consistentes con el cuadro dBase.
```

## Qué hace cada paso del script

```
═══════════════════════════════════════════════════════════════════
 sync_dbase_actual — TMT 2026-05-20
═══════════════════════════════════════════════════════════════════
  source        : ~/Downloads/dbase_dump_2026_05_21
  dry-run       : False
  encoding      : auto                    ← cp850 → cp1252 → latin-1 → utf-8
  backup        : sí                      ← pg_dump --data-only pre-import
  pre-checks    : sí
  post-checks   : sí
  backfills     : sí

─── PRE-CHECKS ────────────────────────────────────────────────
  source dir    : ✓ 15 archivos .DBF detectados
  schema version: ✓ schema OK (última migration: 0039)

─── BACKUP ────────────────────────────────────────────────────
  pg_dump       : ✓ backup guardado en _backups_sync/backup_pre_sync_*.sql

─── IMPORT ────────────────────────────────────────────────────
  ✓ FACTURAS.DBF   [CRITICO]  3.245 filas cargadas en scintela.factura
  ✓ CHEQUES.DBF    [CRITICO]  1.117 filas (+2 skipeadas por stat legacy)
  ✓ POSDAT.DBF     [SUPER]    412 filas
  ... (resto)
  ✓ TINTO.DBF      [CRITICO]  87 filas

─── POST-CHECKS ───────────────────────────────────────────────
  cheque_sin_cliente            ✓ 0
  factura_sin_cliente           ⚠ 3     ← FKs huérfanas, listadas
  posdat_sin_prov               ✓ 0
  factura.fecha                 ✓ 0     ← nulls críticos
  cheque.importe                ✓ 0
  xgast.num_sin_clasificar      ⚠ 8     ← V1..V9 a clasificar
  transacciones_bancarias.no_banco ✓ 0

─── BACKFILLS ─────────────────────────────────────────────────
  snapshots historia: 12 creados · 0 ya existían · 0 errores

─── DRIFT BALANCE ─────────────────────────────────────────────
  banco          snap=     2.374.112  live=     2.395.000  drift=  0.88% ⚠
  cart           snap=     6.956.200  live=     6.956.200  drift=  0.00% ✓
  ustock         snap=     8.097.939  live=     8.097.939  drift=  0.00% ✓
  deuda          snap=     2.106.100  live=     2.106.100  drift=  0.00% ✓
  patrimonio     snap=    20.341.200  live=    20.341.200  drift=  0.00% ✓

═══════════════════════════════════════════════════════════════════
 sync_dbase_actual TERMINADO
═══════════════════════════════════════════════════════════════════
```

## Flags disponibles

| Flag | Default | Para qué |
|---|---|---|
| `--source PATH` | `/Users/.../INTELA copy/Files` | Carpeta con los `.DBF` |
| `--dry-run` | `False` | Lee los DBFs, no toca DB |
| `--only TABLA,TABLA...` | (todas) | Solo importa esos DBFs |
| `--encoding ENC` | auto | Forzar encoding (cp850/cp1252/latin-1/utf-8) |
| `--skip-backup` | `False` | No hace `pg_dump` pre-import |
| `--skip-pre-checks` | `False` | Omite verificación de schema |
| `--skip-post-checks` | `False` | Omite sanity check post-import |
| `--skip-backfills` | `False` | Omite snapshots/reclasificación |
| `--verbose` / `-v` | `False` | Log detallado |

## Exit codes

- `0` — todo OK
- `1` — error en el import o sanity check no superado
- `2` — pre-check falló (schema, source dir)

## Qué resuelve este script respecto al `import_dbf.py` original

Diff funcional (qué cambió):

**Coerciones robustas:**
- `_date_robusto(v)` — acepta `datetime.date`, `DD/MM/YYYY`, `YYYY-MM-DD`,
  `DD-MM-YYYY`, `DD/MM/YY`. Antes solo `D` nativo.
- `_num_robusto(v)` — acepta `"1.234,56"` (es-EC), `"(123.45)"` (negativo
  en paréntesis), strings con espacios. Antes solo `float(v)` directo.

**Stat legacy remap (mappers):**
- Cheques `V → B`, `Y → skip`, `* → skip`.
- Facturas `V → A`, `Y → skip`, `* → skip`.
- `_remap_stat()` + `_STAT_LEGACY_MAP_*` por tabla.

**Encoding auto-detect:**
- `_read_dbf(path)` ahora prueba `cp850 → cp1252 → latin-1 → utf-8` y se
  queda con el que produce menos `?` (replacement chars). Antes solo
  `cp850` fijo.
- `--encoding=` override manual.

**Orchestrator nuevo (`sync_dbase_actual.py`):**
- **Pre-import:** `verificar_source_dir()`, `verificar_schema_version()`,
  `hacer_backup()` (pg_dump --data-only).
- **Post-import:** `contar_huerfanos()`, `contar_nulls_criticos()`.
- **Backfills encadenados:** `crear_snapshots_ultimos_meses(12)`.
- **Drift balance:** compara saldo del último snapshot vs balance live.
- Resumen estructurado por etapa, con `✓ / ⚠ / ✗` por check.

## Smoke test

```bash
python scripts/_test_sync_dbase_actual.py
```

Ejercita coerciones, stat remap, mappers skip-legacy, mes mapping,
source dir check, CLI args parsing, completitud de `TABLE_MAP`, MIN_MIGRATION
pinned. Pasa sin DB real.

## Cosas que NO cubre el script (decisión humana)

Listo aparte porque requieren intervención manual:

1. **FK huérfanas legítimas.** El script reporta `factura_sin_cliente: 3`
   pero NO crea stubs ni borra. Decisión humana: ver el listado, decidir
   si crear el cliente faltante en `/clientes/nuevo` o anular esas facturas.

2. **Xgast sin `num` (V1..V9).** El script reporta cuántos hay; la
   clasificación se hace manualmente desde `/informes/gastos` o corriendo
   `python scripts/sugerir_reclasificacion.py --top 30` después.

3. **Drift de balance > 5%.** Si el script lo reporta en rojo, es síntoma
   de algo mal: cambio de criterio entre dBase y PC, o cifras desplazadas
   por timing (snapshot al 31, live al 20). Decidir si:
   - regenerar snapshot del mes en curso (botón en `/informes/fuentes-y-usos`)
   - o revisar el `import_dbf.py` por algún mapper que está perdiendo data.

4. **Migraciones nuevas (post-0039).** Si agregás migration 0040+,
   actualizar `MIN_MIGRATION` en `sync_dbase_actual.py:84`. Sino el sync
   sigue corriendo contra schema viejo.

5. **Rollback.** Si algo sale catastrófico, el `pg_dump` que dejó el
   script en `_backups_sync/backup_pre_sync_*.sql` se puede restaurar con
   `psql $DATABASE_URL < _backups_sync/backup_pre_sync_YYYYMMDD_HHMMSS.sql`.
   Pero ojo: eso vuelve TODAS las tablas al estado pre-sync (incluyendo lo
   que cambió entre pre-sync y el descubrimiento del problema).
