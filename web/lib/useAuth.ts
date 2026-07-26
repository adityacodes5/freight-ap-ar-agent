"use client";

import { useState, useEffect, useCallback } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase, ALLOWED_EMAILS } from "./supabase";

export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** Shared Google-via-Supabase auth, reused across dashboard pages. The session is
 *  persisted by supabase-js, so every page picks it up independently. */
export function useAuth() {
  const [session, setSession] = useState<Session | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [signingIn, setSigningIn] = useState(false);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setAuthReady(true);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, []);

  const token = session?.access_token;
  const email = (session?.user?.email ?? "").toLowerCase();
  const authorized = !!session && ALLOWED_EMAILS.includes(email);

  const authHeaders = useCallback(
    (): Record<string, string> => ({
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    }),
    [token],
  );

  const signIn = async () => {
    setSigningIn(true);
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: typeof window !== "undefined" ? window.location.origin : undefined },
    });
  };
  const signOut = async () => {
    await supabase.auth.signOut();
    setSession(null);
  };

  return { session, authReady, authorized, email, token, authHeaders, signIn, signOut, signingIn };
}
