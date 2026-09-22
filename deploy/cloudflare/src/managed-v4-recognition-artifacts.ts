import historicalV4UninstallFixture from "../fixtures/historical-v4-uninstall.yml";
import mergeFocusedV4CallerFixture from "../fixtures/merge-focused-v4-caller.yml";
import mergeFocusedV4ConfigFixture from "../fixtures/merge-focused-v4-config.yml";

export const MERGE_FOCUSED_V4_CALLER_SHA256 =
  "ce69d43119e2573853545edf90e595cda93fc0f4a018ede87aa5b604f6ab7742";
export const MERGE_FOCUSED_V4_CONFIG_SHA256 =
  "2a81144f0c22d295b8be49474979f9fa073271b3c763da302ba4f0fcf68cefb0";
export const HISTORICAL_V4_UNINSTALL_SHA256 =
  "e349ede8fa3eca6a303a04d688679b1abc41d13c31ba0d10651c376e9c77a6ec";
export const MERGE_FOCUSED_V4_CALLER_TAG_MARKER =
  "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5";

/**
 * Frozen bytes of artifacts that already exist in installations. Recognition
 * must not be derived from the live templates: an edit to the current setup-v5
 * bytes must never change what these historical artifacts look like. The
 * Python setup recognizer loads the same bytes from its packaged fixtures.
 */
export function mergeFocusedV4CallerBytes(): string {
  return mergeFocusedV4CallerFixture;
}

export function mergeFocusedV4ConfigBytes(): string {
  return mergeFocusedV4ConfigFixture;
}

export function historicalV4UninstallBytes(): string {
  return historicalV4UninstallFixture;
}
