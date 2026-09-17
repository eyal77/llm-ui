"""LLM Compare — FastAPI backend.

    uvicorn app.main:app --reload            # http://127.0.0.1:8000

Endpoints are plain `def` functions on purpose: FastAPI runs them in a thread
pool, so the browser can fire one /api/generate per model in parallel and the
comparison table fills in as each provider answers.
"""

from __future__ import annotations

import hmac
import re
import secrets
import time
import traceback
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, providers

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_TTL_SECONDS = 8 * 3600

app = FastAPI(title="LLM Compare")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

LOGO_DIR = STATIC_DIR / "logos"
LOGO_EXTENSIONS = (".svg", ".png", ".webp", ".jpg", ".jpeg", ".ico")

_sessions: dict[str, float] = {}  # admin token -> expiry (in memory: a restart logs you out)


def logo_url(provider: str) -> str | None:
    """URL of app/static/logos/<provider>.<ext> if you've added one (the UI falls back to a badge)."""
    for ext in LOGO_EXTENSIONS:
        path = LOGO_DIR / f"{provider}{ext}"
        if path.is_file():
            # The mtime query string makes the browser pick up a replaced file.
            return f"/static/logos/{path.name}?v={int(path.stat().st_mtime)}"
    return None


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


# --- Models & generation ----------------------------------------------------------


@app.get("/api/models")
def list_models():
    env = config.read_env()
    out = []
    for spec in config.PROVIDERS.values():
        configured = config.is_configured(spec, env)
        models = config.provider_models(spec, env)
        out.append(
            {
                "provider": spec.id,
                "label": spec.label,
                "logo": logo_url(spec.id),
                "configured": configured,
                "supports": spec.supports,
                "notes": spec.notes,
                "models": [{"id": f"{spec.id}:{m}", "model": m} for m in models] if configured else [],
            }
        )
    return {"providers": out, "defaults": providers.DEFAULTS}


class GenerateRequest(BaseModel):
    provider: str
    model: str = Field(min_length=1)
    system: str = ""
    user: str = Field(min_length=1)
    max_tokens: int = Field(providers.DEFAULTS["max_tokens"], ge=1, le=200_000)
    temperature: float = Field(providers.DEFAULTS["temperature"], ge=0, le=2)
    top_p: float = Field(providers.DEFAULTS["top_p"], ge=0, le=1)
    top_k: int = Field(providers.DEFAULTS["top_k"], ge=1, le=1000)
    use_temperature: bool = providers.DEFAULTS["use_temperature"]
    use_top_p: bool = providers.DEFAULTS["use_top_p"]
    use_top_k: bool = providers.DEFAULTS["use_top_k"]
    reasoning: bool = providers.DEFAULTS["reasoning"]


@app.post("/api/generate")
def generate(req: GenerateRequest):
    """Always 200: a failing provider is a result to show, not a broken request."""
    env = config.read_env()
    spec = config.PROVIDERS.get(req.provider)
    if spec is None:
        raise HTTPException(404, f"Unknown provider {req.provider!r}")
    if req.model not in config.provider_models(spec, env):
        raise HTTPException(400, f"Model {req.model!r} is not configured for {spec.label}")

    params = providers.GenerateParams(**req.model_dump(exclude={"provider", "model"}))
    started = time.monotonic()
    try:
        result = providers.generate(req.provider, req.model, params, env)
    except Exception as exc:  # noqa: BLE001 — every failure is reported to the UI
        traceback.print_exc()
        return {
            "ok": False,
            "provider": req.provider,
            "model": req.model,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {str(exc)[:1500]}",
        }
    return {"ok": True, **result.to_dict()}


# --- Admin ------------------------------------------------------------------------


def _admin_password() -> str:
    return config.read_env().get(config.ADMIN_PASSWORD_VAR, "")


def _new_session() -> str:
    now = time.time()
    for token, expiry in list(_sessions.items()):
        if expiry < now:
            _sessions.pop(token, None)
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + SESSION_TTL_SECONDS
    return token


def require_admin(authorization: str | None = Header(default=None)) -> str:
    token = (authorization or "").removeprefix("Bearer ").strip()
    expiry = _sessions.get(token)
    if not token or expiry is None or expiry < time.time():
        raise HTTPException(401, "Admin login required")
    return token


class PasswordBody(BaseModel):
    password: str = Field(min_length=1)


@app.get("/api/admin/status")
def admin_status():
    return {"password_set": bool(_admin_password()), "env_file": str(config.ENV_FILE)}


