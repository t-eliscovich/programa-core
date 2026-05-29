# Plan E2E Conciliación — turno nocturno 2026-05-29

**Pedido de Tamara**: "podes trabajar ahora haciendo tests End to end en
conciliaciones, quiero que manana este funcionando prolijo, hace un plan y
te organizas para quedarte haciendo".

## Objetivo

Dejar el flujo de conciliación bancaria v2 con cobertura E2E que valide:

1. **Que el bug del idx ($67K) NO regrese.** El test debe forzar que un
   IVA de $0.05 quede grabado como match contra ese IVA (no contra un
   depósito de $15K). Test caso-canario.
2. **Que la pantalla nueva funcione end-to-end**: upload extracto →
   sesión → conciliar manual / impuestos / transferencias → cerrar →
   XLSX → historial → deshacer → anular grupo → saldo correcto.
3. **Que el XLSX exportado tenga el formato esperado** (hoja FEB +
   bloque resumen TOTAL/SISTEMA/CONCILIADO/BANCO/DIFERENCIA).
4. **Que ningún rincón del flujo viejo (`/cambios`) rompa por links
   muertos**.

## Estrategia

Usar `pytest` + `FakeDB` (el mismo patrón de `tests/test_conciliacion_banco_actions.py`).
Sin DB real — `monkeypatch` sobre `db.fetch_one`/`fetch_all`/`execute`/`tx`.
Cada test setup la cola de respuestas que la query va a devolver, ejerce el
endpoint, y assert sobre los `executes` capturados.

Cuando sea inevitable testear contra Postgres (FK constraints en el
anular-grupo, por ejemplo), usar `pytest.mark.db` que el CI saltea por
default — los corre solo si hay `LOCAL_PG_DSN` seteado.

## Fases

### Fase 1 — Setup harness (30')

Crear `tests/conftest_v2.py` con:
- Fixture `client` → Flask test_client con CSRF disabled.
- Fixture `fake_db` → cola FIFO de respuestas + lista de executes.
- Fixture `auth_session` → mockea `g.user` con un admin.
- Fixture `fake_matcher` → controla qué devuelve `matchear_extracto_banco`.
- Helpers para simular un mov banco / mov bancsis / match.

### Fase 2 — Unit tests (45')

`tests/test_sesion_v2.py`:
- `crear_sesion` graba payload + dispara snapshot 'sesion_abierta'.
- `cargar_movs` deserializa lista de MovBanco correctamente.
- `bucketizar` mantiene invariant `idx == real_only[idx]`.
- `_cargar_historicos_pendientes` no pide la columna `no_cheque`
  (regression del bug 4aff731).
- `_hist_to_mov_like` wrappea sin errores.

`tests/test_balance_pichincha.py`:
- `calcular()` devuelve el dict con todas las claves esperadas.
- Edge: pendientes vacíos → neto=0, saldo_si_concilio_todo=saldo.

### Fase 3 — Views v2 integration (90')

`tests/test_banco_v2_views.py`:

