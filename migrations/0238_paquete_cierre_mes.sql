-- =====================================================================
-- 0238 · El paquete PDF del cierre de mes
-- =====================================================================
-- TMT 2026-08-31 (dueña): en el dBase, al cerrar el mes, alguien pegaba
-- capturas de las pantallas de cierre (Resultados/Balance, Ventas del mes,
-- Cartera, Deudas, Gastos + detalle de cada rubro, Flujo de producción,
-- Activos, Anticipos) en un Word -- un archivo por mes (ver FEBRERO.docx).
-- Ese rito se retiro junto con el dBase el 05/08 y no tenia reemplazo:
-- *"quiero que lo hagas vos... hagamoslo para el cierre, que sea parte del
-- proceso"*.
--
-- Esta tabla guarda el PDF armado -- mismas pantallas, mismos datos, pero
-- pedidas a la propia app (no una plantilla nueva a mantener) y juntadas
-- en un solo archivo (`modules/informes/cierres_paquete.py`). Se genera
-- best-effort, dentro de `crear_snapshot_historia()` -- la MISMA foto
-- que ya arma `scintela.historia` -- y solo en la rama LIVE (el mismo dia
-- que se cierra el mes; un backfill/as-of no tiene de donde sacar cartera
-- o gastos de un mes viejo, asi que ahi se salta).
--
-- `UNIQUE(anio, mes)` porque, igual que `scintela.historia`, el paquete se
-- puede regrabar (forzar=True en `crear_snapshot_historia` borra y rehace
-- la foto): la fila vieja del mes se pisa, no se acumula.
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.cierre_paquete (
    id_paquete    bigserial     PRIMARY KEY,
    anio          integer       NOT NULL,
    mes           integer       NOT NULL CHECK (mes BETWEEN 1 AND 12),
    pdf           bytea         NOT NULL,
    tamano_bytes  integer       NOT NULL,
    paginas       integer       NOT NULL DEFAULT 0,
    generado_en   timestamptz   NOT NULL DEFAULT now(),
    generado_por  varchar(50),
    UNIQUE (anio, mes)
);

CREATE INDEX IF NOT EXISTS ix_cierre_paquete_anio_mes
    ON scintela.cierre_paquete (anio DESC, mes DESC);
