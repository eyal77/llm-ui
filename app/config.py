"""Settings live in a .env file — the same layout as the Lesson 2 code.

The Admin page reads and writes this file, so it is the single source of truth:
a provider shows up in the UI only when its credentials AND at least one model
ID are set here. The file is re-read on every request, so edits (from the Admin
page or by hand) take effect without a restart.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Point LLM_UI_ENV_FILE at another file (e.g. your lesson folder's .env) to reuse it.
ENV_FILE = Path(os.getenv("LLM_UI_ENV_FILE") or PROJECT_ROOT / ".env").resolve()

ADMIN_PASSWORD_VAR = "ADMIN_PASSWORD"

_lock = threading.Lock()


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    secret: bool = False
    required: bool = True  # required fields decide whether the provider is "configured"
    default: str = ""
    help: str = ""
    multiline: bool = False
    placeholder: str = ""
    # Optional regex a non-empty value must fully match, with the message shown otherwise.
    pattern: str = ""
    pattern_error: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    credentials: tuple[Field, ...]
    # One variable holding one or more model IDs (comma-separated in .env).
    # Its `default` list is used while the variable is unset.
    models: Field
    settings: tuple[Field, ...] = ()
    # Older single-model variables (from the lesson .env) still read into the list.
    legacy_model_keys: tuple[str, ...] = ()
    # Which sampling knobs this provider's API accepts (shown in the UI).
    supports: dict[str, bool] = field(default_factory=dict)
    notes: str = ""

    @property
    def fields(self) -> tuple[Field, ...]:
        return self.credentials + (self.models,) + self.settings


def _models_field(key: str, *defaults: str) -> Field:
    return Field(key, "Models", default=",".join(defaults), multiline=True,
                 help="One model ID per line. Leave empty to use the defaults.")


PROVIDERS: dict[str, ProviderSpec] = {
    spec.id: spec
    for spec in (
        ProviderSpec(
            id="bedrock",
            label="AWS Bedrock",
            credentials=(
                Field("AWS_ACCESS_KEY_ID", "Access key ID", secret=True),
                Field("AWS_SECRET_ACCESS_KEY", "Secret access key", secret=True),
                Field("AWS_SESSION_TOKEN", "Session token", secret=True, required=False,
                      help="Only for temporary (STS) credentials."),
            ),
            models=_models_field("BEDROCK_MODEL_ID", "us.amazon.nova-2-lite-v1:0"),
            settings=(Field("AWS_REGION", "Region", required=False, default="us-east-1"),),
            supports={"temperature": True, "top_p": True, "top_k": True},
            notes="top_k is model-specific on Bedrock: sent for Amazon Nova and Anthropic models only.",
        ),
        ProviderSpec(
            id="nvidia",
            label="NVIDIA",
            credentials=(Field("NVIDIA_API_KEY", "API key", secret=True, help="Starts with nvapi-"),),
            models=_models_field(
                "NVIDIA_MODEL_ID",
                "nvidia/nemotron-3-super-120b-a12b",
                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            ),
            legacy_model_keys=("NVIDIA_FALLBACK_MODEL_ID",),
            settings=(Field("NVIDIA_BASE_URL", "Base URL", required=False,
                            default="https://integrate.api.nvidia.com/v1"),),
            supports={"temperature": True, "top_p": True, "top_k": True},
            notes="top_k is passed through extra_body; some hosted models ignore it.",
        ),
        ProviderSpec(
            id="gemini",
            label="Google Gemini",
            credentials=(Field("GOOGLE_API_KEY", "API key", secret=True),),
            models=_models_field("GEMINI_MODEL_ID", "gemini-3.5-flash-lite"),
            supports={"temperature": True, "top_p": True, "top_k": True},
        ),
        ProviderSpec(
            id="openai",
            label="OpenAI",
            credentials=(Field("OPENAI_API_KEY", "API key", secret=True),),
            models=_models_field("OPENAI_MODEL_ID", "gpt-5.6-luna"),
            settings=(Field("OPENAI_BASE_URL", "Base URL", required=False,
                            help="Leave empty for api.openai.com."),),
            supports={"temperature": True, "top_p": True, "top_k": False},
            notes="The OpenAI API has no top_k.",
        ),
        ProviderSpec(
            id="anthropic",
            label="Anthropic",
            credentials=(Field("ANTHROPIC_API_KEY", "API key", secret=True),),
            models=_models_field("ANTHROPIC_MODEL_ID", "claude-haiku-4-5"),
            settings=(Field("ANTHROPIC_WORKSPACE_ID", "Workspace ID", required=False,
                            placeholder="wrkspc_…",
                            pattern=r"wrkspc_[A-Za-z0-9]+",
                            pattern_error="Workspace ID must be a workspace's ID, like wrkspc_01Jw… "
                                          "(not the key's scope). Copy it from Claude Console → "
                                          "Settings → Workspaces, ID column.",
                            help="Only for API keys whose scope is Organization (not one workspace). "
                                 "Enter the ID of the workspace to use — it starts with wrkspc_ — "
                                 "from Claude Console → Settings → Workspaces, ID column."),),
            supports={"temperature": True, "top_p": True, "top_k": True},
            notes="Current Claude models reject a non-default temperature, and "
                  "temperature + top_p together; enable only what your model accepts.",
        ),
    )
}

ALL_FIELDS: dict[str, Field] = {f.key: f for spec in PROVIDERS.values() for f in spec.fields}
MODEL_KEYS = {spec.models.key for spec in PROVIDERS.values()}


def read_env() -> dict[str, str]:
    """Current .env contents (missing file -> empty). Empty values count as unset."""
    if not ENV_FILE.exists():
        return {}
    return {k: v for k, v in dotenv_values(ENV_FILE).items() if v not in (None, "")}


def get(values: dict[str, str], key: str) -> str:
    return values.get(key) or ALL_FIELDS.get(key, Field(key, key)).default


def split_models(raw: str) -> list[str]:
    """Model IDs separated by commas and/or new lines, de-duplicated, order kept."""
    out: list[str] = []
    for m in (raw or "").replace("\n", ",").split(","):
        m = m.strip()
        if m and m not in out:
            out.append(m)
    return out


def configured_models(spec: ProviderSpec, values: dict[str, str]) -> list[str]:
    """Models set in .env (primary + legacy variables); empty when none are set."""
    keys = (spec.models.key, *spec.legacy_model_keys)
    return split_models(",".join(values.get(k, "") for k in keys))


def provider_models(spec: ProviderSpec, values: dict[str, str]) -> list[str]:
    """The models offered in the UI: what .env sets, or the defaults."""
    return configured_models(spec, values) or split_models(spec.models.default)


def is_configured(spec: ProviderSpec, values: dict[str, str]) -> bool:
    has_creds = all(values.get(f.key) for f in spec.credentials if f.required)
    return has_creds and bool(provider_models(spec, values))


def write_env(updates: dict[str, str], clear: list[str] | tuple[str, ...] = ()) -> None:
    """Set / remove keys in the .env file, keeping its comments and order."""
    with _lock:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.touch(exist_ok=True)
        existing = dotenv_values(ENV_FILE)
        for key, value in updates.items():
            if any(c in value for c in "\r\n"):
                raise ValueError(f"{key}: value must be a single line")
            # Plain values stay unquoted (like the lesson's .env); quote only when needed.
            needs_quotes = value != value.strip() or any(c in value for c in " #'\"\\=")
            set_key(ENV_FILE, key, value, quote_mode="always" if needs_quotes else "never")
        for key in clear:
            if key in existing:
                unset_key(ENV_FILE, key)


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"
