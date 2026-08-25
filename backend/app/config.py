from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_CURRENCY = "INR"
DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_PHONE_REGION_PREFIX = "+91"
CSV_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
SESSION_COOKIE_NAME = "fyn_session"

# Used only when AUTH_SECRET is unset, which is a local-development state. It is
# never a fallback in a deployment: `require_production_auth_config` refuses to
# start with this value in place once ENVIRONMENT is "production".
DEVELOPMENT_AUTH_SECRET = "development-only-auth-secret"
# Short enough not to be a nuisance, long enough that the key is not the weak
# part of an HMAC over a six-digit code.
MINIMUM_AUTH_SECRET_LENGTH = 32


class Settings(BaseSettings):
    app_name: str = "fyn AI API"
    # "production" turns the development authentication conveniences below into
    # startup failures. It is deliberately not inferred from the database URL:
    # local development runs against PostgreSQL too, and a guess that wrong
    # either blocks local work or waves a real deployment through.
    environment: str = "development"
    database_url: str = "postgresql+psycopg://finance:finance@localhost:5432/finance"
    cors_origins: str = "http://localhost:3000"
    openai_api_key: str | None = None

    # ── Authentication ───────────────────────────────────────────────────────
    # Peppers one-time-code hashes. Rotating it invalidates codes in flight and
    # nothing else; sessions are hashed without it.
    auth_secret: str = DEVELOPMENT_AUTH_SECRET
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    # Set to a shared parent (".example.com") when the app and the API are on
    # sibling subdomains, which is how this deploys behind one reverse proxy.
    session_cookie_domain: str | None = None
    session_ttl_days: int = 30
    # The first account to sign in adopts the seeded local user, so the demo
    # data that predates authentication stays reachable instead of orphaned.
    claim_seeded_user_on_first_login: bool = True

    otp_code_length: int = 6
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_interval_seconds: int = 45
    otp_max_sends_per_hour: int = 5
    # Returns the code in the API response. Local development and tests only;
    # refused at startup once ENVIRONMENT is "production".
    otp_debug_echo: bool = False
    default_phone_prefix: str = DEFAULT_PHONE_REGION_PREFIX

    # "auto" resolves to the real provider when its credentials are present and
    # to a console sender otherwise, so a fresh checkout runs without accounts.
    sms_provider: str = "auto"
    email_provider: str = "auto"
    msg91_auth_key: str | None = None
    msg91_template_id: str | None = None
    msg91_sender_id: str | None = None
    # Flow variable names are case-sensitive. MSG91's OTP placeholder is
    # conventionally registered as ##OTP##, whose payload key is `OTP`.
    msg91_otp_variable: str = "OTP"
    postmark_server_token: str | None = None
    postmark_from_email: str | None = None
    postmark_message_stream: str = "outbound"
    otp_email_subject: str = "Your fyn AI sign-in code"
    # Shared-record notifications use the same providers through a separate
    # transactional outbox. SMS needs its own DLT-registered template because
    # an invitation/reminder is not an OTP message.
    msg91_lending_template_id: str | None = None
    lending_web_url: str = "http://localhost:3000"
    lending_notification_worker_enabled: bool = True
    lending_notification_poll_seconds: int = Field(default=5, ge=1, le=60)
    lending_notification_max_attempts: int = Field(default=5, ge=1, le=20)
    google_client_id: str | None = None
    operator_model: str = "gpt-5.6-luna"
    # Complex SQL analysis gets a quality-first reasoning budget while routine
    # conversational turns keep the lower-latency operator baseline.
    operator_analysis_reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "high"
    planner_model: str = "gpt-5.6-terra"
    validator_model: str = "gpt-5.6-luna"
    reconciler_model: str = "gpt-5.6-luna"
    suggester_model: str = "gpt-5.6-luna"
    embedding_model: str = "text-embedding-3-small"
    primary_agent_enabled: bool = True
    # Mounts run_governed_sql on the agent loop: model-authored SELECTs behind
    # the sqlglot gate and database row-level security (blueprint phase 2).
    sql_lane_enabled: bool = True
    # SQL is the primary native-ledger analysis language. ``hybrid`` retains
    # the legacy AnalysisPlan/template grammar as an authoring option; ``sql``
    # gives the Operator the full tenant-governed schema and one arbitrary
    # read-only SELECT surface instead of a finite transform vocabulary.
    analysis_query_mode: Literal["sql", "hybrid"] = "sql"
    # Kill switches for the foreign-source lanes: a leaking connector or a
    # bad join can be closed by configuration instead of a deploy.
    external_source_lane_enabled: bool = True
    federation_lane_enabled: bool = True
    # Mounts run_python_analysis: model-authored Python over datasets the
    # governed lanes already returned, inside the bounded sandbox. Code
    # execution earns its own switch, closable without a deploy.
    python_lane_enabled: bool = True
    # Comma-separated hosts an external database source may name. Empty keeps
    # local development usable; a deployment sets it to state its answer.
    external_source_hosts: str = ""
    # Turns the coordinates a transaction carries into a place name. Off by
    # default because it sends those coordinates to a third party, which is a
    # decision an operator should make rather than inherit.
    location_enrichment_enabled: bool = False
    # "auto" resolves to Nominatim, which needs no account. Set a different
    # name to point at another implementation, or leave enrichment off.
    geocoding_provider: str = "auto"
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    # Nominatim blocks clients that do not identify themselves. Point this at a
    # contact address a maintainer actually reads before enabling enrichment
    # against the public instance — it is how they reach you instead of
    # blocking you.
    geocoding_user_agent: str = "fyn-ai/0.1 (self-hosted; set GEOCODING_USER_AGENT)"
    # Files are the sole source of operation definitions. Core definitions are
    # shipped with the API; managed definitions may come from a shared volume.
    operations_core_dir: str = str(Path(__file__).resolve().parents[1] / "operations")
    operations_managed_dir: str | None = str(Path(__file__).resolve().parents[1] / "managed-operations")
    operations_watch_enabled: bool = True
    operation_candidate_limit: int = 12
    operation_watch_debounce_ms: int = 500
    # Startup recovery is deliberately a small worker pool. A large backlog
    # therefore increases drain time, not process count, memory use, or model
    # request concurrency.
    agent_recovery_max_concurrency: int = Field(default=4, ge=1, le=32)
    agent_recovery_claim_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    agent_recovery_max_postprocess_attempts: int = Field(default=2, ge=0, le=10)
    agent_recovery_min_interval_ms: int = Field(default=50, ge=0, le=5000)
    agent_recovery_idle_poll_seconds: int = Field(default=5, ge=1, le=60)
    default_currency: str = DEFAULT_CURRENCY
    default_timezone: str = DEFAULT_TIMEZONE
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("operations_managed_dir", mode="before")
    @classmethod
    def empty_managed_operation_dir(cls, value):
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def google_audience(self) -> str | None:
        """The OAuth client whose ID tokens this installation will accept."""
        return (self.google_client_id or "").strip() or None

    @property
    def sms_sender_name(self) -> str:
        if self.sms_provider != "auto":
            return self.sms_provider
        return "msg91" if self.msg91_auth_key and self.msg91_template_id else "console"

    @property
    def email_sender_name(self) -> str:
        if self.email_provider != "auto":
            return self.email_provider
        return "postmark" if self.postmark_server_token and self.postmark_from_email else "console"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def development_auth_shortcuts(settings: Settings) -> list[str]:
    """Every authentication weakness worth naming, safe locally and not deployed."""
    return unsafe_auth_settings(settings) + [
        reason for _, reason in sorted(unavailable_otp_channels(settings).items())
    ]


