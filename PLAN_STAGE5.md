# PLAN_STAGE5 — generation-2 scoring: slim prompt, deterministic fields as input

**Status: NOT implemented. Planning document only.** Stages 1-4 (extractors, post-score
merge, model presets + provenance, analytics captures) are done and deliberately left the
scoring prompt byte-for-byte unchanged. This stage is the one that changes it, which is why
it is written down instead of built.

## The change

Slim `SCORE_SCHEMA` and `_score_prompt` so the LLM emits **only the judgment fields**:

    score, track, reasoning, danish_level, is_tech_company

Everything mechanical (employment_type, work_mode, location, commute_ok, deadline,
matched_skills) is no longer asked of the model at all. Instead, the prompt **receives the
pre-extracted values as structured input** ("Known facts about this role: …"), so the model
spends its whole capacity on the fit judgment rather than re-deriving what a regex already
knows. The extractors (stage 1) become the *only* writer of the mechanical columns.

**Trade-off to accept first:** today (stage 2), when an extractor is silent the LLM's
answer stands. In generation 2 there is no LLM answer to fall back on — a silent extractor
means `"unknown"`/`""` in the archive. The `fields_det` / `fields_llm` columns added to
runs.csv in stage 2 are exactly the instrumentation for this: **do not build generation 2
until several weeks of runs show `fields_det` dominating** (i.e. the extractors already own
the fields in practice). If a field stays extractor-poor (location on Jobindex teasers is
the likely one), either improve that extractor first or keep that one field in the schema.

## Why this is a new score generation (the cost)

- **`tests/test_score_prompt.py` fails, by design.** The golden test pins the owner's
  rendered prompt byte-for-byte because 1500+ archived rows were scored with it. Changing
  the prompt means updating `EXPECTED` **deliberately** — that is the documented workflow,
  not a test to "fix".
- **Old and new scores are not comparable.** A shorter prompt with structured facts shifts
  the score distribution even on the same model. Plan: bump the provenance stamp so the
  generation is visible per row — e.g. stamp `scoring_model` as `<model>@gen2` (or add a
  `prompt_generation` column via the same migration pattern) — then either run
  `a_scrape.py --rescore-all` until the open shortlist is entirely gen-2, or accept mixed
  scales and filter by the stamp in `b_insights`.
- The prompt example block and `SCORE_SCHEMA` must stay in sync (same rule as today).

## The gate: d_model_ab evidence, not vibes

Generation 2 is only worth it if it lets the **16GB-class `fallback` preset** (stage 3)
hold ranking quality — the whole point of pre-extracting is that a smaller model given
clean facts should judge fit about as well as the 31B deriving everything itself.

- `d_model_ab.py` already existed (see CHANGES.md, 2026-07-02) and was removed as unused
  tooling in commit `0faff4b`. **Resurrect it from git** (`git show 0faff4b^:d_model_ab.py`)
  rather than rebuilding: it re-fetches the N most recent archived roles and scores them
  with two models side by side, read-only.
- Extend it to compare **prompt generations as well as models**: current prompt vs gen-2
  prototype, `fast` vs `fallback` preset, on ≥50 recent archived roles (mix of scores, both
  tracks, Danish and English ads).
- Metrics that matter (decide pass thresholds *before* running): rank correlation of scores
  (the shortlist is an ordering problem, not a calibration problem), agreement on shortlist
  membership at `SCORE_THRESHOLD`/`TRACK_B_MIN_SCORE`, agreement on `track` and
  `danish_level`, and latency per role. The tracker (`applications.csv`, statuses as ground
  truth) is the tie-breaker where the models disagree.
- **If the fallback model does NOT hold ranking quality with pre-extracted fields, stage 5
  does not ship.** The current prompt stays, and the fallback preset remains what it is
  today: an emergency scale of its own, stamped as such.

## Part of the same stage: is_tech_company by employer lookup

`is_tech_company` is a property of the **employer**, not the ad — yet it is currently
re-judged by the LLM on every single role. Generation 2 should:

1. **Bootstrap an employer → bool map from the existing archive** (1500+ rows): group by
   normalised company (`core._norm_company`), majority-vote `is_tech_company`, keep
   employers with ≥2 consistent rows; write it under the profile's data dir.
2. At scoring time: known employer → deterministic lookup (and the field leaves the
   schema's required set); unknown employer → the LLM judges it exactly as today, and the
   verdict is appended to the map, so the LLM is only ever consulted **once per new
   employer**.
3. **Per-profile trap:** the column actually means "employer is in this candidate's target
   sector" (`TARGET_SECTOR` is per-profile — see README). The map is therefore only valid
   for profiles sharing the same `TARGET_SECTOR`; key the map file by profile (it already
   lives in the per-profile data dir, which handles this) and never share it across
   profiles with different sectors.

## Suggested order of work

1. Accumulate `fields_det`/`fields_llm` evidence from normal runs (already ticking).
2. Resurrect `d_model_ab.py`; extend to models × prompt generations; define pass criteria.
3. Prototype the slim prompt behind the comparison harness only (never in the live path).
4. Run the gate. If it fails, stop here at zero cost to the live pipeline.
5. If it passes: bump schema + prompt + golden test in ONE deliberate commit, stamp the
   generation, `--rescore-all` the open shortlist, then bootstrap the employer map.
