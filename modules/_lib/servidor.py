"""Cómo está el SERVIDOR donde corre el programa: memoria, CPU y quién se la come.

TMT 2026-09-05 (Andrés, por WhatsApp): *"está super lento el sistema, qué
pasará? se queda pensando"*. Y el 31/08 lo mismo. Las dos veces la pantalla
/admin/pantallas decía que TODO estaba lento a la vez —hasta un archivo
estático tardaba segundos— y eso no es una pantalla: es la máquina.

El EC2 es UNA máquina de 4 GB para cuatro programas (Programa Core, formulas,
máquinas y Metabase). Cuando se queda sin memoria, Windows pagina a disco y
cada cosa tarda 10–30 s. El 31/08 el culpable fue Metabase con 1,6 GB; para
verlo hubo que entrar al servidor por CloudShell. Esto lo hace el propio
programa —que corre EN la máquina— y lo muestra arriba de todo en
/admin/pantallas, y lo vigila el health `servidor`.

⚠ Ordenar por memoria PRIVADA, no por working set: un proceso paginado a
disco muestra un working set chiquito y sigue siendo el que se comió la
memoria (lección del 31/08).

Fail-soft: si psutil no está o algo falla, se devuelve vacío y la pantalla
sigue.
"""

from __future__ import annotations

#: Abajo de esto Windows empieza a paginar en serio (el 31/08 con 63 MB
#: libres todo tardaba 20 s; con 400 MB ya respira).
MEMORIA_MINIMA_MB = 400

#: Cuántos procesos mostrar.
TOP_PROCESOS = 8


def _mb(n: int | float | None) -> int:
    return int(round((n or 0) / 1024 / 1024))


def memoria() -> dict:
    """Total y disponible en MB, y el % usado. Vacío si no se puede leer."""
    try:
        import psutil

        vm = psutil.virtual_memory()
    except Exception:  # noqa: BLE001 -- fail-soft
        return {}
    return {
        "total_mb": _mb(vm.total),
        "disponible_mb": _mb(vm.available),
        "usado_pct": round(float(vm.percent), 1),
    }


def _privada(p) -> int:
    """Memoria privada del proceso (en Windows `private`; si no, `rss`)."""
    mi = p.memory_info()
    return int(getattr(mi, "private", 0) or getattr(mi, "rss", 0) or 0)


