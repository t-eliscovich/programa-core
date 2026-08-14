"""POST smoke del módulo facturas — apretar el botón, no sólo abrir la pantalla.

POR QUÉ EXISTE
--------------
`test_routes_smoke.py` recorre las rutas GET con la base vacía y un stub de
`db`: prueba que la pantalla ABRE, no que el botón HAGA algo. El 14/08/2026
medimos que de las 21 rutas POST de facturas los tests posteaban a un puñado,
y `modules/facturas/views.py` estaba en 24% de cobertura. El 500 de la
conciliación del 13/08 —una clave que faltaba en el dict que iba al template—
era exactamente de esa clase: se ve sólo posteando.

Lo que este archivo persigue es EL 500. Un 400/404/409 o un redirect con flash
es una respuesta legítima a un payload incompleto y no rompe nada; ver
`OK_ESPERADOS` en `_smoke_base`.

POR QUÉ UN BARRIDO Y NO 21 TESTS
--------------------------------
Que falle de a una obliga a arreglar-correr-arreglar. El barrido entero en un
solo mensaje deja ver si lo que se rompió es UNA ruta o UN patrón (por ejemplo:
todas las que redirigen a `desde_asinfo`, o todas las que hablan con Asinfo).

QUÉ NO CUBRE
------------
`/facturas/admin/...`, `/facturas/debug-match` y `/facturas/diag-cartera`:
herramientas de migración de una sola vez, ya corridas.
"""
from __future__ import annotations

import io
import json
import os
from datetime import timedelta

import pytest

from filters import today_ec
from tests._smoke_base import (
    Barrido,
    CazaTragadas,
    app_smoke,
    conexion,
    db_smoke,
    getear_cazando,
    postear_cazando,
)

pytestmark = pytest.mark.db

#: Base propia del smoke de facturas. Cada módulo del barrido (facturas /
#: cheques / bancos) usa la suya: las rutas MUTAN (anulan, borran, recodean) y
#: compartir base haría que el orden de los tests cambiara el resultado.
DB_SMOKE = db_smoke()

#: Los numf de la siembra viven arriba de todo lo real para que un `DELETE
#: WHERE numf >= NUMF_BASE` no pueda llevarse por delante una factura de verdad
#: si alguien apunta esto por error a una base con datos.
NUMF_BASE = 900_000

#: Código de cliente de 3 letras — la casa no usa dos iniciales (feedback
#: 2026-07). SM2 es la "sucursal" de SMK, que es el caso que el mapeo por
#: dirección resuelve.
CLI = "SMK"
CLI_SUC = "SM2"

#: Direcciones de Asinfo inventadas: una ya sembrada (para borrarla por la
#: ruta) y otra que la ruta de alta va a crear.
DIR_SEMBRADA = 990_001
DIR_NUEVA = 990_002

#: Alias propio del smoke. Los aliases que hay en la base los sembraron las
#: migraciones 0052/0132 y son datos reales: si el barrido borrara uno de
#: ésos, la primera corrida cubriría el camino "borró" y todas las siguientes
#: el camino "no existía" — el test diría cosas distintas según cuántas veces
#: se corrió.
ALIAS_ASINFO = "SMQ"


