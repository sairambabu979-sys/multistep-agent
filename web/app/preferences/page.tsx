"use client";

import { useSession } from "@/components/auth";
import { Problem } from "@/components/run-parts";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Tabs } from "@/components/ui/tabs";
import { Cell, Row, Table } from "@/components/ui/table";
import { useResource } from "@/lib/use-resource";

const TONE = { hard: "bad", soft: "waiting", info: "neutral", off: "neutral" } as const;

/** The rules every run works under: preferences with where each comes from, the security
 * policy that outranks them, and the skills each role carries. Changes are made with the
 * CLI (msa prefs, msa skills), where each is validated before it is written. */
export default function PreferencesStudio() {
  const { api } = useSession();
  const preferences = useResource(() => api.preferences(), "preferences");
  const skills = useResource(() => api.skills(), "skills");
  const policy = useResource(() => api.policy(), "policy");
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Preferences Studio</h1>
      <p className="text-sm text-muted">
        Read-only here. Change them with <code className="font-mono">msa prefs add|delete|export</code>{" "}
        and <code className="font-mono">msa skills add|delete|export</code>: every change is
        validated (conflicts, secrets, permissions) before it is saved.
      </p>
      <Tabs
        label="Rules"
        tabs={[
          {
            id: "preferences",
            title: "Preferences",
            body: (
              <Card>
                <Problem message={preferences.error} />
                <Table caption="Preferences in force" head={["Id", "Enforcement", "Scope", "Rule", "From"]}>
                  {(preferences.data ?? []).map((p) => (
                    <Row key={p.id}>
                      <Cell mono>{p.id}</Cell>
                      <Cell>
                        <Badge tone={TONE[p.enforcement as keyof typeof TONE] ?? "neutral"}>{p.enforcement}</Badge>
                      </Cell>
                      <Cell>
                        {p.scope.language ?? "any language"}
                        {p.scope.paths.length ? `, ${p.scope.paths.join(", ")}` : ""}
                      </Cell>
                      <Cell>{p.rule}</Cell>
                      <Cell>{p.origin}</Cell>
                    </Row>
                  ))}
                </Table>
              </Card>
            ),
          },
          {
            id: "policy",
            title: "Security policy",
            body: (
              <Card>
                <Problem message={policy.error} />
                {policy.data && (
                  <div className="space-y-4 text-sm">
                    <p className="text-muted">Source: {policy.data.source}. It outranks every preference and skill.</p>
                    <h2 className="font-semibold">Locked preferences</h2>
                    {policy.data.locked.length === 0 ? (
                      <p className="text-muted">None.</p>
                    ) : (
                      <ul className="list-disc pl-5">
                        {policy.data.locked.map((lock) => (
                          <li key={lock.id}>
                            <span className="font-mono">{lock.id}</span> stays <strong>{lock.enforcement}</strong>
                            {lock.reason ? `: ${lock.reason}` : ""}
                          </li>
                        ))}
                      </ul>
                    )}
                    <h2 className="font-semibold">Forbidden tools</h2>
                    {Object.keys(policy.data.forbidden_tools).length === 0 ? (
                      <p className="text-muted">None.</p>
                    ) : (
                      <ul className="list-disc pl-5">
                        {Object.entries(policy.data.forbidden_tools).map(([role, tools]) => (
                          <li key={role}>
                            {role}: <span className="font-mono">{tools.join(", ")}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </Card>
            ),
          },
          {
            id: "skills",
            title: "Skills",
            body: (
              <Card>
                <Problem message={skills.error} />
                {skills.data?.length === 0 ? (
                  <p className="text-sm text-muted">No skills yet: add one with msa skills add.</p>
                ) : (
                  <Table caption="Skills" head={["Skill", "Roles", "Tools", "Constraints", "Lineage"]}>
                    {(skills.data ?? []).map((s) => (
                      <Row key={s.id}>
                        <Cell mono>
                          {s.id}@{s.version}
                        </Cell>
                        <Cell>{s.roles.join(", ")}</Cell>
                        <Cell mono>{s.allowed_tools ? s.allowed_tools.join(", ") : "any"}</Cell>
                        <Cell>{s.constraints.join("; ") || "-"}</Cell>
                        <Cell mono>{s.lineage.join(" ← ")}</Cell>
                      </Row>
                    ))}
                  </Table>
                )}
              </Card>
            ),
          },
        ]}
      />
    </div>
  );
}
