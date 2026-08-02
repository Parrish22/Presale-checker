import os
import time
from datetime import datetime, timezone
import cloudscraper

# Environment Variable for Discord Webhook
DISCORD_PRESALE_WEBHOOK_URL = os.getenv("DISCORD_PRESALE_WEBHOOK_URL")

# Tracking Caches to prevent duplicate posts
ALERTED_POOLS_CACHE = {}      # { pool_id: timestamp }
ALERTED_SYMBOLS_CACHE = {}    # { symbol: timestamp }
PRESALE_COOLDOWN_SECONDS = 24 * 3600  # 24-hour cooldown

# Initialize Cloudscraper session with standard desktop browser signature
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)


def clean_caches():
    """Purges expired items from cache."""
    now = time.time()
    for pool in list(ALERTED_POOLS_CACHE.keys()):
        if now - ALERTED_POOLS_CACHE[pool] > PRESALE_COOLDOWN_SECONDS:
            del ALERTED_POOLS_CACHE[pool]

    for sym in list(ALERTED_SYMBOLS_CACHE.keys()):
        if now - ALERTED_SYMBOLS_CACHE[sym] > (12 * 3600):
            del ALERTED_SYMBOLS_CACHE[sym]


def is_already_alerted(pool_id, symbol=""):
    clean_caches()
    pool_match = str(pool_id).lower() in ALERTED_POOLS_CACHE
    symbol_match = str(symbol).upper() in ALERTED_SYMBOLS_CACHE if symbol else False
    return pool_match or symbol_match


def record_alerted_presale(pool_id, symbol=""):
    ALERTED_POOLS_CACHE[str(pool_id).lower()] = time.time()
    if symbol:
        ALERTED_SYMBOLS_CACHE[str(symbol).upper()] = time.time()


def build_pinksale_url(pool_id, chain):
    """Guarantees valid PinkSale Launchpad URL."""
    c = str(chain).upper()
    if c in ["ETHEREUM", "ROBINHOOD_ETH"]: c = "ETH"
    elif c in ["SOLANA"]: c = "SOL"
    elif c in ["BINANCE", "BNB"]: c = "BSC"

    return f"https://www.pinksale.finance/launchpad/{pool_id}?chain={c}"


def is_hardcap_active(pool_data):
    """
    STRICT HARD CAP FILTER:
    Rejects presales where status is 'filled', 'completed', or 'ended',
    or where raised funds hit 100% of the Hard Cap.
    """
    status = str(pool_data.get("status", "")).lower()
    if status in ["filled", "completed", "ended", "cancelled", "success"]:
        return False

    try:
        raised = float(pool_data.get("totalRaised", 0) or 0)
        hard_cap = float(pool_data.get("hardCap", 0) or 0)
        if hard_cap > 0 and raised >= hard_cap:
            return False  # Presale is filled
    except Exception:
        pass

    return True


