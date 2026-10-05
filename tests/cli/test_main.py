"""Entry point: config from --home / EVENTTRADE_HOME, exit codes for the cheap paths."""

from pathlib import Path

import pytest

from eventtrade.cli import main as cli


def test_requires_home(monkeypatch, capsys):
    monkeypatch.delenv("EVENTTRADE_HOME", raising=False)
    assert cli.main(["status"]) == 2
    assert "EVENTTRADE_HOME" in capsys.readouterr().err


def test_env_home_and_single_key(monkeypatch, tmp_path: Path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "only.json").write_text("{}")
    monkeypatch.setenv("EVENTTRADE_HOME", str(tmp_path))
    cfg = cli._config(cli._parser().parse_args(["status"]))
    assert cfg.home == tmp_path and cfg.gcp_key_path.name == "only.json"
    assert cfg.db_path == tmp_path / "data" / "eventtrade.sqlite"


def test_status_before_init_is_missing(tmp_path: Path):
    assert cli.main(["--home", str(tmp_path), "status"]) == 4


def test_init_then_status(tmp_path: Path, capsys):
    assert cli.main(["--home", str(tmp_path), "init-db"]) == 0
    assert cli.main(["--home", str(tmp_path), "status"]) == 0
    assert "gdelt_daily" in capsys.readouterr().out


def test_holdout_without_flag_is_refused(tmp_path: Path):
    cli.main(["--home", str(tmp_path), "init-db"])
    assert cli.main(["--home", str(tmp_path), "holdout", "--track", "A", "--trial-id", "x"]) == 2


def test_ingest_prices_into_holdout_is_refused(tmp_path: Path):
    cli.main(["--home", str(tmp_path), "init-db"])
    rc = cli.main(["--home", str(tmp_path), "ingest-prices", "--tickers", "SPY",
                   "--start", "2025-03-01", "--end", "2025-04-01"])
    assert rc == 2


@pytest.mark.parametrize("argv", [["backtest", "--track", "A"], ["nope"],
                                  ["ingest-gdelt", "events", "--start", "2024-13-01"]])
def test_usage_errors_exit_2(tmp_path: Path, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["--home", str(tmp_path), *argv])
    assert e.value.code == 2
