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
DEV_LAUNCH_CACHE = {}          # { dev_address: [ { name, symbol, time } ] }
PRESALE_COOLDOWN_SECONDS = 24 * 3600  # 24 hours

BLOCKED_DOMAINS = ["bit.ly", "tinyurl.com", "t.co", "is.gd", "t.me", "telegram.org", "discord.gg"]


# ---------------------------------------------------------------------------
# Cache Cleanup & Safety Utilities
# ---------------------------------------------------------------------------
def clean_caches():
    now = time.time()
    dev_cutoff = now - (24 * 3600)
    for dev in list(DEV_LAUNCH_CACHE.keys()):
        DEV_LAUNCH_CACHE[dev] = [t for t in DEV_LAUNCH_CACHE[dev] if t["time"] > dev_cutoff]
        if not DEV_LAUNCH_CACHE[dev]:
            del DEV_LAUNCH_CACHE[dev]

    for contract in list(ALERTED_PRESALES_CACHE.keys()):
        if now - ALERTED_PRESALES_CACHE[contract] > PRESALE_COOLDOWN_SECONDS:
            del ALERTED_PRESALES_CACHE[contract]


def is_already_alerted(contract_address):
    clean_caches()
    return contract_address.lower() in ALERTED_PRESALES_CACHE


def record_alerted_presale(contract_address):
    ALERTED_PRESALES_CACHE[contract_address.lower()] = time.time()


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
        "title": f"🔥 [{net_display}] NEW PRESALE DETECTED: ${presale_data['symbol']}",
        "color": 16738816,  # Gold / Flame Orange
        "thumbnail": {"url": presale_data.get("image") or "https://pump.fun/logo.png"},
        "fields": [
            {"name": "Token Name", "value": presale_data["name"], "inline": True},
            {"name": "Symbol", "value": f"${presale_data['symbol']}", "inline": True},
            {"name": "Network", "value": net_display, "inline": True},
            {
                "name": "Launchpad / Provider", 
                "value": f"🚀 {presale_data.get('provider', 'PumpPortal / PinkSale')}", 
                "inline": False
            },
            {
                "name": "Official Links",
                "value": (
                    f"[Official Website]({website if is_safe_website(website) else '#'}) | "
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
        "footer": {"text": "PumpPortal & PinkSale Live Engine • Deduplication Active"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    try:
        res = requests.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload, timeout=5)
        if res.status_code in [200, 204]:
            print(f"✅ DISCORD ALERT DISPATCHED: ${presale_data['symbol']} ({net_display})")
            record_alerted_presale(presale_data["contractAddress"])
        else:
            print(f"❌ Discord Post Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"❌ Discord Post Exception: {e}")


# ---------------------------------------------------------------------------
# Source 1: PumpPortal Real-Time Websocket API (SOLANA)
# ---------------------------------------------------------------------------
async def listen_pumpportal_presales():
    """Streams newly created tokens on Solana bonding curves via PumpPortal."""
    uri = "wss://pumpportal.fun/api/data"
    print("🔌 Connecting to PumpPortal Real-Time WebSocket for SOL Presales...")

    while True:
        try:
            async with websockets.connect(uri) as ws:
                # Subscribe to token creation events
                subscribe_message = json.dumps({"method": "subscribeNewToken"})
                await ws.send(subscribe_message)
                print("⚡ Subscribed to Solana PumpPortal Presale Feed!")

                while True:
                    response = await ws.recv()
                    data = json.loads(response)

                    # Extract metadata from token creation event
                    mint = data.get("mint")
                    name = data.get("name", "Unknown Presale")
                    symbol = data.get("symbol", "SOL")
                    uri_metadata = data.get("uri", "")

                    if not mint or is_already_alerted(mint):
                        continue

                    # Fetch IPFS/Arweave JSON metadata for external official website & twitter
                    website = f"https://pump.fun/coin/{mint}"
                    twitter = "#"
                    image = ""

                    if uri_metadata and uri_metadata.startswith("http"):
                        try:
                            meta_res = requests.get(uri_metadata, timeout=3)
                            if meta_res.status_code == 200:
                                meta_json = meta_res.json()
                                website = meta_json.get("website") or meta_json.get("external_url") or website
                                twitter = meta_json.get("twitter") or meta_json.get("telegram") or "#"
                                image = meta_json.get("image", "")
                        except Exception:
                            pass

                    presale_payload = {
                        "name": name[:28],
                        "symbol": symbol[:10],
                        "network": "solana",
                        "contractAddress": mint,
                        "provider": "Pump.fun Bonding Curve",
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
# Source 2: PinkSale API & Launchpad Directory (BSC & ETH)
# ---------------------------------------------------------------------------
def fetch_pinksale_presales():
    """Queries PinkSale API endpoints for upcoming/active BSC and ETH presales."""
    print("🔍 Fetching PinkSale & EVM Presale Listings...")
    target_chains = ["bsc", "ethereum"]

    endpoints = [
        "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=20&status=active",
        "https://api.pinksale.finance/api/v1/pool/list?page=1&limit=20&status=upcoming"
    ]

    for url in endpoints:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                pools = data.get("docs", []) if isinstance(data, dict) else []

                for pool in pools:
                    contract = pool.get("token", {}).get("address") or pool.get("poolAddress")
                    chain = pool.get("chain", "").lower()

                    if not contract or chain not in target_chains or is_already_alerted(contract):
                        continue

                    token_name = pool.get("token", {}).get("name", "PinkSale Presale")
                    token_symbol = pool.get("token", {}).get("symbol", "PRESALE")
                    pool_id = pool.get("id", contract)
                    
                    official_site = pool.get("website", "")
                    presale_url = f"https://www.pinksale.finance/launchpad/{pool_id}?chain={chain.upper()}"

                    presale_payload = {
                        "name": token_name[:28],
                        "symbol": token_symbol[:10],
                        "network": chain,
                        "contractAddress": contract,
                        "provider": f"PinkSale Launchpad ({chain.upper()})",
                        "website": official_site if is_safe_website(official_site) else presale_url,
                        "presaleUrl": official_site if is_safe_website(official_site) else presale_url,
                        "twitter": pool.get("twitter", "#"),
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
    print("🚀 Presale Screener Service Running (Solana via PumpPortal + EVM via PinkSale)...")
    
    # Run PumpPortal WebSocket stream and PinkSale polling loop concurrently
    await asyncio.gather(
        listen_pumpportal_presales(),
        poll_pinksale_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
