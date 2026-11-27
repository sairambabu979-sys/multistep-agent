import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Timeline } from "@/components/timeline";
import type { JournalEvent } from "@/lib/api";
import { refreshesFor } from "@/lib/run-events";

afterEach(cleanup);

describe("refreshesFor", () => {
  it("refetches the run and its timeline on every event", () => {
    for (const kind of ["run_started", "plan_produced", "pause_requested", "effect_failed"]) {
      expect(refreshesFor(kind)).toEqual(["run", "timeline"]);
    }
  });

  it("refetches the records only when a gate, the review or a ruling writes them", () => {
    for (const kind of [
      "static_analysis_failed",
      "unit_tests_passed",
      "integration_tests_failed",
      "security_review_passed",
      "review_produced",
      "revision_completed",
      "negotiation_round_completed",
      "arbitration_ruled",
      "human_ruled",
    ]) {
      expect(refreshesFor(kind)).toEqual(["run", "timeline", "records"]);
    }
  });

  it("refetches the patch and the PR only around the release", () => {
    for (const kind of ["release_verified", "release_verification_failed", "release_published"]) {
      expect(refreshesFor(kind)).toEqual(["run", "timeline", "records", "files"]);
    }
    expect(refreshesFor("plan_approved")).not.toContain("files");
  });
});

describe("Timeline", () => {
  const events: JournalEvent[] = [
    { seq: 1, kind: "run_started", event: { at: "2026-11-27T10:00:00+00:00" } },
    { seq: 2, kind: "plan_approved", event: { at: "2026-11-27T10:01:00+00:00", by: "person:ada" } },
  ];

  it("shows the events the page's stream delivered, and says it is live", () => {
    render(<Timeline events={events} status="live" />);
    expect(screen.getByText("Live: new events appear as they are journaled.")).toBeTruthy();
    expect(screen.getByText("plan_approved")).toBeTruthy();
    expect(screen.getByText("Event 2: plan_approved")).toBeTruthy();
  });

  it("says when the stream was lost, and that it is reconnecting", () => {
    render(<Timeline events={events} status="lost" />);
    expect(screen.getByText(/The stream was lost: reconnecting/)).toBeTruthy();
  });
});
