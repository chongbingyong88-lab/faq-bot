"""Provider-agnostic text generation.

    generate(system, user) -> str

Returns exactly what the model emitted. No JSON parsing, no cleanup —
callers keep their own, so swapping providers cannot change how a reply is
interpreted.

Select with LLM_PROVIDER: gemini | anthropic | stub, or any of the
OpenAI-compatible ones — groq, openrouter, cerebras, together, deepseek,
openai — which all share a single code path.

    LLM_PROVIDER=gemini
    GEMINI_API_KEY=...
    GEMINI_MODEL=gemini-2.0-flash-lite     # any free-tier Flash / Flash-Lite

    LLM_PROVIDER=groq                      # when Gemini's daily cap is hit
    GROQ_API_KEY=...
    GROQ_MODEL=qwen/qwen3.8-27b

Model ids churn — `list_models("groq")` shows what a key actually reaches.

Every free tier caps requests per day AND per minute, so requests are paced
(<NAME>_RPM) and retried with exponential backoff.

A `model` of the form "provider:id" routes that one call elsewhere:

    generate(sys, usr, model="gemini:gemini-3.1-flash-lite")

which is how the eval keeps its judge fixed while the bot model varies.
Without it, comparing a Groq model against a Gemini one would silently
change the grader too.
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import threading
import time

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "600"))
MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "6"))
# Where to go when the configured model's daily quota is gone. Comma-separated
# "provider:model", tried in order. Free tiers are capped per model per day, so
# without this an exhausted quota is an outage: the clinic's bot answers every
# customer with an apology until midnight Pacific.
FALLBACKS = [f.strip() for f in os.getenv("LLM_FALLBACK", "").split(",") if f.strip()]
# How long to stop offering a model that reported its daily quota gone. The
# provider SDKs retry a 429 internally before raising, so re-trying a known
# dead model cost 18 seconds of a customer's time on every single message.
# Not the rest of the day: quota was observed replenishing gradually.
EXHAUSTED_COOLDOWN_S = float(os.getenv("LLM_EXHAUSTED_COOLDOWN_S", "900"))
# Attempts to spend on one model before trying the next, when there IS a next.
# A 503 "model is overloaded" was costing 26 seconds of a customer's time
# backing off 2s, 4s, 8s against a busy model while a healthy one sat unused.
# Backoff is right when it is the only model; switching is better when it
# is not.
ATTEMPTS_BEFORE_FALLBACK = int(os.getenv("LLM_ATTEMPTS_BEFORE_FALLBACK", "2"))
# A budget for the whole call, retries and fallbacks included. Past it we stop
# starting new attempts and let the caller escalate. A customer would rather
# hear that our team will come back, in six seconds, than get a perfect answer
# in twenty-eight — and 28.5s is the worst case actually measured.
# Must leave room for a fallback attempt after a slow primary, or the
# timeout that was meant to trigger the fallback prevents it: at 12s, one
# 10s attempt used the whole budget and `generate` raised instead of asking
# the next model. That cost six rows in a single eval run.
DEADLINE_S = float(os.getenv("LLM_DEADLINE_S", "30"))
# Ceiling on a SINGLE call, so a model that is merely slow is treated as a
# failure and the chain moves on. DEADLINE_S only stops a new attempt or a
# backoff starting past the budget; it cannot abandon one in-flight call.
# On 9 Sep gemini-3.5-flash-lite answered "say OK" in 28s while
# gemini-3.1-flash-lite answered it in 1.5s on the same key in the same
# minute — a success by every error test we had, and unusable in a chat.
# Gemini refuses anything under 10s ("Manually set deadline 2s is too short.
# Minimum allowed deadline is 10s") and enforces it server-side, answering
# 504 rather than dropping the socket. Real RAG calls measured 4-10s, so a
# ceiling *at* the 10s floor kills healthy work; sit above it.
ATTEMPT_TIMEOUT_S = float(os.getenv("LLM_ATTEMPT_TIMEOUT_S", "15"))
# Where "this model is out for today" survives a restart. Without it every
# restart re-pays the cost of rediscovering the same dead model, which is the
# slowest single request a customer can hit.
COOLDOWN_FILE = os.getenv("LLM_COOLDOWN_FILE", str(
    pathlib.Path(__file__).resolve().parent.parent / "data" / "llm_cooldown.json"))


def _load_cooldowns() -> dict[str, float]:
    """Wall-clock deadlines, so they still mean something after a restart."""
    try:
        with open(COOLDOWN_FILE, encoding="utf-8") as fh:
            return {k: v for k, v in json.load(fh).items() if v > time.time()}
    except Exception:
        return {}


def _save_cooldowns() -> None:
    try:
        pathlib.Path(COOLDOWN_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(COOLDOWN_FILE, "w", encoding="utf-8") as fh:
            json.dump(_exhausted, fh)
    except Exception:
        pass          # a cache we cannot write is a slow reply, not an outage


_exhausted: dict[str, float] = _load_cooldowns()

# Providers that speak the OpenAI chat-completions shape. Same request body,
# different base URL — so one code path covers all of them, and switching
# when a daily quota runs out is a one-line .env change rather than new code.
# Set <NAME>_API_KEY and optionally <NAME>_MODEL.
OPENAI_COMPAT = {
    "groq":       "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "cerebras":   "https://api.cerebras.ai/v1",
    "together":   "https://api.together.xyz/v1",
    "deepseek":   "https://api.deepseek.com",
    "openai":     "https://api.openai.com/v1",
}

_DEFAULT_MODEL = {
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "cerebras": "llama-3.3-70b",
    "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o-mini",
}

MODELS = {
    "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
    "gemini": os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite"),
    "stub": "stub",
    **{p: os.getenv(f"{p.upper()}_MODEL", d) for p, d in _DEFAULT_MODEL.items()},
}
# Requests per minute. Free tiers throttle per minute as well as per day;
# pacing here is cheaper than discovering it as a 429 mid-eval.
RPM = {p: int(os.getenv(f"{p.upper()}_RPM", "0")) or None
       for p in list(OPENAI_COMPAT) + ["gemini"]}
RPM["gemini"] = int(os.getenv("GEMINI_RPM", "15"))
RPM = {k: v for k, v in RPM.items() if v}

_clients: dict[str, object] = {}
_lock = threading.Lock()

# Which model actually served the last call *on this thread*. Thread-local
# because app.py answers inside asyncio.to_thread, so a module global would
# attribute one request's model to another's row.
#
# The eval records it per row. Without it, an exhausted primary silently
# continues on a fallback — and when that fallback is also the judge, the row
# is self-graded with nothing in the data to say so. Reconstructing that from
# interleaved log lines was not worth trusting, so 47 khengdental rows were
# thrown away on 9 Sep rather than reported.
_last = threading.local()


def last_model() -> str | None:
    """Provider:model that answered the most recent generate() on this thread."""
    return getattr(_last, "model", None)


class _Pacer:
    """Space out request starts across threads."""

    def __init__(self, rpm: float):
        self.interval = 60.0 / max(rpm, 1)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            pause = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if pause:
            time.sleep(pause)


_pacer = _Pacer(RPM[PROVIDER]) if PROVIDER in RPM else None


def model_name() -> str:
    return MODELS.get(PROVIDER, "unknown")


def is_exhausted(exc: Exception) -> bool:
    """A daily cap, as opposed to a momentary rate limit.

    Retrying will not help; another model might. Per-minute limits are
    deliberately excluded — those clear on their own in seconds.
    """
    text = str(exc).lower()
    return ("resource_exhausted" in text or "quota" in text
            or "tokens per day" in text or "requests per day" in text
            or "perday" in text.replace("_", ""))


# A daily cap does not clear in fifteen minutes. Cooling for 900s meant an
# exhausted model was re-probed roughly 96 times a day, and every probe is
# another request billed against the quota that is already spent. Cool until
# the provider's quota day actually rolls over instead.
RESET_TZ = {"gemini": "America/Los_Angeles"}     # Google counts a Pacific day
DEFAULT_RESET_TZ = "UTC"


def seconds_to_daily_reset(provider: str) -> float:
    """Until this provider's quota day rolls over, or the flat cooldown."""
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(RESET_TZ.get(provider, DEFAULT_RESET_TZ))
        now = datetime.now(tz)
        midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return max(60.0, (midnight - now).total_seconds())
    except Exception:
        # No tzdata on this machine: a flat hour still beats fifteen minutes.
        return max(EXHAUSTED_COOLDOWN_S, 3600.0)


