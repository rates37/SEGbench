# segbench judge rubric v1

You are grading one agent's diagnosis of a real software defect against the confirmed ground
truth. You are not shown the transcript, the bug tracker, or any other context — only the four
fields below. Do not assume anything not stated in them.

## Ground truth root cause

{ground_truth_root_cause}

## Also-acceptable framings of the root cause

{also_acceptable_root_causes}

## Agent's stated root cause

{agent_root_cause}

## Agent's proposed fix

{agent_proposed_fix}

## Scoring

Score `root_cause_score` from 0-3:

- 0 — wrong, or so vague it identifies nothing.
- 1 — right general area, wrong mechanism.
- 2 — correct mechanism, imprecise or missing a necessary condition.
- 3 — correct mechanism, would let an engineer go straight to the fix.

Treat the root cause as correct if it matches the ground truth OR any of the also-acceptable
framings.

Score `fix_score` from 0-3:

- 0 — no fix, or a harmful one.
- 1 — addresses the symptom only.
- 2 — plausible fix in the right place.
- 3 — substantively the fix that was actually made.

Set `contradicts_ground_truth` to `true` if the agent's root cause actively asserts something
that is inconsistent with the ground truth (not merely vague or incomplete).

Set `unsupported_specifics` to the count of confident, checkable, specific claims in the agent's
answer that are simply wrong — a named function that does not exist, a version that does not
exist, a mechanism that contradicts the ground truth with false specificity. This is a
hallucination signal, separate from correctness.

## Output

Respond with **strict JSON only**, no markdown fences, no prose outside the object, matching
exactly this shape:

```json
{{
  "root_cause_score": 0,
  "root_cause_rationale": "...",
  "fix_score": 0,
  "fix_rationale": "...",
  "contradicts_ground_truth": false,
  "unsupported_specifics": 0
}}
```
