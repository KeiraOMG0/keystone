-- Migration 008: Seed welcome and boost templates + event bindings
-- Converted from hard-coded welcome.py and boost.py.
-- Uses INSERT OR IGNORE so re-running is safe.
-- Guild ID: 1278422221613830178
-- Welcome channel: 1278450589994324000
-- Boost channel:   1350912596894548038
-- Owner ID (created_by): 320737951460098049

-- -----------------------------------------------------------------------
-- Welcome template
-- -----------------------------------------------------------------------

INSERT OR IGNORE INTO message_templates
    (guild_id, name, description, category, content, created_by)
VALUES (
    1278422221613830178,
    'welcome',
    'Auto-seeded from legacy welcome.py',
    'events',
    'hihi, enjoy your stay!♡ {mention}',
    320737951460098049
);

INSERT OR IGNORE INTO template_embeds
    (template_id, position, description, color, image_url, author_name, author_icon_url, footer_text, footer_icon_url)
SELECT
    id,
    0,
    '** w**elcome to  ◜　 ♡　　𝘧𝘯𝘨　　₎ {mention} !
<:heart_pink:1501665256538247258> · make sure to [verify](https://discord.com/channels/1278422221613830178/1278435929144426650)
<:heart_white:1501665305829838959> · please read our [rules](https://discord.com/channels/1278422221613830178/1278426602753228853)
<:heart_pink:1501665256538247258> · *thank you* for joining',
    16758705,
    'https://64.media.tumblr.com/d815c294874283ef6a662f279eb9a316/a0a2f9972f66101e-f9/s2048x3072/2861f4add8e4143c3565f47ed93af52df9903e30.pnj',
    '{username}',
    '{avatar}',
    '{membercount} members !',
    'https://cdn3.emoji.gg/emojis/2911-pixel-toro-heartgive.gif'
FROM message_templates
WHERE guild_id = 1278422221613830178 AND name = 'welcome'
AND NOT EXISTS (
    SELECT 1 FROM template_embeds WHERE template_id = (
        SELECT id FROM message_templates WHERE guild_id = 1278422221613830178 AND name = 'welcome'
    )
);

-- -----------------------------------------------------------------------
-- Boost template
-- -----------------------------------------------------------------------

INSERT OR IGNORE INTO message_templates
    (guild_id, name, description, category, content, created_by)
VALUES (
    1278422221613830178,
    'boost',
    'Auto-seeded from legacy boost.py',
    'events',
    '୨ৎ　　　　　{mention}　　　🌷 　　　｡　　　boosted!',
    320737951460098049
);

INSERT OR IGNORE INTO template_embeds
    (template_id, position, description, color, image_url, author_name, author_icon_url, footer_text, footer_icon_url)
SELECT
    id,
    0,
    '**s**hout out to {mention} !
<:heart_pink:1501665256538247258> · [emoji](https://discord.com/channels/1278422221613830178/1325793825049940028)
<:heart_white:1501665305829838959> · [color](https://discord.com/channels/1278422221613830178/1319776898406154361)
<:heart_pink:1501665256538247258> · [chat](https://discord.com/channels/1278422221613830178/1326677556237439066)
<:heart_white:1501665305829838959> · *thank you* so much.ᐟ',
    16758705,
    'https://64.media.tumblr.com/d815c294874283ef6a662f279eb9a316/a0a2f9972f66101e-f9/s2048x3072/2861f4add8e4143c3565f47ed93af52df9903e30.pnj',
    '{username}',
    '{avatar}',
    '{boostcount} boosts!',
    'https://cdn3.emoji.gg/emojis/8338-catloveheart.gif'
FROM message_templates
WHERE guild_id = 1278422221613830178 AND name = 'boost'
AND NOT EXISTS (
    SELECT 1 FROM template_embeds WHERE template_id = (
        SELECT id FROM message_templates WHERE guild_id = 1278422221613830178 AND name = 'boost'
    )
);

-- -----------------------------------------------------------------------
-- Event bindings
-- -----------------------------------------------------------------------

INSERT OR IGNORE INTO event_bindings
    (guild_id, event_type, channel_id, send_dm, enabled, created_by)
VALUES
    (1278422221613830178, 'member_join', 1278450589994324000, 0, 1, 320737951460098049),
    (1278422221613830178, 'boost',       1350912596894548038, 0, 1, 320737951460098049);

-- Bind welcome template to member_join
INSERT OR IGNORE INTO event_binding_templates (binding_id, template_id, position)
SELECT
    eb.id,
    mt.id,
    0
FROM event_bindings eb, message_templates mt
WHERE eb.guild_id = 1278422221613830178
  AND eb.event_type = 'member_join'
  AND mt.guild_id = 1278422221613830178
  AND mt.name = 'welcome'
  AND NOT EXISTS (
      SELECT 1 FROM event_binding_templates ebt
      WHERE ebt.binding_id = eb.id AND ebt.template_id = mt.id
  );

-- Bind boost template to boost
INSERT OR IGNORE INTO event_binding_templates (binding_id, template_id, position)
SELECT
    eb.id,
    mt.id,
    0
FROM event_bindings eb, message_templates mt
WHERE eb.guild_id = 1278422221613830178
  AND eb.event_type = 'boost'
  AND mt.guild_id = 1278422221613830178
  AND mt.name = 'boost'
  AND NOT EXISTS (
      SELECT 1 FROM event_binding_templates ebt
      WHERE ebt.binding_id = eb.id AND ebt.template_id = mt.id
  );
