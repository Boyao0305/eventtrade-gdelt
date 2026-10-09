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
                                  ["ingest-gdelt", "events", "--start", "2024-13-01"],
                                  ["ingest-gdelt", "events", "--budget-gb", "0"],
                                  ["ingest-gdelt", "events", "--budget-gb", "4601"],
                                  ["ingest-gdelt", "events", "--budget-gb", "1.5"]])
def test_usage_errors_exit_2(tmp_path: Path, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["--home", str(tmp_path), *argv])
    assert e.value.code == 2


def test_screen_without_screen_groups_is_missing(tmp_path: Path):
    # ADR 0003: the screen needs Asset.screen_group, which only a universe reload fills.
    cli.main(["--home", str(tmp_path), "init-db"])
    assert cli.main(["--home", str(tmp_path), "screen", "--track", "A"]) == 4
    assert cli.main(["--home", str(tmp_path), "screen", "--track", "B", "--n-boot", "999"]) == 4


def test_screen_a2_without_theme_pull_is_missing(tmp_path: Path, capsys):
    # ADR 0004 "Run condition": no gkg_themes_daily month stored -> refused with exit 4.
    cli.main(["--home", str(tmp_path), "init-db"])
    assert cli.main(["--home", str(tmp_path), "screen", "--track", "A", "--set", "a2"]) == 4
    assert "gkg_themes_daily" in capsys.readouterr().out


def test_screen_a2_on_track_b_is_refused(tmp_path: Path):
    cli.main(["--home", str(tmp_path), "init-db"])
    assert cli.main(["--home", str(tmp_path), "screen", "--track", "B", "--set", "a2"]) == 2


def test_screen_set_defaults_to_s3():
    args = cli._parser().parse_args(["screen", "--track", "A"])
    assert args.screen_set == "s3"


# n_boot >= 999 (ADR 0003 implementation notes): fewer draws is a usage error, exit 2.
@pytest.mark.parametrize("argv", [["screen"], ["screen", "--track", "C"],
                                  ["screen", "--track", "A", "--n-boot", "0"],
                                  ["screen", "--track", "A", "--n-boot", "998"],
                                  ["screen", "--track", "A", "--n-boot", "x"],
                                  ["screen", "--track", "A", "--set", "a3"]])
def test_screen_usage_errors_exit_2(tmp_path: Path, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["--home", str(tmp_path), *argv])
    assert e.value.code == 2


def test_budget_gb_overrides_monthly_budget_for_ingest_only(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_gdelt(cfg, store):
        seen["budget"] = cfg.bq_monthly_budget_bytes
        return object()

    def fake_ingest(cfg, store, gdelt, *rest):
        seen["cfg_budget"] = cfg.bq_monthly_budget_bytes
        return 0

    monkeypatch.setattr(cli, "_gdelt", fake_gdelt)
    monkeypatch.setattr(cli.pipelines, "ingest_gdelt", fake_ingest)
    cli.main(["--home", str(tmp_path), "init-db"])
    assert cli.main(["--home", str(tmp_path), "ingest-gdelt", "gkg-themes",
                     "--budget-gb", "1600"]) == 0
    assert seen == {"budget": 1600 * 10**9, "cfg_budget": 1600 * 10**9}
    assert cli.main(["--home", str(tmp_path), "ingest-gdelt", "gkg-themes"]) == 0
    assert seen["budget"] == 900 * 10**9  # default unchanged without the flag


@pytest.mark.parametrize("argv", [["ingest-gdelt-15min"], ["ingest-gdelt-15min", "gkg-orgs"],
                                  ["ingest-gdelt-15min", "events", "--start", "2016-02-30"],
                                  ["ingest-gdelt-15min", "orgs", "--budget-gb", "0"],
                                  ["ingest-gdelt-15min", "orgs", "--budget-gb", "4601"]])
def test_ingest_gdelt_15min_usage_errors_exit_2(tmp_path: Path, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["--home", str(tmp_path), *argv])
    assert e.value.code == 2


def test_ingest_gdelt_15min_wiring_and_budget_override(tmp_path: Path, monkeypatch):
    from datetime import date

    from eventtrade.cli import pipelines_intraday

    seen = {}

    def fake_gdelt(cfg, store):
        seen["budget"] = cfg.bq_monthly_budget_bytes
        return "gdelt"

    def fake_ingest(cfg, store, istore, gdelt, dataset, start, end, execute, now, err,
                    cameo_fips=None):
        seen.update(gdelt=gdelt, dataset=dataset, start=start, execute=execute,
                    istore=istore.path.name, err=err.__name__, n_cameo=len(cameo_fips))
        return 0

    monkeypatch.setattr(cli, "_gdelt", fake_gdelt)
    monkeypatch.setattr(pipelines_intraday, "ingest_gdelt_15min", fake_ingest)
    cli.main(["--home", str(tmp_path), "init-db"])
    assert cli.main(["--home", str(tmp_path), "ingest-gdelt-15min", "orgs", "--budget-gb", "1200",
                     "--start", "2016-01-01"]) == 0
    assert seen == {"budget": 1200 * 10**9, "gdelt": "gdelt", "dataset": "orgs",
                    "start": date(2016, 1, 1), "execute": False, "istore": "intraday.sqlite",
                    "err": "BudgetExceeded", "n_cameo": 54}
    assert cli.main(["--home", str(tmp_path), "ingest-gdelt-15min", "events", "--execute"]) == 0
    assert seen["budget"] == 900 * 10**9 and seen["dataset"] == "events" and seen["execute"]
    assert cli.main(["--home", str(tmp_path), "ingest-gdelt-15min", "events_cat"]) == 0
    assert seen["dataset"] == "events_cat" and not seen["execute"] and seen["n_cameo"] == 54
