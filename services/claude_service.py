import logging

from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)

# The heavy model reasons for a long time before answering — a real narrative call measured
# ~7.5 minutes. The SDK's defaults (600 s timeout, 2 retries) would turn one slow call into a
# ~30-minute hang at 3x the token cost, with the browser long gone. Bound it explicitly:
# generous enough for the slowest observed narrative, and a single retry.
_TIMEOUT_SECONDS = 900
_MAX_RETRIES = 1

_MISSING_KEY = ("DEEPSEEK_API_KEY is not set. Add it to backend/.env locally, or to the app's "
                "environment variables on DigitalOcean, then restart the server.")
if not settings.DEEPSEEK_API_KEY.strip():
    logger.error("claude_service: %s", _MISSING_KEY)

# The cheap work — agent analysis and the input-cell fill — always runs here.
# api_key is never None: given None, the SDK silently falls back to OPENAI_API_KEY, which
# would send DeepSeek calls out with the OpenAI key. An empty key is refused in _complete.
client = OpenAI(
    api_key=settings.DEEPSEEK_API_KEY.strip() or "missing",
    base_url="https://api.deepseek.com",
    timeout=_TIMEOUT_SECONDS,
    max_retries=_MAX_RETRIES,
)

# The narrative may run on an entirely different provider — see HEAVY_* in config. Both are
# reached through the OpenAI SDK, so pointing base_url at api.openai.com is all it takes to
# write the report with GPT while the workbook stays on DeepSeek. Unset, this is the same
# object as `client` and nothing changes.
_heavy_client = OpenAI(
    api_key=settings.heavy_api_key,
    base_url=settings.heavy_base_url,
    timeout=_TIMEOUT_SECONDS,
    max_retries=_MAX_RETRIES,
) if settings.heavy_is_separate else client

HEAVY_MODEL = settings.heavy_model

# deepseek-v4 models are REASONING models: they spend output tokens thinking before they
# write the answer. 32000 leaves room for reasoning + the JSON model on top. On the
# HARDEST prompts even this is fully consumed by reasoning, which is why the heavy model
# (which finishes its reasoning) exists as the fallback.
_MAX_TOKENS = 32000

# Providers disagree about how the output cap is named: the classic parameter is
# max_tokens, newer reasoning models want max_completion_tokens and reject a temperature.
# Rather than guess from the model name — which would go stale the moment a provider ships
# a new family — the first shape is tried and the alternative is used if the API objects.
_alt_params: dict[str, bool] = {}


def _create(api: OpenAI, model: str, prompt: str, alt: bool):
    kwargs = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if alt:
        kwargs["max_completion_tokens"] = _MAX_TOKENS
    else:
        kwargs["max_tokens"] = _MAX_TOKENS
        kwargs["temperature"] = 0.7
    return api.chat.completions.create(**kwargs)


def _complete(prompt: str, model: str) -> tuple[str, str]:
    """One chat completion. Returns (content, finish_reason); content may be ''."""
    api = _heavy_client if model == HEAVY_MODEL else client
    if api is client and not settings.DEEPSEEK_API_KEY.strip():
        raise RuntimeError(_MISSING_KEY)
    alt = _alt_params.get(model, False)
    try:
        resp = _create(api, model, prompt, alt)
    except Exception as exc:
        msg = str(exc)
        retryable = not alt and any(k in msg for k in
                                    ("max_tokens", "max_completion_tokens", "temperature"))
        if not retryable:
            raise
        logger.info("invoke_llm: %s rejected the standard parameters (%s); "
                    "retrying with max_completion_tokens", model, msg[:120])
        _alt_params[model] = True
        resp = _create(api, model, prompt, True)
    choice = resp.choices[0]
    return (choice.message.content or ""), choice.finish_reason


def invoke_llm(prompt: str, model: str = None, heavy: bool = False) -> str:
    """Call the LLM and return the text answer.

    Callers pass Anthropic-style names ("claude_sonnet_4_6") only as an intent hint the
    provider can't use, so the actual model is chosen here:

    - Simple prompts (input-cell fill, agent analysis) run on the cheap DEEPSEEK_MODEL
      (v4-flash), which finishes reasoning quickly and answers.
    - Complex generation (the report narrative) is a large JSON that makes v4-flash burn
      its whole 32 K budget on reasoning and return EMPTY content (finish_reason=length),
      which surfaced to the user as a 502 on every regeneration. Those callers pass
      heavy=True and go straight to the heavy model, which finishes — and which may be a
      different provider entirely.
    - As a safety net, ANY empty answer from the cheap model is retried once on the heavy
      one, so a borderline prompt can never again leave the whole report failed.

    The AGENTS deliberately stay on the cheap model and have no switch — see the note in
    config.py. The narrative passes heavy=True; the cell-fill passes its own CELLFILL_HEAVY.
    """
    primary = HEAVY_MODEL if heavy else settings.DEEPSEEK_MODEL
    content, reason = _complete(prompt, primary)
    if not content.strip() and primary != HEAVY_MODEL:
        logger.warning("invoke_llm: %s returned empty content (finish_reason=%s); "
                       "retrying on %s", primary, reason, HEAVY_MODEL)
        content, reason = _complete(prompt, HEAVY_MODEL)
    if not content.strip():
        logger.error("invoke_llm: empty content from %s (finish_reason=%s)", primary, reason)
    return content