@app.post("/api/admin/setup")
def admin_setup(body: PasswordBody):
    if _admin_password():
        raise HTTPException(409, "An admin password is already set")
    if len(body.password) < 6:
        raise HTTPException(400, "Use at least 6 characters")
    config.write_env({config.ADMIN_PASSWORD_VAR: body.password})
    return {"token": _new_session()}


@app.post("/api/admin/login")
def admin_login(body: PasswordBody):
    stored = _admin_password()
    if not stored:
        raise HTTPException(409, "No admin password yet — set one first")
    if not hmac.compare_digest(body.password.encode(), stored.encode()):
        time.sleep(0.5)  # slow down guessing
        raise HTTPException(401, "Wrong password")
    return {"token": _new_session()}


@app.post("/api/admin/logout")
def admin_logout(token: str = Depends(require_admin)):
    _sessions.pop(token, None)
    return {"ok": True}


@app.post("/api/admin/password")
def admin_change_password(body: PasswordBody, _: str = Depends(require_admin)):
    if len(body.password) < 6:
        raise HTTPException(400, "Use at least 6 characters")
    config.write_env({config.ADMIN_PASSWORD_VAR: body.password})
    return {"ok": True}


@app.get("/api/admin/config")
def admin_get_config(_: str = Depends(require_admin)):
    env = config.read_env()
    result = []
    for spec in config.PROVIDERS.values():
        fields = []
        for f in spec.fields:
            value, default = env.get(f.key, ""), f.default
            if f is spec.models:
                # Shown one per line; includes legacy variables such as NVIDIA_FALLBACK_MODEL_ID.
                value = "\n".join(config.configured_models(spec, env))
                default = "\n".join(config.split_models(f.default))
            fields.append(
                {
                    "key": f.key,
                    "label": f.label,
                    "secret": f.secret,
                    "required": f.required,
                    "multiline": f.multiline,
                    "help": f.help,
                    "default": default,
                    "placeholder": f.placeholder,
                    "is_set": bool(value),
                    # Secrets never leave the server in full.
                    "value": config.mask(value) if f.secret else value,
                }
            )
        result.append(
            {
                "provider": spec.id,
                "label": spec.label,
                "logo": logo_url(spec.id),
                "configured": config.is_configured(spec, env),
                "models": config.provider_models(spec, env),
                "notes": spec.notes,
                "fields": fields,
            }
        )
    return {"env_file": str(config.ENV_FILE), "providers": result}


class ConfigUpdate(BaseModel):
    values: dict[str, str] = {}
    clear: list[str] = []


@app.put("/api/admin/config")
def admin_update_config(body: ConfigUpdate, _: str = Depends(require_admin)):
    unknown = [k for k in [*body.values, *body.clear] if k not in config.ALL_FIELDS]
    if unknown:
        raise HTTPException(400, f"Unknown setting(s): {', '.join(unknown)}")
    updates, clear = {}, list(body.clear)
    for key, value in body.values.items():
        value = value.strip()
        field = config.ALL_FIELDS[key]
        if value and field.pattern and not re.fullmatch(field.pattern, value):
            raise HTTPException(400, field.pattern_error or f"{field.label}: invalid value")
        if key in config.MODEL_KEYS:
            value = ",".join(config.split_models(value))  # one line in .env
        if value:
            updates[key] = value
        else:
            clear.append(key)  # an emptied model list falls back to the defaults
    for spec in config.PROVIDERS.values():
        if spec.models.key in updates or spec.models.key in clear:
            # The Models box showed the legacy variables too, so it now replaces them.
            clear.extend(spec.legacy_model_keys)
    try:
        config.write_env(updates, [k for k in dict.fromkeys(clear) if k not in updates])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return admin_get_config(_)


@app.post("/api/admin/test/{provider}")
def admin_test_provider(provider: str, _: str = Depends(require_admin)):
    """Send a tiny request to the provider's first model to check the credentials."""
    env = config.read_env()
    spec = config.PROVIDERS.get(provider)
    if spec is None:
        raise HTTPException(404, f"Unknown provider {provider!r}")
    models = config.provider_models(spec, env)
    if not config.is_configured(spec, env):
        return {"ok": False, "error": "Not configured: credentials and at least one model ID are required."}
    params = providers.GenerateParams(user="Reply with the single word: OK", max_tokens=300, use_temperature=False)
    try:
        result = providers.generate(provider, models[0], params, env)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "model": models[0], "error": f"{type(exc).__name__}: {str(exc)[:1000]}"}
    return {"ok": True, "model": models[0], "text": result.text[:200], "latency_ms": result.latency_ms}
