# Addendum para `skills/programa-core/SKILL.md` — batch 14

> Pegar al SKILL.md cuando haya acceso al host (después de batch 13).

## Sesión 2026-04-17 — batch 14 (UX quick wins para la contadora)

Batch de UX disparado por un análisis en primera persona de los roles que
usan el app — la contadora, el dueño, cobranzas. Los items shippeados son los
de mayor impacto por hora que no requerían cambio de arquitectura.

### Lo que shippó

**1. Fecha texto `DD/MM/YYYY` con auto-formateo**:
- `<input type="date">` (browser-native picker) → `<input type="text">` con
  JS que auto-inserta barras mientras tipeás.
- La contadora teclea `17042026` y aparece `17/04/2026`.
- Pegás ISO `2026-04-17` y lo convierte solo.
- Default del form ya viene `17/04/2026` — no más `2026-04-17` en el input.
- Aplicado en `facturas/nueva`, `cheques/nuevo`, `compras/nueva` (fecha +
  vencimiento/fechad).
- Validación `onblur` con `setCustomValidity()` — se rechaza si no parsea.

**2. Autocomplete de clientes/proveedores con `<datalist>`**:
- Módulo nuevo `modules/autocomplete/queries.py` con
  `clientes_para_datalist()` y `proveedores_para_datalist()` que devuelven
  hasta 2000 (codigo, nombre) ordenados por código.
- Los forms de alta tienen `<datalist id="clientes-list">` + input con
  `list="clientes-list"`. Al tipear "JT" el browser sugiere.
- Sin JS custom, puro HTML5. Case-insensitive by browser.

**3. Autofocus en primer campo de cada form nuevo**:
- `facturas/nueva`, `cheques/nuevo`, `compras/nueva` abren con el cursor
  ya en el campo fecha. No hay que clickear antes de tipear.

**4. Búsqueda global con `Ctrl+K` / `Cmd+K` / `/`**:
- Nuevo `static/app.js` con un overlay modal.
- Atajos dentro del input:
  - `c: texto` → estado de cuenta del cliente (ILIKE)
  - `f: 1234` → facturas filtradas por numf
  - `p: texto` → proveedores
  - `ch: 1234` → cheques
  - sin prefijo → estado de cuenta del cliente (fallback)
- Botón "🔍 Buscar" visible en el header de desktop para discoverability.
- `/` también abre la búsqueda si no estás dentro de un input.

**5. Sidebar mobile con hamburguesa**:
- En pantallas <768px el sidebar sale de pantalla (fixed + translate -100%).
- Botón hamburguesa en el header lo slidea in (con `is-open` class).
- Click fuera o navegación cierra.
- Agregado en `templates/base.html` con CSS inline (no requiere Tailwind JIT).

### Archivos tocados

```
static/app.js                                                NEW — fecha autoformat + Ctrl+K + sidebar mobile
templates/_inputs.html                                       NEW — macros fecha_input, monto_input, codigo_input, datalist_clientes, datalist_proveedores
templates/base.html                                          + <script app.js>, + mobile sidebar CSS, + hamburguesa, + botón "🔍 Buscar"
modules/autocomplete/__init__.py                             NEW
modules/autocomplete/queries.py                              NEW — clientes_para_datalist, proveedores_para_datalist
modules/facturas/views.py                                    + datalist de clientes, fecha DD/MM/YYYY default, pass en error re-renders
modules/facturas/templates/facturas/nueva.html               + autofocus, list="clientes-list", <datalist>, fecha texto con parser
modules/cheques/views.py                                     + datalist de clientes, fecha DD/MM/YYYY default
modules/cheques/templates/cheques/nuevo.html                 + autofocus, datalist, fecha texto con parser
modules/compras/views.py                                     + datalist de proveedores, fecha DD/MM/YYYY default
modules/compras/templates/compras/nueva.html                 + autofocus, datalist, fecha texto con parser
```

### Métricas al cierre

- `pytest -q` → **321 passed, 9 skipped** (sin cambios — no agregamos tests
  nuevos porque los templates los mira el smoke).
- `python tests/test_routes_smoke.py` → **56 GET routes walked, 0 failures**.
- `ruff check` en archivos nuevos → clean.

### Invariantes nuevos

- **Nunca `<input type="date">`**. La contadora viene del dBase y tipea sin
  separadores. Usar `<input type="text">` con `oninput="fechaAutoFormat(this)"`
  y `onblur="fechaValidar(this)"` (ambas en `static/app.js`). Default del form
  viene en formato `DD/MM/YYYY`.
- **`parsers.py::parse_date()` acepta ambos formatos** (`DD/MM/YYYY` e ISO).
  No hace falta cambiar los POST handlers — ya eran tolerantes.
- **Forms largos tienen autofocus** en el primer campo visible (típicamente
  la fecha). La contadora pega al teclado, sin clicks previos.
- **Listas de códigos (cliente/proveedor) expuestas via `<datalist>`** en los
  forms de alta. Con ~500-2000 items la performance es aceptable — si supera
  2000 habría que cambiar a autocomplete async (típeo → XHR).
- **`Ctrl+K` / `Cmd+K` es el atajo canónico de búsqueda global**. "/"
  también funciona pero sólo fuera de inputs.

### Cómo extender a otros forms

Para un form nuevo de alta (por ej. `/retenciones/emitir`):

1. Import en la view:
   ```python
   from modules.autocomplete.queries import clientes_para_datalist
   clientes_datalist = clientes_para_datalist()
   ```
2. Pasar `clientes_datalist=clientes_datalist` en TODOS los `render_template(...)`
   (GET inicial + re-renders de error).
3. En el template, input de fecha:
   ```html
   <input type="text" name="fecha" required
          placeholder="DD/MM/AAAA" inputmode="numeric" maxlength="10"
          autofocus autocomplete="off"
          oninput="fechaAutoFormat(this)" onblur="fechaValidar(this)"
          class="... font-mono">
   ```
4. Input de código cliente:
   ```html
   <input type="text" name="codigo_cli" required list="clientes-list"
          oninput="this.value=this.value.toUpperCase()" class="... uppercase">
   {% if clientes_datalist %}
     <datalist id="clientes-list">
       {% for c in clientes_datalist %}
         <option value="{{ c.codigo_cli }}">{{ c.nombre }}</option>
       {% endfor %}
     </datalist>
   {% endif %}
   ```
5. Fecha default en DD/MM/YYYY: `form["fecha"] = datetime.now().date().strftime("%d/%m/%Y")`
   (NO `.isoformat()`).

### Items UX que quedan (pasan a batch 15)

- Errores técnicos (`ValueError: X`) → español contable.
- Dashboard "modo Dueño" compacto con 4 números gigantes.
- Undo 2-step en acciones destructivas (anular/reversar).
- Recientes por usuario en sidebar.
