import csv
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

LOG_PATH = "trade_log.csv"
SAST = timezone(timedelta(hours=2))


class HermesEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.live_feed = deque(maxlen=200)
        self.status = "Idle"
        self.running = False
        self._stop_event = threading.Event()
        self._thread = None

    def push_log(self, stage, message):
        now_str = datetime.now(SAST).strftime("%H:%M:%S")
        entry = {"time": now_str, "stage": stage, "message": message}
        with self.lock:
            self.live_feed.appendleft(entry)
            self.status = f"[{stage}] {message}"
        print(f"[{now_str}] [{stage}] {message}")

    def record_trade_csv(self, action, symbol, direction, lot, price, sl, tp, reason):
        file_exists = os.path.isfile(LOG_PATH)
        now_str = datetime.now(SAST).strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp_sast",
                    "action",
                    "symbol",
                    "direction",
                    "lot",
                    "price",
                    "sl",
                    "tp",
                    "reason",
                ])
            writer.writerow([now_str, action, symbol, direction, lot, price, sl, tp, reason])

    def start(
        self,
        mt5_login,
        mt5_password,
        mt5_server,
        symbol="EURUSD",
        risk_pct=1.0,
        sl_plus=25,
        re_entry=2.0,
        **kwargs,
    ):
        with self.lock:
            if self.running:
                return False, "Hermes engine is already running."

            if mt5 is None:
                self.status = "MT5 unavailable in cloud environment"
                self.push_log(
                    "ENV",
                    "MetaTrader5 package is not available on Linux/Render; running in standby mode.",
                )
                return True, "Hermes started in cloud standby mode (no MT5 terminal on server)."

            self._stop_event.clear()
            self.running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(mt5_login, mt5_password, mt5_server, symbol, risk_pct, sl_plus, re_entry),
                daemon=True,
            )
            self._thread.start()

        self.push_log("START", f"Hermes initialized for {symbol}.")
        return True, "Hermes engine started."

    def stop(self):
        self._stop_event.set()
        with self.lock:
            self.running = False
            self.status = "Stopped"
        self.push_log("STOP", "Hermes engine stopped.")
        return True, "Hermes engine stopped."

    def _run_loop(self, mt5_login, mt5_password, mt5_server, symbol, risk_pct, sl_plus, re_entry):
        try:
            if not mt5.initialize():
                self.push_log("MT5", "Failed to initialize MetaTrader 5 terminal.")
                return

            if mt5_login and mt5_password and mt5_server:
                authorized = mt5.login(
                    login=int(mt5_login),
                    password=str(mt5_password),
                    server=str(mt5_server),
                )
                if not authorized:
                    self.push_log("MT5", f"Login failed for account {mt5_login}.")
                    return

            self.push_log("READY", f"Connected and monitoring {symbol} (risk={risk_pct}%).")

            while not self._stop_event.is_set():
                tick = mt5.symbol_info_tick(symbol)
                if tick is not None:
                    self.push_log("TICK", f"{symbol} bid={tick.bid} ask={tick.ask}")
                time.sleep(5)
        except Exception as exc:
            self.push_log("ERROR", str(exc))
        finally:
            with self.lock:
                self.running = False
                if mt5 is not None:
                    try:
                        mt5.shutdown()
                    except Exception:
                        pass
