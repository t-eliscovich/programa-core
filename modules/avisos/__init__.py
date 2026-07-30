"""NOVEDADES — el buzón único de avisos del programa.

TMT 2026-07-30 (dueña): *"la campanita debería funcionar también para cargas de
tejeduría, de fórmulas, no la hagas para compras. hacé notificaciones globales.
Y que la pantalla no diga convertir a compra etc: el punto es avisar"*.

Cómo se usa desde cualquier proceso:

    from modules.avisos import avisar
    avisar(fuente="tejeduria", titulo="Tejeduría Reyes · $ 1.630,00",
           detalle="2 compras · 1.618 kg", importe=1630, cantidad=2,
           url="/produccion-tejeduria-asinfo", clave=f"tejeduria:compra:{id}")

Tres reglas que hacen que esto no moleste:

1. **Nunca levanta.** Un aviso que rompe la operación que estaba avisando es
   peor que no avisar. Todo va en try/except y se loguea.
2. **`clave` es obligatoria en la práctica.** Los procesos de fondo reintentan
   lo mismo cada N minutos; sin clave el buzón se llena de repetidos. El INSERT
   la ignora si ya existe (`ON CONFLICT DO NOTHING`).
3. **El aviso viene redactado.** Acá no se decide cómo se ve: el que sabe qué
   pasó escribe el título y el detalle en castellano, sin siglas internas
   (ver la regla de "BAP" del 30/07).
"""
from .queries import (  # noqa: F401
    FUENTES,
    ICONOS,
    NIVELES,
    avisar,
    listar,
    marcar_leidos,
    n_no_leidos,
)
