"""Fail-closed version and Windows reparse-point contracts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_orchestrators import compatibility
from codex_orchestrators.policies import PolicyError
from codex_orchestrators.processes import ProcessResult
from codex_orchestrators.workspace import is_linklike


@pytest.mark.parametrize("text,code", [("codex-cli 0.1.0", 0), ("unknown", 0), ("codex-cli 0.153.4", 1)])
def test_unknown_cli_fails_before_agent(text, code, monkeypatch, tmp_path):
    monkeypatch.setattr(compatibility, "run_process", Mock(return_value=ProcessResult(text, "", code, 0.1)))
    with pytest.raises(PolicyError, match="Unsupported"):
        compatibility.check_version("fictional-codex", tmp_path)


def test_inspected_version_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(
        compatibility, "run_process", Mock(return_value=ProcessResult("codex-cli 0.153.4\n", "", 0, 0.1))
    )
    assert compatibility.check_version("fictional-codex", tmp_path) == "0.153.4"


def test_windows_reparse_point_without_new_path_method():
    path = SimpleNamespace(lstat=lambda: SimpleNamespace(st_file_attributes=0x400), is_symlink=lambda: False)
    assert is_linklike(path)
