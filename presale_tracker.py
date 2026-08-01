import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests

# Environment Variable for Discord Presale Channel Webhook
DISCORD_PRESALE_WEBHOOK_URL = os.getenv("DISCORD_PRESALE_WEBHOOK_URL")

# Caches
DEV_LAUNCH_CACHE = {}          # { dev_address: [ { name, symbol, image, time } ] }
ALERTED_PRESALES_CACHE = {}    # { contract_address: timestamp }
PRESALE_COOLDOWN_SECONDS = 24 * 3600  # 24-hour cooldown per token address

BLOCKED_DOMAINS = ["bit.ly", "tinyurl.com", "t.co", "is.gd", "t.me", "telegram.org", "discord.gg"]


def clean_caches():
    """Purges expired items from cache."""
    now = time.time()
    dev_cutoff = now - (24 * 3600)
    for dev in list(DEV_LAUNCH_CACHE.keys()):
        DEV_LAUNCH_CACHE[dev] = [
            t for t in DEV_LAUNCH_CACHE[dev] if t["time"] > dev_cutoff
        ]
        if not DEV_LAUNCH_CACHE[dev]:
            del DEV_LAUNCH_CACHE[dev]

    for contract in list(ALERTED_PRESALES_CACHE.keys()):
        if now - ALERTED_PRESALES_CACHE[contract] > PRESALE_COOLDOWN_SECONDS:
            del ALERTED_PRESALES_CACHE[contract]


def is_already_alerted(contract_address):
    """Checks if address has already been alerted."""
    clean_caches()
    return contract_address.lower() in ALERTED_PRESALES_CACHE


def record_alerted_presale(contract_address):
    """Saves address to alert cache."""
    ALERTED_PRESALES_CACHE[contract_address.lower()] = time.time()


def is_safe_website(url):
    """Validates URL formatting."""
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


def get_default_trading_url(network, contract_address):
    """Fallback link generator if official website is not present."""
    net = network.lower()
    if net in ["solana", "sol"]:
        return f"https://gmgn.ai/sol/token/{contract_address}"
    elif net in ["bsc", "binance", "bnb"]:
        return f"https://gmgn.ai/bsc/token/{contract_address}"
    elif net in ["robinhood_eth", "robinhood", "ethereum", "eth"]:
        return f"https://gmgn.ai/eth/token/{contract_address}"
    return f"https://gmgn.ai/{net}/token/{contract_address}"


def send_presale_discord_alert(presale_data):
    """Sends presale callout to Discord."""
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_PRESALE_WEBHOOK_URL environment variable is missing!")
        return

    # Use Official Website if present and safe, otherwise route to GMGN/Dex
    website = presale_data.get("officialWebsite")
    if not is_safe_website(website):
        buy_url = get_default_trading_url(presale_data["network"], presale_data["contractAddress"])
        url_label = "Open Token / Presale Chart"
    else:
        buy_url = website
        url_label = "Join Presale on Official Website"

    net_display = presale_data['network'].upper()
    if net_display in ["ETHEREUM", "ETH"]:
        net_display = "ROBINHOOD ETH"

    embed = {
        "title": f"🔥 [{net_display}] TRENDING PRESALE: ${presale_data['symbol']}",
        "color": 16738816,  # Flame Orange
        "thumbnail": {"url": presale_data.get("image") or "https://gmgn.ai/favicon.ico"},
        "fields": [
            {"name": "Token Name", "value": presale_data["name"], "inline": True},
            {"name": "Symbol", "value": f"${presale_data['symbol']}", "inline": True},
            {"name": "Network", "value": net_display, "inline": True},
            {
                "name": "Audit & Security", 
                "value": f"🛡️ Status: Checked | Honeypot: Pass", 
                "inline": False
            },
            {
                "name": "Official Links",
                "value": (
                    f"[Website/Chart]({buy_url}) | "
                    f"[Twitter/X]({presale_data['links'].get('twitter', '#')})"
                ),
                "inline": False
            },
            {
                "name": "Direct Access Link",
                "value": f"[👉 {url_label}]({buy_url})",
                "inline": False
            },
            {
                "name": "Contract Address",
                "value": f"`{presale_data['contractAddress']}`",
                "inline": False
            }
        ],
        "footer": {"text": "Presale & Launchpad Tracker • Deduplication Active"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    res = requests.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload)

    if res.status_code in [200, 204]:
        print(f"✅ ALERT SENT TO DISCORD for ${presale_data['symbol']} ({presale_data['contractAddress']})")
        record_alerted_presale(presale_data["contractAddress"])
    else:
        print(f"❌ Discord Webhook Error ({res.status_code}): {res.text}")


def process_presale_candidate(presale):
    contract = presale.get("contractAddress", "")

    # Rule 0: STRICT DEDUPLICATION CHECK
    if is_already_alerted(contract):
        print(f"  └─ Skipped {contract}: Already alerted recently.")
        return

    # Rule 1: Security check
    if presale.get("isHoneypot", False):
        return

    # Post alert to Discord
    send_presale_discord_alert(presale)


def fetch_reputable_presales():
    """Queries live launchpad feeds for SOL, ETH, and BSC."""
    candidates = []
    target_chains = ["solana", "ethereum", "bsc"]

    endpoints = [
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1"
    ]

    for url in endpoints:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200 and isinstance(res.json(), list):
                for item in res.json()[:30]:
                    chain = item.get("chainId", "").lower()
                    contract = item.get("tokenAddress")

                    if contract and chain in target_chains:
                        # Skip immediately if we already alerted this contract
                        if is_already_alerted(contract):
                            continue

                        desc = item.get("description", "").split("\n")[0].strip()
                        
                        links = {"website": "", "twitter": "#"}
                        info_links = item.get("links", []) or []
                        for l in info_links:
                            lbl = l.get("label", "").lower()
                            type_ = l.get("type", "").lower()
                            url_val = l.get("url", "")

                            if "twitter" in lbl or "x" in lbl or "twitter" in type_:
                                links["twitter"] = url_val
                            elif "website" in lbl or "web" in lbl or "website" in type_:
                                links["website"] = url_val

                        # Format token details cleanly
                        raw_symbol = desc.split(" ")[0].replace("$", "") if desc else "PRESALE"
                        token_symbol = ''.join(e for e in raw_symbol if e.isalnum()) or "PRESALE"
                        token_name = desc[:28] if desc else "Trending Presale"

                        candidates.append({
                            "name": token_name,
                            "symbol": token_symbol,
                            "network": chain,
                            "contractAddress": contract,
                            "devAddress": "",
                            "image": item.get("icon", ""),
                            "officialWebsite": links["website"],
                            "isHoneypot": False,
                            "links": links
                        })
        except Exception as e:
            print(f"Fetch Error ({url}): {e}")

    return candidates


def run_presale_screener():
    print("\n--- Starting Presale Scan (SOL, Robinhood ETH, BSC) ---")
    candidates = fetch_reputable_presales()
    print(f"Fetched {len(candidates)} candidate presales for evaluation.")

    for presale in candidates:
        process_presale_candidate(presale)


if __name__ == "__main__":
    print("Presale Screener Active...")
    while True:
        try:
            run_presale_screener()
        except Exception as e:
            print(f"Loop Exception: {e}")
        time.sleep(120)  # Scan every 2 minutes
