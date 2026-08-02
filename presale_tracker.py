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

# Trackers
callout_cooldowns = {}      # { contract_address: datetime }
COOLDOWN_HOURS = 6

seen_identities = set()     # Permanent Lock: stores (name_lower, symbol_upper)
tracked_presales = {}      # Presale state memory: { contract_address: dict }

intents = discord.Intents.default()
intents.message_content = True 
bot = commands.Bot(command_prefix="!", intents=intents)


# --- DATA HELPERS & SECURITY ---

async def fetch_json(session, url):
    """Safely fetch JSON from endpoints."""
    try:
        async with session.get(url, timeout=12) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        print(f"[HTTP Error] {url}: {e}", flush=True)
    return None


async def check_goplus_security(session, chain_id, contract_address):
    """
    Checks Honeypot status and Top 10 Holder distribution using GoPlus.
    Bypasses Solana and handles missing index data safely.
    """
    if chain_id == "sol":
        return {"safe": True, "top10_percent": 0.0}

    if not contract_address:
        return {"safe": False, "top10_percent": 100.0}

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

def parse_presale_links(websites, socials):
    """
    Priority Rules:
    1. Primary: Official Website / Custom Presale Link. If none, PinkSale becomes primary.
    2. Secondary: PinkSale Link (if an official link took primary slot).
    """
    official_website = None
    custom_presale_link = None
    pinksale_link = None
    twitter_link = None

    if isinstance(websites, list):
        for site in websites:
            if not isinstance(site, dict): continue
            url = site.get("url", "")
            if not url: continue

            url_lower = url.lower()
            if "pinksale.finance" in url_lower or "pinksale.com" in url_lower:
                pinksale_link = url
            elif any(kw in url_lower for kw in ["/presale", "presale.", "/ico", "/launchpad", "/seed"]):
                custom_presale_link = url
            elif not official_website:
                official_website = url

    if isinstance(socials, list):
        for s in socials:
            if not isinstance(s, dict): continue
            url = s.get("url", "")
            if ("twitter" in s.get("type", "").lower() or "x.com" in url.lower()) and not twitter_link:
                twitter_link = url
            elif ("pinksale" in url.lower()) and not pinksale_link:
                pinksale_link = url

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


# --- CORE SCREENER & TRACKER ---

