"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { Api, ApiError, DEFAULT_BASE, type Me } from "@/lib/api";
import { Button } from "@/components/ui/button";

interface Session {
  api: Api;
  me: Me;
  signOut: () => void;
}

export const SessionContext = createContext<Session | null>(null);
const KEY = "aegis.session"; // sessionStorage: per tab, gone when it closes

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (session === null) throw new Error("useSession outside a signed-in page");
  return session;
}

function stored(): { base: string; token: string } | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as { base: string; token: string }) : null;
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const signOut = useCallback(() => {
    try {
      window.sessionStorage.removeItem(KEY);
    } catch {
      // storage unavailable: nothing was kept
    }
    setSession(null);
  }, []);

  const signIn = useCallback(
    async (base: string, token: string) => {
      const api = new Api(base.replace(/\/$/, ""), token.trim());
      try {
        const me = await api.me();
        try {
          window.sessionStorage.setItem(KEY, JSON.stringify({ base: api.base, token: api.token }));
        } catch {
          // storage unavailable: the session lasts until the page reloads
        }
        setError(null);
        setSession({ api, me, signOut });
      } catch (caught) {
        setError(
          caught instanceof ApiError && caught.status === 401
            ? "That token is not valid (msa tokens add issues one)."
            : `Cannot reach the API at ${base}: is msa serve running?`,
        );
      }
    },
    [signOut],
  );

  useEffect(() => {
    const saved = stored();
    if (saved) {
      void signIn(saved.base, saved.token).finally(() => setChecking(false));
    } else {
      setChecking(false);
    }
  }, [signIn]);

  if (checking) {
    return (
      <p role="status" className="p-8 text-muted">
        Checking your session…
      </p>
    );
  }
  if (session === null) return <SignIn onSignIn={signIn} error={error} />;
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}

function SignIn({
  onSignIn,
  error,
}: {
  onSignIn: (base: string, token: string) => Promise<void>;
  error: string | null;
}) {
  const [base, setBase] = useState(DEFAULT_BASE);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <main id="main" className="mx-auto mt-16 max-w-md px-4">
      <h1 className="mb-2 text-2xl font-semibold">Sign in to AEGIS</h1>
      <p className="mb-6 text-sm text-muted">
        Use a token from <code className="font-mono">msa tokens add &lt;you&gt; --role …</code>. It
        is kept in this tab only.
      </p>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          setBusy(true);
          void onSignIn(base, token).finally(() => setBusy(false));
        }}
      >
        <div>
          <label htmlFor="base" className="mb-1 block text-sm font-medium">
            API address
          </label>
          <input
            id="base"
            className="w-full rounded-md border border-border bg-bg px-3 py-2"
            value={base}
            onChange={(e) => setBase(e.target.value)}
            required
          />
        </div>
        <div>
          <label htmlFor="token" className="mb-1 block text-sm font-medium">
            Token
          </label>
          <input
            id="token"
            type="password"
            autoComplete="off"
            className="w-full rounded-md border border-border bg-bg px-3 py-2 font-mono"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            required
          />
        </div>
        {error && (
          <p role="alert" className="rounded-md border border-bad bg-bad-bg px-3 py-2 text-sm text-bad">
            {error}
          </p>
        )}
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </main>
  );
}
