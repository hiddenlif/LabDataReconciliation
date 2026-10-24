"""Differential and edge-case tests: reconcile() against an independent plain-Python formulation of the stated policy.

The generator builds seeded, messy synthetic exports (exact duplicates, blank identities, reused record identifiers, tied or
unorderable revisions, unapproved and out-of-range results, naive timestamps, conflicting or missing sample metadata). The
oracle below states the same policy sample by sample with loops and dictionaries instead of vectorised pandas, so a mistake in
one formulation shows up as a disagreement. Every source row's disposition and reason is compared, not only the selected set.
"""

import json
import math
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from analysis import COLUMNS, ROOT, reconcile, reconcile_files

SEEDS = range(200)


def random_export(seed):
    rng = np.random.default_rng(seed)
    rows, meta, next_id = [], [], 0
    n_samples = int(rng.integers(3, 9))
    for i in range(n_samples):
        sample = f"S{i:02d}"
        if rng.random() > 0.1:
            meta.append((sample, f"D{i % 3}", "control" if i % 2 else "treated"))
            if rng.random() < 0.1:
                meta.append(
                    (sample, f"D{i % 3}", "control" if i % 2 else "treated")
                )  # exact duplicate metadata row
            elif rng.random() < 0.1:
                meta.append((sample, "D9", "treated"))  # conflicting metadata
        revisions = rng.permutation(np.arange(1, int(rng.integers(1, 5)) + 1))
        for rev in revisions:
            next_id += 1
            unit = "percent" if rng.random() < 0.5 else "fraction"
            value = (
                float(rng.integers(40, 100))
                if unit == "percent"
                else float(rng.integers(40, 100)) / 100
            )
            u = rng.random()
            if u < 0.06:
                value = 120.0 if unit == "percent" else 1.4  # outside [0, 1]
            elif u < 0.10:
                value = "abc"  # not a measurement
            stamp = f"2026-01-0{int(1 + int(rng.integers(0, 8)))}T00:00:00+00:00"
            u = rng.random()
            if u < 0.08:
                stamp = "2026-01-05T08:00:00"  # no timezone
            elif u < 0.12:
                stamp = "2026-01-05T08:00:00-05:00"
            status = rng.choice(
                ["approved", "approved", "approved", "pending", "rejected"]
            )
            rows.append(
                [f"r{next_id:03d}", sample, int(rev), str(status), value, unit, stamp]
            )
        if rng.random() < 0.1 and rows:
            rows.append(list(rows[-1]))  # exact duplicate payload
        if rng.random() < 0.1 and len(rows) > 1:
            rows[-1][0] = rows[-2][0]  # reused record identifier
        if rng.random() < 0.08:
            rows.append(
                [
                    f"r{next_id + 500:03d}",
                    sample,
                    int(revisions.max()),
                    "approved",
                    90.0,
                    "percent",
                    "2026-01-02T00:00:00+00:00",
                ]
            )  # tie
        if rng.random() < 0.06:
            rows.append(
                [
                    f"r{next_id + 700:03d}",
                    "  ",
                    1,
                    "approved",
                    90.0,
                    "percent",
                    "2026-01-02T00:00:00+00:00",
                ]
            )  # blank identity
    order = rng.permutation(len(rows))
    records = pd.DataFrame([rows[k] for k in order], columns=COLUMNS, dtype=object)
    metadata = pd.DataFrame(meta, columns=["sample_id", "donor_id", "condition"])
    return records, metadata


def usable_id(x):
    return isinstance(x, str) and x.strip() != ""


def valid_measurement(value, unit, stamp):
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or unit not in ("percent", "fraction"):
        return None
    v = v / 100 if unit == "percent" else v
    if not 0 <= v <= 1:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return v if when.tzinfo is not None else None


