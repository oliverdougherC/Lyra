"""Application settings and the on-disk layout they imply."""

import sys
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend import desktop_paths
from backend.storage import private


class Settings(BaseSettings):
    """Non-secret configuration, overridable by `LYRA_`-prefixed environment variables.

    The tutor API key is deliberately absent: it lives in the OS keychain, never here.
    """

    model_config = SettingsConfigDict(env_prefix="LYRA_", extra="ignore", populate_by_name=True)

    packaged_mode: bool = Field(default=False, validation_alias="LYRA_PACKAGED")
    data_dir: Path = Path("data")
    # None means "derive from data_dir". Setting LYRA_DATA_DIR alone used to relocate
    # uploads, pages, text, and models while the database stayed at data/lyra.db - a split
    # that once wrote 596 chunks into the real database during a verification run. The
    # database now follows data_dir unless LYRA_DB_PATH points it somewhere explicitly.
    db_path: Path | None = None
    cache_dir: Path | None = None
    logs_dir: Path | None = None
    resource_root: Path | None = None
    models_dir_override: Path | None = Field(default=None, validation_alias="LYRA_MODELS_DIR")
    source_data_dir: Path | None = None
    source_db_path: Path | None = None
    llama_port: int = 8081
    host: str = "127.0.0.1"
    port: int = 8000

    @model_validator(mode="after")
    def _derive_paths(self) -> "Settings":
        if "packaged_mode" not in self.model_fields_set and getattr(sys, "frozen", False):
            self.packaged_mode = True
        if self.packaged_mode:
            if "data_dir" not in self.model_fields_set:
                self.data_dir = desktop_paths.platform_application_support_dir()
            if self.cache_dir is None:
                self.cache_dir = desktop_paths.platform_cache_dir()
            if self.logs_dir is None:
                self.logs_dir = desktop_paths.platform_logs_dir()
        else:
            if self.logs_dir is None:
                self.logs_dir = Path("logs")
        if self.resource_root is None:
            self.resource_root = desktop_paths.default_resource_root()
        if self.db_path is None:
            self.db_path = self.data_dir / "lyra.db"
        return self

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def text_dir(self) -> Path:
        return self.data_dir / "text"

    @property
    def pages_dir(self) -> Path:
        """Rendered source pages, cached. Disposable: deleting it costs one re-render."""
        cache_dir = self.cache_dir or self.data_dir
        return cache_dir / "pages"

    @property
    def models_dir(self) -> Path:
        return self.models_dir_override or (self.data_dir / "models")

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

    @property
    def _hardened_marker(self) -> Path:
        """Names the one-time permissions upgrade, so the tree is walked only once."""
        return self.data_dir / ".permissions-hardened"

    def _root_marker(self, root: Path) -> Path:
        if root == self.data_dir:
            return self._hardened_marker
        return root / ".permissions-hardened"

    def ensure_directories(self) -> None:
        """Create the data directories, private to the user, once on startup.

        Every Lyra-owned directory is `0o700` (see `storage.private`), so no other user
        can read the coursework, derived caches, database, or fallback key beneath it, and
        this holds whatever umask the process inherited. The top-level directories are
        re-hardened every startup; the deeper tree is walked once, to bring an installation
        created before this contract up to it without re-walking on every launch.

        The data directory must be a real directory. A symlinked `LYRA_DATA_DIR` is refused
        here rather than followed, so Lyra never recursively hardens, walks, or writes into
        whatever the link points at. The directory's own ancestors may be symlinks - that is
        the user's home layout, not Lyra's to police - and are left alone.
        """
        private.assert_not_symlink(self.data_dir, "LYRA_DATA_DIR")
        cache_dir = self.cache_dir or self.data_dir
        logs_dir = self.logs_dir
        if logs_dir is None:
            raise RuntimeError("LYRA_LOGS_DIR is not configured.")
        private.assert_not_symlink(cache_dir, "LYRA_CACHE_DIR")
        private.assert_not_symlink(logs_dir, "LYRA_LOGS_DIR")
        private.assert_not_symlink(self.models_dir, "LYRA_MODELS_DIR")

        for root in dict.fromkeys((self.data_dir, cache_dir, logs_dir, self.models_dir)):
            private.secure_mkdir(root, root=root)
            private.harden_dir(root)
        for directory, root in (
            (self.uploads_dir, self.data_dir),
            (self.text_dir, self.data_dir),
            (self.pages_dir, cache_dir),
            (self.models_dir, self.models_dir),
        ):
            private.secure_mkdir(directory, root=root)
            private.harden_dir(directory)
        self._harden_root_once(self.data_dir, keep_file_modes=(self.models_dir,))
        for root in dict.fromkeys((cache_dir, logs_dir, self.models_dir)):
            if root != self.data_dir:
                keep_modes = (self.models_dir,) if root == self.models_dir else ()
                self._harden_root_once(root, keep_file_modes=keep_modes)

    def _harden_root_once(self, root: Path, *, keep_file_modes: tuple[Path, ...] = ()) -> None:
        """Tighten a pre-existing data tree to the contract, at most once per installation.

        `models_dir` is kept out of the file pass: it holds a bundled executable, and a
        `0o600` file cannot be run. Its directory is still made private, and every other
        Lyra-owned file is brought to `0o600`.

        The sentinel is security-relevant state, so it is read and written with no-follow
        semantics: a symlink where the marker belongs is refused rather than trusted (it
        must not let a link claim the migration ran, and the write below must not follow it
        to overwrite an outside file).
        """
        marker = self._root_marker(root)
        if private.regular_file_present(marker):
            return
        private.harden_data_tree(root, keep_file_modes=keep_file_modes)
        private.write_private_bytes(marker, b"")


settings = Settings()
