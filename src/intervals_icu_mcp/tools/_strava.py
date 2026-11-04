"""Source-restriction detection helpers (Strava, Garmin Connect).

Activities synced from Strava come back from the Intervals.icu API as a near-empty
stub due to Strava's November 2024 API policy: Intervals.icu cannot legally
redistribute Strava-sourced data through their API. This module detects that case
and produces a clear, actionable message so callers can surface it instead of
letting an LLM hallucinate metrics from empty fields.
"""

from ..client import ICUAPIError, ICUClient
from ..models import ActivitySummary

STRAVA_LIMITATION_NOTE = (
    "This activity was synced from Strava, which restricts redistribution of its "
    "activity data through third-party APIs (Strava policy change, November 2024). "
    "As a result, Intervals.icu cannot expose detailed metrics, streams, intervals, "
    "or histograms for Strava-sourced activities through its API — even though the "
    "activity may display correctly in the Intervals.icu web UI. "
    "To make this data available, connect your device directly to Intervals.icu "
    "(e.g. Garmin Connect, Wahoo, Zwift, COROS, Polar, or Suunto direct sync) "
    "instead of routing it through Strava."
)


GARMIN_LIMITATION_NOTE = (
    "This activity was synced from Garmin Connect, but the Intervals.icu API returned it "
    "without metrics (no duration, distance, power or heart rate). Intervals.icu has "
    "warned it may have to stop exposing Garmin-sourced data through its API; the "
    "activity can still look complete in the Intervals.icu web UI. Do not infer "
    "metrics for it. To keep data available to API clients, sync the device to "
    "Intervals.icu another way (file upload, or a direct integration such as Zwift / "
    "Wahoo / COROS) or check the activity in the web UI."
)
RESTRICTED_NOTE_TEMPLATE = (
    "The Intervals.icu API returned this activity without metrics{source}. It may be "
    "restricted by its data source. Do not infer metrics for it.{note}"
)
# The fields whose absence marks a redistribution-restricted stub. Activity-only
# fields (max_heartrate) are read with getattr so list summaries work too.
_METRIC_FIELDS = (
    "distance",
    "moving_time",
    "elapsed_time",
    "total_elevation_gain",
    "average_watts",
    "normalized_power",
    "average_heartrate",
    "max_heartrate",
    "average_cadence",
    "average_speed",
)


def _has_metrics(activity: ActivitySummary) -> bool:
    return any(getattr(activity, name, None) is not None for name in _METRIC_FIELDS)


def strava_limitation_note(activity: ActivitySummary) -> str | None:
    """Return a user-facing note when an activity is a Strava-restricted stub.

    Returns None for non-Strava activities and for Strava activities that still
    have metric data (so we don't false-positive on, e.g., a manually-edited
    Strava activity that happens to have populated fields).
    """
    if activity.source != "STRAVA" or _has_metrics(activity):
        return None
    return STRAVA_LIMITATION_NOTE


def garmin_limitation_note(activity: ActivitySummary) -> str | None:
    """Return a note when a Garmin Connect activity comes back without any metrics."""
    if activity.source != "GARMIN_CONNECT" or _has_metrics(activity):
        return None
    note = (activity.note or "").strip()
    return GARMIN_LIMITATION_NOTE + (f" Intervals.icu says: {note}" if note else "")


def data_availability_note(activity: ActivitySummary) -> str | None:
    """Strava / Garmin restriction note, or a generic one for any metric-less stub
    the API annotated with ``_note``. None for normal activities."""
    for check in (strava_limitation_note, garmin_limitation_note):
        message = check(activity)
        if message:
            return message
    note = (activity.note or "").strip()
    if note and not _has_metrics(activity):
        source = f" (source: {activity.source})" if activity.source else ""
        return RESTRICTED_NOTE_TEMPLATE.format(source=source, note=f" Intervals.icu says: {note}")
    return None


def restricted_source(activity: ActivitySummary) -> str | None:
    """The source label of a restricted stub (e.g. 'GARMIN_CONNECT'), or None."""
    if data_availability_note(activity) is None:
        return None
    return activity.source or "UNKNOWN"


def summarize_restricted(activities: list[ActivitySummary]) -> str | None:
    """One list-level warning counting metric-less stubs per source, or None."""
    counts: dict[str, int] = {}
    for activity in activities:
        source = restricted_source(activity)
        if source:
            counts[source] = counts.get(source, 0) + 1
    if not counts:
        return None
    parts = ", ".join(f"{n} from {source}" for source, n in sorted(counts.items()))
    total = sum(counts.values())
    message = (
        f"{total} of {len(activities)} activities came back from the Intervals.icu API "
        f"without metrics ({parts}); they are marked data_restricted. Do not infer metrics "
        "for them."
    )
    if "GARMIN_CONNECT" in counts:
        message += " " + GARMIN_LIMITATION_NOTE
    elif "STRAVA" in counts:
        message += " " + STRAVA_LIMITATION_NOTE
    return message


async def fetch_strava_limitation_note(client: ICUClient, activity_id: str) -> str | None:
    """Fetch the activity and check whether it is source-restricted (Strava, Garmin, ...).

    Used by tools (streams, intervals, histograms, best efforts) that don't
    otherwise need the Activity object — only call this on the empty-result
    path so we don't add latency for normal activities.
    """
    try:
        activity = await client.get_activity(activity_id=activity_id)
    except ICUAPIError:
        return None
    return data_availability_note(activity)
