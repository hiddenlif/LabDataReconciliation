"""Synthetic laboratory-export reconciliation with an auditable row disposition."""

from pathlib import Path
import argparse, hashlib, json, sys
import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

ROOT = Path(__file__).parent
COLUMNS = [
    "record_id",
    "sample_id",
    "revision",
    "status",
    "value",
    "unit",
    "measured_at",
]


def clean_id(x):
    return isinstance(x, str) and bool(x.strip())


def normalize(value, unit):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("Boolean is not a measurement")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("Nonnumeric measurement")
    if not np.isfinite(v):
        raise ValueError("Nonfinite measurement")
    if unit == "percent":
        v /= 100
    elif unit != "fraction":
        raise ValueError("Unknown unit")
    if not 0 <= v <= 1:
        raise ValueError("Viability outside biological [0,1] bounds")
    return v


def reconcile(records, metadata):
    if not set(COLUMNS) <= set(records) or not {
        "sample_id",
        "donor_id",
        "condition",
    } <= set(metadata):
        raise ValueError("Missing required columns")
    if records.empty or metadata.empty:
        raise ValueError("Empty source")
    r = records[COLUMNS].copy()
    r["_source_row"] = np.arange(len(r))
    audit = []
    selected = []

    # Named tuples rename underscore columns; use dict records to preserve exact row IDs.
    def mark(rows, disposition, reason):
        for row in rows.to_dict("records"):
            audit.append(
                {
                    "source_row": int(row["_source_row"]),
                    "record_id": row["record_id"],
                    "sample_id": row["sample_id"],
                    "disposition": disposition,
                    "reason": reason,
                }
            )

    dup = r.duplicated(COLUMNS, keep="first")
    mark(r[dup], "duplicate", "exact duplicate payload")
    r = r[~dup].copy()
    invalid = ~r.sample_id.map(clean_id) | ~r.record_id.map(clean_id)
    mark(r[invalid], "quarantined", "missing sample or record identity")
    r = r[~invalid].copy()
    m = metadata.drop_duplicates().copy()
    if not m.sample_id.map(clean_id).all():
        raise ValueError("Metadata has missing identity")
    bad_meta = set(m.loc[m.duplicated("sample_id", keep=False), "sample_id"])
    m = m[~m.sample_id.isin(bad_meta)]
    if not m.donor_id.map(clean_id).all() or not m.condition.map(clean_id).all():
        raise ValueError("Metadata missing donor/condition")
    bad_ids = set(r.loc[r.duplicated("record_id", keep=False), "record_id"])
    bad_record_samples = set(r.loc[r.record_id.isin(bad_ids), "sample_id"])
    for sample, g in r.groupby("sample_id", sort=True):
        reason = None
        if sample in bad_meta:
            reason = "conflicting sample metadata"
        elif sample in bad_record_samples:
            reason = "conflicting record identity"
        elif sample not in set(m.sample_id):
            reason = "orphan sample"
        else:
            rev = pd.to_numeric(g.revision, errors="coerce")
            if (
                g.revision.map(lambda x: isinstance(x, (bool, np.bool_))).any()
                or rev.isna().any()
                or not np.isfinite(rev).all()
                or (rev < 1).any()
                or (rev % 1 != 0).any()
            ):
                reason = "unorderable revision"
            else:
                # Resolve revision identity before filtering status/measurement validity.
                newest = g.loc[rev == rev.max()]
                if len(newest) != 1:
                    reason = "ambiguous latest revision"
                else:
                    row = newest.iloc[0]
                    if row.status != "approved":
                        reason = "latest revision not approved"
                    else:
                        try:
                            value = normalize(row.value, row.unit)
                            timestamp = pd.Timestamp(row.measured_at)
                            if pd.isna(timestamp) or timestamp.tzinfo is None:
                                raise ValueError("Timestamp must include timezone")
                        except (ValueError, TypeError, OverflowError):
                            reason = "invalid latest measurement or timestamp"
        if reason:
            mark(g, "quarantined", reason)
            continue
        chosen = int(row["_source_row"])
        mark(
            g[g["_source_row"] == chosen],
            "selected",
            "latest unique approved valid revision",
        )
        mark(
            g[g["_source_row"] != chosen],
            "superseded",
            "earlier version retained in audit",
        )
        selected.append(
            {
                "sample_id": sample,
                "record_id": row.record_id,
                "revision": int(rev.max()),
                "viability_fraction": value,
                "source_row": chosen,
                "measured_at": str(timestamp.tz_convert("UTC")),
            }
        )
    audit = pd.DataFrame(audit).sort_values("source_row").reset_index(drop=True)
    if (
        len(audit) != len(records)
        or audit.source_row.duplicated().any()
        or set(audit.source_row) != set(range(len(records)))
    ):
        raise AssertionError("Every input row needs one disposition")
    out = pd.DataFrame(
        selected,
        columns=[
            "sample_id",
            "record_id",
            "revision",
            "viability_fraction",
            "source_row",
            "measured_at",
        ],
    ).merge(m, on="sample_id", how="left", validate="one_to_one", indicator=True)
    if not out["_merge"].eq("both").all():
        raise AssertionError("Unmatched canonical record")
    return out.drop(columns="_merge"), audit


