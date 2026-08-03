import os
import sys
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

# Cooldowns and Identity Locking
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
        async with session.get(url, timeout=12) as resp:
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
        return {"safe": True, "top10_percent": 0.0}

    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={contract_address}"
    
    try:
        data = await fetch_json(session, url)
        if not data or not isinstance(data, dict):
            return {"safe": True, "top10_percent": 0.0}
            
        result_map = data.get("result")
        if not result_map or not isinstance(result_map, dict):
            return {"safe": True, "top10_percent": 0.0}

        res = result_map.get(contract_address.lower())
        if not res or not isinstance(res, dict):
            return {"safe": True, "top10_percent": 0.0}
        
        is_honeypot = str(res.get("is_honeypot", "0")) == "1"
        cannot_sell = str(res.get("cannot_sell_all", "0")) == "1"
        if is_honeypot or cannot_sell:
            return {"safe": False, "top10_percent": 100.0}

        holders = res.get("holders", [])
        if not isinstance(holders, list) or len(holders) == 0:
            return {"safe": True, "top10_percent": 0.0}

        top10_percent = 0.0
        for holder in holders[:10]:
            if isinstance(holder, dict):
                try:
                    top10_percent += float(holder.get("percent", 0)) * 100
                except (ValueError, TypeError):
                    pass

        return {"safe": top10_percent < 80.0, "top10_percent": top10_percent}
        
    except Exception as e:
        print(f"[GoPlus Exception] {e}", flush=True)
        return {"safe": True, "top10_percent": 0.0}


# --- PRESALE LINK EXTRACTION & PRIORITIZATION ---

def parse_presale_links(websites, socials, description=""):
    """
    Identifies and prioritizes Presale URLs:
    1. Primary: Official Presale / Custom Launchpad URL. If missing, PinkSale is Primary.
    2. Secondary: PinkSale URL (if an official presale URL took the primary slot).
    """
    official_website = None
    custom_presale_link = None
    pinksale_link = None
    twitter_link = None

    if isinstance(websites, list):
        for site in websites:
            if not isinstance(site, dict): continue
            url = site.get("url", "").strip()
            if not url: continue

            url_lower = url.lower()
            if "pinksale.finance" in url_lower or "pinksale.com" in url_lower:
                pinksale_link = url
            elif any(kw in url_lower for kw in ["/presale", "presale.", "/ico", "/launchpad", "/seed", "fairlaunch"]):
                custom_presale_link = url
            elif not official_website:
                official_website = url

    if isinstance(socials, list):
        for s in socials:
            if not isinstance(s, dict): continue
            url = s.get("url", "").strip()
            if ("twitter" in s.get("type", "").lower() or "x.com" in url.lower()) and not twitter_link:
                twitter_link = url
            elif ("pinksale" in url.lower()) and not pinksale_link:
                pinksale_link = url

    # Parse description text for PinkSale or Presale links if not found in metadata lists
    if not pinksale_link and ("pinksale.finance" in description.lower() or "pinksale.com" in description.lower()):
        match = re.search(r'https?://[^\s]*pinksale\.[^\s]+', description, re.IGNORECASE)
        if match:
            pinksale_link = match.group(0)

    primary_presale = None
    secondary_presale = None

    if custom_presale_link:
        primary_presale = ("Primary Presale", custom_presale_link)
        if pinksale_link:
            secondary_presale = ("Secondary Presale (PinkSale)", pinksale_link)
    elif pinksale_link:
        primary_presale = ("Primary Presale (PinkSale)", pinksale_link)

    return {
        "website": official_website,
        "twitter": twitter_link,
        "primary_presale": primary_presale,      # Tuple: (Label, URL) or None
        "secondary_presale": secondary_presale   # Tuple: (Label, URL) or None
    }


# --- PRESALE-ONLY SCREENER ---

