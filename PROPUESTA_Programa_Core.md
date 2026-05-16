# Programa Core — Propuesta de reescritura

**Para:** Tamara
**Autor:** Claude (Cowork)
**Fecha:** 16 de abril de 2026
**Estado:** borrador para revisión — espera decisiones clave al final del documento

---

## 1. Lo que encontré

Lo primero que hay que entender es que no hay “un” programa. Hay **tres generaciones del mismo sistema** viviendo al mismo tiempo, y una base de datos nueva que en realidad ya está casi bien:

### (a) El programa dBase/Clipper original — `INTELA copy/`
Es el sistema mono-usuario en producción. Menú de texto (`MENU.PRG`), archivos `.DBF`, índices `.NDX`, lógica en `.PRG`. Cubre:

- **Ingresos:** cobranza, ventas, compras
- **Bancos:** Pichincha e Internacional — movimientos, saldos, conciliación
- **Flujo de fondos** (con cortes por cliente)
- **Informes** y **Estado de Cuenta del Cliente**
- **Retenciones**, **Proveedores**, **Posdatados**
- **Cupos**, **Stop Cliente**, **Teléfonos**
- **Localiza** cheque/factura/movimiento
- **Cobros efectivos de la semana**
- **Roles** (nómina, via xlsx histórico)
- **Tintura** (ligado al otro sistema de fórmulas)
- **Paso a F:\STAND / Habitat** — integración con otras empresas del grupo

Control de acceso por “clave” de 3 letras hardcodeada en el `PRG` (`CLAV $ 'GHG,TMT,DAE,FEP,NOR,ALX,DIA3,NTY'`). Inseguro, frágil, pero funciona.

### (b) El intento Flask + SQLite — `INTELA copy/sistema/`
Un rewrite a medio hacer. Flask con SQLAlchemy, SQLite, plantillas Jinja2, autenticación con sesión, usuarios/roles por módulo. Un solo `app.py` + `models.py` + `migrar.py` que lee los `.DBF`. No está terminado y duplica cosas con (c).

### (c) La app PyQt en PostgreSQL — `Programa Core/INTELA.rar`
Es lo más reciente y ambicioso. **275 archivos, 131 de Python.** PyQt6 de escritorio contra PostgreSQL remoto (Hetzner, IP `5.78.186.16`). Tiene pantallas (ventanas) para:

- Cobranza, ventas, compras, anticipos, provisiones, proveedores, retenciones
- Modificación de cheques, facturas, posdatados, compras, tintura, comisiones
- Bancos: depósitos, movimientos, saldos, conciliación, caja
- Informes: estado de cuenta, balance, flujo de fondos, cobros efectivos
- Gestión de seguridad (usuarios/roles)
- Proformas, costos, lista de precios, activos fijos con amortización automática

**Los problemas visibles:**
- Archivos duplicados con variantes raras (`registra_cobro.py`, `registra_cobro_log.py`, `registra_cobro_log_varios_ch.py`, `registra_cobro vant.py` con espacio, `registroAnticipo2.py`, `EstadoCuentaCliLog.py` + `estado_cuenta_cli_log.py`). Esto es la marca típica de “copio‑pego‑modifico”.
- Credenciales de la base de datos **en texto plano** en `config.ini` (`password=1n7el4Pyth0n`) — si el ejecutable se distribuye, la clave queda expuesta.
- `AppIntelaMenu.py` tiene rutas absolutas tipo `envPyQt6/INTELA/iconos/logoG.png`, lo que rompe el empaquetado.
- SQL embebido en cada ventana en lugar de una capa de datos única. Mezcla de `conexion_bd.py`, `conexion_cloud.py`, `conexion_neon.py`, `conexion_nube.py`, `manejar_BD.py`, `ManejaDatos.py`, `sqlserver_repo.py` — clarísimo síntoma de que nunca se decidió *dónde* vive la conexión.
- No hay tests, ni despliegue reproducible, ni log estructurado.
- UI 100% basada en ventanas Qt, no responsive, no móvil, no compartible vía link.

