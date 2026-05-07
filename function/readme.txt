Yes, it's a scalper. It trades gold (XAU/USD) 
every few minutes, targeting small, 
fast profits. 

Here's each piece explained simply:

xau.py — the main trader. This is the heart of the system. 
Every single minute it wakes up, looks at the last 100 five-minute gold candles, and asks three questions: 
(1) Are the two moving averages (EMA 5 and EMA 13) crossing in a promising direction? 

(2) Is the RSI momentum indicator in the right zone? (3) Is the broader H1 and H4 trend aligned? 
If all three say yes, it calls the AI for a second opinion before pulling the trigger.

The dual AI check. Before placing any trade, the bot asks two separate AI models on Groq (a free AI API): 
a big 70-billion-parameter model for the main decision, and a smaller 8-billion-parameter model for a quick sanity check. 
Both must say "CONFIRM" for the trade to go through. This is your extra safety net against bad signals. 

When the free daily token budget runs out (~285 AI calls per day), it falls back to pure math rules so it never goes blind.
advisor.py — the self-tuning coach. 

This runs separately every 30 minutes. It looks at your real trade history directly from MT5, calculates your win rate, 
profit factor, and losing streaks, then sends all that to a more powerful AI model (Llama 4 Scout). 
The AI recommends specific tweaks — tighten the RSI filter, widen the stop loss, 
reduce position size — and those changes get written to scuro_config.json. 

The main bot picks them up automatically within a minute without needing a restart. It's the bot training itself.
mt5_history.py — the recorder. 

Runs in a third terminal, refreshing every 10 seconds. It pulls your live account data, open positions, 
closed trade history, equity curve, and current RSI/ATR readings, then saves everything to scuro_live_data.json. 
This is the data pipe that feeds the dashboard.
upload_to_supabase.py — the cloud bridge. 

Reads that live JSON file and pushes it to your Supabase database every 10 seconds, 
so the dashboard is accessible from any device, not just the machine running MT5.