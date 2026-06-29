"""Endpoint /healthz para healthchecks (Docker, ELB, k8s, monitoring).

Sin auth. Sin side-effects. Tiene que responder rápido y barato.
"""

# TMT 2026-06-29: deploy de verificación (persistencia SECRET_KEY entre deploys).
