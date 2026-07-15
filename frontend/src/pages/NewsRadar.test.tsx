import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import NewsRadar from './NewsRadar'
import { api, type NewsItem, type MarketEvent, type NewsFreshness } from '../lib/api'

vi.mock('../lib/api')

const news: NewsItem[] = [
  {
    item_id: 'n1', symbol: 'NVDA', item_type: 'earnings', headline: 'NVDA 財報前瞻',
    summary: '重點在資料中心指引。', url: 'https://x/1', source_name: 'Bloomberg',
    published_at: new Date().toISOString(), sentiment_score: 0.6, importance: 0.8,
    novelty_score: 0.7, priced_in_estimate: 0.5, transmission_chain: null, source_refs: [],
  },
  {
    item_id: 'n2', symbol: null, item_type: 'macro', headline: 'Fed 官員談話偏鷹',
    summary: '降息機率下降。', url: 'https://x/2', source_name: 'FRED',
    published_at: new Date().toISOString(), sentiment_score: -0.4, importance: 0.9,
    novelty_score: 0.6, priced_in_estimate: 0.5, transmission_chain: null, source_refs: [],
  },
]

const events: MarketEvent[] = [
  { event_id: 'e1', symbol: 'NVDA', event_type: 'earnings', event_type_label: '財報',
    scheduled_at: new Date().toISOString(), status: 'scheduled', days_until: 6 },
]

const freshness: NewsFreshness = {
  last_updated: new Date().toISOString(),
  last_run: { source: 'rss', rows_written: 57, finished_at: new Date().toISOString(), status: 'success' },
  news_last_24h: 12,
  median_ingest_gap_minutes: 90,
}

beforeEach(() => {
  localStorage.clear()
  vi.mocked(api.news).mockResolvedValue(news)
  vi.mocked(api.events).mockResolvedValue(events)
  vi.mocked(api.newsFreshness).mockResolvedValue(freshness)
})

afterEach(() => {
  localStorage.clear()
})

describe('NewsRadar', () => {
  it('renders the feed and filters by type', async () => {
    render(<MemoryRouter><NewsRadar /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('NVDA 財報前瞻')).toBeInTheDocument())
    expect(screen.getByText('Fed 官員談話偏鷹')).toBeInTheDocument()
    // human sentiment labels, not raw numbers
    expect(screen.getByText('強烈偏多')).toBeInTheDocument()
    expect(screen.getByText('偏空')).toBeInTheDocument()

    // filtering to 財報 hides the macro item
    fireEvent.click(screen.getByText(/財報 \(1\)/))
    expect(screen.getByText('NVDA 財報前瞻')).toBeInTheDocument()
    expect(screen.queryByText('Fed 官員談話偏鷹')).not.toBeInTheDocument()
  })

  it('shows the freshness strip from /api/news-freshness', async () => {
    render(<MemoryRouter><NewsRadar /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText(/上次抓取 57 則\(rss\)/)).toBeInTheDocument())
    expect(screen.getByText(/近24h 12 則/)).toBeInTheDocument()
  })

  it('has no "since last visit" badge on a first-ever visit', async () => {
    render(<MemoryRouter><NewsRadar /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('NVDA 財報前瞻')).toBeInTheDocument())
    expect(screen.queryByText(/自你上次到訪/)).not.toBeInTheDocument()
  })

  it('marks items newer than the stored last-seen timestamp as new, and clears on 標記為已讀', async () => {
    // Seed a last-seen timestamp older than both news items so both count as new.
    localStorage.setItem('newsradar_last_seen', new Date(Date.now() - 60 * 60 * 1000).toISOString())

    render(<MemoryRouter><NewsRadar /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText(/自你上次到訪 · 新增 2 則/)).toBeInTheDocument())

    fireEvent.click(screen.getByText('標記為已讀'))
    expect(screen.queryByText(/自你上次到訪/)).not.toBeInTheDocument()
  })
})
