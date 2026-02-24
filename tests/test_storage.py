from pathlib import Path

from chronicle_keeper.storage import GuildSettingsStore


def test_campaign_create_use_and_resolve(tmp_path: Path):
    store = GuildSettingsStore(tmp_path / "guild_settings.json")
    guild_id = 123

    store.set_default_summary_language(guild_id, "ru")
    store.set_default_session_context(guild_id, "Default context")
    store.set_default_name_hints(guild_id, "Default hints")

    campaign = store.create_campaign(guild_id, "One Shot", summary_language="en")
    store.update_campaign(
        guild_id,
        campaign["id"],
        session_context="Campaign context",
        name_hints="Campaign hints",
    )
    store.set_active_campaign(guild_id, campaign["id"])

    resolved = store.resolve_active_campaign_settings(guild_id)
    assert resolved["campaign_id"] == campaign["id"]
    assert resolved["campaign_name"] == "One Shot"
    assert resolved["summary_language"] == "en"
    assert resolved["session_context"] == "Campaign context"
    assert resolved["name_hints"] == "Campaign hints"


def test_campaign_fallback_to_guild_defaults(tmp_path: Path):
    store = GuildSettingsStore(tmp_path / "guild_settings.json")
    guild_id = 456

    store.set_default_summary_language(guild_id, "uk")
    store.set_default_session_context(guild_id, "Guild context")
    store.set_default_name_hints(guild_id, "Guild hints")

    campaign = store.create_campaign(guild_id, "Fallback campaign", summary_language="")
    store.set_active_campaign(guild_id, campaign["id"])

    resolved = store.resolve_active_campaign_settings(guild_id)
    assert resolved["summary_language"] == "uk"
    assert resolved["session_context"] == "Guild context"
    assert resolved["name_hints"] == "Guild hints"
