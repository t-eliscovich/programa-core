-- =====================================================================
-- 0243 · La historia de la memoria del servidor
-- =====================================================================
-- Fase 2 del plan docs/PLAN_MEMORIA_SERVIDOR_2026_09_05.md. El 05/09 el
-- servidor se quedó sin memoria por una fuga que tardó DIEZ DÍAS en llenar
-- 4 GB (1.525 procesos chrome huérfanos): con una lectura por hora se
-- hubiera visto la curva bajar desde el primer día. El vigía
-- (modules/_lib/vigia_servidor.py) guarda una fila por hora y borra las de
-- más de 7 días; /admin/pantallas dibuja la curva y el health `servidor`
-- avisa si la tendencia baja.
-- =====================================================================

CREATE TABLE IF NOT EXISTS scintela.servidor_memoria (
    leido_en      timestamp    NOT NULL PRIMARY KEY DEFAULT CURRENT_TIMESTAMP,
    libres_mb     integer      NOT NULL,
    total_mb      integer      NOT NULL,
    java_mb       integer,
    chrome_mb     integer,
    chrome_n      integer,
    python_mb     integer,
    procesos      integer,
    cpu_pct       numeric(5,1)
);
