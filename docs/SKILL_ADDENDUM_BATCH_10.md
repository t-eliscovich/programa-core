# Addendum para `skills/programa-core/SKILL.md` — batch 10

> El skill vive montado read-only adentro del sandbox, así que este batch queda
> como addendum en el repo. Cuando tengas acceso al host, pegá esta sección al
> final del `SKILL.md` (después de batch 9).

## Sesión 2026-04-17 — Fase 1.1 SRI facturación electrónica (MVP certificación-first)

Arranca el módulo SRI (Servicio de Rentas Internas de Ecuador) bajo un alcance
deliberadamente acotado: **generar y persistir el XML, sin firma ni envío**. La
firma requiere un `.p12` de CA ecuatoriana (BanEcuador, Security Data, Uanataca)
que todavía no está contratado; el envío SOAP depende de la firma. Se dejan
stubs listos para implementar cuando ambas piezas estén disponibles.

### Qué se shipó

- **Migración `0009_factura_electronica.sql`** — crea `scintela.factura_electronica`
  (1:1 con `scintela.factura`, sin FK dura para tolerar facturas anuladas que
  mantienen su comprobante). Columnas clave: `clave_acceso varchar(49) UNIQUE`,
  `ambiente char(1) CHECK ('1','2')`, `tipo_emision char(1) CHECK ('1','2')`,
  `estado varchar(20) CHECK ('borrador','firmado','enviado','autorizado','rechazado','anulado')`,
  snapshot de totales + `xml_generado` + `xml_firmado` + `respuesta_sri jsonb`,
  audit columns. Cuatro índices: UNIQUE(clave_acceso), idx(id_factura),
  idx parcial sobre pendientes, idx(fecha_emision DESC).

- **`modules/sri/core.py`** — helpers puros: `digito_verificador_modulo_11()`
  con casos borde (dv=10→'1', dv=11→'0'), `generar_clave_acceso()` 49 dígitos,
  `validar_clave_acceso()`, `desglosar_clave_acceso()`, `limpiar_ruc()`,
  `es_ruc_valido()`, `fmt_monto()`, `fmt_cantidad()`. Sin Flask, sin DB —
  determinista y testeable.

- **`modules/sri/xml.py`** — generador XSD 2.1.0 de factura. Dataclasses
  `Emisor`, `Comprador`, `DetalleLinea`, `Pago`, `InfoFactura` como contrato;
  `calcular_totales()` agrupa por porcentaje de IVA; `construir_xml_factura()`
  serializa a UTF-8 con declaración; `validar_estructura_factura()` hace
  pre-flight. Usa sólo `xml.etree.ElementTree` (stdlib, sin dependencias
  nuevas). Códigos IVA mapeados: 0→"0", 5→"8", 12→"2", 14→"3",
  15→"4" (vigente desde 2024-04).

- **`modules/sri/firma.py`** — stub. `FirmaNoConfiguradaError(NotImplementedError)`.
  Docstring explica que la implementación requiere `signxml`+`xmlsec`, XAdES-BES,
  SHA1, exc-c14n canonicalization.

- **`modules/sri/envio.py`** — stub. Constantes de URLs (`celcer.sri.gob.ec` vs
  `cel.sri.gob.ec`) + `url_recepcion()` / `url_autorizacion()`.
  `enviar_a_recepcion()` y `consultar_autorizacion()` levantan
  `EnvioNoConfiguradoError`.

- **`modules/sri/queries.py`** — persistencia. `crear_borrador()` inserta O
  regenera (si hay un registro anterior no-terminal, se pisa; si está
  `autorizado`/`anulado`, levanta `ValueError` — hay que emitir nota de
  crédito en vez). `por_id()`, `por_id_factura()`, `obtener_xml()`,
  `proximo_secuencial(estab, pto_emi, tipo)`. Respeta
  `asegurar_fecha_abierta(fecha_emision)`.

- **`modules/sri/views.py`** — blueprint `sri_bp`, 3 rutas:
  - `POST /sri/factura/<id_factura>/generar` → permiso `sri.emitir`. Arma
    `InfoFactura` desde la factura+cliente, calcula secuencial próximo, genera
    clave + XML, persiste como borrador, redirect al comprobante.
  - `GET /sri/factura_electronica/<id>` → permiso `sri.ver`. Detalle del
    comprobante (clave, estado, totales, errores estructurales).
  - `GET /sri/factura_electronica/<id>/xml` → permiso `sri.ver`. XML crudo
    (`application/xml`, inline con Content-Disposition para que "Guardar como"
    funcione).

