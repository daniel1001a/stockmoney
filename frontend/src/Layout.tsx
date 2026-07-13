import { Link, NavLink, Outlet } from 'react-router-dom'

const NAV_ITEMS = [
  { to: '/', label: '今日機會', end: true },
  { to: '/news', label: '消息雷達', end: false },
  { to: '/arena', label: '競技場', end: false },
]

export default function Layout() {
  return (
    <div className="min-h-screen bg-[#0b0c10] text-neutral-100">
      <header className="sticky top-0 z-20 border-b border-neutral-800 bg-[#0b0c10]/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-7">
            <Link to="/" className="flex items-baseline gap-2">
              <span className="text-lg font-bold tracking-tight text-neutral-50">stockmoney</span>
              <span className="hidden text-xs text-neutral-600 sm:inline">市場風向決策儀表板</span>
            </Link>
            <nav className="flex gap-1">
              {NAV_ITEMS.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    `rounded-md px-3 py-1.5 text-sm transition-colors ${
                      isActive
                        ? 'bg-neutral-800 font-medium text-neutral-50'
                        : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200'
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>
          <p className="hidden text-xs text-neutral-600 md:block">僅供裁量參考 · 不下單 · 不改單 · 不撤單</p>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
