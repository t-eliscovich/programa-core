"""Consultas de facturas de venta.

Vocabulario canónico de stats (2026-04-29 — ver docs/SKILL_ADDENDUM_BATCH_18.md):

    Z = emitida (sin abono todavía)        -- estado inicial
    A = abonada parcialmente (saldo > 0)
    T = cancelada por el total (saldo = 0) -- terminal feliz
    X = eliminada por error                -- anulación administrativa

Cartera de facturas = Z + A (las que tienen saldo vivo).

TMT 2026-05-19 v8 — La stat 'Y' fue retirada del universo de facturas
(la dueña confirmó: "factura Y no existe, borremoslo de todos lados").
Anulada = 'X'. Si por algún motivo aparece una fila legacy con stat='Y',
queda fuera de cualquier vista (cartera, canceladas, eliminadas, estado).
"""
from datetime import date, timedelta

import db
from filters import money_es as _money
from filters import today_ec
from modules._lib import busqueda
from periodo_guard import asegurar_fecha_abierta

# Stats que cuentan como "vivos" (cartera viva). Excluye T (cancelada) y X (anulada).
STATS_VIVOS = ("Z", "A")
# Stats que cuentan como "anulado / eliminado" — para excluir en cartera.
# TMT 2026-05-19 v8 — dueña: "factura Y no existe, borremoslo de todos lados".
# Antes había STATS_ANULADAS = ("X","Y") como compat legacy, pero Y nunca
# se usó en la base operativa. Se elimina del universo conocido.
STATS_ANULADAS = ("X",)


def saldo_de(importe, abono, retencion=0) -> float:
    """El saldo de una factura. UNA sola fórmula, para todo el programa.

    TMT 2026-08-07 (dueña): "necesitaríamos dividir esto en dos columnas,
    retenciones y abono, no todo junto (entonces no se debería sumar más)".
    Hasta ayer la retención se sumaba al abono (regla del 06/08, commit
    8ed70dd) y el saldo era `importe − abono`. Ahora la retención vive en su
    propia columna (`factura.retencion`, migración 0179) y sigue bajando la
    deuda igual — pero se ve aparte:

        saldo = importe − abono − retencion

    Cada lugar que recalcula un saldo llama a esta función. Si mañana aparece
    un tercer concepto que descuenta (una NC propia, por ejemplo), se agrega
    acá y no en los diez lugares que antes hacían la resta a mano.
    """
    return round(
        float(importe or 0) - float(abono or 0) - float(retencion or 0), 2
    )


def stat_de(saldo, abono, stat_previo: str = "", tol: float = 0.005) -> str:
    """El ESTADO de una factura a partir de sus números. UNA sola regla.

        |saldo| ≈ 0      → 'T' (cancelada)
        saldo NEGATIVO   → 'A' (crédito a favor VIVO — no está cancelada)
        hay abono        → 'A' (abonada parcialmente)
        sin abono        → 'Z' (emitida, sin abono)

    Devuelve lo que los números DICEN, no lo que la factura traía; `stat_previo`
    no se usa para decidir, queda por compatibilidad con los llamadores.

    🚨 **El saldo negativo va por VALOR ABSOLUTO a propósito.** Con
    `saldo <= 0` un sobrepago cae en 'T' y el crédito del cliente DESAPARECE
    de la cartera y del estado de cuenta, porque las T no se listan. Una
    factura con saldo a favor está VIVA. Es el bug que la dueña hizo arreglar
    el 01/07/2026 en la aplicación de cheques (*"el crédito −42,08 se iba a
    'T' y desaparecía"*) y que volvió a entrar el 07/08 por las retenciones.

    ⚖️ TMT 2026-08-20: aquel arreglo se había hecho en UN lugar. La resta ya
    vivía en `saldo_de`, pero el ESTADO seguía copiado a mano en cinco:
    `cheques.anular_por_error_de_carga`, `cheques.desaplicar_factura`,
    `cheques.reemplazar`, `cheques.deshacer_neteo` y el preview de
    `facturas.reversar_abono_manual`. Los cinco tenían la fórmula vieja
    `saldo <= 0.01 → 'T'`. Si mañana aparece otra regla de estado, se agrega
    acá y no en los cinco.

    `tol` existe porque no todos toleran lo mismo: la cobranza
    (`cheques.aplicar_a_factura`) da por cancelado un residuo de hasta $0,50
    —decisión propia, con su toggle "olvidar saldo"— y los reversos cortan en
    el centavo. La tolerancia es del llamador; la regla del signo, no.
    """
    s = float(saldo or 0)
    if abs(s) <= tol:
        return "T"
    if s < 0:
        return "A"
    if float(abono or 0) > 0.005:
        return "A"
    return "Z"


#: Plazo default de vencimiento cuando el cliente no tiene `pago` numérico.
#: TMT 2026-08-05 (dueña): 30 → 90 días; la mig 0169 recalculó las impagas.
DIAS_VENCIMIENTO_DEFAULT = 90


def dias_vencimiento(pago_raw) -> int:
    """Días de plazo a partir de `cliente.pago`.

    Bug D fix (TMT 2026-05-16): 3545 clientes legacy tienen pago='C'
    (contado), 'X', '+', '.', '', etc. — strings que int() no parsea.
    Si no es numérico, fallback a DIAS_VENCIMIENTO_DEFAULT.
    """
    _pago_str = str(pago_raw or "").strip()
    return int(_pago_str) if _pago_str.isdigit() else DIAS_VENCIMIENTO_DEFAULT


def proximo_numf() -> int:
    """Siguiente número de factura (MAX+1). Fallback a 1 si no hay."""
    row = db.fetch_one("SELECT COALESCE(MAX(numf), 0) + 1 AS siguiente FROM scintela.factura")
    return int(row["siguiente"]) if row else 1


def crear(
    *,
    fecha: date,
    codigo_cli: str,
    kg,
    importe,
    numf: int | None = None,
    vencimiento: date | None = None,
    condic: str | None = None,
    tipo: str | None = None,
    numf_completo: str | None = None,
    clave: str | None = None,
    usuario: str = "web",
) -> dict:
    """Insert de una factura nueva.

    Reglas (vocabulario canónico 2026-04-29):
        - saldo inicial = importe (abono = 0, saldo = importe)
        - stat inicial  = 'Z' (emitida, sin abono)
        - Si no llega vencimiento y el cliente tiene c.pago (días), se usa.
        - Si no llega numf, se asigna MAX+1.

    Notas:
        - El stat 'A' se asigna automáticamente cuando se aplica un cheque
          parcial (ver `cheques.queries.aplicar_a_factura`).
        - El stat 'T' se asigna cuando saldo = 0.
        - La anulación va por `anular()` que setea 'X'.
    """
    asegurar_fecha_abierta(fecha)

    # Vencimiento por defecto = fecha + pago_del_cliente días (si hay)
    if vencimiento is None:
        row = db.fetch_one(
            "SELECT pago FROM scintela.cliente WHERE codigo_cli = %s",
            (codigo_cli,),
        )
        dias = dias_vencimiento(row.get("pago") if row else None)
        vencimiento = fecha + timedelta(days=dias)

    with db.tx() as conn:
        # TMT 2026-05-20 PASADA 3 — race-condition fix.
        # Antes: numf = MAX(numf)+1 fuera de tx. Dos requests concurrentes
        # podían asignar el mismo numf. Con advisory lock por la tabla
        # entera (clave 4242 = "factura.numf"), solo una transacción a la
        # vez recalcula el siguiente. El lock se libera al COMMIT/ROLLBACK.
        if numf is None:
            with conn.cursor() as _cur:
                _cur.execute("SELECT pg_advisory_xact_lock(4242)")
                _cur.execute(
                    "SELECT COALESCE(MAX(numf), 0) + 1 FROM scintela.factura"
                )
                numf = int(_cur.fetchone()[0])

        row = db.execute_returning(
            """
            INSERT INTO scintela.factura
                (numf, fecha, codigo_cli, kg, importe, abono, saldo,
                 stat, condic, tipo, vencimiento, numf_completo, clave, usuario_crea)
            VALUES (%s, %s, %s, %s, %s, 0, %s,
                    'Z', %s, %s, %s, %s, %s, %s)
            RETURNING id_factura, numf
            """,
            (
                numf, fecha, codigo_cli.upper().strip(),
                kg, importe, importe,
                (condic or None), (tipo or None),
                vencimiento, (numf_completo or None),
                (clave or None)[:2] if clave else None,
                usuario,
            ),
            conn=conn,
        )
        # Historial unificado: toda factura emitida queda registrada en
        # mov_doble como auto-referencia (factura→factura) para que
        # aparezca en /historial. Si el registro falla, rollback total
        # (vale más perder la factura que tener una sin huella). El
        # importe se preserva con signo (devoluciones < 0) — el historial
        # ya distingue tipo via metadata.devolucion. TMT 2026-05-13.
        if row and row.get("id_factura"):
            import mov_doble as _md
            es_devolucion = float(importe or 0) < 0
            _md.registrar(
                conn=conn,
                tipo=("factura_devolucion" if es_devolucion else "factura_emitida"),
                origen_table="factura",
                origen_id=row["id_factura"],
                destino_table="factura",
                destino_id=row["id_factura"],
                importe=float(importe or 0),
                fecha=fecha,
                concepto=(("DEVOLUCION " if es_devolucion else "")
                           + f"Factura #{numf} {codigo_cli.upper().strip()}")[:200],
                usuario=usuario,
                metadata={"codigo_cli": codigo_cli.upper().strip(),
                          "numf": numf,
                          "kg": float(kg or 0),
                          "devolucion": es_devolucion},
            )
    return row or {}


