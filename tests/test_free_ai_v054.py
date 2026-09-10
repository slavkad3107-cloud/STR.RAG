"""v0.54: бесплатные ИИ как в ЭКО.DOC — своя подпись клиента в проверке моделей,
облако Ollama отдельно от локальных, Z.ai и Cloudflare, честные причины отказа,
живые модели по умолчанию."""
import threading
import time

import pytest


class _Resp:
    def __init__(self, body=b"{}"):
        self._b = body

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_health_requests_carry_own_user_agent(monkeypatch):
    """Cloudflare (Groq, Cerebras) отбивает подпись «Python-urllib» кодом 1010 —
    из-за этого проверка считала Groq недоступным из РФ и не выбирала его."""
    from pmoos.core import health
    seen = []

    def fake(req, timeout=None):
        seen.append(req.get_header("User-agent"))
        return _Resp(b'{"data": [{"id": "openai/gpt-oss-120b"}]}')

    monkeypatch.setattr(health.urllib.request, "urlopen", fake)
    health._get_json("https://api.groq.com/openai/v1/models", {"Authorization": "Bearer x"})
    ok, _ = health._try_generate("groq", "https://api.groq.com/openai/v1", "k",
                                 "openai/gpt-oss-120b")
    assert ok and len(seen) == 2
    assert all(ua and ua.startswith("StroyRAG/") for ua in seen)


@pytest.mark.parametrize("code, body, headers, expect", [
    (403, "error code: 1010", {}, "подпись"),
    (403, '{"error":{"message":"Forbidden"}}', {}, "стран"),
    (403, '{"success": false, "error": "Access denied by security policy."}', {}, "стран"),
    (400, '{"message": "User location is not supported for the API use."}', {}, "стран"),
    (401, '{"detail":"Your API key expired on 2026-09-08."}', {}, "срок ключа"),
    (402, '{"message":"Payment required to access this resource."}', {}, "оплата"),
    (429, '{"message":"Rate limit exceeded"}', {"x-ratelimit-limit-req-minute": "0"},
     "не входит в бесплатный"),
    (429, '{"message":"Rate limit exceeded"}', {}, "лимит"),
    (403, '{"message":"tier_not_allowed"}', {}, "тарифе"),
    (429, '{"error":{"code":"1305","message":"The service may be temporarily overloaded"}}',
     {}, "перегружен"),
    (401, '{"error":"invalid key"}', {}, "ключ недействителен"),
])
def test_explain_http_names_real_cause(code, body, headers, expect):
    from pmoos.core.health import explain_http
    assert expect in explain_http(code, body, headers)


def test_local_ollama_excludes_cloud_models(monkeypatch):
    """После подключения облака облачные ярлыки стоят в списке Ollama рядом с
    локальными — «локальная, приватная» модель не должна оказаться облачной."""
    from pmoos.core import health, ollama_utils
    tags = ["gpt-oss:120b-cloud", "nemotron-3-super:cloud", "qwen2.5:7b", "bge-m3:latest"]
    monkeypatch.setattr(ollama_utils, "_list_installed_models_raw", lambda base_url=None: tags)
    ollama_utils._PROBE_CACHE.clear()
    assert ollama_utils.list_installed_models() == ["qwen2.5:7b", "bge-m3:latest"]
    assert ollama_utils.list_cloud_models() == ["gpt-oss:120b-cloud", "nemotron-3-super:cloud"]
    ollama_utils._PROBE_CACHE.clear()
    payload = {"models": [{"name": n} for n in tags]}
    monkeypatch.setattr(health, "_get_json", lambda url, headers, timeout=12.0: payload)
    local = health.probe_ollama(["http://localhost:11434"])
    cloud = health.probe_ollama(["http://localhost:11434"], cloud=True)
    assert local["models"] == ["qwen2.5:7b", "bge-m3:latest"]
    assert cloud["models"] == ["gpt-oss:120b-cloud", "nemotron-3-super:cloud"]
    assert health.pick_model("ollama", local["models"]) == "qwen2.5:7b"


def test_ollama_cloud_probe_offers_free_models_when_none_pulled(monkeypatch):
    from pmoos.core import health
    monkeypatch.setattr(health, "_get_json",
                        lambda url, headers, timeout=12.0: {"models": [{"name": "qwen2.5:7b"}]})
    res = health.probe_provider("ollama_cloud", "", "", ["http://localhost:11434"])
    assert res["ok"] and res["models"] == health.OLLAMA_CLOUD_FREE


def test_cloudflare_needs_account_and_builds_url(monkeypatch):
    from pmoos.core.health import cloudflare_base, probe_provider
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    assert cloudflare_base("tok") == ("", "tok")
    assert not probe_provider("cloudflare", "tok")["ok"]
    assert cloudflare_base("acc123:tok") == (
        "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc9")
    base, tok = cloudflare_base("tok")
    assert base.endswith("/accounts/acc9/ai/v1") and tok == "tok"
    assert probe_provider("cloudflare", "tok")["ok"]


