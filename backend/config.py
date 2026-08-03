"""Application settings and the on-disk layout they imply."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret configuration, overridable by `LYRA_`-prefixed environment variables.

    The tutor API key is deliberately absent: it lives in the OS keychain, never here.
    """

    model_config = SettingsConfigDict(env_prefix="LYRA_", extra="ignore")

    data_dir: Path = Path("data")
    db_path: Path = Path("data/lyra.db")
    llama_port: int = 8081
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def text_dir(self) -> Path:
        return self.data_dir / "text"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def llama_dir(self) -> Path:
        """Directory the llama.cpp release archive is extracted into."""
        return self.models_dir / "llama"

    @property
    def embedding_model_path(self) -> Path:
        return self.models_dir / "nomic-embed-text-v1.5.Q8_0.gguf"

    def ensure_directories(self) -> None:
        """Create the data directories. Called once on startup."""
        for directory in (self.data_dir, self.uploads_dir, self.text_dir, self.models_dir):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
