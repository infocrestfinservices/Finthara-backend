from dotenv import load_dotenv
load_dotenv()

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    DEEPSEEK_API_KEY: str = "sk-e8bda1699430447e97c0d6fcbe83471a"


    DEEPSEEK_MODEL: str = "deepseek-v4-flash"
    # The complex report-narrative generation (a big JSON: prose + KPIs + several sheets)
    # makes v4-flash spend its ENTIRE 32 K output budget reasoning and return EMPTY content
    # (finish_reason=length -> "Empty model response" -> 502). v4-pro finishes its reasoning
    # and writes the answer (finish_reason=stop). Used for the narrative and as the fallback
    # whenever flash comes back empty.
    DEEPSEEK_HEAVY_MODEL: str = "deepseek-v4-pro"
    # ── The narrative (what the Word and PDF reports are written from) ────────────
    # This is the one expensive call in the pipeline, and it does not have to run on the
    # same provider as the cheap cell-fill and agent calls. Set these three in .env to
    # send it elsewhere — e.g. OpenAI:
    #
    #   HEAVY_API_KEY=sk-...
    #   HEAVY_BASE_URL=https://api.openai.com/v1
    #   HEAVY_MODEL=gpt-4.1
    #
    # Leave HEAVY_API_KEY blank and everything stays on DeepSeek exactly as before.
    # The client is built with the OpenAI SDK either way, so no new dependency is needed.
    HEAVY_API_KEY: str = ""
    HEAVY_BASE_URL: str = ""
    HEAVY_MODEL: str = ""
    # Setting the conventional OPENAI_API_KEY is enough on its own: it implies OpenAI's
    # endpoint and OPENAI_MODEL (default gpt-4.1). The HEAVY_* trio still wins if given,
    # so any other provider can be pointed at without touching this.
    # The cell-fill is the ONE call whose output every figure in both deliverables is built
    # on, and it is by far the slowest step (measured: ~343 s on v4-flash against ~12 s on
    # gpt-4.1 for the same 81-field prompt — the cheap model is not wrong, it is slow).
    # Set CELLFILL_HEAVY=true in .env to run it on the heavy model instead. Off by default,
    # so this is a one-line switch in either direction with no code change.
    CELLFILL_HEAVY: bool = False
    # The AGENTS stay on the cheap model — deliberately, and not for want of a faster one.
    # Measured: 223 s against 28.5 s on the heavy model for the same market-research prompt,
    # so moving them would cut a generation from ~5 minutes to ~1.5. But the cheap model
    # writes MORE (15,798 characters against 8,144 for that prompt), and the client's call
    # was quality over speed. Do not add a switch for this without asking them again.
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4.1"
    # Cover artwork generated per project. Falls back to dall-e-3, then to the bundled
    # industry photograph, so an unavailable model never breaks a report.
    OPENAI_IMAGE_MODEL: str = "gpt-image-1"
    _OPENAI_BASE = "https://api.openai.com/v1"

    @property
    def _use_openai(self) -> bool:
        return bool(self.OPENAI_API_KEY.strip()) and not self.HEAVY_API_KEY.strip()

    @property
    def heavy_api_key(self) -> str:
        return (self.HEAVY_API_KEY.strip() or self.OPENAI_API_KEY.strip()
                or self.DEEPSEEK_API_KEY)

    @property
    def heavy_base_url(self) -> str:
        if self.HEAVY_BASE_URL.strip():
            return self.HEAVY_BASE_URL.strip()
        return self._OPENAI_BASE if self._use_openai else "https://api.deepseek.com"

    @property
    def heavy_model(self) -> str:
        if self.HEAVY_MODEL.strip():
            return self.HEAVY_MODEL.strip()
        return self.OPENAI_MODEL.strip() if self._use_openai else self.DEEPSEEK_HEAVY_MODEL

    @property
    def heavy_is_separate(self) -> bool:
        """True when the narrative runs somewhere other than the default DeepSeek client."""
        return bool(self.HEAVY_API_KEY.strip() or self.HEAVY_BASE_URL.strip()
                    or self.HEAVY_MODEL.strip() or self.OPENAI_API_KEY.strip())
    # Razorpay. The KEY_ID is public (the browser needs it to open the checkout); the
    # SECRET never leaves the server — it is what proves a payment actually happened.
    # Which deployment this is. Anything other than "production" is treated as a place
    # where it is safe to expose the engine-test harness and the interactive API docs.
    # It defaults to development so a machine with no ENV set behaves as a dev box —
    # a production server is the one that has to say so, and saying so is one line.
    ENV: str = "development"

    # ── who the invoice is FROM ────────────────────────────────────────────────
    # Read from the environment rather than written into the code, because these are legal
    # details on a document a customer keeps: they change without the software changing, and
    # the person who knows them is not the person editing Python.
    COMPANY_NAME: str = "ReportCraft AI"
    COMPANY_ADDRESS: str = ""
    COMPANY_EMAIL: str = "support@infocrest.in"
    COMPANY_STATE: str = ""
    # Empty = NOT registered for GST. That is not a cosmetic difference: an unregistered
    # business must not charge GST and must not issue a "Tax Invoice" — it issues a Bill of
    # Supply carrying a statement that it is not registered. Setting a GSTIN here is the
    # single switch that turns the document into a tax invoice with a tax breakup.
    COMPANY_GSTIN: str = ""
    GST_RATE: float = 0.18
    # Services accounting code. 998314 covers IT design and development services, which is
    # what a generated financial report is. Only appears once a GSTIN exists.
    SAC_CODE: str = "998314"
    # Browser credentials for /docs in production. Empty means the docs do not exist there
    # at all, which is the safe default: an API reference that lists every endpoint and
    # every field is a map of the attack surface, and it is only useful to staff.
    DOCS_USER: str = "admin"
    DOCS_PASSWORD: str = ""

    # Beta/testing switch: when true, every account gets full report + export access,
    # bypassing the plan limits in services/entitlements.py entirely — no paywall for
    # anyone, not just one email. Set UNLOCK_ALL=false (or unset it) before real launch,
    # once payments are meant to actually gate anything.
    UNLOCK_ALL: bool = False

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    # Set in the Razorpay dashboard when the webhook URL is registered. It is NOT the key
    # secret — a different value, and the only thing that distinguishes a real delivery from
    # anyone on the internet posting "subscription.charged". Empty means every webhook is
    # rejected, which is the correct behaviour for a server that cannot tell them apart.
    RAZORPAY_WEBHOOK_SECRET: str = ""

    @property
    def payments_enabled(self) -> bool:
        return bool(self.RAZORPAY_KEY_ID.strip() and self.RAZORPAY_KEY_SECRET.strip())

    @property
    def cors_origins(self) -> list[str]:
        """The origins to allow, or ["*"] when none are configured.

        Falling back to "*" is deliberate for a development machine, where the frontend moves
        between ports. It is NOT safe in production, and main.py refuses to start that way —
        a silent wildcard on a live domain is exactly the failure this setting exists to
        prevent.
        """
        raw = [o.strip().rstrip("/") for o in (self.CORS_ORIGINS or "").split(",")]
        return [o for o in raw if o] or ["*"]

    SECRET_KEY: str = "changeme"
    # Which sites the browser may call this API from, comma separated. Empty means "anything",
    # which is right on a laptop and wrong the moment this is on a domain. Kept as a plain
    # string rather than a list because that is what an environment variable can carry.
    CORS_ORIGINS: str = ""

    # Where the app lives, used to build links that go INTO emails. A reset link has to be
    # absolute and has to point at the browser, not at the API — the two are different hosts
    # in production, and a relative link in an email goes nowhere at all.
    FRONTEND_URL: str = "http://localhost:5173"
    # How long a reset link stays usable. Short, because a link that works for a week is a
    # week in which a forwarded or leaked email is still a working key to the account.
    RESET_TOKEN_MINUTES: int = 30

    RESEND_API_KEY: str = ""
    FROM_EMAIL: str = "onboarding@resend.dev"
    FROM_NAME: str = "Smart Project Blueprint"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    class Config:
        env_file = ".env"

settings = Settings()
