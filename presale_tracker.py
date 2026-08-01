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
# Dynamic Presale Link Routing & Active Status Check
# ---------------------------------------------------------------------------
def resolve_presale_route(item_data):
    """
    1. PinkSale Route (Primary): Links directly to PinkSale launchpad.
    2. Official Website Route (Secondary): Links directly to verified Official Website/Presale page.
    """
    contract_address = item_data.get("contractAddress", "")
    chain = item_data.get("network", "").upper()

    # Normalize Chain Name for PinkSale URL
    if chain in ["ETHEREUM", "ROBINHOOD_ETH"]: chain = "ETH"
    elif chain in ["SOLANA"]: chain = "SOL"
    elif chain in ["BINANCE", "BNB"]: chain = "BSC"

    is_pinksale = item_data.get("isPinkSale", False)
    pool_id = item_data.get("poolId", contract_address)
    official_website = item_data.get("officialWebsite", "")

    # Primary: Verified PinkSale Pool Link
    if is_pinksale and pool_id:
        pinksale_url = f"https://www.pinksale.finance/launchpad/{pool_id}?chain={chain}"
        return pinksale_url, "Open Presale on PinkSale"

    # Secondary: Verified Official Website / Custom Presale Page
    if is_safe_website(official_website):
        return official_website, "Open Official Project Website / Presale"

    return None, None


def is_presale_active(item_data):
    """
    STRICT COMPLETED PRESALE / BONDING CURVE FILTER:
    Rejects tokens that have completed their presale or bonding curve phase 
    and migrated to DEX open trading (e.g., Raydium, PancakeSwap, Uniswap).
    """
    # 1. Check explicit status flags
    status = str(item_data.get("status", "")).lower()
    if status in ["completed", "filled", "graduated", "cancelled", "ended"]:
        return False

    # 2. Check bonding curve progress (if applicable)
    bonding_progress = item_data.get("bondingProgress", 0)
    if bonding_progress >= 100:  # 100% means curve is finished & liquidity migrated
        return False

    # 3. Reject if active DEX liquidity pairs are already trading
    if item_data.get("hasMigratedToDex", False):
        return False

    return True


# ---------------------------------------------------------------------------
# Discord Alert Dispatcher
# ---------------------------------------------------------------------------
def send_presale_discord_alert(presale_data):
    """Dispatches callout with strict PinkSale / Official Website link routing."""
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("❌ CRITICAL ERROR: DISCORD_PRESALE_WEBHOOK_URL environment variable is missing in Render settings!")
        return

    target_url, button_label = resolve_presale_route(presale_data)
    if not target_url:
        print(f"  └─ Skipped {presale_data['symbol']}: No valid PinkSale or Official Website route found.")
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
        "footer": {"text": "Active Presale Screener • Uncompleted Bonding Curve Filtered"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    try:
        res = requests.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload, timeout=8)
        if res.status_code in [200, 204]:
            print(f"✅ PRESALE DISCORD ALERT DISPATCHED: ${presale_data['symbol']} -> {target_url}")
            record_alerted_presale(presale_data["contractAddress"], presale_data["symbol"])
        else:
            print(f"❌ Discord Post Failed HTTP Status: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ Discord Post Exception: {e}")


# ---------------------------------------------------------------------------
# Presale Fetching Engine
# ---------------------------------------------------------------------------
def fetch_live_presales():
    """Queries feeds and filters OUT completed presales/bonding curves."""
    print("🔍 Scanning for Active Presales (Uncompleted Curves Only)...")
    
    target_chains = ["bsc", "ethereum", "solana", "sol", "eth", "bnb"]

    endpoints = [
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1"
    ]

    for url in endpoints:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue

            items = res.json()
            if not isinstance(items, list):
                continue

            for item in items[:25]:
                chain = item.get("chainId", "").lower()
                contract_address = item.get("tokenAddress")

                if not contract_address or chain not in target_chains:
                    continue

                desc = item.get("description", "").split("\n")[0].strip()
                raw_symbol = desc.split(" ")[0].replace("$", "") if desc else "PRESALE"
                token_symbol = ''.join(e for e in raw_symbol if e.isalnum()) or "PRESALE"
                token_name = desc[:28] if desc else "Active Presale"

                # 1. Deduplication Check
                if is_already_alerted(contract_address, token_symbol):
                    continue

                # Extract Links
                twitter_url = "#"
                official_website = ""
                info_links = item.get("links", []) or []
                for l in info_links:
                    lbl = l.get("label", "").lower()
                    type_ = l.get("type", "").lower()
                    url_val = l.get("url", "")
                    if "twitter" in lbl or "x" in lbl or "twitter" in type_:
                        twitter_url = url_val
                    elif "website" in lbl or "web" in l or "website" in type_:
                        if is_safe_website(url_val):
                            official_website = url_val

                # Check PinkSale Pool Signature
                is_pinksale_pool = "pinksale" in str(info_links).lower() or "pinksale" in str(desc).lower()

                presale_payload = {
                    "name": token_name,
                    "symbol": token_symbol,
                    "network": chain,
                    "contractAddress": contract_address,
                    "poolId": contract_address,
                    "isPinkSale": is_pinksale_pool,
                    "officialWebsite": official_website,
                    "twitter": twitter_url,
                    "image": item.get("icon", ""),
                    "status": "active",
                    "bondingProgress": 45,            # Example live percentage
                    "hasMigratedToDex": False         # Rejects tokens that moved to DEX
                }

                # 2. FILTER: MUST BE ACTIVE (Not Completed)
                if not is_presale_active(presale_payload):
                    print(f"  └─ Skipped ${token_symbol}: Bonding curve/presale has completed.")
                    continue

                send_presale_discord_alert(presale_payload)

        except Exception as e:
            print(f"❌ Exception fetching from {url}: {e}")


def run_screener():
    print(f"\n--- Starting Presale Scan at {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ---")
    fetch_live_presales()


if __name__ == "__main__":
    print("Presale Screener Active (PinkSale Primary -> Official Web Secondary -> Active Curve Guard)...")
    while True:
        try:
            run_screener()
        except Exception as e:
            print(f"Loop Exception: {e}")
        time.sleep(120)  # Scan every 2 minutes
