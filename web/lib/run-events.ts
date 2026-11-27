/** What a journaled event can change on a run's page, so the page refetches only that. */
export type Refresh = "run" | "timeline" | "records" | "files";

/** Events whose executors write the run's records: the gates, the review, the rulings. */
const RECORDING = [
  "static_analysis_",
  "unit_tests_",
  "integration_tests_",
  "security_review_",
  "review_produced",
  "revision_completed",
  "negotiation_round_",
  "arbitration_ruled",
  "human_ruled",
  "release_",
];

/** Every event moves the run's state and adds a transition to its timeline. Only a gate,
 * the review or a ruling writes records, and only the release writes the patch and the PR. */
export function refreshesFor(kind: string): Refresh[] {
  const refreshes: Refresh[] = ["run", "timeline"];
  if (RECORDING.some((prefix) => kind.startsWith(prefix))) refreshes.push("records");
  if (kind.startsWith("release_")) refreshes.push("files");
  return refreshes;
}

/** While the stream is down, a slow poll keeps the page roughly current. */
export const FALLBACK_POLL_MS = 15_000;
/** How long the page waits to gather a burst of events into one refetch. */
export const COALESCE_MS = 250;
