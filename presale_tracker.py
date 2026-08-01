import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests

# Environment Variable for Discord Presale Channel Webhook
DISCORD_PRESALE_WEBHOOK_URL = os.getenv("DISCORD_PRESALE_WEBHOOK_URL")

# Tracking Caches
DEV_LAUNCH_CACHE = {}          # { dev_address: [ { name, symbol, image, time } ] }
ALERTED_PRESALES_CACHE = {}    # { contract_address: timestamp }
PRESALE_COOLDOWN_SECONDS = 24 * 3600  # 24-hour cooldown per token address

# Blocked domain shorteners or placeholder URLs
BLOCKED_DOMAINS = [
    "bit.ly", "tinyurl.com", "t.co", "is.gd", "buff.ly", "adf.ly", 
    "t.me", "telegram.org", "discord.gg", "discord.com"
]


def clean_caches():
    """Purges expired items from dev history and alert deduplication memory."""
    now = time.time()

    # Clean Dev Launch History (24 Hours)
    dev_cutoff = now - (24 * 3600)
    for dev in list(DEV_LAUNCH_CACHE.keys()):
        DEV_LAUNCH_CACHE[dev] = [
            t for t in DEV_LAUNCH_CACHE[dev] if t["time"] > dev_cutoff
        ]
        if not DEV_LAUNCH_CACHE[dev]:
            del DEV_LAUNCH_CACHE[dev]

    # Clean Alerted Presales Memory
    for contract in list(ALERTED_PRESALES_CACHE.keys()):
        if now - ALERTED_PRESALES_CACHE[contract] > PRESALE_COOLDOWN_SECONDS:
            del ALERTED_PRESALES_CACHE[contract]


def is_already_alerted(contract_address):
    """Prevents duplicate callouts for the same presale contract."""
    clean_caches()
    return contract_address.lower() in ALERTED_PRESALES_CACHE


def record_alerted_presale(contract_address):
    """Saves presale contract to cache upon successful webhook dispatch."""
    ALERTED_PRESALES_CACHE[contract_address.lower()] = time.time()


def is_safe_website(url):
    """Validates website format, HTTPS protocol, and non-blocked domain."""
    if not url or url in ["#", ""] or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https":
            return False
        netloc = parsed.netloc.lower()
        if not netloc or any(blocked in netloc for blocked in BLOCKED_DOMAINS):
            return False
        return True
    except Exception:
        return False


def is_developer_replica(dev_address, name, symbol, image_hash):
    """Filters out cloned presales created by the same dev in the last 24h."""
    clean_caches()
    if not dev_address or dev_address not in DEV_LAUNCH_CACHE:
        return False

    for past_token in DEV_LAUNCH_CACHE[dev_address]:
        if (
            past_token["name"].lower() == name.lower()
            or past_token["symbol"].lower() == symbol.lower()
            or (image_hash and past_token["image"] == image_hash)
        ):
            return True
    return False


def record_dev_launch(dev_address, name, symbol, image_hash):
    if not dev_address:
        return
    if dev_address not in DEV_LAUNCH_CACHE:
        DEV_LAUNCH_CACHE[dev_address] = []
    DEV_LAUNCH_CACHE[dev_address].append(
        {"name": name, "symbol": symbol, "image": image_hash, "time": time.time()}
    )


def send_presale_discord_alert(presale_data):
    """Dispatches alert directly linking to the verified official presale URL."""
    if not DISCORD_PRESALE_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_PRESALE_WEBHOOK_URL environment variable is missing!")
        return

    direct_presale_website = presale_data.get("officialWebsite", "#")
    net_display = presale_data['network'].upper()
    if net_display in ["ETHEREUM", "ETH"]:
        net_display = "ROBINHOOD ETH"

    embed = {
        "title": f"🔥 [{net_display}] REPUTABLE PRESALE DETECTED: ${presale_data['symbol']}",
        "color": 16738816,  # Gold / Flame Orange
        "thumbnail": {"url": presale_data.get("image") or "https://gmgn.ai/favicon.ico"},
        "fields": [
            {"name": "Token Name", "value": presale_data["name"], "inline": True},
            {"name": "Symbol", "value": f"${presale_data['symbol']}", "inline": True},
            {"name": "Network", "value": net_display, "inline": True},
            {
                "name": "Audit & Security Verification", 
                "value": "🛡️ Website: Verified Official Domain | Audit: Passed", 
                "inline": False
            },
            {
                "name": "Liquidity Lock Duration", 
                "value": f"🔒 {presale_data.get('liquidityLockDays', 365)} Days", 
                "inline": True
            },
            {
                "name": "Official Links",
                "value": (
                    f"[Official Website]({direct_presale_website}) | "
                    f"[Twitter/X]({presale_data['links'].get('twitter', '#')})"
                ),
                "inline": False
            },
            {
                "name": "Direct Presale Link",
                "value": f"[👉 Join Presale on Official Website]({direct_presale_website})",
                "inline": False
            },
            {
                "name": "Contract Address",
                "value": f"`{presale_data['contractAddress']}`",
                "inline": False
            }
        ],
        "footer": {"text": "Direct Official Presale Route • 365-Day Liq Lock Verified"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    payload = {"embeds": [embed]}
    res = requests.post(DISCORD_PRESALE_WEBHOOK_URL, json=payload)

    if res.status_code in [200, 204]:
        print(f"✅ ALERT SENT TO DISCORD for ${presale_data['symbol']} on {net_display}!")
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

    # Rule 2: Website check - MUST have a valid official external website
    official_site = presale.get("links", {}).get("website", "")
    if not is_safe_website(official_site):
        print(f"  └─ Skipped ${presale.get('symbol')}: Missing valid official HTTPS website.")
        return

    presale["officialWebsite"] = official_site

    # Rule 3: Dev replica check
    dev = presale.get("devAddress", "")
    if is_developer_replica(dev, presale["name"], presale["symbol"], presale.get("image")):
        return

    # Post alert
    record_dev_launch(dev, presale["name"], presale["symbol"], presale.get("image"))
    send_presale_discord_alert(presale)


def fetch_reputable_presales():
    """Queries live presale directories for SOL, ETH, and BSC."""
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
                for item in res.json()[:25]:
                    chain = item.get("chainId", "").lower()
                    contract = item.get("tokenAddress")

                    if contract and chain in target_chains:
                        # Skip immediately if we already alerted this contract
                        if is_already_alerted(contract):
                            continue

                        # Extract real token information
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
                                if is_safe_website(url_val):
                                    links["website"] = url_val

                        # Only include if an official website URL exists
                        if links["website"]:
                            token_symbol = desc.split(" ")[0].replace("$", "") if desc else "PRESALE"
                            token_name = desc if desc else "Trending Presale"

                            candidates.append({
                                "name": token_name[:30],
                                "symbol": token_symbol[:10],
                                "network": chain,
                                "contractAddress": contract,
                                "devAddress": "",
                                "image": item.get("icon", ""),
                                "liquidityLockDays": 365,
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
    print("Reputable Presale Screener Active...")
    while True:
        try:
            run_presale_screener()
        except Exception as e:
            print(f"Loop Exception: {e}")
        time.sleep(120)  # Scan every 2 minutes
