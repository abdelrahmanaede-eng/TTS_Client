from __future__ import annotations

from time_tracker.core.remote_segments import build_continuous_remote_segments


def test_application_change_closes_segment() -> None:
    records = [
        (1000, "chrome.exe", "input", None, "123", "25", "ERP"),
        (1100, "code.exe", "input", None, "123", "25", "ERP"),
    ]
    segments = build_continuous_remote_segments(records, grace_seconds=180)
    assert [(segment.application, segment.start_epoch, segment.end_epoch) for segment in segments] == [
        ("chrome.exe", 1000, 1100),
        ("code.exe", 1100, 1280),
    ]


def test_idle_gap_and_project_change_close_segments() -> None:
    records = [
        (1000, "chrome.exe", "input", None, "123", "25", "ERP"),
        (1300, "chrome.exe", "input", None, "123", "25", "ERP"),
        (1400, "chrome.exe", "input", None, "123", "26", "Other"),
    ]
    segments = build_continuous_remote_segments(records, grace_seconds=180)
    assert len(segments) == 3
    assert segments[0].project_id == "25"
    assert segments[1].project_id == "25"
    assert segments[2].project_id == "26"


def test_session_change_never_merges_segments() -> None:
    records = [
        (1000, "chrome.exe", "input", None, "123", "25", "ERP"),
        (1100, "chrome.exe", "input", None, "124", "25", "ERP"),
    ]
    segments = build_continuous_remote_segments(records, grace_seconds=180)
    assert [segment.attendance_id for segment in segments] == ["123", "124"]