# ---------------------------------------------------------------------------
# Siembra
# ---------------------------------------------------------------------------
def _sembrar(conn) -> dict:
    """Escenario mínimo para que las rutas no mueran todas en "no existe".

    Se siembra por SQL directo a propósito: acá no estamos operando
    producción, estamos construyendo el escenario para poder apretar el botón.
    El botón sí se aprieta por la ruta real.

    Cada ruta destructiva se lleva SU factura (anular, reversar-carga, editar
    el numf…). Compartirlas haría que el orden del barrido decidiera el
    resultado, que es justo lo que un smoke no puede permitirse.
    """
    hoy = today_ec()
    ids: dict = {"hoy": hoy}

    with conn.cursor() as cur:
        # Limpieza previa — el barrido CREA facturas (por /facturas/nueva y por
        # las cargas desde Asinfo), así que sin esto la segunda corrida
        # arrancaría con la basura de la primera y dejaría de ser reproducible.
        cur.execute("DELETE FROM scintela.mov_doble")
        cur.execute("DELETE FROM scintela.retencion WHERE codigo_cli IN (%s, %s)",
                    (CLI, CLI_SUC))
        cur.execute("DELETE FROM scintela.factura WHERE codigo_cli IN (%s, %s)",
                    (CLI, CLI_SUC))
        cur.execute("DELETE FROM scintela.cliente WHERE codigo_cli IN (%s, %s)",
                    (CLI, CLI_SUC))
        cur.execute("DELETE FROM scintela.sucursal_direccion WHERE id_direccion IN (%s, %s)",
                    (DIR_SEMBRADA, DIR_NUEVA))
        cur.execute("DELETE FROM scintela.cliente_alias WHERE codigo_asinfo = %s",
                    (ALIAS_ASINFO,))

        for cod, nombre in ((CLI, "SMOKE CLIENTE SA"), (CLI_SUC, "SMOKE SUCURSAL SA")):
            cur.execute(
                "INSERT INTO scintela.cliente (codigo_cli, nombre, ruc, vend, activo) "
                "VALUES (%s, %s, %s, %s, TRUE)",
                (cod, nombre, "1790000000001", "PPR"),
            )

        def _factura(clave: str, *, numf_off: int, importe, abono, saldo,
                     stat: str, retencion=0):
            numf = NUMF_BASE + numf_off
            cur.execute(
                """
                INSERT INTO scintela.factura
                    (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat,
                     condic, tipo, vencimiento, numf_completo, retencion,
                     usuario_crea)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        'CO', 'FA', %s, %s, %s, 'smoke')
                RETURNING id_factura
                """,
                (numf, hoy, CLI, 25, importe, abono, saldo, stat,
                 hoy + timedelta(days=90), f"001-001-{numf:09d}", retencion),
            )
            ids[clave] = cur.fetchone()["id_factura"]
            ids[clave + "_numf"] = numf

        # 'Z' emitida sin abono, 'A' con abono parcial, 'T' cancelada, 'X'
        # anulada: los cuatro estados que las vistas ramifican distinto (las
        # anuladas no se editan, las canceladas no se vuelven a cobrar).
        _factura("z", numf_off=1, importe=1000, abono=0, saldo=1000, stat="Z")
        _factura("parcial", numf_off=2, importe=2000, abono=500, saldo=1500, stat="A")
        _factura("cancelada", numf_off=3, importe=300, abono=300, saldo=0, stat="T")
        _factura("anulada", numf_off=4, importe=400, abono=0, saldo=400, stat="X")
        # Con retención: el saldo YA viene neteado — la retención baja el saldo
        # pero NO se suma al abono (mig 0179, 07/08/2026).
        _factura("con_ret", numf_off=5, importe=1200, abono=0, saldo=1140,
                 stat="Z", retencion=60)
        # Sin ningún movimiento colgando: es la única que `reversar-carga`
        # puede borrar de verdad.
        _factura("borrable", numf_off=6, importe=700, abono=0, saldo=700, stat="Z")
        _factura("abono_manual", numf_off=7, importe=3000, abono=500, saldo=2500,
                 stat="A")

        cur.execute(
            "INSERT INTO scintela.retencion (codigo_cli, rete, numf, fecha, usuario_crea) "
            "VALUES (%s, %s, %s, %s, 'smoke')",
            (CLI, 60, ids["con_ret_numf"], hoy),
        )

        # El mov_doble del abono manual: `reversar_abono_manual` exige tipo
        # exacto, estado activo y `abono_prev` en la metadata — sin los tres la
        # ruta corta en ValueError y no cubriríamos el reverso de verdad.
        cur.execute(
            """
            INSERT INTO scintela.mov_doble
                (fecha_operacion, tipo, origen_table, origen_id,
                 destino_table, destino_id, importe, concepto, usuario,
                 estado, metadata)
            VALUES (%s, 'factura_abono_manual', 'factura', %s,
                    'factura', %s, %s, %s, 'smoke', 'activo', %s)
            RETURNING id_mov_doble
            """,
            (hoy, ids["abono_manual"], ids["abono_manual"], 400,
             f"Abono manual factura #{ids['abono_manual_numf']}",
             json.dumps({"abono_prev": 100, "abono_nuevo": 500})),
        )
        ids["mov_abono_manual"] = cur.fetchone()["id_mov_doble"]

        cur.execute(
            "INSERT INTO scintela.sucursal_direccion "
            "       (id_direccion, codigo_cli, nota, usuario_crea) "
            "VALUES (%s, %s, %s, 'smoke')",
            (DIR_SEMBRADA, CLI_SUC, "sembrada por el smoke para poder borrarla"),
        )

        cur.execute(
            "INSERT INTO scintela.cliente_alias "
            "       (codigo_asinfo, codigo_pc, nota, usuario_crea) "
            "VALUES (%s, %s, %s, 'smoke')",
            (ALIAS_ASINFO, CLI, "sembrado por el smoke para poder borrarlo"),
        )
    conn.commit()
    return ids


