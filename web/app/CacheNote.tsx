"use client";

// Small "served from cache" hint. Analytics payloads carry a `_cache` marker
// ({hit, age_s}); this turns it into a quiet line next to the refresh button so
// users know whether they're looking at a fresh compute or a memoised one.
export function CacheNote({ cache }: { cache?: { hit: boolean; age_s: number } | null }) {
  if (!cache) return null;
  if (!cache.hit) return <span className="text-xs text-gray-600">freshly computed</span>;
  const m = Math.round(cache.age_s / 60);
  const age = cache.age_s < 60 ? "just now" : `${m}m ago`;
  return <span className="text-xs text-gray-600">cached · updated {age}</span>;
}
