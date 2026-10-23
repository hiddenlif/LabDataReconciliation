# Executed findings: Lab data reconciliation

The 18-row laboratory export, sample metadata and independently hand-authored golden output are entirely synthetic.

## Observed results

18 source rows resolve to 4 selected, 1 exact duplicate, 1 superseded and 12 quarantined rows. Canonical viability values are S01 = 0.91, S02 = 0.88, S06 = 0.92 and S10 = 0.94. Their mean is 0.9125. The naive approved-row mean is 0.92267 because its population and record weights differ.

## Scope of evidence

This is an explicit example policy, not a universal laboratory SOP or a validated regulated system. Real workflows require an authorized record hierarchy, audit-trail retention and controlled exception resolution. Quarantine preserves uncertainty; it does not establish that a sample failed. The illustrative 0.90 threshold is not a clinical or product-release specification. The mean comparison is not a treatment effect.

The accompanying validation receipt identifies the analysis and dependency hashes and reports pytest and notebook execution. Re-run after changing code, sources or dependencies.
