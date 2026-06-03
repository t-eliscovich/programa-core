/* sortable-tables.js — TMT 2026-05-16
 *
 * Hace clickeables los <th> de cualquier tabla con class="sortable".
 * Auto-detecta tipo de datos por columna (number / date / text). Toggle
 * de ASC / DESC / sin orden con flecha visual ↑ ↓ ↕ en el header.
 *
 * Sortea las filas DENTRO de cada <tbody> — perfecto para listados
 * estándar de la app (cheques, facturas, clientes, caja, bancos, compras).
 * Para tablas con MÚLTIPLES <tbody> agrupados (como historial con sus
 * batch-cards), sortea CADA tbody internamente — el agrupamiento se
 * preserva.
 *
 * Cómo usar:
 *   1. Agregar class="sortable" al <table>.
 *   2. Para columnas que NO deben sortearse: <th data-no-sort>...
 *   3. Para sobreescribir auto-detect: <th data-sort-type="number">
 *   4. Para usar un valor distinto al texto visible: <td data-sort-value="123.45">$ 123,45</td>
 *
 * Tipos soportados:
 *   - number: "$ 1.234,56", "1.234,56", "-37.64", "1234"
 *   - date:   "16/05/2026"  (dd/mm/yyyy)
 *   - text:   cualquier otra cosa (case-insensitive comparison)
 *
 * Caveat: las columnas "Acum." (corrido por fecha) pierden sentido al
 * sortear por otra cosa. El header de Acum suele tener data-no-sort para
 * no engañar. Las filas con class="no-sort" se quedan al final
 * (típicamente tfoot/totals).
 */
