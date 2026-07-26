import { createClient } from "@supabase/supabase-js";

// Public, client-safe values. The publishable key is meant to ship to the
// browser; access is controlled by Supabase Auth + the single-email allowlist
// (enforced again server-side in the API).
const url =
  process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://yourproject.supabase.co";
const key =
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ??
  "sb_publishable_REPLACE_ME";

export const ALLOWED_EMAILS = (
  process.env.NEXT_PUBLIC_ALLOWED_EMAIL ??
  "owner@demologistics.com,ap.clerk@demologistics.com"
)
  .split(",")
  .map((e) => e.trim().toLowerCase())
  .filter(Boolean);

export const supabase = createClient(url, key, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
  },
});
