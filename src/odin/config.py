"""Runtime configuration. Override any value via an env var (prefix ODIN_) or a .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ODIN_", env_file=".env", extra="ignore")

    # Postgres. Empty host => local unix socket as the current OS user.
    database_url: str = "postgresql:///odin"

    # On-disk "staging file area" — where landed batch files live before load.
    staging_file_area: Path = _PROJECT_ROOT / "staging_files"

    # Where the web UI drops uploaded files before handing them to the extractor.
    upload_dir: Path = _PROJECT_ROOT / "web_uploads"

    # Directory of ordered *.sql migration files.
    sql_dir: Path = _PROJECT_ROOT / "sql"

    # Extraction / load batching.
    batch_rows: int = 100_000

    # Transform chunking (rows read from staging per chunk).
    chunk_rows: int = 100_000

    # Structural cast: max length of any text field that could reach an indexed
    # production column. Longer => quarantined per row, before the bulk write.
    text_cap: int = 256

    # SQL Console (web /sql) guard rails.
    sql_timeout_seconds: int = 30     # statement_timeout for every console statement
    sql_row_cap: int = 1000          # default max rows rendered; "Load all" lifts it
    sql_row_hard_cap: int = 100_000  # ceiling even for "Load all"

    def ensure_dirs(self) -> None:
        self.staging_file_area.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