@pytest.fixture(scope="module")
def escenario():
    try:
        conn = conexion(DB_SMOKE)
    except Exception as e:  # pragma: no cover - depende del entorno
        pytest.skip(f"No hay base {DB_SMOKE} para el POST smoke de facturas: {e}")
    try:
        return _sembrar(conn)
    finally:
        conn.close()


def _csv_facturas(codigo_cli: str, fecha) -> bytes:
    """Un CSV con las columnas que `FACTURAS_CSV_COLS` declara requeridas.

    Va con UNA fila válida: si el archivo estuviera vacío la ruta cortaría
    antes de llamar a `procesar_csv` y no cubriríamos nada del alta masiva.
    """
    f = fecha.strftime("%d/%m/%Y")
    return (
        "Fecha,Código cliente,Kg,Importe,N° factura,Vencimiento,"
        "N° completo,Tipo,Condición,Clave\n"
        f"{f},{codigo_cli},12,345.67,{NUMF_BASE + 500},,"
        f"001-001-{NUMF_BASE + 500:09d},FA,CO,SM\n"
    ).encode()


# ---------------------------------------------------------------------------
# El barrido
# ---------------------------------------------------------------------------
def test_post_smoke_facturas(escenario):
    """Postea las 21 rutas POST de facturas + las GET con parámetro.

    Las GET van PRIMERO y las destructivas (anular, reversar-carga) al final:
    no para esquivar errores, sino para que cada ruta encuentre su factura en
    el estado que la pantalla real le daría.
    """
    ids = escenario
    hoy = ids["hoy"]
    hoy_iso = hoy.isoformat()
    hoy_ec = hoy.strftime("%d/%m/%Y")
    desde_iso = hoy.replace(day=1).isoformat()

    b = Barrido("facturas")

    with app_smoke(DB_SMOKE) as app:
        c = app.test_client()

        # Casi toda vista de facturas termina en `except Exception: flash_exc(…)`
        # + redirect: una vista que revienta contesta 302 igual que un rechazo
        # ordenado del negocio. Sin espiar `flash_exc`, este barrido daría verde
        # con la app rota adentro. Ver el bloque de `_smoke_base.py`.
        from modules.facturas import views as vistas_facturas

        caza_cm = CazaTragadas(vistas_facturas)
        caza = caza_cm.__enter__()

        # -- GET con parámetro en la URL — el smoke viejo los saltea porque
        #    necesitan un id real, que es justo lo que la siembra da.
        b.agregar(getear_cazando(c, caza, f"/facturas/{ids['z_numf']}", nota="detalle por numf"))
        b.agregar(getear_cazando(c, caza, f"/facturas/{ids['z']}",
                         nota="detalle por id interno (canonaliza al numf)"))
        b.agregar(getear_cazando(c, caza, f"/facturas/{ids['anulada_numf']}",
                         nota="detalle de una anulada"))
        b.agregar(getear_cazando(c, caza, f"/facturas/{ids['z']}/editar", nota="form de edición"))
        b.agregar(getear_cazando(c, caza, f"/facturas/{ids['anulada']}/editar",
                         nota="editar una anulada: redirige con flash"))
        b.agregar(getear_cazando(c, caza, f"/facturas/{ids['z']}/confirmar-anulacion",
                         nota="paso 1 del 2-step undo"))
        b.agregar(getear_cazando(c, caza, f"/facturas/abono-manual/{ids['mov_abono_manual']}/reversar",
                         nota="pantalla de confirmación del reverso"))

        # -- Alta -----------------------------------------------------------
        b.agregar(postear_cazando(c, caza, "/facturas/nueva", {
            "fecha": hoy_ec, "codigo_cli": CLI, "kg": "18,50",
            "importe": "1.234,56", "numf": str(NUMF_BASE + 101),
            "numf_completo": f"001-001-{NUMF_BASE + 101:09d}",
            "condic": "CO", "tipo": "FA", "vencimiento": "",
        }, nota="venta normal, numf explícito"))
        # La devolución es una factura con kg/importe NEGATIVOS (MODIFICA.PRG:
        # NT.DEVOL = NUMF>0 AND IMPORTE<0). La vista le pone el signo sola, así
        # que el payload va en positivo igual que lo tipea la contadora.
        b.agregar(postear_cazando(c, caza, "/facturas/nueva", {
            "fecha": hoy_ec, "codigo_cli": CLI, "kg": "5",
            "importe": "100,00", "numf": str(NUMF_BASE + 102),
            "devolucion": "1", "tipo": "NT",
        }, nota="devolución (kg e importe se vuelven negativos)"))
        # Cliente inexistente con permiso clientes.crear = flujo guiado, no
        # error: manda a /clientes/nuevo con el form guardado en el `next`.
        b.agregar(postear_cazando(c, caza, "/facturas/nueva", {
            "fecha": hoy_ec, "codigo_cli": "ZZZ", "kg": "1", "importe": "10",
        }, nota="cliente inexistente: redirige a crear el cliente"))
        b.agregar(postear_cazando(c, caza, "/facturas/nueva", {
            "fecha": "", "codigo_cli": "", "kg": "", "importe": "",
        }, nota="form vacío: 400 con la lista de errores"))

        # -- Edición blanda -------------------------------------------------
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['z']}/editar", {
            "abono": "250,50", "condic": "CO",
            "observacion": "abono cargado por el smoke",
        }, nota="abono parcial"))
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['z']}/editar", {
            "abono": "-1", "condic": "", "observacion": "",
        }, nota="abono negativo: 400"))
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['anulada']}/editar", {
            "abono": "10",
        }, nota="editar una anulada: redirige con flash"))

        # -- Edición inline (JSON) ------------------------------------------
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['con_ret']}/campo", {
            "campo": "importe", "valor": "1.500,00",
        }, nota="importe con coma decimal EU"))
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['con_ret']}/campo", {
            "campo": "fecha", "valor": hoy_iso,
        }, nota="fecha"))
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['con_ret']}/campo", {
            "campo": "saldo", "valor": "1",
        }, nota="campo no soportado: 400"))
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['cancelada']}/numf", {
            "numf": str(NUMF_BASE + 103),
            "numf_completo": f"001-001-{NUMF_BASE + 103:09d}",
        }, nota="corregir el N° de factura"))
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['cancelada']}/numf", {
            "numf": "abc",
        }, nota="numf no numérico: 400"))

        # -- Reverso del abono manual ---------------------------------------
        b.agregar(postear_cazando(c, caza, f"/facturas/abono-manual/{ids['mov_abono_manual']}/reversar", {
            "motivo": "smoke", "next": "/facturas",
        }, nota="deshace el abono manual"))
        b.agregar(postear_cazando(c, caza, f"/facturas/abono-manual/{ids['mov_abono_manual']}/reversar", {
            "motivo": "smoke", "next": "/facturas",
        }, nota="segunda vez: ya reversado, flash warn"))

        # -- Cargas desde Asinfo --------------------------------------------
        b.agregar(postear_cazando(c, caza, "/facturas/cargar-desde-asinfo", {
            "fecha": hoy_iso, "numero": f"001-001-{NUMF_BASE + 201:09d}",
            "tipo": "FACTURA", "codigo_cli": CLI, "kg": "30", "usd": "999.99",
            "id_direccion_empresa": str(DIR_SEMBRADA),
        }, nota="una factura"))
        b.agregar(postear_cazando(c, caza, "/facturas/cargar-desde-asinfo", {
            "fecha": "", "codigo_cli": "", "usd": "0",
        }, nota="faltan datos: redirige con flash"))
        b.agregar(postear_cazando(c, caza, "/facturas/cargar-desde-asinfo-bulk", {
            "rows_json": json.dumps([
                {"fecha": hoy_iso, "numero": f"001-001-{NUMF_BASE + 202:09d}",
                 "tipo": "FACTURA", "codigo_cli": CLI, "kg": "10", "usd": "500.00"},
                {"fecha": hoy_iso, "numero": f"001-001-{NUMF_BASE + 203:09d}",
                 "tipo": "NOTA", "codigo_cli": CLI_SUC, "kg": "2", "usd": "120.00"},
                {"fecha": "", "numero": "", "codigo_cli": "", "usd": "0"},
            ]),
        }, nota="dos filas buenas y una incompleta"))
        b.agregar(postear_cazando(c, caza, "/facturas/cargar-desde-asinfo-bulk", {
            "rows_json": "[]",
        }, nota="nada seleccionado"))

        # -- CSV -------------------------------------------------------------
        b.agregar(postear_cazando(c, caza, "/facturas/cargar-csv", {
            "archivo": (io.BytesIO(_csv_facturas(CLI, hoy)), "facturas.csv"),
        }, nota="CSV con una fila válida"))
        b.agregar(postear_cazando(c, caza, "/facturas/cargar-csv", {},
                          nota="sin archivo: redirige con flash"))

        # -- Sucursales por dirección ---------------------------------------
        b.agregar(postear_cazando(c, caza, "/facturas/sucursal-direccion/agregar", {
            "id_direccion": str(DIR_NUEVA), "codigo_cli": CLI_SUC,
            "nota": "alta por el smoke",
        }, nota="alta"))
        b.agregar(postear_cazando(c, caza, "/facturas/sucursal-direccion/agregar", {
            "id_direccion": str(DIR_NUEVA), "codigo_cli": "ZZZ",
        }, nota="cliente inexistente: no se registra"))
        b.agregar(postear_cazando(c, caza, "/facturas/sucursal-direccion/agregar", {
            "id_direccion": "no-es-un-numero", "codigo_cli": CLI_SUC,
        }, nota="id_direccion no numérico"))
        b.agregar(postear_cazando(c, caza, "/facturas/sucursal-direccion/borrar", {
            "id_direccion": str(DIR_SEMBRADA),
        }, nota="baja"))
        b.agregar(postear_cazando(c, caza, "/facturas/sucursal-direccion/borrar", {
            "id_direccion": str(DIR_SEMBRADA),
        }, nota="segunda vez: no estaba registrada"))

        # -- Recode a sucursal ----------------------------------------------
        b.agregar(postear_cazando(c, caza, "/facturas/sucursales/recode/aplicar", {},
                          nota="sin confirmar: no mueve nada"))
        b.agregar(postear_cazando(c, caza, "/facturas/sucursales/recode/aplicar",
                          {"confirmo": "si"}, nota="confirmado"))
        b.agregar(postear_cazando(c, caza, "/facturas/sucursales/recode/deshacer", {},
                          nota="vuelve todo a la matriz"))

        # -- Aliases (sólo se borran: el alta se retiró el 30/07 — cuando
        #    Asinfo manda todo bajo un código, eso es una SUCURSAL y se
        #    resuelve por la dirección de entrega, no por un alias) ----------
        b.agregar(postear_cazando(c, caza, "/facturas/aliases/borrar", {
            "codigo_asinfo": ALIAS_ASINFO, "codigo_pc": CLI,
        }, nota="alias existente: lo borra"))
        b.agregar(postear_cazando(c, caza, "/facturas/aliases/borrar", {
            "codigo_asinfo": "QQQ", "codigo_pc": "WWW",
        }, nota="alias inexistente"))

        # -- Retenciones ------------------------------------------------------
        b.agregar(postear_cazando(c, caza, "/facturas/aplicar-retenciones-asinfo", {
            "desde": desde_iso, "hasta": hoy_iso,
        }, nota="todo el período"))
        b.agregar(postear_cazando(c, caza, "/facturas/aplicar-retenciones-asinfo-seleccion", {
            "desde": desde_iso, "hasta": hoy_iso,
            "numeros": f"001-001-{ids['con_ret_numf']:09d}",
        }, nota="una retención tildada"))
        b.agregar(postear_cazando(c, caza, "/facturas/aplicar-retenciones-asinfo-seleccion", {
            "desde": desde_iso, "hasta": hoy_iso,
        }, nota="nada tildado"))
        b.agregar(postear_cazando(c, caza, "/facturas/deshacer-retenciones-asinfo", {
            "desde": desde_iso, "hasta": hoy_iso,
        }, nota="deshace el período"))
        b.agregar(postear_cazando(c, caza, "/facturas/mover-retencion-del-abono", {
            "desde": desde_iso, "hasta": hoy_iso,
            "numeros": f"001-001-{ids['con_ret_numf']:09d}",
        }, nota="saca la retención de adentro del abono"))
        b.agregar(postear_cazando(c, caza, "/facturas/mover-retencion-del-abono", {
            "desde": desde_iso, "hasta": hoy_iso,
        }, nota="nada tildado"))
        b.agregar(postear_cazando(c, caza, "/facturas/retenciones-asinfo/corregir-doble", {},
                          nota="desduplica el doble conteo del dBase"))

        # -- Vigía de anuladas en Asinfo --------------------------------------
        b.agregar(postear_cazando(c, caza, "/facturas/anuladas-asinfo/correr", {"dias": "7"},
                          nota="ventana explícita"))
        b.agregar(postear_cazando(c, caza, "/facturas/anuladas-asinfo/correr", {},
                          nota="ventana por defecto"))

        # -- Destructivas, al final -------------------------------------------
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['parcial']}/anular",
                          {"motivo": "smoke: error de carga"}, nota="con motivo"))
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['parcial']}/anular", {},
                          nota="segunda vez: ya anulada"))
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['borrable']}/reversar-carga", {},
                          nota="borra la fila (carga deshecha)"))
        b.agregar(postear_cazando(c, caza, f"/facturas/{ids['z']}/reversar-carga", {},
                          nota="con abono cargado: no se puede borrar"))

        # -- Las pantallas de DESTINO, ya con los datos que el barrido dejó --
        # Casi todas las rutas de arriba contestan 302 y el smoke se quedaría
        # en el redirect. Pero el 500 de la conciliación del 13/08 —una clave
        # que faltaba en el dict que iba al template— vivía en la pantalla de
        # DESTINO, no en el POST. Y estas pantallas el smoke viejo las abre con
        # la base VACÍA: acá se renderizan con facturas, retenciones y
        # sucursales de verdad, que es cuando los bucles del template corren.
        for destino, nota in (
            ("/facturas", "listado (destino de reversar-carga)"),
            ("/facturas/desde-asinfo", "destino de las cargas y los aliases"),
            ("/facturas/sucursales/recode", "destino del recode"),
            ("/facturas/retenciones-asinfo", "destino de la selección tildada"),
            ("/facturas/retenciones-en-abono", "destino de mover-retención"),
            ("/facturas/anuladas-asinfo", "destino del vigía"),
            ("/facturas/cargar-csv", "destino del alta masiva"),
            ("/facturas/backfill-asinfo", "pantalla del backfill"),
            ("/facturas/totales-hoy.json", "el JSON de la campanita"),
        ):
            b.agregar(getear_cazando(c, caza, destino, nota=nota))

    # Un 403 CON permisos `*` sería un permiso mal puesto, no un bug de la
    # vista: se anota, no rompe el barrido (ver `app_smoke`).
    prohibidos = [r for r in b.resultados if r.status == 403]
    if prohibidos:  # pragma: no cover - hallazgo, no camino normal
        print("\n403 con permisos completos (revisar el permiso de la ruta):")
        for r in prohibidos:
            print(f"  · {r}")

    caza_cm.__exit__(None, None, None)


    assert not b.rotos, b.reporte()
