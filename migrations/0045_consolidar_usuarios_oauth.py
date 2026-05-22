"""Consolidar usuarios duplicados (legacy username + OAuth Google).

Pedido dueña 2026-05-22: "federico y feliscovich son lo mismo; tamara y
teliscovich son lo mismo; me gusta mantener el log in con google.
Podemos poner usuario y usuario mail?"

Quedaron 2 entradas por dueño porque el login con Google creaba un usuario
distinto al legacy username/password. Esta migración consolida:

  1) Agrega columna `email` a seguridad.usuario (varchar, nullable).
  2) Para cada par (legacy, oauth):
       - copia el `email` (= username de la entrada OAuth) a la fila legacy
       - copia la clave (FED/TAM) a la fila legacy si todavía la tenía OAuth
       - desactiva la entrada OAuth (activo=FALSE)
  3) Login con Google (modules/auth_google/views.py) ahora resuelve por
     `email` en vez de por `username`, así sigue funcionando contra la
     fila legacy una vez consolidada.

Idempotente: si la columna ya existe o el email ya está copiado, no hace
nada. Re-ejecutable.
"""

from __future__ import annotations

import os

# Pares (username legacy, email OAuth).
PARES = [
    ("federico", "feliscovich@gmail.com"),
    ("tamara", "teliscovich@gmail.com"),
]


def run(conn) -> None:
    cur = conn.cursor()

    # 1) Asegurar columna `email` (varchar nullable, sin UNIQUE para no
    #    romper si dos rows accidentalmente tienen el mismo email durante
    #    la consolidación; podemos agregar UNIQUE después si hace falta).
    cur.execute("""
        ALTER TABLE seguridad.usuario
            ADD COLUMN IF NOT EXISTS email varchar(150)
    """)

    consolidados = []
    sin_oauth = []
    for legacy_user, oauth_email in PARES:
        cur.execute(
            "SELECT id_usuario, clave, email FROM seguridad.usuario "
            "WHERE lower(username) = lower(%s)",
            (legacy_user,),
        )
        legacy = cur.fetchone()
        if not legacy:
            continue
        legacy_id, legacy_clave, legacy_email = legacy

        cur.execute(
            "SELECT id_usuario, clave, activo FROM seguridad.usuario "
            "WHERE lower(username) = lower(%s)",
            (oauth_email,),
        )
        oauth = cur.fetchone()

        # Copiar el email a la fila legacy (siempre — es idempotente).
        if (legacy_email or "").lower() != oauth_email.lower():
            cur.execute(
                "UPDATE seguridad.usuario SET email = %s WHERE id_usuario = %s",
                (oauth_email.lower(), legacy_id),
            )

        if not oauth:
            sin_oauth.append((legacy_user, oauth_email))
            continue
        oauth_id, oauth_clave, oauth_activo = oauth

        # Si la legacy no tenía clave pero la OAuth sí, copiar.
        if not legacy_clave and oauth_clave:
            cur.execute(
                "UPDATE seguridad.usuario SET clave = %s WHERE id_usuario = %s",
                (oauth_clave, legacy_id),
            )

        # Desactivar la entrada OAuth (la legacy se queda activa con el email
        # asociado y el login con Google va a resolver hacia ella).
        if oauth_activo:
            cur.execute(
                "UPDATE seguridad.usuario SET activo = FALSE WHERE id_usuario = %s",
                (oauth_id,),
            )
        consolidados.append((legacy_user, oauth_email))

    cur.close()

    if os.environ.get("MIGRATE_VERBOSE"):
        if consolidados:
            print("    consolidados (OAuth desactivada, email pegado al legacy):")
            for u, m in consolidados:
                print(f"      - {u} <- {m}")
        if sin_oauth:
            print("    sin contraparte OAuth (solo email seteado en legacy):")
            for u, m in sin_oauth:
                print(f"      - {u} ({m})")
