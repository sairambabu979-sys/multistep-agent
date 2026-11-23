import { describe, expect, it } from "vitest";
import { SseParser, can } from "@/lib/api";
import { parseDiff } from "@/lib/diff";
import { PIPELINE, progress } from "@/lib/phases";
import { transitions } from "@/components/workflow-graph";

describe("progress", () => {
  it("counts the stage a run is doing, from the first to the last", () => {
    expect(progress("created", null)).toMatchObject({ stage: 1, of: PIPELINE.length, percent: 0 });
    expect(progress("completed", null)).toMatchObject({ stage: PIPELINE.length, percent: 100 });
    const middle = progress("unit_testing", null);
    expect(middle.stage).toBe(PIPELINE.indexOf("unit_testing") + 1);
    expect(middle.note).toBeNull();
  });

  it("shows a paused or blocked run at the stage it will resume, and says why", () => {
    expect(progress("paused", "integration_testing")).toMatchObject({
      doing: "integration_testing",
      note: "paused by an operator",
    });
    expect(progress("blocked", "unit_testing").note).toBe("blocked: a person must fix the environment");
    expect(progress("blocked", null)).toMatchObject({ doing: "negotiation", note: "waiting for a ruling" });
  });

  it("sends rework back to static analysis, and never invents progress for a stopped run", () => {
    expect(progress("rework", null, 2)).toMatchObject({
      doing: "static_analysis",
      note: "rework: revision 2, every gate runs again",
    });
    expect(progress("failed", null)).toMatchObject({ stage: 0, percent: 0, note: "stopped: failed" });
  });
});

describe("roles", () => {
  it("are ordered: each can do what the ones below it can", () => {
    expect(can("approver", "operator")).toBe(true);
    expect(can("operator", "approver")).toBe(false);
    expect(can(undefined, "viewer")).toBe(false);
  });
});

describe("parseDiff", () => {
  it("numbers lines on both sides and counts changes per file", () => {
    const [file, created] = parseDiff(
      [
        "diff --git a/src/a.py b/src/a.py",
        "index 1..2 100644",
        "--- a/src/a.py",
        "+++ b/src/a.py",
        "@@ -1,3 +1,3 @@",
        " keep",
        "-old",
        "+new",
        " tail",
        "diff --git a/b.py b/b.py",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/b.py",
        "@@ -0,0 +1 @@",
        "+x = 1",
      ].join("\n"),
    );
    expect(file).toMatchObject({ path: "src/a.py", status: "modified", additions: 1, deletions: 1 });
    expect(file?.lines.map((l) => [l.kind, l.oldNumber, l.newNumber])).toEqual([
      ["meta", null, null],
      ["context", 1, 1],
      ["del", 2, null],
      ["add", null, 2],
      ["context", 3, 3],
    ]);
    expect(created).toMatchObject({ path: "b.py", status: "added", additions: 1 });
  });

  it("is empty for an empty patch", () => {
    expect(parseDiff("")).toEqual([]);
  });
});

describe("SseParser", () => {
  it("assembles frames across chunk boundaries and skips comments", () => {
    const parser = new SseParser();
    expect(parser.push("id: 1\nevent: run_sta")).toEqual([]);
    expect(parser.push('rted\ndata: {"seq":1}\n\n: still waiting\n\nevent: end\ndata: {}\n\n')).toEqual([
      { id: "1", event: "run_started", data: '{"seq":1}' },
      { event: "end", data: "{}" },
    ]);
  });

  it("joins multi-line data", () => {
    expect(new SseParser().push("data: a\ndata: b\n\n")).toEqual([{ data: "a\nb" }]);
  });
});

describe("transitions", () => {
  it("reads the path a run took from its timeline", () => {
    expect(
      transitions([
        "  1  START -> CREATED  RunStarted  [admit_run]",
        "  2  CREATED -> VALIDATING  RunAdmitted  [validate_request]",
        " 17  HUMAN_APPROVAL -> COMPLETED  ReleasePublished  [publish_outcome]",
      ]),
    ).toEqual([
      ["created", "validating"],
      ["human_approval", "completed"],
    ]);
  });
});
