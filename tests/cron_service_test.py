import pytest

from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


def test_add_job_rejects_unknown_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="unknown timezone 'America/Vancovuer'"):
        service.add_job(
            name="tz typo",
            schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancovuer"),
            message="hello",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_add_job_accepts_valid_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="tz ok",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancouver"),
        message="hello",
    )

    assert job.schedule.tz == "America/Vancouver"
    assert job.state.next_run_at_ms is not None


def test_edit_job_can_update_name_without_touching_message(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="original name",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="original message",
    )

    updated = service.edit_job(job_id=job.id, name="new name")

    assert updated is not None
    assert updated.name == "new name"
    assert updated.payload.message == "original message"


def test_edit_job_can_update_message_without_touching_name(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="stable name",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="old message",
    )

    updated = service.edit_job(job_id=job.id, message="new message")

    assert updated is not None
    assert updated.name == "stable name"
    assert updated.payload.message == "new message"
