// Plain-language explanations for jargon terms shown across the app.
// Descriptions are derived from the actual computation logic (see
// src/stockmoney/models/*.py, src/stockmoney/api/queries.py) -- not guesses.
export const GLOSSARY: Record<string, string> = {
  conviction: '模型對這個方向判斷的信心，等於機率分佈中最高的那一項（漲/跌/盤整三選一取最大值）。',
  regime: '目前市場狀態分類（趨勢多頭／趨勢空頭／震盪盤整），依已實現波動率、趨勢強度、跨股離散度判斷。',
  overall_accuracy: '樣本外回測的方向判斷準確率，跟瞎猜基準（約1/3）比較才有意義。',
  overall_brier: 'Brier分數：機率預測的校準程度，越接近0代表機率估計越準，不是單純的對錯率。',
  overall_sharpe: '樣本外回測的風險調整後報酬（Sharpe ratio），越高代表相同風險下報酬越好。',
  ev_of_continuing_now: '模組B算出的「現在續抱」期望值，負值代表現在平倉的期望值高於繼續持有。',
  ev_passed_win_rate: 'EV閘門判定「可交易」（落在過去90日EV分佈前25%）的那些交易，實際勝率。',
  ev_blocked_win_rate: 'EV閘門判定「不可交易」而被擋下的那些交易，事後驗證的勝率（用來檢查閘門有沒有篩選力）。',
  realized_vol_20d: '過去20個交易日的已實現波動率，衡量價格擺動劇烈程度。',
  adx_14: '14日ADX趨勢強度指標，數值越高代表目前越像單邊趨勢、越低代表越像盤整。',
  xsec_dispersion: '同板塊個股報酬率的離散度，突然飆升代表市場正在用消息篩選贏家輸家，不是齊漲齊跌。',
  yield_curve_10y2y: '10年期減2年期公債殖利率利差，總經風向的代理指標。',
  yield_curve_10y2y_ffill: '10年期減2年期公債殖利率利差，forward-fill補值以避免發布延遲（含債市休市日）拖累整組特徵。',
  dxy_chg_1d_ffill: '美元指數（DXY）單日變化率，forward-fill補值以避免資料延遲拖累整組特徵。',
  oil_chg_1d_ffill: '原油價格單日變化率，forward-fill補值以避免資料延遲拖累整組特徵。',
  novelty_score: '這則消息催化劑的新穎度，越高代表越不是老新聞重炒。',
  sentiment_score: '這則消息催化劑的情緒分數，正值偏多、負值偏空。',
  priced_in_estimate: '估計市場「已經消化」這則消息的程度，越低代表市場可能還沒完全反映。',
  price_stop: '標的價格逆勢波動達進場時IV換算的-2個標準差時觸發，代表走勢已嚴重不利於買方部位。',
  premium_stop: '權利金較進場成本下跌達50%時觸發，買方部位建議停損。',
  take_profit: '權利金較進場成本上漲達100%時觸發，買方部位達固定停利目標。',
  dynamic_ev_take_profit: '模組B算出「續抱」的期望值已轉負，代表現在平倉比繼續持有划算。',
  seller_early_close: '賣方（short）獲利已達進場權利金的50%~80%，建議提前平倉以規避到期釘住風險（pin risk）。',
  regime_invalidation: '目前市場狀態與進場時不同，代表原本進場邏輯的前提已改變，需重新評估。',
}
