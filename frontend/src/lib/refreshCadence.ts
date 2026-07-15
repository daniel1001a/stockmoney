// Market-hours-aware auto-refresh cadence for the dashboard.
//
// Rationale (the user's operating pattern): fast polling only earns its keep
// when (a) the market is actually moving and (b) the dashboard is open in front
// of you -- there are no alarms/notifications, so refreshing a tab nobody is
// looking at is wasted work (the visibility gating lives in useApi.ts). During
// the first ~2 hours after the open, price action is fastest and that is when
// the user trades, so we tick quickest then; midday we slow down. After hours,
// overnight, and on weekends we still poll, just hourly -- catch-up ingestion
// (nightly refresh, a delayed cross-machine sync, an OpenClaw cron run that
// fired late) can land at any hour, and an open tab should notice within the
// hour instead of only whenever the user happens to refocus it.
//
// HONEST SCOPE (CLAUDE.md section 17 -- no intraday streaming in Phase 1):
// every data source today is a DAILY batch, so intraday ticks mostly re-fetch
// the same cached predictions -- their real present-day value is picking up the
// nightly refresh + Agent 3's sync promptly and re-fetching the moment the user
// returns to the tab in the morning. The finer power-hour cadence only starts
// surfacing genuinely new numbers once intraday ingestion exists; the policy is
// built and tested now so that day needs no rewiring, not because it conjures
// data that isn't there yet. Same caveat applies to the hourly after-hours
// poll: it can't surface data more often than the underlying batch jobs
// actually run -- it only bounds how stale an open tab can look (at most ~1h
// behind whatever last landed) at the cost of one cheap request per hour.

export type MarketPhase = 'open_power_hour' | 'open_regular' | 'closed'

// US regular session in Eastern time, in minutes-since-ET-midnight.
const OPEN_MIN = 9 * 60 + 30 // 09:30
const POWER_HOUR_END_MIN = 11 * 60 + 30 // 11:30 (first two hours)
const CLOSE_MIN = 16 * 60 // 16:00

const POWER_HOUR_MS = 60_000 // 1 min while it moves fastest
const REGULAR_MS = 180_000 // 3 min midday
const CLOSED_MS = 3_600_000 // 1 hour after-hours/overnight/weekend catch-up poll

/** ET weekday (0=Sun..6=Sat) and minutes-since-midnight, DST-correct via Intl. */
function easternParts(now: Date): { weekday: number; minutes: number } {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(now)
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  const weekdayMap: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }
  // Intl can emit "24" for midnight in hour12:false; normalise to 0.
  const hour = parseInt(get('hour'), 10) % 24
  const minute = parseInt(get('minute'), 10)
  return { weekday: weekdayMap[get('weekday')] ?? 0, minutes: hour * 60 + minute }
}

export function marketPhaseET(now: Date): MarketPhase {
  const { weekday, minutes } = easternParts(now)
  if (weekday === 0 || weekday === 6) return 'closed' // weekend
  if (minutes < OPEN_MIN || minutes >= CLOSE_MIN) return 'closed'
  return minutes < POWER_HOUR_END_MIN ? 'open_power_hour' : 'open_regular'
}

/** Auto-refresh interval in ms. Always returns a timed interval (never null)
 *  so an open tab is never more than one cadence-step stale: fastest in the
 *  power hour, brisk midday, hourly the rest of the time (after hours,
 *  overnight, weekends) to catch late-landing batch jobs -- refetch-on-focus
 *  (see useApi.ts) still applies on top of this. Does NOT account for market
 *  holidays -- on a holiday it just polls as if open, which only means a few
 *  harmless extra reads of unchanged data. */
export function refreshIntervalMs(now: Date = new Date()): number | null {
  switch (marketPhaseET(now)) {
    case 'open_power_hour':
      return POWER_HOUR_MS
    case 'open_regular':
      return REGULAR_MS
    case 'closed':
      return CLOSED_MS
  }
}
