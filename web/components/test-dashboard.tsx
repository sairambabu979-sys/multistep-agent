"use client";

import { Badge } from "@/components/ui/badge";
import { Cell, Row, Table } from "@/components/ui/table";
import type { RecordRow } from "@/lib/api";

function Verdict({ passed, text }: { passed: boolean; text: string }) {
  return <Badge tone={passed ? "good" : "bad"}>{text}</Badge>;
}

const short = (value: unknown) => (typeof value === "string" ? value.slice(0, 12) : "-");

/** Every gate's runs on every tree, in order: static analysis, unit and integration tests,
 * the security review, and release verification. */
export function TestDashboard({ records }: { records: Record<string, RecordRow[]> }) {
  const rows: { gate: string; tree: unknown; passed: boolean; text: string; report: unknown }[] = [];
  for (const r of records.verifications ?? []) {
    const failed = Array.isArray(r.failed) ? r.failed.join(", ") : "";
    rows.push({ gate: "Static analysis", tree: r.tree, passed: r.passed === true, text: r.passed ? "passed" : `failed: ${failed}`, report: r.report });
  }
  for (const r of records.tests ?? []) {
    rows.push({ gate: "Unit tests", tree: r.tree, passed: r.verdict === "passed", text: r.verdict === "passed" ? "passed" : `failed: ${String(r.kind)}`, report: r.report });
  }
  for (const r of records.integrations ?? []) {
    const text =
      r.verdict !== "passed" ? `failed: ${String(r.kind)}` : r.ran === false ? "nothing to run" : r.isolated ? "passed, no network" : "passed";
    rows.push({ gate: "Integration tests", tree: r.tree, passed: r.verdict === "passed", text, report: r.report });
  }
  for (const r of records.security ?? []) {
    const text = r.verdict === "passed" ? `passed${r.advisory ? `, ${String(r.advisory)} advisory` : ""}` : `failed: ${String(r.blocking)} blocking`;
    rows.push({ gate: "Security review", tree: r.tree, passed: r.verdict === "passed", text, report: r.report });
  }
  for (const r of records.releases ?? []) {
    rows.push({ gate: "Release", tree: r.tree, passed: r.verdict !== "failed", text: String(r.verdict), report: r.report ?? r.bundle });
  }
  if (rows.length === 0) return <p className="text-sm text-muted">No gate has run yet.</p>;
  return (
    <Table caption="Gate results" head={["Gate", "Tree", "Result", "Report"]}>
      {rows.map((row, n) => (
        <Row key={n}>
          <Cell>{row.gate}</Cell>
          <Cell mono>{short(row.tree)}</Cell>
          <Cell>
            <Verdict passed={row.passed} text={row.text} />
          </Cell>
          <Cell mono>{short(row.report)}</Cell>
        </Row>
      ))}
    </Table>
  );
}
