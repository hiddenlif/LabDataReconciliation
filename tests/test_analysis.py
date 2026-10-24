import numpy as np
import pandas as pd
import pytest

from analysis import ROOT, normalize, reconcile


def inputs():
    return pd.read_csv(
        ROOT / "data/lab_export.csv", keep_default_na=False
    ), pd.read_csv(ROOT / "data/sample_metadata.csv", keep_default_na=False)


def test_hand_authored_expected():
    r, m = inputs()
    c, a = reconcile(r, m)
    expected = pd.read_csv(ROOT / "data/expected_canonical.csv")
    pd.testing.assert_frame_equal(c[["sample_id", "viability_fraction"]], expected)
    assert len(a) == len(r)
    assert a.source_row.is_unique


@pytest.mark.parametrize(
    "v,u,expected",
    [
        (0, "fraction", 0),
        (1, "fraction", 1),
        (0, "percent", 0),
        (100, "percent", 1),
        (90, "percent", 0.9),
    ],
)
def test_units(v, u, expected):
    assert normalize(v, u) == expected


@pytest.mark.parametrize(
    "v,u",
    [
        (101, "percent"),
        (-0.1, "fraction"),
        (1.1, "fraction"),
        (np.nan, "fraction"),
        (np.inf, "fraction"),
        (True, "fraction"),
        ("bad", "percent"),
        (5, "unknown"),
    ],
)
def test_bad_units(v, u):
    with pytest.raises(ValueError):
        normalize(v, u)


def test_no_fallback_from_invalid_latest():
    r, m = inputs()
    c, a = reconcile(r, m)
    assert "S03" not in set(c.sample_id)
    assert set(a[a.sample_id == "S03"].disposition) == {"quarantined"}


def test_no_fallback_from_pending_latest():
    r, m = inputs()
    c, _a = reconcile(r, m)
    assert "S04" not in set(c.sample_id)


def test_shuffle_does_not_change_selected_evidence():
    r, m = inputs()
    a, _ = reconcile(r, m)
    b, _ = reconcile(r.sample(frac=1, random_state=3), m.sample(frac=1, random_state=4))
    pd.testing.assert_frame_equal(
        a.drop(columns="source_row"), b.drop(columns="source_row")
    )


def test_duplicate_exactly_once():
    r, m = inputs()
    c, a = reconcile(r, m)
    assert (a.disposition == "duplicate").sum() == 1
    assert len(c[c.sample_id == "S02"]) == 1


@pytest.mark.parametrize(
    "sample,reason",
    [
        ("S07", "conflicting sample metadata"),
        ("S08", "ambiguous latest revision"),
        ("S09", "conflicting record identity"),
        ("S11", "orphan sample"),
    ],
)
def test_reason(sample, reason):
    r, m = inputs()
    _, a = reconcile(r, m)
    assert set(a[a.sample_id == sample].reason) == {reason}


@pytest.mark.parametrize("issue", ["revision", "timestamp", "missing_column", "empty"])
def test_malformed_source(issue):
    r, m = inputs()
    if issue == "revision":
        r.loc[r.sample_id == "S01", "revision"] = "bad"
        c, _a = reconcile(r, m)
        assert "S01" not in set(c.sample_id)
    elif issue == "timestamp":
        r.loc[r.record_id == "r02", "measured_at"] = "2026-01-01"
        c, _a = reconcile(r, m)
        assert "S01" not in set(c.sample_id)
    else:
        if issue == "missing_column":
            r = r.drop(columns="record_id")
        else:
            r = r.iloc[:0]
        with pytest.raises(ValueError):
            reconcile(r, m)


def test_numeric_string_revision_is_normalized():
    r, m = inputs()
    r["revision"] = r.revision.astype(str) + ".0"
    c, _ = reconcile(r, m)
    assert set(c.sample_id) == {"S01", "S02", "S06", "S10"}


def test_boolean_revision_is_not_an_ordinal():
    r, m = inputs()
    r["revision"] = r.revision.astype(object)
    r.loc[r.sample_id == "S01", "revision"] = True
    c, a = reconcile(r, m)
    assert "S01" not in set(c.sample_id)
    assert set(a[a.sample_id == "S01"].reason) == {"unorderable revision"}
