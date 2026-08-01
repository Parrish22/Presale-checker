import os
import time
from datetime import datetime, timezone
import requests

# Environment Variable for Discord Webhook
DISCORD_PRESALE_WEBHOOK_URL = os.getenv("DISCORD_PRESALE_WEBHOOK_URL")

# Tracking & Deduplication Caches
ALERTED_PRESALES_CACHE = {}    # { pool_address: timestamp }
ALERTED_SYMBOLS_CACHE = {}     # { symbol: timestamp }
PRESALE_COOLDOWN_SECONDS = 24 * 3600  # 24-hour cooldown per presale pool

# Browser User-Agent headers to bypass API scraping blocks
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.pinksale.finance/",
    "Origin": "https://www.pinksale.finance"
}


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


def build_pinksale_launchpad_url(pool_id, chain):
    """Constructs explicit PinkSale Launchpad URL for the exact token pool."""
    chain_param = chain.upper()
    if chain_param in ["ETHEREUM", "ROBINHOOD_ETH"]:
        chain_param = "ETH"
    elif chain_param in ["SOLANA"]:
        chain_param = "SOL"
    elif chain_param in ["BINANCE", "BNB"]:
        chain_param = "BSC"

    return f"https://www.pinksale.finance/launchpad/{pool_id}?chain={chain_param}"


def send_pinksale_discord_alert(presale_data):
    """Dispatches callout pointing STRICTLY to PinkSale.com."""
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("❌ CRITICAL ERROR: DISCORD_PRESALE_WEBHOOK_URL environment variable is missing in Render settings!")
        return

    pinksale_url = presale_data["pinksaleUrl"]
    
    net_display = presale_data['network'].upper()
    if net_display in ["ETHEREUM", "ETH"]:
        net_display = "ROBINHOOD ETH"

    embed = {
        "title": f"🔥 [{net_display}] LIVE PINKSALE PRESALE: ${presale_data['symbol']}",
        "url": pinksale_url,  # Makes main title click directly to PinkSale.com
        "color": 16738816,    # Flame Orange
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
                "name": "Official Presale Page",
                "value": f"👉 **[Click Here to Open Presale on PinkSale.com]({pinksale_url})**",
                "inline": False
            },
            {
                "name": "Project Social (X / Twitter)",
                "value": f"[View Project Twitter/X Profile]({presale_data.get('twitter')})" if presale_data.get('twitter') and presale_data.get('twitter') != '#' else "None Provided",
                "inline": False
            },
            {
                "name": "Token Pool Address",
                "value": f"`{presale_data['contractAddress']}`",
                "inline": False
            }
        ],
        "footer": {"text": "Verified PinkSale.com Direct Link Route"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    try:
        res = requests.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload, timeout=8)
        if res.status_code in [200, 204]:
            print(f"✅ PINKSALE DISCORD ALERT DISPATCHED: ${presale_data['symbol']} -> {pinksale_url}")
            record_alerted_presale(presale_data["poolAddress"], presale_data["symbol"])
        else:
            print(f"❌ Discord Post Failed HTTP Status: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ Discord Post Exception: {e}")


def fetch_live_pinksale_presales():
    """Queries PinkSale API feeds with browser headers."""
    print("🔍 Querying PinkSale Directory for Live Presales...")
    
    target_chains = ["bsc", "ethereum", "solana", "sol", "eth", "bnb"]

    endpoints = [
        "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=30&status=active",
        "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=30&status=trending"
    ]

    total_found = 0

    for url in endpoints:
        try:
            res = requests.get(url, headers=HTTP_HEADERS, timeout=12)
            print(f"  └─ Requesting PinkSale Endpoint ({url}) - Response Code: {res.status_code}")

            if res.status_code != 200:
                continue

            data = res.json()
            pools = data.get("docs", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            total_found += len(pools)

            for pool in pools:
                pool_id = pool.get("id") or pool.get("poolAddress")
                token_obj = pool.get("token", {}) or {}
                contract_address = token_obj.get("address") or pool_id
                chain = pool.get("chain", "").lower()

                if not pool_id or chain not in target_chains:
                    continue

                token_symbol = token_obj.get("symbol") or pool.get("symbol") or "PRESALE"
                token_name = token_obj.get("name") or pool.get("name") or "PinkSale Presale"

                # Deduplication Check
                if is_already_alerted(pool_id, token_symbol):
                    print(f"     └─ Skipped Pool {pool_id} (${token_symbol}): Already alerted recently.")
                    continue

                pinksale_url = build_pinksale_launchpad_url(pool_id, chain)

                presale_payload = {
                    "name": str(token_name)[:28],
                    "symbol": str(token_symbol)[:10],
                    "network": chain,
                    "poolAddress": str(pool_id),
                    "contractAddress": str(contract_address),
                    "pinksaleUrl": pinksale_url,
                    "twitter": pool.get("twitter", "#"),
                    "image": pool.get("logo", "")
                }

                send_pinksale_discord_alert(presale_payload)

        except Exception as e:
            print(f"❌ Exception fetching from {url}: {e}")

    print(f"--- Completed Scan. Evaluated {total_found} total pools from PinkSale. ---")


def run_screener():
    print(f"\n--- Starting PinkSale Scan at {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ---")
    fetch_live_pinksale_presales()


if __name__ == "__main__":
    print("PinkSale Screener Active (Direct Route & Anti-Block Headers Enforced)...")
    while True:
        try:
            run_screener()
        except Exception as e:
            print(f"Loop Exception: {e}")
        time.sleep(120)  # Scan every 2 minutes