### (d) La base de datos PostgreSQL — `intela12042026.sql`
**Esta es la mejor noticia.** El modelo de datos ya está diseñado en PostgreSQL, con dos esquemas:

- **`scintela`** — 34 tablas: `cliente`, `factura`, `cheque`, `chequesxfact` (relación muchos‑a‑muchos cheque↔factura), `chequextransaccion`, `facturas_cubiertas`, `cobro`, `compra`, `proveedor`, `banco`, `cuenta_bancaria`, `transacciones_bancarias`, `retencion`, `retiros`, `posdat`, `dolares`, `caja`, `flujo`, `historia` (snapshots mensuales de 31 columnas), `activos` (con funciones SQL de amortización), `capital`, `iniciales`, `precios`, `tinto`, `costos`, `proforma_cabecera/detalle`, `producto`, `subcategoria_producto`, `grupo_cliente`, `provisiones`, `bitacora_migracion`, `xfactura`, `xgast`, `ahistoria`, `estados`.
- **`seguridad`** — `usuario` (con `password_hash`), `rol`, `permiso` (por nombre de opción). **Ya hay un modelo de permisos listo.**

Hay incluso **triggers y funciones** escritos (`actualizar_amortizacion()`, `fn_update_fecha_modifica()` para auditar `fecha_modifica`/`usuario_modifica`). Todas las tablas llevan cuatro columnas de auditoría: `fecha_crea`, `fecha_modifica`, `usuario_crea`, `usuario_modifica`.

**Traducción:** la parte más cara de una reescritura (diseñar el modelo, migrar los datos históricos) **ya está hecha**. Lo que está mal es la capa de aplicación.

---

## 2. Lo que propongo

### Opción recomendada: **Aplicación web en Flask/FastAPI sobre el PostgreSQL existente**

**Por qué web y no seguir en PyQt:**

| Criterio | Web | PyQt |
|---|---|---|
| Instalación por usuario | Ninguna, solo un link | Empaquetar + distribuir `.exe` por cada cambio |
| Acceso móvil / remoto | Nativo | Imposible sin VNC/TeamViewer |
| Despliegue de cambios | Un deploy, todos actualizados | Reinstalar en cada PC |
| Look & feel moderno | Natural | Esfuerzo alto |
| Permisos por rol/módulo | Estándar | Hay que armarlo |
| Tú ya corres `formulas_app` así | ✅ mismo stack | Nuevo stack paralelo |

Tienes **ya** `formulas_app` (Flask + PostgreSQL) corriendo bien. Reutilizamos el mismo patrón, el mismo servidor, la misma forma de desplegar, y tú ya sabes operarlo. Cero curva de aprendizaje operativa.

### Stack concreto propuesto

- **Backend:** Flask + psycopg2 (como `formulas_app`) — queries explícitas, sin ORM.
  - *Alternativa:* FastAPI si quieres tipado más estricto y una API aparte para integraciones futuras. Para este tipo de CRUD contable, Flask es más que suficiente y más rápido de arrancar.
- **Frontend:** HTML server-side (Jinja2) + Tailwind CSS + HTMX para interactividad sin complicarse con React.
  - Tablas rápidas, modales inline, validación en vivo, y se imprime bien (las pantallas contables necesitan impresión).
- **Base de datos:** PostgreSQL que ya tienes (`Intela` en Hetzner). Solo hay que revisar índices y agregar los que falten.
- **Autenticación:** `seguridad.usuario` + `seguridad.rol` + `seguridad.permiso` que **ya existen**. Passwords con bcrypt. Sesión con cookie firmada.
- **Auditoría:** las columnas `fecha_crea/fecha_modifica/usuario_*` ya están en todas las tablas — solo hay que usarlas consistentemente.
- **Despliegue:** mismo patrón del `formulas_app`: servidor Windows con Waitress o Linux con gunicorn + Nginx. Tarea programada para que no se caiga.

### Arquitectura limpia (regla dura esta vez)

