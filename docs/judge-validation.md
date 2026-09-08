# Judge validation

Plan.md §7.4: before trusting the LLM judge's `root_cause_score`/`fix_score`, a human hand-grades a
sample of runs spanning the score range and we report agreement (exact match, within-one, Cohen's
κ) here. If agreement is poor, the rubric is at fault more often than the model — the fix is
rewording the rubric, not swapping the judge model first.

## Process

1. Grade the runs you want to validate against normally: `segbench grade`.
2. Generate a neutral, hand-gradable sample:
   ```bash
   segbench grade sample --n 40 --out docs/judge-validation/sample
   ```
   Each emitted form (`docs/judge-validation/sample/sample-NNN.json`) carries **only**
   `ground_truth_root_cause`, `also_acceptable_root_causes`, `agent_root_cause`, and
   `agent_proposed_fix` — no model identity, no environment, no channel set, and critically no
   score the judge already assigned. A `manifest.json` in the same directory maps `sample_id` back
   to `run_id` and is kept out of the form itself, so a maintainer hand-grading `sample-000.json`
   cannot see what the judge said and anchor on it.
3. A human (the maintainer, or a second reviewer) fills in `human_root_cause_score` (0–3) and
   `human_fix_score` (0–3) in each form, using the same rubric wording as `judge_prompt_v1.md`
   (`src/segbench/grade/templates/judge_prompt_v1.md`) — score against the rubric, not against gut
   feel about the answer's fluency.
4. Compute agreement:
   ```bash
   segbench grade agreement --sample-dir docs/judge-validation/sample
   ```
   This reports, per axis (root cause, fix) and pooled: exact-match rate, within-one rate, and
   Cohen's κ, computed directly (no `sklearn` dependency) against the judge's own recorded score for
   each sampled `run_id`.
5. Record the resulting numbers in this file, under "Results", along with the date, the judge model
   and prompt version, and the corpus/campaign the sample was drawn from.
6. Repeat step 2–5 whenever the judge model or `judge.prompt_version` changes — an agreement figure
   is only valid for the exact (model, prompt version) pair it was measured against.

## Current sample

Generated 2026-09-09 from the [smoke campaign](operations.md#smoke-campaign-results) described in
`docs/operations.md`, judge `deepseek/deepseek-v4-flash` at `prompt_version: v1`.

- Sample directory: `docs/judge-validation/sample/` (`sample-000.json`, `sample-001.json`,
  `manifest.json`).
- **`n = 2`.** The smoke corpus has exactly one reviewed bug (`lp-2012647`) and, of the 7 smoke-run
  attempts, only 2 reached `outcome: ok` and were therefore judge-scored at all (the rest scored
  zero on `no_answer`/`harness_error` without ever reaching the judge — see
  [operations.md](operations.md)). This is far below the 30–50 run target in plan.md §7.4 and is
  **not** a valid agreement measurement; it exists only to prove the `sample`/`agreement` commands
  work end to end, exactly as the accompanying smoke campaign is a pipeline check, not a capability
  result (see [methodology.md's limitations section](methodology.md#limitations)).

### TODO — maintainer action required

- [ ] Hand-grade `docs/judge-validation/sample/sample-000.json` and `sample-001.json`
      (`human_root_cause_score`, `human_fix_score`).
- [ ] Run `segbench grade agreement --sample-dir docs/judge-validation/sample` and paste the
      resulting exact-match/within-one/κ numbers into the **Results** section below.
- [ ] Once the corpus has grown past a handful of reviewed bugs, regenerate a real 30–50-run sample
      (`segbench grade sample --n 40`) spanning the actual score range — a 2-run sample cannot
      speak to whether the rubric holds up across the diagnosis-quality spectrum plan.md asks for.
- [ ] If agreement comes back poor (low κ, or a systematic direction such as the judge being
      consistently more generous than the human), **first** propose specific rewording of the
      `root_cause_score`/`fix_score` rubric bullets in `judge_prompt_v1.md` (e.g. tightening what
      counts as "correct mechanism" for a 2 vs a 3) and re-sample against the reworded prompt as
      `prompt_version: v2`. Only swap the judge model itself if a reworded rubric still fails to
      agree — plan.md §7.4 and CLAUDE.md are explicit that the rubric is the default suspect, not
      the model.

## Results

_Not yet filled in — pending the maintainer's hand-grading pass above. This section is the
permanent record once it is: date, sample size, judge model/prompt version, exact-match rate,
within-one rate, Cohen's κ (root cause axis, fix axis, pooled), and a one-paragraph note on any
rubric change made in response._
