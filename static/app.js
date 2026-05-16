// Programa Core — UX helpers globales.
// Cargado desde templates/base.html via <script defer src="{{ url_for('static', filename='app.js') }}">.

// =====================================================================
// 1. Input de fecha DD/MM/YYYY — auto-inserta barras mientras tipeás.
// =====================================================================

function fechaAutoFormat(el) {
  // Acepta DDMMYYYY, DD/MM/YYYY, o 2026-04-17 (pegado).
  let v = el.value.replace(/[^\d\/\-]/g, '');
  // Si pegaron ISO 2026-04-17, convertimos a 17/04/2026
  const iso = v.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (iso) {
    el.value = iso[3] + '/' + iso[2] + '/' + iso[1];
    return;
  }
  // Limpiar y rearmar con barras automáticas
  const digits = v.replace(/\D/g, '').slice(0, 8);
  let out = digits;
  if (digits.length > 4) {
    out = digits.slice(0, 2) + '/' + digits.slice(2, 4) + '/' + digits.slice(4);
  } else if (digits.length > 2) {
    out = digits.slice(0, 2) + '/' + digits.slice(2);
  }
  el.value = out;
}

function fechaValidar(el) {
  const v = el.value.trim();
  if (!v) return;  // vacío — lo maneja required si corresponde
  const m = v.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
  if (!m) {
    el.setCustomValidity("Formato DD/MM/AAAA");
    return;
  }
  const d = parseInt(m[1], 10), mo = parseInt(m[2], 10), y = parseInt(m[3], 10);
  if (d < 1 || d > 31 || mo < 1 || mo > 12 || y < 1900 || y > 2100) {
    el.setCustomValidity("Fecha inválida");
    return;
  }
  el.setCustomValidity("");  // OK
}

// =====================================================================
// 2. Búsqueda global (Ctrl+K o "/") — abre un overlay para buscar
//    cualquier cosa (cliente, factura, cheque, proveedor) desde cualquier
//    pantalla.
// =====================================================================

(function initGlobalSearch() {
  let overlay = null;

  function ensureOverlay() {
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = '__gs_overlay';
    overlay.innerHTML = `
      <div style="position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:flex-start;justify-content:center;padding-top:10vh;"
           onclick="if(event.target===this)window.__gsClose()">
        <div style="background:white;width:min(600px,90vw);border-radius:8px;box-shadow:0 10px 40px rgba(0,0,0,.3);overflow:hidden;font-family:system-ui">
          <input id="__gs_input" autofocus placeholder="Buscar cliente, factura, cheque, proveedor…  (Esc para cerrar)"
                 style="width:100%;padding:14px 16px;border:0;outline:0;font-size:15px;border-bottom:1px solid #e5e7eb"/>
          <div id="__gs_tips" style="padding:10px 16px;font-size:12px;color:#6b7280;line-height:1.6">
            <div><b>Atajos:</b></div>
            <div><code>c:</code> seguido de texto → buscar cliente por nombre o código</div>
            <div><code>f:</code> seguido de número → abrir factura por numf</div>
            <div><code>p:</code> seguido de texto → buscar proveedor</div>
            <div><code>ch:</code> seguido de número → buscar cheque por n°</div>
            <div>Sin prefijo → va al estado de cuenta del cliente</div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#__gs_input');
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { window.__gsClose(); return; }
      if (e.key === 'Enter') {
        const v = input.value.trim();
        if (!v) return;
        window.__gsGo(v);
      }
    });
    window.__gsClose = () => { overlay.style.display = 'none'; };
    window.__gsGo = (q) => {
      const parts = q.split(':').map(s => s.trim());
      let url;
      if (parts.length === 2) {
        const [kind, rest] = parts;
        if (kind.toLowerCase() === 'c') {
          url = '/informes/estado-cuenta?q=' + encodeURIComponent(rest);
        } else if (kind.toLowerCase() === 'f') {
          url = '/facturas?q=' + encodeURIComponent(rest);
        } else if (kind.toLowerCase() === 'p') {
          url = '/proveedores?q=' + encodeURIComponent(rest);
        } else if (kind.toLowerCase() === 'ch') {
          url = '/cheques?q=' + encodeURIComponent(rest);
        } else {
          url = '/informes/estado-cuenta?q=' + encodeURIComponent(q);
        }
      } else {
        url = '/informes/estado-cuenta?q=' + encodeURIComponent(q);
      }
      window.location = url;
    };
    return overlay;
  }

  document.addEventListener('keydown', (e) => {
    // Ctrl+K o Cmd+K abren la búsqueda global.
    const isCtrlK = (e.ctrlKey || e.metaKey) && e.key === 'k';
    // "/" abre sólo si no estás dentro de un input.
    const isSlash = e.key === '/' &&
                    !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
    if (isCtrlK || isSlash) {
      e.preventDefault();
      const ov = ensureOverlay();
      ov.style.display = 'block';
      ov.querySelector('#__gs_input').focus();
      ov.querySelector('#__gs_input').select();
      // Marcar como visto la primera vez que el usuario usa el atajo —
      // baja el pulso onboarding del botón.
      try { localStorage.setItem('__gs_seen', '1'); } catch (_) {}
      const trig = document.getElementById('__gs_trigger');
      if (trig) trig.classList.remove('__gs_pulse');
    }
  });

  // Al cargar la página:
  //   - si el usuario está en Mac, mostrar ⌘K en vez de Ctrl+K en el hint.
  //   - si es la primera vez que ve el header, dejar un pulse suave
  //     sobre el botón "Buscar" para que lo note.
  document.addEventListener('DOMContentLoaded', () => {
    const kbd = document.getElementById('__gs_kbd');
    const isMac = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || '');
    if (kbd && isMac) {
      kbd.textContent = '\u2318K';                 // ⌘K
      kbd.setAttribute('title', 'Cmd+K');
    }
    const trig = document.getElementById('__gs_trigger');
    let seen = false;
    try { seen = localStorage.getItem('__gs_seen') === '1'; } catch (_) {}
    if (trig && !seen) trig.classList.add('__gs_pulse');
  });
})();

// =====================================================================
// 3. Sidebar móvil — toggle del sidebar fuera de pantalla en mobile.
// =====================================================================

(function initMobileSidebar() {
  document.addEventListener('DOMContentLoaded', () => {
    const toggle = document.getElementById('nav-toggle');
    const sidebar = document.getElementById('sidebar');
    if (!toggle || !sidebar) return;
    toggle.addEventListener('click', () => {
      sidebar.classList.toggle('is-open');
      document.body.classList.toggle('no-scroll');
    });
    // Cerrar al click fuera
    document.addEventListener('click', (e) => {
      if (!sidebar.classList.contains('is-open')) return;
      if (sidebar.contains(e.target) || toggle.contains(e.target)) return;
      sidebar.classList.remove('is-open');
      document.body.classList.remove('no-scroll');
    });
  });
})();
