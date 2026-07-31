"""Columna AÑO de los anticipos (mig 0135) — TMT 2026-07-29 (dueña).

"¿Y si no una columna con año? porque tampoco la van a encontrar así" — el año
salió del concepto y pasó a su propia columna, en una tabla PC-only enganchada
por FIRMA (el sync del dBase hace TRUNCATE de scintela.dolares y regenera ids).
"""
from unittest.mock import patch

from modules.dolares import anio as anio_mod


def _fila(fecha, cta, concepto, importe, id_dolares=1):
    return {"id_dolares": id_dolares, "fecha": fecha, "cta": cta,
            "concepto": concepto, "importe": importe}


# ── firma y orden ───────────────────────────────────────────────────────────

def test_orden_desempata_las_filas_identicas():
    """Caso real: AC tiene dos "18 TRA" de $1.298,50 (19/06 aplicado y 09/07
    vivo). Si comparten día también, el orden los separa."""
    filas = [
        _fila("2026-07-09", "AC", "18 TRA", 1298.50, id_dolares=77),
        _fila("2026-07-09", "AC", "18 TRA", 1298.50, id_dolares=12),
        _fila("2026-06-19", "AC", "18 TRA", 1298.50, id_dolares=5),
    ]
    anio_mod.marcar_orden(filas)
    por_id = {f["id_dolares"]: f["_orden"] for f in filas}
    assert por_id[12] == 0      # el de id menor primero
    assert por_id[77] == 1
    assert por_id[5] == 0       # otro día → grupo propio


def test_firma_normaliza_concepto_y_cuenta():
    a = anio_mod._firma({**_fila("2026-04-21", "ac", " 58 saldo ", 8241.5),
                         "_orden": 0})
    b = anio_mod._firma({**_fila("2026-04-21", "AC", "58 SALDO", 8241.50),
                         "_orden": 0})
    assert a == b


# ── adjuntar_anio ───────────────────────────────────────────────────────────

def test_la_columna_manda_sobre_el_concepto():
    """Si el anticipo dice `58/25` en el concepto pero la columna dice 2026,
    gana la columna: es el dato explícito de la dueña."""
    filas = [_fila("2026-04-21", "AC", "58/25", 8241.50)]
    guardado = [{"fecha": "2026-04-21", "cta": "AC", "concepto_norm": "58/25",
                 "importe": 8241.50, "orden": 0, "anio": 2026}]
    with patch.object(anio_mod.db, "fetch_all", return_value=guardado):
        anio_mod.adjuntar_anio(filas)
    assert filas[0]["anio_col"] == 2026
    assert filas[0]["anio_ref"] == 2026


def test_sin_columna_queda_el_del_concepto():
    filas = [_fila("2026-04-21", "AC", "58/26", 8241.50)]
    with patch.object(anio_mod.db, "fetch_all", return_value=[]):
        anio_mod.adjuntar_anio(filas)
    assert filas[0]["anio_col"] is None
    assert filas[0]["anio_ref"] == 2026


def test_sin_columna_ni_concepto_no_hay_anio():
    filas = [_fila("2026-04-21", "AC", "58 SALDO", 8241.50)]
    with patch.object(anio_mod.db, "fetch_all", return_value=[]):
        anio_mod.adjuntar_anio(filas)
    assert filas[0]["anio_ref"] is None


def test_tabla_inexistente_no_rompe_la_pantalla():
    """Migración sin correr → se comporta como antes, sin explotar."""
    filas = [_fila("2026-04-21", "AC", "58 SALDO", 8241.50)]
    with patch.object(anio_mod.db, "fetch_all", side_effect=Exception("no existe")):
        anio_mod.adjuntar_anio(filas)
    assert filas[0]["anio_col"] is None


# ── guardar ─────────────────────────────────────────────────────────────────

def test_guardar_rechaza_anio_absurdo():
    fila = {**_fila("2026-04-21", "AC", "58", 8241.50), "_orden": 0}
    for malo in (1998, 2200):
        try:
            anio_mod.guardar(fila=fila, anio=malo)
            raise AssertionError(f"aceptó {malo}")
        except ValueError:
            pass


def test_guardar_vacio_borra_la_fila():
    fila = {**_fila("2026-04-21", "AC", "58", 8241.50), "_orden": 0}
    with patch.object(anio_mod.db, "execute") as ex:
        r = anio_mod.guardar(fila=fila, anio="")
    assert r["anio"] is None
    assert "DELETE" in ex.call_args[0][0]


