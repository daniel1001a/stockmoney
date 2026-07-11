import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { useApi } from '../lib/useApi'
import { DIRECTION_CLASSES, DIRECTION_LABEL, num, pct } from '../lib/format'
import ProbaBar from '../components/ProbaBar'
import Sparkline from '../components/Sparkline'

const OUTCOME_CLASSES: Record<string, string> = {
  win: 'text-emerald-400',
  loss: 'text-rose-400',
}

export default function TickerDetail() {
  const { symbol = '' } = useParams()
  const { data, loading, error } = useApi(() => api.ticker(symbol), [symbol])

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
                回測（GMM，樣本外）：準確率 {pct(data.backtest.overall_accuracy)}（n={data.backtest.overall_n}） · Brier{' '}
                {num(data.backtest.overall_brier, 3)} · Sharpe {num(data.backtest.overall_sharpe, 2)}
                {data.backtest.ev_of_continuing_now !== null && (
                  <> · 目前續抱EV {num(data.backtest.ev_of_continuing_now, 4)}</>
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
                  <span className="text-neutral-500">{k}</span>
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
        </div>
      )}
    </div>
  )
}
