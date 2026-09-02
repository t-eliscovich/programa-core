"""El cuadre del día: lo que SALIÓ por la puerta contra lo que se FACTURÓ.

TMT 2026-08-18 (dueña), mirando el pin de la campanita: *"¿por qué hay más
facturado que despachado?"*. Los dos números viven uno al lado del otro desde
el 13/08 y no cierran casi nunca — porque son dos hechos distintos:

  · **Despachado** = kilos de las GUÍAS de despacho a cliente con fecha de hoy
    (bodega 53 = Producto Terminado, sin anular). Es Asinfo, la mercadería.
  · **Facturado**  = kilos de los DOCUMENTOS de hoy cargados en Programa Core
    (facturas + notas de entrega, kg > 0). Es nuestra base, el papel.

La mercadería va adelante del documento, así que la diferencia es normal. Esta
pantalla no la disimula: la **descompone**, y el cuadre cierra por aritmética:

    Despachado
      + facturado hoy que NO salió por una guía de hoy      (A)
      − despachado hoy que TODAVÍA no se facturó            (B)
      = Facturado

Medido el 18/08/2026: despachado 19.469,06 kg en 131 guías; facturado
19.552,51 kg en 118 documentos; A = 83,45 kg (UNA factura, la 181963 de POS,
tres telas Alemania 1.2 sin guía asociada); B = 0. El residuo dio 0,00.

⭐ **Los kilos de cada documento salen de NUESTRA base, no de Asinfo.** El
detalle de Asinfo trae renglones que no son kilos (unidades sueltas que suman
1,00 en `cantidad`): sumarlos daba 21.791,41 kg contra los 19.552,51 del pin, y
dos pantallas de la misma app diciendo cosas distintas es peor que no tener la
pantalla. A Asinfo se le pregunta sólo lo que sólo él sabe: **de qué guía viene
cada kilo**. El reparto por documento es `kg de PC − kg ligados a una guía de
hoy`, así que los totales no pueden despegarse del recuadro del inicio.

⭐ **Una NOTA DE ENTREGA también se nombra.** Hasta el 19/08 la pantalla las
salteaba enteras al armar "facturado sin guía de hoy", porque se creía que una
NTEN no vivía en `factura_cliente`. Vive. La que no tenía guía de ese día (la
NTEN-10879 de BED, 265,50 kg) salía por la fila "± sin identificar" — el número
correcto y ningún nombre, que es justo lo que esa fila no tiene que hacer.

Fail-soft: si Asinfo no contesta, el lado propio se muestra igual y el cuadre
se marca incompleto. Nunca levanta.
"""
from __future__ import annotations

import logging
import re

import db

_LOG = logging.getLogger("programa_core.facturas.dia_despacho")

#: Producto Terminado. El mismo de `despacho_fisico_dia_info`, para que el
#: total de esta pantalla sea EL número del pin y no un primo lejano.
BODEGA_PT = 53

#: Debajo de esto un renglón es ruido de redondeo, no un caso.
UMBRAL_KG = 0.05

#: Los estados de `factura_cliente` que el programa SÍ importa. Es la misma
#: lista que el WHERE de la card 199 de Metabase (`fc.estado IN (1, 4, 16)`),
#: escrita acá para poder DECIRLO en la pantalla.
#:
#: 🚨 TMT 2026-08-24 (dueña): *"me suena raro porque van más de 5 mins"*, con
#: la 001-099-000182519 en ámbar. No era la carga: Asinfo la tenía en estado
#: **15**, que no está en esa lista, así que el programa ni la ve. Decirle
#: "falta cargarla" era acusar a la carga de algo que no hizo. De las seis
#: facturas que estuvieron alguna vez en estado 15, cuatro son del 16/01/2025
#: y NUNCA salieron de ahí: puede destrabarse solo, y puede quedarse trabado.
ESTADOS_IMPORTABLES = (1, 4, 16)


