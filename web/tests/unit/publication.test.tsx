import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { PublicationFacts } from "@/components/run-parts";
import type { Publication } from "@/lib/api";

afterEach(cleanup);

function publication(overrides: Partial<Publication> = {}): Publication {
  return {
    mode: "pr",
    branch: "aegis/run-1",
    commit: "0123456789abcdef0123456789abcdef01234567",
    remote: "origin",
    remote_branch: "aegis/run-1",
    pr_url: "https://github.com/octo/demo/pull/7",
    error: null,
    ...overrides,
  };
}

describe("PublicationFacts", () => {
  it("links the pull request the release opened", () => {
    render(<PublicationFacts publication={publication()} />);
    const link = screen.getByRole("link", { name: "https://github.com/octo/demo/pull/7" });
    expect(link.getAttribute("href")).toBe("https://github.com/octo/demo/pull/7");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(screen.getByText("origin/aegis/run-1")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a push that failed, and no link", () => {
    render(
      <PublicationFacts
        publication={publication({ remote: null, remote_branch: null, pr_url: null, error: "git push to origin failed" })}
      />,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByRole("alert").textContent).toBe("The pr step did not complete: git push to origin failed");
  });

  it("never turns a URL that is not http(s) into a link", () => {
    render(<PublicationFacts publication={publication({ pr_url: "javascript:alert(1)" })} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("javascript:alert(1)")).toBeTruthy();
  });

  it("says a local release went nowhere else", () => {
    render(
      <PublicationFacts publication={publication({ mode: "local", remote: null, remote_branch: null, pr_url: null })} />,
    );
    expect(screen.getByText("local")).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
