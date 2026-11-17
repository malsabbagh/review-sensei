"""Qualify current review, command and reply lifecycles against installed wheels."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from packaging_guard import assert_distribution_import, is_disarmed  # noqa: E402

assert_distribution_import()

import review_sensei_github  # noqa: E402

if not is_disarmed() and not Path(
    review_sensei_github.__file__
).resolve().is_relative_to(Path(sys.prefix).resolve()):
    raise ImportError("current run qualification requires the installed hosted adapter")

import test_action_reply_entry as reply_contract  # noqa: E402
import test_action_run as command_contract  # noqa: E402
import test_review_run as review_contract  # noqa: E402
import test_review_run_rescan as rescan_contract  # noqa: E402


class InstalledReviewRunEntryTests(review_contract.ReviewRunEntryTests):
    pass


class InstalledConsecutiveReviewRunTests(review_contract.ConsecutiveReviewRunTests):
    pass


class InstalledCurrentCommandEntryTests(command_contract.CurrentCommandEntryTests):
    pass


class InstalledActionReplyEntryTests(reply_contract.ActionReplyEntryTests):
    pass


class InstalledRetainedEntryTests(rescan_contract.RetainedEntryTests):
    pass


# Explicit source-reviewed class isolation. Every original inherited method is
# retained. A changed helper revokes splitting; re-review rather than refreshing
# these canonical-LF pins blindly. This is not permission to skip native tests.
INSTALLED_CLASS_SHARD_CONTRACT = (
    "repeatable-socket-path-classes-v1",
    (
        (
            "InstalledActionReplyEntryTests",
            "test_action_reply_entry",
            "ActionReplyEntryTests",
            (
                "test_broader_publication_handoff_cannot_reuse_prior_human_completion",
                "test_generic_reply_runs_actual_provider_journal_publication_cleanup_and_terminal",
                "test_human_reassessment_keeps_twenty_retained_decisions_and_finishes_known_withholding",
                "test_local_model_discovery_uses_the_same_physical_caller_counter",
                "test_lost_reply_response_preserves_charge_and_does_not_duplicate",
                "test_overlapping_writer_refuses_before_provider_or_business_effect",
                "test_provider_unknown_keeps_action_pending_and_never_reinfers_on_stopped_successor",
                "test_unknown_cleanup_preserves_pending_action_after_one_visible_reply",
            ),
        ),
        (
            "InstalledConsecutiveReviewRunTests",
            "test_review_run",
            "ConsecutiveReviewRunTests",
            (
                "test_active_incomplete_changed_or_nonterminal_predecessor_refuses",
                "test_cached_success_cannot_replace_actual_mutation_or_readback_dispatch",
                "test_changed_part_or_wrong_producer_prevents_accounting_transition",
                "test_close_refusal_reports_terminal_pending_for_published_and_skipped",
                "test_completion_authentication_cannot_mutate_host_effects",
                "test_confirmed_resolution_readback_closes_without_refunding_lost_response",
                "test_current_lifecycle_reuses_thread_instead_of_legacy_supersession",
                "test_hard_kill_before_transaction_attachment_keeps_predecessor_capacity",
                "test_historical_completion_enrolls_fresh_base_and_preserves_exact_predecessor",
                "test_historical_fixture_keeps_canonical_diff_bytes_with_crlf_defaults",
                "test_historical_hold_root_capacity_refuses_before_any_archive_write",
                "test_historical_receipts_are_evidence_and_cannot_restore_enrollment_authority",
                "test_historical_transition_process_death_never_infers_nonentry_from_absence",
                "test_historical_transition_refuses_missing_effects_and_unfunded_tail_before_provider",
                "test_historical_transition_refuses_uncertain_original_terminal_write",
                "test_known_analysis_abort_closes_and_third_run_retains_chain",
                "test_legacy_inventory_requires_authenticated_migration_on_current_route",
                "test_local_resolution_settlement_cannot_replace_durable_caller_close",
                "test_only_confirmed_pre_entry_abort_can_close",
                "test_previous_reconciled_unknown_charge_is_retained_without_transfer",
                "test_published_unconfirmed_cleanup_reports_terminal_pending_and_refuses_next_run",
                "test_resolution_correlation_is_issued_scoped_one_use_and_does_no_io",
                "test_resolution_correlation_rejects_changed_serialized_requests",
                "test_retained_whole_root_keeps_decisions_and_baseline_and_reads_are_metered",
                "test_same_head_followup_closes_without_new_analysis_or_approval",
                "test_second_ordinary_run_preserves_terminal_previous_root",
                "test_separate_resolution_effects_keep_each_lost_response_charged",
                "test_settled_thread_does_not_settle_unrelated_unknown_mutation",
                "test_successor_cannot_infer_tail_completion_from_published_transaction",
                "test_uncertain_provider_failure_does_not_authorize_rollover",
                "test_unconfirmed_resolution_readbacks_and_other_unknown_effects_stay_open",
                "test_unfunded_readback_never_dispatches_or_settles_lost_resolution",
                "test_unknown_archive_or_transition_readback_never_replays_write",
                "test_unresolved_mutation_response_cannot_close_or_roll_over_run",
            ),
        ),
        (
            "InstalledCurrentCommandEntryTests",
            "test_action_run",
            "CurrentCommandEntryTests",
            (
                "test_foreign_or_obsolete_grant_never_initializes_a_native_writer",
                "test_historical_assessment_decision_is_distinct_and_cannot_clear_incomplete_coverage",
                "test_historical_assessment_refusal_never_falls_through_or_dismisses_a_fresh_finding",
                "test_historical_assessment_unknown_writes_preserve_pending_state_without_replay",
                "test_historical_author_shapes_refuse_without_business_effects",
                "test_historical_complete_empty_rescan_and_partial_decisions_keep_all_obligations",
                "test_historical_hold_is_not_removable_and_unverified_v6_cannot_approve",
                "test_informational_worker_fanout_never_changes_caller_charges",
                "test_inherited_successful_review_cannot_roll_over_an_unfinished_command",
                "test_lost_business_and_closure_responses_never_repeat_a_mutation",
                "test_old_selectors_and_mismatched_event_refuse_before_business_http",
                "test_overlap_and_stale_head_refuse_before_native_enrollment",
                "test_pause_and_status_complete_one_metered_action_without_inference",
                "test_put_requires_confirmation_tail_before_physical_dispatch",
                "test_short_historical_fingerprints_refuse_before_enrollment",
                "test_source_edit_and_owner_takeover_preserve_pending_intent_without_effect",
                "test_twenty_decisions_flow_through_current_command_and_finalization",
                "test_uncertain_review_put_keeps_decision_and_blocks_replay_or_approval",
                "test_unfunded_policy_refuses_before_any_http_attempt",
            ),
        ),
        (
            "InstalledRetainedEntryTests",
            "test_review_run_rescan",
            "RetainedEntryTests",
            (
                "test_failed_rescan_then_full_rescan_keeps_decisions_and_finishes_transaction",
                "test_fresh_rescan_preserves_authority_without_consuming_edited_old_prose",
                "test_original_transaction_archive_and_accounting_only_checkpoint_are_readable",
                "test_prior_reference_controls_refuse_without_replacing_decisions",
                "test_same_run_successor_rebuilds_prior_publication_reader_from_records",
            ),
        ),
        (
            "InstalledReviewRunEntryTests",
            "test_review_run",
            "ReviewRunEntryTests",
            (
                "test_actual_package_entry_owns_analysis_and_publication",
                "test_bookkeeping_failure_preserves_confirmed_native_receipt",
                "test_cancellation_before_publication_retains_pending_work",
                "test_changed_head_before_publication_preserves_pending_review",
                "test_current_local_review_preserves_budget_and_refuses_unfunded_native_tail",
                "test_diagnostic_files_cannot_replace_in_memory_publication",
                "test_duplicate_startup_cannot_open_another_process_allowance",
                "test_frozen_configuration_cannot_change_while_provider_runs",
                "test_hard_killed_pending_result_has_one_proven_successor_and_no_reinference",
                "test_interruption_after_confirmed_review_keeps_truthful_pending_output",
                "test_local_review_uses_same_owner_and_meters_credential_free_model_discovery",
                "test_operation_app_identity_comes_from_worker_and_cannot_fall_back",
                "test_policy_head_and_overlap_changes_refuse_before_provider",
                "test_provider_failure_and_handled_cancel_have_one_cleanup_owner",
                "test_rejected_worker_post_still_costs_one_caller_attempt",
                "test_required_fix_withholding_without_completion_proof_keeps_accounting_open",
                "test_stopped_same_run_successor_publishes_retained_result_without_inference",
                "test_undersized_configuration_stops_before_business_http",
                "test_unfunded_publication_stops_without_new_allowance",
                "test_unknown_checkpoint_patch_uses_original_funded_readback",
                "test_unknown_initial_post_is_confirmed_exactly_without_repost",
            ),
        ),
    ),
    (
        (
            "fake_github_http.py",
            "c78d99bea62cfdee2b7a66ed20afb29af38c36a18f58149cd66700157f8cdfbc",
        ),
        (
            "original_finish_interrupt.py",
            "9dc08c0eaa96ff76246bfb4904970fd9819c64c9dc2f731250b1628ccc401f1c",
        ),
        (
            "original_source_observation.py",
            "d3ca86402609a9d8b8f48df0d46b322f6b0c70f9a1a6c08fe11398e0a34cfc5b",
        ),
        (
            "packaging_guard.py",
            "5cfa7381343bd8a9d6d972f32d465771c159cfa26aaf0f3a8cd692f6fc430846",
        ),
        (
            "session_fixture_clock.py",
            "3078625136fac0bc1c543e4b26954d8753ae2119b455df9397a67aa8ae8d0235",
        ),
        (
            "test_action_reply_entry.py",
            "4e79c17a218a1357681a3aff1a0c99d2e35ad5edfcdd3b9f5ffb7fee92217fb9",
        ),
        (
            "test_action_run.py",
            "d704283ef492fe3daefaa3eb4797fdfa95dfbfba03c1d1d57c850307177109c7",
        ),
        (
            "test_api_work_lifecycle.py",
            "1634e38f532b05e4cf90c4c7d8332b9b3b2c7e1addd096127a2dfa48605cb61a",
        ),
        (
            "test_api_work_persistence.py",
            "0bfd6c612de9916abdd45464592fecbbc4a072588bf1176b7143aab84024ed7c",
        ),
        (
            "test_api_work_public_review.py",
            "bffde26f4ac3aa61920f5f5068c10ad3f9430aa1af0a603e691a76b10fa520cf",
        ),
        (
            "test_review_run.py",
            "15729836401e58acb754def3921aadbd80b44333073a0ca63ecf16b18044c43e",
        ),
        (
            "test_review_run_rescan.py",
            "b351c460c5a90d7777403e28ca7237d80cf0b9a975936ab8a7b432efd30a0ca4",
        ),
        (
            "test_review_transaction.py",
            "b964f84dcaa688775c1e4befb3ea56c4b9a04d0e3394e52420853a3dedf3ecf5",
        ),
    ),
)


def setUpModule():
    # Cleanup registration precedes subsequent setup: all class/test/module
    # cleanups stay inside both protections, even if later setup fails.
    unittest.enterModuleContext(
        patch(
            "socket.socket.connect",
            side_effect=AssertionError("live network forbidden"),
        )
    )
    unittest.enterModuleContext(patch.dict(os.environ, {"PYTHONPATH": str(_TESTS)}))
