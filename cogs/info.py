from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class InfoCog(commands.Cog, name="Info"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="userinfo", description="Show information about a server member.")
    @app_commands.describe(member="The member to look up (defaults to you).")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        target = member or interaction.user
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)
            if target is None:
                await interaction.response.send_message("Could not find that member in this server.", ephemeral=True)
                return

        roles = [r.mention for r in target.roles if r.name != "@everyone"]
        roles.reverse()

        embed = discord.Embed(
            title=f"User Info — {target}",
            color=target.top_role.color if target.top_role and target.top_role.color.value else discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="ID", value=str(target.id), inline=True)
        embed.add_field(name="Account Created", value=f"<t:{int(target.created_at.timestamp())}:F>", inline=True)
        if target.joined_at:
            embed.add_field(name="Joined Server", value=f"<t:{int(target.joined_at.timestamp())}:F>", inline=True)
        embed.add_field(
            name=f"Roles ({len(roles)})",
            value=" ".join(roles) if roles else "None",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="Show information about this server.")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Server Info — {guild.name}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>" if guild.owner_id else "Unknown", inline=True)
        embed.add_field(name="Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:F>", inline=True)
        embed.add_field(name="Boost Tier", value=str(guild.premium_tier), inline=True)
        embed.add_field(name="Boosts", value=str(guild.premium_subscription_count or 0), inline=True)
        embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="Server ID", value=str(guild.id), inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="Show a member's avatar.")
    @app_commands.describe(member="The member to look up (defaults to you).")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        embed = discord.Embed(
            title=f"{target}'s Avatar",
            color=discord.Color.blurple(),
        )
        embed.set_image(url=target.display_avatar.with_size(1024).url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction):
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! Latency: {latency_ms}ms")


async def setup(bot: commands.Bot):
    await bot.add_cog(InfoCog(bot))
