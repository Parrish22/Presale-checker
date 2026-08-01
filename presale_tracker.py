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

# Headers to prevent Cloudflare / API blocks
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.pinksale.finance/",
    "Origin": "https://www.pinksale.finance"
}


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
    """Validates URL syntax and ensures it's an external website."""
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


# ---------------------------------------------------------------------------
# Presale Route Resolver & Active Status Guard
# ---------------------------------------------------------------------------
def resolve_presale_route(item_data):
    """
    Primary: Active PinkSale Pool Link (ONLY if generated on PinkSale).
    Secondary: Official Website Presale Page.
    Tertiary: Active Launchpad Bonding Curve (e.g. Pump.fun active curve).
    """
    source = item_data.get("source", "")
    pool_id = item_data.get("poolId", "")
    contract_address = item_data.get("contractAddress", "")
    chain = item_data.get("network", "").upper()
    official_website = item_data.get("officialWebsite", "")

    # Normalize Chain Name for PinkSale
    if chain in ["ETHEREUM", "ROBINHOOD_ETH"]: chain = "ETH"
    elif chain in ["SOLANA"]: chain = "SOL"
    elif chain in ["BINANCE", "BNB"]: chain = "BSC"

    # Route 1: Real PinkSale Launchpad Pool
    if source == "pinksale" and pool_id:
        pinksale_url = f"https://www.pinksale.finance/launchpad/{pool_id}?chain={chain}"
        return pinksale_url, "Open Presale on PinkSale.com"

    # Route 2: Verified Official Project Website
    if is_safe_website(official_website):
        return official_website, "Open Official Project Presale Website"

    # Route 3: Active Bonding Curve Page
    if contract_address.endswith("pump"):
        return f"https://pump.fun/coin/{contract_address}", "Open Active Bonding Curve on Pump.fun"

    return None, None


def is_presale_active(item_data):
    """
    REJECTS completed presales or tokens that have migrated to DEX trading.
    """
    status = str(item_data.get("status", "")).lower()
    
    # 1. Reject completed/filled/ended presales
    if status in ["completed", "filled", "graduated", "cancelled", "ended", "success"]:
        return False

    # 2. Reject 100% completed bonding curves
    if item_data.get("bondingProgress", 0) >= 100:
        return False

    # 3. Reject tokens already listed on DEXs (Raydium, PancakeSwap, Uniswap)
    if item_data.get("hasMigratedToDex", False):
        return False

    return True


