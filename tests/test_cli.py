# Copyright (c) 2026 Kostas Stathoulopoulos

"""Public command behaviour for the installed local interface."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from yimby.cli import _collection_window, main
from yimby.evidence import EvidenceStore
from yimby.store import SqliteStore

PILOT_AUTHORITY_COUNT = 15
ERROR_EXIT = 2
UNAVAILABLE_EXIT = 1
WINDOW_DAYS = 30


def test_collection_window_uses_inclusive_day_count() -> None:
    """Thirty requested days contain thirty dates, including the end date."""
    window = _collection_window(
        WINDOW_DAYS,
        include_open=True,
        end=date(2026, 9, 16),
    )
    assert window.start == date(2026, 8, 18)
    assert window.end == date(2026, 9, 16)
    assert (window.end - window.start).days + 1 == WINDOW_DAYS
    assert window.include_open


def _args(data_dir: Path, *command: str) -> list[str]:
    return ["--data-dir", str(data_dir), *command]


def test_cli_collection_inspection_normalisation_export_and_dashboard(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every read/collect/rebuild/export/dashboard command uses real state."""
    data = tmp_path / "data"
    assert main(_args(data, "authorities")) == 0
    authorities = json.loads(capsys.readouterr().out)
    assert len(authorities) == PILOT_AUTHORITY_COUNT
    assert authorities[0]["id"] == "barnet"

    assert (
        main(
            _args(
                data,
                "bootstrap",
                "--authority",
                "barnet",
                "--days",
                "5",
                "--include-open",
            )
        )
        == UNAVAILABLE_EXIT
    )
    unavailable = json.loads(capsys.readouterr().out)[0]
    assert unavailable["status"] == "unavailable"
    assert unavailable["failure_code"] == "LiveTransportUnavailable"

    assert (
        main(
            _args(
                data,
                "bootstrap",
                "--authority",
                "barnet",
                "--days",
                "5",
                "--include-open",
                "--fixture",
            )
        )
        == 0
    )
    bootstrap = json.loads(capsys.readouterr().out)
    application_id = bootstrap[0]["applications"][0]
    assert bootstrap[0]["attachment_body_requests"] == 0

    assert main(_args(data, "sync", "--authority", "barnet", "--fixture")) == 0
    assert json.loads(capsys.readouterr().out)[0]["authority_id"] == "barnet"

    assert main(_args(data, "inspect", application_id)) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["application"]["proposal"] == "Build two homes & plant four trees"
    assert main(_args(data, "inspect", "missing")) == ERROR_EXIT
    assert "missing" in capsys.readouterr().err

    assert main(_args(data, "normalise", "--rebuild")) == 0
    assert json.loads(capsys.readouterr().out) == {
        "rebuilt": 1,
        "transport_requests": 0,
    }

    public_path = tmp_path / "public.jsonl"
    assert (
        main(
            _args(
                data,
                "export",
                "--format",
                "jsonl",
                "--profile",
                "public",
                "--output",
                str(public_path),
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["exported"] == 1
    assert public_path.is_file()
    assert (
        main(
            _args(
                data,
                "export",
                "--format",
                "csv",
                "--profile",
                "research",
            )
        )
        == 0
    )
    default_export = json.loads(capsys.readouterr().out)["path"]
    assert Path(default_export).is_file()

    assert main(_args(data, "dashboard", "--json")) == 0
    dashboard = json.loads(capsys.readouterr().out)
    assert dashboard["snapshot"]["coverage_denominator"] == PILOT_AUTHORITY_COUNT
    assert "search" not in dashboard
    assert main(_args(data, "dashboard", "--json", "--search", "two homes")) == 0
    dashboard_search = json.loads(capsys.readouterr().out)
    assert dashboard_search["search"][0]["application_id"] == application_id

    assert main(_args(data, "doctor")) == 0
    assert all(check["ok"] for check in json.loads(capsys.readouterr().out)["checks"])


def test_cli_all_authorities_backup_restore_errors_and_default_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-authority selection and non-destructive backup paths are enforced."""
    data = tmp_path / "pilot"
    assert (
        main(
            _args(
                data,
                "bootstrap",
                "--authority",
                "all",
                "--days",
                "30",
                "--fixture",
            )
        )
        == 0
    )
    reports = json.loads(capsys.readouterr().out)
    assert len(reports) == PILOT_AUTHORITY_COUNT
    assert sum(item["attachment_body_requests"] for item in reports) == 0

    backup = tmp_path / "manual-backup"
    assert main(_args(data, "backup", "--output", str(backup))) == 0
    assert json.loads(capsys.readouterr().out)["backup"] == str(backup)
    assert main(_args(data, "backup", "--output", str(backup))) == ERROR_EXIT
    assert "manual-backup" in capsys.readouterr().err

    restored = tmp_path / "restored"
    assert (
        main(
            _args(
                data,
                "restore",
                str(backup),
                "--target",
                str(restored),
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["restored"] == str(restored)
    assert (
        main(
            _args(
                data,
                "restore",
                str(backup),
                "--target",
                str(restored),
            )
        )
        == ERROR_EXIT
    )
    assert "restored" in capsys.readouterr().err

    default_restore_data = tmp_path / "fresh-data"
    assert main(_args(default_restore_data, "restore", str(backup))) == 0
    default_restored = json.loads(capsys.readouterr().out)["restored"]
    assert default_restored == str(tmp_path / "fresh-data-restored")

    assert main(_args(data, "restore", str(tmp_path / "not-a-backup"))) == ERROR_EXIT
    assert "missing manifest" in capsys.readouterr().err

    assert main(_args(data, "backup")) == 0
    automatic_backup = Path(json.loads(capsys.readouterr().out)["backup"])
    assert automatic_backup.parent == data / "backups"

    monkeypatch.setattr("sys.argv", ["yimby", "--data-dir", str(data), "authorities"])
    assert main() == 0
    assert len(json.loads(capsys.readouterr().out)) == PILOT_AUTHORITY_COUNT

    with pytest.raises(SystemExit):
        main(
            _args(
                data,
                "bootstrap",
                "--authority",
                "barnet",
                "--days",
                "0",
                "--fixture",
            )
        )

    store = SqliteStore(
        data / "yimby.sqlite3",
        EvidenceStore(data / "evidence"),
    )
    evidence = next((data / "evidence").rglob("*.gz"))
    evidence.unlink()
    store.close()
    assert main(_args(data, "doctor")) == 1
    failed = json.loads(capsys.readouterr().out)
    assert any(
        check["name"] == "evidence" and not check["ok"] for check in failed["checks"]
    )