(function () {
  'use strict';

  function parseNumber(raw) {
    if (raw == null) return NaN;
    let s = String(raw).trim();
    if (s === '' || s === '—' || s === '-') return NaN;
    // Quitar símbolos comunes y espacios
    s = s.replace(/\s+/g, '').replace(/\$/g, '').replace(/^[+]/, '');
    // Formato es: 1.234,56 → 1234.56  ó  1234.56 → 1234.56
    // Heurística: si tiene coma como último separador decimal, formato es.
    const hasComma = s.indexOf(',') >= 0;
    const hasDot = s.indexOf('.') >= 0;
    if (hasComma && hasDot) {
      // Asumir formato es: "1.234,56"
      s = s.replace(/\./g, '').replace(',', '.');
    } else if (hasComma && !hasDot) {
      // "1234,56" → "1234.56"
      s = s.replace(',', '.');
    }
    // Si solo tiene puntos, podría ser "1.234" (es, sin decimales) o
    // "1234.56" (en). Si hay >1 punto o el último grupo es de 3 dígitos
    // exactos, tratar como separador de miles.
    const dots = (s.match(/\./g) || []).length;
    if (dots > 1) {
      s = s.replace(/\./g, '');
    } else if (dots === 1) {
      const after = s.split('.')[1];
      if (after && after.length === 3 && !/^\d{1,2}$/.test(after)) {
        s = s.replace('.', '');
      }
    }
    const n = parseFloat(s);
    return isFinite(n) ? n : NaN;
  }

  function parseDate(raw) {
    if (!raw) return NaN;
    const s = String(raw).trim();
    // dd/mm/yyyy
    let m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
    if (m) {
      return new Date(
        Number(m[3]), Number(m[2]) - 1, Number(m[1])
      ).getTime();
    }
    // yyyy-mm-dd
    m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
    if (m) {
      return new Date(
        Number(m[1]), Number(m[2]) - 1, Number(m[3])
      ).getTime();
    }
    return NaN;
  }

  function detectType(value) {
    if (value == null || value === '' || value === '—') return null;
    if (!isNaN(parseDate(value))) return 'date';
    if (!isNaN(parseNumber(value))) return 'number';
    return 'text';
  }

  function detectColumnType(tbodies, colIdx) {
    // Muestreamos hasta 5 celdas con contenido para decidir.
    let n = 0;
    const counts = { number: 0, date: 0, text: 0 };
    for (const tb of tbodies) {
      for (const row of tb.rows) {
        if (row.classList.contains('no-sort')) continue;
        const cell = row.cells[colIdx];
        if (!cell) continue;
        // data-sort-value gana — si existe, asumimos tipo de su contenido.
        const explicit = cell.getAttribute('data-sort-value');
        const t = detectType(explicit != null ? explicit : cell.textContent);
        if (t) { counts[t]++; n++; }
        if (n >= 5) break;
      }
      if (n >= 5) break;
    }
    if (counts.date >= counts.number && counts.date >= counts.text) return 'date';
    if (counts.number >= counts.text) return 'number';
    return 'text';
  }

  function cellSortValue(td, type) {
    if (!td) return type === 'text' ? '' : NaN;
    const explicit = td.getAttribute('data-sort-value');
    const raw = explicit != null ? explicit : td.textContent;
    if (type === 'number') return parseNumber(raw);
    if (type === 'date')   return parseDate(raw);
    return (raw || '').trim().toLowerCase();
  }

  function compareValues(a, b, type) {
    // NaN/'' al final siempre.
    const aEmpty = (type === 'text') ? a === '' : isNaN(a);
    const bEmpty = (type === 'text') ? b === '' : isNaN(b);
    if (aEmpty && bEmpty) return 0;
    if (aEmpty) return 1;
    if (bEmpty) return -1;
    if (a < b) return -1;
    if (a > b) return 1;
    return 0;
  }

  function setIcon(th, state) {
    let icon = th.querySelector('.sort-icon');
    if (!icon) {
      icon = document.createElement('span');
      icon.className = 'sort-icon ml-1 text-slate-400 text-[10px]';
      th.appendChild(icon);
    }
    if (state === 'asc')       icon.textContent = '↑';
    else if (state === 'desc') icon.textContent = '↓';
    else                       icon.textContent = '↕';
  }

  function sortTable(table, colIdx, dir) {
    const ths = table.tHead ? table.tHead.rows[0].cells : [];
    const type = ths[colIdx] && ths[colIdx].dataset.sortType
      ? ths[colIdx].dataset.sortType
      : detectColumnType(table.tBodies, colIdx);

    for (const tb of table.tBodies) {
      const rows = Array.from(tb.rows);
      const sortable = rows.filter(r => !r.classList.contains('no-sort'));
      const fixed    = rows.filter(r =>  r.classList.contains('no-sort'));
      sortable.sort((a, b) => {
        const va = cellSortValue(a.cells[colIdx], type);
        const vb = cellSortValue(b.cells[colIdx], type);
        const cmp = compareValues(va, vb, type);
        return dir === 'asc' ? cmp : -cmp;
      });
      // Re-inyectar en orden: sortable primero, fixed (totales/footer) al final.
      const frag = document.createDocumentFragment();
      sortable.forEach(r => frag.appendChild(r));
      fixed.forEach(r => frag.appendChild(r));
      tb.appendChild(frag);
    }

    // Update icons
    Array.from(ths).forEach((th, i) => {
      if (th.hasAttribute('data-no-sort')) return;
      if (i === colIdx) {
        th.dataset.sortDir = dir;
        setIcon(th, dir);
      } else {
        delete th.dataset.sortDir;
        setIcon(th, null);
      }
    });
    // Track sort activo en el table para el dim de Acum (ver applyAcumDim).
    table.dataset.activeSortIdx = String(colIdx);
    table.dataset.activeSortDir = dir;
    applyAcumDim(table);
  }

  // ── Dim/blur de la columna Acum cuando NO se ordena por la columna que
  // la "drivea". Acum se precomputa con SUM(importe) OVER (ORDER BY fecha
  // ASC) — si la usuaria sortea por Importe o por Cliente, los valores
  // ya no son coherentes con el orden visible. Los dejamos visibles pero
  // grisados con tooltip que explica.
  //
  // Convención del HTML:
  //   <th class="acum-col" ...>Acum.</th>          ← columna a dimear
  //   <th data-acum-driver ...>Deposita</th>        ← columna que la genera
  //
  // Si el sort activo coincide con el driver, sin dim. Cualquier otra
  // columna o sin sort = dim (al cargar la página tampoco hay sort: el
  // backend devuelve fecha DESC y el Acum corre desde fecha ASC, así que
  // los números RENDERIZADOS técnicamente están en orden inverso al que
  // Acum implica — pero ese es el comportamiento histórico aceptado, no
  // lo dimeamos al load).
  function applyAcumDim(table) {
    const ths = table.tHead ? table.tHead.rows[0].cells : [];
    let acumIdx = -1, driverIdx = -1;
    for (let i = 0; i < ths.length; i++) {
      if (ths[i].classList.contains('acum-col'))  acumIdx = i;
      if (ths[i].hasAttribute('data-acum-driver')) driverIdx = i;
    }
    if (acumIdx < 0) return;
    const activeIdx = parseInt(table.dataset.activeSortIdx, 10);
    const hasSort = !isNaN(activeIdx);
    // Sin sort = orden default = valid (acepta convención histórica).
    // Sort en driver = valid. Sort en cualquier otra cosa = dim.
    const valid = !hasSort || activeIdx === driverIdx;
    const dim = !valid;

    const acumTh = ths[acumIdx];
    if (dim) {
      acumTh.classList.add('opacity-40', 'italic');
      if (!acumTh.dataset.origTitle) {
        acumTh.dataset.origTitle = acumTh.getAttribute('title') || '';
      }
      acumTh.setAttribute('title',
        'Acum. fuera de orden — el corrido se calcula por fecha. ' +
        'Para verlo bien, ordená por la columna de fecha (la del driver).');
    } else {
      acumTh.classList.remove('opacity-40', 'italic');
      if (acumTh.dataset.origTitle != null) {
        acumTh.setAttribute('title', acumTh.dataset.origTitle);
      }
    }
    for (const tb of table.tBodies) {
      for (const row of tb.rows) {
        const cell = row.cells[acumIdx];
        if (!cell) continue;
        cell.classList.toggle('opacity-40', dim);
        cell.classList.toggle('italic', dim);
      }
    }
  }

  function makeSortable(table) {
    if (!table.tHead) return;
    const ths = table.tHead.rows[0] ? table.tHead.rows[0].cells : [];
    Array.from(ths).forEach((th, i) => {
      if (th.hasAttribute('data-no-sort')) return;
      th.classList.add('cursor-pointer', 'select-none', 'hover:bg-slate-100');
      th.setAttribute('title',
        (th.getAttribute('title') || '') +
        (th.getAttribute('title') ? ' · ' : '') +
        'Click para ordenar (toggle ↑ / ↓)');
      setIcon(th, null);
      th.addEventListener('click', (e) => {
        // Evitar conflicto si el click viene de un <a>/<button>/<input>
        if (e.target.closest('a, button, input, label, select')) return;
        const cur = th.dataset.sortDir;
        const next = cur === 'asc' ? 'desc' : 'asc';
        sortTable(table, i, next);
      });
    });
  }

  function init() {
    // TMT 2026-06-03 dueña: 'agrega js sort en todos lados'. Auto-aplica a
    // toda tabla con thead+tbody+ ≥2 filas, sin requerir class="sortable"
    // explícitamente. Para opt-out, agregar data-no-sort al <table>.
    document.querySelectorAll('table').forEach(t => {
      if (t.dataset.noSort != null) return;  // opt-out explícito
      if (t.classList.contains('sortable')) return makeSortable(t);  // backcompat
      if (!t.tHead || !t.tBodies || t.tBodies.length === 0) return;
      let rowCount = 0;
      for (const tb of t.tBodies) rowCount += tb.rows.length;
      if (rowCount < 2) return;  // tablas de 1 fila no necesitan sort
      makeSortable(t);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
