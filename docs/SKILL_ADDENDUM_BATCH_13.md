# Addendum para `skills/programa-core/SKILL.md` — batch 13

> Pegar al SKILL.md cuando haya acceso al host (después de batch 12).

## Sesión 2026-04-17 — batch 13 (CSV upload inline en transaccionales)

Ampliación del pattern de upload CSV que batch 4 estableció para flujo. Ahora
facturas, cheques y compras tienen upload inline — el usuario puede subir N
registros en un CSV con las mismas columnas que el alta individual.

### Lo que shippó

**1. Helper compartido `csv_upload.py`** (root del proyecto):
- `parse_fecha(s)`, `parse_monto(s)`, `parse_int(s)`, `parse_bool(s)` tolerantes.
- `plantilla_csv(cols)` → string CSV con headers + 1 fila vacía de ejemplo.
- `procesar_csv(raw, cols, crear_fn, usuario)` → parser end-to-end que llama
  `crear_fn(**campos)` por fila. Devuelve `ResultadoUpload(ok, error, detalles)`.
- Mapeo de headers case-insensitive + accent-insensitive ("Codigo" matchea
  "Código").
- Coerción de tipos por nombre de campo: `_INT_FIELDS`, `_FECHA_FIELDS`,
  `_MONTO_FIELDS`, `_BOOL_FIELDS`.
- Best-effort por fila: una fila con error no rompe el batch.

**2. Rutas nuevas `/cargar-csv`** en facturas, cheques y compras:
- `GET /facturas/cargar-csv` → form con upload.
- `GET /facturas/cargar-csv?plantilla=1` → descarga CSV vacío con BOM UTF-8 +
  headers correctos para Excel ES.
- `POST /facturas/cargar-csv` → procesa y muestra reporte per-fila.
- Mismas rutas en `/cheques/cargar-csv` y `/compras/cargar-csv`.

**3. Columnas por entidad** (mismas que `crear()`):
- **Facturas**: fecha, codigo_cli, kg, importe, numf, vencimiento, numf_completo,
  tipo, condición, clave.
- **Cheques**: fecha, codigo_cli, no_cheque, importe, no_banco, banco_texto,
  fechad, stat, prov, clave.
- **Compras**: fecha, codigo_prov, importe, kg, numero, comprobante, concepto,
  tipo, fechad, no_banco, clave, pagada.

**4. Upload inline en los listados**:
- Botón "Cargar CSV" en `/facturas`, `/cheques`, `/compras` del header.
- El botón es un `<label>` que envuelve un `<input type="file" onchange="submit()">` oculto.
- Al elegir archivo, el form se auto-postea al endpoint → va directo al reporte.
- Link "↓ plantilla" al costado descarga el CSV vacío.

**5. Partials compartidos**:
- `templates/_csv_upload.html` — form de upload reusable por cualquier módulo.
- `templates/_csv_upload_resultado.html` — tabla de resultados.

**6. Tests**: `tests/test_csv_upload.py` — 27 tests.

### Métricas al cierre

- `pytest -q` → **321 passed, 9 skipped** (era 293 → +27 + 1 re-escrito).
- `python tests/test_routes_smoke.py` → **56 GET routes walked, 0 failures**
  (era 53 → +3 por `/facturas/cargar-csv`, `/cheques/cargar-csv`, `/compras/cargar-csv`).
- `ruff check` — clean en archivos nuevos.

### Invariantes nuevos

- **CSV upload → llama `crear()`, no inserta directo**. Las reglas de negocio
  (saldo=importe, stat='A', posdat automática, período cerrado, FK check) se
  aplican per-row porque pasa por el mismo path del alta individual.
- **Errores por fila no frenan el batch**. `procesar_csv()` captura
  `ValueError` (reglas de negocio) y `Exception` (SQL) por fila y las reporta
  en `ResultadoUpload.detalles[]`. El resto del CSV sigue.
- **Plantilla es el formato canónico**. Cualquier CSV con otros headers falla
  al faltar campos requeridos. Descargar la plantilla es el camino oficial.

### Archivos tocados

```
csv_upload.py                                              NEW
templates/_csv_upload.html                                 NEW
templates/_csv_upload_resultado.html                       NEW
modules/facturas/views.py                                  + cargar_csv route + FACTURAS_CSV_COLS
modules/facturas/templates/facturas/cargar_csv.html        NEW (extends base, includes partial)
modules/facturas/templates/facturas/cargar_csv_resultado.html NEW
modules/facturas/templates/facturas/lista.html             + botón "Cargar CSV" inline
modules/cheques/views.py                                   + cargar_csv route + CHEQUES_CSV_COLS
modules/cheques/templates/cheques/cargar_csv.html          NEW
modules/cheques/templates/cheques/cargar_csv_resultado.html NEW
modules/cheques/templates/cheques/lista.html               + botón "Cargar CSV"
modules/compras/views.py                                   + cargar_csv route + COMPRAS_CSV_COLS
modules/compras/templates/compras/cargar_csv.html          NEW
modules/compras/templates/compras/cargar_csv_resultado.html NEW
modules/compras/templates/compras/lista.html               + botón "Cargar CSV"
tests/test_csv_upload.py                                   NEW — 27 tests
```

### Cómo extender a otro módulo

Patrón de 5 pasos para agregar upload CSV a `retenciones`, `caja`, etc:

1. Definir `<MODULO>_CSV_COLS` en `<modulo>/views.py` — lista de
   `(campo, header, required)`.
2. Agregar ruta `GET/POST /<modulo>/cargar-csv` que llama `procesar_csv(raw,
   cols, queries.crear, usuario=g.user.username)`.
3. Agregar ruta con `?plantilla=1` que devuelve `plantilla_csv(cols)` con
   BOM + Content-Disposition attachment.
4. Crear `templates/<modulo>/cargar_csv.html` y `cargar_csv_resultado.html`
   que incluyen los partials genéricos.
5. Agregar botón "Cargar CSV" + link "↓ plantilla" en la lista.