def _dia(fecha) -> str:
    """'YYYY-MM-DD' o ValueError. La fecha va como LITERAL en el SQL de Asinfo
    (ver `despacho_fisico_dia_info`: `GETDATE()` está en UTC y correría el día
    cinco horas), así que se valida ACÁ y no se confía en el que llama.

    ⭐ El texto se valida ENTERO, no recortado a 10. Con un `[:10]` previo,
    `"2026-08-18'; DROP…"` pasaba el filtro —el recorte se comía la cola— y la
    función devolvía una fecha buena sin decir que le habían mandado basura.
    No llegaba a ser inyección, pero un validador que acepta lo que no entiende
    no es un validador.
    """
    if hasattr(fecha, "isoformat"):        # date / datetime
        return fecha.isoformat()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fecha)):
        raise ValueError(f"fecha inválida: {fecha!r}")
    return str(fecha)


def _guias(dia: str) -> list[dict]:
    """Las guías de despacho del día, con su cliente y si ya se facturaron."""
    from modules._lib import metabase_client

    sql = f"""
        SELECT dc.numero                                     AS guia,
               CONVERT(varchar(16), dc.fecha_creacion, 120)  AS creado,
               ISNULL(e.nombre_comercial, '')                AS cliente,
               dc.indicador_generado_factura                 AS facturada,
               ROUND(SUM(ISNULL(dd.cantidad, 0)), 2)         AS kg
          FROM despacho_cliente dc
          JOIN detalle_despacho_cliente dd
            ON dd.id_despacho_cliente = dc.id_despacho_cliente
          LEFT JOIN empresa e ON e.id_empresa = dc.id_empresa
         WHERE dc.fecha = '{dia}'
           AND dc.fecha_anulacion IS NULL
           AND dd.id_bodega = {BODEGA_PT}
         GROUP BY dc.numero, CONVERT(varchar(16), dc.fecha_creacion, 120),
                  e.nombre_comercial, dc.indicador_generado_factura
         ORDER BY 2
    """
    out = []
    for r in metabase_client.fetch_dataset(2, sql, max_results=1000) or []:
        try:
            kg = round(float(r.get("kg") or 0), 2)
        except (TypeError, ValueError):
            continue
        fact = str(r.get("facturada"))
        out.append({
            "guia": str(r.get("guia") or "").strip(),
            "hora": str(r.get("creado") or "").strip()[-5:],
            "cliente": str(r.get("cliente") or "").strip(),
            "kg": kg,
            "facturada": fact.lower() in ("true", "1", "1.0"),
        })
    return out


def _kg_con_guia_de_hoy(dia: str) -> dict[str, float]:
    """{número de factura → kilos que vienen de una guía DE ESE DÍA}.

    Lo único que Asinfo sabe y nosotros no. El resto de la cuenta es de PC.
    """
    from modules._lib import metabase_client

    sql = f"""
        SELECT fc.numero AS doc,
               ROUND(SUM(CASE WHEN dc.fecha = '{dia}'
                              THEN ISNULL(dfc.cantidad, 0) ELSE 0 END), 2) AS kg
          FROM factura_cliente fc
          JOIN detalle_factura_cliente dfc
            ON dfc.id_factura_cliente = fc.id_factura_cliente
          LEFT JOIN detalle_despacho_cliente ddc
            ON ddc.id_detalle_despacho_cliente = dfc.id_detalle_despacho_cliente
          LEFT JOIN despacho_cliente dc
            ON dc.id_despacho_cliente = ddc.id_despacho_cliente
         WHERE fc.fecha = '{dia}'
           AND fc.estado <> 0
         GROUP BY fc.numero
    """
    out: dict[str, float] = {}
    for r in metabase_client.fetch_dataset(2, sql, max_results=2000) or []:
        try:
            out[str(r.get("doc") or "").strip()] = round(float(r.get("kg") or 0), 2)
        except (TypeError, ValueError):
            continue
    return out


