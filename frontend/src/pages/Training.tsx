import { useApi } from '../lib/useApi'
import { api, type LeagueTrainingEntry, type TraderStats } from '../lib/api'
import { Card, Loading, ErrorMsg, Empty } from '../components/ui'
import Sparkline from '../components/Sparkline'

// 訓練表現:每位交易員的模型是否隨時間變強 -- 勝率走勢、各盤性別表現,以及系統
// 自動產生的「方法改進提案」與版本歷史(這是整套系統「訓練自己」的可見證據)。

function pct(x: number | null | undefined): string {
  return x === null || x === undefined ? '—' : `${Math.round(x * 100)}%`
}

function num(x: number | null | undefined, digits = 2): string {
  return x === null || x === undefined ? '—' : x.toFixed(digits)
}

// Regime cluster ids are arbitrary/unstable across fits (see models.regime), so
// we label them neutrally rather than guessing 多頭/空頭 from an id.
function regimeName(id: string): string {
  return `盤性 ${id}`
}

function StatCell({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-neutral-500">{label}</div>
      <div className="tabular-nums text-neutral-100">{value}</div>
    </div>
  )
}

function RegimeTable({ byRegime }: { byRegime: Record<string, TraderStats> }) {
  const rows = Object.entries(byRegime).filter(([, s]) => s.n_graded > 0)
  if (rows.length === 0) return <div className="text-sm text-neutral-600">資料不足(尚無已結算的分盤性紀錄)</div>
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-[11px] uppercase tracking-wide text-neutral-500">
          <th className="py-1 font-normal">盤性</th>
          <th className="py-1 text-right font-normal">方向命中率</th>
          <th className="py-1 text-right font-normal">已結算</th>
          <th className="py-1 text-right font-normal">累積選擇權損益</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([id, s]) => (
          <tr key={id} className="border-t border-neutral-900">
            <td className="py-1 text-neutral-300">{regimeName(id)}</td>
            <td className="py-1 text-right tabular-nums text-neutral-100">{pct(s.hit_rate)}</td>
            <td className="py-1 text-right tabular-nums text-neutral-400">{s.n_graded}</td>
            <td className="py-1 text-right tabular-nums text-neutral-400">{num(s.cum_option_pnl ?? 0)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// 共用經驗看板:把 5 位交易員各自的自我改進提案攤在同一張表裡,依日期排序 --
// 讓「B 交易員可以看到 A 交易員最近栽在哪裡」變得容易,而不用分別點開 5 張卡片。
// 純呈現層——不改變任何提案怎麼被產生或審核的邏輯(那仍是 review.py 的工作,
// 一樣需要人工核准才會真的變更 method)。
function SharedExperienceBoard({ data }: { data: LeagueTrainingEntry[] }) {
  const statusCn: Record<string, string> = { proposed: '提議中', accepted: '已採納', rejected: '已駁回' }
  const nameById = Object.fromEntries(data.map((t) => [t.trader_id, t.name]))
  const rows = data
    .flatMap((t) => t.proposals.map((p) => ({ ...p, trader_name: t.name })))
    .sort((a, b) => (b.source_review_date ?? '').localeCompare(a.source_review_date ?? ''))

  return (
    <Card className="mb-6 p-4">
      <div className="mb-1 text-base font-semibold text-neutral-50">共用經驗看板</div>
      <p className="mb-3 text-xs text-neutral-500">
        全部交易員的自我改進提案放在一起看,不分誰是誰的——各交易員仍各自遵守自己的方法(不會因為看到別人栽過跟頭就自動改邏輯),但至少「大家都看得到大家在哪裡跌倒」。目前 {rows.filter((r) => r.status === 'proposed').length} 筆待人工審核。
      </p>
      {rows.length === 0 ? (
        <div className="text-sm text-neutral-600">尚無任何交易員提出改進提案。</div>
      ) : (
        <ul className="space-y-2">
          {rows.map((p) => (
            <li key={p.proposal_id} className="flex flex-wrap items-baseline gap-x-2 text-sm">
              <span className="font-medium text-neutral-200">{nameById[p.trader_id] ?? p.trader_name}</span>
              <span
                className={
                  p.status === 'accepted' ? 'text-emerald-400'
                    : p.status === 'rejected' ? 'text-neutral-500'
                      : 'text-amber-300'
                }
              >
                {statusCn[p.status] ?? p.status}
              </span>
              {p.source_review_date && <span className="text-xs text-neutral-600">{p.source_review_date}</span>}
              <span className="text-neutral-400">{p.rationale}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

function ProposalLog({ t }: { t: LeagueTrainingEntry }) {
  const statusCn: Record<string, string> = { proposed: '提議中', accepted: '已採納', rejected: '已駁回' }
  return (
    <div>
      <div className="mb-1 text-[11px] uppercase tracking-wide text-neutral-500">
        方法版本 · {t.method_versions.map((v) => v.method_version).join(' → ') || '—'}
      </div>
      {t.proposals.length === 0 ? (
        <div className="text-sm text-neutral-600">尚無改進提案(系統覆盤後若發現可強化處會自動提出)</div>
      ) : (
        <ul className="space-y-1.5">
          {t.proposals.map((p) => (
            <li key={p.proposal_id} className="text-sm">
              <span
                className={
                  p.status === 'accepted'
                    ? 'text-emerald-400'
                    : p.status === 'rejected'
                      ? 'text-neutral-500'
                      : 'text-amber-300'
                }
              >
                {statusCn[p.status] ?? p.status}
              </span>
              <span className="ml-2 text-neutral-300">{p.rationale}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function TrainingCard({ t }: { t: LeagueTrainingEntry }) {
  const points = t.win_rate_series.map((p) => ({ value: p.hit_rate }))
  const latest = t.win_rate_series.at(-1)?.hit_rate ?? null
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-base font-semibold text-neutral-50">
            {t.name}
            {!t.active && <span className="ml-2 text-xs text-neutral-600">(已退役)</span>}
          </div>
          <div className="mt-0.5 max-w-2xl text-xs text-neutral-500">{t.philosophy}</div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
        <StatCell label="近窗命中率" value={pct(t.rolling.hit_rate)} />
        <StatCell label="已結算筆數" value={String(t.rolling.n_graded)} />
        <StatCell label="累積選擇權損益" value={num(t.overall.cum_option_pnl ?? 0)} />
        <StatCell label="機率校準 Brier" value={num(t.rolling.brier)} />
      </div>

      <div className="mt-4">
        <div className="mb-1 text-[11px] uppercase tracking-wide text-neutral-500">
          勝率走勢(依進場日,越右越新){latest !== null && ` · 目前 ${pct(latest)}`}
        </div>
        {points.length >= 2 ? (
          <Sparkline points={points} height={44} emptyMessage="資料不足" />
        ) : (
          <div className="text-sm text-neutral-600">資料不足(需更多已結算紀錄才畫得出走勢)</div>
        )}
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wide text-neutral-500">各盤性別表現</div>
          <RegimeTable byRegime={t.by_regime} />
        </div>
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wide text-neutral-500">模型自我改進</div>
          <ProposalLog t={t} />
        </div>
      </div>
    </Card>
  )
}

export default function Training() {
  const { data, loading, error } = useApi(api.leagueTraining)

  return (
    <div className="mx-auto max-w-6xl px-4 py-6">
      <h1 className="text-2xl font-bold text-neutral-50">訓練表現</h1>
      <p className="mt-1 text-sm text-neutral-500">
        每位交易員的模型是否隨時間變強:勝率走勢、各盤性別表現,以及系統覆盤後自動產生的方法改進提案。
        這是整套系統「訓練自己」的可見證據 — 樣本還少時數字會跳動,屬正常。
      </p>

      <div className="mt-5">
        {loading && <Loading />}
        {error && <ErrorMsg error={error} />}
        {!loading && !error && (!data || data.length === 0) && <Empty>尚無交易員訓練紀錄。</Empty>}
        {data && data.length > 0 && <SharedExperienceBoard data={data} />}
      </div>

      <div className="space-y-4">
        {data?.map((t) => <TrainingCard key={t.trader_id} t={t} />)}
      </div>
    </div>
  )
}