def editar(
    id_factura: int,
    *,
    abono=None,
    condic: str | None = None,
    observacion: str | None = None,
    usuario: str = "web",
) -> dict:
    """Edición *blanda* de una factura emitida.

    Regla Ecuador (paridad con MODIFICA.PRG y discusión 2026-04-30):
    una factura emitida NO se edita en sus campos duros (importe, fecha,
    cliente, numf, kg). Para corregir cualquiera de eso → anular y reemitir.
    Esta función sólo permite ajustar:

      - `abono`: corrige el abono manual (e.g., para registrar un pago en
        efectivo no asociado a un cheque). Recompute
        `saldo = importe - abono - retencion`
        atomically. Si nuevo saldo ≈ 0 y stat≠'T', stampa primera vez:
        `stat='T'`, `vencim=CURRENT_DATE`.
      - `condic`: si cambia ' '→'C' aplica 5% pronto pago (importe×=0.95).
        Si cambia 'C'→' ' revierte (importe/=0.95). En ambos casos, el
        SALDO se recomputa con el nuevo importe.
      - `observacion`: append-only, append "[E]" al texto.

    Importe / kg / fecha / cliente / numf NUNCA se editan acá. ValueError
    si el caller los pasa como kwarg.

    Reglas:
      - No se puede editar facturas anuladas (stat ∈ X, Y).
      - `asegurar_fecha_abierta(fact.fecha)` — el período de la factura.
      - Bitácora best-effort vía after_request.

    Devuelve `{id_factura, importe, abono, saldo, stat, condic_previa, condic_nueva}`.
    """
    fact = db.fetch_one(
        "SELECT id_factura, numf, fecha, importe, abono, retencion, saldo, "
        "       stat, condic, vencimiento "
        "FROM scintela.factura WHERE id_factura = %s",
        (id_factura,),
    )
    if not fact:
        raise ValueError("Factura inexistente.")
    stat_actual = (fact.get("stat") or "").upper()
    if stat_actual in STATS_ANULADAS:
        raise ValueError("La factura está anulada/eliminada — no se puede editar.")

    asegurar_fecha_abierta(fact["fecha"])

    importe_actual = float(fact["importe"] or 0)
    abono_actual = float(fact["abono"] or 0)
    # La retención NO se edita acá: se aplica sola desde Asinfo y vive en su
    # propia columna (mig 0179). Pero entra en la cuenta del saldo, así que
    # editar el abono a mano tiene que respetarla.
    retencion_actual = float(fact["retencion"] or 0)
    condic_actual = (fact.get("condic") or "").strip()
    importe_nuevo = importe_actual
    abono_nuevo = abono_actual if abono is None else float(abono or 0)
    condic_nueva = condic_actual if condic is None else (condic or "").strip()

    # Toggle pronto pago (5%)  — paridad MODIFICA.PRG L435-442.
    # Convención dBase: condic vacío (' ') = no aplicado, 'C' = aplicado.
    if condic is not None:
        if condic_actual in ("", " ") and condic_nueva.upper() == "C":
            importe_nuevo = round(importe_actual * 0.95, 2)
        elif condic_actual.upper() == "C" and condic_nueva in ("", " "):
            importe_nuevo = round(importe_actual / 0.95, 2)

    # Validación: abono no puede exceder importe (con epsilon).
    if abono_nuevo < 0:
        raise ValueError("El abono no puede ser negativo.")
    if abono_nuevo + retencion_actual > importe_nuevo + 0.01:
        # El mensaje nombra la retención sólo si existe — si no, decir "abono
        # + retención 0,00" confunde al que nunca vio una.
        _extra = (
            f" + retención ({retencion_actual:.2f})" if retencion_actual > 0.005 else ""
        )
        raise ValueError(
            f"Abono ({abono_nuevo:.2f}){_extra} excede el importe "
            f"({importe_nuevo:.2f})."
        )

    saldo_nuevo = saldo_de(importe_nuevo, abono_nuevo, retencion_actual)

    # Stat recompute — paridad MODIFICA.PRG L443, con la regla del signo
    # (ver stat_de): un saldo a FAVOR queda 'A', no 'T'.
    stat_nuevo = stat_de(saldo_nuevo, abono_nuevo, tol=0.01)

    # Primera vez stat='T' → stampa vencim=CURRENT_DATE (paridad
    # MODIFICA.PRG L425-426).
    stamp_vencim = stat_actual != "T" and stat_nuevo == "T"

    obs_marca = None
    if observacion:
        obs_marca = f"[E] {observacion[:120]}"

    sql_set = ["importe=%s", "abono=%s", "saldo=%s", "stat=%s",
               "condic=%s", "usuario_modifica=%s"]
    params: list = [importe_nuevo, abono_nuevo, saldo_nuevo, stat_nuevo,
                    condic_nueva or None, usuario]
    if stamp_vencim:
        sql_set.append("vencimiento=CURRENT_DATE")
    if obs_marca:
        sql_set.append("observacion = COALESCE(observacion||' | ','')||%s")
        params.append(obs_marca)
    params.append(id_factura)

    # #32 (TMT 2026-05-14): si cambia el abono manualmente (sin pasar por
    # aplicación de cheque), registrar un mov_doble tipo='factura_abono_manual'
    # con el delta. Esto permite verlo en /historial y, eventualmente,
    # reversarlo. Antes el cambio quedaba sin huella.
    abono_cambio = abs(abono_nuevo - abono_actual) > 0.01

    with db.tx() as conn:
        db.execute(
            f"UPDATE scintela.factura SET {', '.join(sql_set)} WHERE id_factura=%s",
            tuple(params),
            conn=conn,
        )
        if abono_cambio:
            try:
                import mov_doble as _md
                _md.registrar(
                    conn=conn,
                    tipo="factura_abono_manual",
                    origen_table="factura",
                    origen_id=id_factura,
                    destino_table="factura",
                    destino_id=id_factura,
                    importe=round(abono_nuevo - abono_actual, 2),
                    fecha=fact.get("fecha") or today_ec(),
                    # TMT 2026-08-07 (dueña: que el abono manual se lea en el
                    # historial). Dos arreglos chicos y necesarios: el número
                    # es el `numf` que ella conoce — antes el SELECT no lo
                    # traía y el concepto mostraba el id interno ("#106" por
                    # "176100") — y los importes van en formato de acá
                    # (punto = miles, coma = decimales), no "250.00".
                    concepto=(
                        f"Abono manual factura {fact.get('numf') or id_factura}: "
                        f"$ {_money(abono_actual)} → $ {_money(abono_nuevo)}"
                    )[:200],
                    usuario=usuario,
                    metadata={"abono_prev": abono_actual,
                              "abono_nuevo": abono_nuevo,
                              "id_factura": id_factura,
                              "stat_previo": stat_actual,
                              "stat_nuevo": stat_nuevo},
                )
            except Exception:
                # mov_doble missing/transient: dejamos burbujar. La edición
                # del abono no debería perder huella.
                raise

    return {
        "id_factura": id_factura,
        "importe": importe_nuevo,
        "abono": abono_nuevo,
        "saldo": saldo_nuevo,
        "stat_previo": stat_actual,
        "stat_nuevo": stat_nuevo,
        "condic_previa": condic_actual,
        "condic_nueva": condic_nueva,
        "vencimiento_stamp": stamp_vencim,
    }


