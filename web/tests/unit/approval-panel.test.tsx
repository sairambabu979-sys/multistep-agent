import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApprovalPanel } from "@/components/approval-panel";
import { SessionContext } from "@/components/auth";
import type { Api, Role, RunDetail } from "@/lib/api";

afterEach(cleanup);

function run(phase: string, pending: string[], overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: "r1",
    phase,
    outcome: null,
    seq: 5,
    pending,
    waiting_on_person: pending.some((p) => p.startsWith("await_")),
    driving: false,
    driver_error: null,
    question: null,
    state: {
      findings: [{ id: "c0-1", severity: "blocker", status: "open", raised_on: "x" }],
    } as unknown as RunDetail["state"],
    ...overrides,
  };
}

function renderAs(role: Role, detail: RunDetail) {
  const act = vi.fn(async () => ({ run: detail }));
  const api = { act } as unknown as Api;
  render(
    <SessionContext.Provider value={{ api, me: { name: "grace", role }, signOut: () => {} }}>
      <ApprovalPanel run={detail} onDone={() => {}} />
    </SessionContext.Provider>,
  );
  return act;
}

describe("ApprovalPanel", () => {
  it("lets an approver approve a release, and sends exactly that action", async () => {
    const act = renderAs("approver", run("human_approval", ["await_release_approval"], { question: "Ship it?" }));
    expect(screen.getByText("Ship it?")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Approve and publish" }));
    expect(act).toHaveBeenCalledWith("r1", "approve", {});
  });

  it("needs feedback before a release can be sent back", async () => {
    const act = renderAs("approver", run("human_approval", ["await_release_approval"]));
    const sendBack = screen.getByRole("button", { name: "Send back" });
    expect((sendBack as HTMLButtonElement).disabled).toBe(true);
    await userEvent.type(screen.getByLabelText("What to change (to send it back)"), "rename it");
    await userEvent.click(sendBack);
    expect(act).toHaveBeenCalledWith("r1", "reject", { feedback: "rename it" });
  });

  it("shows an operator no approval buttons, only what an operator may do", () => {
    renderAs("operator", run("human_approval", ["await_release_approval"]));
    expect(screen.queryByRole("button", { name: "Approve and publish" })).toBeNull();
    expect(screen.getByText("An approver must answer this release gate.")).toBeTruthy();
  });

  it("asks for a verdict on every open finding before a ruling can be sent", async () => {
    const act = renderAs("approver", run("blocked", ["await_human_ruling"]));
    const rule = screen.getByRole("button", { name: "Rule" });
    expect((rule as HTMLButtonElement).disabled).toBe(true);
    await userEvent.selectOptions(screen.getByRole("combobox"), "dismissed");
    await userEvent.click(rule);
    expect(act).toHaveBeenCalledWith("r1", "rule", { verdicts: { "c0-1": "dismissed" } });
  });

  it("offers a working run's operator pause and cancel, and a viewer nothing", () => {
    renderAs("operator", run("unit_testing", ["run_unit_tests"]));
    expect(screen.getByRole("button", { name: "Pause" })).toBeTruthy();
    cleanup();
    renderAs("viewer", run("unit_testing", ["run_unit_tests"]));
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Viewers can follow a run but not change it.")).toBeTruthy();
  });

  it("says when a run is over", () => {
    renderAs("admin", run("completed", []));
    expect(screen.getByText("This run is over: there is nothing left to decide.")).toBeTruthy();
  });
});
