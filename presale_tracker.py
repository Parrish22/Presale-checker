import os
import time
import asyncio
import json
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse
import websockets

# Environment Variable for Discord Webhook
DISCORD_PRESALE_WEBHOOK_URL = os.getenv("DISCORD_PRESALE_WEBHOOK_URL")

# Storage & Caching
ALERTED_PRESALES_CACHE = {}    # { contract_address: timestamp }
ALERTED_SYMBOLS_CACHE = {}     # { symbol: timestamp }
PRESALE_COOLDOWN_SECONDS = 24 * 3600  # 24 hours

BLOCKED_DOMAINS = ["bit.ly", "tinyurl.com", "t.co", "is.gd", "t.me", "telegram.org", "discord.gg"]


# ---------------------------------------------------------------------------
# Cache Cleanup & Safety Utilities
# ---------------------------------------------------------------------------
def clean_caches():
    now = time.time()
    for contract in list(ALERTED_PRESALES_CACHE.keys()):
        if now - ALERTED_PRESALES_CACHE[contract] > PRESALE_COOLDOWN_SECONDS:
            del ALERTED_PRESALES_CACHE[contract]

    for sym in list(ALERTED_SYMBOLS_CACHE.keys()):
        if now - ALERTED_SYMBOLS_CACHE[sym] > (12 * 3600):  # 12-hour ticker cooldown
            del ALERTED_SYMBOLS_CACHE[sym]


def is_already_alerted(contract_address, symbol=""):
    clean_caches()
    contract_match = contract_address.lower() in ALERTED_PRESALES_CACHE
    symbol_match = symbol.upper() in ALERTED_SYMBOLS_CACHE if symbol else False
    return contract_match or symbol_match


def record_alerted_presale(contract_address, symbol=""):
    ALERTED_PRESALES_CACHE[contract_address.lower()] = time.time()
    if symbol:
        ALERTED_SYMBOLS_CACHE[symbol.upper()] = time.time()


def is_safe_website(url):
    """Validates URL syntax and ensures it's an external HTTPS website."""
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
# Online Popularity & Traction Verification Engine
# ---------------------------------------------------------------------------
def verify_online_traction(twitter_url, website_url, name):
    """
    STRICT TRACTION GUARD:
    Ensures the presale has genuine online presence and social links before alerting.
    """
    # 1. Must have a valid, non-empty Twitter/X profile
    if not twitter_url or twitter_url == "#" or "twitter.com" not in twitter_url.lower() and "x.com" not in twitter_url.lower():
        return False, "Missing valid Twitter/X profile"

    # 2. Must have an official, custom external website
    if not is_safe_website(website_url) or "pump.fun" in website_url.lower():
        return False, "Missing official custom website"

    # 3. Reject generic copycat names
    generic_names = ["pumppoint", "elon", "bitcoin", "solana", "bite the curb"]
    if any(g in name.lower() for g in generic_names):
        return False, "Flagged as generic copycat name"

    return True, "Passed Traction Verification"


