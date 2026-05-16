# scripts/_archive/

Scripts conservados por historial — **no usar en operación nueva**. Superados por el sistema de migraciones bajo `migrations/` (ver `scripts/migrate.py`).

## fix_everything.py

Script de emergencia del 2026-04-16 que reseteó `seguridad.*`: dropeó FKs cruzadas a un `rol` foráneo, agregó PK/UNIQUE/secuencias faltantes, `TRUNCATE RESTART IDENTITY CASCADE`, reinsertó FKs a `seguridad.rol(id_rol)`, y verificó el login simulando `auth.login()`. Corrió una sola vez para salir del bootstrap roto. Hoy lo mismo lo hacen `migrations/0001_seguridad_fks.sql` + `migrations/0003_seed_roles.py` aplicados en orden por el runner.

## reset_admin.py

Script interactivo para recrear el usuario Dueño inicial. Hoy ese flujo vive en `scripts/seed_roles.py` (idempotente, lee `INTELA_ADMIN_USER` / `INTELA_ADMIN_PASSWORD` del entorno), y la UI `/usuarios` cubre la gestión posterior.

## Cuándo volver a mirar acá

Sólo como referencia histórica si aparece una inconsistencia de FKs en `seguridad.*` en una DB que no pasó por el sistema de migraciones. En ese caso, escribir una migración nueva — no ejecutar estos scripts directamente.
