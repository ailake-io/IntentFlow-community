# Pipeline audit with IntentFlow: proposal and report template

[Português](pt-BR/PILOT_OFFER.md)

Sales material for the paid pilot. The technical step-by-step guide is [PILOT](PILOT.md).

## Proposal (1 page)

**Problem:** changes in dbt models, Spark/Python jobs and DAGs break consumers and dashboards in production, and the team only finds out after the incident.

**What we deliver in 2 to 4 weeks:**

1. **Impact map**: column lineage of the whole project, including jobs outside dbt (Spark, Python, notebooks through OpenLineage).
2. **What-can-break report**: each risk with what breaks and how to fix it, prioritized by severity.
3. **Preventive findings**: expensive or fragile SQL, duplicated keys, unmasked PII, incrementals without a key, DAGs without retry/timeout.
4. **PR gate turned on** in your CI, with the agreed false positive threshold.
5. **1-hour presentation** of the top 10 risks and the recommended fixes, including the ones we can already apply.

**What we need:**

- Read access to the dbt repository (or to the `manifest.json` and `catalog.json` artifacts).
- Optional: read-only development warehouse; Airflow or Kestra URL; OpenLineage endpoint.
- 2 hours of one person on the team: kickoff and finding review.

**Security:**

- Everything runs in your environment and is read-only.
- No data leaves your environment. AI stays off unless you ask for it.
- At the end we delete the local store.

**Success criterion agreed at kickoff:** at least one real risk you did not know about, and false positives below 50%.

**Investment:** [fixed price] · **After the pilot:** open source IntentFlow in CI at no cost, or the Enterprise edition (SSO, per-project
governance, governed fixes, AI, support), see [EDITIONS](EDITIONS.md).

## Report template

Each section comes from one command. Build everything into a folder and paste it into the customer document.

```bash
mkdir -p report
intentflow risks  --target target --base ../main/target                   > report/1-risks.md
intentflow check  --target target --base ../main/target --format markdown > report/2-findings.md
intentflow lineage --target target --column <model.critical_column> --impact > report/3-impact.txt   # 3 to 5 columns
intentflow lineage --target target --coverage                             > report/4-coverage.txt
intentflow stats                                                          > report/5-precision.txt    # after marking false positives
```
Use `INTENTFLOW_LANG=pt` for a Portuguese report.

| Section | Content | Source |
|---|---|---|
| Executive summary | top 3 risks, number of findings, % useful, gate recommendation | you, from the sections below |
| 1. What can break | risks by area (visibility, fragility, external jobs, process) | `risks` |
| 2. Findings | by severity, with owner and fix | `check` |
| 3. Impact of the critical columns | direct and transitive consumers, external jobs | `lineage --impact` |
| 4. Blind spots | jobs with conservative propagation and unmatched datasets | `lineage --coverage` and `--unmatched` |
| 5. Quality of IntentFlow itself | false positives per check, lineage false negatives | `stats` and the "What to send back" table in [PILOT](PILOT.md) |
| 6. Next steps | CI gate, fixes to apply, what to declare in `intentflow.lineage.json` | you |
