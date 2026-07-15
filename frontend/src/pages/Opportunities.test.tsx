import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Opportunities from './Opportunities'
import { api, type CockpitCard, type Briefing, type Quote, type PredictionsOverview, type PostmarketWrap } from '../lib/api'

vi.mock('../lib/api')

// The page also fetches the model-track-record and post-market-wrap endpoints.
// Stub them to their honest empty states so every mount resolves; these tests
// assert the briefing/watchlist behaviour, not those two panels.
const predictionsOverview: PredictionsOverview = {
  rolling_win_rate: [],
  outcome_counts: {},
  recent: [],
}

const postmarketWrap: PostmarketWrap = {
  as_of_date: null,
  headline: '',
  narrative: '',
  dominant_regime: null,
  vix: null,
  vix_term_slope: null,
  breadth: { up: 0, down: 0, flat: 0 },
  top_movers: [],
  sector_strength: null,
  notable: [],
  data_sufficient: false,
  insufficient_reason: '資料不足',
}

beforeEach(() => {
  vi.mocked(api.predictions).mockResolvedValue(predictionsOverview)
  vi.mocked(api.postmarket).mockResolvedValue(postmarketWrap)
})

const card = (
  symbol: string,
  sector: string,
  gate: 'green' | 'red' | 'unknown' = 'green',
  breakoutState = '區間內盤整',
): CockpitCard => ({
  symbol,
  sector,
  as_of_date: '2026-07-10',
  price: 178.0,
  levels: { nday_high: 190, nday_low: 160, prev_high: 180, prev_low: 175, sma20: 172, sma50: 168 },
  breakout_state: breakoutState,
  regime: '高波動趨勢盤',
  top_news: {
    item_id: 'n1', headline: 'Some fresh headline about ' + symbol, sentiment_score: 0.4,
    importance: 0.6, published_at: '2026-07-10T14:00:00Z',
  },
  sellput: {
    strike: 169.1, otm_pct: 0.05, gate_light: gate,
    reason: gate === 'green' ? '閘門不擋單' : '閘門建議空手',
    note: '示意履約價',
  },
  volume_signal: { ratio: 1.8, state: '放量', today_volume: 90_000_000, avg_volume_20d: 50_000_000 },
  sector_linkage: { state: '跟隨板塊同步', symbol_return: 0.01, peer_avg_return: 0.009, z: 0.2, note: null },
  iv: 0.286,
  dollar_volume: 1_300_000_000,
  earnings_date: '2026-07-30',
})

const briefing: Briefing = {
  as_of_date: '2026-07-10',
  headline: '核心觀察清單 2 檔中,主導 regime 為「高波動趨勢盤」。',
  narrative: '核心觀察清單 2 檔中,主導市場狀態為「高波動趨勢盤」,VIX 現為 18.2,期限結構平靜正常。近期無足夠總經事件新聞可綜合,以上退回 regime + 板塊強弱簡版摘要;不構成漲跌預測。',
  narrative_basis: 'fallback_regime_sector',
  sector_rotation: [
    { sector: 'semiconductor', sector_label: '半導體', n_symbols: 2, ret_1d_avg: 0.012, ret_5d_avg: 0.03, rank: 1 },
  ],
  vix: 18.2,
  vix_term_slope: 0.3,
  breadth: { up: 1, down: 1, flat: 0 },
  guardrail: '無方向 edge;賣方收租是唯一微弱正 edge;做多贏過一切 —— 本工具輔助你的判斷,不預測方向。',
  market_lines: ['核心觀察清單 2 檔中,主導 regime 為「高波動趨勢盤」。'],
  symbol_lines: ['NVDA 現價 178.00;今日觀察 20 日區間 160~190。', 'AMD 現價 178.00;今日觀察 20 日區間 160~190。'],
  scoreboard: {
    as_of: '2026-07-13',
    headline: '8 年 walk-forward 回測:純價格/技術訊號沒有一個打贏單純做多。',
    conclusions: [{ id: 'no_directional_edge', text: '沒有方向性優勢。' }],
    so_what: '本工具不預測漲跌。',
  },
}