async def screen_tokens():
    alerts = []
    pairs_to_evaluate = []
    
    async with aiohttp.ClientSession() as session:
        current_time = datetime.now(timezone.utc)

        # 1. Fetch raw token updates from DexScreener
        profiles = await fetch_json(session, "https://api.dexscreener.com/token-profiles/latest/v1")
        latest_boosts = await fetch_json(session, "https://api.dexscreener.com/token-boosts/latest/v1")
        top_boosts = await fetch_json(session, "https://api.dexscreener.com/token-boosts/top/v1")
        
        candidate_items = []
        if isinstance(profiles, list): candidate_items.extend(profiles)
        if isinstance(latest_boosts, list): candidate_items.extend(latest_boosts)
        if isinstance(top_boosts, list): candidate_items.extend(top_boosts)

        seen_token_addrs = set()
        token_requests = []
        
        for item in candidate_items:
            if not isinstance(item, dict): continue
            chain = str(item.get("chainId", "")).lower()
            token_addr = item.get("tokenAddress")
            
            if chain in CHAIN_MAP and token_addr and (chain, token_addr) not in seen_token_addrs:
                seen_token_addrs.add((chain, token_addr))
                token_requests.append((chain, token_addr))

        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Discovered {len(token_requests)} active tokens. Hydrating market data...", flush=True)

        for chain, token_addr in token_requests:
            p_data = await fetch_json(session, f"https://api.dexscreener.com/tokens/v1/{chain}/{token_addr}")
            if isinstance(p_data, list):
                pairs_to_evaluate.extend(p_data)

        evaluated_contracts = set()

        for pair in pairs_to_evaluate:
            if not isinstance(pair, dict): continue

            base_token = pair.get("baseToken", {})
            if not isinstance(base_token, dict): continue

            contract = base_token.get("address")
            raw_name = str(base_token.get("name", "Unknown Token")).strip()
            raw_symbol = str(base_token.get("symbol", "")).upper().strip()
            chain = str(pair.get("chainId", "")).lower()
            
            if not contract or chain not in CHAIN_MAP or contract in evaluated_contracts:
                continue
            if raw_symbol in EXCLUDED_SYMBOLS:
                continue

            evaluated_contracts.add(contract)

            # 1. PERMANENT IDENTITY LOCK (Name + Symbol)
            identity_key = (raw_name.lower(), raw_symbol)
            if identity_key in seen_identities:
                continue

            # 2. 6-HOUR CONTRACT COOLDOWN
            last_call = callout_cooldowns.get(contract)
            if last_call and (current_time - last_call) < timedelta(hours=COOLDOWN_HOURS):
                continue

            # 3. BREAKOUT METRICS
            volume_data = pair.get("volume", {}) or {}
            price_change_data = pair.get("priceChange", {}) or {}
            txns_data = (pair.get("txns", {}) or {}).get("m5", {}) or {}

            vol_surge_5m = float(volume_data.get("m5", 0) or 0)
            price_change_5m = float(price_change_data.get("m5", 0) or 0)
            buys_5m = int(txns_data.get("buys", 0) or 0)
            sells_5m = int(txns_data.get("sells", 0) or 0)

            if vol_surge_5m < 1000.0 or price_change_5m < 2.0 or buys_5m <= sells_5m:
                continue

            # 4. GO-PLUS SECURITY
            goplus_chain = "1" if chain == "ethereum" else ("56" if chain == "bsc" else chain)
            security = await check_goplus_security(session, goplus_chain, contract)
            if not security["safe"]:
                continue

            # Parse Links with Presale Prioritization
            info = pair.get("info", {}) or {}
            link_data = parse_presale_links(info.get("websites", []), info.get("socials", []))

            # Calculate Display Age
            pair_created = pair.get("pairCreatedAt")
            age_display = f"{((current_time - datetime.fromtimestamp(pair_created / 1000, tz=timezone.utc)).total_seconds() / 86400.0):.1f} Days" if pair_created else "0.0 Days"

            display_network = "Robinhood ETH" if chain == "ethereum" else chain.upper()

            # Determine Event Status Lifecycle
            event_type = "BREAKOUT"
            if link_data["primary_presale"]:
                event_type = "PRESALE_ACTIVE"

            alert_data = {
                "event_type": event_type,
                "name": raw_name,
                "symbol": f"${raw_symbol}",
                "network": display_network,
                "price": pair.get("priceUsd", "0.00"),
                "vol_surge_5m": vol_surge_5m,
                "vol_24h": volume_data.get("h24", 0),
                "price_change_5m": price_change_5m,
                "liquidity": pair.get("liquidity", {}).get("usd", 0),
                "buys_5m": buys_5m,
                "sells_5m": sells_5m,
                "age_days": age_display,
                "photo": info.get("imageUrl"),
                "contract": contract,
                "gmgn_link": f"https://gmgn.ai/{CHAIN_MAP[chain]}/token/{contract}",
                "link_data": link_data,
                "identity_key": identity_key
            }
            
            alerts.append(alert_data)

    return alerts


# --- DISCORD BROADCASTING ENGINE ---

