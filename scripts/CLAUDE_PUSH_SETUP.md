# Setup auto-push de Claude (5 minutos)

Permite que Claude commitee + pushee al repo sin que tengas que correr
`push_to_github.sh` manual cada vez.

## Paso 1 — Crear el PAT (Personal Access Token)

1. Andá a https://github.com/settings/personal-access-tokens/new
   (es "fine-grained", no el legacy).
2. Settings:
   - **Token name**: `claude-programa-core`
   - **Expiration**: 90 días (o lo que quieras — renovás antes de que venza).
   - **Resource owner**: `t-eliscovich`
   - **Repository access** → "Only select repositories" → elegí `programa-core`.
3. **Permissions** (es lo más importante):
   - Repository permissions → **Contents: Read and write**
   - Repository permissions → **Metadata: Read-only** (se selecciona solo)
   - Todo lo demás dejalo en `No access`.
4. Click **Generate token** abajo. GitHub te muestra el token una sola vez:
   empieza con `github_pat_…`. **Copialo ahora.**

## Paso 2 — Guardar el token localmente

Desde la raíz del repo, en tu Mac:

```bash
cd ~/Documents/Claude/Projects/Programa\ Core
echo 'github_pat_PEGA_ACA_EL_TOKEN' > .gh_pat
chmod 600 .gh_pat
```

El archivo `.gh_pat` ya está en `.gitignore`, así que NO se va a subir al repo.
Permisos `600` = solo tu usuario puede leerlo.

## Paso 3 — Probar

```bash
python scripts/claude_push.py "test auto-push desde Claude"
```

Si todo está bien, ves:

```
→ Repo: t-eliscovich/programa-core (branch main)
→ Parent commit: a1b2c3d4
✓ Nada para commitear. Local == remoto.
```

(o "N archivos cambiados/nuevos. Subiendo blobs…" si tenés cambios).

## Paso 4 — De ahora en adelante

Cuando Claude termine un batch de edits, simplemente le decís
**"pushea"** y va a correr:

```bash
python scripts/claude_push.py "mensaje del batch"
```

No necesitás aprobar nada — el sandbox tiene acceso al `.gh_pat` y a la
REST API de GitHub. Cada commit queda con autor "Claude" (porque la
API usa tu PAT pero el commit lo firma como anónimo a menos que
configures el author).

## Si querés que los commits salgan firmados como Claude

Agregá un archivo `.claude_push.json` en la raíz (también gitignored):

```json
{
  "owner":  "t-eliscovich",
  "repo":   "programa-core",
  "branch": "main"
}
```

(El owner/repo ya están como default; este file solo te sirve si querés
overrides — ej. para una branch de feature).

## Revocar el token

Si en algún momento querés revocar:
1. https://github.com/settings/personal-access-tokens
2. Click en el token "claude-programa-core" → **Revoke**.

Listo, el sandbox pierde acceso inmediato.

## Limitaciones del script

- No maneja conflictos. Si vos pushiás cambios desde otra sesión mientras
  Claude trabaja, la próxima push de Claude va a fallar con "fast-forward
  not allowed" y vas a tener que mergear manual.
- No registra deleciones de archivos en el commit (la API requiere
  listarlas con `mode: "100644", sha: null`). Si necesito borrar un
  archivo, te aviso y lo hacés vos.
- Archivos > 1MB se ignoran (típicamente assets binarios; el script no
  está pensado para eso).
