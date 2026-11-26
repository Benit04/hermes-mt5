import json
import os
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from hermes_engine import HermesEngine

app = Flask(__name__)
app.secret_key = "hermes-secret-session-key-change-me"

KEYS_PATH = "keys.json"
ADMIN_PIN = "1234"  # Change this to your own secret PIN!

PLAN_DURATIONS = {
    "24-Hour Trial": timedelta(hours=24),
    "Monthly": timedelta(days=30),
    "3-Month": timedelta(days=90),
    "Lifetime": None,
}


def load_keys():
    if not os.path.isfile(KEYS_PATH):
        return []
    with open(KEYS_PATH, "r", encoding="utf-8", errors="replace") as f:
        return json.load(f)


def save_keys(keys):
    with open(KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2)


def validate_key_detailed(key_str):
    if not key_str:
        return False, "Please enter a valid Product Key."
    clean = key_str.strip().upper()
    for item in load_keys():
        if item.get("key") == clean:
            if not item.get("active", True):
                return False, "This Product Key is disabled. Contact support to renew or activate a key."
            exp = item.get("expires_at")
            if exp:
                exp_dt = datetime.strptime(exp, "%Y-%m-%d %H:%M:%S")
                if datetime.now() > exp_dt:
                    return False, (
                        f"Your Product Key expired on {exp} and the robot has stopped. "
                        "Please contact support on WhatsApp (081 311 8398) or Email (molelekwakemisetso12@gmail.com) to renew your key."
                    )
            return True, "Key valid."
    return False, "Invalid Product Key. Please check the key sent to your WhatsApp or Email."


engine = HermesEngine(key_validator=validate_key_detailed)


@app.route("/")
def master_file():
    return render_template("index.html")


@app.route("/robot", methods=["GET", "POST"])
def robot_portal():
    error = None
    message = None

    if request.method == "POST":
        action = request.form.get("action")
        if action == "stop":
            engine.stop()
            message = "Robot stopped."
        elif action == "start":
            product_key = request.form.get("product_key", "").strip().upper()
            mt5_login = request.form.get("mt5_login", "").strip()
            mt5_password = request.form.get("mt5_password", "")
            mt5_server = request.form.get("mt5_server", "").strip()
            symbol = request.form.get("symbol", "EURUSD").strip().upper()

            valid_key, key_reason = validate_key_detailed(product_key)
            if not valid_key:
                error = key_reason
            elif not mt5_login.isdigit() or not mt5_password or not mt5_server:
                error = "Please enter a valid MT5 Login ID, Password, and Broker Server."
            else:
                ok, msg = engine.start(
                    mt5_login=mt5_login,
                    mt5_password=mt5_password,
                    mt5_server=mt5_server,
                    symbol=symbol,
                    risk_pct=1.0,
                )
                if ok:
                    message = msg
                else:
                    error = msg

    return render_template(
        "robot.html",
        running=engine.running,
        status=engine.status,
        error=error,
        message=message,
    )


@app.route("/api/status")
def api_status():
    with engine.lock:
        return jsonify({
            "running": engine.running,
            "status": engine.status,
            "account": engine.account_summary,
            "feed": list(engine.live_feed),
        })


@app.route("/admin", methods=["GET", "POST"])
def admin_keys():
    if request.method == "POST" and request.form.get("pin"):
        if request.form.get("pin") == ADMIN_PIN:
            session["is_admin"] = True
        else:
            return render_template("admin.html", unlocked=False, error="Wrong Admin PIN.")

    if not session.get("is_admin"):
        return render_template("admin.html", unlocked=False, error=None)

    keys = load_keys()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "generate":
            buyer_contact = request.form.get("buyer_email", "").strip()
            plan = request.form.get("plan", "24-Hour Trial")
            now_dt = datetime.now()
            delta = PLAN_DURATIONS.get(plan)
            expires_at = (now_dt + delta).strftime("%Y-%m-%d %H:%M:%S") if delta else None

            new_key = f"HERMES-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
            keys.insert(0, {
                "key": new_key,
                "buyer_email": buyer_contact,
                "plan": plan,
                "created": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "expires_at": expires_at,
                "active": True,
            })
            save_keys(keys)
            return redirect(url_for("admin_keys"))

        elif action == "toggle":
            target = request.form.get("key")
            for k in keys:
                if k["key"] == target:
                    k["active"] = not k["active"]
            save_keys(keys)
            return redirect(url_for("admin_keys"))

    return render_template("admin.html", unlocked=True, keys=keys, error=None)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
