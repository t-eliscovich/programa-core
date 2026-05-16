"""Flask extension singletons.

Lives in its own module so blueprints (auth.py) can `from extensions import
limiter` to attach route-level decorators without creating an import cycle
with app.py, which imports those blueprints.
"""
import os

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)
