"""Offline tests: every provider SDK is replaced by a fake that records its call.

    pytest -q
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app import config, main, providers

ENV_TEXT = """# my lesson .env
AWS_ACCESS_KEY_ID=AKIAEXAMPLE000000
AWS_SECRET_ACCESS_KEY=secret/with+chars
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.amazon.nova-2-lite-v1:0

NVIDIA_API_KEY=nvapi-abcdefghijkl
NVIDIA_MODEL_ID=nvidia/model-a, nvidia/model-b
NVIDIA_FALLBACK_MODEL_ID=nvidia/model-c

GOOGLE_API_KEY=AIzaEXAMPLEKEY
GEMINI_MODEL_ID=gemini-flash-lite

# keys ship blank
OPENAI_API_KEY=
OPENAI_MODEL_ID=gpt-x
ANTHROPIC_API_KEY=sk-ant-EXAMPLE
ANTHROPIC_MODEL_ID=claude-haiku-4-5
"""


class Recorder:
    def __init__(self):
        self.calls = []


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(ENV_TEXT, encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    return path


@pytest.fixture
def fakes(monkeypatch):
    rec = Recorder()

    # boto3
    class BedrockClient:
        def __init__(self, **kw):
            rec.calls.append(("bedrock.client", kw))

        def converse(self, **kw):
            rec.calls.append(("bedrock", kw))
            return {
                "output": {"message": {"content": [{"reasoningContent": {}}, {"text": " Bedrock answer "}]}},
                "usage": {"inputTokens": 11, "outputTokens": 22},
                "stopReason": "end_turn",
            }

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service, **kw: BedrockClient(service=service, **kw)
    monkeypatch.setitem(sys.modules, "boto3", boto3)

    # openai
    openai = types.ModuleType("openai")

    class BadRequestError(Exception):
        pass

    class Completions:
        def __init__(self, owner):
            self.owner = owner

        def create(self, **kw):
            rec.calls.append(("openai", kw))
            if rec.__dict__.get("reject_reasoning") and "reasoning_effort" in kw:
                raise BadRequestError("Unrecognized request argument supplied: reasoning_effort")
            msg = NS(content="the quick brown fox", reasoning_content=None)
            usage = NS(prompt_tokens=5, completion_tokens=7, completion_tokens_details=NS(reasoning_tokens=0))
            return NS(choices=[NS(message=msg, finish_reason="stop")], usage=usage)

    class OpenAI:
        def __init__(self, **kw):
            rec.calls.append(("openai.client", kw))
            self.chat = NS(completions=Completions(self))

    openai.OpenAI = OpenAI
    openai.BadRequestError = BadRequestError
    monkeypatch.setitem(sys.modules, "openai", openai)

    # google.genai
    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    gtypes = types.ModuleType("google.genai.types")
    gtypes.GenerateContentConfig = lambda **kw: NS(**kw)
    gtypes.ThinkingConfig = lambda **kw: NS(**kw)
    gtypes.FinishReason = NS(MAX_TOKENS="MAX_TOKENS")

    class Models:
        def generate_content(self, **kw):
            rec.calls.append(("gemini", kw))
            return NS(
                text="Gemini answer",
                usage_metadata=NS(prompt_token_count=3, candidates_token_count=4, thoughts_token_count=6),
                candidates=[NS(finish_reason="MAX_TOKENS")],
            )

    genai.Client = lambda **kw: NS(models=Models())
    genai.types = gtypes
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", gtypes)

    # anthropic
    anthropic = types.ModuleType("anthropic")

    class Messages:
        def create(self, **kw):
            rec.calls.append(("anthropic", kw))
            return NS(
                content=[NS(type="thinking"), NS(type="text", text="Claude answer")],
                usage=NS(input_tokens=9, output_tokens=10),
                stop_reason="end_turn",
            )

    def make_anthropic(**kw):
        rec.calls.append(("anthropic.client", kw))
        return NS(messages=Messages())

    anthropic.Anthropic = make_anthropic
    monkeypatch.setitem(sys.modules, "anthropic", anthropic)
    return rec


def last(rec, name):
    return [kw for n, kw in rec.calls if n == name][-1]


# --- provider mapping --------------------------------------------------------------


def all_knobs(**kw):
    return providers.GenerateParams(
        user="hi", system="sys", temperature=0.5, top_p=0.8, top_k=20,
        use_temperature=True, use_top_p=True, use_top_k=True, max_tokens=123, **kw
    )


def test_bedrock_nova_mapping(env_file, fakes):
    r = providers.generate("bedrock", "us.amazon.nova-2-lite-v1:0", all_knobs())
    kw = last(fakes, "bedrock")
    assert kw["inferenceConfig"] == {"maxTokens": 123, "temperature": 0.5, "topP": 0.8}
    assert kw["additionalModelRequestFields"] == {"inferenceConfig": {"topK": 20}}
    assert kw["system"] == [{"text": "sys"}]
    client = last(fakes, "bedrock.client")
    assert client["aws_access_key_id"] == "AKIAEXAMPLE000000" and client["aws_session_token"] is None
    assert (r.text, r.input_tokens, r.output_tokens, r.total_tokens) == ("Bedrock answer", 11, 22, 33)
    assert r.sent["top_k"] == 20 and not r.skipped


def test_reasoning_drops_sampling(env_file, fakes):
    r = providers.generate("bedrock", "us.amazon.nova-2-lite-v1:0", all_knobs(reasoning=True))
    kw = last(fakes, "bedrock")
    assert kw["inferenceConfig"] == {"maxTokens": 123}
    assert kw["additionalModelRequestFields"]["reasoningConfig"]["type"] == "enabled"
    assert len(r.skipped) == 3 and all("reasoning is on" in s for s in r.skipped)


def test_nvidia_mapping(env_file, fakes):
    r = providers.generate("nvidia", "nvidia/model-b", all_knobs())
    kw = last(fakes, "openai")
    assert kw["max_tokens"] == 123 and kw["temperature"] == 0.5 and kw["top_p"] == 0.8
    assert kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20}
    assert kw["messages"][0] == {"role": "system", "content": "sys"}
    assert last(fakes, "openai.client")["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert r.reasoning_tokens == 0


def test_openai_skips_top_k_and_retries_without_reasoning_effort(env_file, fakes):
    env = config.read_env() | {"OPENAI_API_KEY": "sk-test"}
    fakes.reject_reasoning = True
    r = providers.generate("openai", "gpt-x", all_knobs(), env)
    kw = last(fakes, "openai")
    assert "reasoning_effort" not in kw and "top_k" not in kw and "extra_body" not in kw
    assert kw["max_completion_tokens"] == 123
    assert any(s.startswith("top_k: not supported") for s in r.skipped)
    assert any(s.startswith("reasoning:") for s in r.skipped)
    assert r.text == "the quick brown fox"


def test_gemini_mapping_and_truncation(env_file, fakes):
    r = providers.generate("gemini", "gemini-flash-lite", all_knobs())
    cfg = last(fakes, "gemini")["config"]
    assert (cfg.temperature, cfg.top_p, cfg.top_k, cfg.max_output_tokens) == (0.5, 0.8, 20, 123)
    assert cfg.thinking_config is None and cfg.system_instruction == "sys"
    assert r.output_tokens == 10 and r.reasoning_tokens == 6
    assert r.truncated and "Cut off" in r.warning


def test_anthropic_mapping(env_file, fakes):
    r = providers.generate("anthropic", "claude-haiku-4-5", all_knobs())
    kw = last(fakes, "anthropic")
    assert kw["thinking"] == {"type": "disabled"}
    assert kw["extra_body"] == {"temperature": 0.5, "top_p": 0.8, "top_k": 20}
    assert "temperature" not in kw
    assert r.text == "Claude answer"


def test_anthropic_workspace_header(env_file, fakes):
    env = config.read_env()
    providers.generate("anthropic", "claude-haiku-4-5", all_knobs(), env)
    assert last(fakes, "anthropic.client")["default_headers"] is None
    providers.generate("anthropic", "claude-haiku-4-5", all_knobs(), env | {"ANTHROPIC_WORKSPACE_ID": " wrkspc_123 "})
    assert last(fakes, "anthropic.client")["default_headers"] == {"anthropic-workspace-id": "wrkspc_123"}


def test_anthropic_workspace_hint(env_file, fakes, monkeypatch):
    def fail(**kw):
        raise ValueError("Error code: 400 - ... must include the anthropic-workspace-id header ...")

    monkeypatch.setattr(sys.modules["anthropic"], "Anthropic", lambda **kw: NS(messages=NS(create=fail)))
    with pytest.raises(RuntimeError):
        providers.generate("anthropic", "claude-haiku-4-5", all_knobs())

    def invalid(**kw):
        raise ValueError("Error code: 400 - anthropic-workspace-id header must be a valid workspace ID.")

    monkeypatch.setattr(sys.modules["anthropic"], "Anthropic", lambda **kw: NS(messages=NS(create=invalid)))
    env = config.read_env() | {"ANTHROPIC_WORKSPACE_ID": "Organization"}
    try:
        providers.generate("anthropic", "claude-haiku-4-5", all_knobs(), env)
        raise AssertionError("expected an error")
    except RuntimeError as exc:
        assert "currently 'Organization'" in str(exc)


def test_disabled_knobs_are_not_sent(env_file, fakes):
    providers.generate("anthropic", "claude-haiku-4-5", providers.GenerateParams(user="hi", use_temperature=False))
    assert "extra_body" not in last(fakes, "anthropic")


# --- config ------------------------------------------------------------------------


def test_models_and_configured(env_file):
    env = config.read_env()
    assert config.provider_models(config.PROVIDERS["nvidia"], env) == ["nvidia/model-a", "nvidia/model-b", "nvidia/model-c"]
    assert not config.is_configured(config.PROVIDERS["openai"], env)  # blank key
    assert config.is_configured(config.PROVIDERS["bedrock"], env)


def test_default_models_when_unset(env_file):
    env = {"GOOGLE_API_KEY": "k", "NVIDIA_API_KEY": "k"}
    assert config.provider_models(config.PROVIDERS["gemini"], env) == ["gemini-3.5-flash-lite"]
    assert len(config.provider_models(config.PROVIDERS["nvidia"], env)) == 2
    assert config.is_configured(config.PROVIDERS["gemini"], env)  # a key is enough
    assert config.split_models("a\n b, a\n\nc") == ["a", "b", "c"]


def test_write_env_keeps_comments(env_file):
    config.write_env({"OPENAI_API_KEY": "sk-new", "GEMINI_MODEL_ID": "g1,g2"}, clear=["NVIDIA_FALLBACK_MODEL_ID"])
    text = env_file.read_text()
    assert "# my lesson .env" in text and "# keys ship blank" in text
    assert "OPENAI_API_KEY=sk-new" in text and "NVIDIA_FALLBACK_MODEL_ID" not in text
    with pytest.raises(ValueError):
        config.write_env({"OPENAI_API_KEY": "a\nb"})


# --- HTTP API ----------------------------------------------------------------------


@pytest.fixture
def client(env_file, fakes):
    main._sessions.clear()
    return TestClient(main.app)


def test_index_and_models(client):
    assert "LLM Compare" in client.get("/").text
    data = client.get("/api/models").json()
    by = {p["provider"]: p for p in data["providers"]}
    assert [m["id"] for m in by["nvidia"]["models"]][0] == "nvidia:nvidia/model-a"
    assert by["openai"]["configured"] is False and by["openai"]["models"] == []
    assert data["defaults"]["max_tokens"] == 2000


def test_logo_urls(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOGO_DIR", tmp_path)
    by = {p["provider"]: p for p in client.get("/api/models").json()["providers"]}
    assert by["gemini"]["logo"] is None
    (tmp_path / "gemini.png").write_bytes(b"\x89PNG")
    by = {p["provider"]: p for p in client.get("/api/models").json()["providers"]}
    assert by["gemini"]["logo"].startswith("/static/logos/gemini.png?v=")
    assert by["openai"]["logo"] is None


def test_generate_ok_and_errors(client, monkeypatch):
    body = {"provider": "gemini", "model": "gemini-flash-lite", "user": "hello"}
    r = client.post("/api/generate", json=body).json()
    assert r["ok"] and r["total_tokens"] == 13 and r["sent"]["temperature"] == 0.3

    assert client.post("/api/generate", json={**body, "model": "nope"}).status_code == 400
    assert client.post("/api/generate", json={**body, "temperature": 5}).status_code == 422
    assert client.post("/api/generate", json={**body, "user": ""}).status_code == 422

    def boom(*a, **k):
        raise RuntimeError("401 invalid key")

    monkeypatch.setitem(providers.CALLERS, "gemini", boom)
    r = client.post("/api/generate", json=body).json()
    assert r["ok"] is False and "401 invalid key" in r["error"]


def test_admin_flow(client, env_file):
    assert client.get("/api/admin/status").json()["password_set"] is False
    assert client.get("/api/admin/config").status_code == 401
    assert client.post("/api/admin/login", json={"password": "x"}).status_code == 409
    assert client.post("/api/admin/setup", json={"password": "123"}).status_code == 400

    token = client.post("/api/admin/setup", json={"password": "s3cret!"}).json()["token"]
    assert "ADMIN_PASSWORD=s3cret!" in env_file.read_text()
    assert client.post("/api/admin/setup", json={"password": "another1"}).status_code == 409
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    token = client.post("/api/admin/login", json={"password": "s3cret!"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    cfg = client.get("/api/admin/config", headers=auth).json()
    fields = {f["key"]: f for p in cfg["providers"] for f in p["fields"]}
    assert fields["NVIDIA_API_KEY"]["value"] == "nvap••••ijkl"
    assert "secret/with+chars" not in str(cfg)
    assert fields["AWS_REGION"]["value"] == "us-east-1"

    assert "NVIDIA_API_KEY" not in str([f["label"] for f in fields.values()])
    models_field = fields["NVIDIA_MODEL_ID"]
    assert models_field["multiline"] and models_field["is_set"]
    assert models_field["value"] == "nvidia/model-a\nnvidia/model-b\nnvidia/model-c"  # fallback folded in
    assert fields["OPENAI_MODEL_ID"]["default"] == "gpt-5.6-luna"

    # Saving the Models box (one per line) replaces the legacy fallback variable.
    client.put("/api/admin/config", headers=auth, json={"values": {"NVIDIA_MODEL_ID": "m1\nm2\n"}})
    text = env_file.read_text()
    assert "NVIDIA_MODEL_ID=m1,m2" in text and "NVIDIA_FALLBACK_MODEL_ID" not in text
    # Emptying it removes the variable, so the defaults apply.
    res = client.put("/api/admin/config", headers=auth, json={"values": {"GEMINI_MODEL_ID": ""}}).json()
    assert "GEMINI_MODEL_ID" not in env_file.read_text()
    gem = next(p for p in res["providers"] if p["provider"] == "gemini")
    assert gem["models"] == ["gemini-3.5-flash-lite"]
    assert next(f for f in gem["fields"] if f["key"] == "GEMINI_MODEL_ID")["is_set"] is False

    bad = client.put("/api/admin/config", headers=auth, json={"values": {"ANTHROPIC_WORKSPACE_ID": "Organization"}})
    assert bad.status_code == 400 and "wrkspc_" in bad.json()["detail"]
    ok = client.put("/api/admin/config", headers=auth, json={"values": {"ANTHROPIC_WORKSPACE_ID": " wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ "}})
    assert ok.status_code == 200 and "ANTHROPIC_WORKSPACE_ID=wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ" in env_file.read_text()
    client.put("/api/admin/config", headers=auth, json={"values": {"ANTHROPIC_WORKSPACE_ID": ""}})
    assert "ANTHROPIC_WORKSPACE_ID" not in env_file.read_text()

    bad = client.put("/api/admin/config", headers=auth, json={"values": {"PATH": "x"}})
    assert bad.status_code == 400

    res = client.put("/api/admin/config", headers=auth, json={
        "values": {"OPENAI_API_KEY": "sk-live-123456789", "OPENAI_MODEL_ID": " gpt-a ,, gpt-b "},
        "clear": ["ANTHROPIC_API_KEY"],
    }).json()
    by = {p["provider"]: p for p in res["providers"]}
    assert by["openai"]["configured"] and by["openai"]["models"] == ["gpt-a", "gpt-b"]
    assert not by["anthropic"]["configured"]
    models = client.get("/api/models").json()
    assert any(m["id"] == "openai:gpt-b" for p in models["providers"] for m in p["models"])

    t = client.post("/api/admin/test/openai", headers=auth).json()
    assert t["ok"] and t["model"] == "gpt-a"
    assert client.post("/api/admin/test/anthropic", headers=auth).json()["ok"] is False

    assert client.post("/api/admin/password", headers=auth, json={"password": "newpass1"}).status_code == 200
    assert client.post("/api/admin/logout", headers=auth).status_code == 200
    assert client.get("/api/admin/config", headers=auth).status_code == 401