def oracle(records, metadata):
    """Per source row: (disposition, reason); plus {sample: (record_id, revision, value)} for the selected rows."""
    rows = [tuple(r) for r in records.itertuples(index=False, name=None)]
    verdict, seen, live = {}, set(), []
    for i, row in enumerate(rows):
        if row in seen:
            verdict[i] = ("duplicate", "exact duplicate payload")
        else:
            seen.add(row)
            if usable_id(row[1]) and usable_id(row[0]):
                live.append(i)
            else:
                verdict[i] = ("quarantined", "missing sample or record identity")
    meta_rows = {}
    for s, d, c in set(metadata.itertuples(index=False, name=None)):
        meta_rows.setdefault(s, set()).add((d, c))
    conflicting_meta = {s for s, v in meta_rows.items() if len(v) > 1}
    known = {s for s in meta_rows if s not in conflicting_meta}
    ids = {}
    for i in live:
        ids.setdefault(rows[i][0], []).append(i)
    shared_ids = {rid for rid, idx in ids.items() if len(idx) > 1}
    by_sample = {}
    for i in live:
        by_sample.setdefault(rows[i][1], []).append(i)
    selected = {}
    for sample, idx in by_sample.items():
        reason = None
        revisions = [rows[i][2] for i in idx]
        if sample in conflicting_meta:
            reason = "conflicting sample metadata"
        elif any(rows[i][0] in shared_ids for i in idx):
            reason = "conflicting record identity"
        elif sample not in known:
            reason = "orphan sample"
        elif any(
            isinstance(r, bool) or not isinstance(r, int) or r < 1 for r in revisions
        ):
            reason = "unorderable revision"
        else:
            newest = [i for i in idx if rows[i][2] == max(revisions)]
            if len(newest) != 1:
                reason = "ambiguous latest revision"
            elif rows[newest[0]][3] != "approved":
                reason = "latest revision not approved"
            else:
                value = valid_measurement(
                    rows[newest[0]][4], rows[newest[0]][5], rows[newest[0]][6]
                )
                if value is None:
                    reason = "invalid latest measurement or timestamp"
        if reason:
            for i in idx:
                verdict[i] = ("quarantined", reason)
        else:
            chosen = newest[0]
            for i in idx:
                verdict[i] = (
                    ("selected", "latest unique approved valid revision")
                    if i == chosen
                    else ("superseded", "earlier version retained in audit")
                )
            selected[sample] = (rows[chosen][0], max(revisions), value)
    return verdict, selected


@pytest.mark.parametrize("seed", SEEDS)
def test_every_row_disposition_matches_the_independent_oracle(seed):
    records, metadata = random_export(seed)
    clean, audit = reconcile(records, metadata)
    verdict, selected = oracle(records, metadata)
    got = {int(r.source_row): (r.disposition, r.reason) for r in audit.itertuples()}
    assert got == verdict
    assert len(audit) == len(records) and audit.source_row.is_unique
    assert {
        r.sample_id: (r.record_id, r.revision, r.viability_fraction)
        for r in clean.itertuples()
    } == selected


@pytest.mark.parametrize("seed", range(0, 200, 5))
def test_row_order_does_not_change_the_evidence(seed):
    records, metadata = random_export(seed)
    shuffled = records.sample(frac=1, random_state=seed + 1).reset_index(drop=True)
    a, audit_a = reconcile(records, metadata)
    b, audit_b = reconcile(shuffled, metadata)
    key = ["sample_id", "record_id", "revision", "viability_fraction"]
    pd.testing.assert_frame_equal(
        a[key].sort_values("sample_id").reset_index(drop=True),
        b[key].sort_values("sample_id").reset_index(drop=True),
    )
    outcome = lambda audit: sorted(
        zip(audit.record_id, audit.sample_id, audit.disposition, audit.reason)
    )
    assert outcome(audit_a) == outcome(audit_b)


@pytest.mark.parametrize("seed", range(0, 200, 5))
def test_reconciling_the_canonical_result_changes_nothing(seed):
    records, metadata = random_export(seed)
    clean, _ = reconcile(records, metadata)
    if clean.empty:
        return
    again = pd.DataFrame(
        {
            "record_id": clean.record_id,
            "sample_id": clean.sample_id,
            "revision": clean.revision,
            "status": "approved",
            "value": clean.viability_fraction,
            "unit": "fraction",
            "measured_at": clean.measured_at.str.replace(" ", "T"),
        },
        columns=COLUMNS,
    )
    clean2, audit2 = reconcile(again, metadata)
    assert set(audit2.disposition) == {"selected"}
    assert (
        clean2.sort_values("sample_id").viability_fraction.tolist()
        == clean.sort_values("sample_id").viability_fraction.tolist()
    )


def frame(rows):
    return pd.DataFrame(rows, columns=COLUMNS, dtype=object)


META = pd.DataFrame(
    [("A", "D1", "control"), ("B", "D1", "treated"), ("C", "D2", "control")],
    columns=["sample_id", "donor_id", "condition"],
)
STAMP = "2026-02-03T12:00:00+00:00"


