import { useEffect, useRef, useState } from 'react'

interface FetchState<T> {
  data: T | null
  loading: boolean
  error: string | null
}

interface UseApiOptions {
  // Auto-refresh interval policy (ms), re-evaluated before each tick so the
  // cadence can adapt to the market phase (see refreshCadence.ts). Return null
  // to stop timed polling; refetch-on-focus still applies regardless.
  refreshMs?: () => number | null
}

// Deliberately not react-query/swr for v1 -- one dependency-free hook covers
// every read-only GET this app needs (see plan doc: don't add abstractions
// beyond what the task requires).
//
// Beyond the initial load it also, when `refreshMs` is given:
//   - refetches SILENTLY on a self-rescheduling timer (never flips back to the
//     loading spinner or blanks good data -- a transient refresh error keeps
//     the last good payload on screen),
//   - refetches immediately when the tab becomes visible again (the user opens
//     the laptop in the morning and sees fresh numbers without a manual reload),
//   - pauses the timer while the tab is hidden (no point polling a tab nobody
//     is looking at -- there are no alarms/notifications anyway).
export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  options: UseApiOptions = {},
): FetchState<T> {
  const [state, setState] = useState<FetchState<T>>({ data: null, loading: true, error: null })

  // Keep the latest fetcher/policy without retriggering the effect (which is
  // keyed on `deps`), so a new inline closure each render doesn't restart polling.
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const refreshRef = useRef(options.refreshMs)
  refreshRef.current = options.refreshMs

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const clearTimer = () => {
      if (timer) clearTimeout(timer)
      timer = undefined
    }

    const schedule = () => {
      clearTimer()
      const ms = refreshRef.current?.() ?? null
      if (ms != null && ms > 0 && document.visibilityState === 'visible') {
        timer = setTimeout(() => load(true), ms)
      }
    }

    const load = (silent: boolean) => {
      if (!silent) setState({ data: null, loading: true, error: null })
      fetcherRef
        .current()
        .then((data) => {
          if (!cancelled) setState({ data, loading: false, error: null })
        })
        .catch((err: unknown) => {
          const message = err instanceof Error ? err.message : String(err)
          // A background refresh that fails keeps the last good data on screen;
          // only a foreground (initial) load surfaces the error state.
          if (!cancelled && !silent) setState({ data: null, loading: false, error: message })
        })
        .finally(() => {
          if (!cancelled) schedule()
        })
    }

    const onVisibility = () => {
      if (document.visibilityState === 'visible') load(true)
      else clearTimer()
    }

    load(false)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelled = true
      clearTimer()
      document.removeEventListener('visibilitychange', onVisibility)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return state
}