def unsafe_auth_settings(settings: Settings) -> list[str]:
    """Settings that compromise every channel at once.

    The shared pepper makes one-time codes forgeable by anyone with the source,
    and the debug echo hands the code straight back to the caller. Neither
    disables a way in; both make every way in unsound, which is why they stop a
    deployment from starting at all.
    """
    problems = []
    # Checked by shape, not just against the placeholder. `AUTH_SECRET=` in an
    # env file reads as an empty string rather than as unset, so it never equals
    # the placeholder and would otherwise pass — while being the worst of the
    # three, since it keys the HMAC with nothing at all.
    secret = settings.auth_secret.strip()
    if not secret:
        problems.append("AUTH_SECRET is empty, so one-time codes are keyed with nothing")
    elif secret == DEVELOPMENT_AUTH_SECRET:
        problems.append("AUTH_SECRET is still the development placeholder")
    elif len(secret) < MINIMUM_AUTH_SECRET_LENGTH:
        problems.append(f"AUTH_SECRET is under {MINIMUM_AUTH_SECRET_LENGTH} characters")
    if settings.otp_debug_echo:
        problems.append("OTP_DEBUG_ECHO returns one-time codes to the caller")
    return problems


def unavailable_otp_channels(settings: Settings) -> dict[str, str]:
    """Channels with no delivery provider, keyed by OtpChannel value.

    A console sender would print sign-in codes to the server log instead of
    sending them, so the channel cannot be offered. That disables one way in,
    not the service: refusing to start over it would take down sign-in on the
    channel that *is* configured, and every other endpoint with it, for a
    condition only reachable by someone choosing that one way in. So it is
    reported at startup and refused where the code would be sent — the same
    shape as an unconfigured Google client.
    """
    unavailable = {}
    if settings.sms_sender_name == "console":
        unavailable["phone"] = "no SMS provider is configured, so phone codes would only be printed to this log (set MSG91_AUTH_KEY and MSG91_TEMPLATE_ID)"
    if settings.email_sender_name == "console":
        unavailable["email"] = "no email provider is configured, so email codes would only be printed to this log (set POSTMARK_SERVER_TOKEN and POSTMARK_FROM_EMAIL)"
    return unavailable


def require_production_auth_config(settings: Settings) -> None:
    """Refuse to start a deployment whose authentication is unsafe.

    Unsafe is not the same as incomplete — see the two functions above. Outside
    production every finding is printed rather than raised: local work has to be
    possible without an SMS account, but it should never be quietly unclear that
    sign-in codes are going to a terminal.
    """
    if settings.environment == "production":
        problems = unsafe_auth_settings(settings)
        if problems:
            raise RuntimeError("Unsafe authentication configuration: " + "; ".join(problems))
        for channel, reason in sorted(unavailable_otp_channels(settings).items()):
            print(f"[auth] {channel} sign-in is unavailable: {reason}", flush=True)
        return
    for problem in development_auth_shortcuts(settings):
        print(f"[auth] development mode: {problem}", flush=True)