def is_timeout(exc: Exception) -> bool:
    """We gave up waiting, as opposed to the server refusing.

    Distinguished from is_retryable because the right response differs: a 503
    is worth asking the same model again, a timeout means this model is slow
    right now and asking it again spends the budget twice for one answer.

    Gemini enforces the ceiling server-side rather than letting the socket
    expire, so it answers `504 DEADLINE_EXCEEDED` — which says nothing about
    "timeout" and was therefore read as an ordinary server error and retried.
    Two attempts then spent the whole DEADLINE_S budget, the fallback was
    skipped, and six sepadental rows failed outright instead of being answered
    by the next model. Match what the provider actually says.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    return any(s in text for s in
               ("timeout", "timed out", "deadline_exceeded",
                "deadline exceeded", "504"))


def is_retryable(exc: Exception) -> bool:
    """Rate limits AND transient server errors.

    A newly released model answers 503 UNAVAILABLE ('experiencing high
    demand') fairly often. Letting that through looks identical to a wrong
    answer in an eval, so it must be retried, not scored.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    return any(s in text for s in
               ("429", "rate limit", "rate_limit", "resource_exhausted",
                "quota", "too many requests", "overloaded", "529",
                "503", "unavailable", "500", "502", "504",
                "internal error", "timeout", "timed out"))


# --------------------------------------------------------------- providers

def _anthropic(system: str, user: str, model: str | None = None,
               provider: str | None = None) -> str:
    import anthropic

    with _lock:
        if "anthropic" not in _clients:
            _clients["anthropic"] = anthropic.Anthropic()
    resp = _clients["anthropic"].messages.create(
        model=model or MODELS["anthropic"],
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _gemini(system: str, user: str, model: str | None = None,
            provider: str | None = None) -> str:
    from google import genai
    from google.genai import types

    with _lock:
        if "gemini" not in _clients:
            key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not key:
                raise RuntimeError("set GEMINI_API_KEY in .env")
            _clients["gemini"] = genai.Client(api_key=key)
    client = _clients["gemini"]
    name = model or MODELS["gemini"]
    try:
        resp = client.models.generate_content(
            model=name,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=MAX_TOKENS,
                temperature=0,
                # milliseconds. Raises on a slow model, which is_retryable
                # already reads as retryable, so the fallback chain advances.
                http_options=types.HttpOptions(
                    timeout=int(ATTEMPT_TIMEOUT_S * 1000)),
            ),
        )
    except Exception as exc:
        # Match the status, not a substring. "404" anywhere in a quota error's
        # body was enough to report a perfectly valid model as unavailable,
        # which hid the real 429 behind a list of forty model ids. An error
        # handler that discards the error is worse than no handler.
        detail = str(exc)
        if detail.lstrip().startswith("404") or "NOT_FOUND" in detail:
            names = [m.name for m in client.models.list()][:40]
            raise RuntimeError(
                f"GEMINI_MODEL={name!r} not available ({detail[:120]}). "
                f"Models this key can see: {names}"
            ) from exc
        raise
    text = (resp.text or "").strip()
    if not text:
        # Gemini 3.x Flash models think before answering and can spend the
        # whole budget doing it. An empty string would be parsed downstream
        # as an unanswerable reply, i.e. a silent false pass — so say so.
        raise RuntimeError(
            f"{name} returned no text (finish_reason="
            f"{getattr(resp.candidates[0], 'finish_reason', '?') if resp.candidates else '?'}"
            f"); raise LLM_MAX_TOKENS above {MAX_TOKENS}")
    return text


def _openai_compat(system: str, user: str, model: str | None = None,
                   provider: str | None = None) -> str:
    """Groq, OpenRouter, Cerebras, Together, DeepSeek, OpenAI.

    They differ only in base URL and key, so a quota wall on one is a
    one-line .env change rather than a code change.
    """
    import httpx

    p = provider or PROVIDER
    base = OPENAI_COMPAT[p]
    key = os.getenv(f"{p.upper()}_API_KEY")
    if not key:
        raise RuntimeError(f"set {p.upper()}_API_KEY in .env")
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model or MODELS[p],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
        },
        timeout=ATTEMPT_TIMEOUT_S,
    )
    if r.status_code == 429:
        raise RuntimeError(f"429 rate limit from {p}: {r.text[:200]}")
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} from {p}: {r.text[:300]}")
    text = (r.json()["choices"][0]["message"]["content"] or "").strip()
    if not text:
        # Same trap as Gemini's thinking models: an empty string parses
        # downstream as a refusal, which is a silent false pass.
        raise RuntimeError(f"{p} returned no text; raise LLM_MAX_TOKENS")
    return text


