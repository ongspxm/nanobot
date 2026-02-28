from typer.testing import CliRunner

from nanobot.cli.commands import app

runner = CliRunner()


def test_cron_add_rejects_invalid_timezone(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("nanobot.config.loader.get_data_dir", lambda: tmp_path)

    result = runner.invoke(
        app,
        [
            "cron",
            "add",
            "--name",
            "demo",
            "--message",
            "hello",
            "--cron",
            "0 9 * * *",
            "--tz",
            "America/Vancovuer",
        ],
    )

    assert result.exit_code == 1
    assert "Error: unknown timezone 'America/Vancovuer'" in result.stdout
    assert not (tmp_path / "cron" / "jobs.json").exists()


def test_cron_list_truncates_long_job_names(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("nanobot.config.loader.get_data_dir", lambda: tmp_path)

    long_name = "a" * 90
    add_result = runner.invoke(
        app,
        [
            "cron",
            "add",
            "--name",
            long_name,
            "--message",
            "hello",
            "--every",
            "60",
        ],
    )
    assert add_result.exit_code == 0

    list_result = runner.invoke(app, ["cron", "list"])
    assert list_result.exit_code == 0
    assert f"{'a' * 77}..." in list_result.stdout