def procesos(n: int = TOP_PROCESOS) -> list[dict]:
    """Los `n` NOMBRES de proceso que más memoria PRIVADA suman, con cuántos son.

    ⭐ POR NOMBRE y no por proceso, y es la lección del 05/09/2026: en la
    lista por proceso el que más tenía era java (1.614 MB) y se le echó la
    culpa; agrupando por nombre apareció **chrome × 1.525 = 8.903 MB** —
    cada uno chiquito, entre todos el servidor entero (ver
    navegador.correr_y_matar_el_arbol).
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return []
    grupos: dict[str, dict] = {}
    for p in psutil.process_iter(["pid", "name"]):
        try:
            nombre = p.info["name"] or ""
            mb = _mb(_privada(p))
            cpu = float(p.cpu_percent(interval=None) or 0)
        except Exception:  # noqa: BLE001 -- procesos del sistema sin permiso
            continue
        g = grupos.setdefault(nombre, {"nombre": nombre, "cuantos": 0,
                                       "memoria_mb": 0, "cpu_pct": 0.0})
        g["cuantos"] += 1
        g["memoria_mb"] += mb
        g["cpu_pct"] = round(g["cpu_pct"] + cpu, 1)
    filas = sorted(grupos.values(), key=lambda f: f["memoria_mb"], reverse=True)
    return filas[:n]


#: Más navegadores nuestros que esto es una fuga (el prendido son ~8
#: procesos; dos apps × un PDF en curso, ~30).
NAVEGADORES_MAXIMO = 40


def navegadores() -> int:
    """Cuántos procesos de navegadores NUESTROS hay (headless de PDF/imagen)."""
    try:
        from modules._lib import navegador

        return navegador.contar_procesos_nuestros()
    except Exception:  # noqa: BLE001
        return 0


def cpu() -> dict:
    """% de CPU de la máquina (una lectura corta) y cuántos núcleos."""
    try:
        import psutil

        return {"cpu_pct": round(float(psutil.cpu_percent(interval=0.2)), 1),
                "nucleos": int(psutil.cpu_count() or 0)}
    except Exception:  # noqa: BLE001
        return {}


def este_proceso() -> dict:
    """La memoria del propio Programa Core, para saber si el comilón somos nosotros."""
    try:
        import os

        import psutil

        return {"pid": os.getpid(), "memoria_mb": _mb(_privada(psutil.Process()))}
    except Exception:  # noqa: BLE001
        return {}


def estado() -> dict:
    """Todo junto, para la pantalla y el health."""
    mem = memoria()
    return {
        **mem,
        **cpu(),
        "procesos": procesos(),
        "navegadores": navegadores(),
        "programa": este_proceso(),
        "memoria_minima_mb": MEMORIA_MINIMA_MB,
        "falta_memoria": bool(mem) and mem["disponible_mb"] < MEMORIA_MINIMA_MB,
    }


def health() -> dict:
    """`{ok, alerts, stats}` como los demás health de /admin/health/all.

    Alerta sólo cuando la memoria disponible baja del mínimo — un número fijo
    a la vista no es alerta (ver feedback_el_dato_a_la_vista_mata_al_aviso).
    """
    est = estado()
    alerts: list[dict] = []
    if not est.get("total_mb"):
        return {"ok": True, "alerts": [], "stats": {"nota": "sin lectura del servidor"}}
    if est.get("navegadores", 0) > NAVEGADORES_MAXIMO:
        alerts.append({
            "tipo": "navegadores_huerfanos",
            "navegadores": est["navegadores"],
            "detalle": (
                f"Hay {est['navegadores']} procesos de navegadores del programa "
                f"(máximo {NAVEGADORES_MAXIMO}): quedaron huérfanos de sacar PDFs "
                f"e imágenes y se comen la memoria. El latido del navegador los "
                f"barre solo; si el número no baja, reiniciar el programa."
            ),
        })
    try:
        from modules._lib import vigia_servidor as _vigia

        t = _vigia.tendencia()
        if t.get("alerta"):
            alerts.append({
                "tipo": "servidor_memoria_en_baja",
                "baja_mb": t["baja_mb"],
                "detalle": (
                    f"La memoria libre del servidor viene bajando: hace 3 días "
                    f"había {t['antes_mb']} MB y ahora {t['ahora_mb']} "
                    f"(−{t['baja_mb']}). Así se vio la fuga de chrome del 05/09: "
                    f"mirar en /admin/pantallas quién crece."
                ),
            })
    except Exception:  # noqa: BLE001 -- sin base, sin tendencia
        pass
    if est["falta_memoria"]:
        top = ", ".join(f"{p['nombre']} ×{p['cuantos']} {p['memoria_mb']} MB"
                        for p in est["procesos"][:3])
        alerts.append({
            "tipo": "servidor_sin_memoria",
            "disponible_mb": est["disponible_mb"],
            "total_mb": est["total_mb"],
            "detalle": (
                f"El servidor tiene {est['disponible_mb']} MB libres de "
                f"{est['total_mb']} (mínimo {MEMORIA_MINIMA_MB}): todo se pone "
                f"lento a la vez. Los que más tienen: {top}. Ver /admin/pantallas."
            ),
        })
    stats = {k: est.get(k) for k in ("total_mb", "disponible_mb", "usado_pct", "cpu_pct")}
    stats["programa_mb"] = est.get("programa", {}).get("memoria_mb")
    stats["navegadores"] = est.get("navegadores")
    stats["top"] = [{"nombre": p["nombre"], "cuantos": p["cuantos"],
                     "memoria_mb": p["memoria_mb"]} for p in est["procesos"][:3]]
    return {"ok": not alerts, "alerts": alerts, "stats": stats}
