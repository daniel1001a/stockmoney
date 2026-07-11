import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from './Layout'
import Opportunities from './pages/Opportunities'
import TickerDetail from './pages/TickerDetail'
import CatalystRadar from './pages/CatalystRadar'
import TrackRecord from './pages/TrackRecord'
import PositionsRisk from './pages/PositionsRisk'
import League from './pages/League'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Opportunities />} />
          <Route path="ticker/:symbol" element={<TickerDetail />} />
          <Route path="catalysts" element={<CatalystRadar />} />
          <Route path="predictions" element={<TrackRecord />} />
          <Route path="positions" element={<PositionsRisk />} />
          <Route path="league" element={<League />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
