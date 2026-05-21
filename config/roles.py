"""Canonical role → permiso map for Programa Core.

This is the single source of truth. The seeding migration reads it, the UI
reads it (for admin screens), and new features adding a new `x.accion`
permission should add it here first.

Shape: list of (nombre_rol, [permiso, ...]).

`"*"` means wildcard (all permissions). Only Accionista (antes "Dueño",
renombrado por pedido de la dueña 2026-05-19 v8 — el rol legal es
accionista de la empresa) gets that.

Design notes:
    - Contabilidad puede CREAR y ANULAR pero NO editar: una vez emitido un
      documento, queda en el histórico; si está mal, se anula (stat='Y') y
      se vuelve a crear. Esto preserva la traza de auditoría.
    - Cobranzas es un rol propio separado de Ventas: ve clientes/facturas/
      cheques y puede aplicar cheques + emitir retenciones.
    - Compras es un rol propio separado de Contabilidad: crea y anula
      compras y provisiones.
    - Bodega y QC reemplazan al rol "Taller" monolítico: Bodega ve/mueve
      productos, QC ve costos y fórmulas.
    - Administrador ve la bitácora, administra usuarios y cierra períodos.

Permisos RESERVADOS (definidos acá pero todavía no enforcados por ningún
@requiere_permiso) — son placeholders para módulos planeados que aún no
se construyeron. NO podarlos: cuando el módulo aterrice va a poder usar
el string sin migrar roles. Agrupados por estado:

    Pendiente módulo Bodega/Taller (Sprint pendiente):
        productos.ver, stock.ver, stock.mover, costos.ver, precios.ver,
        historia.ver

    Pendiente módulo QC / Tintura (formulas_app puente):
        tintura.ver, tintura.registrar, formulas.ver

    Pendiente UI granular en módulos existentes:
        cobranza.ver, cobranza.registrar (módulo cobranza separado del
            actual flujo cheques→aplicar)
        cupos.editar (sub-permiso de clientes.editar — hoy clientes.editar
            cubre todo)
        deudas.ver (vista dedicada — hoy se accede por compras.ver)
        flujo.ver (sub-permiso de informes.ver para roles más restrictivos)
        proformas.crear (módulo proformas pendiente)
        ventas.ver (sub-permiso de informes.ver, vista por vendedor)

    Pendiente admin UI:
        roles.ver, roles.editar (hoy roles se editan tocando este archivo
            + corriendo migración 0003 con --force)

Si revisás esta lista y un permiso ya no tiene sentido (ej. el módulo se
canceló), removerlo de los roles y dejar nota en el batch addendum del
día.
"""

