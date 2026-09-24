from erg.compare import Track, common_grid, resample, split_attribution, track_from_samples
from erg.metrics import work_samples
from test_metrics import stroke


def even_track(pace_s_per_m: float, total_m: float, step: float = 10.0) -> Track:
    """A piece rowed at a constant speed, one sample every `step` metres."""
    distance, time = [0.0], [0.0]
    d = 0.0
    while d < total_m:
        d += step
        distance.append(d)
        time.append(d * pace_s_per_m)
    n = len(distance)
    return Track(distance, time, [None] * n, [None] * n, [None] * n, [None] * n)


def test_track_from_samples_accumulates_across_intervals():
    samples = work_samples([stroke(0, 2, 10), stroke(0, 4, 20), stroke(1, 2, 10)])
    track = track_from_samples(samples)
    assert track.distance_m == [0.0, 10.0, 20.0, 30.0]
    assert track.time_s == [0.0, 2.0, 4.0, 6.0]  # interval reset does not restart the clock


def test_common_grid_stops_at_the_shortest_piece():
    grid = common_grid([even_track(0.2, 2000), even_track(0.2, 1800)], points=10)
    assert len(grid) == 10 and grid[0] == 0.0 and grid[-1] == 1800.0


def test_resample_interpolates_between_samples():
    track = even_track(0.2, 1000, step=100)  # 0.2 s/m
    out = resample(track, [0.0, 50.0, 250.0, 1000.0])
    assert out["time_s"] == [0.0, 10.0, 50.0, 200.0]


def test_resample_returns_none_outside_the_piece():
    out = resample(even_track(0.2, 500), [600.0])
    assert out["time_s"] == [None]


def test_split_attribution_finds_where_time_was_lost():
    reference = even_track(0.2, 2000)  # 400s
    # Same speed for the first half, then 5% slower.
    distance, time, d, t = [0.0], [0.0], 0.0, 0.0
    while d < 2000:
        d += 10
        t += 10 * (0.2 if d <= 1000 else 0.21)
        distance.append(d)
        time.append(t)
    n = len(distance)
    slower = Track(distance, time, [None] * n, [None] * n, [None] * n, [None] * n)

    splits = split_attribution(reference, slower, 2000, segment_m=500)
    assert [s["from_m"] for s in splits] == [0, 500, 1000, 1500]
    assert [s["delta_s"] for s in splits] == [0.0, 0.0, 5.0, 5.0]  # all of it after 1000m
    assert sum(s["delta_s"] for s in splits) == 10.0


def test_split_attribution_handles_a_ragged_final_segment():
    splits = split_attribution(even_track(0.2, 1997), even_track(0.2, 1997), 1997, segment_m=500)
    assert [(s["from_m"], s["to_m"]) for s in splits] == [(0, 500), (500, 1000), (1000, 1500), (1500, 1997)]
    assert all(s["delta_s"] == 0.0 for s in splits)


def test_no_common_distance_gives_an_empty_grid():
    assert common_grid([]) == []
    assert common_grid([Track([0.0], [0.0], [None], [None], [None], [None])]) == []


def rowed(total_t: float, total_d: float, strokes: int = 100):
    """A piece rowed evenly, sampled one stroke at a time (dt stays under the pause cap)."""
    return work_samples(
        [stroke(0, total_t * i / strokes, total_d * i / strokes) for i in range(1, strokes + 1)]
    )


def test_end_anchors_to_the_logbook_total():
    # The monitor's last stroke lands short of the finish: 1996.7m/383.1s on a 2k that the
    # logbook records as 2000m/383.9s.
    track = track_from_samples(rowed(383.1, 1996.7), total_distance_m=2000.0, total_time_s=383.9)
    assert track.anchored
    assert (track.distance_m[-1], track.time_s[-1]) == (2000.0, 383.9)
    assert track.pace_s_500[-1] == track.pace_s_500[-2]  # carried, not derived from the gap


def test_anchor_is_skipped_when_the_summary_is_behind_the_strokes():
    # Truncated summaries report less than was rowed; the strokes win there.
    track = track_from_samples(rowed(200.0, 1000.0), total_distance_m=500.0, total_time_s=100.0)
    assert not track.anchored and track.distance_m[-1] == 1000.0


def test_anchor_is_skipped_when_the_gap_is_implausible():
    far = track_from_samples(rowed(200.0, 1000.0), total_distance_m=1051.0, total_time_s=210.0)
    late = track_from_samples(rowed(200.0, 1000.0), total_distance_m=1010.0, total_time_s=216.0)
    assert not far.anchored and not late.anchored


def test_anchoring_makes_the_delta_match_the_logbook_difference():
    fast = track_from_samples(rowed(383.1, 1996.7), 2000.0, 383.9)
    slow = track_from_samples(rowed(386.9, 1997.4), 2000.0, 387.4)
    grid = common_grid([fast, slow], points=50)
    assert grid[-1] == 2000.0
    ends = [resample(t, grid)["time_s"][-1] for t in (fast, slow)]
    assert round(ends[1] - ends[0], 2) == 3.5
