"""Validate distributable metadata, release notes, and synthetic fixtures."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from custom_components.kepco_on.const import DOMAIN, VERSION
from packaging.version import Version
from tools.release_notes import extract_release_notes

ROOT = Path(__file__).resolve().parents[1]

WORKFLOWS = ROOT / ".github" / "workflows"

ALLOWED_FIXTURES = {
    "bill_202607.json",
    "house_customer_list.json",
    "house_main_chart.json",
    "house_bill_detail.json",
    "house_power_planner.json",
    "bill_latest.json",
    "customer_list_multiple.json",
    "customer_list_single.json",
}

SENSITIVE_FILE_SUFFIXES = (".har", ".jsonl", ".trace.zip")

SENSITIVE_FILE_NAMES = {"secrets.yaml"}

RAW_ARTIFACT_PREFIXES = ("session", "cookies", "login-schema")

FORBIDDEN_KEYS = {
    "access_token",
    "address",
    "api_body",
    "authorization",
    "body",
    "cookie",
    "cookies",
    "email",
    "headers",
    "member_name",
    "name",
    "password",
    "phone",
    "pwdval",
    "raw",
    "refresh_token",
    "token",
}

FORBIDDEN_KEY_PARTS = (
    "address",
    "authorization",
    "cookie",
    "email",
    "header",
    "name",
    "nm",
    "password",
    "phone",
    "pwd",
    "raw",
    "token",
)

FORBIDDEN_VALUE_PARTS = (
    "bearer ",
    "cookie:",
    "eyj",
    "password",
    "secret_canary",
    "set-cookie",
)

SAFE_SYNTHETIC_FIXTURE_KEYS = {"apt_name", "apt_nm", "cust_no", "si_cust_no"}

SAFE_DATE_FIXTURE_KEYS = {"dc_user_chg_nm_ymd"}


def load_yaml(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def load_json(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def workflow_on(workflow: dict[str, Any]) -> dict[str, Any]:
    raw_workflow = cast("dict[Any, Any]", workflow)
    on_value = raw_workflow["on"] if "on" in raw_workflow else raw_workflow[True]
    return cast("dict[str, Any]", on_value)


def tracked_files() -> list[str]:
    paths = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return [path for path in paths if (ROOT / path).is_file()]


def assert_fixture_value_is_safe(key: str, value: object) -> None:
    normalized_key = key.lower()
    if normalized_key in SAFE_SYNTHETIC_FIXTURE_KEYS:
        assert isinstance(value, str)
        assert value.startswith("TEST_")
        return
    if not isinstance(value, str):
        return
    lowered = value.lower()
    assert "@" not in value
    assert not any(part in lowered for part in FORBIDDEN_VALUE_PARTS)
    assert not any(character.isdigit() for character in value if "phone" in normalized_key)


def scan_fixture(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).replace("-", "_").lower()
            if normalized_key in SAFE_DATE_FIXTURE_KEYS:
                assert isinstance(item, str)
                assert re.fullmatch(r"[0-9]{8}", item)
                continue
            if normalized_key not in SAFE_SYNTHETIC_FIXTURE_KEYS:
                assert normalized_key not in FORBIDDEN_KEYS
                assert not any(part in normalized_key for part in FORBIDDEN_KEY_PARTS)
            scan_fixture(item, (*path, str(key)))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            scan_fixture(item, (*path, str(index)))
        return
    if path:
        assert_fixture_value_is_safe(path[-1], value)


def test_repository_tracks_no_raw_capture_or_secret_artifacts() -> None:
    for relative in tracked_files():
        normalized = relative.lower()
        path = Path(relative)
        assert path.name.lower() not in SENSITIVE_FILE_NAMES
        assert not normalized.endswith(SENSITIVE_FILE_SUFFIXES)
        if path.parts[:2] == ("tests", "fixtures"):
            assert path.name in ALLOWED_FIXTURES
            continue
        if path.suffix in {".json", ".zip"}:
            assert not any(path.name.lower().startswith(prefix) for prefix in RAW_ARTIFACT_PREFIXES)


def test_committed_json_fixtures_are_sanitized_and_allowlisted() -> None:
    fixture_paths = list((ROOT / "tests/fixtures").glob("*.json"))

    assert {path.name for path in fixture_paths} == ALLOWED_FIXTURES
    for path in fixture_paths:
        scan_fixture(load_json(path))


def test_package_versions_and_serialized_metadata_are_valid() -> None:
    manifest = load_json(ROOT / "custom_components/kepco_on/manifest.json")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert VERSION == manifest["version"] == pyproject["project"]["version"]
    assert not Version(VERSION).is_prerelease
    assert manifest["domain"] == DOMAIN
    assert manifest["requirements"] == []
    assert manifest["config_flow"] is True
    for path in (ROOT / "custom_components/kepco_on").rglob("*.json"):
        assert isinstance(load_json(path), dict)
    assert load_json(ROOT / "hacs.json")["homeassistant"] == "2026.8.3"
    assert load_json(ROOT / "package.json")["private"] is True
    services = load_yaml(ROOT / "custom_components/kepco_on/services.yaml")
    assert set(services) == {"get_monthly_bill", "get_usage_history"}


def test_workflows_keep_pinned_actions_and_release_only_validated_main() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        workflow = load_yaml(path)
        assert isinstance(workflow_on(workflow), dict)
        assert "continue-on-error" not in json.dumps(workflow)
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
    release = load_yaml(WORKFLOWS / "release.yml")
    assert set(workflow_on(release)) == {"workflow_dispatch"}
    assert release["jobs"]["release"]["if"] == "github.ref == 'refs/heads/main'"
    assert release["jobs"]["release"]["env"]["RELEASE_SHA"] == "${{ github.sha }}"
    source = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert 'for name in ("Tests", "Validate")' in source
    assert "${RELEASE_SHA}:custom_components/kepco_on" in source
    assert "--notes-file .local/release-notes.md" in source


def test_release_notes_select_current_version_without_unreleased_changes() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = extract_release_notes(changelog, VERSION)
    assert notes.startswith(f"## {VERSION}")
    assert "\n- " in notes
    assert "미배포" not in notes
    assert "\n## " not in notes
    with pytest.raises(ValueError, match="Missing changelog section"):
        extract_release_notes(changelog, "999.0.0")