@tasks.loop(minutes=5)
async def tracker_loop():
    print(f"\n=========================================", flush=True)
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Starting Presale & Breakout Scan...", flush=True)
    print(f"=========================================", flush=True)
    
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"[Error] Discord Channel ID {CHANNEL_ID} inaccessible.", flush=True)
        return

    alerts = await screen_tokens()
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Scan complete. Found {len(alerts)} items.", flush=True)
    
    current_time = datetime.now(timezone.utc)

    for alert in alerts:
        ld = alert["link_data"]
        
        # Dynamic Title Formatting
        if alert["event_type"] == "PRESALE_ACTIVE":
            embed_title = f"🚨 Presale Alert! | [{alert['network']}] {alert['symbol']}"
            color = discord.Color.gold()
        else:
            embed_title = f"Breakout Detected! | [{alert['network']}] {alert['symbol']}"
            color = discord.Color.red()

        embed = discord.Embed(
            title=embed_title,
            color=color,
            timestamp=datetime.now(timezone.utc)
        )

        photo_url = alert.get("photo")
        if photo_url and isinstance(photo_url, str) and photo_url.startswith(("http://", "https://")):
            embed.set_thumbnail(url=photo_url)

        # Row 1
        embed.add_field(name="Token Name", value=alert["name"], inline=True)
        embed.add_field(name="Symbol", value=alert["symbol"], inline=True)
        embed.add_field(name="Network", value=alert["network"], inline=True)

        # Row 2
        try:
            p_val = float(alert["price"])
            p_fmt = f"${p_val:.8f}".rstrip('0').rstrip('.') if p_val < 0.01 else f"${p_val:,.4f}"
        except (ValueError, TypeError):
            p_fmt = f"${alert['price']}"

        try:
            surge_5m = f"${float(alert['vol_surge_5m']):,.2f}"
        except (ValueError, TypeError):
            surge_5m = f"${alert['vol_surge_5m']}"

        try:
            vol_24h = f"${float(alert['vol_24h']):,.2f}"
        except (ValueError, TypeError):
            vol_24h = f"${alert['vol_24h']}"

        embed.add_field(name="Price USD", value=p_fmt, inline=True)
        embed.add_field(name="5m Volume Surge", value=surge_5m, inline=True)
        embed.add_field(name="24h Volume", value=vol_24h, inline=True)

        # Row 3
        try:
            pct_val = float(alert["price_change_5m"])
            pct_fmt = f"+{pct_val:.2f}%" if pct_val >= 0 else f"{pct_val:.2f}%"
        except (ValueError, TypeError):
            pct_fmt = f"{alert['price_change_5m']}%"

        try:
            liq_fmt = f"${float(alert['liquidity']):,.2f}"
        except (ValueError, TypeError):
            liq_fmt = f"${alert['liquidity']}"

        embed.add_field(name="5m Price Change", value=pct_fmt, inline=True)
        embed.add_field(name="Liquidity", value=liq_fmt, inline=True)
        embed.add_field(name="5m Buy/Sell Ratio", value=f"🟢 {alert['buys_5m']} / 🔴 {alert['sells_5m']}", inline=True)

        # Row 4
        embed.add_field(name="Age", value=alert["age_days"], inline=False)

        # Row 5: Presale Links & Socials
        links_list = []
        if ld["primary_presale"]:
            links_list.append(f"[{ld['primary_presale'][0]}]({ld['primary_presale'][1]})")
        if ld["secondary_presale"]:
            links_list.append(f"[{ld['secondary_presale'][0]}]({ld['secondary_presale'][1]})")
        if ld["website"]:
            links_list.append(f"[Website]({ld['website']})")
        if ld["twitter"]:
            links_list.append(f"[Twitter/X]({ld['twitter']})")

        embed.add_field(name="Official & Presale Links", value=" | ".join(links_list) if links_list else "None", inline=False)

        # Row 6 & 7
        embed.add_field(name="Trade on GMGN", value=f"👈 [Open GMGN Live Chart]({alert['gmgn_link']})", inline=False)
        embed.add_field(name="Contract Address", value=f"`{alert['contract']}`", inline=False)

        embed.set_footer(text="Pro Presale & Breakout Screener • 6h Deduplication Active")

        try:
            await channel.send(embed=embed)
            callout_cooldowns[alert["contract"]] = current_time
            seen_identities.add(alert["identity_key"])
            print(f"[Discord] Sent alert for {alert['symbol']} ({alert['name']})", flush=True)
        except Exception as e:
            print(f"[Discord Send Error] {e}", flush=True)


@bot.event
async def on_ready():
    print(f"Logged in successfully as {bot.user.name}", flush=True)
    if not tracker_loop.is_running():
        tracker_loop.start()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
