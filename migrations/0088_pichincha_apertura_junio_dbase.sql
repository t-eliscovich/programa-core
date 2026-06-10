-- 0088_pichincha_apertura_junio_dbase.sql
-- TMT 2026-06-10 — decisión dueña: "dBase está bien, abrilos con lo mismo".
--
-- /admin/dbase-compare (saldo fin de día) mostró que PC abrió JUNIO con
-- +207.882,03 sobre el dBase en Pichincha (01/06: dBase 2.831.570,53 vs
-- PC 3.039.452,56) y que el gap es CONSTANTE salvo movimientos puntuales
-- identificados (03/06 −254,05 · 08/06 −100,92 · 09/06 +8.941,86 — esos
-- son diferencias de tipeo, visibles en el comparador, NO se tocan acá).
--
-- Corrección: bajar 207.882,03 la cadena de saldos de Pichincha
-- (no_banco=10) desde el 01/06. Las filas previas al 01/06 NO se tocan
-- (períodos viejos ya conciliados — la discontinuidad en el corte de mes
-- ES la corrección, igual que la apertura del archivo mensual del dBase).
--
-- IDEMPOTENTE: solo corre si la fila del 01/06 todavía tiene el saldo
-- equivocado (3.039.452,56). Re-correrla no resta dos veces.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM scintela.transacciones_bancarias
         WHERE no_banco = 10
           AND fecha = DATE '2026-06-01'
           AND saldo BETWEEN 3039452.55 AND 3039452.57
    ) THEN
        UPDATE scintela.transacciones_bancarias
           SET saldo = saldo - 207882.03
         WHERE no_banco = 10
           AND fecha >= DATE '2026-06-01';
        RAISE NOTICE 'Pichincha: apertura de junio alineada al dBase (−207.882,03 desde 01/06)';
    ELSE
        RAISE NOTICE 'Pichincha: apertura ya alineada (o saldo base distinto) — no-op';
    END IF;
END $$;
