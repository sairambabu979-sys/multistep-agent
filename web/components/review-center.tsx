"use client";

import { Cell, Row, Table } from "@/components/ui/table";
import type { RecordRow } from "@/lib/api";

interface FindingRecord {
  id: string;
  severity: string;
  admitted: boolean;
  reason?: string;
  finding: { kind: string; claim: string; location: { path: string; start_line: number; end_line: number } };
}

/** Every finding, what was moved on it, and what settled it (ADR-014). */
export function ReviewCenter({
  findings,
  rounds,
  rulings,
}: {
  findings: RecordRow[];
  rounds: RecordRow[];
  rulings: RecordRow[];
}) {
  if (findings.length === 0) {
    return <p className="text-sm text-muted">No findings were raised in this run.</p>;
  }
  const settled = new Map<string, string>();
  for (const row of [...rounds, ...rulings]) {
    if (typeof row.finding === "string" && typeof row.verdict === "string" && row.verdict) {
      const how = typeof row.rung === "string" ? row.rung : `round ${String(row.round)}`;
      settled.set(row.finding, `${row.verdict} (${how})`);
    }
  }
  const moves = rounds.filter((r) => typeof r.author === "string");
  const accepted = moves.filter((r) => r.author === "accept").length;
  return (
    <div className="space-y-4">
      <Table caption="Findings" head={["Finding", "Severity", "Kind", "Where", "Claim", "Outcome"]}>
        {(findings as unknown as FindingRecord[]).map((f) => (
          <Row key={f.id}>
            <Cell mono>{f.id}</Cell>
            <Cell>{f.severity}</Cell>
            <Cell>{f.finding.kind}</Cell>
            <Cell mono>
              {f.finding.location.path}:{f.finding.location.start_line}-{f.finding.location.end_line}
            </Cell>
            <Cell>{f.finding.claim}</Cell>
            <Cell>
              {!f.admitted
                ? `not admitted: ${f.reason ?? ""}`
                : f.severity === "minor" || f.severity === "nit"
                  ? "reported, not negotiated"
                  : (settled.get(f.id) ?? "open")}
            </Cell>
          </Row>
        ))}
      </Table>
      {moves.length > 0 && (
        <p className="text-sm text-muted">
          Negotiation: the author accepted {accepted} of {moves.length} move(s).
        </p>
      )}
    </div>
  );
}