def run(output=ROOT / "artifacts/run"):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    records = pd.read_csv(ROOT / "data/lab_export.csv", keep_default_na=False)
    meta = pd.read_csv(ROOT / "data/sample_metadata.csv", keep_default_na=False)
    clean, audit = reconcile(records, meta)
    gold = pd.read_csv(ROOT / "data/expected_canonical.csv")
    pd.testing.assert_frame_equal(
        clean[["sample_id", "viability_fraction"]]
        .sort_values("sample_id")
        .reset_index(drop=True),
        gold,
        check_dtype=False,
    )
    clean.to_csv(output / "canonical.csv", index=False)
    audit.to_csv(output / "row_audit.csv", index=False)
    # Deliberately wrong comparison: approved rows count duplicates and old versions.
    naive = []
    for row in records.itertuples():
        if row.status == "approved":
            try:
                naive.append(normalize(row.value, row.unit))
            except ValueError:
                pass
    counts = audit.disposition.value_counts().to_dict()
    sns.set_theme(style="whitegrid")
    fig, axs = plt.subplots(1, 2, figsize=(10, 4))
    sns.barplot(x=list(counts), y=list(counts.values()), ax=axs[0], color="#238b45")
    axs[0].tick_params(axis="x", rotation=25)
    axs[0].set(title="Every source row has a disposition", ylabel="Source rows")
    sns.barplot(
        x=clean.sample_id, y=clean.viability_fraction, ax=axs[1], color="#2b8cbe"
    )
    axs[1].axhline(0.9, color="black", ls="--", label="Illustrative review threshold")
    axs[1].set(
        ylim=(0, 1), ylabel="Viability fraction", title="Canonical sample results"
    )
    axs[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(output / "reconciliation.png", dpi=170)
    plt.close(fig)
    result = {
        "source": "Fully synthetic laboratory export; not clinical or manufacturing records",
        "input_rows": len(records),
        "metadata_unique_sample_ids": int(meta.sample_id.nunique()),
        "canonical_samples": len(clean),
        "dispositions": counts,
        "canonical_mean_viability": float(clean.viability_fraction.mean()),
        "naive_approved_row_mean": float(np.mean(naive)),
        "mean_comparison_limit": "Different record populations; this contrast demonstrates weighting/eligibility differences, not a treatment effect.",
        "illustrative_threshold": 0.9,
        "canonical_samples_at_or_above_threshold": int(
            (clean.viability_fraction >= 0.9).sum()
        ),
        "quarantine_reasons": audit.loc[audit.disposition == "quarantined", "reason"]
        .value_counts()
        .to_dict(),
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "data").glob("*.csv"))
        },
        "policy": "Latest revision resolved before validity/status filtering. Invalid or unapproved latest records quarantine the sample; never silently fall back. Timestamp is evidence, not a tie-breaker.",
    }
    (output / "summary.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n"
    )
    return result


def reconcile_files(export, metadata, output):
    """Reconcile two CSV files; write canonical.csv, row_audit.csv and dispositions.json to output and return the counts."""
    records = pd.read_csv(export, keep_default_na=False)
    meta = pd.read_csv(metadata, keep_default_na=False)
    clean, audit = reconcile(records, meta)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    clean.to_csv(output / "canonical.csv", index=False)
    audit.to_csv(output / "row_audit.csv", index=False)
    counts = {k: int(v) for k, v in audit.disposition.value_counts().items()}
    (output / "dispositions.json").write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
    return counts


def main(argv):
    parser = argparse.ArgumentParser(description="Synthetic laboratory-export reconciliation")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="regenerate the reference results (default)")
    rec = sub.add_parser("reconcile", help="reconcile your own export against sample metadata")
    rec.add_argument("export")
    rec.add_argument("metadata")
    rec.add_argument("output")
    args = parser.parse_args(argv)
    if args.command == "reconcile":
        print(json.dumps(reconcile_files(args.export, args.metadata, args.output), indent=2, sort_keys=True))
    else:
        print(json.dumps(run(), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
