import math

from erg import metrics
from erg.metrics import StrokePoint, WorkSample


def stroke(interval, t, d, pace=105.0, spm=28, hr=150):
    return StrokePoint(interval_idx=interval, t_s=t, d_m=d, pace_s_500=pace, spm=spm, hr=hr)


def sample(dt=2.0, watts=300.0, hr=150, pace=105.0, spm=28, distance=10.0, elapsed=0.0):
    return WorkSample(dt_s=dt, distance_m=distance, watts=watts, hr=hr, pace_s_500=pace, spm=spm, elapsed_d_m=elapsed)


def test_work_samples_restart_at_each_interval():
    points = [stroke(0, 2, 10), stroke(0, 4, 20), stroke(1, 2, 10), stroke(1, 4, 20)]
    samples = metrics.work_samples(points)
    assert [s.dt_s for s in samples] == [2, 2, 2, 2]
    assert [s.elapsed_d_m for s in samples] == [10, 20, 30, 40]  # accumulates across intervals


def test_work_samples_drop_backwards_jitter_and_long_pauses():
    points = [stroke(0, 2, 10), stroke(0, 1.5, 12), stroke(0, 20, 100), stroke(0, 22, 110)]
    samples = metrics.work_samples(points)
    assert len(samples) == 2  # the jitter stroke and the 18s gap are skipped
    assert samples[-1].dt_s == 2


def test_efficiency_factor_is_time_weighted():
    ef, watts, hr = metrics.efficiency_factor([sample(dt=1, watts=200, hr=100), sample(dt=3, watts=300, hr=150)])
    assert watts == 275 and hr == 137.5  # 3x weight on the longer stroke
    assert ef == 2.0
    assert metrics.efficiency_factor([sample(watts=None), sample(hr=None)]) == (None, None, None)


def test_decoupling_positive_when_the_second_half_fades():
    first = [sample(watts=300, hr=150) for _ in range(10)]  # EF 2.0
    second = [sample(watts=300, hr=165) for _ in range(10)]  # EF ~1.82
    pct, ef1, ef2 = metrics.decoupling_pct(first + second)
    assert round(pct, 1) == 9.1 and ef1 > ef2

    steady = [sample() for _ in range(20)]
    assert metrics.decoupling_pct(steady)[0] == 0.0

    negative, _, _ = metrics.decoupling_pct(
        [sample(watts=300, hr=165) for _ in range(10)] + [sample(watts=300, hr=150) for _ in range(10)]
    )
    assert negative < 0  # second half more efficient: a negative split


def test_pace_shape_halves_and_variability():
    samples = [sample(pace=100.0, elapsed=float(i)) for i in range(1, 11)]
    samples += [sample(pace=110.0, elapsed=float(i)) for i in range(11, 21)]
    shape = metrics.pace_shape(samples)
    assert shape["first_half_pace"] == 100.0 and shape["second_half_pace"] == 110.0
    assert shape["pace_cv"] > 0
    assert metrics.pace_shape([sample()])["pace_cv"] is None


def test_fade_onset_finds_a_sustained_slowdown():
    # 1000m: even to 600m, then slower and never recovering.
    fading = [sample(pace=100.0, elapsed=float(d)) for d in range(0, 600, 10)]
    fading += [sample(pace=112.0, elapsed=float(d)) for d in range(600, 1000, 10)]
    assert metrics.pace_shape(fading)["fade_onset_m"] == 600

    even = [sample(pace=100.0, elapsed=float(d)) for d in range(0, 1000, 10)]
    assert metrics.pace_shape(even)["fade_onset_m"] is None

    # A dip that recovers is not a fade.
    dip = [sample(pace=100.0, elapsed=float(d)) for d in range(0, 400, 10)]
    dip += [sample(pace=112.0, elapsed=float(d)) for d in range(400, 600, 10)]
    dip += [sample(pace=100.0, elapsed=float(d)) for d in range(600, 1000, 10)]
    assert metrics.pace_shape(dip)["fade_onset_m"] is None


def test_hr_recovery_uses_the_dominant_rest_length():
    # Stroke data rarely reaches 60s into a rest, so this comes from the C2 interval summary.
    drop, rest_s, n = metrics.hr_recovery([(120.0, 170, 125), (120.0, 172, 129), (120.0, 174, 129), (30.0, 168, 158)])
    assert (drop, rest_s, n) == (45.0, 120.0, 3)  # median of 45/43/45, ignoring the odd 30s rest
    assert metrics.hr_recovery([(120.0, None, 125), (0.0, 170, 130)]) == (None, None, 0)


def test_dps_and_trimp():
    assert metrics.distance_per_stroke(2000, 233) == 2000 / 233
    assert metrics.distance_per_stroke(2000, None) is None

    easy = metrics.trimp(3600, 130, 193, 60)
    hard = metrics.trimp(3600, 175, 193, 60)
    assert hard > easy > 0
    assert metrics.trimp(3600, None, 193) is None
    assert metrics.trimp(3600, 55, 193, 60) is None  # below resting


def test_kilojoules_is_watts_times_seconds():
    assert metrics.kilojoules([sample(dt=10, watts=200), sample(dt=10, watts=300)]) == 5.0
    assert metrics.kilojoules([sample(watts=None)]) is None


def test_acwr_and_monotony():
    assert metrics.acwr(100, 80) == 1.25
    assert metrics.acwr(100, 0) is None
    assert metrics.monotony([10, 10, 10, 10]) is None  # no variation: undefined
    assert math.isclose(metrics.monotony([0, 10, 0, 10]), 0.8660254, rel_tol=1e-6)
    assert metrics.monotony([5]) is None


def test_stroke_length_mean_and_variability():
    even = [sample(distance=10.0) for _ in range(10)]
    mean, cv = metrics.stroke_length(even)
    assert mean == 10.0 and cv == 0.0

    varied = [sample(distance=d) for d in (8.0, 10.0, 12.0, 10.0, 9.0, 11.0)]
    mean, cv = metrics.stroke_length(varied)
    assert round(mean, 2) == 10.0 and 0 < cv < 0.2
    assert metrics.stroke_length([sample()]) == (None, None)
