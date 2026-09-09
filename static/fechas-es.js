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
 * El cuadro de texto se maneja POR SEGMENTOS, como el nativo (dueña
 * 09/09/2026: "el cursor debe seleccionar primero el día, luego el mes, luego
 * el año"): al entrar queda marcado el día; se escriben dos dígitos (o uno
 * que ya alcanza: día 4-9, mes 2-9) y pasa solo al mes, después al año.
 * Flechas ← → cambian de segmento, ↑ ↓ suben y bajan el valor, Backspace
 * vacía el segmento y vuelve al anterior, y "/" también avanza. Al salir, un
 * día sin mes ni año es el día de este mes; un año de dos cifras es 20xx.
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

  // Lo que se puede escribir (Andrés, 09/09/2026: "debo tipear toda la fecha,
  // antes el cursor solo pasaba al día, luego al mes y luego al año"):
  //   15          → el 15 de este mes
  //   15/10, 1510 → el 15 de octubre de este año
  //   15/10/26, 151026, 15102026, 15.10.2026, 15-10-2026 → completo
  // (Eso es lo que se acepta pegado o escrito de corrido; tipeando, el cuadro
  // va por segmentos: ver más abajo.)
  function aIso(texto) {
    var t = (texto || '').trim().replace(/[\/.\-]+$/, '');  // "15/" es "15"
    var hoy = new Date();
    var m = /^(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2}|\d{4})$/.exec(t);
    if (!m) {
      var p = /^(\d{1,2})[\/.\-](\d{1,2})$/.exec(t);
      if (p) m = [t, p[1], p[2], String(hoy.getFullYear())];
    }
    if (!m) {
      var q = /^(\d{1,2})$/.exec(t);
      if (q) m = [t, q[1], String(hoy.getMonth() + 1), String(hoy.getFullYear())];
    }
    if (!m && /^\d+$/.test(t)) {
      var n = t.length;
      if (n === 8) m = [t, t.slice(0, 2), t.slice(2, 4), t.slice(4)];
      else if (n === 7) m = [t, t.slice(0, 1), t.slice(1, 3), t.slice(3)];
      else if (n === 6) m = [t, t.slice(0, 2), t.slice(2, 4), t.slice(4)];
      else if (n === 5) m = [t, t.slice(0, 1), t.slice(1, 3), t.slice(3)];
      else if (n === 4) m = [t, t.slice(0, 2), t.slice(2, 4), String(hoy.getFullYear())];
      else if (n === 3) m = [t, t.slice(0, 1), t.slice(1, 3), String(hoy.getFullYear())];
    }
    if (!m) return null;
    var d = parseInt(m[1], 10), mo = parseInt(m[2], 10), a = parseInt(m[3], 10);
    if (m[3].length === 2) a += 2000;
    if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
    var f = new Date(Date.UTC(a, mo - 1, d));
    if (f.getUTCMonth() !== mo - 1 || f.getUTCDate() !== d) return null;
    return a + '-' + (mo < 10 ? '0' : '') + mo + '-' + (d < 10 ? '0' : '') + d;
  }

  var disparando = false;  // los eventos que YO le mando al nativo
  function disparar(el, tipo) {
    disparando = true;
    try { el.dispatchEvent(new Event(tipo, { bubbles: true })); } finally { disparando = false; }
  }
  // Mientras se escribe, sólo cuenta lo que ya trae AÑO; "15" recién se
  // vuelve "el 15 de este mes" al salir del campo (change).
  function completa(texto) {
    var t = (texto || '').trim();
    return /^\d{1,2}[\/.\-]\d{1,2}[\/.\-](\d{4})$/.test(t) || /^\d{7,8}$/.test(t);
  }

  // Los vestidos VIVOS (con sus listeners). Un WeakSet y no un atributo: un
  // `cloneNode(true)` (el "+ Otro cheque" de Cobranza, y otros cuatro) copia
  // los atributos pero NO los listeners, así que el clon llegaba con el cuadro
  // de texto suelto —escribir ahí no movía nada— y el atributo lo hacía pasar
  // por vestido. Dueña 09/09/2026: la fecha del anticipo "al revés".
  var vivos = new WeakSet();

  function desvestir(orig) {
    // Un clon muerto: sacar el cuadro y el botón viejos y dejar el nativo
    // como estaba, con lo que se le guardó en data-fe-*.
    var caja = orig.parentNode;
    if (!caja || !caja.classList || !caja.classList.contains('fecha-es')) return;
    caja.parentNode.insertBefore(orig, caja);
    caja.parentNode.removeChild(caja);
    if (orig.dataset.feStyle) orig.setAttribute('style', orig.dataset.feStyle);
    else orig.removeAttribute('style');
    if (orig.dataset.feMin) orig.setAttribute('min', orig.dataset.feMin);
    if (orig.dataset.feMax) orig.setAttribute('max', orig.dataset.feMax);
    if (orig.dataset.feReq === '1') orig.required = true;
    orig.removeAttribute('tabindex');
    orig.removeAttribute('aria-hidden');
  }

  // ---- El cuadro por segmentos: día [0,2) · mes [3,5) · año [6,10) --------
  // Mientras el cuadro tiene el foco su valor SIEMPRE tiene la forma
  // dd/mm/aaaa, con "dd", "mm", "aaaa" en lo que falta. Lo que se tipea
  // reemplaza el segmento marcado; no hay cursor libre.
  //   alEscribir(t): cada vez que queda una fecha completa (o vacía) tipeando.
  //   alSalir(t):    al salir del cuadro, con lo que quedó (t = '' si nada).
  // t es texto suelto ("15/9/2026"); aIso(t) lo vuelve ISO o null.
  function segmentar(texto, alEscribir, alSalir) {
    var SEG = [[0, 2, 'dd'], [3, 5, 'mm'], [6, 10, 'aaaa']];
    var seg = 0, buf = '', porMouse = false;

    function partes() {
      var v = texto.value;
      if (/^.{2}\/.{2}\/.{4}$/.test(v)) return [v.slice(0, 2), v.slice(3, 5), v.slice(6, 10)];
      var iso = aIso(v);
      if (iso) { var e = aEs(iso); return [e.slice(0, 2), e.slice(3, 5), e.slice(6)]; }
      return ['dd', 'mm', 'aaaa'];
    }
    function mostrar(p) { texto.value = p[0] + '/' + p[1] + '/' + p[2]; }
    function marcar(i) {
      seg = i;
      try { texto.setSelectionRange(SEG[i][0], SEG[i][1]); } catch (e) { /* sin foco */ }
    }
    function elegir(i) { buf = ''; marcar(i); }
    function vacio(p, i) { return p[i] === SEG[i][2]; }
    // Lo escrito, como texto suelto: '' si no hay nada; con el mes y el año
    // de hoy si sólo se puso el día (Andrés, 09/09: "15" es el 15 de este mes).
    function textoDe(p) {
      if (vacio(p, 0) && vacio(p, 1) && vacio(p, 2)) return '';
      if (vacio(p, 0)) return p.join('/');  // sin día no hay fecha: que avise
      var hoy = new Date();
      var mes = vacio(p, 1) ? String(hoy.getMonth() + 1) : p[1];
      var anio = vacio(p, 2) ? String(hoy.getFullYear()) : p[2];
      if (/^\d{4}$/.test(anio) && parseInt(anio, 10) < 100) anio = String(2000 + parseInt(anio, 10));
      return p[0] + '/' + mes + '/' + anio;
    }
    function sincronizar(tipo) {
      var p = partes();
      var t = textoDe(p);
      if (tipo === 'input') {
        // Mientras se escribe sólo cuenta lo completo (o lo vacío): un año a
        // medio tipear ("0202" camino a 2026) no se avisa.
        var anioListo = /^\d{4}$/.test(p[2]) && parseInt(p[2], 10) >= 1000;
        if (t === '' || !(vacio(p, 1) || vacio(p, 2)) && anioListo) alEscribir(t);
        return;
      }
      alSalir(t);
    }
    function digito(d) {
      var p = partes(), i = seg;
      if (i < 2) {
        var tope = i === 0 ? 31 : 12;
        buf = (buf + d).slice(-2);
        if (parseInt(buf, 10) > tope) buf = d;     // "35" no es un día: empieza de nuevo
        p[i] = ('0' + buf).slice(-2);
        mostrar(p);
        var n = parseInt(buf, 10);
        // Igual que el nativo: un día que empieza en 4..9 o un mes que
        // empieza en 2..9 ya están completos con un dígito.
        var lleno = buf.length === 2 || (i === 0 ? n >= 4 : n >= 2);
        if (lleno) elegir(i + 1); else marcar(i);
      } else {
        buf = (buf + d).slice(-4);
        p[2] = ('0000' + buf).slice(-4);
        mostrar(p);
        marcar(2);
      }
      sincronizar('input');
    }
    function borrar() {
      var p = partes();
      if (vacio(p, seg)) {
        if (seg > 0) elegir(seg - 1);
        return;
      }
      p[seg] = SEG[seg][2];
      mostrar(p);
      elegir(seg);
      sincronizar('input');
    }
    function avanzar() { if (seg < 2) elegir(seg + 1); }
    function retroceder() { if (seg > 0) elegir(seg - 1); }
    function subir(paso) {
      var p = partes(), hoy = new Date();
      var n = vacio(p, seg) ? [hoy.getDate(), hoy.getMonth() + 1, hoy.getFullYear()][seg]
                            : parseInt(p[seg], 10);
      n += paso;
      if (seg === 0) n = n < 1 ? 31 : n > 31 ? 1 : n;
      else if (seg === 1) n = n < 1 ? 12 : n > 12 ? 1 : n;
      p[seg] = seg === 2 ? ('0000' + n).slice(-4) : ('0' + n).slice(-2);
      mostrar(p);
      elegir(seg);
      sincronizar('input');
    }
    function segmentoDelCursor() {
      var c = texto.selectionStart || 0;
      return c <= 2 ? 0 : c <= 5 ? 1 : 2;
    }

    if (texto.disabled || texto.readOnly) return;
    var estabaVacio = false;
    texto.addEventListener('mousedown', function () { porMouse = true; estabaVacio = texto.value.trim() === ''; });
    texto.addEventListener('focus', function () {
      mostrar(partes());
      if (porMouse) return;  // el clic elige el segmento
      // Con Tab, Chrome marca todo el cuadro DESPUÉS del focus: se vuelve a
      // marcar el día un instante más tarde.
      elegir(0);
      setTimeout(function () { if (document.activeElement === texto) elegir(0); }, 0);
    });
    texto.addEventListener('click', function () {
      porMouse = false;
      mostrar(partes());
      // En un cuadro vacío el clic cae al final (después de "aaaa"):
      // igual se empieza por el día.
      elegir(estabaVacio ? 0 : segmentoDelCursor());
      estabaVacio = false;
    });
    // Los dígitos entran por beforeinput y no por keydown: en el celular
    // (Android) keydown viene como "Unidentified" y lo único que trae la
    // tecla es beforeinput.
    texto.addEventListener('beforeinput', function (ev) {
      var tipo = ev.inputType || '';
      if (tipo === 'insertText' && /^\d$/.test(ev.data || '')) { ev.preventDefault(); digito(ev.data); }
      else if (tipo === 'insertText' && /^[\/.\-\s]$/.test(ev.data || '')) { ev.preventDefault(); avanzar(); }
      else if (tipo === 'deleteContentBackward' || tipo === 'deleteContentForward') { ev.preventDefault(); borrar(); }
      else if (tipo === 'insertFromPaste') { /* lo atiende 'paste' */ }
      else if (tipo === 'insertLineBreak' || tipo === 'insertParagraph') { /* Enter: manda el form */ }
      else if (tipo.indexOf('insert') === 0 || tipo.indexOf('delete') === 0) ev.preventDefault();
    });
    texto.addEventListener('paste', function (ev) {
      ev.preventDefault();
      var cb = ev.clipboardData || window.clipboardData;
      var iso = aIso(cb ? cb.getData('text') : '');
      if (!iso) return;
      texto.value = aEs(iso);
      elegir(2);
      sincronizar('input');
    });
    texto.addEventListener('keydown', function (ev) {
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      switch (ev.key) {
        case 'ArrowRight': ev.preventDefault(); avanzar(); break;
        case 'ArrowLeft': ev.preventDefault(); retroceder(); break;
        case 'ArrowUp': ev.preventDefault(); subir(1); break;
        case 'ArrowDown': ev.preventDefault(); subir(-1); break;
        case 'Backspace': case 'Delete': ev.preventDefault(); borrar(); break;
        case 'Enter':
          sincronizar('change');
          if (texto.value.trim() !== '' && aIso(texto.value) === null) { ev.preventDefault(); texto.reportValidity(); }
          break;
        default: break;
      }
    });
    texto.addEventListener('blur', function () { sincronizar('change'); });
  }

  function vestir(orig) {
    if (vivos.has(orig) || orig.hasAttribute('data-nativo')) return;
    vivos.add(orig);
    if (orig.parentNode && orig.parentNode.classList &&
        orig.parentNode.classList.contains('fecha-es')) {
      desvestir(orig);
    }
    // Lo que hay que recordar para poder desvestir un clon.
    orig.dataset.feStyle = orig.getAttribute('style') || '';
    orig.dataset.feMin = orig.getAttribute('min') || '';
    orig.dataset.feMax = orig.getAttribute('max') || '';
    orig.dataset.feReq = orig.required ? '1' : '';
    if (orig.offsetWidth > 0) orig.dataset.feWidth = String(orig.offsetWidth);

    var texto = document.createElement('input');
    texto.type = 'text';
    texto.className = orig.className;
    if (orig.getAttribute('style')) texto.setAttribute('style', orig.getAttribute('style'));
    texto.placeholder = 'dd/mm/aaaa';
    texto.inputMode = 'numeric';
    texto.autocomplete = 'off';
    texto.value = aEs(orig.value);
    // El mismo ancho que tenía el nativo (un input de texto sale más ancho).
    if (orig.dataset.feWidth) texto.style.width = orig.dataset.feWidth + 'px';
    if (orig.title) texto.title = orig.title;
    if (orig.disabled) texto.disabled = true;
    if (orig.readOnly) texto.readOnly = true;
    if (orig.required) { texto.required = true; orig.required = false; }
    // min/max se validan acá, con mensaje en castellano. Si quedaran en el
    // nativo escondido, una fecha fuera de rango lo dejaba inválido y el
    // botón del form no hacía NADA (Chrome no puede enfocar un campo
    // escondido para mostrar el aviso) — dueña 09/09/2026, "no puedo sacar
    // el filtro" en la traza.
    var minIso = orig.getAttribute('min') || '', maxIso = orig.getAttribute('max') || '';
    orig.removeAttribute('min'); orig.removeAttribute('max');
    if (orig.id) {
      // El <label for=…> tiene que seguir apuntando al cuadro que se ve.
      var lab = document.querySelector('label[for="' + orig.id + '"], label[for="' + orig.id + '-es"]');
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

    function llevarAlNativo(tipo, t) {
      t = (t === undefined ? texto.value : t).trim();
      if (t === '') {
        texto.setCustomValidity('');
        if (orig.value !== '') { orig.value = ''; disparar(orig, 'input'); if (tipo === 'change') disparar(orig, 'change'); }
        return;
      }
      if (tipo === 'input' && !completa(t)) return;
      var iso = aIso(t);
      if (!iso) {
        // Se avisa al salir del campo, no en cada tecla.
        if (tipo === 'change') {
          texto.setCustomValidity(/^\d{1,2}[\/.\-]\d{1,2}[\/.\-]\d{4}$/.test(t)
            ? 'Esa fecha no existe: ' + t
            : 'La fecha va día/mes/año: dd/mm/aaaa');
        }
        return;
      }
      if (minIso && iso < minIso) {
        if (tipo === 'change') texto.setCustomValidity('La fecha tiene que ser ' + aEs(minIso) + ' o después');
        return;
      }
      if (maxIso && iso > maxIso) {
        if (tipo === 'change') texto.setCustomValidity('La fecha tiene que ser ' + aEs(maxIso) + ' o antes');
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

    segmentar(texto,
      function (t) { llevarAlNativo('input', t); },
      function (t) { llevarAlNativo('change', t); if (t === '') texto.value = ''; });
    // Lo que entra por otro camino (autocompletar, un JS que escribe el cuadro).
    texto.addEventListener('input', function () { llevarAlNativo('input'); });
    texto.addEventListener('change', function () { llevarAlNativo('change'); });
    // Lo que se elige en el calendario (o que otro JS escribe con change).
    orig.addEventListener('input', function () { if (!disparando) { texto.value = aEs(orig.value); texto.setCustomValidity(''); } });
    orig.addEventListener('change', function () { if (!disparando) { texto.value = aEs(orig.value); texto.setCustomValidity(''); } });
    // Un form.reset() vuelve el nativo a su valor inicial; el texto lo sigue.
    if (texto.form) texto.form.addEventListener('reset', function () {
      setTimeout(function () { texto.value = aEs(orig.value); }, 0);
    });
  }

  // Los cuadros de TEXTO dd/mm/aaaa (Cobranza, Compras, Gastos, Facturas,
  // postergar…: `placeholder="DD/MM/AAAA"`) van por segmentos igual, sobre
  // su propio valor — el form sigue mandando el texto dd/mm/aaaa de siempre.
  // Dueña 09/09/2026: "eso para Cobranza, con día mes año".
  function esTextoFecha(el) {
    return el.type === 'text' && /^dd\/mm\/aaaa$/i.test(el.placeholder || '') &&
      !el.hasAttribute('data-nativo');
  }
  function segmentarTexto(texto) {
    if (vivos.has(texto)) return;
    vivos.add(texto);
    var alEntrar = '';
    texto.addEventListener('focus', function () { alEntrar = texto.value; });
    segmentar(texto, function () { /* nada hasta salir */ }, function (t) {
      var iso = aIso(t);
      if (t === '') { texto.value = ''; texto.setCustomValidity(''); }
      else if (iso) { texto.value = aEs(iso); texto.setCustomValidity(''); }
      else {
        texto.setCustomValidity(/^\d{1,2}[\/.\-]\d{1,2}[\/.\-]\d{4}$/.test(t)
          ? 'Esa fecha no existe: ' + t : 'La fecha va día/mes/año: dd/mm/aaaa');
      }
      // Lo tipeado entró sin eventos nativos: el `change` (el que dispara un
      // form con onchange, como postergar en la lista) se manda a mano.
      if (texto.value !== alEntrar) { alEntrar = texto.value; disparar(texto, 'change'); }
    });
  }

  function vestirTodos(raiz) {
    var lista = (raiz || document).querySelectorAll('input[type="date"], input[type="text"]');
    for (var i = 0; i < lista.length; i++) {
      if (lista[i].type === 'date') vestir(lista[i]);
      else if (esTextoFecha(lista[i])) segmentarTexto(lista[i]);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { vestirTodos(); });
  } else {
    vestirTodos();
  }
  // Lo que htmx trae después también.
  document.addEventListener('htmx:afterSwap', function (ev) { vestirTodos(ev.target); });
  document.addEventListener('htmx:load', function (ev) { vestirTodos(ev.target); });
  // Y lo que cualquier JS agrega después (clones de "+ Otro cheque", filas
  // nuevas): se viste solo, sin que cada pantalla tenga que acordarse.
  if (window.MutationObserver) {
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var agregados = muts[i].addedNodes;
        for (var j = 0; j < agregados.length; j++) {
          var n = agregados[j];
          if (n.nodeType !== 1) continue;
          if (n.matches && n.matches('input')) vestirTodos({ querySelectorAll: function () { return [n]; } });
          else vestirTodos(n);
        }
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
  }

  window.fechasEs = { vestir: vestirTodos, aIso: aIso, aEs: aEs };
})();
