import os
import sys
import re
import asyncio
from datetime import datetime, timezone, timedelta
import aiohttp
import discord
from discord.ext import tasks, commands

# Force unbuffered output for real-time Render logging
sys.stdout.reconfigure(line_buffering=True)

# --- CONFIGURATION ---
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

CHAIN_MAP = {
    "solana": "sol",
    "bsc": "bsc",
    "ethereum": "eth",
    "base": "base"
}

EXCLUDED_SYMBOLS = {
    "SOL", "WSOL", "ETH", "WETH", "BNB", "WBNB", "USDC", "USDT", "BTC", "WBTC", "DAI"
}

callout_cooldowns = {}      # { contract_address: datetime }
COOLDOWN_HOURS = 6
seen_identities = set()     # Permanent Lock: stores (name_lower, symbol_upper)

intents = discord.Intents.default()
intents.message_content = True 
bot = commands.Bot(command_prefix="!", intents=intents)


# --- DATA HELPERS & SECURITY ---

async def fetch_json(session, url):
    """Safely fetch JSON from API endpoints."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with session.get(url, headers=headers, timeout=12) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        print(f"[HTTP Error] {url}: {e}", flush=True)
    return None


async def check_goplus_security(session, chain_id, contract_address):
    """
    Checks Honeypot status and Top 10 Holder distribution using GoPlus Security API.
    Bypasses Solana and handles unindexed contracts safely.
    """
    if chain_id == "sol" or not contract_address:
        return {"safe": True, "top10_percent": 0.0, "reason": "Solana/No CA"}

    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={contract_address}"
    
    try:
        data = await fetch_json(session, url)
        if not data or not isinstance(data, dict):
            return {"safe": True, "top10_percent": 0.0, "reason": "No GoPlus Index"}
            
        result_map = data.get("result")
        if not result_map or not isinstance(result_map, dict):
            return {"safe": True, "top10_percent": 0.0, "reason": "No GoPlus Index"}

        res = result_map.get(contract_address.lower())
        if not res or not isinstance(res, dict):
            return {"safe": True, "top10_percent": 0.0, "reason": "Unindexed"}
        
        is_honeypot = str(res.get("is_honeypot", "0")) == "1"
        cannot_sell = str(res.get("cannot_sell_all", "0")) == "1"
        if is_honeypot or cannot_sell:
            return {"safe": False, "top10_percent": 100.0, "reason": "Honeypot / Cannot Sell Flag"}

        holders = res.get("holders", [])
        if not isinstance(holders, list) or len(holders) == 0:
            return {"safe": True, "top10_percent": 0.0, "reason": "Clean"}

        top10_percent = 0.0
        for holder in holders[:10]:
            if isinstance(holder, dict):
                try:
                    top10_percent += float(holder.get("percent", 0)) * 100
                except (ValueError, TypeError):
                    pass

        safe = top10_percent < 80.0
        return {"safe": safe, "top10_percent": top10_percent, "reason": "Clean" if safe else "Top 10 Concentration > 80%"}
        
    except Exception as e:
        print(f"[GoPlus Exception] {e}", flush=True)
        return {"safe": True, "top10_percent": 0.0, "reason": "Exception Bypass"}


# --- DYNAMIC PRESALE PARSER ---

def parse_presale_links(websites, socials, description=""):
    """
    Parses metadata and descriptions for presale indicators.
    Priority:
    1. Explicit PinkSale Link
    2. Explicit Custom Presale Link
    3. Official Website (if description indicates presale / fair launch)
    """
    official_website = None
    custom_presale_link = None
    pinksale_link = None
    twitter_link = None

    all_links = []
    if isinstance(websites, list): all_links.extend(websites)
    if isinstance(socials, list): all_links.extend(socials)

    for item in all_links:
        if not isinstance(item, dict): continue
        url = str(item.get("url", "")).strip()
        if not url: continue

        url_lower = url.lower()
        if "pinksale.finance" in url_lower or "pinksale.com" in url_lower:
            pinksale_link = url
        elif any(kw in url_lower for kw in ["/presale", "presale.", "/ico", "/launchpad", "/seed", "fairlaunch"]):
            custom_presale_link = url
        elif "twitter.com" in url_lower or "x.com" in url_lower:
            twitter_link = url
        elif not official_website and "http" in url_lower:
            official_website = url

    # Regex search in description for raw URLs
    if description:
        if not pinksale_link:
            match = re.search(r'https?://[^\s]*pinksale\.[^\s]+', description, re.IGNORECASE)
            if match:
                pinksale_link = match.group(0).rstrip('.,)!')

        if not custom_presale_link:
            match_custom = re.search(r'https?://[^\s]*(?:presale|launchpad|fairlaunch)[^\s]+', description, re.IGNORECASE)
            if match_custom:
                custom_presale_link = match_custom.group(0).rstrip('.,)!')

    desc_lower = description.lower() if description else ""
    is_presale_context = any(kw in desc_lower for kw in ["presale", "pink sale", "pinksale", "fair launch", "fairlaunch", "seed round", "launchpad", "ico"])

    primary_presale = None
    secondary_presale = None

    if pinksale_link:
        primary_presale = ("Primary Presale (PinkSale)", pinksale_link)
        if custom_presale_link:
            secondary_presale = ("Secondary Presale", custom_presale_link)
        elif official_website:
            secondary_presale = ("Official Site", official_website)
    elif custom_presale_link:
        primary_presale = ("Primary Presale", custom_presale_link)
        if official_website:
            secondary_presale = ("Official Site", official_website)
    elif is_presale_context and official_website:
        primary_presale = ("Primary Presale (Official Page)", official_website)

    return {
        "website": official_website,
        "twitter": twitter_link,
        "primary_presale": primary_presale,      # Tuple: (Label, URL) or None
        "secondary_presale": secondary_presale   # Tuple: (Label, URL) or None
    }


# --- UNLAUNCHED PRESALE SCREENER ---

async def screen_presales():
    """Screens candidate token sources for UNLAUNCHED Presales."""
    alerts = []
    candidate_items = []
    
    async with aiohttp.ClientSession() as session:
        current_time = datetime.now(timezone.utc)

        # Fetch DexScreener Token Profiles & Boosted listings
        profiles = await fetch_json(session, "https://api.dexscreener.com/token-profiles/latest/v1")
        latest_boosts = await fetch_json(session, "https://api.dexscreener.com/token-boosts/latest/v1")
        top_boosts = await fetch_json(session, "https://api.dexscreener.com/token-boosts/top/v1")

        if isinstance(profiles, list): candidate_items.extend(profiles)
        if isinstance(latest_boosts, list): candidate_items.extend(latest_boosts)
        if isinstance(top_boosts, list): candidate_items.extend(top_boosts)

        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Collected {len(candidate_items)} potential presale profiles. Evaluating...", flush=True)

        evaluated_contracts = set()

        for item in candidate_items:
            if not isinstance(item, dict): continue

            chain = str(item.get("chainId", "")).lower()
            contract = item.get("tokenAddress")
            
            if chain not in CHAIN_MAP or not contract or contract in evaluated_contracts:
                continue

            description = str(item.get("description", ""))
            links = item.get("links", [])
            icon_url = item.get("icon")

            # Parse Presale links or keywords
            link_data = parse_presale_links(links, links, description)

            # RULE 1: REQUIRE PRESALE CONTEXT OR LINK
            if not link_data["primary_presale"]:
                print(f"  [Skip] ({contract[:6]}...): No presale context or URL found.", flush=True)
                continue

            # RULE 2: DEX ACTIVE LIQUIDITY CHECK (Must NOT be actively trading)
            p_data = await fetch_json(session, f"https://api.dexscreener.com/tokens/v1/{chain}/{contract}")
            
            if isinstance(p_data, list) and len(p_data) > 0:
                pair_info = p_data[0]
                if isinstance(pair_info, dict):
                    liquidity_usd = float((pair_info.get("liquidity", {}) or {}).get("usd", 0) or 0)
                    volume_24h = float((pair_info.get("volume", {}) or {}).get("h24", 0) or 0)

                    # Reject if trading is already active on DEX (> $500 USD Liquidity or Volume)
                    if liquidity_usd > 500.0 or volume_24h > 500.0:
                        print(f"  [Skip] ({contract[:6]}...): Already active on DEX (Liq: ${liquidity_usd:,.2f}, 24h Vol: ${volume_24h:,.2f}).", flush=True)
                        continue
            else:
                pair_info = {}

            evaluated_contracts.add(contract)

            base_token = pair_info.get("baseToken", {}) if isinstance(pair_info, dict) else {}
            raw_name = base_token.get("name") or item.get("header") or "Unknown Presale Token"
            raw_symbol = str(base_token.get("symbol") or item.get("symbol") or "TOKEN").upper().strip()

            if raw_symbol in EXCLUDED_SYMBOLS:
                continue

            # RULE 3: PERMANENT IDENTITY LOCK
            identity_key = (str(raw_name).lower().strip(), raw_symbol)
            if identity_key in seen_identities:
                print(f"  [Skip] {raw_name} (${raw_symbol}): Identity lock active.", flush=True)
                continue

            # RULE 4: 6-HOUR CONTRACT COOLDOWN
            last_call = callout_cooldowns.get(contract)
            if last_call and (current_time - last_call) < timedelta(hours=COOLDOWN_HOURS):
                print(f"  [Skip] {raw_symbol} ({contract[:6]}...): Under 6-hour cooldown.", flush=True)
                continue

            # RULE 5: GOPLUS SECURITY CHECK
            goplus_chain = "1" if chain == "ethereum" else ("56" if chain == "bsc" else chain)
            security = await check_goplus_security(session, goplus_chain, contract)
            if not security["safe"]:
                print(f"  [Skip] {raw_name} ({contract[:6]}...): Failed GoPlus check ({security['reason']}).", flush=True)
                continue

            display_network = "Robinhood ETH" if chain == "ethereum" else chain.upper()

            alert_data = {
                "name": raw_name,
                "symbol": f"${raw_symbol}",
                "network": display_network,
                "photo": icon_url or (pair_info.get("info", {}).get("imageUrl") if isinstance(pair_info, dict) else None),
                "contract": contract,
                "gmgn_link": f"https://gmgn.ai/{CHAIN_MAP[chain]}/token/{contract}",
                "link_data": link_data,
                "identity_key": identity_key,
                "description": description[:300] if description else "Active presale opportunity."
            }
            
            alerts.append(alert_data)

    return alerts


# --- DISCORD BROADCASTING ENGINE ---

@tasks.loop(minutes=5)
async def tracker_loop():
    print(f"\n=========================================", flush=True)
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Starting Presale Scan...", flush=True)
    print(f"=========================================", flush=True)
    
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"[Error] Discord Channel ID {CHANNEL_ID} inaccessible.", flush=True)
        return

    alerts = await screen_presales()
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Scan complete. Found {len(alerts)} verified unlaunched presales.", flush=True)
    
    current_time = datetime.now(timezone.utc)

    for alert in alerts:
        ld = alert["link_data"]
        
        embed_title = f"🚨 New Presale Alert! | [{alert['network']}] {alert['symbol']}"
        
        embed = discord.Embed(
            title=embed_title,
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        photo_url = alert.get("photo")
        if photo_url and isinstance(photo_url, str) and photo_url.startswith(("http://", "https://")):
            embed.set_thumbnail(url=photo_url)

        # Row 1: Token Details
        embed.add_field(name="Token Name", value=alert["name"], inline=True)
        embed.add_field(name="Symbol", value=alert["symbol"], inline=True)
        embed.add_field(name="Network", value=alert["network"], inline=True)

        # Row 2: Presale Links
        presale_links_formatted = []
        if ld["primary_presale"]:
            presale_links_formatted.append(f"📌 [{ld['primary_presale'][0]}]({ld['primary_presale'][1]})")
        if ld["secondary_presale"]:
            presale_links_formatted.append(f"🔗 [{ld['secondary_presale'][0]}]({ld['secondary_presale'][1]})")

        embed.add_field(name="Presale Access", value="\n".join(presale_links_formatted) if presale_links_formatted else "See Official Socials", inline=False)

        # Row 3: Official Socials
        social_links = []
        if ld["website"]:
            social_links.append(f"[Website]({ld['website']})")
        if ld["twitter"]:
            social_links.append(f"[Twitter/X]({ld['twitter']})")

        embed.add_field(name="Official Socials", value=" | ".join(social_links) if social_links else "None", inline=False)

        # Row 4: Summary
        embed.add_field(name="Project Summary", value=f"```{alert['description']}```", inline=False)

        # Row 5 & 6: Pre-Launch GMGN Page & Contract Address
        embed.add_field(name="Pre-Chart / GMGN Page", value=f"👈 [Open GMGN Page]({alert['gmgn_link']})", inline=False)
        embed.add_field(name="Contract Address", value=f"`{alert['contract']}`", inline=False)

        embed.set_footer(text="Verified Presale Tracker • Unlaunched Only • 6h Cooldown Active")

        try:
            await channel.send(embed=embed)
            callout_cooldowns[alert["contract"]] = current_time
            seen_identities.add(alert["identity_key"])
            print(f"[Discord] Sent PRESALE alert for {alert['symbol']} ({alert['name']})", flush=True)
        except Exception as e:
            print(f"[Discord Send Error] {e}", flush=True)


@bot.event
async def on_ready():
    print(f"Logged in successfully as {bot.user.name}", flush=True)
    if not tracker_loop.is_running():
        tracker_loop.start()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
