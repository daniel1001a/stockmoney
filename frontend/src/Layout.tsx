import { Link, NavLink, Outlet } from 'react-router-dom'

const NAV_ITEMS = [
  { to: '/', label: '今日機會', end: true },
  { to: '/catalysts', label: '消息雷達', end: false },
  { to: '/predictions', label: '戰績', end: false },
  { to: '/positions', label: '持倉風控', end: false },
  { to: '/league', label: '聯賽', end: false },
]

export default function Layout() {
  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-100">
      <header className="border-b border-neutral-800 bg-neutral-950/95 sticky top-0 z-10 backdrop-blur">
        <div className="max-w-6xl mx-auto px-4 py-3 flex items-baseline justify-between">
          <div className="flex items-baseline gap-6">
            <Link to="/" className="text-lg font-semibold tracking-tight text-neutral-50">
              stockmoney
            </Link>
            <nav className="flex gap-4">
              {NAV_ITEMS.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    `text-sm ${isActive ? 'text-neutral-50 font-medium' : 'text-neutral-500 hover:text-neutral-300'}`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>
          <p className="text-xs text-neutral-500">僅供裁量參考，不下單、不改單、不撤單。</p>
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
