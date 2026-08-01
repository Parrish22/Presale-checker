import os
import time
from datetime import datetime, timezone
import requests

# Environment Variable for Discord Webhook
DISCORD_PRESALE_WEBHOOK_URL = os.getenv("DISCORD_PRESALE_WEBHOOK_URL")

# Tracking & Deduplication Caches
ALERTED_PRESALES_CACHE = {}    # { pool_address: timestamp }
DEV_LAUNCH_CACHE = {}          # { dev_address: [ { name, symbol, time } ] }
PRESALE_COOLDOWN_SECONDS = 24 * 3600  # 24-hour cooldown per presale pool


# ---------------------------------------------------------------------------
# Cache Cleanup & Safety Utilities
# ---------------------------------------------------------------------------
def clean_caches():
    """Purges expired items from alert memory."""
    now = time.time()
    dev_cutoff = now - (24 * 3600)
    for dev in list(DEV_LAUNCH_CACHE.keys()):
        DEV_LAUNCH_CACHE[dev] = [t for t in DEV_LAUNCH_CACHE[dev] if t["time"] > dev_cutoff]
        if not DEV_LAUNCH_CACHE[dev]:
            del DEV_LAUNCH_CACHE[dev]

    for pool in list(ALERTED_PRESALES_CACHE.keys()):
        if now - ALERTED_PRESALES_CACHE[pool] > PRESALE_COOLDOWN_SECONDS:
            del ALERTED_PRESALES_CACHE[pool]


def is_already_alerted(pool_address):
    """Checks if pool address has already been alerted."""
    clean_caches()
    return pool_address.lower() in ALERTED_PRESALES_CACHE


def record_alerted_presale(pool_address):
    """Saves pool address to alert cache."""
    ALERTED_PRESALES_CACHE[pool_address.lower()] = time.time()


def build_pinksale_presale_url(pool_address, chain):
    """Formats direct link to the asset's PinkSale presale launchpad page."""
    chain_param = chain.upper()
    if chain_param in ["ETHEREUM", "ROBINHOOD_ETH"]:
        chain_param = "ETH"
    elif chain_param in ["SOLANA"]:
        chain_param = "SOL"

    return f"https://www.pinksale.finance/launchpad/{pool_address}?chain={chain_param}"


# ---------------------------------------------------------------------------
# Discord Alert Dispatcher
# ---------------------------------------------------------------------------
def send_pinksale_discord_alert(presale_data):
    """Dispatches high-visibility callout directly linking to the PinkSale presale page."""
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_PRESALE_WEBHOOK_URL environment variable is missing in Render!")
        return

    pinksale_presale_page = presale_data["pinksalePresaleUrl"]
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
                "name": "Soft / Hard Cap", 
                "value": f"{presale_data.get('softCap', 'N/A')} / {presale_data.get('hardCap', 'N/A')} {presale_data.get('currency', '')}", 
                "inline": True
            },
            {
                "name": "Liquidity Lock", 
                "value": f"🔒 {presale_data.get('liquidityLockPercent', 51)}% ({presale_data.get('lockDays', 365)} Days)", 
                "inline": True
            },
            {
                "name": "Official Links",
                "value": (
                    f"[Official Website]({presale_data['links'].get('website', pinksale_presale_page)}) | "
                    f"[Twitter/X]({presale_data['links'].get('twitter', '#')})"
                ),
                "inline": False
            },
            {
                "name": "Direct PinkSale Presale Link",
                "value": f"[👉 Join Presale on PinkSale Page]({pinksale_presale_page})",
                "inline": False
            },
            {
                "name": "Token / Pool Address",
                "value": f"`{presale_data['contractAddress']}`",
                "inline": False
            }
        ],
        "footer": {"text": "PinkSale Live Directory Engine • Direct Asset Page Route"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    try:
        res = requests.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload, timeout=5)
        if res.status_code in [200, 204]:
            print(f"✅ ALERT SENT TO DISCORD for ${presale_data['symbol']} ({net_display})")
            record_alerted_presale(presale_data["poolAddress"])
        else:
            print(f"❌ Discord Webhook Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"❌ Discord Post Exception: {e}")


# ---------------------------------------------------------------------------
# PinkSale Live Presale Scanner Engine
# ---------------------------------------------------------------------------
def fetch_live_pinksale_presales():
    """Queries PinkSale API for active live presales across SOL, ETH, and BSC."""
    print("🔍 Fetching Live Presales from PinkSale Directory...")
    
    target_chains = ["bsc", "ethereum", "solana", "sol", "eth"]

    # Endpoint targeting active, live pools on PinkSale
    url = "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=30&status=active"

    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            print(f"⚠️ PinkSale API returned status {res.status_code}")
            return

        data = res.json()
        pools = data.get("docs", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

        for pool in pools:
            pool_address = pool.get("id") or pool.get("poolAddress") or pool.get("token", {}).get("address")
            chain = pool.get("chain", "").lower()

            if not pool_address or chain not in target_chains:
                continue

            # Deduplication Check
            if is_already_alerted(pool_address):
                continue

            # Extract token details
            token_obj = pool.get("token", {}) or {}
            token_name = token_obj.get("name") or pool.get("name") or "PinkSale Presale"
            token_symbol = token_obj.get("symbol") or "PRESALE"
            contract_address = token_obj.get("address") or pool_address

            # Extract Direct PinkSale Presale URL
            pinksale_url = build_pinksale_presale_url(pool_address, chain)

            # Social links
            website = pool.get("website", "#")
            twitter = pool.get("twitter", "#")

            presale_payload = {
                "name": token_name[:28],
                "symbol": token_symbol[:10],
                "network": chain,
                "poolAddress": pool_address,
                "contractAddress": contract_address,
                "pinksalePresaleUrl": pinksale_url,
                "softCap": pool.get("softCap", "N/A"),
                "hardCap": pool.get("hardCap", "N/A"),
                "currency": pool.get("currencySymbol", "BNB"),
                "liquidityLockPercent": pool.get("liquidityPercent", 51),
                "lockDays": pool.get("lockDays", 365),
                "image": pool.get("logo", ""),
                "links": {
                    "website": website,
                    "twitter": twitter
                }
            }

            send_pinksale_discord_alert(presale_payload)

    except Exception as e:
        print(f"❌ PinkSale Fetch Exception: {e}")


def run_screener():
    print("\n--- Scanning Live PinkSale Presales (SOL, Robinhood ETH, BSC) ---")
    fetch_live_pinksale_presales()


if __name__ == "__main__":
    print("PinkSale Live Presale Screener Service Active...")
    while True:
        try:
            run_screener()
        except Exception as e:
            print(f"Loop Exception: {e}")
        time.sleep(120)  # Scan PinkSale every 2 minutes
