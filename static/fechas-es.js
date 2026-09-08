/* Fechas en dd/mm/aaaa en TODOS los <input type="date"> de la app.
 *
 * Dueña, tres veces (19/05, 08/07 y 08/09/2026: "fecha está en USA fecha…
 * pero en el filtro sigue estando mal"): el selector de fecha nativo de Chrome
 * muestra mm/dd/aaaa porque el formato lo decide el IDIOMA del navegador, no
 * la página, y no hay atributo que lo cambie. Las dos veces anteriores se
 * arregló UN input a mano (cheques/lista.html, flujo_grafico.html); esta vez
 * se arreglan los ~100 de una sola vez, acá.
 *
 * Cómo: cada <input type="date"> se ESCONDE (sigue en el form con su `name`,
 * su `value` ISO y sus eventos, así ningún servidor ni JS cambia) y delante se
 * pone un cuadro de texto dd/mm/aaaa más un botoncito que abre el calendario
 * nativo (showPicker). Escribir en el texto actualiza el escondido y le
 * dispara `input`/`change`; elegir en el calendario actualiza el texto.
 *
 * Para dejar un input como estaba: data-nativo.
 */
(function fechasEs() {
  'use strict';

  var ICONO = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" ' +
    'height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" ' +
    'y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>';

  function aEs(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
    return m ? m[3] + '/' + m[2] + '/' + m[1] : '';
  }

  function aIso(texto) {
    var t = (texto || '').trim();
    var m = /^(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2}|\d{4})$/.exec(t) ||
            (/^\d{8}$/.test(t) ? [t, t.slice(0, 2), t.slice(2, 4), t.slice(4)] : null);
    if (!m) return null;
    var d = parseInt(m[1], 10), mo = parseInt(m[2], 10), a = parseInt(m[3], 10);
    if (m[3].length === 2) a += 2000;
    if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
    var f = new Date(Date.UTC(a, mo - 1, d));
    if (f.getUTCMonth() !== mo - 1 || f.getUTCDate() !== d) return null;
    return a + '-' + (mo < 10 ? '0' : '') + mo + '-' + (d < 10 ? '0' : '') + d;
  }

  function disparar(el, tipo) {
    el.dispatchEvent(new Event(tipo, { bubbles: true }));
  }

  function vestir(orig) {
    if (orig.dataset.fechasEs || orig.hasAttribute('data-nativo')) return;
    orig.dataset.fechasEs = '1';

    var texto = document.createElement('input');
    texto.type = 'text';
    texto.className = orig.className;
    if (orig.getAttribute('style')) texto.setAttribute('style', orig.getAttribute('style'));
    texto.placeholder = 'dd/mm/aaaa';
    texto.inputMode = 'numeric';
    texto.autocomplete = 'off';
    texto.value = aEs(orig.value);
    // El mismo ancho que tenía el nativo (un input de texto sale más ancho).
    if (orig.offsetWidth > 0) texto.style.width = orig.offsetWidth + 'px';
    if (orig.title) texto.title = orig.title;
    if (orig.disabled) texto.disabled = true;
    if (orig.readOnly) texto.readOnly = true;
    if (orig.required) { texto.required = true; orig.required = false; }
    if (orig.id) {
      // El <label for=…> tiene que seguir apuntando al cuadro que se ve.
      var lab = document.querySelector('label[for="' + orig.id + '"]');
      texto.id = orig.id + '-es';
      if (lab) lab.setAttribute('for', texto.id);
    }
    // El nativo sigue en el form (name + value ISO) pero no se ve ni ocupa.
    orig.tabIndex = -1;
    orig.setAttribute('aria-hidden', 'true');
    orig.style.position = 'absolute';
    orig.style.opacity = '0';
    orig.style.width = '0';
    orig.style.height = '0';
    orig.style.padding = '0';
    orig.style.border = '0';
    orig.style.pointerEvents = 'none';

    var caja = document.createElement('span');
    caja.className = 'fecha-es';
    caja.style.cssText = 'position:relative;display:inline-flex;align-items:center;';

    var boton = null;
    if (typeof orig.showPicker === 'function' && !orig.disabled && !orig.readOnly) {
      boton = document.createElement('button');
      boton.type = 'button';
      boton.tabIndex = -1;
      boton.title = 'Elegir en el calendario';
      boton.innerHTML = ICONO;
      boton.style.cssText = 'position:absolute;right:4px;top:0;bottom:0;margin:auto;' +
        'height:20px;width:20px;display:inline-flex;align-items:center;justify-content:center;' +
        'border:0;background:transparent;color:#64748b;cursor:pointer;padding:0;line-height:0;';
      boton.addEventListener('click', function () {
        try { orig.showPicker(); } catch (e) { /* sin gesto o sin soporte */ }
      });
      texto.style.paddingRight = '26px';
    }

    orig.parentNode.insertBefore(caja, orig);
    caja.appendChild(texto);
    caja.appendChild(orig);
    if (boton) caja.appendChild(boton);

    function llevarAlNativo(tipo) {
      var t = texto.value.trim();
      if (t === '') {
        texto.setCustomValidity('');
        if (orig.value !== '') { orig.value = ''; disparar(orig, 'input'); if (tipo === 'change') disparar(orig, 'change'); }
        return;
      }
      var iso = aIso(t);
      if (!iso) {
        // Se avisa al salir del campo, no en cada tecla.
        if (tipo === 'change') texto.setCustomValidity('La fecha va día/mes/año: dd/mm/aaaa');
        return;
      }
      texto.setCustomValidity('');
      if (tipo === 'change') texto.value = aEs(iso);
      if (orig.value !== iso) {
        orig.value = iso;
        disparar(orig, 'input');
        if (tipo === 'change') disparar(orig, 'change');
      } else if (tipo === 'change') {
        disparar(orig, 'change');
      }
    }
    texto.addEventListener('input', function () { llevarAlNativo('input'); });
    texto.addEventListener('change', function () { llevarAlNativo('change'); });
    texto.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && texto.form && aIso(texto.value) === null && texto.value.trim() !== '') {
        ev.preventDefault(); llevarAlNativo('change'); texto.reportValidity();
      }
    });
    // Lo que se elige en el calendario (o que otro JS escribe con change).
    orig.addEventListener('input', function () { texto.value = aEs(orig.value); texto.setCustomValidity(''); });
    orig.addEventListener('change', function () { texto.value = aEs(orig.value); texto.setCustomValidity(''); });
    // Un form.reset() vuelve el nativo a su valor inicial; el texto lo sigue.
    if (texto.form) texto.form.addEventListener('reset', function () {
      setTimeout(function () { texto.value = aEs(orig.value); }, 0);
    });
  }

  function vestirTodos(raiz) {
    var lista = (raiz || document).querySelectorAll('input[type="date"]');
    for (var i = 0; i < lista.length; i++) vestir(lista[i]);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { vestirTodos(); });
  } else {
    vestirTodos();
  }
  // Lo que htmx trae después también.
  document.addEventListener('htmx:afterSwap', function (ev) { vestirTodos(ev.target); });
  document.addEventListener('htmx:load', function (ev) { vestirTodos(ev.target); });

  window.fechasEs = { vestir: vestirTodos, aIso: aIso, aEs: aEs };
})();