| Test | Endpoint | Assert |
|---|---|---|
| `redirect_si_no_hay_sesion` | GET /banco-v2 | 302 → /conciliacion/ |
| `renderiza_tab_manual` | GET /banco-v2?tab=manual | 200 + "Manual" en HTML |
| `renderiza_tab_impuestos` | GET /banco-v2?tab=impuestos | 200 + "Impuestos" |
| `renderiza_tab_transferencias` | GET /banco-v2?tab=transferencias | 200 + "Transferencias" |
| `crear_sesion_redirige_a_v2` | POST /banco-v2/crear-sesion + xlsx | 302 → /banco-v2?sesion_id=N |
| `manual_confirmar_usa_real_only` | POST /banco-v2/manual/confirmar real_ids=2 | el mov que se matchea es res.real_only[2], NO movs[2] |
| `impuestos_confirmar_monto_correcto` | POST /banco-v2/impuestos/confirmar | crear_transaccion_agrupada llamado con suma de los reales seleccionados (NO suma de movs equivocados) |
| `transferencias_confirmar_PASS0` | POST /banco-v2/transferencias/confirmar | N confirmar_match calls, uno por par marcado |
| `terminar_genera_xlsx_y_cierra` | POST /banco-v2/terminar | sesion.cerrada_en=NOW + xlsx_path no nulo + redirect a cerrada |
| `anular_grupo_hard_delete` | POST /banco-v2/anular-grupo | DELETE matches + DELETE tx + recompute |
| `anular_grupo_falla_si_no_es_grupo` | id_tx que NO es created_from_real | flash error + redirect |
| `historial_muestra_saldos` | GET /banco-v2/historial | render con saldo_inicial/saldo_final del snapshot |
| `deshacer_lista_matches_y_grupos` | GET /banco-v2/deshacer | render con dos secciones |
| `pdf_descarga_xlsx` | GET /banco-v2/pdf/<id> | 200 + Content-Type xlsx + as_attachment |
| `migracion_faltante_no_500` | tabla_existe()=False, GET /banco-v2 | flash + redirect a hub, no 500 |

### Fase 4 — Template render smoke (30')

`tests/test_templates_v2_render.py`:
- Renderizar cada template con context mínimo:
  - `_balance_pichincha.html` (modos full + compact)
  - `banco_v2.html` con cada tab activo
  - `_banco_v2_tab_manual.html`
  - `_banco_v2_tab_impuestos.html`
  - `_banco_v2_tab_transferencias.html`
  - `banco_v2_deshacer.html` (con / sin grupos)
  - `banco_v2_historial.html` (con / sin saldos)
  - `banco_v2_cerrada.html`
  - `banco_upload.html` (con / sin saldo_pc_actual)

### Fase 5 — XLSX output validation (30')

`tests/test_xlsx_pendientes.py`:
- Mock `banco_historicos_pendientes` con 5 filas conocidas.
- Llamar `_generar_xlsx_pendientes(sesion, balance)`.
- Abrir el xlsx con openpyxl y verificar:
  - Sheet "DEPÓSITOS PENDIENTES"
  - Header en row 4: ["FECHA","DETALLE","CODIGO","VALOR","DETALLE"]
  - 5 filas de datos
  - Bloque resumen al final con las 5 líneas TOTAL/SALDO SISTEMA/TOTAL/SALDO BANCO/DIFERENCIA
  - Formato celda valor = `#,##0.00;(#,##0.00)` (paréntesis para neg)
  - Total numérico = suma de los VALOR

### Fase 6 — Suite full + bugs encontrados (30')

- Correr `pytest tests/ -v` (incluyendo los tests viejos).
- Asegurar que ninguno preexistente se rompió por mis cambios.
- Documentar bugs encontrados en `HALLAZGOS_E2E_2026_05_29.md`.
- Fixearlos si son rápidos. Si requieren decisión de producto → documentar.

### Fase 7 — Push + reporte (15')

- Push del commit con tests + fixes.
- Reporte para Tamara con: cuántos tests pasan, cuántos bugs, qué se
  fixeó y qué queda.

## Reglas

- Tests deben ser **rápidos** (<5s la suite v2 entera) — todo mock.
- Tests deben ser **deterministas** — no usar `datetime.now()` directo,
  freezear con fixture.
- Cada test cubre UN comportamiento — si rompe, el nombre dice qué.
- Tests del bug 67K son **canary**: deben fallar si alguien revierte el
  fix del idx accidentalmente.
- No tocar lógica de negocio — los tests cubren el código actual; si
  encuentro un bug NUEVO, lo documento + fix mínimo + test.

## Out of scope (queda para mañana con Tamara)

- Test end-to-end real con Postgres (requiere setup CI con DB).
- Test del flujo desmatch que NO baja saldo cuando el match apunta a
  grupo BANCSIS — requiere decisión de producto: ¿auto-anular grupo
  al deshacer último match?
- Selenium/playwright para JS (filtro Manual, totalizadores) — fuera
  de scope sin headless browser configurado.