def transcribe(audio: bytes, filename: str = "voice.ogg",
               mime: str = "audio/ogg") -> str:
    """Speech to text, via whichever OpenAI-compatible host has Whisper.

    Malaysians send voice notes constantly, and a clinic bot that answers
    "please type your question" to half its messages is not much of a bot.
    Groq hosts whisper-large-v3 on a quota separate from chat, so a spent
    text budget does not also take transcription down.

    The transcript then goes through the ordinary pipeline, which means the
    price and fact guards apply to a spoken question exactly as to a typed
    one — the reply is the part that must be safe, not the input.
    """
    import httpx

    p = os.getenv("TRANSCRIBE_PROVIDER", "groq").lower()
    if p not in OPENAI_COMPAT:
        raise RuntimeError(f"TRANSCRIBE_PROVIDER={p!r} has no audio endpoint")
    key = os.getenv(f"{p.upper()}_API_KEY")
    if not key:
        raise RuntimeError(f"set {p.upper()}_API_KEY to transcribe voice notes")
    model = os.getenv("TRANSCRIBE_MODEL", "whisper-large-v3")
    # A vocabulary prompt was tried here and made transcription worse:
    # priming with dental terms turned "TTDI clinic" into "TTDI codec" and
    # did not fix the Malay word it was meant to fix. Reverted.
    r = httpx.post(
        f"{OPENAI_COMPAT[p]}/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": (filename, audio, mime)},
        data={"model": model, "response_format": "json"},
        timeout=120,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} from {p} transcribe: {r.text[:250]}")
    return (r.json().get("text") or "").strip()