- **`config/emisor.py`** — datos fijos de la fábrica leídos de env vars
  `SRI_EMISOR_*` con defaults. Única fuente del `Emisor`, `estab`, `pto_emi`
  y `ambiente_default`. Antes de prod, completar las env vars con los datos
  reales del RUC de Intela.

- **Template `modules/sri/templates/sri/detalle.html`** — tarjeta de estado
  (stat_chip coloreado según ambiente/estado), grid de clave/secuencial/fechas,
  snapshot de totales, panel de errores estructurales, próximos pasos.

- **Botón "Generar XML SRI"** en `modules/facturas/templates/facturas/detalle.html`,
  gateado por `g.permisos and 'sri.emitir' in g.permisos and fact.stat != 'Y'`.
  Confirma con `onsubmit` antes de POST.

- **`config/roles.py`** — agregado `sri.ver` a Administrador, Gerente, Lectura;
  `sri.ver, sri.emitir` a Contabilidad y Cobranzas. Dueño cubre con `"*"`.
  Ventas NO tiene `sri.emitir` (los cobradores facturan, no los vendedores).

- **Tests** — 61 nuevos (`tests/test_sri_core.py`: 31, `tests/test_sri_xml.py`: 30).
  Suite total **149 tests verde** (`python -m pytest -q`).

### Reglas específicas del módulo SRI (consolidar aquí para batches futuros)

1. **`clave_acceso` es identidad global, no local.** 49 dígitos, UNIQUE a través
   de todo el ecosistema SRI. Un INSERT duplicado = bug. Nunca regenerar la
   clave de un comprobante autorizado.

2. **Ambiente por defecto es certificación (`'1'`).** Producción (`'2'`)
   requiere override explícito con env var `SRI_AMBIENTE=2` + `.p12` de firma
   real + habilitación del RUC en el SRI. No cambiar el default.

3. **`fechaEmision` tiene DOS formatos distintos en el XML.** En la `claveAcceso`:
   `DDMMAAAA` (8 dígitos, sin separadores). En `infoFactura.fechaEmision`:
   `DD/MM/AAAA` (con barras). Confundirlos = rechazo SRI instantáneo.

4. **Montos en el XML: punto decimal, 2 decimales exactos.** El XSD del SRI
   rechaza coma decimal. Usar `fmt_monto()` y `fmt_cantidad()` siempre —
   nunca f-strings ad-hoc.

5. **Secuencial con zero-padding a 9 dígitos.** `proximo_secuencial()` devuelve
   int; `views.py` hace `str(n).zfill(9)` antes de pasarlo al XML. No confundir
   con `factura.numf` (legacy, no relacionado).

6. **Ciclo de vida unidireccional.** `borrador` → `firmado` → `enviado` →
   `autorizado|rechazado`. Un `autorizado` ya no se edita; si algo está mal,
   nota de crédito. Un `rechazado` sí se puede regenerar (pisa el registro).

7. **Una factura anulada (stat='Y') no se emite electrónicamente.**
   `views.generar()` chequea `fact.stat == 'Y'` y flashea warn. El botón en el
   template ya está gateado por la misma condición.

8. **Código de IVA SRI 15% = `'4'`, no `'15'`.** El mapping `_PCT_TO_COD_IVA`
   en `xml.py` es la única fuente de verdad. Agregar ahí si el SRI define uno
   nuevo (p.e. transitorio post-reforma).

9. **Emisor NO se lee de la DB.** Viene de env vars via `config/emisor.py`.
   El RUC de la fábrica es fijo; no hay razón para un lookup por request.

10. **`modules/sri/xml.py` es stdlib-only.** No agregar `lxml` ni `xmltodict`.
    La firma SÍ necesitará dependencias (`signxml`, `cryptography`, `xmlsec`) —
    agregar a `requirements.txt` sólo cuando se implemente firma.

11. **El heurístico de `tipo_identificacion` en `views._tipo_identificacion_de_cliente()`
    es MVP.** 13 dígitos → RUC (`'04'`), 10 → cédula (`'05'`), otro →
    consumidor final (`'07'`). Cuando se pida rigor, agregar columna
    `scintela.cliente.tipo_identificacion` y reemplazar la función.

