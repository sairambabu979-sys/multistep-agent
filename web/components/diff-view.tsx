"use client";

import { parseDiff } from "@/lib/diff";

export function DiffView({ patch }: { patch: string }) {
  const files = parseDiff(patch);
  if (files.length === 0) return <p className="text-sm text-muted">The patch is empty.</p>;
  const added = files.reduce((n, f) => n + f.additions, 0);
  const removed = files.reduce((n, f) => n + f.deletions, 0);
  return (
    <div className="space-y-3">
      <p className="text-sm">
        {files.length} file(s), <span className="text-good">+{added}</span>{" "}
        <span className="text-bad">−{removed}</span>
      </p>
      {files.map((file) => (
        <details key={file.path} open={files.length <= 5} className="rounded-md border border-border">
          <summary className="cursor-pointer bg-surface px-3 py-2 font-mono text-sm">
            {file.path}{" "}
            <span className="text-muted">
              ({file.status}, <span className="text-good">+{file.additions}</span>{" "}
              <span className="text-bad">−{file.deletions}</span>)
            </span>
          </summary>
          <table className="w-full border-collapse font-mono text-xs">
            <caption className="sr-only">Changes to {file.path}</caption>
            <tbody>
              {file.lines.map((line, n) => (
                <tr
                  key={n}
                  className={
                    line.kind === "add" ? "bg-add-bg" : line.kind === "del" ? "bg-del-bg" : line.kind === "meta" ? "bg-surface text-muted" : ""
                  }
                >
                  <td className="w-12 select-none px-2 text-right text-muted">{line.oldNumber ?? ""}</td>
                  <td className="w-12 select-none px-2 text-right text-muted">{line.newNumber ?? ""}</td>
                  <td className="w-4 select-none" aria-hidden="true">
                    {line.kind === "add" ? "+" : line.kind === "del" ? "−" : ""}
                  </td>
                  <td className="whitespace-pre-wrap break-all px-2">
                    <span className="sr-only">{line.kind === "add" ? "added: " : line.kind === "del" ? "removed: " : ""}</span>
                    {line.text}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ))}
    </div>
  );
}