async def screen_presales():
    """Screens new profiles and launchpad items EXCLUSIVELY for active/upcoming Presales."""
    alerts = []
    
    async with aiohttp.ClientSession() as session:
        current_time = datetime.now(timezone.utc)

        # Fetch live token profiles and boosted listings (primary sources for presales)
        profiles = await fetch_json(session, "https://api.dexscreener.com/token-profiles/latest/v1")
        latest_boosts = await fetch_json(session, "https://api.dexscreener.com/token-boosts/latest/v1")
        top_boosts = await fetch_json(session, "https://api.dexscreener.com/token-boosts/top/v1")
        
        candidate_items = []
        if isinstance(profiles, list): candidate_items.extend(profiles)
        if isinstance(latest_boosts, list): candidate_items.extend(latest_boosts)
        if isinstance(top_boosts, list): candidate_items.extend(top_boosts)

        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Discovered {len(candidate_items)} potential presale profiles. Evaluating...", flush=True)

        evaluated_contracts = set()

        for item in candidate_items:
            if not isinstance(item, dict): continue

            chain = str(item.get("chainId", "")).lower()
            contract = item.get("tokenAddress")
            
            if chain not in CHAIN_MAP or not contract or contract in evaluated_contracts:
                continue

            # Extract basic info from profile item
            description = item.get("description", "")
            links = item.get("links", [])
            icon_url = item.get("icon")

            # Parse presale links
            link_data = parse_presale_links(links, links, description)

            # --- STRICT PRESALE FILTER ---
            # If NO presale link (PinkSale or Custom Presale) is found, SKIP IT!
            if not link_data["primary_presale"]:
                continue

            evaluated_contracts.add(contract)

            # Hydrate full token pair data if available
            p_data = await fetch_json(session, f"https://api.dexscreener.com/tokens/v1/{chain}/{contract}")
            pair_info = p_data[0] if isinstance(p_data, list) and len(p_data) > 0 else {}

            base_token = pair_info.get("baseToken", {}) if isinstance(pair_info, dict) else {}
            raw_name = base_token.get("name") or item.get("header") or "Unknown Presale Token"
            raw_symbol = str(base_token.get("symbol") or "TOKEN").upper().strip()

            if raw_symbol in EXCLUDED_SYMBOLS:
                continue

            # 1. PERMANENT IDENTITY LOCK (Name + Symbol)
            identity_key = (str(raw_name).lower().strip(), raw_symbol)
            if identity_key in seen_identities:
                print(f"  [Skip] {raw_name} (${raw_symbol}): Already alerted previously.", flush=True)
                continue

            # 2. 6-HOUR CONTRACT COOLDOWN
            last_call = callout_cooldowns.get(contract)
            if last_call and (current_time - last_call) < timedelta(hours=COOLDOWN_HOURS):
                print(f"  [Skip] {raw_symbol} ({contract[:6]}...): Under 6-hour cooldown.", flush=True)
                continue

            # 3. SECURITY CHECK
            goplus_chain = "1" if chain == "ethereum" else ("56" if chain == "bsc" else chain)
            security = await check_goplus_security(session, goplus_chain, contract)
            if not security["safe"]:
                print(f"  [Skip] {raw_name} ({contract[:6]}...): Failed security check.", flush=True)
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
                "description": description[:250] if description else "No description provided."
            }
            
            alerts.append(alert_data)

    return alerts


# --- DISCORD BROADCASTING ENGINE ---

@tasks.loop(minutes=5)
async def tracker_loop():
    print(f"\n=========================================", flush=True)
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Starting Presale-Only Scan...", flush=True)
    print(f"=========================================", flush=True)
    
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"[Error] Discord Channel ID {CHANNEL_ID} inaccessible.", flush=True)
        return

    alerts = await screen_presales()
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Scan complete. Found {len(alerts)} verified presale tokens.", flush=True)
    
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

        # Row 2: Presale Links (Prioritized)
        presale_links_formatted = []
        if ld["primary_presale"]:
            presale_links_formatted.append(f"📌 [{ld['primary_presale'][0]}]({ld['primary_presale'][1]})")
        if ld["secondary_presale"]:
            presale_links_formatted.append(f"🔗 [{ld['secondary_presale'][0]}]({ld['secondary_presale'][1]})")

        embed.add_field(name="Presale Access", value="\n".join(presale_links_formatted), inline=False)

        # Row 3: Official Socials
        social_links = []
        if ld["website"]:
            social_links.append(f"[Website]({ld['website']})")
        if ld["twitter"]:
            social_links.append(f"[Twitter/X]({ld['twitter']})")

        embed.add_field(name="Official Socials", value=" | ".join(social_links) if social_links else "None", inline=False)

        # Row 4: Description
        embed.add_field(name="Project Summary", value=f"```{alert['description']}```", inline=False)

        # Row 5 & 6: GMGN Link & Contract Address
        embed.add_field(name="Trade / Pre-Chart", value=f"👈 [Open GMGN Page]({alert['gmgn_link']})", inline=False)
        embed.add_field(name="Contract Address", value=f"`{alert['contract']}`", inline=False)

        embed.set_footer(text="Verified Presale Tracker • 6h Cooldown & Identity Lock Active")

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
