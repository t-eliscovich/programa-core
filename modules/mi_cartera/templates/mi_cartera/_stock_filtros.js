/* Los filtros del Stock por tela — los usan el portal de vendedores
   (/mi-cartera/stock) y la oficina (/stock/por-tela, 15/10/2026). Mismos ids
   y data-* en las dos pantallas: st-q, st-cat, st-tela, st-cal, st-lista,
   st-sub, st-vacio, st-eco.

   ⭐ Cada .st-col es UNA LÍNEA: color × calidad × acabado (15/10/2026, "como
   en la competencia"). data-col agrupa las líneas del mismo color para que
   ordenar por kilos no las separe. Filtro por CATEGORÍA y después por
   SUBCATEGORÍA (la tela): la segunda lista sólo ofrece las de la primera. */
/* Los filtros corren en el navegador: las ~3.700 filas ya están en la página.

   El buscador es por PALABRAS y sin tildes (13/10/2026, "que sea fácil"):
   "jersey negro" trae el negro de cada Jersey, "poliéster" encuentra
   Poliester. Cada palabra tiene que aparecer en la tela, el grupo, el color o
   el código del color. Las telas que se abren solas por una búsqueda se
   vuelven a cerrar al borrarla, y la lista se ordena por los kilos que se ven. */
(function () {
  var input = document.getElementById('st-q');
  var lista = document.getElementById('st-lista');
  if (!input || !lista) { return; }
  var catSel = document.getElementById('st-cat');
  var telaSel = document.getElementById('st-tela');
  var telaOpts = Array.prototype.slice.call(telaSel.options).slice(1);
  var sub = document.getElementById('st-sub');
  var vacio = document.getElementById('st-vacio');
  var eco = document.getElementById('st-eco');
  var telas = Array.prototype.slice.call(lista.querySelectorAll('.st-tela'));
  var cal = 'todo', calOrden = 'todo', ordenTelas = telas.slice();

  function n0(n) { return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '.'); }
  function norm(s) { return (s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, ''); }
  function todas(pals, txt) { for (var i = 0; i < pals.length; i++) { if (txt.indexOf(pals[i]) === -1) { return false; } } return true; }

  telas.forEach(function (t) {
    t._txt = norm(t.getAttribute('data-buscar'));
    t._cols = Array.prototype.slice.call(t.querySelectorAll('.st-col'));
    t._cols.forEach(function (c, i) { c._i = i; });
    t._cols.forEach(function (c) { c._txt = t._txt + ' ' + norm(c.getAttribute('data-buscar')); });
    /* Si la abre o cierra la persona, ya no es "de la búsqueda": no se la
       cierra sola. (El evento toggle no sirve: llega tarde, también cuando
       la abre el código.) */
    t.querySelector('summary').addEventListener('click', function () { t._auto = false; });
  });

  function aplicar() {
    var pals = norm(input.value).split(/\s+/).filter(Boolean);
    var g = catSel.value, tl = telaSel.value;
    var vis = 0, kgTot = 0, rolTot = 0;
    telas.forEach(function (t) {
      var okGrupo = (!g || t.getAttribute('data-grupo') === g) &&
                    (!tl || t.getAttribute('data-tela') === tl);
      var porTela = todas(pals, t._txt);
      var kg = 0, nCol = 0;
      t._cols.forEach(function (c) {
        var seg = parseFloat(c.getAttribute('data-seg')), tot = parseFloat(c.getAttribute('data-kg'));
        var k = cal === 'seg' ? seg : (cal === 'pri' ? tot - seg : tot);
        c._kc = k;  // para ordenar: los kilos de esta calidad, se vea o no
        var ok = okGrupo && k > 0.05 && (porTela || todas(pals, c._txt));
        c.hidden = !ok;
        c._k = ok ? k : 0;
        if (ok) {
          nCol++; kg += k;
          var tk = n0(k); if (c._tk !== tk) { c.querySelector('.kg-c').textContent = tk; c._tk = tk; }
          var rT = parseInt(c.getAttribute('data-rollos'), 10), rS = parseInt(c.getAttribute('data-seg-rollos'), 10);
          var r = cal === 'seg' ? rS : (cal === 'pri' ? rT - rS : rT);
          rolTot += r;
          if (c._r !== r) { c.querySelector('.rol-c').textContent = r + ' r'; c._r = r; }
        }
      });
      t._kg = kg;
      t.hidden = nCol === 0;
      if (nCol) {
        vis++; kgTot += kg;
        t.querySelector('.kg-t').textContent = n0(kg);
      }
      /* Se abre sola si la búsqueda la encontró por un COLOR (si la encontró
         por el nombre de la tela, mostrar 200 colores no ayuda); se cierra
         sola al borrar, pero sólo si la había abierto la búsqueda. */
      var abrir = nCol > 0 && pals.length > 0 && !porTela;
      if (abrir && !t.open) { t.open = true; t._auto = true; }
      else if (!abrir && t._auto) { t.open = false; t._auto = false; }
    });
    /* Reordenar el DOM es lo caro: los colores sólo cambian de orden con
       Todo/PRI/SEG (buscar no los mueve entre sí), y las telas sólo si el
       orden nuevo es distinto del que ya está. */
    if (cal !== calOrden) {
      telas.forEach(function (t) {
        /* Por los kilos del COLOR (sus líneas suman), y adentro del color
           el orden del servidor: PRI antes que SEG. */
        var porCol = {};
        t._cols.forEach(function (c) { var k = c.getAttribute('data-col'); porCol[k] = (porCol[k] || 0) + c._kc; });
        t._cols.slice().sort(function (a, b) {
          var ka = a.getAttribute('data-col'), kb = b.getAttribute('data-col');
          return (porCol[kb] - porCol[ka]) || (ka < kb ? -1 : ka > kb ? 1 : a._i - b._i);
        }).forEach(function (c) { t.appendChild(c); });
      });
      calOrden = cal;
    }
    var nuevo = telas.slice().sort(function (a, b) { return b._kg - a._kg; });
    for (var i = 0; i < nuevo.length; i++) {
      if (nuevo[i] !== ordenTelas[i]) { nuevo.forEach(function (t) { lista.appendChild(t); }); break; }
    }
    ordenTelas = nuevo;
    sub.textContent = n0(kgTot) + ' kg · ' + n0(rolTot) + ' rollos' + (cal === 'seg' ? ' de segunda' : (cal === 'pri' ? ' de primera' : ''));
    lista.hidden = vis === 0;
    vacio.hidden = vis !== 0;
    /* El cartel dice POR QUÉ no hay nada: con un grupo o "SEG" puestos, la
       búsqueda vacía no es la causa y "No encontré nada con “”" confunde. */
    var q = input.value.trim(), donde = [];
    if (tl) { donde.push('en ' + tl); } else if (g) { donde.push('en ' + g); }
    if (cal !== 'todo') { donde.push('de ' + (cal === 'seg' ? 'segunda' : 'primera')); }
    eco.textContent = (q ? 'No encontré “' + q + '”' : 'No hay stock') + (donde.length ? ' ' + donde.join(' ') : '') + '.';
  }
  /* Espera a que deje de tipear (120 ms): en un celular lento filtrar
     3.700 renglones por cada letra se siente. */
  var espera;
  input.addEventListener('input', function () { clearTimeout(espera); espera = setTimeout(aplicar, 120); });
  catSel.addEventListener('change', function () {
    /* Se rearma la lista en vez de esconder opciones: Safari del iPhone no
       respeta `hidden` en un <option>. */
    var g = catSel.value, antes = telaSel.value, sigue = false;
    telaOpts.forEach(function (o) { if (o.parentNode) { telaSel.removeChild(o); } });
    telaOpts.forEach(function (o) {
      if (!g || o.getAttribute('data-cat') === g) { telaSel.appendChild(o); if (o.value === antes) { sigue = true; } }
    });
    telaSel.value = sigue ? antes : '';
    aplicar();
  });
  telaSel.addEventListener('change', aplicar);
  document.querySelectorAll('#st-cal a').forEach(function (a) {
    a.addEventListener('click', function (ev) {
      ev.preventDefault();
      document.querySelectorAll('#st-cal a').forEach(function (x) { x.classList.remove('on'); });
      a.classList.add('on'); cal = a.getAttribute('data-cal'); aplicar();
    });
  });
  // Llega buscando algo (desde Saldos, cuando allá no hay: "fleece negro").
  var pedido = new URLSearchParams(location.search).get('q');
  if (pedido) { input.value = pedido; aplicar(); }
})();
