import { useEffect, useState } from 'react'

interface FetchState<T> {
  data: T | null
  loading: boolean
  error: string | null
}

// Deliberately not react-query/swr for v1 -- one dependency-free hook covers
// every read-only GET this app needs (see plan doc: don't add abstractions
// beyond what the task requires).
export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[] = []): FetchState<T> {
  const [state, setState] = useState<FetchState<T>>({ data: null, loading: true, error: null })

  useEffect(() => {
    let cancelled = false
    setState({ data: null, loading: true, error: null })
    fetcher()
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null })
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ data: null, loading: false, error: err instanceof Error ? err.message : String(err) })
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return state
}
