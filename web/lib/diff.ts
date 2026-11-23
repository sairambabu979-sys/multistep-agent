// A unified diff (git's), split into files and hunks for display.

export type LineKind = "add" | "del" | "context" | "meta";

export interface DiffLine {
  kind: LineKind;
  text: string;
  oldNumber: number | null;
  newNumber: number | null;
}

export interface DiffFile {
  path: string;
  status: "added" | "deleted" | "modified";
  additions: number;
  deletions: number;
  lines: DiffLine[];
}

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let oldLine = 0;
  let newLine = 0;
  for (const raw of text.split("\n")) {
    if (raw.startsWith("diff --git ")) {
      const match = /^diff --git a\/(.+) b\/(.+)$/.exec(raw);
      file = { path: match?.[2] ?? raw, status: "modified", additions: 0, deletions: 0, lines: [] };
      files.push(file);
      continue;
    }
    if (file === null) continue;
    if (raw.startsWith("new file mode")) file.status = "added";
    else if (raw.startsWith("deleted file mode")) file.status = "deleted";
    if (raw.startsWith("--- ") || raw.startsWith("+++ ") || raw.startsWith("index ")) continue;
    if (/^(new|deleted) file mode|^similarity|^rename|^old mode|^new mode/.test(raw)) continue;
    const hunk = HUNK.exec(raw);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      file.lines.push({ kind: "meta", text: raw, oldNumber: null, newNumber: null });
    } else if (raw.startsWith("+")) {
      file.additions += 1;
      file.lines.push({ kind: "add", text: raw.slice(1), oldNumber: null, newNumber: newLine++ });
    } else if (raw.startsWith("-")) {
      file.deletions += 1;
      file.lines.push({ kind: "del", text: raw.slice(1), oldNumber: oldLine++, newNumber: null });
    } else if (raw.startsWith(" ")) {
      file.lines.push({
        kind: "context",
        text: raw.slice(1),
        oldNumber: oldLine++,
        newNumber: newLine++,
      });
    }
  }
  return files;
}
