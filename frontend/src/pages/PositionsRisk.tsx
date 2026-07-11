import { Link } from 'react-router-dom'
import { api, type PositionRisk } from '../lib/api'
import { useApi } from '../lib/useApi'
import { RISK_LIGHT_CLASSES, num } from '../lib/format'

const TRIGGER_LIGHT_CLASSES: Record<string, string> = {
  yellow: 'text-amber-300 border-amber-800 bg-amber-950',
  red: 'text-rose-400 border-rose-800 bg-rose-950',
}

function PositionCard({ position }: { position: PositionRisk }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
      <div className="flex items-start justify-between">
        <div>
          <Link to={`/ticker/${position.symbol}`} className="text-lg font-semibold text-neutral-50 hover:text-neutral-300">
            {position.symbol}
          </Link>
          <p className="text-xs text-neutral-500">
            {position.option_right} · {position.side} · 進場 {position.entry_date}
          </p>
        </div>
        <span className={`w-3 h-3 rounded-full mt-1.5 ${RISK_LIGHT_CLASSES[position.light]}`} title={position.light} />
      </div>

      <div className="mt-3 flex gap-4 text-xs text-neutral-500">
        <span>進場標的價 ${num(position.entry_underlying_price)}</span>
        <span>目前標的價 ${num(position.current_underlying_price)}</span>
        <span>進場權利金 ${num(position.entry_premium)}</span>
      </div>
      <p className="mt-1 text-xs text-neutral-600">資料截至 {position.as_of_date}</p>

      {position.triggers.length > 0 && (
        <div className="mt-3 space-y-1.5">
          {position.triggers.map((t, i) => (
            <div
              key={i}
              className={`text-xs px-2 py-1 rounded border ${TRIGGER_LIGHT_CLASSES[t.light] ?? TRIGGER_LIGHT_CLASSES.yellow}`}
            >
              <span className="font-medium">[{t.kind}]</span> {t.detail}
            </div>
          ))}
        </div>
      )}

      {position.notes.length > 0 && (
        <div className="mt-2 space-y-0.5">
          {position.notes.map((n, i) => (
            <p key={i} className="text-xs text-neutral-600">
              (略過) {n}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

export default function PositionsRisk() {
  const { data, loading, error } = useApi(api.positions)

  return (
    <div>
      <h1 className="text-2xl font-semibold text-neutral-50 mb-1">持倉風控</h1>
      <p className="text-sm text-neutral-500 mb-6">
        燈號反映的是上個交易日收盤後的快取數字，不是即時報價。僅供裁量參考，不下單、不改單、不撤單。
      </p>

      {loading && <p className="text-neutral-500">載入中…</p>}
      {error && <p className="text-rose-400">載入失敗：{error}</p>}

      {data && data.length === 0 && (
        <p className="text-neutral-500">
          目前沒有開倉部位。用 <code className="text-neutral-400">scripts/options_positions_cli.py open ...</code> 登記一筆。
        </p>
      )}

      {data && data.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {data.map((p) => (
            <PositionCard key={p.position_id} position={p} />
          ))}
        </div>
      )}
    </div>
  )
}
