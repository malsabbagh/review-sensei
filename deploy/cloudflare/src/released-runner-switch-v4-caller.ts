import releasedRunnerSwitchV4CallerFixture from "../fixtures/released-v4-resolve-trigger-runner-switch.yml";

export const RELEASED_RUNNER_SWITCH_V4_SHA256 =
  "222c520f06ff3de44d57c5c4176ece68d0682e422c45df121c719438ec415f5e";

let releasedRunnerSwitchV4CallerBytesCache: string | undefined;

/** Return the byte-exact released setup-v4 caller bundled with the Worker. */
export function releasedRunnerSwitchV4CallerBytes(): string {
  if (releasedRunnerSwitchV4CallerBytesCache !== undefined) {
    return releasedRunnerSwitchV4CallerBytesCache;
  }
  releasedRunnerSwitchV4CallerBytesCache = releasedRunnerSwitchV4CallerFixture;
  return releasedRunnerSwitchV4CallerBytesCache;
}
