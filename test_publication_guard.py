"""Publication gates use fictional data and disposable Git repositories."""

import subprocess

import pytest

from tools.check_public_tree import MAX_BYTES, check_tree, inspect_file


@pytest.mark.parametrize("name", ["logs/run/session.json", ".env", "backup.bundle",
                                  "nested/secret.key", "update.md", "build/app.apk"])
def test_forbidden_artifact_paths(name):
    assert "private-artifact-path" in inspect_file(name, b"fictional")


def test_safe_example_and_source_are_allowed():
    assert not inspect_file(".env.example", b"PROVIDER_KEY=replace-me")
    assert not inspect_file("examples/fictional_answer.md", b"Example answer.")


def test_secret_categories_do_not_expose_values():
    fictional = b"gh" + b"p_" + b"x" * 36
    findings = inspect_file("config.txt", fictional)
    assert findings == ["github-token"]
    assert fictional.decode() not in str(findings)


def test_binary_size_and_symlink_modes_fail_closed():
    assert "binary-requires-review" in inspect_file("file.bin", b"\x00")
    assert "oversized-file" in inspect_file("file.txt", b"a" * (MAX_BYTES + 1))
    assert "unsupported-file-mode" in inspect_file("link", b"target", "120000")


def test_canonical_redactor_masks_constructed_fixture():
    from congress2 import _redact_sensitive_text

    synthetic_key = "sk-" + "fictional-redaction-test-" + "0" * 20
    result = _redact_sensitive_text(f"OPENAI_API_KEY={synthetic_key} password=plain-secret")
    assert synthetic_key not in result
    assert "plain-secret" not in result


def run_git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, timeout=30)


def test_staged_blob_is_checked_even_if_worktree_is_cleaned(tmp_path):
    run_git(tmp_path, "init")
    sample = tmp_path / "config.txt"
    sample.write_bytes(b"gh" + b"p_" + b"x" * 36)
    run_git(tmp_path, "add", "config.txt")
    sample.write_text("safe replacement", encoding="utf-8")
    assert check_tree(tmp_path, staged=False) == []
    assert check_tree(tmp_path, staged=True) == [("config.txt", "github-token")]


def test_staged_deletion_removes_private_artifact(tmp_path):
    run_git(tmp_path, "init")
    sample = tmp_path / "session.json"
    sample.write_text("fictional", encoding="utf-8")
    run_git(tmp_path, "add", "session.json")
    assert check_tree(tmp_path, staged=True)
    run_git(tmp_path, "rm", "--cached", "session.json")
    assert check_tree(tmp_path, staged=True) == []
    assert sample.exists()
