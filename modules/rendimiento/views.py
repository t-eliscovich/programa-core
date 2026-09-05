"""La pantalla que dice qué pantalla está lenta — `/admin/pantallas`.

TMT 2026-08-26 (dueña): *"cómo se podría evaluar las pantallas del programa y
hacerlas más rápido"*.

El dato lo junta `modules/_lib/medidor.py` (ver ahí el porqué). Acá sólo se
muestra, y se muestra ordenado por el TIEMPO TOTAL que se lleva cada pantalla
—visitas × mediana— y no por la más lenta: una pantalla de 3 segundos que se
abre una vez por mes molesta menos que una de 400 ms que se abre doscientas
veces por día, y la segunda es la que conviene arreglar.

⚠ Los números empiezan de cero en cada deploy: viven en memoria. Es lo que se
quiere — la pregunta es *"qué está lento hoy"*—, pero significa que recién
después de un rato de uso la tabla dice algo. El encabezado avisa desde cuándo
mide y cuántas visitas juntó, para que nadie saque conclusiones de tres visitas.
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for

from auth import requiere_login, requiere_permiso
from modules._lib import medidor, servidor

bp = Blueprint("rendimiento", __name__, url_prefix="/admin/pantallas",
               template_folder="templates")


@bp.route("", methods=["GET"])
@bp.route("/", methods=["GET"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def pantallas():
    """Qué pantalla se lleva el tiempo, y qué consulta se lo lleva adentro."""
    return render_template(
        "rendimiento/pantallas.html",
        filas=medidor.resumen(),
        lentas=medidor.lentas(),
        estado=medidor.estado(),
        calentador=medidor.calentador(),
        servidor=servidor.estado(),
        lenta_ms=int(medidor.LENTA_MS),
        seccion="admin",
    )


@bp.route("/reiniciar", methods=["POST"])
@requiere_login
@requiere_permiso("admin_dbase.ver")
def reiniciar():
    """Volver a cero — para medir UN cambio sin el arrastre de lo anterior.

    Es el botón que hace útil la pantalla cuando se está trabajando: se
    reinicia, se usan las tres pantallas que interesan, y la tabla habla de
    eso y no del día entero.
    """
    medidor.limpiar()
    return redirect(url_for("rendimiento.pantallas"))
