# v6 onboarding for verified new installations

The Worker keeps existing setup and broker behavior when `V6_NEW_INSTALL_ONBOARDING` is absent. The new caller handles non-draft pull requests from the same repository. Decide which manual, comment, fork, and uninstall behavior the new default should offer before expanding enrollment.

## Enrollment evidence

A maintainer records positive first-time evidence for the GitHub App installation and every selected repository. GitHub can send `installation.created` on reinstall. A recent timestamp or absent files alone is insufficient. Existing, reinstalled, or historically uncertain repositories stay on their current path.

The Worker checks the GitHub webhook MAC, the exact delivery ID and body digest, the installation ID and creation time, and the repository IDs in the initial selection. It rechecks the repository ID and fork status before proposing the caller. It holds for existing setup files, earlier setup PRs, and incomplete setup history. A signed continuation carries the same approved selection across retries. The ordinary deterministic setup PR stores the signed enrollment receipt. The Worker adds no storage and the PR remains the sole recovery ledger.

The optional `V6_NEW_INSTALL_ONBOARDING` variable is a bounded JSON object with these fields.

| Field | Value |
| --- | --- |
| `app_id` | Numeric ID of the configured GitHub.com App |
| `setup_enabled` | Explicit boolean for new setup proposals, defaults to false |
| `start_at` | Approved UTC cutoff as `YYYY-MM-DDTHH:MM:SSZ` |
| `tag_object`, `source_sha` | Exact signed annotated `v6` object and its distinct peeled release commit |
| `enrollments` | Records with `installation_id`, `repository_id`, lowercase `repository`, `delivery_id`, and `body_sha256` of the original authentic payload |
| Optional `enrollments[].activation` | Object containing the emitted `receipt` and optionally the observed UTC `fenced_at` timestamp |

Keep the record identities and ref pair stable while an enrollment is active. An absent or unverified enrollment does not grant v6 access. Use the preexisting v6 drain procedure before removing an activation or changing its identity. Keep enrollment records scoped to specific verified installations and avoid overlap with the separate pilot list.

## Release and rollout sequence

1. Qualify the Worker candidate in trusted main and build its release through the existing pipeline. Keep `PUBLIC_WORKFLOW_TAG=v5`.
2. Separately verify the public `review-sensei-v6-pilot.yml` callable and its pinned package source. The already prepared signed `v6` tag can point to the published `0.9.0` / `0.3.35` source that contains this callable if it passes exact callable qualification. Publish it as a new ref only after authorization and an absence check. If the callable needs changes, qualify and publish a fresh immutable release before selecting it for `v6`. Existing package tags keep their original objects.
3. Deploy the qualified Worker with enrollment setup disabled. Verify production health, legacy v5 admission, and the signed v6 tag identity. Confirm the exact trusted first-time evidence and absence of earlier v5 enrollment or competing setup proposals for each intended repository before enabling proposals.
4. Enable setup for the approved records and review the bot-authored setup PR. Copy its signed enrollment receipt into that record's `activation` and omit `fenced_at`. This fences legacy v5 writes and keeps v6 lease admission closed. The proposal can stay unmerged during the fence and drain.
5. Verify the fence on the serving deployment and record the observation time as `fenced_at`. The Worker waits at least one hour from that observed fence before admitting v6. Confirm legacy grants and writes are settled, verify the exact tag again, and merge the approved setup PR. Qualify this narrowly scoped cohort before extending enrollment.
6. Set `setup_enabled=false` to pause fresh onboarding while preserving active fences. To stop an enrolled repository, use the coordinator's existing acquisition hold and drain outstanding slots before retiring its fence. A rename or reinstall that conflicts with an active enrollment needs operator reconciliation before re-enrollment.

An authoritative source for first-time attestations still needs an owner decision before automatic enrollment can be enabled. The Worker deliberately leaves historical uncertainty with v5.