def test_guardar_hace_upsert():
    fila = {**_fila("2026-04-21", "AC", "58", 8241.50), "_orden": 0}
    with patch.object(anio_mod.db, "execute") as ex:
        r = anio_mod.guardar(fila=fila, anio=2026, usuario="tamara")
    assert r["anio"] == 2026
    sql = ex.call_args[0][0]
    assert "ON CONFLICT" in sql and "INSERT" in sql


def test_guardar_exige_fecha_y_cuenta():
    try:
        anio_mod.guardar(fila={"fecha": None, "cta": "", "concepto": "x",
                               "importe": 1, "_orden": 0}, anio=2026)
        raise AssertionError("aceptó una fila sin firma")
    except ValueError:
        pass


# ── evidencia (la sugerencia se retiró: dueña 29/07 "no pongas sugerencia") ──

_IM_2026 = {"im_numero": "IM-0000622", "fecha": "2026-07-20",
            "nota": "ACMT/EXP/2026-27/8368 ( AC 58)", "recibida": False}
_IM_2025 = {"im_numero": "IM-0000380", "fecha": "2025-04-02",
            "nota": "ACMT/EXP/2025-26/6611 ( AC 58)", "recibida": True}


def test_no_propone_ningun_anio():
    """Dueña 29/07: la pantalla NO sugiere un año. Para el AC 77 la sugerencia
    decía 2025 cuando el correcto era 2026 — una sugerencia que puede estar mal
    en el campo que existe para desambiguar es peor que ninguna."""
    filas = [{**_fila("2026-04-21", "AC", "58", 8241.50), "ref": 58,
              "anio_col": None}]
    with patch.object(anio_mod, "_historial_por_ref", return_value={}):
        anio_mod.adjuntar_evidencia(
            filas, index_importaciones={("AC", 58): [dict(_IM_2025)]})
    assert "anio_sug" not in filas[0]
    assert filas[0]["anio_evidencia"]


def test_evidencia_nombra_las_importaciones_y_el_ciclo_anterior():
    filas = [{**_fila("2026-04-21", "AC", "58", 8241.50), "ref": 58,
              "anio_col": None}]
    hist = {("AC", 58): [
        {"fecha": "2025-06-06", "importe": 1150.40, "st": "B", "ref": 58},
        {"fecha": "2025-04-28", "importe": 48684.80, "st": "B", "ref": 58},
    ]}
    with patch.object(anio_mod, "_historial_por_ref", return_value=hist):
        anio_mod.adjuntar_evidencia(
            filas, index_importaciones={("AC", 58): [dict(_IM_2025), dict(_IM_2026)]})
    ev = filas[0]["anio_evidencia"]
    assert "IM-0000622" in ev and "IM-0000380" in ev
    assert "Ciclo anterior" in ev and "cerrado" in ev


def test_evidencia_avisa_cuando_no_hay_importacion():
    """El caso AC 77: no existe ninguna del año → hay que verlo."""
    filas = [{**_fila("2026-06-18", "AC", "77", 7298.20), "ref": 77,
              "anio_col": None}]
    with patch.object(anio_mod, "_historial_por_ref", return_value={}):
        anio_mod.adjuntar_evidencia(filas, index_importaciones={})
    assert "No hay ninguna importación AC 77" in filas[0]["anio_evidencia"]


def test_ciclo_anulado_se_rotula_distinto():
    """AC 76: INI $10.598,90 y un −$10.599,00 que lo anula → neto 0."""
    filas = [{**_fila("2026-06-18", "AC", "76", 8360.60), "ref": 76,
              "anio_col": None}]
    hist = {("AC", 76): [
        {"fecha": "2025-12-26", "importe": -10599.00, "st": "B", "ref": 76},
        {"fecha": "2025-09-15", "importe": 10598.90, "st": "B", "ref": 76},
    ]}
    with patch.object(anio_mod, "_historial_por_ref", return_value=hist):
        anio_mod.adjuntar_evidencia(filas, index_importaciones={})
    assert "anulado" in filas[0]["anio_evidencia"]


def test_no_calcula_evidencia_si_ya_tiene_anio_guardado():
    filas = [{**_fila("2026-04-21", "AC", "58", 8241.50), "ref": 58,
              "anio_col": 2026}]
    with patch.object(anio_mod, "_historial_por_ref", return_value={}) as h:
        anio_mod.adjuntar_evidencia(filas, index_importaciones={})
    assert not filas[0]["anio_evidencia"]
    h.assert_not_called()


