# Checklist — confirmar backups de RDS (retención 30 días)

Objetivo: verificar (y activar si hace falta) que la base de producción
`intela` en RDS tenga **backups automáticos con 30 días de retención + WAL
continuo**, para poder restaurar a cualquier segundo de los últimos 30 días si
algún día "se ensucia" toda la base.

> La data vive en **AWS RDS PostgreSQL 16**, base `intela`
> (`intela-db.…us-east-2.rds.amazonaws.com`), separada del server EC2 de la app.

---

## 1. Verificar el estado actual (2 min)

1. AWS Console → **RDS** → **Databases** → clic en la instance `intela-db`.
2. Pestaña **Maintenance & backups**.
3. Mirá la sección **Backup**:

   | Campo | Qué tiene que decir |
   |---|---|
   | **Automated backups** | `Enabled` |
   | **Backup retention period** | `30 days` (si dice `0 days` → están APAGADOS ⚠) |
   | **Backup window** | una franja de ~30 min (ej. `06:00-06:30 UTC` = 01:00 Ecuador, app dormida) |
   | **Latest restore time** | una fecha/hora reciente (≈ hace unos minutos) → confirma que el WAL está fluyendo |

✅ Si "retention = 30 days" y "Latest restore time" es reciente → **estás cubierto**, no hay que hacer nada más.

⚠ Si "retention = 0 days" → los backups están apagados. Seguí al paso 2.

---

## 2. Activar / cambiar a 30 días (si hace falta)

1. En la instance → botón **Modify** (arriba a la derecha).
2. Bajá a la sección **Backup**:
   - **Backup retention period**: elegí `30`.
   - **Backup window**: `06:00-06:30 UTC` (o dejá "No preference").
3. Abajo → **Continue**.
4. En "Scheduling of modifications" elegí **Apply immediately**.
5. **Modify DB instance**.

> Activar backups NO reinicia la base ni corta la app. El primer snapshot full
> puede tardar unos minutos en completarse; el WAL continuo arranca enseguida.

---

## 3. Confirmar que quedó bien (al otro día)

- Volvé a la pestaña **Maintenance & backups** → **Backup retention period** = `30 days`.
- Pestaña **Maintenance & backups** → sección **Automated backups** → tenés que ver snapshots listados (uno por día que vaya pasando).

---

## 4. Cómo se restaura (para tenerlo claro, NO ejecutar salvo emergencia)

Escenario "quiero toda la base como estaba ayer 15:00":

1. RDS → instance `intela-db` → **Actions** → **Restore to point in time**.
2. Elegí **Custom** → fecha/hora exacta (ej. ayer 15:00).
3. RDS crea una **instancia NUEVA** con esa foto (no pisa la actual).
4. Cuando levanta (~10-15 min), apuntás la app a la instancia nueva
   (cambiás `DB_HOST` en el `.env`) o promovés según convenga.

> Clave: el restore crea una copia aparte → podés comparar antes de switchear.
> La instancia productiva actual NO se toca hasta que vos decidas.

---

## 5. (Opcional) Backup `.sql` portable además del de RDS

Si querés un archivo `.sql` que puedas bajar/abrir en pgAdmin o llevarte,
además del backup interno de RDS, está documentado el pg_dump rotativo por día
en `docs/BACKUP_DIARIO_ROTATIVO.md` (Opción B).

---

## Resumen

- [ ] Retención = **30 días** (paso 1, o activarlo en paso 2).
- [ ] "Latest restore time" reciente (WAL fluyendo).
- [ ] (Opcional) pg_dump rotativo para tener `.sql` portables.

Con esto, "volver a ayer" ante un desastre = restore point-in-time por consola,
~10-15 min, sin perder la base actual.
