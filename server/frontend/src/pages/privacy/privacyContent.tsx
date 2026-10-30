import type { ReactNode } from "react"
import { Link } from "react-router-dom"

/**
 * Privacy Policy content for the public /privacy page (PrivacyPage.tsx renders
 * it; this module is only the words). Every statement here was checked against
 * the code on 2026-10-28 (see docs/ARCHITECTURE.md "Public legal pages" for the
 * item -> code map). When a feature starts storing something new, or a
 * retention changes, update the matching section, the RETENTION table and
 * PRIVACY_LAST_UPDATED. Do not claim anything the code doesn't do.
 */

/** Shown as "Last updated" at the top of the page. ISO date (YYYY-MM-DD). */
export const PRIVACY_LAST_UPDATED = "2026-10-30"

/**
 * CONTACT placeholder: optional extra contact line shown in the "Contact"
 * section (for example a privacy email). Left null on purpose: no email address
 * exists for this project, so none is invented. Set a string here to show it;
 * the Discord routes (ticket / server owner) are always shown.
 */
export const CONTACT_EXTRA: string | null = null

export interface PrivacySection {
  /** Anchor id (stable: people link to /privacy#<id>). */
  id: string
  title: string
  body: ReactNode
}

function P({ children }: { children: ReactNode }) {
  return <p>{children}</p>
}

function List({ children }: { children: ReactNode }) {
  return <ul className="flex list-disc flex-col gap-1.5 pl-5 marker:text-subtle-foreground">{children}</ul>
}

function H3({ children }: { children: ReactNode }) {
  return <h3 className="mt-2 text-md font-semibold text-foreground">{children}</h3>
}

const LINK_CLASS = "rounded-sm font-medium text-primary underline underline-offset-4 hover:text-primary-hover"

/** In-app link; a "#section" target stays a plain anchor so the browser scrolls to it. */
function A({ to, children }: { to: string; children: ReactNode }) {
  if (to.startsWith("#")) {
    return (
      <a href={to} className={LINK_CLASS}>
        {children}
      </a>
    )
  }
  return (
    <Link to={to} className={LINK_CLASS}>
      {children}
    </Link>
  )
}

/** Link to another site, opened in a new tab. */
function Ext({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className={LINK_CLASS}>
      {children}
    </a>
  )
}

function Code({ children }: { children: ReactNode }) {
  return <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.9em] text-foreground">{children}</code>
}

/** One row of the "How long we keep it" table. */
interface RetentionRow {
  what: string
  kept: string
  who: string
}

const RETENTION: RetentionRow[] = [
  {
    what: "Recent messages (moderation message log)",
    kept: "7 days, then deleted automatically (checked daily)",
    who: "Staff (exported by Discord DM only)",
  },
  {
    what: "Activity log (joins, leaves, role and nickname changes, voice joins, edited and deleted message text, moderation and settings actions)",
    kept: "365 days, then deleted automatically",
    who: "Staff and members given the audit-log permission",
  },
  {
    what: "Remembered profile (name, avatar, membership status)",
    kept: "While you are a member; 365 days after you leave; kept while you are still banned",
    who: "Staff and dashboard users who can see the pages that show names",
  },
  {
    what: "Warnings",
    kept: "Until staff remove them",
    who: "Staff and moderators",
  },
  {
    what: "Moderation history (kicks, bans, timeouts, warnings) and the kick log",
    kept: "No automatic deletion",
    who: "Staff and moderators",
  },
  {
    what: "Support tickets and ticket chat logs",
    kept: "While the ticket is open; 30 days after it is closed (the default), then deleted automatically",
    who: "Staff and members given the ticket permission",
  },
  {
    what: "Trap-channel catches and ban appeals",
    kept: "No automatic deletion",
    who: "Staff and moderators",
  },
  {
    what: "Birthday and time zone",
    kept: "Until you (or staff) remove it",
    who: "Everyone in the server can look it up; staff can export the list",
  },
  {
    what: "Fortnite shop wishlist",
    kept: "Until you remove the item",
    who: "You; the bot pings you in the shop post",
  },
  {
    what: "Dashboard login session",
    kept: "Until you log out, or 30 days without using the dashboard",
    who: "Nobody reads it; it only keeps you signed in",
  },
  {
    what: "Appeal sign-in cookie",
    kept: "30 minutes (or until you sign out)",
    who: "Only your browser and the website",
  },
  {
    what: "Website error reports (the error text, the page, your browser's name and version, and your Discord user ID and username if you were signed in; never your IP address)",
    kept: "30 days",
    who: "Only the bot owner",
  },
  {
    what: "Website and bot logs (may include IP addresses of website visitors)",
    kept: "Rotated weekly; old copies deleted after 28 days",
    who: "The server owner and staff",
  },
]

