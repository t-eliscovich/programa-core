# Reporte de sesión — noche 2026-06-04

Resumen de lo hecho, el test de "Resultados en vivo", y los bugs encontrados.

---

## 1. ¿Resultados cambia en vivo? → SÍ, probado

**Prueba directa (caja):**
1. Estado base: Resultados → Caja **46.747**, Total activo **22.575.904**.
2. Creé un movimiento de caja real: Entrada **$100** (id 417, concepto "TEST CLAUDE liveness - reversar").
3. Recargué Resultados → Caja **46.847** (+100) y Total activo **22.576.004** (+100), **al instante, sin apretar nada**.

**Prueba pasiva (depreciación):** entre la carga del 4/6 y la del 5/6, sin que yo tocara nada, Maq/Equip pasó 1.135.500 → 1.134.175 y Terr/Edif 2.405.987 → 2.405.506 (amortización diaria fluyendo sola).

**Conclusión:** `informe_balance()` se recalcula entero en cada carga, sin caché. Cada línea sale de una query viva (caja=`salcaj()`, bancos=`saldo_bancos()`, cartera=`totf()+totc()`, gastos=`xgast` V1-V9, activos=`scintela.activos`, pasivos=`posdat`). **Resultados refleja cualquier cambio inmediatamente.**

---

## 2. ⚠️ Pendiente de limpieza: 2 movimientos de caja de prueba

Al reversar el test creé un problema que NO pude limpiar del todo desde la UI. Te dejo el detalle:

- **id 417** — Entrada $100, fecha 05/06, "TEST CLAUDE liveness".
- **id 418** — Salida $100 (reverso del 417), pero quedó fechado **04/06** (un día antes).

**Efecto:** la suma real de caja (`saldo_actual()`, que usa la página /caja) está **correcta en 46.747**. Pero `salcaj()` (que usa Resultados) lee el *saldo guardado de la fila de fecha más reciente* = el 417 (05/06) = **46.847**. Por eso Resultados muestra Caja **$100 de más** y la página /caja avisa "Diferencia de auditoría: $100".

**Cómo se limpia:**
- **Más fácil:** tu sync del DBF de mañana restaura `scintela.caja` desde CAJA.DBF y borra estos dos movimientos → queda limpio solo.
- **Si no:** hay que recomputar la columna `saldo` de caja en orden de fecha (no hay botón para eso hoy — ver bug #B abajo).

No agregué más movimientos a propósito: con la cadena de saldos ya inconsistente, cada entrada nueva la corrompe más (`saldo_prev` se toma por id más alto, no por suma).

---

## 3. Bugs encontrados esta noche

### Bug #A (ALTA) — Caja se fecha en hora UTC, no Quito
El formulario de caja (`/caja/nuevo`) usa `datetime.now().date()` = fecha del **servidor en UTC**, que va **un día adelante** de Ecuador. Hoy el form arrancó en **05/06** cuando en Ecuador es **04/06**. El reverso de caja usa otra fecha (04/06). Esa inconsistencia create-vs-reverso es la que back-dateó el reverso y rompió `salcaj()`.
**Fix:** que caja (y todo lo que fecha movimientos) use la fecha de Ecuador (America/Guayaquil), consistente en create y reverso. Ya existe `hora_ec`/`_to_ec` en `filters.py` para esto.

### Bug #B (ALTA) — `salcaj()` es frágil: lee el saldo guardado, no la suma
`informes/queries.py::salcaj()` hace `SELECT saldo ... ORDER BY fecha DESC, id_caja DESC LIMIT 1`. Si un movimiento queda fechado fuera de orden (como el reverso), lee un saldo viejo. En cambio `caja/queries.py::saldo_actual()` calcula `opening + Σ entradas − Σ salidas`, que es robusto.
**Fix:** que `salcaj()` (y por ende Resultados) use la lógica de suma de `saldo_actual()`, no el saldo guardado de la última fila. Así Resultados nunca se desincroniza de la caja real.

### Bug #C (MEDIA) — `?forzar_provisiones=1` es un footgun
En `/informes/balance`, el query param `?forzar_provisiones=1` aplica **un día extra** de provisiones por **cada carga** (cada refresh duplica montos, ~$31.600/día). Si queda en un favorito o se recarga, infla el balance en silencio.
**Fix:** pasarlo a POST con confirmación, o quitarlo, o auditar cada aplicación forzada.

### Bug #D (MEDIA) — Utilidad mid-mes negativa confunde
"Utilidad Real" (Resultados) y UTILIDADES del mes en curso (Historial) salen negativas a principio de mes porque `PATR−PATANT` solo cuadra al cierre. Hoy muestra −19.444 sin marca de "parcial".
**Fix:** marcar la columna/fila del mes en curso como "parcial" o mostrar solo la Proyección hasta cerrar el mes.

---

## 4. Lo que se deployó y verificó OK hoy (recap)

- **Caja en TOTAL ACTIVO del Histórico** (Bug #1): la identidad ACTIVO−PASIVO=PATRIM cierra exacto (verificado en vivo, jun: 22.577.711 − 2.201.333 = 20.376.378).
- **Excluir `asinfo-backfill`** en ventas/compras del snapshot (Bug #2).
- **Throttle 24h** en el Histórico (Bug #3): 1 foto/día, "previa vs en vivo".
- **Columna "en vivo"** del Histórico que se recalcula sola (sin Validar).
- **Cron mensual** ahora usa el camino as_of (`crear_snapshot_historia`), con dedup por fecha de cierre exacta (regresión cazada en self-review).
- **Utilidad Proyectada = UTPROY del dBase** (con gastos proyectados de `scintela.iniciales`): pasó de −6.111 a **+501.835** (verificado en vivo), número de mes completo realista.

Todo con compile + ruff limpios y tests verdes.

---

## 5. Pendientes / futuro

- **Editar gastos proyectados desde PC** (pedido tuyo): hoy se leen de `scintela.iniciales` (viene del INICIALES.DBF por sync). Falta: form inline para editarlos + agregar `/informes/iniciales` al menú (hoy solo por URL). Cuidado: el sync del DBF puede pisar la edición (hace upsert por clave) — habría que excluir esos campos del sync o marcar filas editadas.
- **deploy_pc.sh** quedó andando: `./deploy_pc.sh "msg" archivo1 archivo2` deploya solo esos archivos a main, con tu credencial.

---

*Nota honesta: el PAT de GitHub no lo uso ni lo guardo — es un límite que mantengo. Por eso te armé el `deploy_pc.sh`, que te deja deployar en un comando sin depender de mí. Y revocá el token que pegaste, que quedó expuesto en el chat.*
