# 實驗三報告 — 真實 GDELT 新聞訊號有沒有加值

> 三行摘要:**做了**——回填完整 8 年(2018–2026)真實 GDELT GKG per-symbol tone/volume(130 萬列),建 leak-safe 新聞特徵,加兩個記分板策略跑 walk-forward OOS。**結論**——**null,新聞訊號無加值**:新聞催化做方向顯著虧損、輸給隨機與做多;新聞風險閘門實質等於裸賣。**下一步**——見末節的岔路;不建議再追 GDELT,除非升級到真標題資料源。

## 數字(walk-forward OOS,扣 theta+spread)

| 策略 | horizon | n | 勝率 | mean_ret | 95% CI | 對照 |
|---|---|---|---|---|---|---|
| news_catalyst_dir | 5 | 429 | 32.4% | −0.0773 | [−0.148,−0.003] | 顯著虧,輸 B2_random & long_stock |
| news_catalyst_dir | 3 | 429 | 35.4% | −0.0457 | [−0.101,+0.014] | 輸 B2_random & long_stock |
| sellput_otm5_newsgated | 5 | 26,773 | 90.5% | +0.0004 | [+0.0001,+0.0007]* | 略輸 sellput_otm5_naive(+0.0005) |
| sellput_otm5_newsgated | 3 | 25,547 | 93.1% | +0.0001 | [−0.0001,+0.0003] | 略輸 naive |
| (對照)sellput_otm5_naive | 5 | 29,596 | 90.4% | +0.0005 | [+0.0003,+0.0008]* | — |
| (對照)sellput_otm5 價格閘門 | 5 | 14,677 | 92.1% | +0.0001 | [−0.0002,+0.0004] | 不顯著 |
| (對照)long_stock | 5 | 31,130 | 54.4% | +0.0049 | [+0.0044,+0.0055]* | 仍是最強 |

## 判讀
1. **新聞催化做方向:輸,且 h5 顯著虧損(−7.7%)。** 真實新聞也無法預測 1-3 天方向(GDELT 方向第二次失敗)。
2. **新聞風險閘門:實質等於裸賣。** 亮點是它保住顯著正報酬(不像價格閘門殺到不顯著),但觸發太少(僅少 ~10% 交易),既沒明顯砍尾端也沒改善報酬。

## 但書(為何 null 不等於「新聞無用」定論)
GKG per-symbol 是「有提到」非「主要在講」(噪音),且僅 tone、無真標題。null 同時相容於「新聞對 1-3 天沒用」與「GKG 太雜訊」。更高品質測試(真標題 DOC 2.0 API / 付費 per-ticker feed)未完成(沙盒 429)。

## 資產(可重用)
- `scripts/backfill_news_real.py` — 真 GDELT GKG 回填(8y 免費可行,~357GB 在 1TB/月內)。
- `data/news_backfill/` — 130 萬列真實新聞(2018–2026,9 檔)。
- `src/stockmoney/backtest/news_features.py` — leak-safe 每日新聞特徵。
- `src/stockmoney/backtest/news_strategies.py` — 兩個記分板 plug-in。
- `scripts/news_experiment_cli.py` — 一鍵重跑。

## 全局結論(五實驗)
`long_stock`(單純持股)勝過所有測過的期權策略。純價格與真實新聞訊號,沒有一個在 1-3 天期權上打贏「單純做多」。可靠報酬只有 equity beta(做多)與微小且尾端兇的 vol premium(sell-put)。