# ---------------------------------------------------------------------------
# Discord Alert Dispatcher
# ---------------------------------------------------------------------------
def send_presale_discord_alert(presale_data):
    """Dispatches callout with strict link routing."""
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_PRESALE_WEBHOOK_URL environment variable is missing in Render settings!")
        return

    target_url, button_label = resolve_presale_route(presale_data)
    if not target_url:
        print(f"  └─ Skipped ${presale_data['symbol']}: No valid presale or website URL found.")
        return

    net_display = presale_data['network'].upper()
    if net_display in ["ETHEREUM", "ETH"]:
        net_display = "ROBINHOOD ETH"

    embed = {
        "title": f"🔥 [{net_display}] ACTIVE PRESALE: ${presale_data['symbol']}",
        "url": target_url,
        "color": 16738816,  # Flame Orange
        "thumbnail": {"url": presale_data.get("image") or "https://www.pinksale.finance/static/media/logo.f081edeb.png"},
        "fields": [
            {"name": "Token Name", "value": presale_data["name"], "inline": True},
            {"name": "Symbol", "value": f"${presale_data['symbol']}", "inline": True},
            {"name": "Network", "value": net_display, "inline": True},
            {
                "name": "Presale Status", 
                "value": "🟢 ACTIVE PRESALE IN PROGRESS", 
                "inline": False
            },
            {
                "name": "Direct Presale Route",
                "value": f"👉 **[{button_label}]({target_url})**",
                "inline": False
            },
            {
                "name": "Project Social (X / Twitter)",
                "value": f"[View Project Twitter/X Profile]({presale_data.get('twitter')})" if presale_data.get('twitter') and presale_data.get('twitter') != '#' else "None Provided",
                "inline": False
            },
            {
                "name": "Token / Pool Address",
                "value": f"`{presale_data['contractAddress']}`",
                "inline": False
            }
        ],
        "footer": {"text": "Active Presale Screener • Uncompleted Curve Filter Active"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    try:
        res = requests.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload, timeout=8)
        if res.status_code in [200, 204]:
            print(f"✅ DISCORD ALERT DISPATCHED: ${presale_data['symbol']} -> {target_url}")
            record_alerted_presale(presale_data["contractAddress"], presale_data["symbol"])
        else:
            print(f"❌ Discord Post Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"❌ Discord Post Exception: {e}")


# ---------------------------------------------------------------------------
# Source 1: PinkSale Live Active Presale Directory
# ---------------------------------------------------------------------------
def fetch_pinksale_active_presales():
    """Queries PinkSale directly for active live pools."""
    print("🔍 Fetching Active Pools from PinkSale...")
    target_chains = ["bsc", "ethereum", "solana", "sol", "eth", "bnb"]
    url = "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=30&status=active"

    try:
        res = requests.get(url, headers=HTTP_HEADERS, timeout=10)
        if res.status_code == 200:
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

                if is_already_alerted(pool_id, token_symbol):
                    continue

                presale_payload = {
                    "source": "pinksale",
                    "name": str(token_name)[:28],
                    "symbol": str(token_symbol)[:10],
                    "network": chain,
                    "poolId": str(pool_id),
                    "contractAddress": str(contract_address),
                    "officialWebsite": pool.get("website", ""),
                    "twitter": pool.get("twitter", "#"),
                    "image": pool.get("logo", ""),
                    "status": pool.get("status", "active"),
                    "bondingProgress": 50,
                    "hasMigratedToDex": False
                }

                if is_presale_active(presale_payload):
                    send_presale_discord_alert(presale_payload)

    except Exception as e:
        print(f"PinkSale Fetch Note: {e}")


# ---------------------------------------------------------------------------
# Source 2: Active Bonding Curve Engine
# ---------------------------------------------------------------------------
def fetch_active_bonding_curves():
    """Queries live active presales and checks for official website / active curve status."""
    print("🔍 Fetching Active Presale & Launchpad Candidates...")
    target_chains = ["bsc", "ethereum", "solana", "sol", "eth", "bnb"]
    url = "https://api.dexscreener.com/token-boosts/latest/v1"

    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json()[:30]:
                chain = item.get("chainId", "").lower()
                contract_address = item.get("tokenAddress")

                if not contract_address or chain not in target_chains:
                    continue

                desc = item.get("description", "").split("\n")[0].strip()
                raw_symbol = desc.split(" ")[0].replace("$", "") if desc else "PRESALE"
                token_symbol = ''.join(e for e in raw_symbol if e.isalnum()) or "PRESALE"
                token_name = desc[:28] if desc else "Active Presale"

                if is_already_alerted(contract_address, token_symbol):
                    continue

                # Extract Twitter & Official Website
                twitter_url = "#"
                official_website = ""
                info_links = item.get("links", []) or []
                for l in info_links:
                    lbl = l.get("label", "").lower()
                    type_ = l.get("type", "").lower()
                    url_val = l.get("url", "")
                    if "twitter" in lbl or "x" in lbl or "twitter" in type_:
                        twitter_url = url_val
                    elif "website" in lbl or "web" in lbl or "website" in type_:
                        if is_safe_website(url_val):
                            official_website = url_val

                # If contract address ends in "pump", check if it's still an uncompleted bonding curve
                is_pump = contract_address.endswith("pump")
                
                presale_payload = {
                    "source": "custom",
                    "name": token_name,
                    "symbol": token_symbol,
                    "network": chain,
                    "contractAddress": contract_address,
                    "officialWebsite": official_website,
                    "twitter": twitter_url,
                    "image": item.get("icon", ""),
                    "status": "active",
                    "bondingProgress": 30 if is_pump else 0,
                    "hasMigratedToDex": False
                }

                if is_presale_active(presale_payload):
                    send_presale_discord_alert(presale_payload)

    except Exception as e:
        print(f"Bonding Curve Fetch Note: {e}")


def run_screener():
    print(f"\n--- Starting Presale Scan at {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ---")
    fetch_pinksale_active_presales()
    fetch_active_bonding_curves()


if __name__ == "__main__":
    print("Presale Screener Service Active...")
    while True:
        try:
            run_screener()
        except Exception as e:
            print(f"Loop Exception: {e}")
        time.sleep(120)  # Scan every 2 minutes
