"""La ventana contada en castellano.

TMT 2026-08-07: *"un botón 'explicame esta fila' que arme el texto… en vez de
leer siete columnas, un renglón que diga qué pasó"*.

Es lo que la dueña hace hoy de cabeza cada vez que abre una fila: mirar doce
números y traducirlos a un hecho. La traducción no es obvia, y ahí está el
valor: **hay movimientos que engañan**.

    · Una cobranza NO sube la utilidad. El cheque entra y la factura baja: la
      plata cambia de lugar. Si la frase dijera "entraron $2.366" estaría
      mintiendo sobre el resultado.
    · Un pasivo que BAJA sube la utilidad, aunque haya salido plata del banco.
    · Un anticipo que se vuelve stock no es plata nueva: es la misma, con otro
      nombre, del lado de adentro.
    · La amortización baja la utilidad todos los días y no la mueve nadie.

🚨 **Determinista, sin modelo.** Los números ya cierran al centavo contra el
balance; acá sólo se los ordena y se los nombra. Meter un modelo a redactar
sobre plata sería reintroducir la hipótesis en la pantalla que se hizo para
sacarlas — y un número inventado que suena convincente es peor que ninguno.
"""
from __future__ import annotations

#: Debajo de esto un movimiento no se menciona: es ruido.
UMBRAL = 1.0

#: Cuántos hechos entran en la frase antes de resumir el resto.
TOPE = 3


def _n(v: float) -> str:
    """Un importe en formato Ecuador, sin decimales y sin signo."""
    return f"{abs(v):,.0f}".replace(",", ".")


def _frase_traspasos(grupos: list[dict]) -> str:
    """Lo que cambió de lugar sin crear resultado, dicho para que no confunda.

    Es la parte que más se malinterpreta: una cobranza grande hace ruido en la
    tabla —dos columnas se mueven— y no aporta un peso.
    """
    tras = [g for g in grupos if g.get("familia") == "traspaso"
            and abs(g.get("aporte") or 0) < UMBRAL]
    if not tras:
        return ""
    bruto = max((abs(g.get("bruto") or g.get("aporte") or 0) for g in tras),
                default=0)
    que = tras[0].get("texto") or "un movimiento"
    if bruto >= UMBRAL:
        return f"{que} cambió de lugar $ {_n(bruto)}: no movió la utilidad."
    return f"{que}: cambió de lugar, no movió la utilidad."


def explicar(fila: dict) -> str:
    """Una o dos frases sobre lo que pasó en esa ventana. '' si no hay nada.

    La forma importa: arranca por el titular —cuánto se movió la utilidad—,
    sigue con los dos o tres hechos que más pesaron, y recién al final aclara
    lo que engaña. Una enumeración de siete cosas es la tabla otra vez, y de
    eso justamente se quería salir.
    """
    if not fila:
        return ""
    if fila.get("sin_registro"):
        return ("Esta foto es anterior a la grabadora: se sabe cuánto se movió "
                "cada componente, pero no qué documentos lo movieron.")

    grupos = [g for g in (fila.get("resumen") or [])
              if abs(g.get("aporte") or 0) >= UMBRAL]
    d = float(fila.get("d_utilidad") or 0)
    if not grupos:
        return "En esta ventana no se movió nada." if abs(d) < UMBRAL else ""

    titular = (f"La utilidad {'subió' if d > 0 else 'bajó'} $ {_n(d)}"
               if abs(d) >= UMBRAL else "La utilidad quedó igual")

    # Los que mueven el resultado; lo que netea cero se cuenta aparte.
    mueven = [g for g in grupos if g.get("familia") != "traspaso"]
    partes = []
    for g in mueven[:TOPE]:
        partes.append(f"{g['texto']} ({'+' if g['aporte'] > 0 else '−'}"
                      f"$ {_n(g['aporte'])})")
    if len(mueven) > TOPE:
        resto = sum(g["aporte"] for g in mueven[TOPE:])
        partes.append(f"y otros {len(mueven) - TOPE}, "
                      f"{'+' if resto > 0 else '−'}$ {_n(resto)}")

    if partes:
        cuerpo = (partes[0] if len(partes) == 1
                  else ", ".join(partes[:-1]) + " y " + partes[-1]
                  if not partes[-1].startswith("y otros")
                  else ", ".join(partes[:-1]) + " " + partes[-1])
        frase = f"{titular}: {cuerpo}."
    else:
        frase = titular + "."

    extra = _frase_traspasos(fila.get("resumen") or [])
    if extra:
        frase += " " + extra[0].upper() + extra[1:]

    ciego = [g for g in grupos if g.get("familia") == "sin_explicar"]
    if ciego:
        frase += (f" Queda $ {_n(sum(g['aporte'] for g in ciego))} sin poder "
                  f"atribuir a ningún documento.")
    return frase
