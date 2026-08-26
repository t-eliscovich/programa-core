"""Las pantallas del ERP: los ~80 blueprints de Programa Core.

Esto vivía adentro de `create_app()`. Se sacó el 24/08/2026 para poder tener un
**modo portal**: un proceso que levanta SÓLO las pantallas del cliente y en el
que las del ERP —cobranza, posdatados, balance, compras— **no existen**.

⭐ Por qué separar el proceso y no poner un candado: un candado hay que
escribirlo bien; que la ruta no exista no hay que escribirlo. Un agujero en el
portal, que va a estar abierto a internet, no puede tener del otro lado el ERP
entero. El candado (`scope_vendedor`) sigue puesto: protege ADEMÁS, no en vez.

El bloque se movió **tal cual, sin tocar una línea**, y hay un test que compara
el `url_map` completo contra el de antes para probarlo.

Ver `modo.py` y `registro_portal.py`.
"""
from __future__ import annotations

from flask import Flask


def registrar(app: Flask) -> None:
    """Registra todas las pantallas del ERP en `app`."""
    from modules.dashboard.views import dashboard_bp

    app.register_blueprint(dashboard_bp)

    from modules.informes.views import informes_bp

    app.register_blueprint(informes_bp, url_prefix="/informes")

    # TMT 2026-05-22 — blueprint propio (bajo /informes) que cruza
    # tintorería PC vs formulas_app. Aislado del informes_bp para evitar
    # conflictos con cambios en paralelo de modules/informes/views.py.
    from modules.comparativa_tintoreria.views import comparativa_tintoreria_bp

    app.register_blueprint(comparativa_tintoreria_bp, url_prefix="/informes")

    from modules.clientes.views import clientes_bp

    app.register_blueprint(clientes_bp)

    # Sync del maestro de clientes con Asinfo — pisa nombre/RUC, alta de
    # nuevos con campanita para cupo/descuento. TMT 2026-08-05.
    from modules.clientes.sync_asinfo_view import bp as clientes_sync_asinfo_bp

    app.register_blueprint(clientes_sync_asinfo_bp)

    from modules.proveedores.views import proveedores_bp

    app.register_blueprint(proveedores_bp)

    from modules.precios.views import precios_bp

    app.register_blueprint(precios_bp)

    from modules.facturas.views import facturas_bp

    app.register_blueprint(facturas_bp)

    from modules.cheques.views import cheques_bp

    app.register_blueprint(cheques_bp)

    from modules.bancos.views import bancos_bp

    app.register_blueprint(bancos_bp)

    from modules.compras.views import compras_bp

    app.register_blueprint(compras_bp)

    # TMT 2026-08-17 (dueña): /pedidos — lo que los clientes pidieron y todavía
    # no se despachó, cruzado con bodega y tinturado. Todo desde Asinfo; PC no
    # guarda pedidos. Va en el menú debajo de Factura Proforma.
    from modules.pedidos.views import pedidos_bp

    app.register_blueprint(pedidos_bp)

    # TMT 2026-08-18 (dueña): /inventario-rotativo — las telas y colores que se
    # venden casi todas las semanas, con lo que hay en bodega, lo que se está
    # tinturando y para cuántas semanas alcanza. Vive de Asinfo; no guarda nada
    # propio. Va en "Producción y stocks", con el mismo gate que Inventario.
    from modules.inventario_rotativo.views import inventario_rotativo_bp

    app.register_blueprint(inventario_rotativo_bp)

    from modules.tejeduria_asinfo.views import tejeduria_asinfo_bp

    app.register_blueprint(tejeduria_asinfo_bp)

    # TMT 2026-08-04 (dueña: "copia de tejeduría asinfo → terminado asinfo").
    # El otro paso de fabricación (bodega 53). Solo lectura: no hay compras.
    from modules.terminado_asinfo.views import terminado_asinfo_bp

    app.register_blueprint(terminado_asinfo_bp)

    from modules.stock.views import stock_bp

    app.register_blueprint(stock_bp)

    # TMT 2026-08-17 — sección Análisis: su propio menú, aparte del programa
    # del día a día. Gate `analisis.ver`, que sólo tienen los roles wildcard
    # (Accionista / Administrador) — el resto recibe 404 y ni ve el link.
    from modules.analisis.views import analisis_bp

    app.register_blueprint(analisis_bp)

    # TMT 2026-05-22 — blueprint nuevo /stock/asinfo (cantidad stock desde
    # ERP via Metabase). Aislado del stock_bp para no tocar modules/stock/.
    from modules.stock_asinfo.views import stock_asinfo_bp

    app.register_blueprint(stock_asinfo_bp, url_prefix="/stock")

    # TMT 2026-06-09 — /importaciones: importaciones de Asinfo cruzadas con las
    # compras/anticipos del programa por el código que va en la Nota.
    from modules.importaciones.views import importaciones_bp

    app.register_blueprint(importaciones_bp)

    # TMT 2026-07-30 (dueña): NOVEDADES — el buzón único de la campanita.
    # "hacé notificaciones globales" + "una pantalla novedades nueva".
    from modules.avisos.views import avisos_bp

    app.register_blueprint(avisos_bp)

    from modules.retenciones.views import retenciones_bp

    app.register_blueprint(retenciones_bp)

    from modules.caja.views import caja_bp

    app.register_blueprint(caja_bp)

    from modules.capital.views import capital_bp

    app.register_blueprint(capital_bp)

    from modules.provisiones.views import provisiones_bp

    app.register_blueprint(provisiones_bp)

    from modules.proformas.views import proformas_bp

    app.register_blueprint(proformas_bp)

    from modules.posdat.views import posdat_bp

    app.register_blueprint(posdat_bp)

    from modules.dolares.views import dolares_bp

    app.register_blueprint(dolares_bp)

    from modules.cartera.views import cartera_bp

    app.register_blueprint(cartera_bp)

    from modules.historial.views import historial_bp

    app.register_blueprint(historial_bp)

    from modules.checklist.views import checklist_bp
    from modules.cobranzas.views import cobranzas_bp

    app.register_blueprint(cobranzas_bp)
    # Checklist del día — qué falta cargar en PC vs la operación de ayer
    # (pedido dueña 2026-06-12, transición dBase→PC). TMT.
    app.register_blueprint(checklist_bp)

    from modules.comisiones.views import comisiones_bp

    app.register_blueprint(comisiones_bp)

    from modules.gastos.views import gastos_bp

    app.register_blueprint(gastos_bp)

    from modules.retiros.views import retiros_bp

    app.register_blueprint(retiros_bp)

    from modules.activos.views import activos_bp

    app.register_blueprint(activos_bp)

    from modules.iniciales.views import iniciales_bp

    app.register_blueprint(iniciales_bp)

    from modules.bitacora.views import bitacora_bp

    app.register_blueprint(bitacora_bp)

    from modules.periodos.views import periodos_bp

    app.register_blueprint(periodos_bp)

    from modules.usuarios.views import usuarios_bp

    app.register_blueprint(usuarios_bp)

    # Portal de vendedores (/mi-cartera) + carga de metas (/vendedores/metas).
    # TMT 2026-08-03. Ver scope_vendedor.py: los usuarios con `vend` cargado
    # SÓLO pueden entrar acá.
    from modules.mi_cartera.views import mi_cartera_bp

    app.register_blueprint(mi_cartera_bp)

    from modules.sri.views import sri_bp

    app.register_blueprint(sri_bp)



    # banco_v2_view registra los endpoints /conciliacion/banco-v2/* — Reforma
    # Sprint 1 (2026-05-28). Coexiste con /conciliacion/hub vigente hasta swap.
    from modules.conciliacion import banco_v2_view  # noqa: F401
    from modules.conciliacion.views import conciliacion_bp
    # /conciliacion/cambios eliminado 2026-05-29 dueña: 'esta pantalla no
    # sirve para nada'. El historial de matches se ve en /banco-v2/deshacer.

    app.register_blueprint(conciliacion_bp)

    from modules.healthz.views import healthz_bp

    app.register_blueprint(healthz_bp)

    # Diagnóstico de bridges externos — admin-only, no escribe nada.
    from modules.diag.views import bp as diag_bp

    app.register_blueprint(diag_bp)

    # Sync dBase en 1-click — TMT 2026-05-28. Reemplaza el dance manual
    # CloudShell+S3+SSM por POST a /admin/dbase-sync (admin-only).
    from modules.admin_dbase.views import bp as admin_dbase_bp

    app.register_blueprint(admin_dbase_bp)

    # Auto-match xlsx → scintela.transacciones_bancarias — TMT 2026-05-28.
    # Dueña: "conecta uno con uno me da igual" — endpoint que parsea xlsx y
    # crea matches por (fecha,monto,tipo) con tolerancia de centavo.
    from modules.admin_dbase.auto_match_view import bp as admin_automatch_bp

    app.register_blueprint(admin_automatch_bp)

    # Balance audit: PC vs Banco con desglose por categoría — TMT 2026-05-28.
    from modules.admin_dbase.balance_view import bp as admin_balance_bp

    app.register_blueprint(admin_balance_bp)

    # Aplicar migraciones SQL/Python en 1-click — TMT 2026-05-28.
    # Reemplaza RDP+migrate.py manual por POST a /admin/migraciones (admin).
    from modules.admin_dbase.migraciones_view import bp as admin_migraciones_bp

    app.register_blueprint(admin_migraciones_bp)

    # Deploy 1-click — TMT 2026-05-29. /admin/deploy hace `git pull origin
    # main` y Restart-ScheduledTask en 2 botones, reemplazando el dance
    # SSM/Run Command que la dueña hacía a mano cada push.
    from modules.admin_dbase.deploy_view import bp as admin_deploy_bp

    app.register_blueprint(admin_deploy_bp)

    # Reconciliador POSDAT — TMT 2026-06-05. /admin/posdat-reconcile alinea
    # scintela.posdat con POSDAT.DBF (quirúrgico: UPDATE in-place preservando
    # id_posdat, DELETE de las que sobran salvo linkeadas, INSERT de las que
    # faltan; YY fija baseline=hoy). Dry-run por defecto.
    from modules.admin_dbase.posdat_reconcile_view import bp as posdat_reconcile_bp

    app.register_blueprint(posdat_reconcile_bp)

    # Reconciliador FACTURAS (dry-run) — TMT 2026-06-10. /admin/facturas-reconcile
    # compara scintela.factura con FACTURAS.DBF y bucketé: pendiente de sync /
    # backfill Asinfo / creadas en PC (el sync las borraría) / huérfanas / diffs
    # de cobranza. SOLO LECTURA: el "apply" de facturas es el sync normal.
    from modules.admin_dbase.facturas_reconcile_view import bp as facturas_reconcile_bp

    app.register_blueprint(facturas_reconcile_bp)

    # Comparador sistemático PC vs dBase — TMT 2026-06-10 (pedido dueña:
    # "quiero poder comparar exacto sin sync, se están usando los dos
    # programas"). /admin/dbase-compare: tarball DBFs → 13 checks con reglas
    # PRG + identidad de utilidad (residuo 0 = todo explicado). SOLO LECTURA.
    from modules.admin_dbase.dbase_compare_view import bp as dbase_compare_bp

    app.register_blueprint(dbase_compare_bp)

    # /admin/salud — corre scripts/check_salud_dia.py POR LA UI. El chequeo
    # existía desde mayo pero NO lo corría nadie: script suelto en scripts/,
    # sin cron ni workflow. TMT 2026-07-29. SOLO LECTURA.
    from modules.admin_dbase.salud_view import bp as salud_bp

    app.register_blueprint(salud_bp)

    # Fechas de depósito desde el dBase — TMT 2026-07-20 (dueña: "¿no podés
    # traer el campo depositado?"). /admin/cheques-fechas-deposito completa
    # SOLO cheque.fechaing (columna Depositado) de cheques B/A sin fecha,
    # leyendo FECHING del CHEQUES.DBF ya subido al comparador. Display-only.
    from modules.admin_dbase.cheques_feching_view import bp as cheques_feching_bp

    app.register_blueprint(cheques_feching_bp)

    # TOTF 1 a 1 — TMT 2026-06-11. /admin/totf-1a1: pareo completo factura
    # por factura (N° SRI) PC vs FACTURAS.DBF, sin truncar, con cross-check
    # de backfill/stat del otro lado. SOLO LECTURA.
    from modules.admin_dbase.totf_1a1_view import bp as totf_1a1_bp

    app.register_blueprint(totf_1a1_bp)

    # Anticipos (scintela.dolares) — TMT 2026-06-11 dueña: sin sync, los
    # anticipos se cargan directo en PC. Alta + cancelación, suma a ANTIC.
    # TMT 2026-07-06 (dueña): "/anticipos/ borrar, tiene que ser /dolares" —
    # alta + cancelar MOVIDOS a modules/dolares; este blueprint queda solo
    # como redirects de compatibilidad (no borrar del disco todavía).
    from modules.anticipos.views import bp as anticipos_bp

    app.register_blueprint(anticipos_bp)

    # Importador de fichas de clientes — TMT 2026-06-06. /admin/clientes-import
    # completa dirección/teléfono/RUC/provincia desde CLIENTES.DBF (que no entra
    # al sync normal) y agrega los clientes que falten. Dry-run por defecto.
    from modules.admin_dbase.clientes_import_view import bp as clientes_import_bp
    from modules.admin_dbase.ficha_asinfo_view import bp as ficha_asinfo_bp

    app.register_blueprint(clientes_import_bp)
    app.register_blueprint(ficha_asinfo_bp)

    # Códigos de cliente DUPLICADOS vs Asinfo — TMT 2026-08-04.
    # /admin/clientes-asinfo cruza el maestro `empresa` de Asinfo contra
    # scintela.cliente y muestra qué código le da el ERP a cada RUC. Sólo
    # lectura: es la pantalla que se mira ANTES de borrar la ficha sobrante.
    from modules.admin_dbase.clientes_asinfo_view import bp as clientes_asinfo_bp

    app.register_blueprint(clientes_asinfo_bp)

    # Detalle de UN código repetido — /admin/clientes-asinfo/<codigo>. Cruza
    # las facturas de PC contra Asinfo POR RUC para decir de cuál de las dos
    # fichas es cada una. Es lo único que destraba BLP (75 facturas) y BRC
    # (18): con la plata mezclada, renombrar es una apuesta.
    from modules.admin_dbase.clientes_asinfo_detalle_view import bp as clientes_asinfo_det_bp

    app.register_blueprint(clientes_asinfo_det_bp)

    # Importador de proveedores desde FABRICA.DBF — TMT 2026-06-19.
    # /admin/proveedores-import crea los proveedores que faltan (BP, AC, AQ…)
    # con nombre/RUC/retenciones del maestro FABRICA. Dry-run por defecto.
    from modules.admin_dbase.proveedores_import_view import bp as proveedores_import_bp

    app.register_blueprint(proveedores_import_bp)

    # Cleanup one-off — marcar facturas Asinfo retroactivas como
    # usuario_crea='asinfo-backfill'. TMT 2026-06-10.
    from modules.admin_dbase.marcar_asinfo_view import bp as marcar_asinfo_bp

    app.register_blueprint(marcar_asinfo_bp)

    # Debug READ-ONLY de facturas en Asinfo (via Metabase DB 2) — TMT
    # 2026-06-12. /admin/debug-asinfo-facturas: investigar atributos de
    # facturas del ERP (vendedor, serie SRI, usuario, estado, forma de
    # pago) sin tocar datos. SOLO LECTURA.
    from modules.admin_dbase.debug_asinfo_facturas_view import bp as debug_asinfo_fact_bp

    # /admin/debug-dbase-compras — buscar una compra en el COMPRAS.DBF del
    # dBase (dueña 2026-07-31: "fijate si dBase lo tiene, si no no podemos
    # seguir tocando"). SOLO LECTURA sobre el tarball de dbase-compare.
    from modules.admin_dbase.debug_dbase_compras_view import (
        bp as debug_dbase_compras_bp,
    )
    from modules.admin_dbase.debug_fabricacion_wip_view import bp as debug_fab_wip_bp

    # /admin/debug-import-recepcion — kg facturados vs kg REALMENTE recibidos
    # por importación. Para medir el desfase anticipo↔stock (dueña 2026-07-31:
    # "están pistoleando hilo, todavía no cargó todo el stock"). SOLO LECTURA.
    from modules.admin_dbase.debug_import_recepcion_view import (
        bp as debug_import_recep_bp,
    )

    # /admin/debug-terminado-otros — de dónde salen los "otros mov." de la
    # bodega de terminado (dueña 2026-08-04: "hacé un deep dive y me contás qué
    # son"). SOLO LECTURA sobre Asinfo.
    from modules.admin_dbase.debug_terminado_otros_view import (
        bp as debug_terminado_otros_bp,
    )

    # /admin/facturas-centavos — cerrar las facturas que quedaron abiertas por
    # uno o dos centavos de redondeo. GET = dry-run, ?aplicar=1 escribe.
    from modules.admin_dbase.facturas_centavos_view import bp as facturas_centavos_bp

    app.register_blueprint(debug_asinfo_fact_bp)
    app.register_blueprint(facturas_centavos_bp)
    app.register_blueprint(debug_fab_wip_bp)
    app.register_blueprint(debug_terminado_otros_bp)
    app.register_blueprint(debug_import_recep_bp)
    app.register_blueprint(debug_dbase_compras_bp)
    # /admin/debug-cruce-compras — a qué importación se le atribuye cada compra
    # de hilado, y si esa importación está recibida. Para contestar por qué la
    # plata de algo NO recibido aparece en la tarifa del hilado (dueña
    # 2026-07-31). SOLO LECTURA.
    from modules.admin_dbase.debug_cruce_compras_view import (
        bp as debug_cruce_compras_bp,
    )

    app.register_blueprint(debug_cruce_compras_bp)

    # /admin/debug-grupos-partidas — qué importaciones son MITADES (---1/---2)
    # de una misma factura. Corre la MISMA agrupación que usa el balance y
    # muestra por qué cada grupo se armó o se descartó. SOLO LECTURA.
    from modules.admin_dbase.debug_grupos_partidas_view import (
        bp as debug_grupos_partidas_bp,
    )

    app.register_blueprint(debug_grupos_partidas_bp)

    # /admin/debug-maduracion-importacion — cuántos días tarda una importación
    # en tener toda su plata cargada, desde que llegó la mercadería. Para sacar
    # del dato el umbral de la alarma de banda. SOLO LECTURA.
    from modules.admin_dbase.debug_maduracion_import_view import (
        bp as debug_maduracion_import_bp,
    )

    app.register_blueprint(debug_maduracion_import_bp)

    # /admin/debug-costo-importacion — lo cargado contra lo que el hilo VALE
    # según el promedio por TIPO DE HILADO. Para fijar el corte de la alarma
    # con el dato (26/08: la banda única era el precio del polialgodón).
    # SOLO LECTURA.
    from modules.admin_dbase.debug_costo_importacion_view import (
        bp as debug_costo_importacion_bp,
    )

    app.register_blueprint(debug_costo_importacion_bp)

    # /admin/importaciones-sin-plata — las que llegaron y quedaron sin toda su
    # plata cargada. ?correr=1 fuerza la revisión sin esperar el ciclo de 6 h.
    from modules.admin_dbase.import_sin_plata_view import (
        bp as import_sin_plata_bp,
    )

    app.register_blueprint(import_sin_plata_bp)

    # /admin/debug-kg-por-mes — ¿los kilos del hilado se duplican o faltan?
    # kcom del balance vs los kg que Asinfo dice que entraron, mes por mes, y el
    # detalle de los códigos con más de una compra. SOLO LECTURA.
    from modules.admin_dbase.debug_kg_por_mes_view import (
        bp as debug_kg_por_mes_bp,
    )

    app.register_blueprint(debug_kg_por_mes_bp)

    # Health audit endpoints (Capas 3+4) — usuario_crea audit + utilidad
    # watchdog. JSON-only, para cron / curl manual. TMT 2026-06-10.
    from modules.admin_dbase.health_audit_view import bp as health_audit_bp

    app.register_blueprint(health_audit_bp)

    # Consola SQL de SÓLO LECTURA. Cada pregunta de datos costaba un endpoint
    # nuevo + 3 min de CI/deploy; con esto se contesta en el momento. La
    # garantía de que no escribe la da Postgres (`SET TRANSACTION READ ONLY`),
    # no un filtro de texto. TMT 2026-08-03.
    from modules.admin_dbase.sql_console_view import bp as sql_console_bp

    app.register_blueprint(sql_console_bp)

    # Regenerar snapshot scintela.historia. TMT 2026-06-10.
    from modules.admin_dbase.regen_snapshot_view import bp as regen_snapshot_bp

    app.register_blueprint(regen_snapshot_bp)

    # Vincular cheques históricos del dBase a sus facturas — TMT 2026-06-07.
    # /admin/abonos-historicos reconstruye el chequesxfact que el dBase nunca
    # guardó (CHEQUES.DBF no referencia la factura) y recalcula
    # abono = SUM(chequesxfact). Dry-run + confirmar.
    from modules.admin_dbase.abonos_historicos_view import bp as abonos_historicos_bp

    app.register_blueprint(abonos_historicos_bp)

    # Debug YY display-time — TMT 2026-05-28. Endpoint diagnóstico que
    # corre el helper fila por fila y devuelve tracebacks para encontrar
    # qué provoca el 500 de /posdat?tab=yy sin acceso al log del EC2.
    from modules.admin_dbase.debug_yy_view import bp as admin_debug_yy_bp

    app.register_blueprint(admin_debug_yy_bp)

    # Debug ustock=0 live — TMT 2026-06-02. /admin/debug-ustock devuelve
    # JSON con historia[top3], iniciales[mes actual + fallback],
    # kg_facturas_pc, y simulación del vsto final. Sin SSH/SSM.
    from modules.admin_dbase.debug_ustock_view import bp as admin_debug_ustock_bp

    app.register_blueprint(admin_debug_ustock_bp)

    # TMT 2026-07-20 (dueña): /admin/diag-pendientes-banco/* BORRADO (54
    # probes de una sesión de debugging de junio) — basura.
