"""Puente formulas_app ↔ Programa Core — costos de órdenes de tintura (OT).

Hoy los costos de las OTs que cierran en formulas_app no llegan a Programa Core;
la operaria avisa por WhatsApp. Este módulo expone la misma información en el
app con un adaptador intercambiable:

    - FakeAdapter     — para desarrollo y demo; datos sintéticos pero realistas.
    - MetabaseAdapter — lee de una pregunta guardada en Metabase vía API.
    - PostgresAdapter — vista cross-schema en RDS compartida (cuando formulas_app
                        migre a la misma instancia).

El selector se controla con env var COSTOS_OT_ADAPTER (default: fake).
Ver `modules/costos_ot/adapters.py` para los contratos.
"""

from modules.costos_ot.service import costos_por_cliente, costos_por_factura, disponible

__all__ = ["costos_por_cliente", "costos_por_factura", "disponible"]
