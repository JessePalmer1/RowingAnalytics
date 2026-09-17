import httpx
import respx
from conftest import result_payload

from erg import cli
from erg.c2.client import C2Client
from test_client import NoLimit
from test_db import INTERVAL_WORKOUT, STROKES, fake_c2, strokes_url


@respx.mock
def test_sync_pulls_workouts_then_strokes(db, settings, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "client_for_athlete", lambda s, a: C2Client(s, lambda: "tok", http=httpx.Client(), limiter=NoLimit())
    )
    fake_c2([result_payload(id=1, workout_type="FixedTimeInterval", workout=INTERVAL_WORKOUT)])
    respx.get(strokes_url(1)).mock(return_value=httpx.Response(200, json={"data": STROKES}))

    assert cli._run(settings, 42, workouts=True, strokes=True) == 0
    out = capsys.readouterr().out
    assert "workouts: fetched=1 inserted=1" in out
    assert "strokes: fetched=1 strokes=5" in out

    # Second sync: nothing new, nothing fetched.
    assert cli._run(settings, 42, workouts=True, strokes=True) == 0
    out = capsys.readouterr().out
    assert "unchanged=1" in out and "strokes: fetched=0" in out


@respx.mock
def test_sync_exits_nonzero_when_stroke_fetch_errors(db, settings, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "client_for_athlete", lambda s, a: C2Client(s, lambda: "tok", http=httpx.Client(), limiter=NoLimit())
    )
    fake_c2([result_payload(id=1)])
    respx.get(strokes_url(1)).mock(return_value=httpx.Response(400, text="bad"))
    assert cli._run(settings, 42, workouts=True, strokes=True) == 1
