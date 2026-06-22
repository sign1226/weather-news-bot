import asyncio
import sys
sys.path.insert(0, '/app')
from main import WeatherBot, BOT_TOKEN, CHANNEL_ID, build_embed, get_jma_warnings, WeatherView
import discord

async def test():
    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)
    
    @bot.event
    async def on_ready():
        channel = bot.get_channel(CHANNEL_ID)
        if not channel:
            print(f'Channel {CHANNEL_ID} not found')
            await bot.close()
            return
        
        warnings = get_jma_warnings()
        warning_text = ''
        if warnings:
            warning_text = '**⚠️ 警報・注意報（長野県）**\n' + '\n'.join(f'🔴 {w}' for w in warnings) + '\n\n'
        
        embed = build_embed('matsumoto')
        view = WeatherView()
        await channel.send(content=warning_text, embed=embed, view=view)
        print('Posted!')
        await bot.close()
    
    await bot.start(BOT_TOKEN)

asyncio.run(test())