ROLES: list[tuple[str, list[str]]] = [
    (
        "Accionista",
        [
            "*",  # acceso total
        ],
    ),
    (
        "Administrador",
        [
            "informes.ver",
            "flujo.ver",
            "bitacora.ver",
            "usuarios.admin",
            "periodo.cerrar",
            "roles.ver",
            "roles.editar",
            "clientes.ver",
            "proveedores.ver",
            "facturas.ver",
            "cheques.ver",
            "compras.ver",
            "bancos.ver",
            "cartera.ver",
            "deudas.ver",
            "retenciones.ver",
            "activos.ver",
            "activos.crear",
            "activos.amortizar",
            "iniciales.editar",
            "caja.ver",
            "provisiones.ver",
            "proformas.ver",
            "ventas.ver",
            "gastos.ver",
            "retiros.ver",
            "historia.ver",
            "iniciales.ver",
            "capital.ver",
            "posdat.ver",
            "sri.ver",
            "comisiones.ver",
        ],
    ),
    (
        "Gerente",
        [
            "informes.ver",
            "flujo.ver",
            "bitacora.ver",
            "clientes.ver",
            "proveedores.ver",
            "facturas.ver",
            "cheques.ver",
            "compras.ver",
            "bancos.ver",
            "cartera.ver",
            "deudas.ver",
            "retenciones.ver",
            "activos.ver",
            "iniciales.editar",
            "caja.ver",
            "provisiones.ver",
            "proformas.ver",
            "capital.ver",
            "posdat.ver",
            "ventas.ver",
            "gastos.ver",
            "retiros.ver",
            "historia.ver",
            "iniciales.ver",
            "comisiones.ver",
        ],
    ),
    (
        "Contabilidad",
        [
            "informes.ver",
            "clientes.ver",
            "proveedores.ver",
            "facturas.ver",
            "facturas.crear",
            "facturas.editar",
            "facturas.anular",
            "cheques.ver",
            "cheques.crear",
            "cheques.editar",
            "cheques.transicionar",
            "cheques.aplicar",
            "cheques.anular",
            "compras.ver",
            "compras.editar",
            "bancos.ver",
            "bancos.conciliar",
            "retenciones.ver",
            "retenciones.emitir",
            "retenciones.anular",
            "caja.ver",
            "caja.crear",
            "capital.ver",
            "capital.crear",
            "provisiones.ver",
            "posdat.ver",
            "activos.ver",
            "activos.crear",
            "activos.amortizar",
            "ventas.ver",
            "gastos.ver",
            "gastos.crear",
            "retiros.ver",
            "historia.ver",
            "iniciales.ver",
            "cartera.ver",
            "deudas.ver",
            "flujo.ver",
            "sri.ver",
            "sri.emitir",
            "comisiones.ver",
        ],
    ),
    (
        "Compras",
        [
            "proveedores.ver",
            "proveedores.crear",
            "proveedores.editar",
            "compras.ver",
            "compras.crear",
            "compras.anular",
            "provisiones.ver",
            "provisiones.crear",
            "provisiones.editar",
            "posdat.ver",
            "posdat.crear",
            "posdat.editar",
            "posdat.anular",
            "deudas.ver",
            "bancos.ver",
        ],
    ),
    (
        "Cobranzas",
        [
            "clientes.ver",
            "clientes.editar",
            "cupos.editar",
            "stop_cliente.editar",
            "facturas.ver",
            "cheques.ver",
            "cheques.crear",
            "cheques.aplicar",
            "cheques.anular",
            "retenciones.ver",
            "retenciones.emitir",
            "cobranza.ver",
            "cobranza.registrar",
            "cartera.ver",
            "deudas.ver",
            "informes.ver",
            "sri.ver",
            "sri.emitir",
        ],
    ),
    (
        "Ventas",
        [
            "clientes.ver",
            "clientes.crear",
            "clientes.editar",
            "cupos.editar",
            "stop_cliente.editar",
            "facturas.ver",
            "cheques.ver",
            "proformas.ver",
            "proformas.crear",
        ],
    ),
    (
        "Bodega",
        [
            "productos.ver",
            "stock.ver",
            "stock.mover",
            "compras.ver",
        ],
    ),
    (
        "QC",
        [
            "tintura.ver",
            "tintura.registrar",
            "costos.ver",
            "precios.ver",
            "productos.ver",
            "formulas.ver",
        ],
    ),
    (
        # TMT 2026-05-21 dueña: rol pedido para Alex (ALX). "Puede ver y editar
        # todo MENOS Informes y todas las pantallas que salgan solo desde
        # Informes (utilidades, retiros, ventas, comisiones, F&U, etc)".
        # Cartera/bancos/caja/cheques/compras/facturas/clientes/proveedores SÍ.
        "Operario",
        [
            # Operativa diaria — todo el flujo de cheques, caja, bancos.
            "caja.ver",
            "caja.crear",
            "cheques.ver",
            "cheques.crear",
            "cheques.editar",
            "cheques.transicionar",
            "cheques.aplicar",
            "cheques.anular",
            "bancos.ver",
            "bancos.conciliar",
            "cartera.ver",
            "posdat.ver",
            "posdat.crear",
            "posdat.editar",
            "posdat.anular",
            # Clientes / proveedores / cobranza.
            "clientes.ver",
            "clientes.crear",
            "clientes.editar",
            "cupos.editar",
            "stop_cliente.editar",
            "proveedores.ver",
            "proveedores.crear",
            "proveedores.editar",
            "cobranza.ver",
            "cobranza.registrar",
            # Facturas y compras (crear/editar/anular).
            "facturas.ver",
            "facturas.crear",
            "facturas.editar",
            "facturas.anular",
            "compras.ver",
            "compras.crear",
            "compras.editar",
            "compras.anular",
            "deudas.ver",
            "provisiones.ver",
            "provisiones.crear",
            "provisiones.editar",
            "retenciones.ver",
            "retenciones.emitir",
            "retenciones.anular",
            # SRI / proformas (operativas).
            "sri.ver",
            "sri.emitir",
            "proformas.ver",
            "proformas.crear",
            # Activos (operativa de altas/amortización).
            "activos.ver",
            "activos.crear",
            "activos.amortizar",
            # Iniciales editable para que pueda actualizar saldos bancos/caja.
            "iniciales.editar",
            # TMT 2026-05-21 dueña update: comisiones, ventas-año y gastos
            # clasificados SÍ. Estas 3 vistas viven bajo /informes/* pero ya
            # tienen permisos granulares — Alex las ve sin tener informes.ver.
            "comisiones.ver",
            "ventas.ver",
            "gastos.ver",
            # NO incluidos a propósito (todo "Informes" general y todo lo que
            # solo se accede desde ahí sin permiso granular): informes.ver,
            # flujo.ver, retiros.ver, historia.ver, iniciales.ver, capital.ver,
            # bitacora.ver, usuarios.admin, periodo.cerrar, roles.ver,
            # roles.editar.
        ],
    ),
    (
        "Lectura",
        [
            "informes.ver",
            "clientes.ver",
            "proveedores.ver",
            "facturas.ver",
            "cheques.ver",
            "compras.ver",
            "bancos.ver",
            "cartera.ver",
            "deudas.ver",
            "ventas.ver",
            "historia.ver",
            "iniciales.ver",
            "activos.ver",
            "caja.ver",
            "provisiones.ver",
            "proformas.ver",
            "capital.ver",
            "posdat.ver",
            "sri.ver",
            "comisiones.ver",
        ],
    ),
]
