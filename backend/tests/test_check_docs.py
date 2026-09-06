"""Keep documentation checks offline and catch real relative-path regressions."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_docs", Path(__file__).resolve().parents[2] / "scripts/check_docs.py"
)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def test_local_links_and_images_resolve_from_document(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "LICENSE").touch()
    (docs / "a file.png").touch()
    page = docs / "index.md"
    page.write_text(
        "[license](../LICENSE) ![image](<a%20file.png>) [anchor](#title) "
        "[remote](https://example.org/missing) [missing](missing.md#part)\n"
        "[ref]: /LICENSE\n[bad]: absent.md\n"
        "```md\n[example](not-a-real-file.md)\n```\n"
    )
    assert checker.broken_links(page, tmp_path) == [
        "docs/index.md: missing link destination missing.md#part",
        "docs/index.md: missing link destination absent.md",
    ]