def list_models(provider: str | None = None) -> list[str]:
    """Model ids a key can actually see. Ids churn; check rather than guess."""
    import httpx

    p = provider or PROVIDER
    if p not in OPENAI_COMPAT:
        raise RuntimeError(f"{p} has no /models endpoint here")
    key = os.getenv(f"{p.upper()}_API_KEY")
    r = httpx.get(f"{OPENAI_COMPAT[p]}/models",
                  headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    return sorted(m["id"] for m in r.json().get("data", []))


def _stub(system: str, user: str, model: str | None = None,
          provider: str | None = None) -> str:
    """Canned reply so the scorer can be exercised without an API key."""
    return os.getenv("LLM_STUB_REPLY",
                     '{"answerable": false, "reply": "Let me check that with '
                     'my colleague and get back to you shortly.", "sources": []}')


_PROVIDERS = {"anthropic": _anthropic, "gemini": _gemini, "stub": _stub,
              **{p: _openai_compat for p in OPENAI_COMPAT}}


def generate(system: str, user: str, model: str | None = None) -> str:
    """One completion, as raw text. Paced and retried on rate limits.

    `model` overrides the configured one for this call only. Free-tier
    quota is counted per model id, so pointing the eval judge at a
    different id both keeps the grader fixed while the bot model varies
    and spends a separate daily allowance.
    """
    if PROVIDER not in _PROVIDERS:
        raise RuntimeError(
            f"LLM_PROVIDER={PROVIDER!r} unknown; pick one of {sorted(_PROVIDERS)}")

    # "gemini:gemini-3.1-flash-lite" pins the judge to one provider while the
    # bot runs on another — without it, comparing a Groq model against a
    # Gemini one silently changes the grader too. A bare id keeps the
    # configured provider; OpenRouter's ":free" suffix is left alone because
    # its prefix is never a provider name.
    def route(spec: str | None) -> tuple[str, str | None]:
        if spec and ":" in spec:
            prefix, _, rest = spec.partition(":")
            if prefix in _PROVIDERS:
                return prefix, (rest or None)
        return PROVIDER, spec

    # An explicit model argument pins the call — the eval judge must not be
    # quietly answered by a different model than the one it was pinned to.
    chain = [model] if model else [None, *FALLBACKS]

    def key_of(spec: str | None) -> str:
        # One key function for both the cooldown check and the cooldown set:
        # writing them separately meant the primary was recorded as
        # "gemini:gemini-3.5-flash-lite" and looked up as "gemini:None", so
        # the dead model was retried on every message.
        prov, mdl = route(spec)
        return f"{prov}:{mdl or MODELS.get(prov)}"

    now = time.time()
    live = [c for c in chain if _exhausted.get(key_of(c), 0) < now]
    # All of them cooling down: try the original anyway rather than refuse,
    # since a stale cooldown must never be the reason a customer gets nothing.
    chain = live or chain[:1]

    started = time.monotonic()
    last: Exception | None = None
    # Clear first: a raising call must not leave the previous call's model
    # behind for a caller to record against the wrong row.
    _last.model = None
    for i, spec in enumerate(chain):
        if i and time.monotonic() - started > DEADLINE_S:
            print(f"  {DEADLINE_S:.0f}s budget spent — escalating instead of "
                  f"trying another model", flush=True)
            break
        prov, mdl = route(spec)
        fn = _PROVIDERS[prov]
        if i:
            print(f"  falling back to {prov}:{mdl or MODELS[prov]}", flush=True)
        for attempt in range(MAX_RETRIES):
            if _pacer and prov == PROVIDER:
                _pacer.wait()
            try:
                text = fn(system, user, mdl, prov)
                _last.model = f"{prov}:{mdl or MODELS.get(prov)}"
                return text
            except Exception as exc:
                last = exc
                # A day's quota will not clear by waiting. Stop burning the
                # retry budget and try the next model instead.
                if is_exhausted(exc):
                    key = key_of(spec)
                    cool = seconds_to_daily_reset(prov)
                    _exhausted[key] = time.time() + cool
                    _save_cooldowns()
                    print(f"  {key} out of quota — skipping it for "
                          f"{cool / 3600:.1f}h, until the quota day rolls over",
                          flush=True)
                    if i < len(chain) - 1:
                        break
                # Slowness is this model's current condition, not a blip.
                # Asking it again costs another full timeout for the same
                # answer, so move while there is somewhere to move to.
                if is_timeout(exc) and i < len(chain) - 1:
                    print(f"  {prov}:{mdl or MODELS[prov]} exceeded "
                          f"{ATTEMPT_TIMEOUT_S:.0f}s — switching", flush=True)
                    break

                if not is_retryable(exc) or attempt == MAX_RETRIES - 1:
                    if i < len(chain) - 1:
                        break
                    raise
                # Another model is waiting: stop nursing this one.
                if (i < len(chain) - 1
                        and attempt + 1 >= ATTEMPTS_BEFORE_FALLBACK):
                    print(f"  {prov} still failing after {attempt + 1} "
                          f"attempts — switching", flush=True)
                    break
                # 2s, 4s, 8s, 16s, 32s plus jitter, so parallel callers
                # don't resync
                delay = 2 ** (attempt + 1) + random.uniform(0, 1.5)
                if time.monotonic() - started + delay > DEADLINE_S:
                    break        # the wait alone would blow the budget
                print(f"  retrying in {delay:.1f}s ({attempt + 1}/"
                      f"{MAX_RETRIES - 1}): {str(exc)[:70]}", flush=True)
                time.sleep(delay)
    raise last or RuntimeError("no provider available")
