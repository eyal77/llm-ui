"""The provider gateway — adapted from Lesson 2's 06a-provider-gateway.py.

Application code builds a neutral `GenerateParams` and gets back a neutral
`GenerateResult`; one translation function per provider maps it to that
vendor's native API. Compared with 06a this adds top_p / top_k, per-knob
on/off switches, and a record of which knobs were actually sent or skipped —
so a comparison table can show *why* two providers were not called the same way.

SDKs are imported lazily: a missing package only breaks its own provider.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from . import config

SAMPLING_KNOBS = ("temperature", "top_p", "top_k")

DEFAULTS = {
    "temperature": 0.3,
    "max_tokens": 2000,
    "top_p": 0.9,
    "top_k": 40,
    # Which optional knobs are sent by default. Several current models reject
    # top_p/top_k (or temperature + top_p together), so those start switched off.
    "use_temperature": True,
    "use_top_p": False,
    "use_top_k": False,
    "reasoning": False,
}


@dataclass
class GenerateParams:
    user: str
    system: str = ""
    max_tokens: int = DEFAULTS["max_tokens"]
    temperature: float = DEFAULTS["temperature"]
    top_p: float = DEFAULTS["top_p"]
    top_k: int = DEFAULTS["top_k"]
    use_temperature: bool = DEFAULTS["use_temperature"]
    use_top_p: bool = DEFAULTS["use_top_p"]
    use_top_k: bool = DEFAULTS["use_top_k"]
    # Off by default, like 06a: models that think unless told not to get an explicit "off".
    reasoning: bool = DEFAULTS["reasoning"]

    @property
    def messages(self) -> list[dict[str, str]]:
        return [{"role": "user", "content": self.user}]


@dataclass
class GenerateResult:
    provider: str
    model: str
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None  # part of output_tokens, when the API reports it
    truncated: bool = False
    stop_reason: str = ""
    latency_ms: int = 0
    sent: dict = field(default_factory=dict)      # parameters actually sent
    skipped: list = field(default_factory=list)   # "top_k: not supported by OpenAI", ...
    warning: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        data = asdict(self)
        data["total_tokens"] = self.total_tokens
        return data


class _Plan:
    """Collects what a call sends and what it deliberately leaves out."""

    def __init__(self, spec: config.ProviderSpec, params: GenerateParams, result: GenerateResult):
        self.spec, self.params, self.result = spec, params, result
        result.sent["max_tokens"] = params.max_tokens
        result.sent["reasoning"] = params.reasoning

    def wanted(self) -> dict[str, float | int]:
        """Enabled sampling knobs this provider can take (reasoning drops them all, as in 06a)."""
        p, out = self.params, {}
        for knob in SAMPLING_KNOBS:
            if not getattr(p, f"use_{knob}"):
                continue
            if p.reasoning:
                self.skip(knob, "reasoning is on (the model samples for itself)")
            elif not self.spec.supports.get(knob, False):
                self.skip(knob, f"not supported by {self.spec.label}")
            else:
                value = getattr(p, knob)
                if knob == "temperature" and value > self.spec.temperature_max:
                    # Sent, not dropped — just capped to what the API accepts, instead of erroring.
                    self.result.skipped.append(
                        f"temperature: capped to {self.spec.temperature_max} (was {value} — "
                        f"{self.spec.label}'s max)"
                    )
                    value = self.spec.temperature_max
                out[knob] = value
        return out

    def send(self, knob: str, value) -> None:
        self.result.sent[knob] = value

    def skip(self, knob: str, reason: str) -> None:
        self.result.skipped.append(f"{knob}: {reason}")


# --- Bedrock (Converse API) -----------------------------------------------------


def call_bedrock(model: str, params: GenerateParams, env: dict, plan: _Plan) -> None:
    import boto3

    r = plan.result
    client = boto3.client(
        "bedrock-runtime",
        region_name=config.get(env, "AWS_REGION"),
        aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=env.get("AWS_SESSION_TOKEN") or None,
    )
    inference: dict = {"maxTokens": params.max_tokens}
    additional: dict = {}
    family = model.lower()
    for knob, value in plan.wanted().items():
        if knob == "temperature":
            inference["temperature"] = value
        elif knob == "top_p":
            inference["topP"] = value
        elif knob == "top_k":
            # Converse has no common topK; it goes in the model's own request fields.
            if "anthropic" in family:
                additional["top_k"] = int(value)
            elif "nova" in family:
                additional.setdefault("inferenceConfig", {})["topK"] = int(value)
            else:
                plan.skip("top_k", "no known top_k field for this Bedrock model family")
                continue
        plan.send(knob, value)
    if params.reasoning:
        if "nova" in family:
            additional["reasoningConfig"] = {"type": "enabled", "maxReasoningEffort": "low"}
        else:
            plan.skip("reasoning", "switch implemented for Amazon Nova models only")

    kwargs = {
        "modelId": model,
        "messages": [{"role": m["role"], "content": [{"text": m["content"]}]} for m in params.messages],
        "inferenceConfig": inference,
    }
    if params.system:
        kwargs["system"] = [{"text": params.system}]
    if additional:
        kwargs["additionalModelRequestFields"] = additional

    response = client.converse(**kwargs)
    # Keep only text blocks; with reasoning on the list also carries reasoningContent.
    r.text = "".join(b.get("text", "") for b in response["output"]["message"]["content"]).strip()
    usage = response.get("usage", {})
    r.input_tokens = usage.get("inputTokens", 0)
    r.output_tokens = usage.get("outputTokens", 0)
    r.stop_reason = response.get("stopReason", "")
    r.truncated = r.stop_reason == "max_tokens"


# --- OpenAI Chat Completions (OpenAI, NVIDIA and xAI Grok) ----------------------


def call_openai_compatible(model: str, params: GenerateParams, env: dict, plan: _Plan) -> None:
    import openai

    r = plan.result
    provider = plan.spec.id
    if provider == "nvidia":
        # The free tier queues requests; 60s per attempt instead of the 10-minute default.
        client = openai.OpenAI(api_key=env["NVIDIA_API_KEY"], base_url=config.get(env, "NVIDIA_BASE_URL"), timeout=60)
    elif provider == "grok":
        client = openai.OpenAI(api_key=env["GROK_API_KEY"], base_url=config.get(env, "GROK_BASE_URL"))
    else:
        client = openai.OpenAI(api_key=env["OPENAI_API_KEY"], base_url=env.get("OPENAI_BASE_URL") or None)

    kwargs: dict = {
        "model": model,
        "messages": ([{"role": "system", "content": params.system}] if params.system else []) + params.messages,
    }
    extra_body: dict = {}
    if provider == "nvidia":
        kwargs["max_tokens"] = params.max_tokens
        # Nemotron thinks by default; NVIDIA's switch is a chat-template flag.
        extra_body["chat_template_kwargs"] = {"enable_thinking": params.reasoning}
    elif provider == "grok":
        kwargs["max_tokens"] = params.max_tokens
        # Only grok-3-mini-style models accept reasoning_effort; non-reasoning models
        # error on it, so it's sent only when asked for (and dropped on retry below).
        if params.reasoning:
            kwargs["reasoning_effort"] = "high"
    else:
        # Reasoning models take max_completion_tokens (it covers hidden reasoning too).
        kwargs["max_completion_tokens"] = params.max_tokens
        kwargs["reasoning_effort"] = "low" if params.reasoning else "none"

    for knob, value in plan.wanted().items():
        if knob == "top_k":
            extra_body["top_k"] = int(value)  # not part of the OpenAI protocol
        else:
            kwargs[knob] = value
        plan.send(knob, value)
    if extra_body:
        kwargs["extra_body"] = extra_body

    try:
        response = client.chat.completions.create(**kwargs)
    except openai.BadRequestError as exc:
        # Non-reasoning OpenAI models (gpt-4.1, gpt-4o, ...) reject reasoning_effort.
        if "reasoning_effort" not in kwargs or "reasoning" not in str(exc).lower():
            raise
        kwargs.pop("reasoning_effort")
        plan.skip("reasoning", "model does not accept reasoning_effort (retried without it)")
        response = client.chat.completions.create(**kwargs)

    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    # If max_tokens runs out mid-thought, NVIDIA returns the unfinished reasoning as content.
    reasoning = (getattr(choice.message, "reasoning_content", None) or "").strip()
    if choice.finish_reason == "length" and reasoning and text == reasoning:
        text = ""
    r.text = text
    usage = response.usage
    if usage:
        r.input_tokens = usage.prompt_tokens or 0
        r.output_tokens = usage.completion_tokens or 0  # already includes reasoning tokens
        details = getattr(usage, "completion_tokens_details", None)
        if details is not None and getattr(details, "reasoning_tokens", None) is not None:
            r.reasoning_tokens = details.reasoning_tokens
    r.stop_reason = choice.finish_reason or ""
    r.truncated = choice.finish_reason == "length"


# --- Gemini ---------------------------------------------------------------------


def call_gemini(model: str, params: GenerateParams, env: dict, plan: _Plan) -> None:
    from google import genai
    from google.genai import types

    r = plan.result
    client = genai.Client(api_key=env["GOOGLE_API_KEY"])
    config_kwargs: dict = {
        "system_instruction": params.system or None,
        "max_output_tokens": params.max_tokens,
        # Flash-Lite models don't think unless given a thinking_level.
        "thinking_config": types.ThinkingConfig(thinking_level="low") if params.reasoning else None,
    }
    for knob, value in plan.wanted().items():
        config_kwargs[knob] = value
        plan.send(knob, value)

    response = client.models.generate_content(
        model=model,
        contents=[
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in params.messages
        ],
        config=types.GenerateContentConfig(**config_kwargs),
    )
    r.text = (response.text or "").strip()
    usage = response.usage_metadata
    if usage:
        thoughts = usage.thoughts_token_count or 0
        r.input_tokens = usage.prompt_token_count or 0
        # Gemini counts thinking separately; fold it in to compare like with like.
        r.output_tokens = (usage.candidates_token_count or 0) + thoughts
        r.reasoning_tokens = thoughts or None
    if response.candidates:
        reason = response.candidates[0].finish_reason
        r.stop_reason = getattr(reason, "name", str(reason or ""))
        r.truncated = reason == types.FinishReason.MAX_TOKENS


# --- Anthropic ------------------------------------------------------------------


def call_anthropic(model: str, params: GenerateParams, env: dict, plan: _Plan) -> None:
    import anthropic

    r = plan.result
    # Keys that aren't scoped to one workspace must name it on every request.
    workspace = (env.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    client = anthropic.Anthropic(
        api_key=env["ANTHROPIC_API_KEY"],
        default_headers={"anthropic-workspace-id": workspace} if workspace else None,
    )
    kwargs: dict = {"model": model, "messages": params.messages, "max_tokens": params.max_tokens}
    if params.system:
        kwargs["system"] = params.system
    if not params.reasoning:
        # Newer Claude models think by default, so "off" must be explicit.
        kwargs["thinking"] = {"type": "disabled"}
    # Sampling knobs go through extra_body: newer SDK versions dropped some of these
    # arguments, while the HTTP API still decides per model what it accepts.
    extra_body = {}
    for knob, value in plan.wanted().items():
        extra_body[knob] = int(value) if knob == "top_k" else value
        plan.send(knob, value)
    if extra_body:
        kwargs["extra_body"] = extra_body

    try:
        response = client.messages.create(**kwargs)
    except Exception as exc:
        msg = str(exc)
        if "anthropic-workspace-id" in msg and not workspace:
            raise RuntimeError(
                f"{msg} — This key needs a workspace: set 'Workspace ID' in Admin → Anthropic."
            ) from exc
        if "valid workspace ID" in msg or "Workspace `" in msg:
            raise RuntimeError(
                f"{msg} — Check 'Workspace ID' in Admin → Anthropic: it must be a workspace's ID "
                f"(wrkspc_…) that this key can access; currently {workspace!r}."
            ) from exc
        raise
    r.text = "".join(b.text for b in response.content if b.type == "text").strip()
    r.input_tokens = response.usage.input_tokens
    r.output_tokens = response.usage.output_tokens
    r.stop_reason = response.stop_reason or ""
    r.truncated = response.stop_reason == "max_tokens"


CALLERS = {
    "bedrock": call_bedrock,
    "nvidia": call_openai_compatible,
    "openai": call_openai_compatible,
    "grok": call_openai_compatible,
    "gemini": call_gemini,
    "anthropic": call_anthropic,
}


def generate(provider: str, model: str, params: GenerateParams, env: dict | None = None) -> GenerateResult:
    """The one function the web app calls. Raises on API errors."""
    env = config.read_env() if env is None else env
    spec = config.PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"Unknown provider: {provider}")
    if not config.is_configured(spec, env):
        raise ValueError(f"{spec.label} is not configured — set its credentials and model IDs in Admin")

    result = GenerateResult(provider=provider, model=model)
    plan = _Plan(spec, params, result)
    start = time.monotonic()
    try:
        CALLERS[provider](model, params, env, plan)
    finally:
        result.latency_ms = int((time.monotonic() - start) * 1000)
    if not result.text:
        result.warning = "No answer text — the model likely spent max_tokens on reasoning."
    elif result.truncated:
        result.warning = "Cut off at max_tokens — the answer is incomplete."
    return result
