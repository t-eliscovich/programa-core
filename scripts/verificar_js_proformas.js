/**
 * Verifica con jsdom el JS de la pantalla de proformas: que el botón Imprimir
 * GRABE antes de abrir la hoja, que la hoja salga con el N° y que un segundo
 * click no cree una proforma nueva.
 *
 * El chromium del sandbox no arranca (falta libxdamage1 y no hay sudo), así
 * que este es el único modo de probar la hoja que arma el JS en un
 * `window.open`. Se corre en dos pasos:
 *
 *     python3 scripts/vista_local.py /pedidos/nuevo -o /tmp/prev_form.html
 *     node scripts/verificar_js_proformas.js
 *
 * Sale con código 1 si algo falla, así que sirve tal cual en un pipeline.
 * TMT 2026-08-11.
 */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('/tmp/prev_form.html', 'utf8');
const vc = new (require('jsdom').VirtualConsole)();  // silenciar ruido de base.html

const dom = new JSDOM(html, {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'https://local/pedidos/nuevo',
  virtualConsole: vc,
});
const w = dom.window, d = w.document;

// ── stubs ──────────────────────────────────────────────────────────────
const posts = [];
w.fetch = (url, opts) => {
  if (opts && opts.method === 'POST') {
    posts.push({url, body: JSON.parse(opts.body), csrf: (opts.headers||{})['X-CSRFToken']});
    return Promise.resolve({ok: true, json: () => Promise.resolve({ok: true, id_proforma: 4242})});
  }
  return Promise.resolve({ok: true, json: () => Promise.resolve({ok: true, nombre: 'CLIENTE X'})});
};
let escrito = '';
w.open = () => ({
  document: {open(){}, write(s){ escrito += s; }, close(){}, getElementById: () => null},
  focus(){}, print(){},
});

// ── cargar una línea como lo haría Alex ────────────────────────────────
function set(el, v) { el.value = v; el.dispatchEvent(new w.Event('input', {bubbles:true})); el.dispatchEvent(new w.Event('change', {bubbles:true})); }
set(d.getElementById('codigo-cli-input'), 'ZQX');
const fila = d.querySelector('#lineas-body tr');
set(fila.querySelector('.js-tipo'), 'JERSEY');
set(fila.querySelector('.js-color'), 'NEGRO');
set(fila.querySelector('.js-piezas'), '100');
set(fila.querySelector('.js-cant'), '2200');
set(fila.querySelector('.js-precio'), '9,12');

const total = d.getElementById('t-total').textContent;

d.getElementById('btn-imprimir').click();

setTimeout(() => {
  const err = [];
  if (posts.length !== 1) err.push(`esperaba 1 POST, hubo ${posts.length}`);
  const p = posts[0] || {};
  if (p.url && !String(p.url).includes('/proformas/guardar')) err.push('POST a otra ruta: ' + p.url);
  if (!p.csrf) err.push('el POST fue sin CSRF');
  const b = p.body || {};
  if (b.codigo_cli !== 'ZQX') err.push('cliente mal: ' + b.codigo_cli);
  if (!b.lineas || b.lineas.length !== 1) err.push('líneas: ' + JSON.stringify(b.lineas));
  else {
    const l = b.lineas[0];
    if (l.tela !== 'jersey') err.push('tela mal: ' + l.tela);
    if (l.cantidad !== 100) err.push('piezas mal: ' + l.cantidad);
    if (l.cantidad_kilos !== 2200) err.push('kg mal: ' + l.cantidad_kilos);
    if (l.precio_unitario !== 9.12) err.push('precio mal: ' + l.precio_unitario);
  }
  if (b.es_pedido !== true) err.push('es_pedido mal');
  if (!escrito.includes('Guardando…')) err.push('la ventana no mostró "Guardando…" mientras tanto');
  if (!escrito.includes('N° 4242')) err.push('la hoja salió SIN el N° de proforma');
  if (!escrito.includes('Factura Proforma')) err.push('la hoja no dice Factura Proforma');
  const estado = d.getElementById('prof-estado').textContent;
  if (!estado.includes('4242')) err.push('la pantalla no muestra el N°: ' + estado);

  // segundo click: manda el id que ya tiene (no crea otra)
  d.getElementById('btn-imprimir').click();
  setTimeout(() => {
    if (posts.length !== 2) err.push('el segundo click no grabó');
    else if (posts[1].body.id_proforma !== 4242) err.push('el segundo POST no manda el N° que ya tenía → duplicaría');
    console.log(err.length ? '❌ ' + err.join('\n❌ ') : `✅ todo OK · total en pantalla ${total} · POSTs: ${posts.length}`);
    process.exit(err.length ? 1 : 0);
  }, 300);
}, 300);