def _doc_por_guia(dia: str) -> dict[str, dict[str, float]]:
    """{número de guía → {número de documento: kilos facturados de esa guía}}.

    Los KILOS van acá y no en otra query porque salen del mismo join: TMT
    2026-08-19 pidió ver *"kg de despacho y kg de factura"* uno al lado del
    otro, que es la comparación que la tabla no podía hacer — una guía puede
    facturarse por un peso distinto del que salió, y eso hasta ahora no se veía.

    Es la pieza que faltaba para que la cuenta cierre. Antes se preguntaba sólo
    `indicador_generado_factura`, que dice "esta guía YA tiene documento **en
    Asinfo**" — y eso no es lo mismo que "está cargado en Programa Core". El
    19/08 a las 08:28 salió la guía DES-000095927 (AJO, 12,90 kg), Asinfo la
    facturó a las 08:30 con la 001-099-000182106… y PC todavía no la había
    importado. Esos 12,90 kg no caían en ningún balde y aparecían como residuo.
    """
    from modules._lib import metabase_client

    sql = f"""
        SELECT dc.numero AS guia, fc.numero AS doc,
               ROUND(SUM(ISNULL(dfc.cantidad, 0)), 2) AS kg
          FROM despacho_cliente dc
          JOIN detalle_despacho_cliente ddc
            ON ddc.id_despacho_cliente = dc.id_despacho_cliente
          JOIN detalle_factura_cliente dfc
            ON dfc.id_detalle_despacho_cliente = ddc.id_detalle_despacho_cliente
          JOIN factura_cliente fc
            ON fc.id_factura_cliente = dfc.id_factura_cliente
         WHERE dc.fecha = '{dia}'
           AND dc.fecha_anulacion IS NULL
           AND fc.estado <> 0
         GROUP BY dc.numero, fc.numero
    """
    out: dict[str, dict[str, float]] = {}
    for r in metabase_client.fetch_dataset(2, sql, max_results=2000) or []:
        guia = str(r.get("guia") or "").strip()
        doc = str(r.get("doc") or "").strip()
        if not (guia and doc):
            continue
        try:
            kg = round(float(r.get("kg") or 0), 2)
        except (TypeError, ValueError):
            kg = 0.0
        out.setdefault(guia, {})[doc] = kg
    return out


def _docs_sin_autorizar(dia: str) -> set[str]:
    """Los documentos del día que Asinfo tiene en un estado que NO se importa.

    Existen, tienen número y salieron por una guía, pero el programa no los va
    a levantar mientras sigan así. La pantalla los nombra distinto para no
    mandar a nadie a buscar un problema de carga que no existe.
    """
    from modules._lib import metabase_client

    lista = ", ".join(str(e) for e in ESTADOS_IMPORTABLES)
    sql = f"""
        SELECT fc.numero AS doc
          FROM factura_cliente fc
         WHERE fc.fecha = '{dia}'
           AND fc.estado <> 0
           AND fc.estado NOT IN ({lista})
    """
    out = set()
    for r in metabase_client.fetch_dataset(2, sql, max_results=500) or []:
        doc = str(r.get("doc") or "").strip()
        if doc:
            out.add(doc)
    return out


def _documentos_pc(dia: str) -> list[dict]:
    """Los documentos del día que SUMAN kilos, tal como los cuenta el pin."""
    filas = db.fetch_all(
        """
        SELECT numf, numf_completo, codigo_cli, kg, importe
          FROM scintela.factura
         WHERE fecha = %s
           AND COALESCE(stat, '') <> 'X'
           AND kg > 0
         ORDER BY numf_completo, id_factura
        """,
        (dia,),
    ) or []
    return [{
        "numf": r.get("numf"),
        "doc": (r.get("numf_completo") or "").strip(),
        "cliente": (r.get("codigo_cli") or "").strip(),
        "kg": round(float(r.get("kg") or 0), 2),
        "importe": round(float(r.get("importe") or 0), 2),
    } for r in filas]


