# Leaderboard records and qualification

**2026-02 · Beads only · term_v3**

The [class leaderboard](https://phoniness-reenact-veggie.ngrok-free.dev/) is active for official bead attempts and final representative selection. Instructor qualification records are kept outside classroom standings. Activation uses the verified instructor-PC evidence. GTX 1660 SUPER runtime and memory remain unmeasured. Process liquid and sand are locked. The three active rules are `ap2026-easy-beads-term-v3`, `ap2026-medium-beads-term-v3` and `ap2026-hard-beads-term-v3`, shown as Lower, Moderate and Higher complexity.

Use the [run and submit guide](EVALUATION_AND_SUBMISSION.md) for exact review, retry, history and final-selection commands. Students use the existing Python environment and an instructor-issued team token kept outside Git. No student Docker, ngrok or VPN installation is required.

## Attempt history and representatives

Every completed full bead review automatically records success or failure. CPU planning, empty-cup replay and short practice remain local. An acknowledged upload confirms receipt, not physical success. An unavailable connection or token leaves a local queue that can be retried without rerunning Genesis. Retrying an acknowledged attempt does not create another record.

The live comparison uses each team's latest successful run under the same rule. A later failure stays in history and does not replace that success. The official comparison uses each team's explicitly selected successful run, bound to the actual student files in its full 40-character Git commit. Final selection is open for these active rules. The instructor freezes those selections for assessment.

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

An installed rule or an uploaded staging result does not activate competition. `scoring_enabled: false` identifies staging. Activation is a separate instructor operation bound to the exact rule ID, digest and reviewed evidence. The instructor authorized current-PC activation while retaining the explicit GTX 1660 SUPER measurement limitation. `scoring_enabled: true` identifies an active rule. Activation is recorded outside the immutable rule file, whose original `qualified: false` field remains unchanged.

Preserve the installed rule bytes and digest. A changed map, evaluator, acceptance condition, material, protected source or runtime contract requires a separately qualified version. Later liquid or sand work cannot rewrite bead rules or inherit bead qualification.

Operator database locations, tokens, provisioning commands and backup procedures remain in private instructor records. Preserve the original classroom data volume and the separate qualification database. Before an authorized service change, use the established private backup procedure and verify restoration with the same teams, rules and attempt history. Keep qualification records separate from official classroom standings. This page does not authorize activation, database replacement or publication of private operating data.

Leaderboard records do not replace the required presentation or the LMS repository URL and full commit ID.


## Results and design privacy

The public board shares team labels, ranks and result metrics. Record identifiers and source hashes identify versions. It does not expose robot layouts, handoff coordinates, joint paths or student source code. Detailed attempts and history require the owning team's token. Other teams cannot read those records. The instructor's issue view requires instructor authentication.

The course starter is public. Use one private working and submission repository per team and grant access to teammates and **SSUMechE**. Keep design files, saved paths and motion previews private. Each member commits and pushes their own work using their own GitHub account. Explain contributions in the presentation using the actual shared-repository commit and push history.

## Course-code updates

The [student update procedure](EVALUATION_AND_SUBMISSION.md#keep-supplied-code-current) applies an exact instructor release without replacing student work or upload queues. Update status and rollback are separate from `retry`, which only resends records. A pending attempt retains its original source and rule identity. An update cannot turn an earlier failure into a success or attach new code to an old result.

The instructor resolves reported issues and publishes tested releases. A change to protected evaluation source must be paired with a separately reviewed rule version. The updater rejects a changed digest under an existing rule ID. New rule IDs must already be active with matching digests at the class server before the update is applied. PyTorch and Genesis remain fixed.
