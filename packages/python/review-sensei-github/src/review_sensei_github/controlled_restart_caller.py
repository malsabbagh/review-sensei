"""Prepare an explicit manual launcher caller without changing installed setup."""

from __future__ import annotations

from .errors import GitHubSetupError
from .setup import DEFAULT_PUBLIC_WORKFLOW_TAG, _tagged_workflow


def restart_caller(tag: str = DEFAULT_PUBLIC_WORKFLOW_TAG) -> str:
    """Return reviewable source for the existing caller path; never install it."""
    caller = _tagged_workflow(tag)
    fields = (
        ("hosted_version", "Reserved exact later signed hosted release"),
        ("core_version", "Exact core release pinned by the later hosted package"),
        ("parent_directory", "Approved existing private retained parent directory"),
        ("runner", "Approved existing self-hosted runner label"),
    )
    inputs = "".join(
        f"      controlled_restart_{name}:\n        description: {description}\n"
        "        required: false\n        default: ''\n        type: string\n"
        for name, description in fields
    )
    anchor = "      review_remaining:\n"
    if caller.count(anchor) != 1:
        raise GitHubSetupError("restart caller input seam is unavailable")
    caller = caller.replace(anchor, inputs + anchor, 1)
    anchor = "      operation: ${{ needs.resolve-trigger.outputs.operation }}\n"
    forwarded = "".join(
        f"      controlled_restart_{name}: ${{{{ github.event_name == 'workflow_dispatch' && inputs.controlled_restart_{name} || '' }}}}\n"
        for name, _ in fields
        if name != "runner"
    )
    forwarded += "      hosted_runner: ${{ github.event_name == 'workflow_dispatch' && inputs.controlled_restart_hosted_version != '' && inputs.controlled_restart_runner || 'ubuntu-latest' }}\n"
    if caller.count(anchor) != 1:
        raise GitHubSetupError("restart caller invocation seam is unavailable")
    caller = caller.replace(anchor, anchor + forwarded, 1)
    # Fail before routing/checkout if any restart option is supplied without
    # the complete valid set. A failed resolver job skips its reusable caller;
    # partial intent must never fall through to an ordinary capability job.
    anchor = "      - name: Check out trusted trigger resolver\n"
    validation = "      - name: Validate controlled restart inputs\n        env:\n"
    validation += "".join(
        f"          RESTART_{name.upper()}: ${{{{ inputs.controlled_restart_{name} }}}}\n"
        for name, _ in fields
    )
    validation += r"""        run: |
          set -euo pipefail
          if [[ -z "$RESTART_HOSTED_VERSION$RESTART_CORE_VERSION$RESTART_PARENT_DIRECTORY$RESTART_RUNNER" ]]; then
            exit 0
          fi
          if [[ "$GITHUB_EVENT_NAME" != workflow_dispatch || "$GITHUB_RUN_ATTEMPT" != 1 || ! "$RESTART_HOSTED_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || ! "$RESTART_CORE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || ! "$RESTART_RUNNER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ || "$RESTART_PARENT_DIRECTORY" != /* || "$RESTART_PARENT_DIRECTORY" == *//* || "$RESTART_PARENT_DIRECTORY" == */./* || "$RESTART_PARENT_DIRECTORY" == */../* || "$RESTART_PARENT_DIRECTORY" == */. || "$RESTART_PARENT_DIRECTORY" == */.. || "$RESTART_PARENT_DIRECTORY" =~ [[:cntrl:]] ]]; then
            echo '::error::restart inputs require all four valid values on one manual first execution' >&2
            exit 1
          fi

"""
    if caller.count(anchor) != 1:
        raise GitHubSetupError("restart caller validation seam is unavailable")
    caller = caller.replace(anchor, validation + anchor, 1)
    # Explicit restart intent needs the authenticated read-only metadata path
    # before the hold; ordinary manual review keeps its routing-only shortcut.
    anchor = "          RECOVER_PUBLICATION: ${{ inputs.recover_publication }}\n"
    intent = " || ".join(
        f"inputs.controlled_restart_{name} != ''" for name, _ in fields
    )
    if caller.count(anchor) != 1:
        raise GitHubSetupError("restart caller metadata intent seam is unavailable")
    caller = caller.replace(
        anchor,
        anchor + "          CONTROLLED_RESTART_SCOPE: ${{ " + intent + " }}\n",
        1,
    )
    anchor = 'if [[ "$EVENT_NAME" == "workflow_dispatch" && "${RECOVER_PUBLICATION:-false}" != "true" ]]; then'
    if caller.count(anchor) != 1:
        raise GitHubSetupError("restart caller metadata routing seam is unavailable")
    caller = caller.replace(
        anchor,
        anchor.replace(
            " ]]; then", ' && "${CONTROLLED_RESTART_SCOPE:-false}" != "true" ]]; then'
        ),
        1,
    )
    # The existing trigger resolver already has its own explicit read-only
    # contents/pull-requests/issues permissions. Preserve that mapping exactly;
    # adding a second permissions key would produce an invalid workflow.
    return caller
