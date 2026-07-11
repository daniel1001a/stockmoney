import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { useApi } from '../lib/useApi'
import { DIRECTION_CLASSES, DIRECTION_LABEL, num, pct } from '../lib/format'
import ProbaBar from '../components/ProbaBar'
import Sparkline from '../components/Sparkline'
import GlossaryTerm from '../components/GlossaryTerm'

const OUTCOME_CLASSES: Record<string, string> = {
  win: 'text-emerald-400',
  loss: 'text-rose-400',
}

export default function TickerDetail() {
  const { symbol = '' } = useParams()
  const { data, loading, error } = useApi(() => api.ticker(symbol), [symbol])
  const {
    data: tradersData,
    loading: tradersLoading,
    error: tradersError,
  } = useApi(() => api.tickerTraders(symbol), [symbol])

  return (
    <div>
      <Link to="/" className="text-sm text-neutral-500 hover:text-neutral-300">
        ← 回今日機會
      </Link>

      {loading && <p className="text-neutral-500 mt-4">載入中…</p>}
      {error && <p className="text-rose-400 mt-4">載入失敗：{error}</p>}

      {data && (
        <div className="mt-4 space-y-6">
          <div className="flex items-start justify-between">
            <div>
              <h1 className="text-3xl font-semibold text-neutral-50">{data.symbol}</h1>
              <p className="text-sm text-neutral-500">
                {data.sector} · regime {data.regime} · 資料日期 {data.trade_date}
              </p>
            </div>
            <span
              className={`text-sm font-medium px-3 py-1.5 rounded border ${
                DIRECTION_CLASSES[data.predicted_direction] ?? DIRECTION_CLASSES.range
              }`}
            >
              {DIRECTION_LABEL[data.predicted_direction] ?? data.predicted_direction} {pct(data.conviction)}
            </span>
          </div>

          <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
            <h2 className="text-sm font-medium text-neutral-300 mb-3">價格走勢</h2>
            <Sparkline
              points={data.price_history.map((p) => ({ value: p.close }))}
              emptyMessage="價格歷史不足"
            />
            <div className="mt-3 flex gap-4 text-xs text-neutral-500">
              <span>進場 ${num(data.entry_price)}</span>
              <span>
                目標區間 ${num(data.target_price_down, 0)}–${num(data.target_price_up, 0)}
              </span>
              <span>到期 {data.label_end_date}</span>
            </div>
          </section>

          <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
            <h2 className="text-sm font-medium text-neutral-300 mb-3">模型判斷機率</h2>
            <ProbaBar proba={data.proba} />
            {data.backtest ? (
              <p className="mt-3 text-xs text-neutral-500">
                回測（GMM，樣本外）：
                <GlossaryTerm term="overall_accuracy">準確率 {pct(data.backtest.overall_accuracy)}</GlossaryTerm>
                （n={data.backtest.overall_n}） ·{' '}
                <GlossaryTerm term="overall_brier">Brier {num(data.backtest.overall_brier, 3)}</GlossaryTerm> ·{' '}
                <GlossaryTerm term="overall_sharpe">Sharpe {num(data.backtest.overall_sharpe, 2)}</GlossaryTerm>
                {data.backtest.ev_of_continuing_now !== null && (
                  <>
                    {' · '}
                    <GlossaryTerm term="ev_of_continuing_now">
                      目前續抱EV {num(data.backtest.ev_of_continuing_now, 4)}
                    </GlossaryTerm>
                  </>
                )}
              </p>
            ) : (
              <p className="mt-3 text-xs text-neutral-600 italic">回測快取尚未就緒</p>
            )}
          </section>

          <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
            <h2 className="text-sm font-medium text-neutral-300 mb-3">為什麼：關鍵特徵值</h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
              {Object.entries(data.feature_values).map(([k, v]) => (
                <div key={k} className="flex justify-between border-b border-neutral-800 pb-1">
                  <GlossaryTerm term={k} className="text-neutral-500">
                    {k}
                  </GlossaryTerm>
                  <span className="text-neutral-200 tabular-nums">{num(v, 4)}</span>
                </div>
              ))}
            </div>
            <p className="mt-3 text-xs text-neutral-600">model_version={data.model_version}</p>
          </section>

          <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
            <h2 className="text-sm font-medium text-neutral-300 mb-3">消息催化劑</h2>
            {data.catalyst ? (
              <div className="space-y-2 text-sm">
                <p className="text-neutral-100">{data.catalyst.catalyst_summary}</p>
                <p className="text-neutral-400">{data.catalyst.transmission_chain}</p>
                <div className="flex gap-4 text-xs text-neutral-500 pt-1">
                  <span>新穎度 {num(data.catalyst.novelty_score, 2)}</span>
                  <span>情緒 {num(data.catalyst.sentiment_score, 2)}</span>
                  <span>已被市場消化程度 {pct(data.catalyst.priced_in_estimate)}</span>
                  <span>{data.catalyst.source_refs.length} 則來源</span>
                </div>
              </div>
            ) : (
              <p className="text-neutral-600 italic text-sm">
                尚無消息面資料 —— 需先跑 scripts/classify_scan.py 與 scripts/synthesize_catalysts.py
              </p>
            )}
          </section>

          <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
            <h2 className="text-sm font-medium text-neutral-300 mb-3">預測歷史</h2>
            {data.history.length === 0 ? (
              <p className="text-neutral-600 italic text-sm">尚無歷史紀錄</p>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-neutral-500 text-left border-b border-neutral-800">
                    <th className="py-1 font-normal">日期</th>
                    <th className="py-1 font-normal">預測</th>
                    <th className="py-1 font-normal">狀態</th>
                    <th className="py-1 font-normal">結果</th>
                  </tr>
                </thead>
                <tbody>
                  {data.history.map((h) => (
                    <tr key={h.prediction_id} className="border-b border-neutral-900">
                      <td className="py-1.5 text-neutral-400">{h.trade_date}</td>
                      <td className="py-1.5">{DIRECTION_LABEL[h.predicted_direction] ?? h.predicted_direction}</td>
                      <td className="py-1.5 text-neutral-400">{h.status}</td>
                      <td className={`py-1.5 ${h.outcome ? OUTCOME_CLASSES[h.outcome] : 'text-neutral-600'}`}>
                        {h.outcome ?? '--'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
            <h2 className="text-sm font-medium text-neutral-300 mb-3">各交易員判斷</h2>
            {tradersLoading && <p className="text-neutral-500 text-sm">載入中…</p>}
            {tradersError && (
              <p className="text-rose-400 text-sm">載入失敗：{tradersError}</p>
            )}
            {!tradersLoading && !tradersError && tradersData === null && (
              <p className="text-neutral-600 italic text-sm">
                此標的尚無交易員判斷紀錄 —— Analyst 消息薄時多為 skip，Chartist 需有技術型態才出判斷。
              </p>
            )}
            {tradersData && tradersData.traders.length === 0 && (
              <p className="text-neutral-600 italic text-sm">此交易日無交易員判斷。</p>
            )}
            {tradersData && tradersData.traders.length > 0 && (
              <div className="space-y-3">
                {tradersData.consensus.n_traders > 1 && (
                  <p className="text-xs text-neutral-500 mb-1">
                    {tradersData.consensus.agree
                      ? `${tradersData.consensus.n_traders} 位交易員共識：${tradersData.consensus.directions.map((d) => DIRECTION_LABEL[d] ?? d).join('、')}`
                      : `${tradersData.consensus.n_traders} 位交易員有分歧：${tradersData.consensus.directions.map((d) => DIRECTION_LABEL[d] ?? d).join(' vs ')}`}
                  </p>
                )}
                {tradersData.traders.map((t) => (
                  <div
                    key={t.trader_id}
                    className="border-b border-neutral-800 pb-3 last:border-0 last:pb-0"
                  >
                    <div className="flex items-start justify-between mb-1">
                      <span className="text-sm text-neutral-200">{t.trader_id}</span>
                      <span
                        className={`text-xs font-medium px-2 py-0.5 rounded border ${
                          DIRECTION_CLASSES[t.direction] ?? DIRECTION_CLASSES.range
                        }`}
                      >
                        {DIRECTION_LABEL[t.direction] ?? t.direction} {pct(t.conviction)}
                      </span>
                    </div>
                    {t.rationale && (
                      <p className="text-xs text-neutral-400">{t.rationale}</p>
                    )}
                    {t.invalidation && (
                      <p className="text-xs text-neutral-600 mt-0.5">
                        失效條件：{t.invalidation}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
