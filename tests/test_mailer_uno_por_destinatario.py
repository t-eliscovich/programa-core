"""El mail sale UNO POR DESTINATARIO, no uno solo con todos juntos.

🚨 TMT 2026-08-12: `mailer.enviar()` metía a todos los destinatarios en el
mismo `To`. Una dirección sin verificar en SES hizo rebotar el envío entero
(`MessageRejected ... Email address is not verified ... feliscovich@gmail.com`)
y esa noche **nadie** recibió la nota del cierre por culpa de una sola
dirección.

Y hay una segunda razón, que aparece con el portal del cliente: un mail con
todos los correos a la vista no se le manda a un cliente.

La decisión fina está en `ok`: es **"salió al menos uno"**, no "salieron
todos". El que llama marca la nota como mandada y, si `ok` es False, la libera
para reintentar — reintentar le mandaría el mail de nuevo a los que ya lo
recibieron. El fallo parcial no se esconde: va en `motivo` y en `fallidos`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules._lib import mailer  # noqa: E402

TRES = ["ana@intela.com.ec", "beto@intela.com.ec", "caro@intela.com.ec"]


class _SESFalso:
    """Un SES de mentira que puede hacer rebotar direcciones elegidas."""

    def __init__(self, rebotan=()):
        self.rebotan = set(rebotan)
        self.enviados = []

    def send_email(self, **kw):
        destinos = kw["Destination"]["ToAddresses"]
        self.enviados.append(list(destinos))
        for d in destinos:
            if d in self.rebotan:
                raise RuntimeError(
                    f"MessageRejected: Email address is not verified: {d}")
        return {"MessageId": "m-" + destinos[0].split("@")[0]}


def _con(ses):
    """Pone el SES falso y da por habilitado el envío."""
    import types
    fake_boto3 = types.SimpleNamespace(client=lambda *a, **k: ses)
    return (patch.object(mailer, "habilitado", return_value=True),
            patch.object(mailer, "remitente", return_value="no-reply@intela.com.ec"),
            patch.dict(sys.modules, {"boto3": fake_boto3}))


def _enviar(ses, destinatarios=TRES, **kw):
    a, b, c = _con(ses)
    with a, b, c:
        return mailer.enviar("asunto", "texto", destinatarios, **kw)


# ---------------------------------------------------------------------------
# Uno por cabeza
# ---------------------------------------------------------------------------


def test_manda_un_mail_por_cada_uno():
    ses = _SESFalso()
    r = _enviar(ses)
    assert ses.enviados == [["ana@intela.com.ec"],
                            ["beto@intela.com.ec"],
                            ["caro@intela.com.ec"]]
    assert r["ok"] is True
    assert r["enviados"] == 3 and r["fallidos"] == 0


def test_nadie_ve_el_correo_de_los_demas():
    """Cada envío lleva UNA sola dirección: es lo que permite mandarle esto a
    un cliente sin mostrarle la lista entera."""
    ses = _SESFalso()
    _enviar(ses)
    assert all(len(d) == 1 for d in ses.enviados)


# ---------------------------------------------------------------------------
# El que rebota se cae solo
# ---------------------------------------------------------------------------


def test_una_direccion_rota_no_deja_a_los_demas_sin_mail():
    """⭐ El bug del 12/08, exacto: la dirección sin verificar es la del medio
    y los otros dos tienen que recibirlo igual."""
    ses = _SESFalso(rebotan={"beto@intela.com.ec"})
    r = _enviar(ses)
    assert r["ok"] is True
    assert r["enviados"] == 2 and r["fallidos"] == 1
    assert len(ses.enviados) == 3, "el que falla no puede cortar el for"


def test_el_fallo_parcial_no_se_esconde_detras_del_ok():
    ses = _SESFalso(rebotan={"beto@intela.com.ec"})
    r = _enviar(ses)
    assert "beto@intela.com.ec" in r["motivo"], (
        "si el motivo queda vacío cuando salió alguno, el que no lo recibió no "
        "se entera nunca")
    assert "not verified" in r["motivo"]


def test_el_detalle_dice_quien_si_y_quien_no():
    ses = _SESFalso(rebotan={"caro@intela.com.ec"})
    r = _enviar(ses)
    por_correo = {d["correo"]: d for d in r["detalle"]}
    assert por_correo["ana@intela.com.ec"]["ok"] is True
    assert por_correo["ana@intela.com.ec"]["id"] == "m-ana"
    assert por_correo["caro@intela.com.ec"]["ok"] is False
    assert por_correo["caro@intela.com.ec"]["motivo"]


def test_si_rebotan_todos_no_esta_ok():
    ses = _SESFalso(rebotan=set(TRES))
    r = _enviar(ses)
    assert r["ok"] is False
    assert r["enviados"] == 0 and r["fallidos"] == 3


def test_muchos_fallidos_no_arman_un_motivo_kilometrico():
    muchos = [f"u{i}@intela.com.ec" for i in range(12)]
    ses = _SESFalso(rebotan=set(muchos[:-1]))
    r = _enviar(ses, muchos)
    assert "y 6 más" in r["motivo"]
    assert len(r["motivo"]) < 300


# ---------------------------------------------------------------------------
# Lo que no cambió
# ---------------------------------------------------------------------------


def test_el_id_es_el_del_primero_que_salio():
    """Es lo único que sirve para rastrear en SES; los otros están en detalle."""
    ses = _SESFalso(rebotan={"ana@intela.com.ec"})
    r = _enviar(ses)
    assert r["id"] == "m-beto"


def test_sigue_yendo_en_texto_y_en_html():
    """Nunca HTML solo: un mail sin alternativa de texto puntúa peor en los
    filtros de spam, y este mail ya tuvo ese problema."""
    ses = _SESFalso()
    guardado = {}
    original = ses.send_email

    def _espia(**kw):
        guardado.update(kw)
        return original(**kw)
    ses.send_email = _espia
    _enviar(ses, ["ana@intela.com.ec"], html="<b>hola</b>")
    cuerpo = guardado["Message"]["Body"]
    assert cuerpo["Text"]["Data"] == "texto"
    assert cuerpo["Html"]["Data"] == "<b>hola</b>"


def test_sin_destinatarios_no_toca_ses():
    ses = _SESFalso()
    r = _enviar(ses, [])
    assert r["ok"] is False and r["motivo"] == "sin destinatarios"
    assert ses.enviados == []


def test_apagado_por_entorno_no_manda():
    ses = _SESFalso()
    with patch.object(mailer, "habilitado", return_value=False):
        r = mailer.enviar("a", "t", TRES)
    assert r["ok"] is False
    assert ses.enviados == []


def test_nunca_lanza_aunque_ses_ni_se_pueda_abrir():
    """Esto cuelga del hilo de fondo: un mail que no sale no puede tumbar la
    captura del cierre."""
    with patch.object(mailer, "habilitado", return_value=True), \
            patch.object(mailer, "remitente", side_effect=RuntimeError("boom")):
        r = mailer.enviar("a", "t", TRES)
    assert r["ok"] is False and "boom" in r["motivo"]