describe('Opportunities', () => {
  it('renders the guardrail banner, briefing narrative, and the watchlist cards', async () => {
    vi.mocked(api.cockpit).mockResolvedValue([card('NVDA', 'semiconductor'), card('AMD', 'semiconductor')])
    vi.mocked(api.briefing).mockResolvedValue(briefing)
    vi.mocked(api.quotes).mockResolvedValue({})

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    await waitFor(() => expect(screen.getAllByText('NVDA').length).toBeGreaterThan(0))
    // Honest guardrail line is shown, not a confidence number.
    expect(screen.getByText(/無方向 edge/)).toBeInTheDocument()
    // No "方向信心" / prediction confidence framing anywhere.
    expect(screen.queryByText(/方向信心/)).not.toBeInTheDocument()
    expect(screen.queryByText(/精選/)).not.toBeInTheDocument()
    // Regime + breakout state render as descriptive chips.
    expect(screen.getAllByText('高波動趨勢盤').length).toBeGreaterThan(0)
    expect(screen.getAllByText('區間內盤整').length).toBeGreaterThan(0)
    // The daily briefing narrative paragraph renders.
    expect(screen.getByText(/主導市場狀態為「高波動趨勢盤」/)).toBeInTheDocument()
    // Sector rotation panel renders (also appears per-card as the sector label).
    expect(screen.getAllByText('半導體').length).toBeGreaterThan(0)
    // Per-card volume + sector-linkage badges render.
    expect(screen.getAllByText(/放量/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('跟隨板塊同步').length).toBeGreaterThan(0)
  })

  it('shows the sell-put gate light and strike suggestion per card', async () => {
    vi.mocked(api.cockpit).mockResolvedValue([card('NVDA', 'semiconductor', 'red')])
    vi.mocked(api.briefing).mockResolvedValue(briefing)
    vi.mocked(api.quotes).mockResolvedValue({})

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText(/🔴 賣方閘門關/)).toBeInTheDocument())
    expect(screen.getByText(/169.1/)).toBeInTheDocument()
  })

  it('shows a live price + change% badge once a quote is available', async () => {
    vi.mocked(api.cockpit).mockResolvedValue([card('NVDA', 'semiconductor')])
    vi.mocked(api.briefing).mockResolvedValue(briefing)
    const quote: Quote = { price: 185.5, prev_close: 178.0, change_pct: 0.0421, as_of: '2026-07-13T14:00:00Z' }
    vi.mocked(api.quotes).mockResolvedValue({ NVDA: quote })

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('$185.50')).toBeInTheDocument())
    // The change% appears on the card badge and again in the 今日焦點 top-mover
    // tile (NVDA is the sole/top mover), so match all occurrences.
    expect(screen.getAllByText('+4.2%').length).toBeGreaterThan(0)
  })

  it('defaults to posture grouping and buckets cards by breakout_state, never predicting direction', async () => {
    vi.mocked(api.cockpit).mockResolvedValue([
      card('NVDA', 'semiconductor', 'green', '站上前日高點'),
      card('AMD', 'semiconductor', 'green', '區間內盤整'),
      card('SOXS', 'semiconductor_etf', 'green', '跌破前日低點'),
    ])
    vi.mocked(api.briefing).mockResolvedValue(briefing)
    vi.mocked(api.quotes).mockResolvedValue({})

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    await waitFor(() => expect(screen.getAllByText('NVDA').length).toBeGreaterThan(0))
    // Honest posture-section framing, never a forecast.
    expect(screen.getByText(/當前姿態.*非漲跌預測/)).toBeInTheDocument()
    expect(screen.getByText(/偏強姿態/)).toBeInTheDocument()
    expect(screen.getAllByText(/中性/).length).toBeGreaterThan(0)
    expect(screen.getByText(/偏弱姿態/)).toBeInTheDocument()
  })

  it('collapses the sell-put box to a one-line summary by default and expands the reason on click', async () => {
    vi.mocked(api.cockpit).mockResolvedValue([card('NVDA', 'semiconductor', 'red')])
    vi.mocked(api.briefing).mockResolvedValue(briefing)
    vi.mocked(api.quotes).mockResolvedValue({})

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText(/🔴 賣方閘門關/)).toBeInTheDocument())
    // Reason detail is not shown until expanded.
    expect(screen.queryByText('閘門建議空手')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('詳情 ▼'))

    expect(screen.getByText('閘門建議空手')).toBeInTheDocument()
  })

  it('shows IV, dollar volume, and next earnings date per card, and "未知" when earnings date is unavailable', async () => {
    vi.mocked(api.cockpit).mockResolvedValue([
      card('NVDA', 'semiconductor'),
      { ...card('AVGO', 'semiconductor'), earnings_date: null },
    ])
    vi.mocked(api.briefing).mockResolvedValue(briefing)
    vi.mocked(api.quotes).mockResolvedValue({})

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    await waitFor(() => expect(screen.getAllByText('NVDA').length).toBeGreaterThan(0))
    expect(screen.getAllByText(/IV 28\.6%/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/成交額 \$1\.3B/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/財報日 7\/30/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/財報日 未知/).length).toBeGreaterThan(0)
  })

  it('shows a compact market-prep strip (選擇權環境 / 今日焦點 / 最近財報)', async () => {
    vi.mocked(api.cockpit).mockResolvedValue([card('NVDA', 'semiconductor')])
    vi.mocked(api.briefing).mockResolvedValue(briefing)
    vi.mocked(api.quotes).mockResolvedValue({})

    render(<MemoryRouter><Opportunities /></MemoryRouter>)

    // The rebuilt strip surfaces the options-premium environment (VIX reframed),
    // today's focus mover, and the nearest earnings — not the old raw
    // VIX/term-structure/breadth tiles.
    await waitFor(() => expect(screen.getByText(/選擇權環境/)).toBeInTheDocument())
    expect(screen.getByText(/VIX 18\.2/)).toBeInTheDocument()
    expect(screen.getByText('今日焦點')).toBeInTheDocument()
    expect(screen.getByText('最近財報')).toBeInTheDocument()
  })
})