def reversar_abono_manual(
    id_mov_doble: int, *, usuario: str = "web", motivo: str = "",
) -> dict:
    """Deshace un cambio de abono hecho a mano — vuelve al abono anterior.

    TMT 2026-07-29. El mov_doble 'factura_abono_manual' se creó el 14/05 con
    el comentario "esto permite verlo en /historial y, eventualmente,
    reversarlo". El "eventualmente" nunca llegó: eran 14 movs activos que el
    /historial mostraba pero no dejaba deshacer, y un abono es justo el campo
    donde un error de tipeo (un cero de más) manda la factura a stat='T' y la
    saca de la cobranza.

    No hace falta inventar nada: `editar()` ya guarda `abono_prev` en la
    metadata del mov_doble. Restaurar es escribir ese valor y recomputar
    saldo/stat con la MISMA regla de `editar()` (paridad MODIFICA.PRG L443).

    OJO con dos cosas, y las dos están guardadas como precondición:

    1. Se recomputa el saldo con el `importe` que la factura tiene HOY, no
       con el de entonces. Si el importe cambió después (toggle de pronto
       pago 5%), restaurar el saldo viejo dejaría `saldo ≠ importe − abono`,
       que es exactamente uno de los errores que el chequeo de salud
       persigue.
    2. Si el abono ya NO es el que dejó esta edición, algo pasó en el medio
       (se aplicó un cheque, se editó de nuevo). Reversar pisaría ese cambio
       posterior sin avisar → ValueError. Reversá primero lo de arriba.

    Devuelve `{id_factura, numf, abono_previo, abono_actual, saldo, stat}`.
    """
    import json as _json

    import mov_doble as _md

    mv = db.fetch_one(
        "SELECT id_mov_doble, tipo, origen_id, estado, metadata, fecha_operacion "
        "  FROM scintela.mov_doble WHERE id_mov_doble = %s",
        (id_mov_doble,),
    )
    if not mv:
        raise ValueError("No encuentro ese movimiento.")
    if (mv.get("tipo") or "") != "factura_abono_manual":
        raise ValueError(
            f"Ese movimiento es '{mv.get('tipo')}', no un abono manual.")
    if (mv.get("estado") or "activo") != "activo":
        raise ValueError(
            f"Ese abono manual ya está {mv.get('estado')} — no se reversa dos veces.")

    meta = mv.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    if "abono_prev" not in meta:
        raise ValueError(
            "Ese movimiento no guardó el abono anterior en la metadata "
            "(es de antes de que se registrara). Corregí el abono a mano "
            "desde la factura.")

    abono_prev = round(float(meta.get("abono_prev") or 0), 2)
    abono_esperado = round(float(meta.get("abono_nuevo") or 0), 2)
    id_factura = int(mv.get("origen_id") or 0)

    with db.tx() as conn:
        fact = db.fetch_one(
            "SELECT id_factura, numf, numf_completo, fecha, importe, abono, "
            "       saldo, stat "
            "  FROM scintela.factura WHERE id_factura = %s FOR UPDATE",
            (id_factura,), conn=conn,
        )
        if not fact:
            raise ValueError("La factura de ese movimiento ya no existe.")
        stat_actual = (fact.get("stat") or "").upper()
        if stat_actual in STATS_ANULADAS:
            raise ValueError(
                "La factura está anulada — no se puede tocar el abono.")

        asegurar_fecha_abierta(fact["fecha"])

        importe_hoy = round(float(fact.get("importe") or 0), 2)
        abono_hoy = round(float(fact.get("abono") or 0), 2)
        if abs(abono_hoy - abono_esperado) > 0.01:
            raise ValueError(
                f"El abono de la factura cambió después de esta edición "
                f"(quedó en $ {abono_hoy:,.2f}, esta edición lo había dejado "
                f"en $ {abono_esperado:,.2f}). Reversá primero el movimiento "
                f"más nuevo — si no, este reverso lo pisaría sin avisar.")
        if abono_prev > importe_hoy + 0.01:
            raise ValueError(
                f"El abono anterior ($ {abono_prev:,.2f}) es mayor que el "
                f"importe actual de la factura ($ {importe_hoy:,.2f}): el "
                f"importe cambió en el medio. Ajustalo a mano.")

        retencion_hoy = round(float(fact.get("retencion") or 0), 2)
        saldo_nuevo = saldo_de(importe_hoy, abono_prev, retencion_hoy)
        # MISMA regla que `editar()`: el reverso tiene que dejar la factura
        # igual a como la habría dejado la edición inversa, no parecida.
        stat_nuevo = stat_de(saldo_nuevo, abono_prev, tol=0.01)

        db.execute(
            "UPDATE scintela.factura "
            "   SET abono = %s, saldo = %s, stat = %s, usuario_modifica = %s "
            " WHERE id_factura = %s",
            (abono_prev, saldo_nuevo, stat_nuevo, usuario, id_factura),
            conn=conn,
        )
        _md.registrar(
            conn=conn,
            tipo="reverso_factura_abono_manual",
            origen_table="factura", origen_id=id_factura,
            destino_table="factura", destino_id=id_factura,
            importe=round(abono_prev - abono_hoy, 2),
            fecha=fact.get("fecha") or today_ec(),
            concepto=(
                f"REVERSO abono manual factura "
                f"#{fact.get('numf') or id_factura}: {abono_hoy:.2f} → "
                f"{abono_prev:.2f}" + (f" ({motivo})" if motivo else "")
            )[:200],
            usuario=usuario,
            metadata={"abono_deshecho": abono_hoy,
                      "abono_restaurado": abono_prev,
                      "stat_restaurado": stat_nuevo,
                      "motivo": motivo or None},
            id_original=id_mov_doble,
        )

    return {
        "id_factura": id_factura,
        "numf": fact.get("numf_completo") or fact.get("numf"),
        "abono_previo": abono_hoy,
        "abono_actual": abono_prev,
        "saldo": saldo_nuevo,
        "stat": stat_nuevo,
    }


def editar_numf(
    id_factura: int,
    nuevo_numf: int,
    *,
    nuevo_numf_completo: str | None = None,
    usuario: str = "web",
) -> dict:
    """Corrige el N° de factura cuando se cargó mal (typo o orden).

    Tamara 2026-05-28 — dueña: 'dejame editar numero de facturas'. La regla
    Ecuador (anular y reemitir) aplica para corregir importe/cliente/fecha,
    pero acá la dueña pide habilitar la corrección puntual del N° impreso
    cuando se tipeó mal al cargar (la factura física tiene un N° y PC tiene
    otro). NO genera espejo en cuentas — sólo cambia la columna `numf` (y
    `numf_completo` si se pasa).

    Validaciones:
      - factura no anulada (stat != X)
      - nuevo numf > 0
      - nuevo numf no usado por otra factura del mismo tipo (excepción si
        es la misma fila)

    Devuelve: {id_factura, numf_previo, numf_nuevo, numf_completo_nuevo}.
    """
    if not nuevo_numf or int(nuevo_numf) <= 0:
        raise ValueError("El N° debe ser un entero positivo.")

    fact = db.fetch_one(
        "SELECT id_factura, fecha, numf, numf_completo, stat "
        "FROM scintela.factura WHERE id_factura = %s",
        (id_factura,),
    )
    if not fact:
        raise ValueError("Factura inexistente.")
    if (fact.get("stat") or "").upper() in STATS_ANULADAS:
        raise ValueError("Factura anulada/eliminada — no se puede editar.")

    asegurar_fecha_abierta(fact["fecha"])

    nuevo_numf = int(nuevo_numf)
    numf_previo = fact.get("numf")
    if numf_previo == nuevo_numf and (
        nuevo_numf_completo is None
        or nuevo_numf_completo == fact.get("numf_completo")
    ):
        # No-op — devuelvo OK silencioso.
        return {
            "id_factura": id_factura,
            "numf_previo": numf_previo,
            "numf_nuevo": nuevo_numf,
            "numf_completo_nuevo": fact.get("numf_completo"),
        }

    # TMT 2026-06-03 audit fix: chequeo de duplicados + UPDATE en una sola
    # tx con advisory lock (misma clave 4242 que usa crear() para numf).
    # Antes: dos editar_numf() concurrentes podían pasar ambos el dup check
    # y ambos UPDATE al mismo valor → dos facturas vivas con el mismo numf.
    with db.tx() as conn:
        # try/except defensivo: cursor mock en tests no tiene rowcount.
        try:
            db.execute(
                "SELECT pg_advisory_xact_lock(4242)",
                (), conn=conn,
            )
        except (AttributeError, TypeError) as _e:
            from modules._lib.silencios import avisar
            avisar(__name__, "editar_numf", _e, nivel="debug")
        dup = db.fetch_one(
            "SELECT id_factura, COALESCE(stat,'') AS stat, codigo_cli, fecha, saldo "
            "FROM scintela.factura "
            "WHERE numf = %s AND id_factura <> %s "
            "  AND COALESCE(stat,'') NOT IN ('X','x') "
            "LIMIT 1",
            (nuevo_numf, id_factura), conn=conn,
        )
        if dup:
            raise ValueError(
                f"El N° {nuevo_numf} ya está usado por la factura id={dup['id_factura']} "
                f"(cliente={dup.get('codigo_cli','?')}, fecha={dup.get('fecha','?')}, "
                f"stat={dup.get('stat','?')}, saldo=${dup.get('saldo','?')}). "
                f"Si es duplicado, anulala primero."
            )

        sql_set = ["numf=%s", "usuario_modifica=%s"]
        params: list = [nuevo_numf, usuario]
        if nuevo_numf_completo is not None:
            sql_set.append("numf_completo=%s")
            params.append(nuevo_numf_completo or None)
        params.append(id_factura)

        db.execute(
            f"UPDATE scintela.factura SET {', '.join(sql_set)} WHERE id_factura=%s",
            tuple(params), conn=conn,
        )
    return {
        "id_factura": id_factura,
        "numf_previo": numf_previo,
        "numf_nuevo": nuevo_numf,
        "numf_completo_nuevo": nuevo_numf_completo if nuevo_numf_completo is not None
                                else fact.get("numf_completo"),
    }