def send_pinksale_discord_alert(presale_data):
    """Dispatches callout directly linking to PinkSale.com."""
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_PRESALE_WEBHOOK_URL environment variable is missing!")
        return

    pinksale_url = presale_data["pinksaleUrl"]
    net_display = presale_data['network'].upper()
    if net_display in ["ETHEREUM", "ETH"]:
        net_display = "ROBINHOOD ETH"

    embed = {
        "title": f"🔥 [{net_display}] LIVE PINKSALE PRESALE: ${presale_data['symbol']}",
        "url": pinksale_url,
        "color": 16738816,  # Flame Orange
        "thumbnail": {"url": presale_data.get("image") or "https://www.pinksale.finance/static/media/logo.f081edeb.png"},
        "fields": [
            {"name": "Token Name", "value": presale_data["name"], "inline": True},
            {"name": "Symbol", "value": f"${presale_data['symbol']}", "inline": True},
            {"name": "Network", "value": net_display, "inline": True},
            {
                "name": "Hard Cap / Soft Cap",
                "value": f"🎯 Hard Cap: {presale_data.get('hardCap', 'N/A')} {presale_data.get('currency', '')}\n🛡️ Soft Cap: {presale_data.get('softCap', 'N/A')} {presale_data.get('currency', '')}",
                "inline": False
            },
            {
                "name": "Direct PinkSale Link",
                "value": f"👉 **[Click Here to Join Presale on PinkSale.com]({pinksale_url})**",
                "inline": False
            },
            {
                "name": "Project Social (X / Twitter)",
                "value": f"[View Project Twitter/X Profile]({presale_data.get('twitter')})" if presale_data.get('twitter') and presale_data.get('twitter') != '#' else "None Provided",
                "inline": False
            },
            {
                "name": "Pool Address",
                "value": f"`{presale_data['poolId']}`",
                "inline": False
            }
        ],
        "footer": {"text": "Local Resident Runner • PinkSale Hard Cap Method"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    try:
        res = scraper.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload, timeout=8)
        if res.status_code in [200, 204]:
            print(f"✅ PINKSALE DISCORD ALERT SENT: ${presale_data['symbol']} -> {pinksale_url}")
            record_alerted_presale(presale_data["poolId"], presale_data["symbol"])
        else:
            print(f"❌ Discord Post Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"❌ Discord Post Exception: {e}")


def fetch_pinksale_hardcap_presales():
    """Queries PinkSale active pool directory using Cloudscraper over local ISP connection."""
    print("🔍 Fetching Active Hard Cap Presales from PinkSale...")

    target_chains = ["bsc", "ethereum", "solana", "sol", "eth", "bnb"]
    
    endpoints = [
        "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=40&status=active",
        "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=40&status=upcoming"
    ]

    found_count = 0

    for url in endpoints:
        try:
            # Execute cloudscraper GET request over local residential network
            res = scraper.get(url, timeout=15)
            print(f"  └─ Request {url} -> HTTP Status Code: {res.status_code}")

            if res.status_code != 200:
                print(f"     ⚠️ Non-200 Status Received. Content Sample: {res.text[:150]}")
                continue

            data = res.json()
            pools = data.get("docs", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

            for pool in pools:
                pool_id = pool.get("id") or pool.get("poolAddress")
                chain = pool.get("chain", "").lower()

                if not pool_id or chain not in target_chains:
                    continue

                token_obj = pool.get("token", {}) or {}
                token_symbol = token_obj.get("symbol") or pool.get("symbol") or "PRESALE"
                token_name = token_obj.get("name") or pool.get("name") or "PinkSale Presale"

                if is_already_alerted(pool_id, token_symbol):
                    continue

                pinksale_url = build_pinksale_url(pool_id, chain)

                presale_payload = {
                    "poolId": str(pool_id),
                    "name": str(token_name)[:28],
                    "symbol": str(token_symbol)[:10],
                    "network": chain,
                    "pinksaleUrl": pinksale_url,
                    "softCap": pool.get("softCap", "N/A"),
                    "hardCap": pool.get("hardCap", "N/A"),
                    "totalRaised": pool.get("totalRaised", 0),
                    "currency": pool.get("currencySymbol", "BNB"),
                    "twitter": pool.get("twitter", "#"),
                    "image": pool.get("logo", ""),
                    "status": pool.get("status", "active")
                }

                if not is_hardcap_active(presale_payload):
                    continue

                send_pinksale_discord_alert(presale_payload)
                found_count += 1

        except Exception as e:
            print(f"❌ Cloudscraper Exception on {url}: {e}")

    print(f"--- Scan Completed. Dispatched {found_count} new PinkSale presales. ---")


def run_screener():
    print(f"\n--- Starting PinkSale Scan at {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ---")
    fetch_pinksale_hardcap_presales()


if __name__ == "__main__":
    print("PinkSale Local Screener Active (Running on Residential Network)...")
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("⚠️ WARNING: DISCORD_PRESALE_WEBHOOK_URL environment variable is NOT set locally!")
    while True:
        try:
            run_screener()
        except Exception as e:
            print(f"Loop Exception: {e}")
        time.sleep(120)  # Scan every 2 minutes