def test_offset_timestamps_are_stored_in_utc():
    clean, _ = reconcile(
        frame(
            [["r1", "A", 1, "approved", 90.0, "percent", "2026-02-03T07:00:00-05:00"]]
        ),
        META,
    )
    assert clean.measured_at.iloc[0] == "2026-02-03 12:00:00+00:00"


def test_a_naive_timestamp_quarantines_the_sample():
    _, audit = reconcile(
        frame([["r1", "A", 1, "approved", 90.0, "percent", "2026-02-03T07:00:00"]]),
        META,
    )
    assert audit.reason.tolist() == ["invalid latest measurement or timestamp"]


def test_whitespace_only_identities_are_quarantined_not_dropped():
    clean, audit = reconcile(
        frame(
            [
                ["r1", " ", 1, "approved", 90.0, "percent", STAMP],
                ["r2", "A", 1, "approved", 80.0, "percent", STAMP],
            ]
        ),
        META,
    )
    assert audit.disposition.tolist() == ["quarantined", "selected"]
    assert clean.sample_id.tolist() == ["A"]


def test_a_record_identifier_shared_by_two_samples_quarantines_both():
    _, audit = reconcile(
        frame(
            [
                ["r1", "A", 1, "approved", 90.0, "percent", STAMP],
                ["r1", "B", 1, "approved", 80.0, "percent", STAMP],
            ]
        ),
        META,
    )
    assert set(audit.reason) == {"conflicting record identity"} and set(
        audit.disposition
    ) == {"quarantined"}


def test_conflicting_metadata_quarantines_the_sample_but_not_its_neighbours():
    meta = pd.concat(
        [META, pd.DataFrame([("A", "D9", "treated")], columns=META.columns)],
        ignore_index=True,
    )
    clean, audit = reconcile(
        frame(
            [
                ["r1", "A", 1, "approved", 90.0, "percent", STAMP],
                ["r2", "B", 1, "approved", 80.0, "percent", STAMP],
            ]
        ),
        meta,
    )
    assert (
        dict(zip(audit.record_id, audit.reason))["r1"] == "conflicting sample metadata"
    )
    assert clean.sample_id.tolist() == ["B"]


def test_an_exact_duplicate_metadata_row_is_not_a_conflict():
    meta = pd.concat([META, META.iloc[[0]]], ignore_index=True)
    clean, _ = reconcile(
        frame([["r1", "A", 1, "approved", 90.0, "percent", STAMP]]), meta
    )
    assert clean.sample_id.tolist() == ["A"]


def test_an_unapproved_latest_revision_never_falls_back_to_an_older_approved_one():
    clean, audit = reconcile(
        frame(
            [
                ["r1", "A", 1, "approved", 90.0, "percent", STAMP],
                ["r2", "A", 2, "pending", 91.0, "percent", STAMP],
            ]
        ),
        META,
    )
    assert clean.empty and set(audit.reason) == {"latest revision not approved"}


def test_a_tie_at_the_latest_revision_is_ambiguous_even_when_values_agree():
    _, audit = reconcile(
        frame(
            [
                ["r1", "A", 2, "approved", 90.0, "percent", STAMP],
                ["r2", "A", 2, "approved", 90.0, "percent", STAMP],
            ]
        ),
        META,
    )
    assert set(audit.reason) == {"ambiguous latest revision"}


def test_a_fractional_revision_cannot_be_ordered():
    _, audit = reconcile(
        frame([["r1", "A", 1.5, "approved", 90.0, "percent", STAMP]]), META
    )
    assert audit.reason.tolist() == ["unorderable revision"]


def test_command_line_round_trip(tmp_path):
    counts = reconcile_files(
        ROOT / "data/lab_export.csv", ROOT / "data/sample_metadata.csv", tmp_path
    )
    assert counts == {"duplicate": 1, "quarantined": 12, "selected": 4, "superseded": 1}
    assert json.loads((tmp_path / "dispositions.json").read_text()) == counts
    canonical = pd.read_csv(tmp_path / "canonical.csv")
    expected = pd.read_csv(ROOT / "data/expected_canonical.csv")
    pd.testing.assert_frame_equal(
        canonical[["sample_id", "viability_fraction"]]
        .sort_values("sample_id")
        .reset_index(drop=True),
        expected,
        check_dtype=False,
    )
