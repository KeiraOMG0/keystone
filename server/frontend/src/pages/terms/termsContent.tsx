import type { ReactNode } from "react"
import { Link } from "react-router-dom"

/**
 * Terms of Service content for the public /terms page (TermsPage.tsx renders
 * it; this module is only the words). Every statement here must match what
 * the bot and dashboard really do (docs/ARCHITECTURE.md + the code): when a
 * feature changes, update the matching section and TERMS_LAST_UPDATED.
 */

/** Shown as "Last updated" at the top of the page. ISO date (YYYY-MM-DD). */
export const TERMS_LAST_UPDATED = "2026-10-30"

/**
 * CONTACT placeholder: optional extra contact line shown in the "Contact"
 * section (for example a support email). Left null on purpose: no email
 * address exists for this project, so none is invented. Set a string here to
 * show it; the Discord routes (ticket / server owner) are always shown.
 */
export const CONTACT_EXTRA: string | null = null

export interface TermsSection {
  /** Anchor id (stable: people link to /terms#<id>). */
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

export const TERMS_INTRO: ReactNode = (
  <>
    These terms cover the <strong>Gamer Girls HQ</strong> Discord server&apos;s bot and its website at{" "}
    <strong>dash.idlesys.xyz</strong>. They are written in plain English. Please read them; by using the bot or the
    website you agree to them.
  </>
)

export const TERMS_SECTIONS: TermsSection[] = [
  {
    id: "about",
    title: "About these terms",
    body: (
      <>
        <P>
          &quot;We&quot;, &quot;us&quot; and &quot;staff&quot; mean the owner and the moderators of the Gamer Girls HQ
          Discord server (&quot;the server&quot;). &quot;The bot&quot; is the server&apos;s own Discord bot.
          &quot;The website&quot; is dash.idlesys.xyz, including the staff dashboard and the public pages. This is a
          community project run by volunteers, not a company, and it only serves this one server.
        </P>
        <P>
          By joining the server, using any bot command or button, or using the website, you agree to these terms. If
          you don&apos;t agree, please don&apos;t use the bot or the website.
        </P>
        <P>
          Discord&apos;s own <Ext href="https://discord.com/terms">Terms of Service</Ext> and{" "}
          <Ext href="https://discord.com/guidelines">Community Guidelines</Ext>{" "}
          always apply as well, and so do the server&apos;s rules posted in Discord. If anything here conflicts with
          Discord&apos;s terms, Discord&apos;s terms win.
        </P>
      </>
    ),
  },
  {
    id: "eligibility",
    title: "Who can use the bot and website",
    body: (
      <>
        <P>
          You must be old enough to use Discord where you live (at least 13 in most countries, older in some) and have a
          Discord account that Discord allows you to use. If you&apos;re below that age, please don&apos;t use the bot,
          the server or the website.
        </P>
        <P>
          Signing in to the website is only possible for members of the server (the ban appeal page has its own,
          separate sign-in, see <A to="#appeals">Ban appeals</A>).
        </P>
      </>
    ),
  },
  {
    id: "features",
    title: "What the bot does",
    body: (
      <>
        <P>The bot helps run the server. Depending on what staff have switched on, it:</P>
        <List>
          <li>gives staff moderation tools (warnings, timeouts, kicks, bans, message clean-up) and logs what happens;</li>
          <li>
            handles verification: new members start as Unverified and get full access once staff verify them (with a
            command or from a verification ticket);
          </li>
          <li>runs support tickets (private channels between you and staff);</li>
          <li>offers self-role buttons, so you can pick some roles yourself;</li>
          <li>posts welcome and boost messages, birthday announcements and a Question of the Day;</li>
          <li>plays music in voice channels;</li>
          <li>posts the daily Fortnite item shop and lets you keep a shop wishlist;</li>
          <li>runs automatic anti-spam and anti-hacked-account protection (explained below).</li>
        </List>
        <P>
          To do this the bot stores some information, such as moderation records and recent messages kept for a short
          time for moderation. What is stored and for how long is explained in the <A to="/privacy">Privacy Policy</A>.
        </P>
      </>
    ),
  },
  {
    id: "moderation",
    title: "Moderation and staff discretion",
    body: (
      <>
        <P>
          Staff may warn you, time you out, delete your messages, kick you or ban you when they believe it&apos;s
          needed to keep the server safe and friendly, including for things not spelled out in the rules. These are
          judgement calls and staff have the final say in the server.
        </P>
        <P>
          Moderation actions are recorded (who did it, to whom, when and why), shown to staff on the dashboard and
          posted in a staff-only log channel. Actions taken directly in Discord are logged too.
        </P>
        <P>
          If you think a moderation decision was a mistake, open a ticket or message the server owner on Discord (see{" "}
          <A to="#contact">Contact</A>). Please do it calmly; arguing in public channels won&apos;t help.
        </P>
      </>
    ),
  },
  {
    id: "automated-moderation",
    title: "Automated moderation",
    body: (
      <>
        <P>
          Some moderation happens automatically, without a person looking first. Staff, moderators and server admins are
          not affected by the anti-spam and trap-channel checks.
        </P>
        <List>
          <li>
            <strong>Anti-spam.</strong> Sending lots of messages very quickly, posting the same message in several
            channels, or mentioning many people in one message gets those messages deleted automatically, and you may be
            timed out (staff set the limits and the timeout length). Messages from an app you run in the server count as
            your messages.
          </li>
          <li>
            <strong>Trap channel.</strong> The server has one channel that exists only to catch hacked accounts and spam
            bots, which post in every channel they can find. <strong>Anyone who posts in it is removed automatically</strong>:
            the message is deleted and you are banned (or kicked, depending on the current staff setting). This includes
            messages sent through an app you run. The channel&apos;s name is random and changes regularly, so you
            can&apos;t rely on the name to recognise it; the warning message inside the channel is your notice. If you
            don&apos;t know what a channel is for, don&apos;t post in it. Messages from webhooks or other bots in that
            channel are deleted, and the webhook is removed.
          </li>
          <li>
            <strong>Unverified clean-up.</strong> Members who are still Unverified more than 7 days after joining are
            kicked automatically. You can join again and get verified.
          </li>
        </List>
        <P>
          Automatic systems can get things wrong. If one caught you by mistake, use the{" "}
          <A to="/appeal">ban appeal page</A> for a trap-channel ban, or tell staff for anything else.
        </P>
      </>
    ),
  },
  {
    id: "appeals",
    title: "Ban appeals",
    body: (
      <>
        <P>
          If you were banned by the trap channel, you can appeal at <A to="/appeal">dash.idlesys.xyz/appeal</A> (or
          from the link in the message the bot tried to send you). You must sign in with Discord to appeal, so we know
          the appeal comes from the banned account.
        </P>
        <List>
          <li>
            Signing in with Discord only proves which account is appealing. The bot reads your username and ID once and
            does not keep your Discord login.
          </li>
          <li>
            Staff review each appeal and approve or deny it. You can come back to the appeal page to see whether it is
            pending, approved or not approved. Staff notes are not shown to you. The bot also tries to message you the
            outcome, but Discord may not deliver it after a ban.
          </li>
          <li>
            If it&apos;s approved, you&apos;re unbanned and can use <strong>Rejoin server</strong> on the appeal page
            (Discord asks you to allow &quot;join servers for you&quot;, used once to add you back) or an invite link.
            Your old roles are not restored; you go through verification again like a new member.
          </li>
          <li>If it&apos;s not approved, the ban stays.</li>
          <li>
            One appeal per ban. Please be honest: false information or repeated junk submissions will be denied. The
            appeal pages are rate limited.
          </li>
        </List>
        <P>
          The appeal page is only for trap-channel bans. For any other ban, contact the server owner on Discord.
        </P>
      </>
    ),
  },
  {
    id: "tickets",
    title: "Tickets",
    body: (
      <>
        <P>
          You can open a ticket from a ticket panel in the server to talk privately with staff (for example for help or
          verification). A panel may limit how many tickets you can have open at once.
        </P>
        <List>
          <li>Only staff can close a ticket. Closing it deletes the channel about 30 seconds later.</li>
          <li>Tickets with no activity for 7 days are closed automatically.</li>
          <li>
            When ticket chat logging is turned on, the conversation is saved so staff can read it later on the
            dashboard. Old closed tickets are deleted after a period staff set.
          </li>
          <li>Please don&apos;t open tickets for spam or jokes, or to harass staff.</li>
        </List>
      </>
    ),
  },
  {
    id: "music",
    title: "Music",
    body: (
      <>
        <P>
          Any member can use the music commands (<Code>!play</Code>, <Code>!skip</Code>, <Code>!queue</Code> and so on) in
          a voice channel. Skipping a song someone else asked for is a vote.
        </P>
        <List>
          <li>
            <strong>You are responsible for what you queue.</strong> Don&apos;t play anything that breaks Discord&apos;s
            rules or the server&apos;s rules.
          </li>
          <li>
            Music is played from YouTube. The videos and songs belong to their owners, and YouTube&apos;s terms apply to
            them. The bot only streams them into the voice channel; it doesn&apos;t sell, host or claim any of it.
          </li>
          <li>Staff may stop playback or remove songs at any time. Saved playlists can be lost.</li>
        </List>
      </>
    ),
  },
  {
    id: "community",
    title: "Birthdays, Question of the Day and self-roles",
    body: (
      <List>
        <li>
          <strong>Birthdays.</strong> Adding your birthday and time zone is optional. On the day, the bot gives you a
          birthday role and posts an announcement. You can remove your birthday at any time with{" "}
          <Code>/birthday remove</Code>; staff can also correct or remove it.
        </li>
        <li>
          <strong>Question of the Day.</strong> Questions may be written by an AI model or picked from a list, and staff
          approve each one before it is posted.
        </li>
        <li>
          <strong>Self-roles.</strong> Role buttons let you add or remove some roles yourself. Roles that carry staff or
          moderation powers can never be self-assigned.
        </li>
        <li>
          <strong>Fortnite item shop.</strong> Shop data comes from the unofficial fortnite-api.com. This server and bot
          are not affiliated with Epic Games.
        </li>
      </List>
    ),
  },
  {
    id: "dashboard",
    title: "The website and dashboard",
    body: (
      <>
        <P>
          You sign in to the dashboard with Discord. The moderation and management tools are only for staff and for
          members whose roles staff have given a specific permission. Your access is checked again on every request.
          Being able to sign in does not mean you&apos;re allowed to use the tools.
        </P>
        <P>Please don&apos;t:</P>
        <List>
          <li>try to open pages or actions you haven&apos;t been given, or get around the permission checks;</li>
          <li>share your session or sign in as someone else;</li>
          <li>
            scrape, automate, flood or attack the website or its public pages and endpoints (status, ban appeals, sign-in).
            They are rate limited, and abuse can get you blocked and removed from the server;
          </li>
          <li>look for or use security holes. If you find one, tell the server owner privately instead.</li>
        </List>
        <P>
          The public pages (home, <A to="/status">status</A>, <A to="/appeal">ban appeal</A>, these terms and the{" "}
          <A to="/privacy">Privacy Policy</A>) are open to everyone without signing in. The <A to="/hub">member hub</A>{" "}
          needs a Discord sign-in: it shows you only your own information, and roles you pick there follow the same
          rules as the role buttons in the server. Sessions last up to 30 days and you can sign out at any time.
        </P>
      </>
    ),
  },
  {
    id: "conduct",
    title: "Your conduct",
    body: (
      <List>
        <li>Follow Discord&apos;s rules and the server&apos;s rules when you use the bot.</li>
        <li>Don&apos;t use the bot to spam, harass, or mass-ping people.</li>
        <li>Don&apos;t try to break, overload or exploit the bot, or trick it into doing something staff didn&apos;t intend.</li>
        <li>Don&apos;t pretend to be staff or the bot.</li>
      </List>
    ),
  },
  {
    id: "availability",
    title: "Availability",
    body: (
      <>
        <P>
          The bot and website are free and run on a privately hosted computer. They are provided <strong>&quot;as
          is&quot;</strong> and <strong>&quot;as available&quot;</strong>: there is no promise that they will always be
          online, work without errors, or keep any feature. They can be restarted, changed, paused or shut down at any
          time, sometimes without notice. Data such as playlists, wishlists or saved settings could be lost.
        </P>
        <P>
          You can check whether things are up on the <A to="/status">status page</A>.
        </P>
      </>
    ),
  },
  {
    id: "termination",
    title: "Ending your access",
    body: (
      <>
        <P>
          You can stop using the bot and website, and leave the server, whenever you like. Some records (for example
          moderation history) are kept after you leave; the <A to="/privacy">Privacy Policy</A> explains this.
        </P>
        <P>
          Staff can remove you from the server, or block your use of the bot or website, at any time, especially if you
          break these terms, Discord&apos;s rules or the server&apos;s rules, or to protect the community.
        </P>
      </>
    ),
  },
  {
    id: "liability",
    title: "Limitation of liability",
    body: (
      <>
        <P>In plain language, and as far as the law where you live allows:</P>
        <List>
          <li>The bot and website are a free community service, used at your own risk.</li>
          <li>
            We aren&apos;t responsible for losses caused by outages, bugs, lost data, mistakes by automated systems that
            were later corrected, or what other members post or play.
          </li>
          <li>
            We aren&apos;t responsible for services we don&apos;t run, such as Discord, YouTube, fortnite-api.com or
            Cloudflare (which carries the website&apos;s traffic). Their own terms apply to them.
          </li>
          <li>Nothing here takes away rights that the law says can&apos;t be taken away.</li>
        </List>
        <P>This project is not made by, endorsed by or affiliated with Discord, YouTube or Epic Games.</P>
      </>
    ),
  },
  {
    id: "privacy",
    title: "Privacy",
    body: (
      <P>
        How the bot and website collect, use and keep information is explained in the{" "}
        <A to="/privacy">Privacy Policy</A>.
      </P>
    ),
  },
  {
    id: "changes",
    title: "Changes to these terms",
    body: (
      <P>
        We may update these terms when the bot or website changes. The &quot;Last updated&quot; date at the top shows
        the latest version, and we&apos;ll try to mention bigger changes in the server. If you keep using the bot or
        website after a change, you accept the updated terms.
      </P>
    ),
  },
  {
    id: "contact",
    title: "Contact",
    body: (
      <>
        <P>Questions about these terms, a moderation decision or the bot:</P>
        <List>
          <li>open a ticket from a ticket panel in the Gamer Girls HQ server, or</li>
          <li>message the server owner on Discord.</li>
        </List>
        {CONTACT_EXTRA ? <P>{CONTACT_EXTRA}</P> : null}
        <P>
          Banned by the trap channel? Use the <A to="/appeal">ban appeal page</A> instead.
        </P>
      </>
    ),
  },
]