def editar_campo(
    id_factura: int,
    campo: str,
    valor,
    *,
    usuario: str = "web",
) -> dict:
    """Edición inline de un campo puntual desde el listado /facturas.

    Tamara 2026-05-28: 'dejame editar los montos en facturas'. La regla
    Ecuador (anular y reemitir para tocar importe/fecha/cliente/kg) sigue
    aplicando para reemisiones formales, pero acá habilitamos correcciones
    de typo desde el listado sin pasar por anulación.

    Campos soportados:
      - 'numf'    -> usa editar_numf() (compatibilidad)
      - 'importe' -> actualiza importe + recomputa saldo/stat
      - 'kg'      -> actualiza kg (sin tocar nada más)
      - 'fecha'   -> actualiza fecha (asegura período abierto del nuevo Y del viejo)

    No editamos: cliente, condic (eso va por editar()).
    Anuladas: ValueError.

    Devuelve dict con los valores actualizados.
    """
    if campo not in ("numf", "importe", "kg", "fecha", "codigo_cli"):
        raise ValueError(f"Campo no soportado: {campo}")

    if campo == "numf":
        # Delegar a editar_numf (mismo contrato)
        return editar_numf(id_factura, int(valor), usuario=usuario)

    fact = db.fetch_one(
        "SELECT id_factura, fecha, importe, abono, kg, stat "
        "FROM scintela.factura WHERE id_factura = %s",
        (id_factura,),
    )
    if not fact:
        raise ValueError("Factura inexistente.")
    if (fact.get("stat") or "").upper() in STATS_ANULADAS:
        raise ValueError("Factura anulada/eliminada — no se puede editar.")

    asegurar_fecha_abierta(fact["fecha"])

    if campo == "importe":
        try:
            nuevo = float(valor)
        except (TypeError, ValueError):
            raise ValueError("Importe inválido.")
        if nuevo <= 0:
            raise ValueError("El importe debe ser > 0.")
        abono = float(fact.get("abono") or 0)
        # TMT 2026-08-20: la retención no estaba en ninguna de las dos
        # cuentas. `nuevo - abono` se escapó del candado de `saldo_de`
        # porque la variable no se llama `importe`, y bajar el importe de
        # una factura con retención le borraba el descuento del saldo.
        retencion = float(fact.get("retencion") or 0)
        if abono + retencion > nuevo + 0.01:
            _ret = f" + retención ({retencion:.2f})" if retencion > 0.005 else ""
            raise ValueError(
                f"El nuevo importe ({nuevo:.2f}) es menor al abono ya cobrado "
                f"({abono:.2f}){_ret}. Anulá la factura para corregir."
            )
        saldo_nuevo = saldo_de(nuevo, abono, retencion)
        stat_nuevo = stat_de(saldo_nuevo, abono, tol=0.01)
        db.execute(
            "UPDATE scintela.factura "
            "SET importe = %s, saldo = %s, stat = %s, usuario_modifica = %s "
            "WHERE id_factura = %s",
            (round(nuevo, 2), saldo_nuevo, stat_nuevo, usuario, id_factura),
        )
        return {
            "id_factura": id_factura,
            "campo": "importe",
            "valor_nuevo": round(nuevo, 2),
            "saldo_nuevo": saldo_nuevo,
            "stat_nuevo": stat_nuevo,
        }

    if campo == "kg":
        try:
            nuevo = float(valor)
        except (TypeError, ValueError):
            raise ValueError("Kg inválido.")
        # kg puede ser negativo (devolución) — no validamos signo.
        db.execute(
            "UPDATE scintela.factura "
            "SET kg = %s, usuario_modifica = %s "
            "WHERE id_factura = %s",
            (round(nuevo, 2), usuario, id_factura),
        )
        return {
            "id_factura": id_factura,
            "campo": "kg",
            "valor_nuevo": round(nuevo, 2),
        }

    if campo == "fecha":
        # Aceptar 'YYYY-MM-DD' o 'DD/MM/YYYY'
        from datetime import datetime as _dt
        s = str(valor or "").strip()
        nueva_fecha = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                nueva_fecha = _dt.strptime(s, fmt).date()
                break
            except ValueError:
                continue
        if nueva_fecha is None:
            raise ValueError(f"Fecha inválida: {s} (use YYYY-MM-DD o DD/MM/YYYY).")
        # Validar período abierto del nuevo período también.
        asegurar_fecha_abierta(nueva_fecha)
        db.execute(
            "UPDATE scintela.factura "
            "SET fecha = %s, usuario_modifica = %s "
            "WHERE id_factura = %s",
            (nueva_fecha, usuario, id_factura),
        )
        return {
            "id_factura": id_factura,
            "campo": "fecha",
            "valor_nuevo": nueva_fecha.isoformat(),
        }

    if campo == "codigo_cli":
        # Dueña 2026-06-30: "dejame editar cliente". Corrección de typo del
        # código de cliente desde el listado. No bloqueamos si el cliente no
        # existe (los flujos asinfo/cobranza auto-crean) pero devolvemos el
        # nombre si lo encontramos para feedback. "PC es el futuro": editable.
        nuevo = (str(valor or "").strip().upper())[:10]
        if not nuevo:
            raise ValueError("Código de cliente vacío.")
        nom = db.fetch_one(
            "SELECT nombre FROM scintela.cliente WHERE UPPER(TRIM(codigo_cli)) = %s",
            (nuevo,),
        )
        db.execute(
            "UPDATE scintela.factura SET codigo_cli = %s, usuario_modifica = %s "
            "WHERE id_factura = %s",
            (nuevo, usuario, id_factura),
        )
        return {
            "id_factura": id_factura,
            "campo": "codigo_cli",
            "valor_nuevo": nuevo,
            "nombre": (nom or {}).get("nombre"),
        }


def por_id_interno(id_factura: int) -> dict | None:
    """Cabecera de factura ESTRICTAMENTE por la PK interna (id_factura).

    TMT 2026-06-07: a diferencia de `por_id` (que resuelve numf-OR-id,
    priorizando numf para soportar URLs con el número del dBase), esta
    versión busca SOLO por la PK interna. Es OBLIGATORIA en handlers de
    ACCIÓN (editar/anular/confirmar) donde el parámetro es el id interno:
    como el id interno de una factura puede COINCIDIR con el numf de OTRA
    (ej. id 161497 de una = numf 161497 de otra), usar `por_id` ahí
    mostraría/editaría la factura equivocada.
    """
    return db.fetch_one(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.vencimiento,
               f.codigo_cli, f.kg, f.importe, f.abono, f.retencion, f.saldo,
               f.stat, f.condic, f.tipo, f.pase, f.clave,
               COALESCE(c.nombre, '')    AS cliente,
               c.ruc, c.telefono, c.pago,
               -- ⭐ El vendedor del CLIENTE, que es el que cobra la comisión
               --    de esta factura. Se trae con el nombre porque el código de
               --    tres letras no se lo sabe todo el mundo.
               UPPER(TRIM(COALESCE(c.vend, ''))) AS vend,
               COALESCE(NULLIF(TRIM(v.nombre), ''), '') AS vendedor_nombre
          FROM scintela.factura f
          LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
          LEFT JOIN scintela.vendedor v
                 ON UPPER(TRIM(v.codigo)) = UPPER(TRIM(COALESCE(c.vend, '')))
         WHERE f.id_factura = %s
         LIMIT 1
        """,
        (id_factura,),
    )


def por_id(id_factura: int) -> dict | None:
    """Cabecera de factura por id_factura interno O por numf.

    Tamara 2026-05-23: los links del historial/facturas usan el numf real
    (175763) en la URL en lugar del id_factura interno (5074). Esta
    función acepta ambos — prioriza numf si hay match (más probable que
    el caller use el visible) y fallback a id_factura.
    """
    return db.fetch_one(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.vencimiento,
               f.codigo_cli, f.kg, f.importe, f.abono, f.retencion, f.saldo,
               f.stat, f.condic, f.tipo, f.pase, f.clave,
               COALESCE(c.nombre, '')    AS cliente,
               c.ruc, c.telefono, c.pago,
               -- ⭐ El vendedor del CLIENTE, que es el que cobra la comisión
               --    de esta factura. Se trae con el nombre porque el código de
               --    tres letras no se lo sabe todo el mundo.
               UPPER(TRIM(COALESCE(c.vend, ''))) AS vend,
               COALESCE(NULLIF(TRIM(v.nombre), ''), '') AS vendedor_nombre
          FROM scintela.factura f
          LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
          LEFT JOIN scintela.vendedor v
                 ON UPPER(TRIM(v.codigo)) = UPPER(TRIM(COALESCE(c.vend, '')))
         WHERE f.numf = %s OR f.id_factura = %s
         ORDER BY (f.numf = %s) DESC, f.id_factura ASC
         LIMIT 1
        """,
        (id_factura, id_factura, id_factura),
    )


