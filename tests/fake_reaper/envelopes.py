"""The fake REAPER's track envelopes: Point (time, raw value; shape and
tension as attributes) lists per (track pointer, envelope name). A point's shape is how the curve
goes to the next point; Envelope_Evaluate follows linear (0) and square (1,
the value holds until the next point) and treats every other shape as linear,
so a tool that turns a square step into a slide is caught.

REAPER keeps points sorted by time once Envelope_SortPoints runs (or at once
when a call is not told noSort); a point inserted with noSort lands at the
end until then, as in REAPER, so a tool that forgets to sort is caught.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper

SQUARE = 1


class Point(tuple):
    """An envelope point: compares and unpacks as (time, raw value), so tests
    can write plain pairs (linear points); shape and tension ride along."""

    def __new__(cls, time: float, value: float, shape: int = 0, tension: float = 0.0):
        point = super().__new__(cls, (time, value))
        point.shape, point.tension = shape, tension
        return point

    def __getnewargs__(self):
        return (*self, self.shape, self.tension)


def shape_of(point: tuple) -> int:
    return getattr(point, "shape", 0)


def tension_of(point: tuple) -> float:
    return getattr(point, "tension", 0.0)


NULL_ENVELOPE = "(TrackEnvelope*)0x0000000000000000"
# What an envelope with no points evaluates to, raw: 0 dB (fader-scaled) or center.
EMPTY_VALUE = {"Volume": 1000.0, "Pan": 0.0}


class Envelopes:
    """Mixed into FakeReaper."""

    def _points(self: FakeReaper, envelope: str) -> list[Point]:
        track, name = envelope.removeprefix("(TrackEnvelope*)").rsplit("|", 1)
        return self.envelopes[(track, name)]

    def _sort_points(self: FakeReaper, envelope: str) -> None:
        self._points(envelope).sort(key=lambda point: point[0])

    def GetTrackEnvelopeByName(self: FakeReaper, track: str, name: str) -> str:
        # REAPER returns a null pointer string, not "", for a missing envelope.
        if (track, name) not in self.envelopes:
            return NULL_ENVELOPE
        return f"(TrackEnvelope*){track}|{name}"

    def InsertEnvelopePoint(self: FakeReaper, envelope, time, value, shape, tension, selected,
                            no_sort) -> bool:
        self._points(envelope).append(Point(time, value, shape, tension))
        if not no_sort:
            self._sort_points(envelope)
        return True

    def Envelope_SortPoints(self: FakeReaper, envelope: str) -> bool:
        self._sort_points(envelope)
        return True

    def CountEnvelopePoints(self: FakeReaper, envelope: str) -> int:
        return len(self._points(envelope))

    def GetEnvelopePoint(self: FakeReaper, envelope, index, *_out) -> list:
        points = self._points(envelope)
        if not 0 <= index < len(points):
            return [False, envelope, index, 0.0, 0.0, 0, 0.0, False]
        point = points[index]
        return [True, envelope, index, *point, shape_of(point), tension_of(point), False]

    def SetEnvelopePoint(self: FakeReaper, envelope, index, time, value, shape, tension,
                         selected, no_sort) -> list:
        points = self._points(envelope)
        ok = 0 <= index < len(points)
        if ok:
            points[index] = Point(time, value, shape, tension)
            if not no_sort:
                self._sort_points(envelope)
        return [ok, envelope, index, time, value, shape, tension, selected, no_sort]

    def DeleteEnvelopePointEx(self: FakeReaper, envelope, automation_item, index) -> bool:
        points = self._points(envelope)
        if automation_item != -1 or not 0 <= index < len(points):
            return False
        del points[index]
        return True

    def Envelope_Evaluate(self: FakeReaper, envelope, time, rate, samples, *_out) -> list:
        """Linear (or square) between points; the first/last point's value
        before/after them."""
        points = sorted(self._points(envelope), key=lambda point: point[0])
        if not points:
            value = EMPTY_VALUE[envelope.rsplit("|", 1)[1]]
        elif time <= points[0][0]:
            value = points[0][1]
        elif time >= points[-1][0]:
            value = points[-1][1]
        else:
            first, (t1, v1) = next(
                (a, b) for a, b in zip(points, points[1:], strict=False) if a[0] <= time <= b[0])
            t0, v0 = first
            if t1 == t0 or (shape_of(first) == SQUARE and time < t1):
                value = v0 if time < t1 else v1
            else:
                value = v0 + (v1 - v0) * (time - t0) / (t1 - t0)
        return [1, envelope, time, rate, samples, value, 0.0, 0.0, 0.0]

    def GetEnvelopeScalingMode(self: FakeReaper, envelope: str) -> int:
        """REAPER's volume envelopes use fader scaling (1); pan is linear (0)."""
        return 1 if envelope.endswith("|Volume") else 0

    def ScaleToEnvelopeMode(self: FakeReaper, mode: int, value: float) -> float:
        """A stand-in for REAPER's fader curve, distinguishable from the raw
        value; the real curve is exercised by tests/live (-6 dB renders -6 dB)."""
        return value * 1000.0 if mode == 1 else value

    def ScaleFromEnvelopeMode(self: FakeReaper, mode: int, value: float) -> float:
        return value / 1000.0 if mode == 1 else value
