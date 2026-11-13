# Leaderboard records and qualification

**2026-02 · Beads only · term_v3**

The [class leaderboard](https://phoniness-reenact-veggie.ngrok-free.dev/) currently supports qualification records. Production competition activation remains pending the complete release checks. Process liquid and sand are locked. The three candidate rules are `ap2026-easy-beads-term-v3`, `ap2026-medium-beads-term-v3` and `ap2026-hard-beads-term-v3`, shown as Lower, Moderate and Higher complexity.

Use the [run and submit guide](EVALUATION_AND_SUBMISSION.md) for exact review, retry, history and final-selection commands. Students use the existing Python environment and an instructor-issued team token kept outside Git. No student Docker, ngrok or VPN installation is required.

## Attempt history and representatives

Every completed full bead review automatically records success or failure. CPU planning, empty-cup replay and short practice remain local. An acknowledged upload confirms receipt, not physical success. An unavailable connection or token leaves a local queue that can be retried without rerunning Genesis. Retrying an acknowledged attempt does not create another record.

The live comparison uses each team's latest successful run under the same rule. A later failure stays in history and does not replace that success. The official comparison uses each team's explicitly selected successful run, bound to the actual student files in its full 40-character Git commit. Final selection opens after qualification activation. The instructor freezes those selections for assessment.

The server validates submitted records and does not execute student Python or Genesis. A failed attempt can remain in history but cannot enter the successful competition population. Keep the original native output for review. Source matching enforces the supplied-code boundary without proving execution on a student-controlled computer.

## Distance and time ranking

For each complexity and rule, compare one successful representative per team. `D` is actual cup-centre travel while held, summed over physical outer steps and rounded once to the nearest whole millimetre, with half a millimetre rounded up. `T` is the verified task-completion time in integer `0.0025 s` ticks. The initial 200 preparation ticks and zero-time donor returns are excluded from `T`. Subsequent physical approach, carrying, handling, waits and required settling are included.

Normalize each metric using that comparison population:

```text
Dn = (D - Dmin) / (Dmax - Dmin)
Tn = (T - Tmin) / (Tmax - Tmin)
C  = 0.5 * Dn + 0.5 * Tn
```

A metric with no variation contributes zero. The other weight remains 0.5. Lower `C` ranks higher. Exact rational arithmetic determines ties, with shared competition ranks such as 1, 1, 3. Upload order and computer runtime do not break ties. New representatives can change the population's ranges and displayed composites. See [assessment and competition](SCORING.md) for carry credit, competition adjustments and presentation assessment.

## Qualification and private operations

An installed rule or an uploaded staging result does not activate competition. `scoring_enabled: false` identifies staging. Qualification activation is a separate instructor operation bound to complete release evidence for the exact rule ID and digest. It records activation outside the immutable rule file. The candidate's `qualified: false` field is not edited to manufacture approval.

Preserve the installed rule bytes and digest. A changed map, evaluator, acceptance condition, material, protected source or runtime contract requires a separately qualified version. Later liquid or sand work cannot rewrite bead rules or inherit bead qualification.

Operator database locations, tokens, provisioning commands and backup procedures remain in private instructor records. Preserve the original classroom data volume and the separate qualification database. Before an authorized service change, use the established private backup procedure and verify restoration with the same teams, rules and attempt history. Keep qualification records separate from official classroom standings. This page does not authorize activation, database replacement or publication of private operating data.

Leaderboard records do not replace the required presentation or the LMS repository URL and full commit ID.
