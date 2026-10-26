from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_portfolio_review_documentation_has_reproducible_evidence_links():
    """Keep public-review claims linked to local source evidence."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    for heading in (
        "## Run",
        "## API and OpenAPI",
        "## Evidence and portfolio review",
        "### Security threats and limitations",
        "### Verified CV bullets",
    ):
        assert heading in readme

    for evidence_path in (
        "tests/test_auth.py",
        "tests/test_core.py",
        "tests/e2e/collabdesk.spec.js",
        "docs/task-reports/DJ-011.md",
        "docs/task-reports/DJ-012.md",
    ):
        assert evidence_path in readme
        assert (ROOT / evidence_path).is_file()

    benchmark_report = (ROOT / "docs/task-reports/DJ-012.md").read_text(encoding="utf-8")
    for raw_result in (
        "DJ-012-raw-10000-postgres.json",
        "DJ-012-raw-50000-postgres.json",
        "DJ-012-raw-100000-postgres.json",
    ):
        assert raw_result in benchmark_report
        assert (ROOT / "docs/task-reports" / raw_result).is_file()

    assert "/api/schema/" in readme
    assert "Idempotency-Key" in readme
    assert "```mermaid" in architecture
    assert "PostgreSQL is the source of truth" in architecture

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env.*" in gitignore
    assert "!.env.example" in gitignore
    assert "*.sqlite3" in gitignore
