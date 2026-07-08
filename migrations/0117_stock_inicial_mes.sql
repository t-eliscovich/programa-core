-- Migration 0117: scintela.stock_inicial_mes
--
-- Stock inicial (OPENING) mensual en KG por etapa, capturado desde Asinfo.
--
-- CONTEXTO (dueña 2026-07-08 "fijate cómo hacer stock inicial de Asinfo por
-- mes"): Asinfo (ERP SQL Server) es LIVE-ONLY vía Metabase — no guarda
-- histórico consultable de saldos de bodega por fecha. Para tener una línea
-- base mensual de "stock inicial" del flujo de producción hay que TOMAR UNA
-- FOTO del inventario live de Asinfo y persistirla en el Postgres de PC.
--
-- Esta tabla guarda UNA fila por (anio, mes, etapa). La foto la toma
-- modules.informes.stock_inicial.capturar(), que lee
-- asinfo.service.inventario_por_etapa() (bodegas 51 Hilo / 52 Tela Cruda /
-- 53 PT + WIP en proceso) y hace un upsert idempotente sobre el UNIQUE.
--
-- Etapas: 'hilo' | 'tela_cruda' | 'terminada' | 'en_proceso_tc' | 'en_proceso_pt'
--   (mismos nombres que las claves de inventario_por_etapa()).
--
-- Idempotente (IF NOT EXISTS + ON CONFLICT lo hace la app).

CREATE TABLE IF NOT EXISTS scintela.stock_inicial_mes (
    id_stock_inicial  SERIAL       PRIMARY KEY,
    anio              INT          NOT NULL,
    mes               INT          NOT NULL,
    etapa             TEXT         NOT NULL,
    kg                NUMERIC(14, 2) NOT NULL DEFAULT 0,
    capturado_en      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    usuario           TEXT,
    CONSTRAINT stock_inicial_mes_mes_chk   CHECK (mes BETWEEN 1 AND 12),
    CONSTRAINT stock_inicial_mes_etapa_chk CHECK (
        etapa IN ('hilo', 'tela_cruda', 'terminada',
                  'en_proceso_tc', 'en_proceso_pt')),
    CONSTRAINT stock_inicial_mes_uq UNIQUE (anio, mes, etapa)
);

COMMENT ON TABLE scintela.stock_inicial_mes IS
    'Foto mensual del stock inicial (OPENING) en KG por etapa del flujo de '
    'producción, capturada desde Asinfo (live-only vía Metabase). Una fila por '
    '(anio, mes, etapa). Idempotente vía UNIQUE(anio, mes, etapa). Escrita por '
    'modules.informes.stock_inicial.capturar(); leída por leer()/meses_capturados().';

CREATE INDEX IF NOT EXISTS stock_inicial_mes_periodo_idx
    ON scintela.stock_inicial_mes (anio, mes);