12. **El desglose "una línea por factura" es MVP.** El schema legacy no tiene
    `factura_detalle`. El generador arma UNA línea "TELA" con cantidad=kg y
    precio_unitario calculado (base = importe / 1.15; precio_unit = base / kg).
    Cuando exista `scintela.factura_detalle`, reemplazar
    `_construir_info_factura()` con un `fetch_all()` sobre detalles reales.

### Pendiente (no en este batch)

- **Fase 1.1.2 Firma electrónica** — contratar `.p12`, implementar `firma.py`
  con `signxml` + XAdES-BES + SHA1 + exc-c14n canonicalization. Env vars:
  `SRI_P12_PATH`, `SRI_P12_PASSWORD`.
- **Fase 1.1.3 Envío SOAP** — implementar `envio.py` con `zeep` o `requests` +
  envelope SOAP manual. Manejar reintentos (el WS del SRI es inestable),
  timeout, rate limit.
- **Fase 1.1.4 Flujo completo** — ruta `/sri/factura_electronica/<id>/firmar`
  + `/enviar` + tarea programada que reenvía pendientes. Actualizar `estado`
  + `respuesta_sri` + `mensaje_error` + `numero_autorizacion`.
- **Notas de crédito/débito, guías de remisión, retenciones electrónicas** —
  cada una tiene su XSD propio; se puede reutilizar `core.py` (la clave de
  acceso es uniforme) pero hay que escribir un generador XML por tipo.
- **Puente con formulas_app** — cuando el usuario pida generar automáticamente
  la factura electrónica al cerrar una orden de tintura, el trigger vive en
  formulas_app.
- **ATS (Anexo Transaccional Simplificado)** — exportación mensual al SRI de
  compras/ventas. Reporte XML distinto, no comprobante.

### Comandos de rutina actualizados

```bash
# Tests del módulo SRI
python -m pytest tests/test_sri_core.py tests/test_sri_xml.py -q

# Ruff del módulo SRI (debe pasar limpio; el resto del repo tiene 18 errors pre-existentes)
python -m ruff check modules/sri/ config/emisor.py tests/test_sri_core.py tests/test_sri_xml.py

# Suite completa
python -m pytest -q    # 149 tests verde a fecha 2026-04-17

# Aplicar la migración 0009 cuando se deploye
python scripts/migrate.py --status          # verificar que 0009 esté pending
python scripts/migrate.py                    # aplica todo lo que falte
```

### Env vars nuevas para `.env` producción

```bash
# Datos fiscales del emisor (Intela). Completar con valores reales antes de prod.
SRI_EMISOR_RUC=1790012345001                          # 13 dígitos, RUC real Intela
SRI_EMISOR_RAZON_SOCIAL="TEXTILES INTELA S.A."         # razón social oficial
SRI_EMISOR_NOMBRE_COMERCIAL="INTELA"                   # nombre comercial
SRI_EMISOR_DIR_MATRIZ="Panamericana Sur ..."           # dirección fiscal completa
SRI_EMISOR_DIR_ESTABLECIMIENTO=""                      # vacío ⇒ usa dir_matriz
SRI_EMISOR_OBLIGADO_CONTABILIDAD=SI                    # "SI" o "NO"
SRI_EMISOR_CONTRIBUYENTE_ESPECIAL=""                   # número de resolución, vacío si no aplica
SRI_EMISOR_ESTAB=001                                   # establecimiento
SRI_EMISOR_PTO_EMI=001                                 # punto de emisión
SRI_AMBIENTE=1                                         # 1=certificación (default), 2=producción
```

### Decisiones registradas — 2026-04-17

- **Firma electrónica: NO implementar hoy.** Requiere .p12 contratado. Se shipa
  con stubs claros (`FirmaNoConfiguradaError`) para que cualquier llamada
  accidental falle rápido y con mensaje útil.
- **Ambiente: certificación por default.** Producción sólo se activa con env
  var explícita + .p12 de firma real. Esto es irreversible de facto —
  facturar en producción es un evento fiscal real.
- **Una línea por factura en el XML.** El schema legacy no guarda detalles;
  no justifica inventar ahora. Cuando venga la primera factura multi-item,
  agregar `scintela.factura_detalle`.
- **Secuencial por MAX+1 sobre `factura_electronica`, no sequence Postgres.**
  La fábrica factura serialmente; el UNIQUE sobre `clave_acceso` atrapa race
  conditions. Migrar a sequence cuando aparezca concurrencia real.
