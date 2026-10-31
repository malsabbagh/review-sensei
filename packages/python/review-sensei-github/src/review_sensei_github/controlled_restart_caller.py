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
    # The trigger resolver reads PR metadata only. It must not acquire OIDC
    # while the downstream reusable writer is still waiting for its hold.
    caller = caller.replace(
        "  resolve-trigger:\n",
        "  resolve-trigger:\n    permissions:\n      contents: read\n      pull-requests: read\n",
        1,
    )
    return caller
