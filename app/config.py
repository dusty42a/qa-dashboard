from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    redmine_base: str = "https://dev.workbooks.com"
    redmine_project: str = "workbooks"
    redmine_key: str = ""
    redmine_verify_ssl: bool = False

    anthropic_api_key: str | None = None
    risk_model: str = "claude-haiku-4-5-20251001"
    risk_enabled: bool = True
    # Cost guardrails: comma-separated list of version names to score. Empty
    # means score everything (production setting). During dev, restrict to one
    # release to keep the Anthropic bill bounded.
    risk_versions: str = ""
    # Hard cap on commits scored per refresh cycle, regardless of how many new
    # ones appeared. Stops a backlog (or a misconfig) from spending hundreds
    # of dollars on a single page load.
    risk_max_per_cycle: int = 20

    svn_binary: str = ""
    svn_repo_url: str = ""

    upstream_ttl_seconds: int = 600
    diff_line_cap: int = 2000

    data_dir: Path = Field(default=Path("/data"))

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "qa.sqlite"

    @property
    def risk_versions_set(self) -> set[str]:
        """Allowlist of version names; empty set means 'no restriction'."""
        return {s.strip() for s in self.risk_versions.split(",") if s.strip()}


settings = Settings()
