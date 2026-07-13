import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { useApi } from '../lib/useApi'
import { DIRECTION_CLASSES, DIRECTION_LABEL, regimeClass, num, pct } from '../lib/format'
import ProbaBar from '../components/ProbaBar'
import Sparkline from '../components/Sparkline'
import GlossaryTerm from '../components/GlossaryTerm'
import NewsRow from '../components/NewsRow'
import { Card, Chip, SectionTitle, Loading, ErrorMsg, Empty } from '../components/ui'

const OUTCOME_CLASSES: Record<string, string> = { win: 'text-emerald-400', loss: 'text-rose-400' }

export default function TickerDetail() {
  const { symbol = '' } = useParams()
  const { data, loading, error } = useApi(() => api.ticker(symbol), [symbol])
  const { data: tradersData } = useApi(() => api.tickerTraders(symbol), [symbol])

  const last = data?.price_history?.at(-1)?.close
  const prev = data?.price_history?.at(-2)?.close
  const dayChange = last !== undefined && prev !== undefined && prev ? (last - prev) / prev : null

  return (
    <div>
      <Link to="/" className="text-sm text-neutral-500 hover:text-neutral-300">← 回今日機會</Link>

      {loading && <div className="mt-4"><Loading /></div>}
      {error && <div className="mt-4"><ErrorMsg error={error} /></div>}

      {data && (
        <div className="mt-4 space-y-6">
          {/* Header */}
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-3xl font-bold text-neutral-50">{data.symbol}</h1>
                {last !== undefined && (
                  <div className="flex items-baseline gap-2">
                    <span className="text-2xl font-semibold tabular-nums text-neutral-100">${num(last)}</span>
                    {dayChange !== null && (
                      <span className={`text-sm tabular-nums ${dayChange >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {dayChange >= 0 ? '+' : ''}{pct(dayChange, 1)}
                      </span>
                    )}
                  </div>
                )}
              </div>
              <p className="mt-1 text-sm text-neutral-500">{data.sector} · 資料日期 {data.trade_date}</p>
            </div>
            <div className="flex items-center gap-2">
              <Chip className={regimeClass(data.regime_label)}>
                {data.regime_label}
              </Chip>
              <Chip className={DIRECTION_CLASSES[data.predicted_direction] ?? DIRECTION_CLASSES.range}>
                {DIRECTION_LABEL[data.predicted_direction] ?? data.predicted_direction} · 信心 {pct(data.conviction)}
              </Chip>
            </div>
          </div>

          {/* Thesis banner */}
          <Card className="p-4">
            <p className="text-neutral-100">{data.catalyst?.catalyst_summary ?? data.thesis}</p>
            {data.catalyst?.transmission_chain && (
              <p className="mt-1 text-sm text-neutral-400">{data.catalyst.transmission_chain}</p>
            )}
          </Card>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
            {/* Left column: price + model */}
            <div className="space-y-6 lg:col-span-2">
              <Card className="p-4">
                <SectionTitle title="價格走勢(近 90 日)" />
                <Sparkline points={data.price_history.map((p) => ({ value: p.close }))} emptyMessage="價格歷史不足" />
                <div className="mt-3 flex flex-wrap gap-4 text-xs text-neutral-500">
                  <span>進場參考 ${num(data.entry_price)}</span>
                  <span>目標區間 ${num(data.target_price_down, 0)}–${num(data.target_price_up, 0)}</span>
                  <span>評估到期 {data.label_end_date}</span>
                </div>
              </Card>

              <Card className="p-4">
                <SectionTitle
                  title="模型判斷機率"
                  hint="三選一(漲/盤整/跌)的機率分佈,取最高者為方向,其值即為信心。"
                />
                <ProbaBar proba={data.proba} />
                {data.backtest ? (
                  <p className="mt-3 text-xs text-neutral-500">
                    <GlossaryTerm term="overall_accuracy">回測方向準確率 {pct(data.backtest.overall_accuracy)}</GlossaryTerm>
                    {' · '}
                    <GlossaryTerm term="overall_brier">機率校準(Brier) {num(data.backtest.overall_brier, 3)}</GlossaryTerm>
                    {' · '}
                    <GlossaryTerm term="overall_sharpe">風險調整報酬(Sharpe) {num(data.backtest.overall_sharpe, 2)}</GlossaryTerm>
                  </p>
                ) : (
                  <p className="mt-3 text-xs italic text-neutral-600">回測快取尚未就緒</p>
                )}
              </Card>

              <Card className="p-4">
                <SectionTitle title="為什麼:關鍵特徵值" hint="模型這次判斷主要參考的特徵(滑鼠移到名稱看解釋)。" />
                <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
                  {Object.entries(data.feature_values).map(([k, v]) => (
                    <div key={k} className="flex justify-between border-b border-neutral-800/70 pb-1">
                      <GlossaryTerm term={k} className="text-neutral-500">{k}</GlossaryTerm>
                      <span className="tabular-nums text-neutral-200">{num(v, 3)}</span>
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-xs text-neutral-600">model_version={data.model_version}</p>
              </Card>
            </div>

            {/* Right column: news + traders */}
            <div className="space-y-6">
              <Card>
                <div className="flex items-center justify-between px-4 pt-4">
                  <SectionTitle title="相關消息" hint="點任一則進入消息詳情。" />
                  <Link to="/news" className="pb-3 text-xs text-neutral-500 hover:text-neutral-300">看全部 →</Link>
                </div>
                {data.news.length === 0 ? (
                  <div className="px-4 pb-4"><Empty>目前沒有這檔的相關消息。</Empty></div>
                ) : (
                  <div>
                    {data.news.map((n) => (
                      <NewsRow key={n.item_id} item={n} showSymbol={false} />
                    ))}
                  </div>
                )}
              </Card>

              <Card className="p-4">
                <SectionTitle title="各交易員判斷" hint="不同流派對這檔的看法與共識/分歧。" />
                {tradersData === null && (
                  <Empty>此標的尚無交易員判斷。</Empty>
                )}
                {tradersData && tradersData.traders.length > 0 && (
                  <div className="space-y-3">
                    {tradersData.consensus.n_traders > 1 && (
                      <p className="text-xs text-neutral-500">
                        {tradersData.consensus.agree
                          ? `${tradersData.consensus.n_traders} 位共識:${tradersData.consensus.directions.map((d) => DIRECTION_LABEL[d] ?? d).join('、')}`
                          : `${tradersData.consensus.n_traders} 位分歧:${tradersData.consensus.directions.map((d) => DIRECTION_LABEL[d] ?? d).join(' vs ')}`}
                      </p>
                    )}
                    {tradersData.traders.map((t) => (
                      <div key={t.trader_id} className="border-b border-neutral-800 pb-3 last:border-0 last:pb-0">
                        <div className="mb-1 flex items-center justify-between">
                          <Link to={`/trader/${t.trader_id}`} className="text-sm text-neutral-200 hover:text-neutral-50">
                            {t.trader_id}
                          </Link>
                          <Chip className={DIRECTION_CLASSES[t.direction] ?? DIRECTION_CLASSES.range}>
                            {DIRECTION_LABEL[t.direction] ?? t.direction} {pct(t.conviction)}
                          </Chip>
                        </div>
                        {t.rationale && <p className="text-xs text-neutral-400">{t.rationale}</p>}
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            </div>
          </div>

          {/* Prediction history */}
          <Card className="p-4">
            <SectionTitle title="這檔的預測歷史" hint="每筆到期後對帳的輸贏紀錄。" />
            {data.history.length === 0 ? (
              <Empty>尚無歷史紀錄。</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-neutral-800 text-left text-neutral-500">
                      <th className="py-1.5 pr-4 font-normal">日期</th>
                      <th className="py-1.5 pr-4 font-normal">預測</th>
                      <th className="py-1.5 pr-4 font-normal">狀態</th>
                      <th className="py-1.5 font-normal">結果</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.history.map((h) => (
                      <tr key={h.prediction_id} className="border-b border-neutral-900 last:border-0">
                        <td className="py-1.5 pr-4 text-neutral-400">{h.trade_date}</td>
                        <td className="py-1.5 pr-4">{DIRECTION_LABEL[h.predicted_direction] ?? h.predicted_direction}</td>
                        <td className="py-1.5 pr-4 text-neutral-500">{h.status === 'graded' ? '已對帳' : '待驗證'}</td>
                        <td className={`py-1.5 ${h.outcome ? OUTCOME_CLASSES[h.outcome] : 'text-neutral-600'}`}>
                          {h.outcome === 'win' ? '贏' : h.outcome === 'loss' ? '輸' : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  )
}