def las_del_mismo_numero(numf: int) -> list[dict]:
    """Todas las facturas que comparten ese número visible. Casi siempre una.

    ⭐ Existe para poder NO ADIVINAR. El `numf` no es único —al 26/08/2026 hay
    **2.064 números repetidos entre 4.416 facturas**, el 12% del total— porque
    las notas de entrega (NTEN) y las notas de crédito llevan su propia
    numeración y chocan con las facturas viejas. Ejemplo real: el 10919 es la
    NTEN del mostrador del 26/08 por $5,53 **y** una devolución de AGL del
    03/06 por −$462,15.

    Hasta hoy `por_id` elegía sola con `ORDER BY id_factura ASC`, o sea SIEMPRE
    la más vieja, sin decirle a nadie que había otra. La dueña lo cazó el
    26/08/2026: *"el link me manda a cualquier lado, es hasta otro cliente"*.
    """
    return db.fetch_all(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.tipo,
               f.importe, f.codigo_cli, COALESCE(c.nombre, '') AS cliente
          FROM scintela.factura f
          LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
         WHERE f.numf = %s
         ORDER BY f.fecha DESC, f.id_factura DESC
        """,
        (numf,),
    ) or []


def por_numf_completo(numf_completo: str) -> dict | None:
    """Cabecera de factura por su número VISIBLE completo, sin ambigüedad.

    ⭐ Para qué existe. `por_id` acepta numf O id interno y prioriza el numf,
    que es el número que la dueña conoce — pero el `numf` de una NOTA DE
    ENTREGA se repite con el de una factura vieja de otro cliente (el 10879 es
    una NTEN de BED del 19/08 y también una factura de 2022), así que un link
    por número abría la que no era. El `numf_completo` (NTEN-10909,
    001-099-000182675) sí es único: cuando el que linkea lo tiene, se usa éste
    y no hay forma de errarle.
    """
    n = (numf_completo or "").strip()
    if not n:
        return None
    return db.fetch_one(
        """
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.vencimiento,
               f.codigo_cli, f.kg, f.importe, f.abono, f.retencion, f.saldo,
               f.stat, f.condic, f.tipo, f.pase, f.clave,
               COALESCE(c.nombre, '')    AS cliente,
               c.ruc, c.telefono, c.pago,
               -- ⭐ El vendedor del CLIENTE, que es el que cobra la comisión
               --    de esta factura. Se trae con el nombre porque el código de
               --    tres letras no se lo sabe todo el mundo.
               UPPER(TRIM(COALESCE(c.vend, ''))) AS vend,
               COALESCE(NULLIF(TRIM(v.nombre), ''), '') AS vendedor_nombre
          FROM scintela.factura f
          LEFT JOIN scintela.cliente c ON c.codigo_cli = f.codigo_cli
          LEFT JOIN scintela.vendedor v
                 ON UPPER(TRIM(v.codigo)) = UPPER(TRIM(COALESCE(c.vend, '')))
         WHERE f.numf_completo = %s
         ORDER BY f.id_factura ASC
         LIMIT 1
        """,
        (n,),
    )


def cheques_aplicados(id_factura: int) -> list[dict]:
    """Aplicaciones de cheques a esta factura vía chequesxfact."""
    return db.fetch_all(
        """
        SELECT cxf.id_chequexfact, cxf.fechaing, cxf.tipo, cxf.importe AS aplicado,
               cxf.abono_f, cxf.saldo_f, cxf.stat_f, cxf.no_banco,
               ch.id_cheque, ch.no_cheque, ch.fecha AS cheque_fecha, ch.fechad,
               -- TMT 2026-05-17: fechad_original/fecha_postergacion para
               -- mostrar "original X · postergado Y" si el cheque fue postergado.
               ch.fechad_original, ch.fecha_postergacion,
               ch.importe AS cheque_importe, ch.stat AS cheque_stat,
               -- TMT 2026-06-07: si el cheque no tiene texto de banco
               -- (ej. creados en PC), resolvemos el nombre desde la tabla
               -- banco por no_banco → evita mostrar "None".
               COALESCE(NULLIF(ch.banco, ''), b.nombre, '') AS cheque_banco
        FROM scintela.chequesxfact cxf
        LEFT JOIN scintela.cheque ch ON ch.id_cheque = cxf.id_cheque
        LEFT JOIN scintela.banco  b  ON b.no_banco  = ch.no_banco
        WHERE cxf.id_fact = %s
        ORDER BY cxf.fechaing DESC
        """,
        (id_factura,),
    )


def retenciones_aplicadas(codigo_cli: str, numf: int) -> list[dict]:
    """Retenciones emitidas para esta factura (por codigo_cli + numf)."""
    return db.fetch_all(
        """
        SELECT id_retencion, fecha, rete
        FROM scintela.retencion
        WHERE codigo_cli = %s AND numf = %s
        ORDER BY fecha DESC
        """,
        (codigo_cli, numf),
    )


def borrar_carga_erronea(id_factura: int, *, usuario: str = "web") -> dict:
    """Borra una factura COMPLETAMENTE (DELETE) — para revertir cargas erróneas.

    A diferencia de anular() que setea stat='X' (queda histórica), esto
    elimina la fila de la DB. Solo permitido si NUNCA tuvo movimientos:
    sin abonos, sin aplicaciones de cheques, sin retenciones.

    Caso de uso: Tamara carga por error una factura desde Asinfo y
    quiere deshacer la carga (no que quede como anulada).
    """
    fact = db.fetch_one(
        "SELECT id_factura, numf, numf_completo, codigo_cli, importe, abono, saldo, stat "
        "FROM scintela.factura WHERE id_factura = %s",
        (id_factura,),
    )
    if not fact:
        raise ValueError("Factura inexistente.")
    if float(fact.get("abono") or 0) != 0:
        raise ValueError("La factura ya tiene abonos cargados. Usá Anular en su lugar.")
    if float(fact.get("saldo") or 0) != float(fact.get("importe") or 0):
        raise ValueError("La factura tiene movimientos. Usá Anular en su lugar.")
    aplic = db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.chequesxfact WHERE id_fact = %s",
        (id_factura,),
    )
    if aplic and int(aplic["n"]) > 0:
        raise ValueError("La factura tiene aplicaciones de cheques. Reversalas primero o usá Anular.")
    ret = db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.retencion WHERE codigo_cli = %s AND numf = %s",
        (fact["codigo_cli"], fact["numf"]),
    )
    if ret and int(ret["n"]) > 0:
        raise ValueError("La factura tiene retenciones. No se puede borrar.")

    with db.tx() as conn:
        # Borrar mov_doble linkeado (auto-referencia que crea() guarda).
        db.execute(
            "DELETE FROM scintela.mov_doble "
            " WHERE (origen_table  = 'factura' AND origen_id  = %s) "
            "    OR (destino_table = 'factura' AND destino_id = %s)",
            (id_factura, id_factura),
            conn=conn,
        )
        db.execute(
            "DELETE FROM scintela.factura WHERE id_factura = %s",
            (id_factura,),
            conn=conn,
        )
    return {
        "id_factura": id_factura,
        "numf": fact["numf"],
        "numf_completo": fact.get("numf_completo"),
        "borrado": True,
    }


def anular(id_factura: int, *, motivo: str, usuario: str = "web") -> int:
    """Marca una factura como eliminada por error (stat='X').

    Vocabulario canónico (2026-04-29): el botón "anular" del UI es
    realmente "eliminé esto por error" — la factura no debería existir.
    Distinto de la anulación SRI (que sería un trámite tributario aparte).

    Reglas:
        - Debe existir.
        - Stat actual NO puede ser 'X' (ya eliminada).
        - No puede tener aplicaciones de cheques vigentes (chequesxfact).
        - No puede tener retenciones emitidas (retencion).
        - Conserva el histórico — sólo cambia stat a 'X' y deja motivo en
          observacion.
    """
    motivo = (motivo or "").strip()  # opcional. TMT 2026-05-13.

    fact = db.fetch_one(
        "SELECT id_factura, numf, codigo_cli, stat, saldo, importe, fecha "
        "FROM scintela.factura WHERE id_factura = %s",
        (id_factura,),
    )
    if not fact:
        raise ValueError("Factura inexistente.")
    stat_actual = (fact.get("stat") or "").upper()
    if stat_actual in STATS_ANULADAS:
        raise ValueError("La factura ya está anulada/eliminada.")

    # #33 (TMT 2026-05-14): no contar cheques YA reversados/anulados como
    # "aplicaciones vivas". Antes el COUNT(*) incluía aplicaciones de
    # cheques con stat X/3/R (terminales/eliminados) que ya no afectan el
    # saldo — eso bloqueaba la anulación sin sentido.
    aplicadas = db.fetch_one(
        """
        SELECT COUNT(*) AS n
          FROM scintela.chequesxfact cxf
          JOIN scintela.cheque c ON c.id_cheque = cxf.id_cheque
         WHERE cxf.id_fact = %s
           AND COALESCE(c.stat, '') NOT IN ('X', '3', 'R')
        """,
        (id_factura,),
    )
    if aplicadas and int(aplicadas["n"]) > 0:
        raise ValueError(
            "No se puede eliminar: hay cheques aplicados ACTIVOS a esta "
            "factura. Reversar las aplicaciones primero (cheques con "
            "stat X/3/R no cuentan)."
        )

    ret = db.fetch_one(
        "SELECT COUNT(*) AS n FROM scintela.retencion "
        "WHERE codigo_cli = %s AND numf = %s",
        (fact["codigo_cli"], fact["numf"]),
    )
    if ret and int(ret["n"]) > 0:
        raise ValueError(
            "No se puede eliminar: existen retenciones emitidas para esta factura. "
            "Anular las retenciones primero."
        )

    # Bug E fix (TMT 2026-05-16): scintela.factura no tiene columna
    # `observacion` — el motivo queda en bitácora vía registrar_bitacora()
    # que llama el view, y en metadata del mov_doble de reverso (ver abajo).
    with db.tx() as conn:
        rc = db.execute(
            """
            UPDATE scintela.factura
               SET stat = 'X',
                   usuario_modifica = %s,
                   fecha_modifica   = CURRENT_TIMESTAMP
             WHERE id_factura = %s
            """,
            (usuario, id_factura),
            conn=conn,
        )
        # Actualizar mov_doble — marcar el original como 'reversado' y
        # registrar un mov_doble de reverso linkeado. R2 (TMT 2026-05-14):
        # NO suprimir excepciones — antes había try/except: pass silencioso
        # que ocultaba bugs reales. Si falla, abortamos la anulación entera.
        import mov_doble as _md
        md_orig = db.fetch_one(
            """
            SELECT id_mov_doble, importe FROM scintela.mov_doble
             WHERE origen_table = 'factura'
               AND origen_id    = %s
               AND tipo IN ('factura_emitida','factura_devolucion')
               AND estado       = 'activo'
             ORDER BY id_mov_doble DESC LIMIT 1
            """,
            (id_factura,), conn=conn,
        )
        if md_orig:
            _md.registrar(
                conn=conn,
                tipo="reverso_factura_anulada",
                origen_table="factura",
                origen_id=id_factura,
                destino_table="factura",
                destino_id=id_factura,
                importe=float(md_orig.get("importe") or fact.get("importe") or 0),
                fecha=fact.get("fecha"),
                concepto=(f"ANULACION factura #{fact.get('numf') or id_factura}"
                          + (f" — {motivo}" if motivo else ""))[:200],
                usuario=usuario,
                metadata={"motivo": motivo or "",
                          "id_factura": id_factura,
                          "numf": fact.get("numf")},
                id_original=md_orig["id_mov_doble"],
            )
    return rc


def buscar(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    solo_abiertas: bool = False,
    limite: int = 10000,  # TMT 2026-05-20 v3 — antes 500 truncaba a las
                          # 500 más antiguas y el running ACUM final no
                          # coincidía con el total del header (que cuenta
                          # las 4500+ del bucket entero). Pedido dueña:
                          # 'facturas, acum total, no es igual al total'.
                          # 10k cubre con margen — si las facturas crecen
                          # mucho más, paginamos.
                          # TMT 2026-05-22 — paginación opt-in via `offset`.
                          # `lista()` ahora pagina default 500 con controles
                          # de página; el limite=10000 sigue funcionando
                          # para callers externos sin offset.
    offset: int = 0,
    vista: str = "cartera",
    cliente: str = "",
    monto_min: float | None = None,
    monto_max: float | None = None,
    estado: str = "",
    estados: list[str] | None = None,
    tipo: str | None = None,
) -> list[dict]:
    """Filtros:
        q             — busqueda libre (numero, numf_completo, nombre)
        cliente       — filtro EXPLÍCITO por codigo_cli; si exactamente
                        3 chars alfanuméricos, match exacto; si no, LIKE
        monto_min     — filtra importe >= monto_min
        monto_max     — filtra importe <= monto_max
        desde/hasta   — fecha (YYYY-MM-DD)
        solo_abiertas — saldo > 0 (deprecado a favor de `vista=cartera`)
        vista (TMT 2026-05-19 — pedido dueña):
            'cartera'    → stat IN (Z, A) AND saldo > 0  (cartera viva — DEFAULT)
            'estado'     → todas (antes 'todas'); filtrable con `estado`.
            'canceladas' → stat = T  (cobradas total)
            'eliminadas' → stat = X  (eliminadas — Y removido 2026-05-19, no existe)
        estado (TMT 2026-05-19, sólo aplica con vista='estado'):
            'Z' | 'A' | 'T' | 'X' | 'N' o '' (vacío = todos). 'Y' retirado.
            'N' = anulada en Asinfo (sincronizada por el bridge — 2026-05-22).
        estados (TMT 2026-05-19 v8, sólo aplica con vista='estado'):
            lista de stats — permite filtrar por VARIOS estados a la vez,
            ej. ['Z','A','T']. Lista vacía o None = todos. Si `estados` se
            pasa, tiene precedencia sobre `estado` (scalar legacy).
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # Nombre del cliente: match por PALABRAS (ver el comentario en el SQL).
    _nom_sql, _nom_params = busqueda.condicion(q, ("c.nombre",), prefijo="bqn")
    vista = (vista or "cartera").lower().strip()
    # Back-compat — la vista antes se llamaba 'todas'.
    if vista == "todas":
        vista = "estado"
    estado = (estado or "").upper().strip()
    # TMT 2026-05-19 v8 — multi-estado. Filtrar/normalizar.
    # TMT 2026-05-19 v8 — 'Y' retirado del universo de stats.
    estados_validos = ("Z", "A", "T", "X", "N")
    estados_lista = [
        s.upper().strip() for s in (estados or [])
        if s and s.upper().strip() in estados_validos
    ]
    # De-dup conservando orden.
    seen: set[str] = set()
    estados_lista = [s for s in estados_lista if not (s in seen or seen.add(s))]
    # Si vino solo `estado` scalar (legacy), promovemos a lista.
    if not estados_lista and estado in estados_validos:
        estados_lista = [estado]
    # Para el SQL: si está vacía → no filtra; si tiene Z, incluye también
    # los NULL/empty/' ' (legacy = Z implícito).
    estado_incluye_z = "Z" in estados_lista
    # Lista de stats explícitos (sin la Z especial).
    estados_para_in = [s for s in estados_lista if s != "Z"] or [""]
    cliente = (cliente or "").strip().upper()
    # Detector de "código de cliente exacto": 3 caracteres alfanuméricos.
    # Tanto en el campo `q` legacy como en el campo `cliente` nuevo:
    # si tiene 3 chars alfanum, match EXACTO sobre codigo_cli (no fuzzy).
    q_upper = q.upper() if q else ""
    # TMT 2026-06-23 (dueña): "código es código" — BED → Bedón exacto, sin buscar
    # nombres (se mantiene para q de 3 LETRAS). PERO si q es puramente numérico
    # (ej. "588") NO es código: cae a búsqueda por número de factura.
    es_q_codigo_exacto = (
        bool(q_upper) and len(q_upper) == 3
        and q_upper.replace("_", "").isalnum()
        and not q_upper.isdigit()
    )
    es_cli_codigo_exacto = bool(cliente) and len(cliente) == 3 and cliente.replace("_", "").isalnum()
    cliente_like = f"%{cliente}%" if cliente else None
    rows = db.fetch_all(
        """
        WITH filtradas AS (
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.vencimiento,
               f.codigo_cli, COALESCE(c.nombre, '') AS cliente,
               f.kg, f.importe, f.abono, f.retencion, f.saldo, f.stat,
               f.condic, f.tipo
        FROM scintela.factura f
        -- TMT 2026-06-10: LATERAL escalar para evitar fanout cuando un
        -- codigo_cli tiene >1 fila en scintela.cliente (drift TOTF $23k
        -- detectado por /admin/health/cartera-coherence). Mismo patrón
        -- que se aplicó a cheques previamente (lección 2026-05-19 v8).
        LEFT JOIN LATERAL (
          SELECT nombre FROM scintela.cliente
           WHERE codigo_cli = f.codigo_cli
           LIMIT 1
        ) c ON true
        WHERE (
                %(q)s IS NULL
             OR (
                  %(q_codigo_exacto)s
                  AND UPPER(TRIM(COALESCE(f.codigo_cli, ''))) = %(q_upper)s
                )
             OR (
                  NOT %(q_codigo_exacto)s
                  AND (
                       UPPER(f.codigo_cli) LIKE UPPER(%(like)s)
                    OR UPPER(COALESCE(f.numf_completo,'')) LIKE UPPER(%(like)s)
                    OR CAST(f.numf AS TEXT) LIKE %(like)s
                    -- TMT 2026-08-04 (dueña "no funciona si solo busco
                    -- condor"): el nombre del cliente matchea por PALABRAS
                    -- sueltas y sin acentos. Ver modules/_lib/busqueda.py.
                    OR (__NOMBRE_CLI__)
                  )
                )
              )
          -- Filtro explícito por cliente (campo nuevo, separado de `q`).
          AND (
                %(cliente)s IS NULL
             OR (%(cli_codigo_exacto)s
                 AND UPPER(TRIM(COALESCE(f.codigo_cli, ''))) = %(cliente)s)
             OR (NOT %(cli_codigo_exacto)s
                 AND UPPER(COALESCE(f.codigo_cli, '')) LIKE UPPER(%(cliente_like)s))
              )
          -- Filtro por monto USD (importe).
          AND (%(monto_min)s::numeric IS NULL OR COALESCE(f.importe, 0) >= %(monto_min)s::numeric)
          AND (%(monto_max)s::numeric IS NULL OR COALESCE(f.importe, 0) <= %(monto_max)s::numeric)
          -- TMT 2026-08-14 (dueña "no anda el filtro"): el TIPO se filtra
          -- ACÁ, en el WHERE. Antes se recortaba en Python después de traer
          -- la página, así que en la pestaña Estado (72 páginas) daba cero
          -- y los totales del encabezado —que salen de este mismo WHERE—
          -- nunca lo veían. `__SIN__` pide las que no tienen tipo, que es
          -- una pregunta legítima y antes no se podía hacer.
          AND (%(tipo)s::text IS NULL
               OR (%(tipo)s = '__SIN__' AND NULLIF(TRIM(COALESCE(f.tipo,'')),'') IS NULL)
               OR UPPER(TRIM(COALESCE(f.tipo,''))) = %(tipo)s)
          AND (%(desde)s::date IS NULL OR f.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR f.fecha <= %(hasta)s::date)
          AND (NOT %(solo_abiertas)s OR COALESCE(f.saldo, 0) > 0)
          AND (
                %(vista)s = 'estado'
             -- TMT 2026-05-19 v7 — dueña: el total en /resultados (b.totf)
             -- no coincidía con el de /facturas vista=cartera. Bug:
             -- informes.totf() NO filtra por signo (incluye sobrepagos
             -- saldo<0 que netean cartera, fórmula dBase legacy). Acá
             -- teníamos saldo > 0 que excluía las 664 facturas negativas
             -- (~$-293k). Cambio: saldo <> 0 para que ambos números cuadren.
             OR (%(vista)s = 'cartera'
                 AND COALESCE(f.saldo, 0) <> 0
                 AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
                 -- TMT 2026-06-10 decisión dueña: backfill automático Asinfo
                 -- NO es cartera (solo lo cargado a propósito / dBase).
                 AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill')
             OR (%(vista)s = 'canceladas' AND f.stat = 'T')
             OR (%(vista)s = 'eliminadas' AND f.stat = 'X')
             -- Federico 2026-08-12: vista 'facturado' = MISMO universo que la
             -- "Venta del mes" de Resultados (ventas_mes_corriente_resultado):
             -- todas las facturas menos eliminadas (stat X) y menos backfill
             -- Asinfo, sin filtro de saldo (cobradas + pendientes + canceladas).
             -- Usada por el botón "Mes actual" de /facturas para que el total cuadre con Resultados.
             OR (%(vista)s = 'facturado'
                 AND (f.stat IS NULL OR f.stat <> 'X')
                 AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill')
          )
          -- TMT 2026-05-19 v8 — filtro multi-estado (lista de stats).
          -- Si la lista está vacía → no filtra (lo marcamos con flag
          -- `estados_vacia`). Si tiene Z, ese matchea NULL/empty/' ' también
          -- (legacy = Z implícito).
          AND (
                %(estados_vacia)s
             OR (%(estado_incluye_z)s
                 AND (f.stat IS NULL OR f.stat IN ('Z','',' ')))
             OR f.stat = ANY(%(estados_para_in)s::text[])
          )
        )
        -- TMT 2026-08-05 (dueña: "a nadie le importa la pagina visible") —
        -- el ACUM se calcula con una window sobre el UNIVERSO filtrado
        -- ENTERO, antes del LIMIT/OFFSET. Antes se acumulaba en Python
        -- sobre las filas de la pagina, asi que con paginacion la fila de
        -- arriba mostraba el corrido de esas 500 y no cerraba nunca con el
        -- total del header. Ahora la fila mas nueva de la pagina 1 = total
        -- del header, y el corrido sigue siendo continuo pagina a pagina.
        -- NULLS FIRST + ROWS replica exactamente el orden que hacia Python
        -- (fecha NULL -> date.min, numf NULL -> 0, fila por fila sin peers).
        SELECT fi.*,
               SUM(COALESCE(fi.saldo, 0)) OVER (
                 ORDER BY fi.fecha ASC NULLS FIRST, fi.numf ASC NULLS FIRST,
                          fi.id_factura ASC
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS saldo_acumulado
          FROM filtradas fi
        -- El desempate por id_factura NO es cosmetico: hay numf REPETIDOS
        -- (10719, 10724 x3, ...). Con (fecha, numf) empatados el orden de la
        -- window y el del LIMIT/OFFSET se resuelven por separado, asi que el
        -- corrido puede saltar en el borde de pagina y la paginacion puede
        -- saltear o repetir una fila. Los dos ORDER BY tienen que ser
        -- exactamente inversos y totales.
        ORDER BY fi.fecha DESC, fi.numf DESC, fi.id_factura DESC
        LIMIT %(limite)s OFFSET %(offset)s
        """.replace("__NOMBRE_CLI__", _nom_sql or "FALSE"),
        {
            **_nom_params,
            "q": q or None, "like": like,
            "q_upper": q_upper, "q_codigo_exacto": es_q_codigo_exacto,
            "cliente": cliente or None, "cliente_like": cliente_like,
            "cli_codigo_exacto": es_cli_codigo_exacto,
            "monto_min": monto_min, "monto_max": monto_max,
            "desde": desde or None, "hasta": hasta or None,
            "solo_abiertas": solo_abiertas,
            "vista": vista,
            "estado": estado,
            "tipo": (tipo or "").strip().upper() or None,
            "estados_vacia": not estados_lista,
            "estado_incluye_z": estado_incluye_z,
            "estados_para_in": estados_para_in,
            "limite": limite,
            "offset": offset,
        },
    ) or []
    # El ACUM (`saldo_acumulado`) lo calcula el SQL de arriba con una window
    # sobre el universo filtrado entero — ver el comentario del CTE.
    # TMT 2026-05-20 v2 — acumula SALDO (no importe), para que cierre con el
    # header (que muestra SUM(saldo) del bucket cartera). Pedido dueña: "el
    # total de arriba no coincide con el acumulado. es porque no tenes en
    # cuenta las negativas". Devoluciones y sobrepagos (saldo negativo)
    # restan del corrido, lo mismo que hacen en el header.
    from datetime import date as _date
    # La pantalla muestra las facturas en orden DESC (pedido dueña
    # 2026-05-21: las nuevas arriba). Reordenamos en Python para que las
    # filas sin fecha queden al final (el SQL las pondria primero) y para
    # no depender del orden que devuelva el driver.
    rows_asc = sorted(rows, key=lambda r: (r.get("fecha") or _date.min,
                                           r.get("numf") or 0,
                                           r.get("id_factura") or 0))
    for r in rows_asc:
        r["saldo_acumulado"] = float(r.get("saldo_acumulado") or 0)
    return list(reversed(rows_asc))


def contar_filtrado(
    q: str = "",
    desde: str | None = None,
    hasta: str | None = None,
    solo_abiertas: bool = False,
    vista: str = "cartera",
    cliente: str = "",
    monto_min: float | None = None,
    monto_max: float | None = None,
    estado: str = "",
    estados: list[str] | None = None,
    tipo: str | None = None,
) -> dict:
    """COUNT(*) + SUM(saldo) + SUM(importe) con los MISMOS filtros que `buscar()`.

    TMT 2026-05-22 — usado por la paginación de `/facturas` para mostrar
    "Mostrando X-Y de Z" y el total de importes / saldos del UNIVERSO filtrado
    (no solo la página visible).
    """
    q = (q or "").strip()
    like = f"%{q}%" if q else None
    # Nombre del cliente: match por PALABRAS (ver el comentario en el SQL).
    _nom_sql, _nom_params = busqueda.condicion(q, ("c.nombre",), prefijo="bqn")
    vista = (vista or "cartera").lower().strip()
    if vista == "todas":
        vista = "estado"
    estado = (estado or "").upper().strip()
    estados_validos = ("Z", "A", "T", "X", "N")
    estados_lista = [
        s.upper().strip() for s in (estados or [])
        if s and s.upper().strip() in estados_validos
    ]
    seen: set[str] = set()
    estados_lista = [s for s in estados_lista if not (s in seen or seen.add(s))]
    if not estados_lista and estado in estados_validos:
        estados_lista = [estado]
    estado_incluye_z = "Z" in estados_lista
    estados_para_in = [s for s in estados_lista if s != "Z"] or [""]
    cliente = (cliente or "").strip().upper()
    q_upper = q.upper() if q else ""
    # TMT 2026-06-23 (dueña): "código es código" — BED → Bedón exacto, sin buscar
    # nombres (se mantiene para q de 3 LETRAS). PERO si q es puramente numérico
    # (ej. "588") NO es código: cae a búsqueda por número de factura.
    es_q_codigo_exacto = (
        bool(q_upper) and len(q_upper) == 3
        and q_upper.replace("_", "").isalnum()
        and not q_upper.isdigit()
    )
    es_cli_codigo_exacto = bool(cliente) and len(cliente) == 3 and cliente.replace("_", "").isalnum()
    cliente_like = f"%{cliente}%" if cliente else None
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(f.importe), 0) AS total_importe,
               COALESCE(SUM(f.saldo), 0)   AS total_saldo,
               COALESCE(SUM(f.kg), 0)      AS total_kg
        FROM scintela.factura f
        -- TMT 2026-06-10: LATERAL escalar (mismo motivo que buscar() arriba)
        LEFT JOIN LATERAL (
          SELECT nombre FROM scintela.cliente
           WHERE codigo_cli = f.codigo_cli
           LIMIT 1
        ) c ON true
        WHERE (
                %(q)s IS NULL
             OR (
                  %(q_codigo_exacto)s
                  AND UPPER(TRIM(COALESCE(f.codigo_cli, ''))) = %(q_upper)s
                )
             OR (
                  NOT %(q_codigo_exacto)s
                  AND (
                       UPPER(f.codigo_cli) LIKE UPPER(%(like)s)
                    OR UPPER(COALESCE(f.numf_completo,'')) LIKE UPPER(%(like)s)
                    OR CAST(f.numf AS TEXT) LIKE %(like)s
                    -- TMT 2026-08-04 (dueña "no funciona si solo busco
                    -- condor"): el nombre del cliente matchea por PALABRAS
                    -- sueltas y sin acentos. Ver modules/_lib/busqueda.py.
                    OR (__NOMBRE_CLI__)
                  )
                )
              )
          AND (
                %(cliente)s IS NULL
             OR (%(cli_codigo_exacto)s
                 AND UPPER(TRIM(COALESCE(f.codigo_cli, ''))) = %(cliente)s)
             OR (NOT %(cli_codigo_exacto)s
                 AND UPPER(COALESCE(f.codigo_cli, '')) LIKE UPPER(%(cliente_like)s))
              )
          AND (%(monto_min)s::numeric IS NULL OR COALESCE(f.importe, 0) >= %(monto_min)s::numeric)
          AND (%(monto_max)s::numeric IS NULL OR COALESCE(f.importe, 0) <= %(monto_max)s::numeric)
          -- TMT 2026-08-14 (dueña "no anda el filtro"): el TIPO se filtra
          -- ACÁ, en el WHERE. Antes se recortaba en Python después de traer
          -- la página, así que en la pestaña Estado (72 páginas) daba cero
          -- y los totales del encabezado —que salen de este mismo WHERE—
          -- nunca lo veían. `__SIN__` pide las que no tienen tipo, que es
          -- una pregunta legítima y antes no se podía hacer.
          AND (%(tipo)s::text IS NULL
               OR (%(tipo)s = '__SIN__' AND NULLIF(TRIM(COALESCE(f.tipo,'')),'') IS NULL)
               OR UPPER(TRIM(COALESCE(f.tipo,''))) = %(tipo)s)
          AND (%(desde)s::date IS NULL OR f.fecha >= %(desde)s::date)
          AND (%(hasta)s::date IS NULL OR f.fecha <= %(hasta)s::date)
          AND (NOT %(solo_abiertas)s OR COALESCE(f.saldo, 0) > 0)
          AND (
                %(vista)s = 'estado'
             OR (%(vista)s = 'cartera'
                 AND COALESCE(f.saldo, 0) <> 0
                 AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
                 -- TMT 2026-06-10 decisión dueña: backfill automático Asinfo
                 -- NO es cartera (solo lo cargado a propósito / dBase).
                 AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill')
             OR (%(vista)s = 'canceladas' AND f.stat = 'T')
             OR (%(vista)s = 'eliminadas' AND f.stat = 'X')
             -- Federico 2026-08-12: 'facturado' = universo de la Venta del mes de
             -- Resultados (stat ≠ X + sin backfill, sin filtro de saldo).
             OR (%(vista)s = 'facturado'
                 AND (f.stat IS NULL OR f.stat <> 'X')
                 AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill')
          )
          AND (
                %(estados_vacia)s
             OR (%(estado_incluye_z)s
                 AND (f.stat IS NULL OR f.stat IN ('Z','',' ')))
             OR f.stat = ANY(%(estados_para_in)s::text[])
          )
        """.replace("__NOMBRE_CLI__", _nom_sql or "FALSE"),
        {
            **_nom_params,
            "q": q or None, "like": like,
            "q_upper": q_upper, "q_codigo_exacto": es_q_codigo_exacto,
            "cliente": cliente or None, "cliente_like": cliente_like,
            "cli_codigo_exacto": es_cli_codigo_exacto,
            "monto_min": monto_min, "monto_max": monto_max,
            "desde": desde or None, "hasta": hasta or None,
            "solo_abiertas": solo_abiertas,
            "vista": vista,
            "estado": estado,
            "tipo": (tipo or "").strip().upper() or None,
            "estados_vacia": not estados_lista,
            "estado_incluye_z": estado_incluye_z,
            "estados_para_in": estados_para_in,
        },
    ) or {}
    return {
        "n": int(row.get("n") or 0),
        "total_importe": float(row.get("total_importe") or 0),
        "total_saldo": float(row.get("total_saldo") or 0),
        "total_kg": float(row.get("total_kg") or 0),
    }


def conteos_por_vista() -> dict:
    """Conteos rápidos para los tabs: cartera / canceladas / eliminadas / todas."""
    rows = db.fetch_all(
        """
        SELECT
          -- TMT 2026-05-19 v7 — alineado con buscar() y informes.totf():
          -- "cartera" = stat ∈ (Z,A,blank) AND saldo <> 0 (incluye saldos
          -- negativos por sobrepago — netean cartera).
          CASE
            WHEN COALESCE(saldo, 0) <> 0
                 AND (stat IS NULL OR stat IN ('Z','A','',' '))
                 -- TMT 2026-06-10: backfill automático fuera de cartera
                 AND COALESCE(usuario_crea, '') <> 'asinfo-backfill' THEN 'cartera'
            WHEN stat = 'T'                                         THEN 'canceladas'
            WHEN stat = 'X'                                         THEN 'eliminadas'
            ELSE 'otras'
          END                                AS bucket,
          COUNT(*)                           AS n,
          COALESCE(SUM(saldo), 0)            AS total_saldo,
          COALESCE(SUM(importe), 0)          AS total_importe,
          COALESCE(SUM(kg), 0)               AS total_kg
        FROM scintela.factura
        GROUP BY 1
        """
    ) or []
    out = {r["bucket"]: dict(r) for r in rows}
    # TMT 2026-05-19 — 'estado' es el bucket que abarca todo (= antes 'todas').
    # Mantengo 'todas' como alias por back-compat con cualquier caller externo.
    total_row = {
        "n": sum(r["n"] for r in rows),
        "total_saldo": sum(float(r["total_saldo"] or 0) for r in rows),
        "total_importe": sum(float(r["total_importe"] or 0) for r in rows),
        "total_kg": sum(float(r["total_kg"] or 0) for r in rows),
    }
    out["estado"] = total_row
    out["todas"] = total_row
    return out
