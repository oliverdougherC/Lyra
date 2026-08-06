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
    def pages_dir(self) -> Path:
        """Rendered source pages, cached. Disposable: deleting it costs one re-render."""
        return self.data_dir / "pages"

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

    @property
    def rerank_model_path(self) -> Path:
        """The cross-encoder that reorders retrieval's over-fetch, if it was downloaded."""
        return self.models_dir / "bge-reranker-v2-m3-Q8_0.gguf"

    @property
    def rerank_installed(self) -> bool:
        """Whether reranking is available on this machine."""
        return self.rerank_model_path.exists()

    @property
    def ocr_model_path(self) -> Path:
        """The Unlimited-OCR language model. Absent unless the student asked for it."""
        return self.models_dir / "unlimited-ocr-Q4_K_M.gguf"

    @property
    def ocr_mmproj_path(self) -> Path:
        """Its multimodal projector. llama.cpp needs both files to load the model at all."""
        return self.models_dir / "mmproj-unlimited-ocr-bf16.gguf"

    @property
    def ocr_installed(self) -> bool:
        """Whether the specialist OCR path is available on this machine."""
        return self.ocr_model_path.exists() and self.ocr_mmproj_path.exists()

    def ensure_directories(self) -> None:
        """Create the data directories. Called once on startup."""
        for directory in (
            self.data_dir,
            self.uploads_dir,
            self.text_dir,
            self.pages_dir,
            self.models_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
