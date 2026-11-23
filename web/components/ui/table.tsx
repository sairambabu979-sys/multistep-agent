import type { ReactNode } from "react";

export function Table({
  caption,
  head,
  children,
}: {
  caption: string;
  head: ReactNode[];
  children: ReactNode;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-border text-left text-muted">
            {head.map((cell, n) => (
              <th key={n} scope="col" className="px-2 py-2 font-medium">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Row({ children }: { children: ReactNode }) {
  return <tr className="border-b border-border last:border-0">{children}</tr>;
}

export function Cell({ children, mono = false }: { children: ReactNode; mono?: boolean }) {
  return <td className={`px-2 py-2 align-top ${mono ? "font-mono text-xs" : ""}`}>{children}</td>;
}
