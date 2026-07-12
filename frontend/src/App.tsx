import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Layout from './Layout'
import Opportunities from './pages/Opportunities'
import TickerDetail from './pages/TickerDetail'
import NewsRadar from './pages/NewsRadar'
import NewsDetail from './pages/NewsDetail'
import Arena from './pages/Arena'
import TraderProfile from './pages/TraderProfile'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Opportunities />} />
          <Route path="ticker/:symbol" element={<TickerDetail />} />
          <Route path="news" element={<NewsRadar />} />
          <Route path="news/:id" element={<NewsDetail />} />
          <Route path="arena" element={<Arena />} />
          <Route path="trader/:id" element={<TraderProfile />} />
          {/* legacy paths from the pre-redesign nav */}
          <Route path="catalysts" element={<Navigate to="/news" replace />} />
          <Route path="predictions" element={<Navigate to="/arena" replace />} />
          <Route path="positions" element={<Navigate to="/arena" replace />} />
          <Route path="league" element={<Navigate to="/arena" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
