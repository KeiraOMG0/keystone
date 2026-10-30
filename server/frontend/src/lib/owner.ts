/**
 * Pseudo-permission key for the hidden owner-only Debug page. AuthContext sets
 * `permissions[OWNER_ONLY_PERMISSION] = true` only when GET /api/me says
 * `is_owner`, so NAV_LINKS / the command palette / `g` hotkeys can reuse their
 * existing `requiredPermission` filter. Never a real bot permission string; the
 * server enforces the page with require_owner (404 for everyone else).
 */
export const OWNER_ONLY_PERMISSION = "__owner_only"

/**
 * Pseudo-permission for pages the server gates with require_staff (owner or a
 * STAFF_ROLE_IDS role): Diagnostics, Roles, Permissions, Settings. AuthContext
 * sets it only when GET /api/me says `is_staff` (missing = false). The
 * grantable `staff_base` permission is NOT enough for those pages (they would
 * 403), so their nav items, palette entries and quick actions use this key.
 */
export const STAFF_ONLY_PERMISSION = "__staff_only"

/**
 * Pseudo-permission for the Automation page's nav item: AuthContext sets it
 * when the user holds ANY of the page's tab permissions (staff hold all of
 * them, and the Welcome & Boost tab is staff-only). Each tab is still gated by
 * its own permission, server-side and with an inline 403.
 */
export const AUTOMATION_ANY_PERMISSION = "__automation_any"
export const AUTOMATION_PERMISSIONS = ["manage_events", "manage_schedules", "manage_autorole", "manage_templates", "staff_base"] as const

/**
 * Pseudo-permission for the Moderation nav item / page: set when the user
 * holds ANY of moderate_members, kick_members, ban_members (staff hold all),
 * so kick-only or ban-only roles can use the page. Each section and action is
 * still gated by its own permission server-side (inline 403 / hidden here).
 */
export const MODERATION_ANY_PERMISSION = "__moderation_any"
export const MODERATION_PERMISSIONS = ["moderate_members", "kick_members", "ban_members"] as const
