import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests

# Environment Variable for Discord Webhook
DISCORD_PRESALE_WEBHOOK_URL = os.getenv("DISCORD_PRESALE_WEBHOOK_URL")

# Tracking & Deduplication Caches
ALERTED_PRESALES_CACHE = {}    # { pool_address: timestamp }
ALERTED_SYMBOLS_CACHE = {}     # { symbol: timestamp }
PRESALE_COOLDOWN_SECONDS = 24 * 3600  # 24-hour cooldown per presale pool

BLOCKED_DOMAINS = ["bit.ly", "tinyurl.com", "t.co", "is.gd", "t.me", "telegram.org", "discord.gg"]


# ---------------------------------------------------------------------------
# Cache Cleanup & Safety Utilities
# ---------------------------------------------------------------------------
def clean_caches():
    """Purges expired items from memory."""
    now = time.time()
    for pool in list(ALERTED_PRESALES_CACHE.keys()):
        if now - ALERTED_PRESALES_CACHE[pool] > PRESALE_COOLDOWN_SECONDS:
            del ALERTED_PRESALES_CACHE[pool]

    for sym in list(ALERTED_SYMBOLS_CACHE.keys()):
        if now - ALERTED_SYMBOLS_CACHE[sym] > (12 * 3600):
            del ALERTED_SYMBOLS_CACHE[sym]


def is_already_alerted(pool_address, symbol=""):
    clean_caches()
    contract_match = pool_address.lower() in ALERTED_PRESALES_CACHE
    symbol_match = symbol.upper() in ALERTED_SYMBOLS_CACHE if symbol else False
    return contract_match or symbol_match


def record_alerted_presale(pool_address, symbol=""):
    ALERTED_PRESALES_CACHE[pool_address.lower()] = time.time()
    if symbol:
        ALERTED_SYMBOLS_CACHE[symbol.upper()] = time.time()


def is_safe_website(url):
    """Validates URL syntax and ensures it's a valid external URL."""
    if not url or url in ["#", ""] or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ["http", "https"]:
            return False
        netloc = parsed.netloc.lower()
        if not netloc or any(blocked in netloc for blocked in BLOCKED_DOMAINS):
            return False
        return True
    except Exception:
        return False


def build_pinksale_url(pool_id, chain):
    """Constructs direct PinkSale launchpad asset page URL."""
    chain_param = chain.upper()
    if chain_param in ["ETHEREUM", "ROBINHOOD_ETH"]:
        chain_param = "ETH"
    elif chain_param in ["SOLANA"]:
        chain_param = "SOL"

    return f"https://www.pinksale.finance/launchpad/{pool_id}?chain={chain_param}"


# ---------------------------------------------------------------------------
# Discord Alert Dispatcher
# ---------------------------------------------------------------------------
def send_pinksale_discord_alert(presale_data):
    """Dispatches callout directly linking to the token's PinkSale page."""
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_PRESALE_WEBHOOK_URL environment variable is missing!")
        return

    pinksale_url = presale_data["pinksaleUrl"]
    official_website = presale_data.get("website", pinksale_url)

    net_display = presale_data['network'].upper()
    if net_display in ["ETHEREUM", "ETH"]:
        net_display = "ROBINHOOD ETH"

    embed = {
        "title": f"🔥 [{net_display}] LIVE PINKSALE PRESALE: ${presale_data['symbol']}",
        "color": 16738816,  # Gold / Flame Orange
        "thumbnail": {"url": presale_data.get("image") or "https://www.pinksale.finance/static/media/logo.f081edeb.png"},
        "fields": [
            {"name": "Token Name", "value": presale_data["name"], "inline": True},
            {"name": "Symbol", "value": f"${presale_data['symbol']}", "inline": True},
            {"name": "Network", "value": net_display, "inline": True},
            {
                "name": "Presale Status", 
                "value": "🟢 LIVE NOW ON PINKSALE", 
                "inline": False
            },
            {
                "name": "Official Links",
                "value": (
                    f"[Official Website]({official_website if is_safe_website(official_website) else pinksale_url}) | "
                    f"[Twitter/X]({presale_data.get('twitter', '#')})"
                ),
                "inline": False
            },
            {
                "name": "Direct PinkSale Link",
                "value": f"[👉 Open Presale on PinkSale]({pinksale_url})",
                "inline": False
            },
            {
                "name": "Token / Pool Address",
                "value": f"`{presale_data['contractAddress']}`",
                "inline": False
            }
        ],
        "footer": {"text": "PinkSale Exclusive Engine • Verified Link Routing"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    try:
        res = requests.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload, timeout=5)
        if res.status_code in [200, 204]:
            print(f"✅ PINKSALE ALERT DISPATCHED: ${presale_data['symbol']} ({net_display}) -> {pinksale_url}")
            record_alerted_presale(presale_data["poolAddress"], presale_data["symbol"])
        else:
            print(f"❌ Discord Post Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"❌ Discord Post Exception: {e}")


# ---------------------------------------------------------------------------
# PinkSale Scanner Engine
# ---------------------------------------------------------------------------
def fetch_live_pinksale_presales():
    """Queries PinkSale API for active live presales across SOL, ETH, and BSC."""
    print("🔍 Querying PinkSale Directory for Live Presales...")
    
    target_chains = ["bsc", "ethereum", "solana", "sol", "eth"]

    # Endpoint querying live active pools on PinkSale
    url = "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=30&status=active"

    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            print(f"⚠️ PinkSale API returned status code {res.status_code}")
            return

        data = res.json()
        pools = data.get("docs", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

        for pool in pools:
            pool_id = pool.get("id") or pool.get("poolAddress")
            token_obj = pool.get("token", {}) or {}
            contract_address = token_obj.get("address") or pool_id
            chain = pool.get("chain", "").lower()

            if not pool_id or chain not in target_chains:
                continue

            token_symbol = token_obj.get("symbol") or pool.get("symbol") or "PRESALE"
            token_name = token_obj.get("name") or pool.get("name") or "PinkSale Presale"

            # Skip if already alerted
            if is_already_alerted(pool_id, token_symbol):
                continue

            # Build direct PinkSale launchpad URL
            pinksale_url = build_pinksale_url(pool_id, chain)

            presale_payload = {
                "name": token_name[:28],
                "symbol": token_symbol[:10],
                "network": chain,
                "poolAddress": pool_id,
                "contractAddress": contract_address,
                "pinksaleUrl": pinksale_url,
                "website": pool.get("website", "#"),
                "twitter": pool.get("twitter", "#"),
                "image": pool.get("logo", "")
            }

            send_pinksale_discord_alert(presale_payload)

    except Exception as e:
        print(f"❌ PinkSale Fetch Exception: {e}")


def run_screener():
    print("\n--- Scanning Live PinkSale Presales (SOL, Robinhood ETH, BSC) ---")
    fetch_live_pinksale_presales()


if __name__ == "__main__":
    print("PinkSale Exclusive Presale Screener Active...")
    while True:
        try:
            run_screener()
        except Exception as e:
            print(f"Loop Exception: {e}")
        time.sleep(120)  # Scan PinkSale every 2 minutes