def test_new_free_providers_registered_and_ollama_cloud_keyless():
    from pmoos.config import ENV_KEYS, load_config
    from pmoos.core.health import PREFERRED, TIERS
    for p in ("zai", "cloudflare", "ollama_cloud"):
        assert TIERS.get(p) == 0 and p in PREFERRED and p in ENV_KEYS
    cfg = load_config()
    assert cfg.has_key("ollama_cloud")
    assert cfg.model_for("zai") == "glm-4.7-flash"
    assert cfg.base_url("zai") == "https://api.z.ai/api/paas/v4"


def test_default_models_are_current_free_ones():
    """10.09.2026: kimi-k2 и llama-3.3-70b у Groq сняты, mistral-large не входит в
    бесплатный тариф Mistral — по умолчанию должны стоять живые модели."""
    from pmoos.config import load_config
    from pmoos.core.health import pick_model
    cfg = load_config()
    assert cfg.model_for("groq") == "openai/gpt-oss-120b"
    assert cfg.model_for("mistral") == "ministral-14b-latest"
    groq_live = ["allam-2-7b", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
                 "openai/gpt-oss-safeguard-20b", "qwen/qwen3.6-27b", "whisper-large-v3"]
    assert pick_model("groq", groq_live) == "openai/gpt-oss-120b"
    mistral_live = ["mistral-small-latest", "mistral-large-latest", "ministral-8b-latest",
                    "ministral-14b-latest", "codestral-latest"]
    assert pick_model("mistral", mistral_live) == "ministral-14b-latest"


def test_zai_disables_thinking_and_cloudflare_uses_account(monkeypatch):
    from pmoos.config import load_config
    from pmoos.core import ai_providers
    calls = []

    def fake(base, key, model, messages, t, mt, jm, extra_body=None):
        calls.append((base, key, model, extra_body))
        return "ok"

    monkeypatch.setattr(ai_providers, "_openai_like", fake)
    monkeypatch.setenv("ZAI_API_KEY", "zk")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cft")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc7")
    cfg = load_config()
    msgs = [{"role": "user", "content": "x"}]
    for p in ("zai", "cloudflare"):
        ai_providers._chat_once(cfg, msgs, provider=p, role="answer", model=None,
                                temperature=0, max_tokens=5, json_mode=False,
                                use_cache=False)
    (zb, zk, zm, zx), (cb, ck, cm, cx) = calls
    assert zb == "https://api.z.ai/api/paas/v4" and zm == "glm-4.7-flash"
    assert zx == {"thinking": {"type": "disabled"}}
    assert cb == "https://api.cloudflare.com/client/v4/accounts/acc7/ai/v1" and ck == "cft"
    assert cx is None


def test_ollama_cloud_pulls_missing_model_then_retries(monkeypatch):
    import requests
    from pmoos.core import ai_providers
    calls = []

    class R:
        def __init__(self, code, data=None):
            self.status_code, self._d = code, data or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(response=self)

        def json(self):
            return self._d

    def fake_post(url, json=None, timeout=None):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/api/chat") and calls.count("chat") == 1:
            return R(404)
        if url.endswith("/api/pull"):
            return R(200)
        return R(200, {"message": {"content": "работает"}})

    monkeypatch.setattr(requests, "post", fake_post)
    out = ai_providers._ollama_cloud("http://localhost:11434", "gemma4:31b-cloud",
                                     [{"role": "user", "content": "x"}], 0, 5, False)
    assert out == "работает" and calls == ["chat", "pull", "chat"]


def test_keysync_carries_new_keys():
    from pmoos.core.keysync import _syncable
    for k in ("ZAI_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        assert _syncable(k)


def test_batch_chat_runs_ollama_cloud_one_at_a_time(monkeypatch):
    """Бесплатный ollama.com пускает 1 запрос одновременно."""
    from pmoos.config import load_config
    from pmoos.core import ai_providers
    lock, now = threading.Lock(), {"cur": 0, "max": 0}

    def fake_chat(cfg, msgs, **kw):
        with lock:
            now["cur"] += 1
            now["max"] = max(now["max"], now["cur"])
        time.sleep(0.03)
        with lock:
            now["cur"] -= 1
        return "ok"

    monkeypatch.setattr(ai_providers, "chat", fake_chat)
    res = ai_providers.batch_chat(load_config(),
                                  [[{"role": "user", "content": str(i)}] for i in range(5)],
                                  provider="ollama_cloud")
    assert now["max"] == 1 and all(r["ok"] for r in res)

def test_zai_probe_uses_free_models_not_paid_list(monkeypatch):
    """В /models у Z.ai бесплатных flash-моделей нет — проверка по списку брала
    платные glm-4.5…5.3 и писала «нет средств»."""
    from pmoos.core import health

    def boom(*a, **k):
        raise AssertionError("список моделей Z.ai спрашивать не нужно")

    monkeypatch.setattr(health, "_get_json", boom)
    res = health.probe_provider("zai", "key")
    assert res["ok"] and res["models"] == ["glm-4.7-flash", "glm-4.5-flash"]
