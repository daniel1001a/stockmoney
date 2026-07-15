import { describe, expect, it } from 'vitest'
import { marketPhaseET, refreshIntervalMs } from './refreshCadence'

// Helper: build a Date that reads as a given wall-clock time in US Eastern.
// July is EDT (UTC-4), so 13:30 UTC == 09:30 ET. Using fixed July dates keeps
// the DST offset unambiguous.
const etJuly = (hhmmUtc: string, day = 13) => new Date(`2026-07-${day}T${hhmmUtc}:00Z`)

describe('marketPhaseET', () => {
  it('is power hour in the first two hours after the open', () => {
    // 13:30Z = 09:30 ET (open), 15:29Z = 11:29 ET (still < 11:30)
    expect(marketPhaseET(etJuly('13:30'))).toBe('open_power_hour') // Mon 2026-07-13
    expect(marketPhaseET(etJuly('15:29'))).toBe('open_power_hour')
  })

  it('is regular session from 11:30 ET to the close', () => {
    expect(marketPhaseET(etJuly('15:30'))).toBe('open_regular') // 11:30 ET
    expect(marketPhaseET(etJuly('19:59'))).toBe('open_regular') // 15:59 ET
  })

  it('is closed before the open, after the close, and on weekends', () => {
    expect(marketPhaseET(etJuly('13:29'))).toBe('closed') // 09:29 ET, pre-open
    expect(marketPhaseET(etJuly('20:00'))).toBe('closed') // 16:00 ET, at close
    expect(marketPhaseET(etJuly('16:00', 18))).toBe('closed') // Sat 2026-07-18, midday ET
    expect(marketPhaseET(etJuly('16:00', 19))).toBe('closed') // Sun 2026-07-19
  })
})

describe('refreshIntervalMs', () => {
  it('ticks fastest in the power hour, slower midday, and hourly when closed', () => {
    expect(refreshIntervalMs(etJuly('14:00'))).toBe(60_000) // 10:00 ET
    expect(refreshIntervalMs(etJuly('18:00'))).toBe(180_000) // 14:00 ET
    expect(refreshIntervalMs(etJuly('23:00'))).toBe(3_600_000) // 19:00 ET, after close
  })

  it('never stops timed polling -- always returns a number, even overnight/weekend', () => {
    expect(refreshIntervalMs(etJuly('10:00'))).toBe(3_600_000) // 06:00 ET, pre-open
    expect(refreshIntervalMs(etJuly('16:00', 18))).toBe(3_600_000) // Sat 2026-07-18, midday ET
    expect(refreshIntervalMs(etJuly('16:00', 19))).toBe(3_600_000) // Sun 2026-07-19
  })
})