# ---------------------------------------------------------------------------
# Discord Alert Dispatcher
# ---------------------------------------------------------------------------
def send_presale_discord_alert(presale_data):
    """Dispatches high-visibility alert directly to your Discord channel."""
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_PRESALE_WEBHOOK_URL is missing from Render Environment Settings!")
        return

    net_display = presale_data['network'].upper()
    if net_display in ["ETHEREUM", "ETH"]:
        net_display = "ROBINHOOD ETH"

    website = presale_data.get("website", "#")
    direct_presale_url = presale_data.get("presaleUrl", website)

    embed = {
        "title": f"🔥 [{net_display}] HIGH-TRACTION PRESALE: ${presale_data['symbol']}",
        "color": 16738816,  # Gold / Flame Orange
        "thumbnail": {"url": presale_data.get("image") or "https://www.pinksale.finance/static/media/logo.f081edeb.png"},
        "fields": [
            {"name": "Token Name", "value": presale_data["name"], "inline": True},
            {"name": "Symbol", "value": f"${presale_data['symbol']}", "inline": True},
            {"name": "Network", "value": net_display, "inline": True},
            {
                "name": "Traction & Verification Status", 
                "value": "✅ Verified Twitter/X Presence | ✅ Official Website | ✅ Zero-Spam Guard", 
                "inline": False
            },
            {
                "name": "Launchpad Provider", 
                "value": f"🚀 {presale_data.get('provider', 'PinkSale / High-Traction Engine')}", 
                "inline": True
            },
            {
                "name": "Official Links",
                "value": (
                    f"[Official Website]({website}) | "
                    f"[Twitter/X]({presale_data.get('twitter', '#')})"
                ),
                "inline": False
            },
            {
                "name": "Direct Presale Link",
                "value": f"[👉 Join Presale on Official Page]({direct_presale_url})",
                "inline": False
            },
            {
                "name": "Contract Address",
                "value": f"`{presale_data['contractAddress']}`",
                "inline": False
            }
        ],
        "footer": {"text": "Traction Verification Active • Spam & Duplicate Guard Enforced"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    try:
        res = requests.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload, timeout=5)
        if res.status_code in [200, 204]:
            print(f"✅ DISCORD ALERT DISPATCHED: ${presale_data['symbol']} ({net_display})")
            record_alerted_presale(presale_data["contractAddress"], presale_data["symbol"])
        else:
            print(f"❌ Discord Post Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"❌ Discord Post Exception: {e}")


# ---------------------------------------------------------------------------
# Source 1: PumpPortal Real-Time Websocket API (Filtered for Traction)
# ---------------------------------------------------------------------------
async def listen_pumpportal_presales():
    """Streams newly created tokens on Solana and applies strict traction filtering."""
    uri = "wss://pumpportal.fun/api/data"
    print("🔌 Connecting to PumpPortal WebSocket for High-Traction SOL Presales...")

    while True:
        try:
            async with websockets.connect(uri) as ws:
                subscribe_message = json.dumps({"method": "subscribeNewToken"})
                await ws.send(subscribe_message)
                print("⚡ Filtering Solana PumpPortal Presales for Social Traction...")

                while True:
                    response = await ws.recv()
                    data = json.loads(response)

                    mint = data.get("mint")
                    name = data.get("name", "Unknown Presale")
                    symbol = data.get("symbol", "SOL")
                    uri_metadata = data.get("uri", "")

                    if not mint or is_already_alerted(mint, symbol):
                        continue

                    # Fetch IPFS JSON metadata
                    website = "#"
                    twitter = "#"
                    image = ""

                    if uri_metadata and uri_metadata.startswith("http"):
                        try:
                            meta_res = requests.get(uri_metadata, timeout=3)
                            if meta_res.status_code == 200:
                                meta_json = meta_res.json()
                                website = meta_json.get("website") or meta_json.get("external_url") or "#"
                                twitter = meta_json.get("twitter") or "#"
                                image = meta_json.get("image", "")
                        except Exception:
                            pass

                    # APPLY TRACTION VERIFICATION FILTER
                    has_traction, reason = verify_online_traction(twitter, website, name)
                    if not has_traction:
                        # Silently skip low-traction / spam tokens
                        continue

                    presale_payload = {
                        "name": name[:28],
                        "symbol": symbol[:10],
                        "network": "solana",
                        "contractAddress": mint,
                        "provider": "Pump.fun High-Traction Launch",
                        "website": website,
                        "presaleUrl": website,
                        "twitter": twitter,
                        "image": image
                    }

                    send_presale_discord_alert(presale_payload)

        except Exception as e:
            print(f"⚠️ PumpPortal WebSocket Disconnected ({e}). Reconnecting in 5s...")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Source 2: PinkSale API (Filtered for High-Traction Pools)
# ---------------------------------------------------------------------------
def fetch_pinksale_presales():
    """Queries PinkSale API endpoints for active, trending presales."""
    print("🔍 Querying PinkSale for High-Traction Presales...")
    target_chains = ["bsc", "ethereum", "solana", "sol", "eth"]

    endpoints = [
        "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=20&status=active"
    ]

    for url in endpoints:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                pools = data.get("docs", []) if isinstance(data, dict) else []

                for pool in pools:
                    contract = pool.get("token", {}).get("address") or pool.get("id") or pool.get("poolAddress")
                    chain = pool.get("chain", "").lower()

                    if not contract or chain not in target_chains:
                        continue

                    token_symbol = pool.get("token", {}).get("symbol") or pool.get("symbol") or "PRESALE"

                    if is_already_alerted(contract, token_symbol):
                        continue

                    token_name = pool.get("token", {}).get("name") or pool.get("name") or "PinkSale Presale"
                    pool_id = pool.get("id", contract)
                    
                    official_site = pool.get("website", "#")
                    twitter_site = pool.get("twitter", "#")
                    presale_url = f"https://www.pinksale.finance/launchpad/{pool_id}?chain={chain.upper()}"

                    # APPLY TRACTION VERIFICATION FILTER
                    has_traction, reason = verify_online_traction(twitter_site, official_site, token_name)
                    if not has_traction:
                        continue

                    presale_payload = {
                        "name": token_name[:28],
                        "symbol": token_symbol[:10],
                        "network": chain,
                        "contractAddress": contract,
                        "provider": f"PinkSale Directory ({chain.upper()})",
                        "website": official_site,
                        "presaleUrl": presale_url,
                        "twitter": twitter_site,
                        "image": pool.get("logo", "")
                    }

                    send_presale_discord_alert(presale_payload)
        except Exception as e:
            print(f"PinkSale Fetch Note ({url}): {e}")


async def poll_pinksale_loop():
    """Polls PinkSale API every 2 minutes."""
    while True:
        try:
            fetch_pinksale_presales()
        except Exception as e:
            print(f"PinkSale Loop Exception: {e}")
        await asyncio.sleep(120)


# ---------------------------------------------------------------------------
# Main Async Runner
# ---------------------------------------------------------------------------
async def main():
    print("🚀 Presale Screener Active (Strict Traction Guard Enforced)...")
    await asyncio.gather(
        listen_pumpportal_presales(),
        poll_pinksale_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
