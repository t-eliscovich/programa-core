"""POST smoke de `modules/bancos` — apretar los botones, no sólo abrir la pantalla.

POR QUÉ EXISTE
--------------
`test_routes_smoke.py` recorre las rutas GET con la base vacía y un stub de
`db`: prueba que la pantalla ABRE. El 14/08/2026 medimos que de las 60 rutas
que aceptan POST en facturas/cheques/bancos los tests posteaban a 11 — y a
bancos, a UNA sola. `modules/bancos/views.py` quedaba en 20% de cobertura.

El 500 de la conciliación del 13/08 —una clave que faltaba en el dict que iba
al template— es exactamente la clase de bug que se ve sólo posteando: la
pantalla abría perfecto, el botón reventaba.

Y hay ~800 líneas de `modules/bancos/queries.py` que no nombraba ningún test y
que son justo las que mueven plata sin vuelta: `reversar_cheque_emitido`,
`mover_movimiento_banco`, `transferir_entre_bancos`, `reversar_transferencia`,
`reversar_movimiento_simple`, `restaurar_movimiento_papelera`. Cubrir las rutas
que las llaman es el punto de este archivo.

CÓMO ESTÁ ARMADO
----------------
Un solo test que hace un BARRIDO: postea las 16 rutas POST (las cuatro más
gordas con varias variantes) más las 7 GET con parámetro que el smoke viejo
saltea, junta todo en un `Barrido` y afirma al final. Que falle de a una
obliga a arreglar-correr-arreglar; el barrido entero en un mensaje deja ver si
lo roto es UNA ruta o un PATRÓN. [[feedback_espejo_clasificador_compartido]]

Lo único que se persigue es el 500. Un 400/404/409/302-con-flash es la vista
rebotando bien un payload incompleto, y eso está OK (ver `OK_ESPERADOS`).
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _smoke_base import (  # noqa: E402
    Barrido,
    CazaTragadas,
    app_smoke,
    conexion,
    db_smoke,
    getear,
    postear,
)

DB = db_smoke()

# Los dos bancos que la casa opera de verdad. El nombre importa: tanto
# `bancos_operativos()` como `no_banco_pichincha()` filtran por LIKE '%PICHINC%'
# — si el seed los llamara "BANCO A"/"BANCO B" la mitad de las pantallas
# quedaría sin bancos y el barrido sería falso (todo rebotaría en "elegí banco").
NB_PICHINCHA = 10
NB_INTERNACIONAL = 11

HOY = date.today()


def _tx(cur, *, no_banco, fecha, documento, importe, concepto,
        usuario_crea="smoke", stat=None, prov=None, numreferencia=None):
    """Inserta una fila de banco y devuelve su id.

    Sembramos por SQL a propósito: acá no estamos operando producción, estamos
    construyendo el escenario mínimo para poder apretar el botón. El botón sí
    se aprieta por la ruta real. [[operar-por-la-ui]]

    `usuario_crea` no es decorativo: las rutas de eliminar y mover cortan si la
    fila viene del sync del dBase (`dbf-import`, `asinfo-carga`…). Con el
    usuario en blanco —el default de un INSERT descuidado— TODO el barrido
    rebotaría en "esto viene del dBase" y no cubriría nada.
    """
    cur.execute(
        """
        INSERT INTO scintela.transacciones_bancarias
            (no_banco, fecha, documento, importe, concepto, saldo, stat, prov,
             numreferencia, usuario_crea)
        VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s, %s)
        RETURNING id_transaccion
        """,
        (no_banco, fecha, documento, importe, concepto, stat, prov,
         numreferencia, usuario_crea),
    )
    return cur.fetchone()["id_transaccion"]


def _mov_doble(cur, *, tipo, origen_id, destino_id, importe, fecha,
               concepto="", metadata="{}", destino_table="transacciones_bancarias"):
    cur.execute(
        """
        INSERT INTO scintela.mov_doble
            (fecha_operacion, tipo, origen_table, origen_id, destino_table,
             destino_id, importe, concepto, usuario, estado, metadata)
        VALUES (%s, %s, 'transacciones_bancarias', %s, %s, %s, %s, %s,
                'smoke', 'activo', %s::jsonb)
        RETURNING id_mov_doble
        """,
        (fecha, tipo, origen_id, destino_table, destino_id, importe,
         concepto, metadata),
    )
    return cur.fetchone()["id_mov_doble"]


def _sembrar(conn) -> dict:
    """El escenario mínimo para que las rutas no mueran todas en "no existe".

    UN OBJETO POR RUTA DESTRUCTIVA. Las rutas de reverso/borrado CONSUMEN lo
    que tocan: si el barrido compartiera un solo cheque, el primer reverso le
    sacaría el escenario al siguiente y los demás contestarían "ya fue
    reversado" — verde, y sin haber ejecutado una línea de la query que
    interesa. Por eso hay un cheque para el preview, otro para el reverso, una
    fila para eliminar y otra para mover.
    """
    cur = conn.cursor()
    ids: dict = {}

    # --- Catálogo -------------------------------------------------------
    for nb, nombre in ((NB_PICHINCHA, "PICHINCHA"), (NB_INTERNACIONAL, "INTERNACIONAL")):
        cur.execute(
            "INSERT INTO scintela.banco (no_banco, nombre, usuario_crea) "
            "VALUES (%s, %s, 'smoke') ON CONFLICT (no_banco) DO NOTHING",
            (nb, nombre),
        )
    cur.execute(
        "INSERT INTO scintela.proveedor (codigo_prov, nombre, activo, plazo, usuario_crea) "
        "VALUES ('SMK', 'PROVEEDOR SMOKE', 'S', 30, 'smoke') RETURNING id_proveedor"
    )
    ids["id_proveedor"] = cur.fetchone()["id_proveedor"]

    # Posdatados ABIERTOS (banc=0). Tres: uno para el pago single, dos para el
    # multi-select `id_posdats[]` (dueña 27/05: "dejame seleccionar múltiples").
    ids["posdats"] = []
    for i, imp in enumerate((150.00, 80.00, 45.00)):
        cur.execute(
            """
            INSERT INTO scintela.posdat
                (fecha, fechad, prov, num, importe, concepto, banc, usuario_crea)
            VALUES (%s, %s, 'SMK', %s, %s, %s, 0, 'smoke')
            RETURNING id_posdat
            """,
            (HOY - timedelta(days=40), HOY + timedelta(days=20), 9000 + i, imp,
             f"COMPRA SMOKE {i}"),
        )
        ids["posdats"].append(cur.fetchone()["id_posdat"])

    # --- Anclas de apertura ---------------------------------------------
    # Las dos filas más viejas de cada banco vienen "del dBase" a propósito:
    # `mover_movimiento_banco` aborta si la fila movida es la más vieja del
    # origen o quedaría de apertura en el destino (mover la apertura corre el
    # saldo publicado). Con estas anclas, las guardas se cumplen y el POST
    # llega a la query en vez de rebotar en el guard.
    for nb in (NB_PICHINCHA, NB_INTERNACIONAL):
        _tx(cur, no_banco=nb, fecha=date(2026, 1, 2), documento="DE",
            importe=5000, concepto="APERTURA", usuario_crea="dbf-import")
        _tx(cur, no_banco=nb, fecha=date(2026, 1, 3), documento="DE",
            importe=1000, concepto="SEGUNDA", usuario_crea="dbf-import")

    d5, d4, d3, d2 = (HOY - timedelta(days=n) for n in (5, 4, 3, 2))

    # --- Filas de trabajo, una por ruta ---------------------------------
    ids["tx_de"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d5, documento="DE",
                       importe=300, concepto="DEPOSITO SMOKE")
    ids["tx_nd"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d5, documento="ND",
                       importe=120, concepto="COMISION SMOKE", prov="SMK")
    ids["tx_numref"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d2, documento="DE",
                           importe=50, concepto="SIN NUMERO DOC")
    ids["tx_eliminar"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d4, documento="DE",
                             importe=250, concepto="A BORRAR")
    ids["tx_mover"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d4, documento="DE",
                          importe=260, concepto="BANCO EQUIVOCADO")

    # Conciliada: `toggle-conciliado` tiene DOS caminos y el que importa es el
    # de desconciliar, que llama a `matcher_banco.romper_match`. Sin un match
    # ACTIVO en la tabla ese camino no se ejecuta nunca.
    ids["tx_conciliada"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d5, documento="DE",
                               importe=400, concepto="YA CONCILIADA", stat="*")
    cur.execute(
        """
        INSERT INTO scintela.banco_conciliacion_match
            (no_banco, estado, metodo, id_transaccion, usuario)
        VALUES (%s, 'matched', 'smoke:seed', %s, 'smoke')
        RETURNING id
        """,
        (NB_PICHINCHA, ids["tx_conciliada"]),
    )
    ids["match_conciliacion"] = cur.fetchone()["id"]

    # Cheques emitidos: uno para mirar (GET preview + GET del wizard) y otro
    # para reversar de verdad. Sin mov_doble asociado → el reverso cae en la
    # rama "sin side effect", que es la que igual inserta la NC compensatoria.
    ids["tx_cheque_preview"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d3,
                                   documento="CH", importe=710,
                                   concepto="CH SOLO MIRAR", numreferencia=8001)
    ids["tx_cheque_reversar"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d3,
                                    documento="CH", importe=700,
                                    concepto="CH A REVERSAR", numreferencia=8002)

    # Movimiento simple reversable: la ND + su mov_doble tipo 'nota_debito'
    # (el mismo que graba `crear_movimiento_simple`). `reversar_movimiento_simple`
    # rechaza cualquier otro tipo.
    ids["tx_mov_simple"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d2,
                               documento="ND", importe=90,
                               concepto="ND REVERSABLE")
    ids["md_mov_simple"] = _mov_doble(
        cur, tipo="nota_debito", origen_id=ids["tx_mov_simple"],
        destino_id=ids["tx_mov_simple"], importe=90, fecha=d2,
        concepto="ND REVERSABLE",
        metadata=f'{{"no_banco": {NB_PICHINCHA}, "documento": "ND"}}',
    )

    # Transferencia YA HECHA: CH en origen + TR en destino + el mov_doble que
    # los une. Es la forma exacta que deja `transferir_entre_bancos`; sin ella
    # `/bancos/transferencia/<id>/reversar` no tiene qué reversar.
    ids["tx_transf_origen"] = _tx(cur, no_banco=NB_PICHINCHA, fecha=d2,
                                  documento="CH", importe=500,
                                  concepto="TR a INTERNACIONAL")
    ids["tx_transf_destino"] = _tx(cur, no_banco=NB_INTERNACIONAL, fecha=d2,
                                   documento="TR", importe=500,
                                   concepto="TR de PICHINCHA")
    ids["md_transferencia"] = _mov_doble(
        cur, tipo="transfer_banco_banco", origen_id=ids["tx_transf_origen"],
        destino_id=ids["tx_transf_destino"], importe=500, fecha=d2,
        concepto="PICHINCHA → INTERNACIONAL",
        metadata=f'{{"no_banco_origen": {NB_PICHINCHA}, '
                 f'"no_banco_destino": {NB_INTERNACIONAL}}}',
    )

    # Papelera: el snapshot se arma con `to_jsonb` de la fila REAL y recién
    # después se borra la fila. `restaurar_movimiento_papelera` la re-inserta
    # con `jsonb_populate_record`, así que un dict armado a mano con las claves
    # que se nos ocurran no probaría nada: tiene que tener la forma de la tabla.
    id_pap_tx = _tx(cur, no_banco=NB_PICHINCHA, fecha=d3, documento="DE",
                    importe=333, concepto="BORRADO RESTAURABLE")
    cur.execute(
        """
        INSERT INTO scintela.papelera_movimiento_banco
            (id_transaccion, no_banco, banco_nombre, documento, importe, fecha,
             concepto, prov, snapshot, borrado_por)
        SELECT t.id_transaccion, t.no_banco, 'PICHINCHA', t.documento, t.importe,
               t.fecha, t.concepto, t.prov, to_jsonb(t), 'smoke'
          FROM scintela.transacciones_bancarias t
         WHERE t.id_transaccion = %s
        RETURNING id_papelera
        """,
        (id_pap_tx,),
    )
    ids["id_papelera"] = cur.fetchone()["id_papelera"]
    cur.execute(
        "DELETE FROM scintela.transacciones_bancarias WHERE id_transaccion = %s",
        (id_pap_tx,),
    )

    conn.commit()
    return ids


def _limpiar(conn) -> None:
    """Borra lo que dejó una corrida ANTERIOR de este mismo smoke.

    El archivo se corre muchas veces mientras se lo escribe, y una segunda
    corrida sobre los restos de la primera MIENTE: los guards de duplicado y de
    "ya fue reversado" empiezan a rebotar cosas que la primera vez sí
    ejecutaron, y el barrido queda verde sin haber probado nada.

    TMT 2026-08-14 — esto borraba `transacciones_bancarias` ENTERA, con un
    guard que exigía que la base se llamara `pc_*`. En el CI hay una sola base
    (`programa_core_test`) para los tres barridos, así que ese guard cortaba la
    corrida... y sin el guard, el smoke de bancos le hubiera vaciado la mesa a
    los de facturas y cheques. Ahora cada DELETE está acotado a lo que ESTE
    archivo sembró: los bancos 10 y 11, y las filas marcadas SMOKE/SMK. Se
    borra lo propio, no la tabla.
    """
    cur = conn.cursor()
    bancos = f"({NB_PICHINCHA}, {NB_INTERNACIONAL})"
    for sql in (
        f"DELETE FROM scintela.banco_conciliacion_match WHERE no_banco IN {bancos}",
        f"DELETE FROM scintela.papelera_movimiento_banco WHERE no_banco IN {bancos}",
        "DELETE FROM scintela.mov_doble WHERE concepto LIKE '%SMOKE%' OR usuario = 'smoke'",
        "DELETE FROM scintela.chequextransaccion WHERE id_transaccion IN ("
        f"  SELECT id_transaccion FROM scintela.transacciones_bancarias WHERE no_banco IN {bancos})",
        f"DELETE FROM scintela.transacciones_bancarias WHERE no_banco IN {bancos}",
        "DELETE FROM scintela.retiros WHERE concepto LIKE '%SMOKE%'",
        "DELETE FROM scintela.caja WHERE concepto LIKE '%SMOKE%'",
        "DELETE FROM scintela.xgast WHERE concepto LIKE '%SMOKE%' OR prov LIKE 'SMK%'",
        "DELETE FROM scintela.dolares WHERE concepto LIKE '%SMOKE%' OR cta = 'MP'",
        "DELETE FROM scintela.posdat WHERE concepto LIKE '%SMOKE%' OR prov LIKE 'SMK%'",
        "DELETE FROM scintela.proveedor WHERE nombre LIKE '%SMOKE%'",
        f"DELETE FROM scintela.banco WHERE no_banco IN {bancos}",
    ):
        try:
            cur.execute(sql)
        except Exception:  # noqa: BLE001 - tabla que este dump no tiene
            conn.rollback()
    conn.commit()


@pytest.fixture(scope="module")
def escenario():
    conn = conexion(DB)
    _limpiar(conn)
    ids = _sembrar(conn)
    with app_smoke(DB) as app:
        yield app, ids
    _limpiar(conn)
    conn.close()


# ---------------------------------------------------------------------------
# El 500 que no llega a ser 500
# ---------------------------------------------------------------------------
# Casi todas las vistas de bancos terminan en `except Exception as e:
# flash_exc(...)`. Eso está bien para el usuario —ve un mensaje en castellano
# en vez de una pantalla de error— pero para un smoke es una trampa: la vista
# EXPLOTÓ y el barrido ve un 302 alegre. El 500 de la conciliación del 13/08 se
# vio porque ahí no había `except`; con uno, hubiera pasado desapercibido meses.
#
# Por eso `CazaTragadas` (en `_smoke_base`) espía `humanize()` —el único camino
# por el que una excepción se vuelve texto para el usuario— y convierte cada
# excepción tragada en un resultado ROTO, con su traceback. Espiar sólo
# `flash_exc` no alcanzaba: hay vistas que tragan devolviendo un 400, que es un
# código legítimo y estaba en `OK_ESPERADOS`.
# ---------------------------------------------------------------------------


def _vistas_bancos():
    from modules.bancos import views

    return views


def _post(c, b, caza, url, data=None, *, nota=""):
    caza.tomar()  # descarta lo que haya quedado de la ruta anterior
    r = postear(c, url, data=data, nota=nota)
    r.error = caza.tomar() or r.error
    return b.agregar(r)


def _get(c, b, caza, url, *, nota=""):
    caza.tomar()
    r = getear(c, url, nota=nota)
    r.error = caza.tomar() or r.error
    return b.agregar(r)


# ---------------------------------------------------------------------------
# Las cuatro rutas gordas, cada una con sus variantes. Están afuera del test
# para que se lean como lo que son: la lista de lo que un usuario puede
# apretar en esa pantalla.
# ---------------------------------------------------------------------------

def _variantes_emitir_cheque(ids: dict) -> list[tuple[dict, str]]:
    """206 líneas de vista + los 6 tipos de side effect de `queries.emitir_cheque`.

    Cada tipo escribe en una tabla distinta (posdat / retiros / caja / xgast /
    dolares) y esa rama sólo se ejecuta si el POST trae el `tipo` correcto.
    """
    base = {"no_banco": NB_PICHINCHA, "fecha": HOY.isoformat(),
            "beneficiario": "PROVEEDOR SMOKE", "concepto": "PAGO SMOKE"}
    return [
        ({**base, "tipo": "otro", "importe": "100,00", "no_cheque": "5001"},
         "tipo=otro, el caso sin side effect"),
        ({**base, "tipo": "proveedor", "importe": "150,00", "no_cheque": "5002",
          "id_posdat": str(ids["posdats"][0])},
         "tipo=proveedor contra UNA posdat (id_posdat legacy)"),
        ({**base, "tipo": "proveedor", "importe": "125,00", "no_cheque": "5003",
          "id_posdats[]": [str(ids["posdats"][1]), str(ids["posdats"][2])]},
         "tipo=proveedor contra DOS posdats (multi-select, dueña 27/05)"),
        ({**base, "tipo": "retiro", "importe": "200,00", "no_cheque": "5004",
          "de_socio": "TM"},
         "tipo=retiro de socio → INSERT en retiros"),
        ({**base, "tipo": "caja", "importe": "60,00", "no_cheque": "5005"},
         "tipo=caja → entrada de caja"),
        ({**base, "tipo": "gasto", "importe": "45,00", "no_cheque": "5006",
          "xgast_num": "3"},
         "tipo=gasto con xgast_num=3 (sin él el gasto queda invisible en V1..V9)"),
        # 🔎 OJO con el largo: `queries.emitir_cheque` hace
        # `cta_usd = beneficiario[:5]` y `scintela.dolares.cta` es
        # varchar(3) — un código de 4 o 5 letras revienta con
        # StringDataRightTruncation y el `except` lo convierte en un flash.
        # Acá va el código de 2 letras, que es lo que la casa usa.
        ({**base, "tipo": "anticipo_usd", "importe": "500,00", "no_cheque": "5007",
          "beneficiario": "MP"},
         "tipo=anticipo_usd → fila en dolares. `beneficiario` acá NO es un "
         "nombre: es el código de cuenta USD de 2 letras (paridad BANCOS.PRG "
         "> CHEQUERA con concepto IN.<CT>)"),
        ({**base, "tipo": "otro", "importe": "75,00", "no_cheque": "5008",
          "es_postdatado": "1", "fechad": (HOY + timedelta(days=30)).isoformat()},
         "cheque POSTDATADO (fechad futura)"),
        ({**base, "tipo": "otro", "importe": "0", "no_cheque": "5009"},
         "importe cero — tiene que rebotar con flash, no explotar"),
        # ⭐ TMT 2026-08-24 — la NOTA DE DÉBITO se emite por esta misma
        # pantalla, sin N° de cheque (dueña). Mismos destinos que el cheque.
        ({**base, "tipo": "otro", "importe": "31,00", "documento": "ND",
          "concepto": "ND SMOKE"},
         "documento=ND sin número → nota de débito, mismo camino que el cheque"),
        ({**base, "tipo": "gasto", "importe": "18,00", "documento": "ND",
          "concepto": "COMISIONES", "xgast_num": "9"},
         "ND + gasto V9 (dueña 30/07: 'si hago ND no puedo mandar a gasto')"),
        ({**base, "tipo": "retiro", "importe": "40,00", "documento": "ND",
          "de_socio": "TM", "concepto": "RETIRO SMOKE"},
         "ND ruteada a RETIRO"),
        ({**base, "tipo": "anticipo_usd", "importe": "88,00", "documento": "ND",
          "beneficiario": "MP", "concepto": "ANTICIPO SMOKE"},
         "ND como anticipo a proveedor (la fila en dolares, cuenta de 2 letras)"),
        ({**base, "tipo": "posdato", "importe": "35,00", "documento": "ND",
          "beneficiario": "SM", "concepto": "A POSDATO"},
         "tipo=posdato → posdat negativa a 120 días (el viejo INOP)"),
        ({**base, "tipo": "proveedor", "importe": "55,00", "no_cheque": "5010",
          "beneficiario": "SMK"},
         "tipo=proveedor SIN posdat → graba la compra (pago directo)"),
        ({**base, "tipo": "otro", "importe": "31,00", "documento": "ND",
          "concepto": "ND SMOKE"},
         "la misma ND de nuevo → freno de repetido, pregunta y no graba"),
        ({**base, "tipo": "otro", "importe": "31,00", "documento": "ND",
          "concepto": "ND SMOKE", "permitir_duplicado": "1"},
         "la misma ND confirmada → son dos de verdad, entra"),
    ]


def _variantes_nuevo_movimiento() -> list[tuple[str, dict, str]]:
    """Depósito y nota de crédito, la re-pregunta de fecha vieja (07/08: los
    movimientos de Alex cargados con fecha de anteayer) y el override de
    duplicado.

    TMT 2026-08-24 — la NOTA DE DÉBITO se fue de esta pantalla a la de emitir
    (dueña: *"tiene que ser igual que emitir cheque, misma pantalla"*), así que
    sus destinos (retiro / caja / posdato / anticipo) se ejercitan ahora en
    `_variantes_emitir_cheque`. Acá queda una sola ND, para que el redirect que
    cubre los links viejos no se despinte."""
    base = {"no_banco": str(NB_PICHINCHA), "importe": "123,45",
            "fecha": HOY.isoformat(), "concepto": "MOV SMOKE"}
    vieja = (HOY - timedelta(days=6)).isoformat()
    return [
        ("DE", {**base, "documento": "DE"}, "depósito del día"),
        ("NC", {**base, "documento": "NC", "importe": "77,00",
                "concepto": "NC SMOKE"}, "nota de crédito"),
        ("DE", {**base, "documento": "DE", "fecha": vieja,
                "concepto": "DE FECHA VIEJA"},
         "fecha vieja SIN confirmar → re-pregunta, no graba"),
        ("DE", {**base, "documento": "DE", "fecha": vieja,
                "concepto": "DE FECHA VIEJA", "confirmar_fecha": "1"},
         "fecha vieja CONFIRMADA → graba"),
        ("DE", {**base, "documento": "DE", "concepto": "MOV SMOKE"},
         "mismo concepto+importe del primero → guard anti-duplicado"),
        ("DE", {**base, "documento": "DE", "concepto": "MOV SMOKE",
                "permitir_duplicado": "1"},
         "el mismo, con permitir_duplicado=1 → repetido legítimo"),
        ("DE", {**base, "documento": "DE", "fecha": vieja,
                "concepto": "DE CON USAR HOY", "usar_hoy": "1"},
         "botón 'No, ponele la de hoy' (name propio: con `fecha` el form.get "
         "devolvería el primero) [[feedback_name_repetido_get_devuelve_el_primero]]"),
        ("ND", {**base, "documento": "ND", "importe": "31,00",
                "concepto": "ND SMOKE"},
         "la ND ya no se carga acá: redirige a /bancos/emitir-cheque?doc=ND "
         "(TMT 2026-08-24, misma pantalla que el cheque)"),
        ("XX", {**base, "documento": "XX"},
         "documento inválido → redirect a /bancos"),
    ]


def _variantes_reencadenar() -> list[tuple[dict, str]]:
    """182 líneas y la única ruta que REESCRIBE la columna `saldo` de muchas
    filas a la vez. El dry-run (sin `aplicar`) y el aplicado son dos caminos
    distintos; el modo importa porque 'atras' ancla en el cierre y 'adelante'
    lo MUEVE — que fue justo lo que se abortó el 03/08."""
    desde = (HOY - timedelta(days=10)).isoformat()
    return [
        ({"no_banco": str(NB_PICHINCHA)},
         "dry-run modo default (atras): muestra el plan, no escribe"),
        ({"no_banco": str(NB_PICHINCHA), "modo": "adelante", "desde": desde},
         "dry-run modo adelante con fecha ancla"),
        ({"no_banco": str(NB_PICHINCHA), "aplicar": "1"},
         "APLICA hacia atrás — el cierre queda invariante por construcción "
         "y la apertura se asienta en la misma transacción"),
        ({"no_banco": str(NB_PICHINCHA), "aplicar": "1", "apertura": "6000"},
         "Aplicar AFIRMANDO una apertura que no es: la guarda espejo del "
         "12/05 (un walk sin ancla dejó Pichincha en −917.651,96) tiene que "
         "abortar, no escribir"),
        ({"no_banco": str(NB_PICHINCHA), "modo": "adelante", "desde": desde,
          "aplicar": "1"},
         "APLICA hacia adelante desde una fecha"),
        ({"no_banco": str(NB_PICHINCHA), "modo": "adelante", "desde": "31/12/2026"},
         "fecha en formato de pantalla → 'usá AAAA-MM-DD', no un 500"),
        ({}, "sin banco → la pantalla vacía"),
    ]


def _variantes_transferir() -> list[tuple[dict, str]]:
    """El wizard de 2 pasos: sin `confirmado` muestra el preview de saldos,
    con `confirmado=1` mueve la plata. El bug C del 16/05 vivía justo en el
    paso 2 (los hidden no se renderizaban y el confirm llegaba vacío)."""
    base = {"no_banco_origen": str(NB_PICHINCHA),
            "no_banco_destino": str(NB_INTERNACIONAL),
            "importe": "150,00", "fecha": HOY.isoformat(),
            "concepto": "TRANSFER SMOKE"}
    return [
        (base, "paso 2: sin confirmar → wizard con saldo_preview"),
        ({**base, "confirmado": "1"}, "paso 3: confirmado → mueve la plata"),
        ({**base, "no_banco_destino": str(NB_PICHINCHA), "confirmado": "1"},
         "origen == destino → tiene que rebotar con flash"),
        ({**base, "importe": "-10", "confirmado": "1"},
         "importe negativo → rebota"),
    ]


@pytest.mark.db
def test_post_smoke_bancos(escenario):
    """Barrido: las 16 rutas POST + las 7 GET con parámetro, todas de una."""
    app, ids = escenario
    c = app.test_client()
    b = Barrido("bancos")
    nb = NB_PICHINCHA
    d30 = (HOY - timedelta(days=30)).isoformat()

    with CazaTragadas(_vistas_bancos()) as caza:
        # =================================================================
        # 1) GET con parámetro — van PRIMERO porque no consumen nada y
        #    porque si la pantalla de confirmación no abre, el POST que le
        #    sigue tampoco dice mucho.
        # =================================================================
        _get(c, b, caza, f"/bancos/{nb}", nota="lista de movimientos (366 líneas)")
        _get(c, b, caza,
             f"/bancos/{nb}?desde={d30}&hasta={HOY.isoformat()}&conciliado=1"
             f"&monto=400&doc_num=8001&cliente=SMK",
             nota="la misma con TODOS los filtros puestos")
        # Los dos exports viven DENTRO de la misma vista de 366 líneas y no
        # se ejercitan nunca: con la base vacía `filas` es [] y el `if ... and
        # filas` ni entra.
        _get(c, b, caza, f"/bancos/{nb}?export=csv", nota="export CSV de movimientos")
        _get(c, b, caza, f"/bancos/{nb}?export=xlsx", nota="export XLSX de movimientos")
        _get(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_eliminar']}/eliminar",
             nota="confirmación de baja")
        _get(c, b, caza, f"/bancos/mov-simple/{ids['md_mov_simple']}/reversar",
             nota="wizard de reverso de mov simple")
        _get(c, b, caza, f"/bancos/transferencia/{ids['md_transferencia']}/reversar",
             nota="wizard de reverso de transferencia")
        _get(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_mover']}/mover",
             nota="selector de banco destino")
        _get(c, b, caza, f"/bancos/cheque-emitido/{ids['tx_cheque_reversar']}/reversar",
             nota="wizard de reverso de cheque emitido")
        _get(c, b, caza,
             f"/bancos/cheque-emitido/{ids['tx_cheque_preview']}/reverso-preview",
             nota="preview del reverso (el JSON del modal)")

        # =================================================================
        # 2) Las cuatro gordas, con variantes.
        # =================================================================
        for data, nota in _variantes_emitir_cheque(ids):
            _post(c, b, caza, "/bancos/emitir-cheque", data, nota=nota)

        for doc, data, nota in _variantes_nuevo_movimiento():
            _post(c, b, caza, f"/bancos/nuevo-movimiento?doc={doc}", data, nota=nota)

        # Antes del recompute la cadena está torcida (los saldos sembrados no
        # cierran): es el escenario en el que la guarda espejo levanta
        # `AperturaDistintaError` y la pantalla, en vez de fallar, PREGUNTA
        # cuál es la apertura. Ese camino sólo existe con la cadena rota.
        _post(c, b, caza, "/bancos/reencadenar", {"no_banco": str(nb)},
              nota="dry-run con la cadena rota → apertura_conflicto")

        # ORDEN A PROPÓSITO: el recompute walk-forward deja la cadena de
        # saldos coherente. Sin él, `reencadenar_retro` levanta siempre
        # `AperturaDistintaError` (la guarda espejo del 12/05) y el Aplicar
        # —que es el único camino que ESCRIBE— no se ejecuta nunca: el
        # barrido se vería verde sin haber tocado esas 24 líneas.
        _post(c, b, caza, "/bancos/recompute-saldos", {},
              nota="recompute global de saldos (deja la cadena coherente)")

        for data, nota in _variantes_reencadenar():
            _post(c, b, caza, "/bancos/reencadenar", data, nota=nota)

        for data, nota in _variantes_transferir():
            _post(c, b, caza, "/bancos/transferir", data, nota=nota)

        # =================================================================
        # 3) El resto de los POST. Cada uno sobre SU objeto sembrado.
        # =================================================================
        _post(c, b, caza, "/bancos/cargar/agregar",
              {"fecha": [HOY.isoformat(), HOY.isoformat()],
               "tipo": ["DE", "ND"],
               "importe": ["10,50", "20,25"],
               "concepto": ["CARGA BULK 1", "CARGA BULK 2"],
               "doc_banco": ["DOC1", "DOC2"]},
              nota="carga multi-línea, 2 filas (all-or-nothing)")
        _post(c, b, caza, "/bancos/cargar/agregar",
              {"fecha": [HOY.isoformat()], "tipo": ["XX"], "importe": ["5"],
               "concepto": ["TIPO MALO"], "doc_banco": [""]},
              nota="tipo inválido → no se carga NADA")

        _post(c, b, caza, "/bancos/apertura",
              {"no_banco": str(nb), "saldo_apertura": "1.234,56", "nota": "smoke"},
              nota="fija la apertura (formato EU: punto=miles, coma=decimales) "
                   "[[feedback_formato_numerico_eu_ecuador]]")
        _post(c, b, caza, "/bancos/apertura",
              {"no_banco": str(nb), "saldo_apertura": ""},
              nota="apertura sin importe → flash de error")

        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_numref']}/set-numreferencia",
              {"numreferencia": "7777"},
              nota="N° de documento manual (sobrevive al sync del dBase)")
        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_numref']}/set-numreferencia",
              {"numreferencia": ""}, nota="borrar el N° de documento")

        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_nd']}/clasificar-gasto",
              {"num": "5", "concepto": "COMISION SMOKE", "prov": "SMK"},
              nota="ND del banco → gasto V5 (NO toca caja: la plata ya salió "
                   "por el banco)")
        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_nd']}/clasificar-gasto",
              {"num": "5"}, nota="el mismo otra vez → 'ya está clasificado'")
        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_de']}/clasificar-gasto",
              {"num": "99"}, nota="categoría fuera de V1..V9 → rebota")

        # Toggle: primero DESCONCILIAR la que tiene match activo (pasa por
        # matcher_banco.romper_match), después CONCILIAR una que no lo estaba.
        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_conciliada']}/toggle-conciliado",
              {"desde": d30, "hasta": HOY.isoformat()},
              nota="desconciliar (rompe el match activo)")
        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_de']}/toggle-conciliado", {},
              nota="conciliar a mano (stat='*', sin extracto)")

        _post(c, b, caza, f"/bancos/papelera/{ids['id_papelera']}/restaurar", {},
              nota="restaurar de la papelera (retención 30 días)")
        _post(c, b, caza, f"/bancos/papelera/{ids['id_papelera']}/restaurar", {},
              nota="restaurar dos veces → 'ya fue restaurado'")

        # =================================================================
        # 4) Las destructivas, al final: consumen su objeto.
        # =================================================================
        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_mover']}/mover",
              {"no_banco_destino": str(NB_INTERNACIONAL)},
              nota="mover de banco (caso ch2825 CG3, dueña 22/07)")

        _post(c, b, caza,
              f"/bancos/cheque-emitido/{ids['tx_cheque_reversar']}/reversar",
              {"motivo": "smoke test", "next": f"/bancos/{nb}"},
              nota="reversar cheque emitido → NC compensatoria (NO ND: 'ND' "
                   "restaba el saldo otra vez en lugar de devolverlo)")
        _post(c, b, caza,
              f"/bancos/cheque-emitido/{ids['tx_cheque_reversar']}/reversar",
              {"motivo": "otra vez"},
              nota="reversar el MISMO cheque → 'ya fue reversado'")
        _post(c, b, caza,
              f"/bancos/cheque-emitido/{ids['tx_cheque_reversar']}/reversar", {},
              nota="reverso SIN motivo → acá el motivo sí es obligatorio")

        _post(c, b, caza, f"/bancos/mov-simple/{ids['md_mov_simple']}/reversar",
              {"motivo": "smoke test"},
              nota="reversar mov simple (ND → NC). El SELECT pedía `fecha` y la "
                   "columna es `fecha_operacion`: estuvo roto desde el día 1")
        _post(c, b, caza, f"/bancos/mov-simple/{ids['md_mov_simple']}/reversar",
              {"motivo": "otra vez"}, nota="el mismo mov_doble, ya reversado")

        _post(c, b, caza,
              f"/bancos/transferencia/{ids['md_transferencia']}/reversar",
              {"motivo": "smoke test"},
              nota="reversar transferencia (compensa los DOS lados)")
        _post(c, b, caza,
              f"/bancos/transferencia/{ids['md_transferencia']}/reversar",
              {"motivo": "otra vez"}, nota="la misma transferencia ya reversada")

        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_eliminar']}/eliminar", {},
              nota="eliminar movimiento PC (snapshot a la papelera)")
        _post(c, b, caza, f"/bancos/{nb}/tx/{ids['tx_eliminar']}/eliminar", {},
              nota="eliminar el mismo → 404, ya no está")

    # Un 403 con permisos `*` no puede pasar: querría decir que la ruta pide un
    # permiso que ni el accionista tiene. Se chequea aparte del 500 porque el
    # arreglo es otro (la tabla `seguridad.permiso`, no el código de la vista).
    prohibidas = [r for r in b.resultados if r.status == 403]
    assert not prohibidas, (
        "Rutas que contestaron 403 CON permisos completos:\n"
        + "\n".join(f"  {r}" for r in prohibidas)
    )
    assert not b.rotos, b.reporte()


@pytest.mark.db
def test_pantallas_de_bancos_con_datos(escenario):
    """Las pantallas sin parámetro que el smoke viejo sí abría — pero con la
    base VACÍA, que es casi lo mismo que no abrirlas: ningún `{% for %}` daba
    una vuelta. Con el escenario sembrado los templates recorren filas de
    verdad, que es donde vive el `Undefined` que no se ve con cero filas."""
    app, ids = escenario
    c = app.test_client()
    b = Barrido("bancos-pantallas")
    with CazaTragadas(_vistas_bancos()) as caza:
        for url, nota in (
            ("/bancos", "índice con dos bancos y sus saldos"),
            ("/bancos/papelera", "papelera con filas"),
            ("/bancos/cargar", "carga multi-línea"),
            ("/bancos/apertura", "panel de aperturas"),
            ("/bancos/transferir", "form de transferencia"),
            ("/bancos/emitir-cheque", "wizard de cheque con posdats abiertas"),
            (f"/bancos/emitir-cheque?id_posdat={ids['posdats'][0]}",
             "wizard entrado desde el botón Pagar de /posdat"),
            ("/bancos/emitir-cheque?tipo=anticipo_usd&no_banco=%d" % NB_PICHINCHA,
             "wizard con tipo y banco pre-seleccionados"),
            ("/bancos/emitir-cheque?doc=ND", "form de nota de débito"),
            ("/bancos/reencadenar", "dry-run vacío"),
            ("/bancos/cheque-imprimible?importe=1234.56&beneficiario=SMOKE",
             "impresión del cheque con el monto EN LETRAS"),
            ("/bancos/_api/preview-concepto?concepto=RR TM prueba&documento=ND",
             "preview del ruteo del concepto"),
        ):
            _get(c, b, caza, url, nota=nota)
    assert not b.rotos, b.reporte()
