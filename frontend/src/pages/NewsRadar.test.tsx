import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import NewsRadar from './NewsRadar'
import { api, type NewsItem, type MarketEvent } from '../lib/api'

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

describe('NewsRadar', () => {
  it('renders the feed and filters by type', async () => {
    vi.mocked(api.news).mockResolvedValue(news)
    vi.mocked(api.events).mockResolvedValue(events)

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
})
