import { useLive } from "@/live/useLive"

/** Shown as `title` / hint on buttons that need the bot while it is unreachable. */
export const BOT_OFFLINE_REASON = "The bot is offline. This action is paused until it is back."

/**
 * True while the live socket is up and reports the bot process unreachable.
 * Pages use it to disable actions that hit Discord (with BOT_OFFLINE_REASON as
 * the reason). Read-only data keeps showing. Purely derived from `useLive()`.
 */
export function useBotOffline(): { offline: boolean; reason: string | undefined } {
  const { connected, bot } = useLive()
  const offline = connected && bot.online === false
  return { offline, reason: offline ? BOT_OFFLINE_REASON : undefined }
}
