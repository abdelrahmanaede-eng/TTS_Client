"""Pure helpers for candidate Remote Work activity Segments.

This module has no network behavior and does not define a server API contract.
It only converts existing local activity evidence into immutable local candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence


@dataclass(frozen=True)
class RemoteActivitySegment:
    attendance_id: str
    project_id: str | None
    project_name: str | None
    application: str | None
    start_epoch: int
    end_epoch: int

    @property
    def start_utc(self) -> str:
        return datetime.fromtimestamp(self.start_epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @property
    def end_utc(self) -> str:
        return datetime.fromtimestamp(self.end_epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_continuous_remote_segments(
    records: Iterable[Sequence[object]],
    *,
    grace_seconds: int = 180,
) -> list[RemoteActivitySegment]:
    """Build candidate Segments without crossing remote or application boundaries."""
    normalized: list[tuple[int, str, str, str | None, str | None, str | None]] = []
    for record in sorted(records, key=lambda item: int(item[0])):
        if len(record) < 7:
            continue
        ts = int(record[0])
        app = str(record[1] or "Unknown")
        attendance_id = str(record[4]) if record[4] is not None else ""
        project_id = str(record[5]) if record[5] is not None else None
        project_name = str(record[6]) if record[6] is not None else None
        if not attendance_id:
            continue
        normalized.append((ts, app, attendance_id, project_id, project_name, str(record[2])))

    if not normalized:
        return []

    grace = max(0, int(grace_seconds))
    segments: list[RemoteActivitySegment] = []
    start_ts = normalized[0][0]
    last_ts = normalized[0][0]
    app = normalized[0][1]
    attendance_id = normalized[0][2]
    project_id = normalized[0][3]
    project_name = normalized[0][4]

    for ts, next_app, next_attendance_id, next_project_id, next_project_name, _source in normalized[1:]:
        gap = ts - last_ts
        boundary = (
            next_app != app
            or next_attendance_id != attendance_id
            or next_project_id != project_id
            or gap > grace
        )
        if boundary:
            end_ts = min(last_ts + grace, ts)
            if end_ts > start_ts:
                segments.append(
                    RemoteActivitySegment(
                        attendance_id=attendance_id,
                        project_id=project_id,
                        project_name=project_name,
                        application=app,
                        start_epoch=start_ts,
                        end_epoch=end_ts,
                    )
                )
            start_ts = ts
            last_ts = ts
            app = next_app
            attendance_id = next_attendance_id
            project_id = next_project_id
            project_name = next_project_name
        else:
            last_ts = ts

    end_ts = last_ts + grace
    if end_ts > start_ts:
        segments.append(
            RemoteActivitySegment(
                attendance_id=attendance_id,
                project_id=project_id,
                project_name=project_name,
                application=app,
                start_epoch=start_ts,
                end_epoch=end_ts,
            )
        )
    return segments
