"""Central configuration. Every tunable default lives here and can be overridden by env vars.

Model names are deliberately *only* defined here (and mirrored in ``.env.example``) because
Gemini model availability changes over time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["auto", "gemini", "stub"]
LLMCacheMode = Literal["live", "replay", "off"]
MCPTransport = Literal["http", "inprocess"]
AgentMode = Literal["multi", "single"]


class Settings(BaseSettings):
    """Runtime settings, loaded from the environment and an optional ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- paths -----------------------------------------------------------------------------
    data_dir: Path = Field(default=Path("data"), description="All mutable state lives here.")
    database_url: str | None = Field(
        default=None, description="SQLAlchemy URL. Defaults to sqlite in data_dir."
    )

    # --- LLM ---------------------------------------------------------------------------------
    gemini_api_key: str | None = Field(default=None, description="Google AI Studio key.")
    llm_provider: LLMProvider = Field(
        default="auto",
        description="'auto' uses Gemini when a key is present, otherwise the offline stub.",
    )
    llm_model_triage: str = Field(
        default="gemini-3.5-flash-lite", description="Cheapest model; used for triage."
    )
    llm_model_planner: str = Field(
        default="gemini-3.5-flash", description="Stronger model; planning + verification."
    )
    llm_rpm_triage: int = Field(default=5, ge=1, description="Requests per minute (triage).")
    llm_rpm_planner: int = Field(default=5, ge=1, description="Requests per minute (planner).")
    llm_rpd_triage: int = Field(default=800, ge=1, description="Daily request budget (triage).")
    llm_rpd_planner: int = Field(default=200, ge=1, description="Daily request budget (planner).")
    llm_cache_mode: LLMCacheMode = Field(
        default="live", description="live = cache then network; replay = cache only; off."
    )
    llm_max_retries: int = Field(default=5, ge=0)
    llm_timeout_s: float = Field(default=60.0, gt=0)
    llm_temperature: float = Field(default=0.1, ge=0, le=2)
    llm_thinking_budget: int | None = Field(
        default=None,
        description="Gemini thinking budget (0 disables thinking). None = provider default.",
    )

    # --- MCP ---------------------------------------------------------------------------------
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    mcp_transport: MCPTransport = Field(
        default="inprocess",
        description="How agents reach the MCP server. `make run` sets http.",
    )

    # --- web ---------------------------------------------------------------------------------
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # --- simulator ---------------------------------------------------------------------------
    sim_seed: int = 42
    sim_fleet_size: int = Field(default=25, ge=3, le=200)
    sim_hours: float = Field(default=48.0, gt=0, description="Simulated hours per run.")
    sim_speed: float = Field(
        default=120.0,
        ge=0,
        description="Simulated seconds per real second when streaming. 0 = no delay.",
    )
    sim_autostart: bool = Field(default=True, description="Start streaming on web boot.")

    # --- agents ------------------------------------------------------------------------------
    agent_mode: AgentMode = "multi"
    rag_enabled: bool = True
    memory_enabled: bool = True
    cluster_window_min: int = Field(default=30, ge=1, description="Event cluster window.")

    # --- policy ------------------------------------------------------------------------------
    rl_message_driver_per_driver_per_hour: int = 3
    rl_message_driver_fleet_per_hour: int = 30
    rl_work_orders_per_hour: int = 8
    rl_flags_per_hour: int = 20
    rl_reassignments_per_hour: int = 8

    # --- misc --------------------------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False

    # --- derived -----------------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "haulmind.db"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/mcp"

    @property
    def llm_cache_dir(self) -> Path:
        return self.data_dir / "llm_cache"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def faiss_dir(self) -> Path:
        return self.data_dir / "faiss"

    @property
    def quota_file(self) -> Path:
        return self.data_dir / "quota.json"

    @property
    def effective_llm_provider(self) -> Literal["gemini", "stub"]:
        if self.llm_provider == "auto":
            return "gemini" if self.gemini_api_key else "stub"
        return self.llm_provider

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.llm_cache_dir, self.runs_dir, self.faiss_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Testing hook: forget the cached settings so env changes take effect."""
    get_settings.cache_clear()
