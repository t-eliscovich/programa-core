"""Endpoint /admin/salud — corre `scripts/check_salud_dia.py` desde la app.

POR QUÉ EXISTE (TMT 2026-07-29): el chequeo de salud existía desde mayo pero
**no lo corría nadie** — es un script suelto en scripts/, sin cron ni workflow,
que había que ejecutar a mano con las env vars puestas. Un health check que
nadie corre no sirve de nada: el 29/07 la Utilidad Real se mostró ~495.000 más
baja durante minutos (196.010 en vez de 687.519) y lo descubrió la dueña
mirando la pantalla, no el chequeo.

Acá se expone por la UI, que es donde alguien lo va a ver. SOLO LECTURA: los
checks son SELECTs y llamadas a los bridges; ninguno escribe.

Streaming igual que /admin/dbase-compare/run — algunos checks tardan (BANCOS
recorre el running de cada banco, BRIDGES toca Metabase).
"""
from __future__ import annotations

import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path

from flask import Blueprint, Response, render_template_string, stream_with_context

from auth import requiere_login, requiere_permiso

bp = Blueprint("salud", __name__, url_prefix="/admin/salud")

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "check_salud_dia.py"


def _cargar_checks():
    """Importa el script como módulo (scripts/ no es paquete)."""
    spec = importlib.util.spec_from_file_location("check_salud_dia", _SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"no pude cargar {_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_TPL = """
<!doctype html><meta charset="utf-8">
<title>Chequeo de salud — Programa Core</title>
<style>
 body{font:14px system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;color:#0f172a}
 h1{font-size:1.25rem;margin-bottom:.3rem}
 .muted{color:#64748b;font-size:13px}
 button{padding:.55rem 1.1rem;border-radius:6px;border:0;background:#0f172a;color:#fff;
        cursor:pointer;font-size:14px;margin-top:1rem}
 ul{font-size:13px;color:#475569;line-height:1.7}
 code{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12px}
</style>
<p><a href="/" style="font-size:13px">&larr; Volver al menú</a></p>
<h1>Chequeo de salud</h1>
<p class="muted">Pasada de invariantes sobre la base. <strong>No cambia nada</strong>: sólo mira y reporta.</p>
<ul>
  <li><strong>Caja, Gastos, Bancos, Mov. doble, Cheques, Facturas, Posdat, Cheques×Factura,
      Reversibilidad</strong> — que el programa no se contradiga a sí mismo
      (saldos que cierren, nada huérfano, todo reversible).</li>
  <li><strong>Provisiones</strong> — que las 12 cuotas diarias sigan siendo las del
      <code>MENU.PRG</code>. Si alguien las cambia en el FoxPro, salta acá.
      Necesita que se haya subido un tarball <em>con MENU.PRG</em> en
      Comparación con dBase.</li>
  <li><strong>Bridges</strong> — que Asinfo conteste y que la utilidad del balance
      esté saliendo de Asinfo y no del dBase por un fallback silencioso.</li>
</ul>
<form method="get" action="/admin/salud/run">
  <button type="submit">Correr chequeo</button>
</form>
"""


@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("informes.ver")
def form():
    return render_template_string(_TPL)


@bp.route("/run", methods=["GET"])
@requiere_login
@requiere_permiso("informes.ver")
def run():
    def _gen():
        yield "=== CHEQUEO DE SALUD — Programa Core (solo lectura) ===\n\n"
        try:
            chk = _cargar_checks()
        except Exception as exc:  # noqa: BLE001
            yield f"[ERROR] no pude cargar el chequeo: {exc!r}\n"
            return

        chk._resultados.clear()
        for nombre, fn in chk.ALL_CHECKS.items():
            buf = io.StringIO()
            try:
                # Los checks imprimen a stdout; se captura y se re-emite para
                # que salga por el stream y no por el log del servidor.
                with redirect_stdout(buf):
                    fn(verbose=False)
            except Exception as exc:  # noqa: BLE001
                yield buf.getvalue()
                yield f"  [ERR]  {nombre}: explotó → {exc!r}\n"
                chk._resultados.append((nombre.upper(), chk.ERROR, repr(exc)))
                continue
            yield buf.getvalue()

        n_err = sum(1 for _, st, _ in chk._resultados if st.strip() == "[ERR]")
        n_warn = sum(1 for _, st, _ in chk._resultados if st.strip() == "[WARN]")
        n_ok = sum(1 for _, st, _ in chk._resultados if st.strip() == "[OK]")
        yield "\n" + "═" * 64 + "\n"
        yield f"  Resumen: {n_ok} OK · {n_warn} WARN · {n_err} ERR\n"
        if n_err == 0 and n_warn == 0:
            yield "  ✓ Todo verde.\n"
        elif n_err == 0:
            yield "  ~ Sólo warnings — operable, pero conviene mirar.\n"
        else:
            yield "  ✗ Hay errores — mirar antes de cerrar el día.\n"
        yield "═" * 64 + "\n"

    return Response(stream_with_context(_gen()), mimetype="text/plain; charset=utf-8")