```
programa_core/
├── app.py                 # solo rutas y wiring
├── db.py                  # pool de conexiones + helpers (1 sola capa)
├── auth.py                # login, sesión, decoradores @requiere_permiso("compras.editar")
├── modules/
│   ├── clientes/          # views + queries + templates de ese módulo
│   ├── facturas/
│   ├── cheques/
│   ├── compras/
│   ├── bancos/
│   ├── flujo/
│   ├── retenciones/
│   ├── proveedores/
│   ├── activos/
│   ├── caja/
│   └── informes/
├── templates/
│   ├── base.html          # navbar, sidebar, tema común
│   └── componentes/       # tabla, filtros, paginación reusables
├── static/
│   ├── app.css            # Tailwind compilado
│   └── app.js             # HTMX + un poco de JS
└── tests/
```

**Regla de oro:** un módulo = una carpeta = un conjunto de rutas + sus queries + sus plantillas. Nada de archivos sueltos con nombres variantes. El viejo mundo de `registra_cobro_log_varios_ch.py` se termina.

### Modelo de permisos

Basado en lo que ya hay en `seguridad.permiso`:

| Rol | Qué puede hacer |
|---|---|
| **Dueño (owner)** | Todo, incluido ver utilidad/patrimonio/flujo de fondos completo y crear usuarios |
| **Gerente** | Todo excepto capital y patrimonio; puede ver flujo sin clientes individuales |
| **Contabilidad** | Cheques, facturas, compras, retenciones, bancos, caja; lectura de informes |
| **Ventas** | Clientes, cobranza, proformas, cupos, stop-cliente; sus propias comisiones |
| **Taller / Tintura** | Solo pantallas de tintura, costos, y lectura de productos/precios |
| **Solo lectura** | Ve informes asignados, no modifica nada |

Cada opción del menú (ej. `"compras.crear"`, `"facturas.anular"`, `"flujo.ver"`) es un permiso en `seguridad.permiso`. Los roles son combinaciones. Los usuarios se asignan a un rol.

### Look & feel

- **Tema oscuro/claro** conmutable (factor de confort para pantallas largas).
- **Navegación lateral** con los módulos, breadcrumbs arriba, botón de acción primaria siempre visible en la esquina superior derecha.
- **Dashboard de inicio** por rol:
  - Dueño: saldos bancarios, cartera total, flujo próximos 30 días, top deudores, patrimonio.
  - Gerente: cobranza esperada esta semana, compras pendientes de pago, stock en alerta.
  - Contabilidad: pendientes de conciliar, retenciones sin emitir, facturas sin cobrar.
- **Tablas** con filtro rápido (sin página nueva), ordenamiento por columna, exportación CSV/XLSX, impresión limpia.
- **Formularios** con autosave visible, validación inline, mensajes en español.
- **Números en formato Ecuador** (coma decimal, punto de miles) — como ya hace `formulas_app` con su filtro `num_es`.

---

## 3. Alcance — fases propuestas

No todo a la vez. Orden sugerido:

### Fase 0 — Fundaciones (1–2 semanas)
- Saneamiento del schema PostgreSQL existente (índices faltantes, constraints, cleanup de `xfactura` vs `factura`).
- App base con login, sesión, menú, layout, tema.
- Módulo de gestión de usuarios/roles/permisos.

### Fase 1 — Núcleo contable (3–4 semanas)
- Clientes, proveedores (CRUD + búsquedas).
- Facturas (crear, listar, ver detalle, anular).
- Cheques (ingreso, aplicación a facturas, posdatados).
- Cobros y estado de cuenta por cliente.
- Compras (ingreso, retenciones).
- Bancos (movimientos manuales, saldos).

### Fase 2 — Operación diaria (2–3 semanas)
- Caja.
- Retiros.
- Retenciones (emisión + listado).
- Provisiones / pagos recurrentes.
- Conciliación bancaria asistida (lo más útil del PyQt).

### Fase 3 — Dirección (2 semanas)
- Flujo de fondos con corte por cliente.
- Dashboard del dueño.
- Informe Resultados / Balance.
- Activos fijos con amortización (ya hay función SQL).
- Histórico mensual (`historia` — ya hay 31 columnas).