# ── rescate LAXO: el concepto se mueve, el año no se pierde ────────────────
# TMT 2026-07-31 (dueña: "si se cargó este 11 que puse el año, no me avisó").
# El AI 11 tenía el año puesto y la celda salía vacía: la firma incluye el
# concepto, y el concepto lo mueve el ✎ de la pantalla y lo revierte el
# TRUNCATE+INSERT del sync (DOLARES.DBF no tiene delete_where).

def test_el_anio_sobrevive_a_que_cambie_el_concepto():
    filas = [_fila("2026-07-23", "AI", "11", 15518.68)]          # concepto de HOY
    guardado = [{"fecha": "2026-07-23", "cta": "AI", "concepto_norm": "11/26",
                 "importe": 15518.68, "orden": 0, "anio": 2026}]  # el de ANTES
    with patch.object(anio_mod.db, "fetch_all", return_value=guardado):
        anio_mod.adjuntar_anio(filas)
    assert filas[0]["anio_col"] == 2026
    assert filas[0]["anio_ref"] == 2026
    assert filas[0]["anio_origen"] == "columna-rescatada"


def test_el_rescate_no_adivina_si_hay_gemelos():
    """Dos anticipos del mismo día, cuenta e importe con conceptos distintos:
    la clave laxa no alcanza para saber cuál es cuál → no se asigna nada."""
    filas = [_fila("2026-07-09", "AC", "18 TRA", 1298.50, id_dolares=12),
             _fila("2026-07-09", "AC", "20 TRA", 1298.50, id_dolares=77)]
    guardado = [{"fecha": "2026-07-09", "cta": "AC", "concepto_norm": "18/26 TRA",
                 "importe": 1298.50, "orden": 0, "anio": 2026}]
    with patch.object(anio_mod.db, "fetch_all", return_value=guardado):
        anio_mod.adjuntar_anio(filas)
    assert [f["anio_col"] for f in filas] == [None, None]


def test_el_rescate_no_le_roba_el_anio_a_una_firma_exacta():
    """Si el año guardado ya matcheó exacto con otra fila, no se reusa."""
    filas = [_fila("2026-07-09", "AC", "18 TRA", 1298.50, id_dolares=12),
             _fila("2026-07-09", "AC", "20 TRA", 1298.50, id_dolares=77)]
    guardado = [{"fecha": "2026-07-09", "cta": "AC", "concepto_norm": "18 TRA",
                 "importe": 1298.50, "orden": 0, "anio": 2026}]
    with patch.object(anio_mod.db, "fetch_all", return_value=guardado):
        anio_mod.adjuntar_anio(filas)
    assert filas[0]["anio_col"] == 2026 and filas[0]["anio_origen"] == "columna"
    assert filas[1]["anio_col"] is None


def test_origen_marca_de_donde_salio_el_anio():
    filas = [_fila("2026-07-30", "AC", "49/26", 313.09)]
    with patch.object(anio_mod.db, "fetch_all", return_value=[]):
        anio_mod.adjuntar_anio(filas)
    assert filas[0]["anio_ref"] == 2026
    assert filas[0]["anio_col"] is None
    assert filas[0]["anio_origen"] == "concepto"


# ── mudar: el ✎ del concepto se lleva el año con él ────────────────────────

def test_mudar_reubica_el_anio_guardado():
    antes = {**_fila("2026-07-23", "AI", "11", 15518.68), "_orden": 0}
    nueva = {**_fila("2026-07-23", "AI", "11/26", 15518.68), "_orden": 0}
    with patch.object(anio_mod.db, "fetch_one",
                      return_value={"anio": 2026, "origen": "manual"}), \
         patch.object(anio_mod.db, "execute") as ex, \
         patch.object(anio_mod, "fila_por_id", return_value=nueva):
        assert anio_mod.mudar(fila_antes=antes, id_dolares=9) == 2026
    sqls = " ".join(str(c.args[0]) for c in ex.call_args_list)
    assert "DELETE" in sqls and "INSERT" in sqls


def test_mudar_no_hace_nada_si_no_habia_anio():
    antes = {**_fila("2026-07-23", "AI", "11", 15518.68), "_orden": 0}
    with patch.object(anio_mod.db, "fetch_one", return_value=None), \
         patch.object(anio_mod.db, "execute") as ex:
        assert anio_mod.mudar(fila_antes=antes, id_dolares=9) is None
    ex.assert_not_called()