def cuadre(fecha) -> dict:
    """El día entero: los dos totales, los tres detalles y lo que no cierra."""
    dia = _dia(fecha)
    docs = _documentos_pc(dia)
    facturado = round(sum(d["kg"] for d in docs), 2)
    docs_pc = {d["doc"] for d in docs}

    guias: list[dict] = []
    ligado: dict[str, float] = {}
    por_guia: dict[str, dict[str, float]] = {}
    sin_autorizar_docs: set[str] = set()
    asinfo_ok = True
    try:
        # Las cuatro preguntas a Asinfo no dependen una de otra: van A LA VEZ.
        # TMT 2026-09-02 (dueña: *"¿páginas lentas?"*): medida en vivo, la
        # pantalla tardaba 2,0 s SIEMPRE con 10 ms de base propia — eran los
        # cuatro peajes del puente, uno atrás del otro. Juntos cuestan lo que
        # el más lento.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cuadre") as pool:
            f_guias = pool.submit(_guias, dia)
            f_ligado = pool.submit(_kg_con_guia_de_hoy, dia)
            f_por_guia = pool.submit(_doc_por_guia, dia)
            f_sin_aut = pool.submit(_docs_sin_autorizar, dia)
        guias = f_guias.result()
        ligado = f_ligado.result()
        por_guia = f_por_guia.result()
        sin_autorizar_docs = f_sin_aut.result()
    except Exception as e:  # noqa: BLE001 -- el ERP nunca tumba la pantalla
        _LOG.warning("no pude leer los despachos del %s: %s", dia, e)
        asinfo_ok = False

    despachado = round(sum(g["kg"] for g in guias), 2) if asinfo_ok else None

    # (A) Lo facturado hoy que no salió por una guía de hoy.
    #
    # ⭐ TMT 2026-08-19: *"no me dice cuál es la factura extra"*. Acá había un
    # `continue` que salteaba TODA nota de entrega, con la idea de que una NTEN
    # es la guía misma documentada y no vive en `factura_cliente`. Vive: el
    # 19/08 las seis NTEN del día salieron por el join de `_doc_por_guia` con
    # su guía al lado. Con el salteo, la NTEN-10879 (BED, 265,50 kg) —que no
    # tenía guía de ese día— no caía en ningún balde y aparecía como "± sin
    # identificar": el número estaba, el nombre no. Ahora entra como cualquier
    # otro documento y la pantalla la nombra.
    sin_guia = []
    if asinfo_ok:
        for d in docs:
            resto = round(d["kg"] - ligado.get(d["doc"], 0.0), 2)
            if resto > UMBRAL_KG:
                sin_guia.append({**d, "kg_sin_guia": resto})
        sin_guia.sort(key=lambda x: -x["kg_sin_guia"])

    # (B) y (C) — lo que salió y todavía no está en NUESTRA base. Son dos cosas
    # distintas y se dicen distinto, porque lo que hay que hacer es distinto:
    #   · sin factura todavía  → falta que Asinfo la emita;
    #   · facturada en Asinfo  → ya existe el documento, falta IMPORTARLO acá.
    # Mirar sólo `indicador_generado_factura` mezclaba las dos y dejaba la
    # segunda afuera de la cuenta (era el residuo del 19/08).
    sin_factura, sin_cargar, sin_autorizar = [], [], []
    kg_nten_esperado = 0.0
    for g in guias:
        kg_de = por_guia.get(g["guia"]) or {}
        suyos = sorted(kg_de)
        # TMT 2026-08-19: *"acá también poneme cada factura de cada despacho
        # así lo pueden pueden usar"* — la columna decía "facturada" / "sin
        # factura", que es el estado pero no el dato: con el NÚMERO al lado, la
        # tabla del día sirve para trabajar (buscar la factura, reclamarla,
        # cruzarla) y no sólo para mirar.
        g["docs"] = suyos
        g["en_pc"] = [x for x in suyos if x in docs_pc]
        # Los que Asinfo todavía no autoriza: existen, pero el programa no los
        # puede levantar. Se nombran distinto (ver ESTADOS_IMPORTABLES).
        g["sin_autorizar"] = [x for x in suyos if x in sin_autorizar_docs]
        # Los kilos que la factura dice, contra los que salieron por la puerta.
        g["kg_fact"] = round(sum(kg_de.values()), 2) if suyos else None
        g["difiere"] = bool(suyos and abs(g["kg_fact"] - g["kg"]) > UMBRAL_KG)
        if suyos:
            faltan = [x for x in suyos if x not in docs_pc]
            if faltan:
                # Dos causas distintas y dos arreglos distintos: una la
                # resuelve la carga sola en dos minutos, la otra la tiene que
                # destrabar quien factura.
                destino = (sin_autorizar if all(x in sin_autorizar_docs
                                                for x in faltan)
                           else sin_cargar)
                destino.append({**g, "doc": faltan[0]})
        elif not g["facturada"]:
            sin_factura.append(g)
        else:
            # Facturada en Asinfo pero sin `factura_cliente` que la consuma:
            # su documento es una NOTA DE ENTREGA que el join no alcanza. No
            # hay número con el que casarla una por una, así que se compara el
            # TOTAL contra las NTEN del día que quedaron sin guía.
            kg_nten_esperado += g["kg"]
    kg_sin_factura = round(sum(g["kg"] for g in sin_factura), 2)

    # Las NTEN sin guía se ofrecen para tapar ese total, de mayor a menor: la
    # que entra ES la guía documentada y sale de la lista de "sin guía de hoy".
    # Las que sobran se quedan CON NOMBRE, que es lo que se venía perdiendo.
    resto_nten = kg_nten_esperado
    if resto_nten > UMBRAL_KG:
        quedan = []
        for f in sin_guia:
            if (f["doc"].upper().startswith("NTEN")
                    and resto_nten >= f["kg_sin_guia"] - UMBRAL_KG):
                resto_nten -= f["kg_sin_guia"]
            else:
                quedan.append(f)
        sin_guia = quedan
    kg_nten_falta = round(max(0.0, resto_nten), 2)
    kg_sin_cargar = round(sum(g["kg"] for g in sin_cargar) + kg_nten_falta, 2)
    kg_sin_autorizar = round(sum(g["kg"] for g in sin_autorizar), 2)
    kg_sin_guia = round(sum(d["kg_sin_guia"] for d in sin_guia), 2)

    diferencia = round(facturado - despachado, 2) if despachado is not None else None
    residuo = (round(facturado - (despachado - kg_sin_factura - kg_sin_cargar
                                  - kg_sin_autorizar + kg_sin_guia), 2)
               if despachado is not None else None)

    return {
        "fecha": dia,
        "asinfo_ok": asinfo_ok,
        "despachado": {"kg": despachado, "n": len(guias)},
        "facturado": {"kg": facturado, "n": len(docs),
                      "importe": round(sum(d["importe"] for d in docs), 2)},
        "diferencia": diferencia,
        "sin_guia": {"kg": kg_sin_guia, "items": sin_guia},
        "sin_factura": {"kg": kg_sin_factura, "items": sin_factura},
        "sin_cargar": {"kg": kg_sin_cargar, "items": sin_cargar,
                       "kg_nten": kg_nten_falta},
        "sin_autorizar": {"kg": kg_sin_autorizar, "items": sin_autorizar},
        "residuo": residuo,
        "guias": guias,
        # ⭐ El número visible de cada documento → su `numf`, para que la tabla
        #    pueda linkear a la ficha. Los que todavía no están acá no entran:
        #    un link a una factura que no existe es un 404 servido.
        "numf_por_doc": {d["doc"]: d["numf"] for d in docs if d["doc"]},
    }