### Fase 4 — Extras y pulido
- Comisiones.
- Proformas.
- Lista de precios / costos por tela.
- Integración con `formulas_app` (link desde factura a órdenes de tintura y viceversa).
- Reportes masivos / exportaciones.
- Bitácora de auditoría visible.

### Fase 5 — Despliegue y corte
- Deploy productivo, en paralelo al sistema viejo por ~30 días.
- Migración final, reentrenamiento del equipo, apagado del dBase.

Total: **≈ 10–12 semanas de trabajo real si vamos a buen ritmo** y tú respondes dudas rápido. Los números pueden bajar si recortamos fases o si el modelo de datos actual ya tiene lo que necesitamos (que en gran parte sí).

---

## 4. Qué pasa con lo que ya existe

- **Los `.DBF` antiguos:** se quedan como backup histórico. No se tocan más. Ya hubo migración a PostgreSQL (veo `bitacora_migracion` en el schema).
- **La app Flask de `sistema/`:** se abandona. No vale la pena seguirla.
- **La app PyQt de `Programa Core/INTELA.rar`:** se abandona como aplicación, pero se **leen sus 131 archivos** como documentación de las reglas de negocio que el programador implementó. Cosas como el cálculo de saldos, la lógica de conciliación, las pantallas de provisiones — todo eso se aprovecha como referencia al reimplementar cada módulo limpio.
- **La base PostgreSQL:** se **conserva y se limpia**. Es la inversión más grande que ya está hecha.

---

## 5. Riesgos y cómo los mitigo

1. **Interrumpir la operación de la fábrica durante el cambio.** — Solución: correr en paralelo al dBase viejo durante la Fase 5. Migración solo cuando la app nueva tenga 30 días sin bugs.
2. **Credenciales en texto plano / acceso expuesto.** — Solución: variables de entorno + secret manager; borrar `config.ini` con password del repo, rotar la contraseña.
3. **Regla de negocio perdida.** — Solución: cada módulo tiene un doc corto de “qué hace y qué reglas aplica” antes de escribirlo. Sacado de leer el `.PRG` original + el módulo PyQt.
4. **Usuarios temen el cambio.** — Solución: UI bilingüe de conceptos (“cheques posdatados”, “estado de cuenta”) idéntica a la del sistema viejo; no se renombran pantallas.
5. **Tú te cansas de esperar.** — Solución: entregamos algo usable **al final de la Fase 1**. No hay “big bang”.

---

## 6. Decisiones que necesito de ti (las pregunto a continuación en el chat)

1. **Plataforma** — ¿Confirmamos web, o quieres seguir por PyQt por alguna razón que no vi?
2. **Hosting** — ¿Seguimos en el servidor Hetzner actual, migramos al AWS donde vive `formulas_app`, o prefieres un servidor local en la fábrica?
3. **Orden de módulos** — ¿Coincide con mi propuesta de fases, o hay algo que arde y necesita salir antes?
4. **Usuarios concretos** — ¿Quiénes son las personas reales que lo van a usar y qué hace cada una? Eso calibra los roles.
5. **Integración con `formulas_app`** — ¿Quieres que se hable con el sistema de fórmulas desde el día uno (para asociar facturas a órdenes de tintura), o lo dejamos para la Fase 4?
6. **“Save everything into skills”** — En las instrucciones del proyecto me pediste guardar todo en skills. ¿Quieres que arme este trabajo como una **skill reusable** (`programa-core`) que guíe mis futuras sesiones sobre este proyecto? Si sí, la armo apenas tengamos las decisiones anteriores cerradas.

---

## 7. Qué hago si me dices “adelante”

Una vez tengamos las decisiones de la sección 6:

1. Creo la skill `programa-core` (arquitectura, convenciones, gotchas).
2. Monto el esqueleto (`app.py`, `db.py`, `auth.py`, layout base) y lo subo a tu carpeta.
3. Arranco la Fase 0 (login + usuarios/roles).
4. Te pido revisión al final de cada módulo antes de pasar al siguiente.
