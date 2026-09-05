"""El VIGÍA del servidor: mira la memoria cada minuto y, si falta, la recupera y avisa.

Tamara 2026-09-05: *"necesito una alarma que cuando esté así se corrija rápido"*.
Ese día (y el 31/08) el EC2 se quedó sin memoria y Andrés se enteró antes que
nadie, con el programa "pensando" 76 s por pantalla. La causa eran procesos
huérfanos del navegador de los PDFs (ver navegador.correr_y_matar_el_arbol);
la vez anterior se creyó que era Metabase. Las dos veces el arreglo llegó
horas después, a mano, por CloudShell.

Esto corre EN el servidor, en un hilo del programa de la oficina, y en cada
vuelta (cada `_CADA_S`):

  1. Lee la memoria libre. Si hay más de `MEMORIA_MINIMA_MB`, no hace nada.
  2. Si falta: barre los navegadores huérfanos (siempre, es gratis).
  3. Si sigue faltando y java (Metabase) está por encima de su tope,
     reinicia Metabase con el mismo script de la noche — como mucho una
     vez cada `_METABASE_CADA_S`.
  4. Deja la campanita y manda UN mail a los administradores diciendo qué
     vio y qué hizo — como mucho uno cada `_AVISO_CADA_S`, para no llenar
     la casilla mientras dura el episodio. Cuando la memoria vuelve, otro
     mail corto diciendo que volvió.

Lo que NO hace: matar nada que no sea nuestro, ni tocar formulas/máquinas.
Si lo que falta no se arregla con esto, el mail lo dice y la decisión (más
memoria: t3.large) es de Tamara. Fail-soft: ninguna vuelta puede tirar el
hilo, y el hilo jamás frena el arranque. Apagable con VIGIA_SERVIDOR=0.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from modules._lib import servidor

_LOG = logging.getLogger(__name__)

_CADA_S = 60.0
#: Abajo de esto se actúa (el mismo umbral del health).
MEMORIA_MINIMA_MB = servidor.MEMORIA_MINIMA_MB
#: Java arriba de esto está fuera de su tope (768m de heap + ~350 de resto).
JAVA_MAXIMO_MB = 1300
_METABASE_CADA_S = 2 * 3600.0
_AVISO_CADA_S = 3 * 3600.0
_ROOT = Path(__file__).resolve().parent.parent.parent
REINICIAR_METABASE = _ROOT / "scripts" / "servidor" / "reiniciar-metabase.ps1"

_started = False
_lock = threading.Lock()
#: Lo último que pasó, para /admin/pantallas.
_estado: dict = {"ultima_revision": None, "episodio": None, "acciones": []}
_ultimo_aviso = 0.0
_ultimo_metabase = 0.0
_en_episodio = False


def apagado() -> bool:
    return os.environ.get("VIGIA_SERVIDOR", "1").strip() == "0"


def _java_mb(est: dict) -> int:
    for p in est.get("procesos") or []:
        if (p.get("nombre") or "").lower().startswith("java"):
            return int(p.get("memoria_mb") or 0)
    return 0


def _barrer() -> int:
    from modules._lib import navegador

    mi_dir = str(navegador._NAV.dir) if navegador._NAV.dir else None  # noqa: SLF001
    return navegador.barrer_procesos_huerfanos(mi_dir=mi_dir)


def _reiniciar_metabase() -> str:
    """Corre el script de la noche. Devuelve qué pasó, en una línea."""
    if not sys.platform.startswith("win"):
        return "no es Windows: no se reinicia Metabase"
    if not REINICIAR_METABASE.exists():
        return f"no está {REINICIAR_METABASE.name}"
    try:
        subprocess.Popen(
            ["powershell.exe", "-ExecutionPolicy", "Bypass", "-NoProfile",
             "-File", str(REINICIAR_METABASE)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "Metabase reiniciado"
    except Exception as e:  # noqa: BLE001
        return f"no se pudo reiniciar Metabase: {e}"


def _correos_admin() -> list[str]:
    """Los administradores con correo: los que tienen `*` o `usuarios.admin`."""
    try:
        import db

        rows = db.fetch_all(
            """
            SELECT DISTINCT lower(u.email) AS email
              FROM seguridad.usuario u
              JOIN seguridad.permiso p USING (id_rol)
             WHERE u.activo AND u.email IS NOT NULL AND u.email <> ''
               AND p.nombre_opcion IN ('*', 'usuarios.admin')
            """)
        return [r["email"] for r in rows if r.get("email")]
    except Exception as e:  # noqa: BLE001
        _LOG.warning("vigia: no pude leer los correos de los administradores (%s)", e)
        return []


def _avisar(titulo: str, detalle: str, nivel: str = "alerta", clave: str = "") -> dict:
    """Campanita + mail. Nunca lanza."""
    res = {"campanita": False, "mail": 0}
    try:
        from modules.avisos.queries import avisar

        res["campanita"] = avisar(fuente="servidor", nivel=nivel, titulo=titulo,
                                  detalle=detalle[:200], url="/admin/pantallas",
                                  clave=clave or None)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("vigia: campanita (%s)", e)
    try:
        from modules._lib import mailer

        correos = _correos_admin()
        if correos and mailer.habilitado():
            env = mailer.enviar(f"INTELA · servidor: {titulo}", detalle, correos)
            res["mail"] = int(env.get("enviados") or 0)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("vigia: mail (%s)", e)
    return res


def revisar(ahora: float | None = None) -> dict:
    """Una vuelta. Devuelve qué vio y qué hizo (para tests y para la pantalla)."""
    global _ultimo_aviso, _ultimo_metabase, _en_episodio
    ahora = time.time() if ahora is None else ahora
    est = servidor.estado()
    vuelta: dict = {"cuando": ahora, "libres_mb": est.get("disponible_mb"),
                    "acciones": [], "aviso": None}
    _estado["ultima_revision"] = ahora
    if not est.get("total_mb"):
        return vuelta
    if not est["falta_memoria"]:
        if _en_episodio:
            _en_episodio = False
            vuelta["aviso"] = _avisar(
                "la memoria volvió",
                f"El servidor tiene otra vez {est['disponible_mb']} MB libres de "
                f"{est['total_mb']}. Ya no hace falta hacer nada.",
                nivel="ok", clave=f"servidor-volvio-{int(ahora // 3600)}")
        return vuelta

    # --- Falta memoria: primero lo gratis --------------------------------
    muertos = _barrer()
    if muertos:
        vuelta["acciones"].append(f"maté {muertos} navegadores huérfanos")
    time.sleep(3)
    est = servidor.estado()
    vuelta["libres_despues_mb"] = est.get("disponible_mb")
    java = _java_mb(est)
    if est["falta_memoria"] and java > JAVA_MAXIMO_MB \
            and ahora - _ultimo_metabase > _METABASE_CADA_S:
        _ultimo_metabase = ahora
        vuelta["acciones"].append(f"java tenía {java} MB: {_reiniciar_metabase()}")
    _estado["episodio"] = ahora
    _estado["acciones"] = list(vuelta["acciones"])
    _en_episodio = True

    # --- Aviso, como mucho uno cada _AVISO_CADA_S ------------------------
    if ahora - _ultimo_aviso > _AVISO_CADA_S:
        _ultimo_aviso = ahora
        top = ", ".join(f"{p['nombre']} ×{p['cuantos']} {p['memoria_mb']} MB"
                        for p in (est.get("procesos") or [])[:4])
        hecho = "; ".join(vuelta["acciones"]) or "nada que hacer desde el programa"
        detalle = (
            f"El servidor tenía {vuelta['libres_mb']} MB libres de {est['total_mb']} "
            f"(mínimo {MEMORIA_MINIMA_MB}) y todo se pone lento a la vez. "
            f"Los que más tienen: {top}. Lo que hice: {hecho}. "
            f"Quedaron {est.get('disponible_mb')} MB libres. "
            f"Si sigue así, es la máquina (4 GB) la que no da: ver /admin/pantallas."
        )
        vuelta["aviso"] = _avisar("le falta memoria", detalle,
                                  clave=f"servidor-memoria-{int(ahora // _AVISO_CADA_S)}")
    return vuelta


def estado() -> dict:
    """Para /admin/pantallas."""
    ahora = time.time()
    return {
        "prendido": _started and not apagado(),
        "hace_s": (round(ahora - _estado["ultima_revision"]) if _estado["ultima_revision"] else None),
        "episodio_hace_s": (round(ahora - _estado["episodio"]) if _estado["episodio"] else None),
        "acciones": list(_estado["acciones"]),
    }


def _loop() -> None:
    time.sleep(20)
    while True:
        try:
            with _lock:
                revisar()
        except Exception as e:  # noqa: BLE001 -- el hilo no se muere nunca
            _LOG.warning("vigia del servidor: %s", e)
        time.sleep(_CADA_S)


def arrancar_en_segundo_plano() -> bool:
    global _started
    if _started or apagado() or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    _started = True
    threading.Thread(target=_loop, name="vigia-servidor", daemon=True).start()
    _LOG.info("vigía del servidor prendido (cada %ss)", _CADA_S)
    return True
