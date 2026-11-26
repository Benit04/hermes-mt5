import csv
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
import pandas as pd
try:
    import MetaTrader5 as mt5 
    except ImportError:
        mt5 = None
LOG_PATH = "trade_log.csv"
SAST = timezone(timedelta(hours=2))


class HermesEngine:
    def __init__(self, key_validator=None):
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        self.live_feed = deque(maxlen=50)
        self.status = "Idle (Waiting to start)"
        self.account_summary = {}
        self.active_key = None
        self.key_validator = key_validator

        self.push_log(
            "NOTICE",
            "Reminder: Hermes is automated software and makes mistakes. Markets change quickly—always test carefully and never risk money you cannot afford to lose.",
        )

    def push_log(self, stage, message):
        now_str = datetime.now(SAST).strftime("%H:%M:%S")
        entry = {"time": now_str, "stage": stage, "message": message}
        with self.lock:
            self.live_feed.appendleft(entry)
            self.status = f"[{stage}] {message}"
        print(f"[{now_str}] [{stage}] {message}")

    def record_trade_csv(self, action, symbol, direction, lot, price, sl, tp, reason):
        file_exists = os.path.isfile(LOG_PATH)
        with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp_sast", "action", "symbol", "direction", "lot", "price", "sl", "tp", "reason"])
            now_str = datetime.now(SAST).strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([now_str, action, symbol, direction, lot, price, sl, tp, reason])

    def start(self, mt5_login, mt5_password, mt5_server, symbol="EURUSD", risk_pct=1.0, sl_pips=25, rr_ratio=2.0, product_key=""):
        if self.running:
            return False, "Hermes engine is already running."

        self.running = True
        self.active_key = product_key.strip().upper()
        self.thread = threading.Thread(
            target=self._run_loop,
            args=(int(mt5_login), mt5_password, mt5_server, symbol.upper(), float(risk_pct), int(sl_pips), float(rr_ratio)),
            daemon=True,
        )
        self.thread.start()
        return True, "Hermes engine started."

    def stop(self, reason="User stopped the robot. Shutting down MT5 connection."):
        self.running = False
        self.push_log("STOPPED", reason)

    def _check_key_still_valid(self):
        if not self.key_validator:
            return True, "OK"
        ok, reason = self.key_validator(self.active_key)
        return ok, reason

    def _calculate_1pct_lot(self, symbol_info, equity, risk_pct, sl_pips):
        risk_money = equity * (risk_pct / 100.0)
        point = symbol_info.point
        pip_size = point * 10 if symbol_info.digits in (3, 5) else point
        tick_value = symbol_info.trade_tick_value
        tick_size = symbol_info.trade_tick_size

        if tick_value <= 0 or tick_size <= 0:
            return symbol_info.volume_min, risk_money

        sl_distance = sl_pips * pip_size
        loss_per_lot = (sl_distance / tick_size) * tick_value
        raw_lot = risk_money / loss_per_lot if loss_per_lot > 0 else symbol_info.volume_min

        step = symbol_info.volume_step
        lot = max(symbol_info.volume_min, round(raw_lot / step) * step)
        lot = min(lot, symbol_info.volume_max)
        return round(lot, 2), round(risk_money, 2)

    def _predict_market(self, symbol):
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 220)
        if rates is None or len(rates) < 205:
            return None, "Waiting for 200+ M15 candles from broker..."

        df = pd.DataFrame(rates)
        df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        candle = df.iloc[-2]
        price = float(candle["close"])
        ema200 = float(candle["ema200"])
        rsi = float(candle["rsi"])

        if price > ema200 and 52 <= rsi <= 68:
            return "BUY", f"Bullish trend: Price {price:.5f} > 200 EMA {ema200:.5f} | RSI {rsi:.1f}"
        if price < ema200 and 32 <= rsi <= 48:
            return "SELL", f"Bearish trend: Price {price:.5f} < 200 EMA {ema200:.5f} | RSI {rsi:.1f}"

        return None, f"Scanning {symbol}: Price {price:.5f} vs 200 EMA {ema200:.5f} | RSI {rsi:.1f} (No entry)"

    def _run_loop(self, mt5_login, mt5_password, mt5_server, symbol, risk_pct, sl_pips, rr_ratio):
        magic_number = 77001
        valid, reason = self._check_key_still_valid()
        if not valid:
            self.push_log("KEY_EXPIRED", reason)
            self.running = False
            return

        self.push_log("CONNECTING", f"Connecting to MT5 account {mt5_login} on {mt5_server}...")
         
         if mt5 is None:
             self.push_log("ERROR", "MT5 terminal is not available on this Linux web server.")
             self.running = False
             return
        if not mt5.initialize(login=mt5_login, password=mt5_password, server=mt5_server):
            self.push_log("ERROR", f"MT5 login failed: {mt5.last_error()}")
            self.running = False
            return

        acc = mt5.account_info()
        if acc is None:
            self.push_log("ERROR", "Connected to terminal, but could not read MT5 account details.")
            self.running = False
            mt5.shutdown()
            return

        mode_label = "DEMO" if acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL"
        peak_balance = acc.balance
        self.push_log("CONNECTED", f"Logged in ({mode_label}) | Balance: {acc.balance:.2f} {acc.currency}")

        try:
            while self.running:
                # Re-check key validity/expiration every loop
                valid, key_msg = self._check_key_still_valid()
                if not valid:
                    self.push_log("KEY_EXPIRED", key_msg)
                    self.running = False
                    break

                acc = mt5.account_info()
                if acc is None:
                    self.push_log("WARNING", "Lost MT5 account connection, retrying...")
                    time.sleep(5)
                    continue

                peak_balance = max(peak_balance, acc.balance)
                dd_pct = ((peak_balance - acc.equity) / peak_balance) * 100 if peak_balance > 0 else 0

                with self.lock:
                    self.account_summary = {
                        "login": acc.login,
                        "mode": mode_label,
                        "balance": round(acc.balance, 2),
                        "equity": round(acc.equity, 2),
                        "currency": acc.currency,
                        "drawdown_pct": round(dd_pct, 2),
                    }

                if dd_pct >= 10.0:
                    self.push_log("KILL_SWITCH", f"Drawdown reached {dd_pct:.2f}% (>=10%). Stopping robot to protect capital.")
                    self.running = False
                    break

                now_sast = datetime.now(SAST)
                if not (9 <= now_sast.hour < 17):
                    self.push_log("STANDBY", f"Outside London session (09:00-17:00 SAST). Current SAST: {now_sast.strftime('%H:%M')}.")
                    time.sleep(20)
                    continue

                start_day = datetime(now_sast.year, now_sast.month, now_sast.day, tzinfo=SAST)
                deals = mt5.history_deals_get(start_day, now_sast) or []
                todays_entries = sum(1 for d in deals if d.magic == magic_number and d.entry == mt5.DEAL_ENTRY_IN)
                todays_pnl = sum((d.profit + d.swap + d.commission) for d in deals if d.magic == magic_number and d.entry == mt5.DEAL_ENTRY_OUT)

                if todays_entries >= 2:
                    self.push_log("DAILY_CAP", "Max 2 trades reached for today. Resting until tomorrow.")
                    time.sleep(30)
                    continue

                if todays_pnl <= -(acc.balance * 0.02):
                    self.push_log("DAILY_LOCK", f"Daily loss limit (2%) hit ({todays_pnl:.2f}). Robot paused for today.")
                    time.sleep(30)
                    continue

                open_positions = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == magic_number]
                if open_positions:
                    pos = open_positions[0]
                    self.push_log("MANAGING", f"Managing open {symbol} trade | Floating P/L: {pos.profit:.2f} {acc.currency}")
                    time.sleep(10)
                    continue

                mt5.symbol_select(symbol, True)
                info = mt5.symbol_info(symbol)
                tick = mt5.symbol_info_tick(symbol)
                if info is None or tick is None:
                    self.push_log("WARNING", f"Symbol {symbol} not found in MT5 Market Watch.")
                    time.sleep(10)
                    continue

                pip_size = info.point * 10 if info.digits in (3, 5) else info.point
                spread_pips = (tick.ask - tick.bid) / pip_size
                if spread_pips > 2.5:
                    self.push_log("FILTERED", f"Spread too high ({spread_pips:.1f} pips > 2.5). Waiting for tighter spread.")
                    time.sleep(10)
                    continue

                signal, reason = self._predict_market(symbol)
                self.push_log("ANALYSIS", reason)

                if signal in ("BUY", "SELL"):
                    lot, risk_money = self._calculate_1pct_lot(info, acc.equity, risk_pct, sl_pips)
                    sl_dist = sl_pips * pip_size
                    tp_dist = sl_dist * rr_ratio

                    if signal == "BUY":
                        order_type = mt5.ORDER_TYPE_BUY
                        price = tick.ask
                        sl = round(price - sl_dist, info.digits)
                        tp = round(price + tp_dist, info.digits)
                    else:
                        order_type = mt5.ORDER_TYPE_SELL
                        price = tick.bid
                        sl = round(price + sl_dist, info.digits)
                        tp = round(price - tp_dist, info.digits)

                    self.push_log("1%_RULE", f"Risking {risk_pct}% ({risk_money} {acc.currency}) -> Lot: {lot} | SL: {sl} | TP: {tp}")

                    req = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": symbol,
                        "volume": lot,
                        "type": order_type,
                        "price": price,
                        "sl": sl,
                        "tp": tp,
                        "deviation": 20,
                        "magic": magic_number,
                        "comment": "Hermes_1Pct_Pro",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": mt5.ORDER_FILLING_IOC,
                    }
                    res = mt5.order_send(req)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        self.push_log("EXECUTED", f"{signal} {lot} {symbol} @ {price} (SL {sl}, TP {tp})")
                        self.record_trade_csv("OPENED", symbol, signal, lot, price, sl, tp, reason)
                    else:
                        err = res.comment if res else "Order rejected"
                        self.push_log("ORDER_ERR", f"Trade failed: {err}")

                time.sleep(10)
        finally:
            mt5.shutdown()
            self.running = False
            
            if __name__ == "__main__":
                port = int(os.environ.get("PORT",5000))
                app.run(host="0.0.0.0" port=port, debug=false)