function RetentionTable() {
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card">
      <table className="w-full min-w-[560px] text-left text-sm">
        <thead>
          <tr className="border-b border-border">
            <th scope="col" className="px-4 py-2.5 text-xs font-semibold text-muted-foreground">
              What
            </th>
            <th scope="col" className="px-4 py-2.5 text-xs font-semibold text-muted-foreground">
              How long we keep it
            </th>
            <th scope="col" className="px-4 py-2.5 text-xs font-semibold text-muted-foreground">
              Who can see it
            </th>
          </tr>
        </thead>
        <tbody>
          {RETENTION.map((r) => (
            <tr key={r.what} className="border-b border-border align-top last:border-b-0">
              <td className="px-4 py-3 font-medium text-foreground">{r.what}</td>
              <td className="px-4 py-3">{r.kept}</td>
              <td className="px-4 py-3">{r.who}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export const PRIVACY_INTRO: ReactNode = (
  <>
    This policy explains what the <strong>Gamer Girls HQ</strong> Discord bot and its website at{" "}
    <strong>dash.idlesys.xyz</strong> collect about you, why, how long it is kept and who can see it. It describes what
    the software actually does today, in plain English.
  </>
)

export const PRIVACY_SECTIONS: PrivacySection[] = [
  {
    id: "about",
    title: "Who we are",
    body: (
      <>
        <P>
          The bot and website are run by the owner and staff team of the Gamer Girls HQ Discord server, as a free
          community project. They serve that one server only. They are not a company product, and they are not made by,
          endorsed by or affiliated with Discord.
        </P>
        <P>
          In this policy, &quot;we&quot; means the server owner and staff, &quot;the bot&quot; means our Discord bot,
          and &quot;the website&quot; means dash.idlesys.xyz (the dashboard, the ban appeal page and the other public
          pages). This page is written for transparency so members know exactly what happens with their information;
          it is not a lawyer-drafted legal document.
        </P>
      </>
    ),
  },
  {
    id: "summary",
    title: "The short version",
    body: (
      <List>
        <li>
          <strong>We never sell or rent your information</strong>, and there are no ads, analytics or tracking scripts
          on the website.
        </li>
        <li>
          Almost everything we have comes from Discord: your account ID, names, avatar, roles and what you do in our
          server.
        </li>
        <li>
          To deal with spam and abuse, the bot keeps a copy of messages sent in the server for{" "}
          <strong>7 days</strong>. Some activity (like deleted or edited messages and moderation actions) stays in a
          staff-only activity log for <strong>365 days</strong>.
        </li>
        <li>
          Data is stored on a private computer run by the server owner, not in a cloud database. Only staff, and
          members staff have given a specific permission, can see it.
        </li>
        <li>
          When you sign in to the website with Discord, we use Discord&apos;s login token once and{" "}
          <strong>never store it</strong>.
        </li>
        <li>
          You can ask to see or delete your information. See <A to="#rights">Your choices and rights</A>.
        </li>
      </List>
    ),
  },
  {
    id: "collect",
    title: "What we collect",
    body: (
      <>
        <H3>Your Discord profile</H3>
        <P>
          As a member of the server, the bot can see what Discord shows every server bot: your user ID, username,
          display name, server nickname, avatar, roles, when you joined, and whether you are online. The bot also
          remembers your last known username, display name, nickname, avatar, top role colour, join date and membership
          status (member, left, kicked, banned or unbanned, with when it changed and, for kicks and bans, which staff
          member did it), so staff tools still show a name instead of a number after someone leaves. Reasons, messages
          and roles are not part of that record. Online status is only ever shown to staff as a total count (for example
          &quot;40 online&quot;) and is never stored.
        </P>

        <H3>Messages (short-term moderation log)</H3>
        <P>
          The bot keeps a copy of messages sent in the server&apos;s channels: the text (including text inside embeds),
          the names and links of attachments (not the files themselves), the channel, the author, when it was sent, the
          latest edited version and when it was edited or deleted. For replies from apps and slash commands it also notes
          who ran the command. This exists so staff can see what a spammer or a hacked account posted, even after it was
          deleted.
        </P>
        <P>
          It does <strong>not</strong> include direct messages, support-ticket channels, system messages (like join or
          pin notices) or the bot&apos;s own messages.
        </P>

        <H3>Activity log</H3>
        <P>
          The bot records server events in a staff activity log: joins and leaves, role changes, nickname changes,
          joining, leaving or switching voice channels, server mutes and deafens by staff, server boosts, invites
          created (who made it and the invite code), and changes to channels, roles, emojis and stickers. When a message
          is deleted or edited, the log keeps the first 500 characters of the deleted text, or of the text before and
          after the edit. It also records every moderation action, anti-spam action, settings change and permission
          change, with who did it.
        </P>

        <H3>Moderation records</H3>
        <List>
          <li>
            <strong>Warnings, timeouts, kicks and bans</strong>: who it was about, which staff member did it, the reason
            and the time (and for timeouts, how long).
          </li>
          <li>
            <strong>Kick log</strong>: for each kick, the username and ID, when you joined and the names of your roles at
            the time.
          </li>
          <li>
            <strong>Automatic removals</strong>: members who stay unverified for more than 7 days are kicked
            automatically, and that kick is recorded the same way.
          </li>
          <li>
            <strong>Anti-spam</strong>: to spot flooding, repeated messages and mass mentions, the bot looks at the last
            few seconds of each member&apos;s messages in memory only (nothing is saved). If it acts, the action (which
            rule, how many messages it removed, the timeout) goes into the activity log and the staff log channel.
          </li>
        </List>

        <H3>The trap channel and ban appeals</H3>
        <P>
          The server has a trap channel that members are clearly warned not to post in, because hacked accounts and spam
          bots do. When someone posts there, the bot records the catch: user ID, username, display name, avatar, the
          first 500 characters of the message, when the account was created and when it joined, which app posted it (if
          any), simple flags (whether it had a link, an invite, an @everyone or an attachment), what action was taken and
          whether the notice DM was delivered.
        </P>
        <P>
          If you appeal, we store your Discord user ID and username, the name you typed, your message, the status of the
          appeal, and the moderator&apos;s decision, note and time. The private appeal link we DM you is stored only as a
          one-way scrambled fingerprint (a hash), never as the link itself. If you sign in with Discord on the appeal
          page, Discord tells us your user ID and username; we use Discord&apos;s login token once and then cancel it
          straight away. If you choose &quot;Rejoin server&quot; after an approved appeal, you grant permission to add you
          back to the server; the bot uses it once to add you and the token is then cancelled. No Discord login token is
          ever stored.
        </P>

        <H3>Support tickets</H3>
        <P>
          When you open a ticket, we store the ticket number, who opened it, which staff member claimed it, its status
          and timestamps. While ticket chat logging is turned on (it currently is), the text of messages sent in ticket
          channels is saved as a chat log (plain text only, never attachments or voice messages) so staff can review it
          on the dashboard. When a ticket is closed, a short transcript (up to 50 lines) may be posted to a staff log
          channel in Discord.
        </P>

        <H3>Things you choose to give us</H3>
        <List>
          <li>
            <strong>Birthday</strong>: the month and day you set (never the year) and, if you set one, your time zone, so
            the bot can announce it and give you the birthday role on the right day.
          </li>
          <li>
            <strong>Fortnite shop wishlist</strong>: the item names you add, so the bot can ping you when they are in the
            shop.
          </li>
          <li>
            <strong>Music</strong>: the songs or links you request. The queue and the &quot;recently played&quot; list
            live in memory only. Saved playlists contain song titles and links, not who saved them.
          </li>
          <li>
            <strong>Self-roles</strong>: picking a role from a role menu just gives you that Discord role; we don&apos;t
            keep a separate record of it beyond the activity log entry for the role change.
          </li>
        </List>

        <H3>Signing in to the dashboard</H3>
        <P>
          Staff and members can sign in to the dashboard with Discord. We ask Discord for two permissions:{" "}
          <Code>identify</Code> (your user ID, username and avatar) and <Code>guilds.members.read</Code> (your roles in
          this server, to check what you&apos;re allowed to see). We store a login session with your user ID, username,
          avatar, whether you are a member, your role IDs at sign-in, and when the session was created and last used. The
          Discord login token is used only during sign-in, is not stored, and is cancelled with Discord right after.
        </P>

        <H3>Website visits</H3>
        <P>
          Like most websites, our web server logs requests: the page or API address asked for, the time, the result and
          the visitor&apos;s IP address (passed on by Cloudflare). We use these logs only to keep the site running and to
          investigate problems or abuse. Appeal links and sign-in codes are removed from each request before it is
          written to the log, and hidden again when staff view logs in the dashboard. Public forms (the appeal page and sign-in) are rate limited by IP address; that counter lives in
          memory and is not saved.
        </P>
        <P>
          If a page on the website crashes or hits an unexpected error, your browser sends us an error report: the error
          message and where in our code it happened, the page address (without anything after a &quot;?&quot;, and with
          appeal links hidden), and your browser&apos;s name and version. If you are signed in to the dashboard, the
          report also includes your Discord user ID and username. We do not store your IP address with it. Only the bot
          owner can see these reports, and they are deleted after <strong>30 days</strong>. They are used only to find
          and fix bugs.
        </P>
      </>
    ),
  },
  {
    id: "use",
    title: "Why we use it",
    body: (
      <>
        <P>We only use information to run the server and keep it safe:</P>
        <List>
          <li>
            <strong>Keeping the community safe</strong> (our legitimate interest): moderation, anti-spam, catching hacked
            accounts, verifying new members, reviewing appeals, and the message and activity logs.
          </li>
          <li>
            <strong>Providing features you use</strong>: tickets, birthdays, music, self-roles, the Fortnite wishlist,
            Question of the Day and the dashboard.
          </li>
          <li>
            <strong>Things you choose</strong> (your consent): your birthday, time zone and wishlist are only stored
            because you added them, and you can remove them at any time.
          </li>
          <li>
            <strong>Keeping things working and secure</strong>: login sessions, rate limits and logs.
          </li>
        </List>
        <P>
          We don&apos;t use your information for advertising, profiling or selling, and we don&apos;t train AI models on
          it. The Question of the Day is written by an AI model that runs on the server owner&apos;s own computer; it is
          only asked for a new question and never sees member messages or profiles.
        </P>
      </>
    ),
  },
  {
    id: "retention",
    title: "How long we keep it",
    body: (
      <>
        <P>
          Automatic deletion runs on a schedule, so something may stay up to about a day past the time shown. Anything the
          bot posts into Discord channels (for example welcome messages, staff log posts or transcripts) stays in Discord
          until someone deletes it.
        </P>
        <RetentionTable />
        <P>
          &quot;No automatic deletion&quot; means the record is kept until staff remove it, because ban and moderation
          history is needed to protect the server from people coming back. You can still ask about it; see{" "}
          <A to="#rights">Your choices and rights</A>.
        </P>
      </>
    ),
  },
  {
    id: "access",
    title: "Who can see it",
    body: (
      <>
        <List>
          <li>
            <strong>The server owner and staff</strong> can see all of the information described here, through Discord
            commands and the dashboard.
          </li>
          <li>
            <strong>Members given a specific bot permission</strong> by staff (for example moderators) can see the parts
            that permission covers, such as moderation tools, tickets, trap-channel catches or the activity log. Every
            dashboard request checks this permission live.
          </li>
          <li>
            <strong>Everyone in the server</strong> can see what is public in Discord anyway, and can look up a
            member&apos;s birthday with the birthday command.
          </li>
          <li>
            <strong>You</strong> can see your own appeal and its outcome on the appeal page (never the staff note).
          </li>
        </List>
        <P>The message log can only be exported by staff, and only into their Discord DMs, never into a channel.</P>
      </>
    ),
  },
  {
    id: "sharing",
    title: "Sharing and third parties",
    body: (
      <>
        <P>
          <strong>We never sell, rent or trade your information.</strong> We don&apos;t share it with anyone except the
          services the bot and website need to work:
        </P>
        <List>
          <li>
            <strong>Discord</strong>: the bot and website run on Discord. Everything in the server, sign-in, DMs and
            role changes go through Discord. See{" "}
            <Ext href="https://discord.com/privacy">Discord&apos;s Privacy Policy</Ext>.
          </li>
          <li>
            <strong>Cloudflare</strong>: the website is reached through a Cloudflare Tunnel, so Cloudflare carries all
            website traffic and processes things like your IP address to deliver and protect it. See{" "}
            <Ext href="https://www.cloudflare.com/privacypolicy/">Cloudflare&apos;s Privacy Policy</Ext>.
          </li>
          <li>
            <strong>YouTube (Google)</strong>: music searches and links you request are looked up and played from YouTube
            (or, for a link to another site, from that site) by the server owner&apos;s computer. They see the search or
            the link, not who asked for it. See{" "}
            <Ext href="https://policies.google.com/privacy">Google&apos;s Privacy Policy</Ext>.
          </li>
          <li>
            <strong>fortnite-api.com</strong>: the bot downloads the daily Fortnite item shop from it. No member
            information is sent.
          </li>
        </List>
        <P>
          Avatars on the website load from Discord&apos;s image servers. We may also share information if the law
          requires it, or with Discord&apos;s Trust &amp; Safety team to report serious abuse.
        </P>
      </>
    ),
  },
  {
    id: "cookies",
    title: "Cookies and browser storage",
    body: (
      <>
        <P>
          The website only uses cookies needed for signing in. There are no advertising or analytics cookies, and none
          are set just by visiting the public pages.
        </P>
        <List>
          <li>
            <Code>dashboard_session</Code>: keeps you signed in to the dashboard. Signed, HttpOnly, sent only over HTTPS on
            the live site. The session behind it ends after you log out or after 30 days without using the dashboard.
          </li>
          <li>
            <Code>dashboard_oauth_state</Code>: a random value that protects the Discord sign-in from being forged. Lasts
            10 minutes.
          </li>
          <li>
            <Code>appeal_identity</Code>: remembers who you signed in as on the appeal page (your user ID and username).
            Signed, HttpOnly, lasts 30 minutes, cleared when you sign out.
          </li>
          <li>
            <Code>appeal_oauth_state</Code>: the same sign-in protection for the appeal page. Lasts 10 minutes.
          </li>
        </List>
        <P>
          The website also saves a few display preferences in your browser&apos;s local storage (theme, layout density,
          sidebar state, recently visited dashboard pages and similar settings). They never leave your browser, and you
          can clear them by clearing site data.
        </P>
      </>
    ),
  },
  {
    id: "security",
    title: "How we protect it",
    body: (
      <>
        <List>
          <li>The public website is only reachable over HTTPS, through Cloudflare.</li>
          <li>
            The bot&apos;s internal services only accept connections from the same computer and require a secret key.
          </li>
          <li>Every dashboard page and action checks your permissions on the server, not just in your browser.</li>
          <li>Discord login tokens are never stored, and appeal links are stored only as hashes.</li>
          <li>Public forms are rate limited, and sign-in is protected against forged requests.</li>
          <li>Secrets, tokens and visitor IP addresses are hidden when staff view logs in the dashboard.</li>
          <li>Old data is deleted automatically on the schedules above.</li>
        </List>
        <P>
          No system is perfectly secure. If we ever learn that member information was exposed, we will tell the server
          as soon as we reasonably can.
        </P>
      </>
    ),
  },
  {
    id: "rights",
    title: "Your choices and rights",
    body: (
      <>
        <P>Some things you can do yourself, any time:</P>
        <List>
          <li>
            Remove your birthday with <Code>/birthday remove</Code>.
          </li>
          <li>
            Remove wishlist items with <Code>/fortnite wishlist remove</Code>.
          </li>
          <li>
            Sign out of the dashboard (profile menu, &quot;Sign out&quot;), which ends that login session.
          </li>
          <li>Sign out on the appeal page to remove the appeal cookie.</li>
          <li>
            Revoke the bot&apos;s access under Discord&apos;s <strong>User Settings → Authorized Apps</strong>.
          </li>
        </List>
        <P>
          You can also ask us for a copy of the information we hold about you, to correct it, or to delete it. Depending
          on where you live, data protection laws may give you these rights. We&apos;ll do our best to help. We may keep
          some records where we need them to protect the server, for example the record of a ban, and if so we&apos;ll
          tell you what we kept and why. For example, after you leave, staff can delete your remembered profile straight
          away instead of waiting for the 365 days (while you are still a member it is simply refreshed from Discord
          again). Messages and activity that are already in Discord itself are controlled by
          Discord, not us.
        </P>
        <P>
          To ask, use the <A to="#contact">contact options below</A>. We may need to confirm the request really comes
          from your Discord account.
        </P>
      </>
    ),
  },
  {
    id: "children",
    title: "Children",
    body: (
      <P>
        You must meet Discord&apos;s minimum age (13 in most countries, higher in some) to use Discord, our server, the
        bot or the website. We don&apos;t knowingly collect information from anyone under that age. If you think a child
        under the minimum age is using the server, tell staff and we&apos;ll deal with it.
      </P>
    ),
  },
  {
    id: "changes",
    title: "Changes to this policy",
    body: (
      <P>
        When the bot or website starts handling information differently, we&apos;ll update this page and the &quot;Last
        updated&quot; date at the top, and we&apos;ll try to mention bigger changes in the server.
      </P>
    ),
  },
  {
    id: "contact",
    title: "Contact",
    body: (
      <>
        <P>For privacy questions or requests:</P>
        <List>
          <li>open a ticket from a ticket panel in the Gamer Girls HQ server, or</li>
          <li>message the server owner on Discord.</li>
        </List>
        {CONTACT_EXTRA ? <P>{CONTACT_EXTRA}</P> : null}
        <P>
          See also our <A to="/terms">Terms of Service</A> and{" "}
          <Ext href="https://discord.com/privacy">Discord&apos;s Privacy Policy</Ext>.
        </P>
      </>
    ),
  },
]
