import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { ResolvedUser } from "@/components/ResolvedUser"
import { readStorage, writeStorage } from "@/lib/storage"
import type { Sec } from "@/api/diagnostics"
import { fmtBytes, fmtDate, fmtEpoch, fmtMs, fmtNum, fmtPct, fmtUptime } from "@/pages/diagnostics/fmt"
import { Bar, Dict, KV, Mono, Pills, Pre, SortTable, Sub, Tile, Tiles, YesNo, type Col } from "@/pages/diagnostics/widgets"

type Row = Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any
const arr = (v: unknown): Row[] => (Array.isArray(v) ? (v as Row[]) : [])
const entries = (o: Record<string, unknown> | null | undefined) => Object.entries(o ?? {}).map(([k, v]) => ({ k, v: v as number }))

function CountTable({ title, data, label = "name" }: { title: string; data: Record<string, unknown> | undefined; label?: string }) {
  if (!data || (data as Row).error) {
    return (
      <Sub title={title}>
        <p className="text-sm text-muted-foreground">{(data as Row)?.error ?? "No data."}</p>
      </Sub>
    )
  }
  return (
    <Sub title={title}>
      <SortTable
        rows={entries(data)}
        initialSort="v"
        desc
        maxHeight="max-h-60"
        cols={[
          { key: "k", label, sort: (r) => r.k, render: (r) => <Mono>{r.k}</Mono> },
          { key: "v", label: "count", right: true, sort: (r) => r.v, render: (r) => fmtNum(r.v) },
        ]}
      />
    </Sub>
  )
}

function Cmd({ argv }: { argv: string[] | null | undefined }) {
  if (!argv?.length) return <span className="text-muted-foreground">n/a</span>
  return <span className="break-all font-mono text-[11px]">{argv.join(" ")}</span>
}

function SocketTable({ sockets }: { sockets: Row | undefined }) {
  if (!sockets) return null
  return (
    <Sub
      title={`Sockets (${sockets.total})`}
      right={<Pills items={Object.entries(sockets.by_state ?? {}).map(([k, v]) => `${k}: ${v}`)} empty="" />}
    >
      <SortTable
        rows={arr(sockets.rows)}
        empty="No inet sockets."
        maxHeight="max-h-60"
        initialSort="status"
        cols={[
          { key: "type", label: "Type", sort: (r) => r.type },
          { key: "local", label: "Local", sort: (r) => r.local, render: (r) => <Mono>{r.local}</Mono> },
          { key: "remote", label: "Remote", sort: (r) => r.remote, render: (r) => (r.remote ? <Mono>{r.remote}</Mono> : "-") },
          { key: "status", label: "State", sort: (r) => r.status },
        ]}
      />
    </Sub>
  )
}

// ---------------------------------------------------------------------------
// Process
// ---------------------------------------------------------------------------

export function ProcessBody({ s }: { s: Sec }) {
  const rt = s.runtime ?? {}
  const py = s.python ?? {}
  const asy = s.asyncio ?? {}
  const lag = asy.loop_lag_ms as number | undefined
  return (
    <>
      <Tiles>
        <Tile label="PID" value={s.pid} hint={`parent ${s.ppid ?? "n/a"}`} />
        <Tile label="Uptime" value={fmtUptime(s.uptime_seconds)} hint={fmtDate(s.started_at)} />
        <Tile label="CPU" value={fmtPct(s.cpu_percent)} hint="of whole machine" tone={s.cpu_percent > 80 ? "bad" : s.cpu_percent > 50 ? "warn" : undefined} />
        <Tile label="RSS" value={fmtBytes(s.rss_bytes)} hint={`VMS ${fmtBytes(s.vms_bytes)}`} />
        <Tile label="Threads" value={s.threads} hint={`python: ${py.threads_python ?? "n/a"}`} />
        <Tile label="Handles" value={fmtNum(s.handles)} hint={`${s.open_files?.total ?? 0} open files`} />
        <Tile label="Loop lag" value={fmtMs(lag)} tone={lag == null ? undefined : lag > 500 ? "bad" : lag > 100 ? "warn" : "ok"} hint={asy.loop} />
        <Tile label="Asyncio tasks" value={asy.tasks?.total} hint={`${asy.tasks?.pending ?? 0} pending`} />
        <Tile label="Sockets" value={s.sockets?.total} />
        <Tile label="Modules" value={rt.modules_loaded} hint={`${rt.top_level_packages ?? "?"} packages`} />
        <Tile label="Tracked objects" value={fmtNum(rt.gc?.tracked_objects)} />
        <Tile label="Private (USS)" value={fmtBytes(rt.memory_full?.uss)} />
      </Tiles>

      <Sub title="Details">
        <KV
          items={[
            ["Executable", <Mono key="e">{s.exe}</Mono>],
            ["Working directory", <Mono key="c">{s.cwd}</Mono>],
            ["Command line", <Cmd key="cl" argv={s.cmdline} />],
            ["User", s.username],
            ["Status / priority", `${s.status} / ${String(s.priority ?? "n/a")}`],
            ["Platform", s.platform],
            ["Python", `${py.implementation} ${py.version} (${py.compiler})`],
            ["Python executable", <Mono key="pe">{py.executable}</Mono>],
            ["Python prefix", <Mono key="pp">{py.prefix}</Mono>],
            ["Recursion limit / switch interval", `${py.recursion_limit} / ${py.switch_interval}s`],
            ["CPU time user / system", `${s.cpu_times?.user}s / ${s.cpu_times?.system}s`],
            ["I/O read / write", `${fmtBytes(s.io?.read_bytes)} (${fmtNum(s.io?.read_count)}) / ${fmtBytes(s.io?.write_bytes)} (${fmtNum(s.io?.write_count)})`],
            ["Context switches", `${fmtNum(s.ctx_switches?.voluntary)} vol / ${fmtNum(s.ctx_switches?.involuntary)} invol`],
            ["Working dir name", s.working_dir_name],
          ]}
        />
      </Sub>

      {rt.memory_full ? (
        <Sub title="Memory (full)">
          <Dict data={rt.memory_full} fmt={(k, v) => (typeof v === "number" && /(rss|vms|uss|wset|paged|pool|pagefile|private|swap|peak)/.test(k) ? fmtBytes(v) : String(v))} />
        </Sub>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-2">
        <Sub title={`Threads by CPU time (${arr(s.thread_rows).length})`}>
          <SortTable
            rows={arr(s.thread_rows)}
            initialSort="cpu"
            desc
            maxHeight="max-h-72"
            cols={[
              { key: "id", label: "Native id", sort: (r) => r.id },
              { key: "name", label: "Name", sort: (r) => r.name, render: (r) => r.name ?? "-" },
              { key: "cpu", label: "CPU s", right: true, sort: (r) => r.user_time + r.system_time, render: (r) => (r.user_time + r.system_time).toFixed(2) },
              { key: "user", label: "User", right: true, sort: (r) => r.user_time, render: (r) => r.user_time.toFixed(2) },
              { key: "sys", label: "System", right: true, sort: (r) => r.system_time, render: (r) => r.system_time.toFixed(2) },
            ]}
          />
        </Sub>
        <Sub title="Asyncio tasks by coroutine">
          <SortTable
            rows={arr(asy.tasks?.by_coroutine)}
            initialSort="count"
            desc
            maxHeight="max-h-72"
            cols={[
              { key: "coro", label: "Coroutine", sort: (r) => r.coro, render: (r) => <Mono>{r.coro}</Mono> },
              { key: "count", label: "Tasks", right: true, sort: (r) => r.count },
            ]}
          />
        </Sub>
      </div>

      <Sub title={`Asyncio task list (${arr(asy.tasks?.rows).length} shown of ${asy.tasks?.total ?? 0})`}>
        <SortTable
          rows={arr(asy.tasks?.rows)}
          initialSort="name"
          maxHeight="max-h-72"
          limit={15}
          cols={[
            { key: "name", label: "Task name", sort: (r) => r.name, render: (r) => <Mono>{r.name}</Mono> },
            { key: "coro", label: "Coroutine", sort: (r) => r.coro, render: (r) => <Mono>{r.coro}</Mono> },
            { key: "done", label: "Done", sort: (r) => r.done, render: (r) => (r.done ? "yes" : "no") },
          ]}
        />
      </Sub>

      <SocketTable sockets={s.sockets} />

      <Sub title={`Open files (${s.open_files?.total ?? 0})`}>
        <SortTable
          rows={arr(s.open_files?.rows)}
          empty="No open regular files reported."
          maxHeight="max-h-60"
          limit={20}
          initialSort="path"
          cols={[{ key: "path", label: "Path", sort: (r) => r.path, render: (r) => <Mono>{r.path}</Mono> }]}
        />
      </Sub>

      <div className="grid gap-5 lg:grid-cols-2">
        <Sub title="GC">
          <KV
            items={[
              ["Enabled", String(rt.gc?.enabled)],
              ["Generation counts", (rt.gc?.counts ?? []).join(" / ")],
              ["Thresholds", (rt.gc?.thresholds ?? []).join(" / ")],
              ...arr(rt.gc?.stats).map((g, i) => [`Gen ${i}`, `${fmtNum(g.collections)} collections, ${fmtNum(g.collected)} collected, ${fmtNum(g.uncollectable)} uncollectable`] as [string, ReactNode]),
            ]}
          />
        </Sub>
        <Sub title="Top object types">
          <SortTable
            rows={arr(rt.gc?.top_types)}
            initialSort="count"
            desc
            maxHeight="max-h-60"
            cols={[
              { key: "type", label: "Type", sort: (r) => r.type, render: (r) => <Mono>{r.type}</Mono> },
              { key: "count", label: "Objects", right: true, sort: (r) => r.count, render: (r) => fmtNum(r.count) },
            ]}
          />
        </Sub>
      </div>

      <Sub title="sys.path">
        <Pre lines={py.path ?? []} max="max-h-40" />
      </Sub>
    </>
  )
}

// ---------------------------------------------------------------------------
// Children
// ---------------------------------------------------------------------------

export function ChildrenBody({ s }: { s: Sec }) {
  const rows = arr(s.processes)
  const cols: Col<Row>[] = [
    { key: "role", label: "Role", sort: (r) => r.role, render: (r) => <Badge variant="info">{r.role}</Badge> },
    { key: "origin", label: "Origin", sort: (r) => r.origin, render: (r) => (r.origin === "adopted external" ? <Badge variant="warning">adopted external</Badge> : r.origin) },
    { key: "pid", label: "PID", sort: (r) => r.pid },
    { key: "ppid", label: "PPID", sort: (r) => r.ppid },
    { key: "name", label: "Name", sort: (r) => r.name, render: (r) => r.error ?? r.name },
    { key: "status", label: "Status", sort: (r) => r.status },
    { key: "cpu", label: "CPU", right: true, sort: (r) => r.cpu_percent, render: (r) => fmtPct(r.cpu_percent) },
    { key: "rss", label: "RSS", right: true, sort: (r) => r.rss_bytes, render: (r) => fmtBytes(r.rss_bytes) },
    { key: "threads", label: "Thr", right: true, sort: (r) => r.threads },
    { key: "up", label: "Uptime", right: true, sort: (r) => r.uptime_seconds, render: (r) => fmtUptime(r.uptime_seconds) },
    { key: "listen", label: "Listening", render: (r) => (arr(r.listening).length ? (r.listening as string[]).join(", ") : "-") },
    { key: "cmd", label: "Command line", render: (r) => <Cmd argv={r.cmdline} />, className: "min-w-[20rem]" },
  ]
  return (
    <>
      <p className="text-sm text-muted-foreground">
        {rows.length} process(es) belonging to the bot stack: the bot and its children, the pot server (spawned or adopted external), the dashboard and our Cloudflare tunnel. Nothing else on the machine is listed. Secret-looking arguments are masked.
      </p>
      <SortTable rows={rows} cols={cols} initialSort="origin" empty="No stack processes found." maxHeight="max-h-[28rem]" />
    </>
  )
}

// ---------------------------------------------------------------------------
// System
// ---------------------------------------------------------------------------

export function SystemBody({ s, netRate }: { s: Sec; netRate?: { up: number; down: number } | null }) {
  const mem = s.memory ?? {}
  const swap = s.swap ?? {}
  const cpu = s.cpu ?? {}
  const net = s.network ?? {}
  return (
    <>
      <Tiles>
        <Tile label="Host" value={s.hostname} hint={s.os} />
        <Tile label="Host uptime" value={fmtUptime(s.host_uptime_seconds)} hint={fmtDate(s.boot_time)} />
        <Tile label="RAM used" value={fmtPct(mem.percent)} tone={mem.percent >= 95 ? "bad" : mem.percent >= 90 ? "warn" : undefined} hint={`${fmtBytes(mem.available)} free of ${fmtBytes(mem.total)}`} />
        <Tile label="Swap / pagefile" value={fmtPct(swap.percent)} hint={`${fmtBytes(swap.used)} of ${fmtBytes(swap.total)}`} />
        <Tile label="CPU (system)" value={fmtPct(cpu.system_percent)} hint={`${cpu.logical} logical / ${cpu.physical} physical`} />
        <Tile label="Discord latency" value={fmtMs(s.discord_latency_ms)} hint={`shard ${s.shards?.shard_id ?? 0} of ${s.shards?.shard_count}`} />
        <Tile label="Net up" value={netRate ? `${fmtBytes(netRate.up)}/s` : "n/a"} hint={`total ${fmtBytes(s.net_io?.bytes_sent)}`} />
        <Tile label="Net down" value={netRate ? `${fmtBytes(netRate.down)}/s` : "n/a"} hint={`total ${fmtBytes(s.net_io?.bytes_recv)}`} />
        <Tile label="Connections" value={fmtNum(net.connections_total)} hint="bot stack only" />
        <Tile label="Load avg" value={(cpu.load_avg ?? []).join(" / ") || "n/a"} />
        <Tile label="CPU freq" value={cpu.freq_mhz ? `${Math.round(cpu.freq_mhz.current)} MHz` : "n/a"} hint={cpu.freq_mhz ? `max ${Math.round(cpu.freq_mhz.max)}` : undefined} />
        <Tile label="Disk I/O" value={s.disk_io ? fmtBytes(s.disk_io.read_bytes + s.disk_io.write_bytes) : "n/a"} hint={s.disk_io ? `R ${fmtBytes(s.disk_io.read_bytes)} / W ${fmtBytes(s.disk_io.write_bytes)}` : undefined} />
      </Tiles>

      <Sub title="CPU per core">
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3 lg:grid-cols-4">
          {(cpu.per_core_percent ?? []).map((p: number, i: number) => (
            <div key={i} className="flex flex-col gap-1">
              <div className="flex justify-between text-xs">
                <span className="text-muted-foreground">core {i}</span>
                <span className="tabular-nums">{fmtPct(p, 0)}</span>
              </div>
              <Bar percent={p} />
            </div>
          ))}
        </div>
      </Sub>

      <Sub title="Disks">
        <SortTable
          rows={arr(s.disks)}
          initialSort="mount"
          maxHeight="max-h-60"
          cols={[
            { key: "mount", label: "Mount", sort: (r) => r.mount, render: (r) => <Mono>{r.mount ?? r.device}</Mono> },
            { key: "fstype", label: "FS", sort: (r) => r.fstype },
            { key: "total", label: "Total", right: true, sort: (r) => r.total_bytes, render: (r) => fmtBytes(r.total_bytes) },
            { key: "free", label: "Free", right: true, sort: (r) => r.free_bytes, render: (r) => fmtBytes(r.free_bytes) },
            { key: "pct", label: "Used", sort: (r) => r.percent, render: (r) => (r.error ? r.error : <div className="flex min-w-32 items-center gap-2"><Bar percent={r.percent} warnAt={85} badAt={95} /><span className="text-xs tabular-nums">{fmtPct(r.percent, 0)}</span></div>) },
          ]}
        />
        {s.db_disk && !s.db_disk.error ? (
          <p className="text-xs text-muted-foreground">
            Database drive: <Mono>{s.db_disk.path}</Mono>, {fmtBytes(s.db_disk.free_bytes)} free
          </p>
        ) : null}
      </Sub>

      <div className="grid gap-5 lg:grid-cols-2">
        <Sub title="Network I/O (totals since boot)">
          <Dict data={s.net_io} fmt={(k, v) => (k.startsWith("bytes") ? fmtBytes(v as number) : fmtNum(v as number))} />
        </Sub>
        <Sub title="Stack connections by state">
          <Dict data={net.connections_by_state} />
        </Sub>
      </div>

      <Sub title={`Stack listening ports (${arr(net.listening).length})`}>
        <SortTable
          rows={arr(net.listening)}
          initialSort="local"
          maxHeight="max-h-72"
          limit={20}
          cols={[
            { key: "role", label: "Role", sort: (r) => r.role, render: (r) => <Badge variant="info">{r.role}</Badge> },
            { key: "local", label: "Address", sort: (r) => r.local, render: (r) => <Mono>{r.local}</Mono> },
            { key: "pid", label: "PID", sort: (r) => r.pid },
            { key: "process", label: "Process", sort: (r) => r.process },
          ]}
        />
      </Sub>

      {s.temperatures ? (
        <Sub title="Temperatures">
          <Dict data={Object.fromEntries(Object.entries(s.temperatures as Row).map(([k, v]) => [k, (v as Row[]).map((t) => `${t.label || "?"}: ${t.current}C`).join(", ")]))} />
        </Sub>
      ) : (
        <p className="text-xs text-muted-foreground">Temperature sensors are not exposed on this platform.</p>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Discord
// ---------------------------------------------------------------------------

export function DiscordBody({ s }: { s: Sec }) {
  const g = s.guild ?? {}
  const p = s.permissions ?? {}
  const sc = s.slash_commands ?? {}
  const required = Object.entries(p.required ?? {}).map(([k, v]) => ({ k, v: v as boolean }))
  return (
    <>
      <Tiles>
        <Tile label="Bot user" value={s.bot_user?.name ?? "n/a"} hint={s.bot_user?.id} />
        <Tile label="Gateway" value={s.ready ? "ready" : "not ready"} tone={s.ready ? "ok" : "bad"} hint={s.closed ? "closed" : "open"} />
        <Tile label="Latency" value={fmtMs(s.latency_ms)} tone={s.latency_ms > 1000 ? "bad" : s.latency_ms > 300 ? "warn" : "ok"} />
        <Tile label="Guilds" value={s.guild_count} />
        <Tile label="Cogs" value={arr(s.cogs).length} hint={`${arr(s.extensions as unknown as Row[]).length} extensions`} />
        <Tile label="Slash commands" value={sc.global_total ?? "n/a"} hint={`${sc.global_top_level ?? 0} top level`} />
        <Tile label="Cached users" value={fmtNum(s.cache?.users)} />
        <Tile label="Cached messages" value={fmtNum(s.cache?.messages)} />
        <Tile label="Voice clients" value={s.cache?.voice_clients} />
        <Tile label="Session id" value={s.gateway?.session_id_present ? "present" : "none"} hint={`seq ${s.gateway?.sequence ?? "n/a"}`} />
      </Tiles>

      <Sub title="Guild">
        {g.error ? (
          <p className="text-sm text-danger">{g.error}</p>
        ) : (
          <KV
            items={[
              ["Name", g.name],
              ["ID", g.id],
              ["Owner", g.owner_id],
              ["Members (count / cached)", `${fmtNum(g.members)} / ${fmtNum(g.members_cached)}`],
              ["Channels", `${g.channels} (${Object.entries(g.channel_breakdown ?? {}).map(([k, v]) => `${v} ${k}`).join(", ")})`],
              ["Roles / emojis", `${g.roles} / ${g.emojis}`],
              ["Boost tier / boosts", `${g.boost_tier} / ${g.boosts}`],
              ["Members chunked", String(g.chunked)],
            ]}
          />
        )}
      </Sub>

      <Sub title="Bot permissions vs what features need">
        {p.error ? (
          <p className="text-sm text-danger">{p.error}</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              {(p.missing ?? []).length ? (
                <Badge variant="destructive">Missing: {p.missing.join(", ")}</Badge>
              ) : (
                <Badge variant="success">No required permission is missing</Badge>
              )}
              <span className="text-muted-foreground">
                Top role <Mono>{p.top_role?.name}</Mono> at position {p.top_role?.position} of {p.top_role?.of_roles}
                {p.administrator ? " (Administrator)" : ""}
              </span>
            </div>
            <SortTable
              rows={required}
              maxHeight="max-h-60"
              cols={[
                { key: "k", label: "Permission", sort: (r) => r.k },
                { key: "v", label: "Held", sort: (r) => r.v, render: (r) => <YesNo value={r.v || p.administrator} /> },
              ]}
            />
            <details className="text-xs text-muted-foreground">
              <summary className="cursor-pointer">All guild permissions held ({(p.all_granted ?? []).length})</summary>
              <div className="mt-2">
                <Pills items={p.all_granted ?? []} />
              </div>
            </details>
          </>
        )}
      </Sub>

      <Sub title="Intents enabled">
        <Pills items={s.intents ?? []} />
      </Sub>

      <Sub title={`Cogs (${arr(s.cogs).length})`}>
        <SortTable
          rows={arr(s.cogs)}
          initialSort="name"
          maxHeight="max-h-80"
          cols={[
            { key: "name", label: "Cog", sort: (r) => r.name, render: (r) => r.error ? `${r.name}: ${r.error}` : r.name },
            { key: "commands", label: "Prefix cmds", right: true, sort: (r) => r.commands },
            { key: "app", label: "Slash cmds", right: true, sort: (r) => r.app_commands },
            { key: "listeners", label: "Listeners", right: true, sort: (r) => r.listeners },
            { key: "loops", label: "Loops", right: true, sort: (r) => r.loops },
          ]}
        />
      </Sub>

      <Sub title="Slash commands">
        <KV
          items={[
            ["Global (top level / total)", `${sc.global_top_level} / ${sc.global_total}`],
            ["Guild-specific", String(sc.guild_specific ?? "n/a")],
            ["Last sync", "not tracked by discord.py"],
          ]}
        />
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">All command names</summary>
          <div className="mt-2">
            <Pills items={sc.names ?? []} />
          </div>
        </details>
      </Sub>

      {arr(s.voice).length ? (
        <Sub title="Voice connections">
          <SortTable
            rows={arr(s.voice)}
            cols={[
              { key: "guild_id", label: "Guild" },
              { key: "channel", label: "Channel" },
              { key: "connected", label: "Connected", render: (r) => <YesNo value={r.connected} /> },
              { key: "playing", label: "Playing", render: (r) => (r.playing ? "yes" : "no") },
              { key: "latency", label: "Latency", right: true, sort: (r) => r.latency_ms, render: (r) => fmtMs(r.latency_ms) },
            ]}
          />
        </Sub>
      ) : null}

      <Sub title="Event listeners registered on the bot">
        <SortTable
          rows={entries(s.event_listeners)}
          initialSort="v"
          desc
          maxHeight="max-h-60"
          limit={12}
          cols={[
            { key: "k", label: "Event", sort: (r) => r.k, render: (r) => <Mono>{r.k}</Mono> },
            { key: "v", label: "Listeners", right: true, sort: (r) => r.v },
          ]}
        />
      </Sub>
      <Sub title="Loaded extensions">
        <Pills items={(s.extensions as unknown as string[]) ?? []} />
      </Sub>
    </>
  )
}

// ---------------------------------------------------------------------------
// Database
// ---------------------------------------------------------------------------

export function DatabaseBody({ s }: { s: Sec }) {
  const mig = s.migrations ?? {}
  const tables = entries(s.tables).map((t) => ({ name: t.k, count: t.v as unknown }))
  return (
    <>
      <Tiles>
        <Tile label="DB size" value={fmtBytes(s.size_bytes)} hint={fmtDate(s.modified_at)} />
        <Tile label="WAL size" value={fmtBytes(s.wal_bytes)} tone={s.wal_bytes > 100 * 1024 ** 2 ? "warn" : undefined} hint={`SHM ${fmtBytes(s.shm_bytes)}`} />
        <Tile label="Quick check" value={String(s.quick_check ?? "n/a")} tone={s.quick_check === "ok" ? "ok" : s.quick_check ? "bad" : undefined} />
        <Tile label="Migrations" value={`v${mig.latest_version ?? "?"}`} hint={`${mig.applied_count ?? 0} applied`} />
        <Tile label="Tables / indexes" value={`${s.table_count ?? "?"} / ${s.index_count ?? "?"}`} />
        <Tile label="Page size" value={fmtBytes(s.page_size)} hint={`${fmtNum(s.page_count)} pages`} />
        <Tile label="Free pages" value={fmtNum(s.freelist_count)} hint={s.freelist_count ? fmtBytes(s.freelist_count * s.page_size) + " reclaimable" : undefined} />
        <Tile label="Journal mode" value={String(s.journal_mode ?? "n/a")} />
        <Tile label="Heavy query time" value={fmtMs(s.heavy_query_ms)} hint={s.reader} />
        <Tile label="Last audit entry" value={typeof s.last_audit_log_at === "string" ? fmtDate(s.last_audit_log_at) : "n/a"} />
      </Tiles>
      <KV items={[["File", <Mono key="f">{s.path}</Mono>], ["Latest migration", mig.latest_name]]} />

      <Sub title={`Row counts (all ${tables.length} tables)`}>
        <SortTable
          rows={tables}
          initialSort="count"
          desc
          maxHeight="max-h-96"
          cols={[
            { key: "name", label: "Table", sort: (r) => r.name, render: (r) => <Mono>{r.name}</Mono> },
            { key: "count", label: "Rows", right: true, sort: (r) => (typeof r.count === "number" ? r.count : -1), render: (r) => (typeof r.count === "number" ? fmtNum(r.count) : String((r.count as Row)?.error ?? "?")) },
          ]}
        />
      </Sub>

      <div className="grid gap-5 lg:grid-cols-2">
        <CountTable title="Audit log actions, last 24h" data={s.audit_actions_24h} label="action" />
        <CountTable title="Audit log actions, last 7d" data={s.audit_actions_7d} label="action" />
        <CountTable title="Moderation actions, all time" data={s.mod_actions_by_type} label="type" />
        <CountTable title="Moderation actions, last 7d" data={s.mod_actions_7d} label="type" />
        <CountTable title="Tickets by status" data={s.tickets_by_status} label="status" />
      </div>

      <Sub title="Applied migrations">
        <SortTable
          rows={arr(mig.applied)}
          initialSort="version"
          desc
          maxHeight="max-h-60"
          limit={8}
          cols={[
            { key: "version", label: "Version", sort: (r) => r.version },
            { key: "name", label: "Name", sort: (r) => r.name },
            { key: "applied_at", label: "Applied", sort: (r) => r.applied_at, render: (r) => fmtDate(r.applied_at) },
          ]}
        />
      </Sub>
    </>
  )
}

// ---------------------------------------------------------------------------
// Services
// ---------------------------------------------------------------------------

export function ServicesBody({ s }: { s: Sec }) {
  const api = s.internal_api ?? {}
  const pot = s.pot_server ?? {}
  const hp = s.honeypot ?? {}
  const loops = arr(s.background_loops)
  return (
    <>
      <Sub title="Internal API">
        <KV
          items={[
            ["Status", <Badge key="s" variant="success">{api.status}</Badge>],
            ["Bind", `${api.host}:${api.port}`],
            ["Shared secret configured", <YesNo key="sc" value={api.secret_configured} />],
            ["Routes", String(arr(api.routes).length)],
          ]}
        />
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">Route list</summary>
          <div className="mt-2">
            <SortTable
              rows={arr(api.routes)}
              initialSort="path"
              maxHeight="max-h-72"
              cols={[
                { key: "path", label: "Path", sort: (r) => r.path, render: (r) => <Mono>{r.path}</Mono> },
                { key: "methods", label: "Methods", render: (r) => (r.methods as string[]).join(", ") },
              ]}
            />
          </div>
        </details>
      </Sub>

      <Sub title="PO-token (pot) server">
        {pot.error ? (
          <p className="text-sm text-danger">{pot.error}</p>
        ) : pot.available === false ? (
          <p className="text-sm text-muted-foreground">bot.pot_server_service is not attached.</p>
        ) : (
          <>
            <Tiles>
              <Tile label="State" value={pot.state} tone={pot.state === "running" ? "ok" : pot.enabled ? "bad" : undefined} hint={pot.enabled ? "enabled" : "disabled"} />
              <Tile label="PID" value={pot.pid ?? "n/a"} hint={pot.adopted_external ? "adopted external" : "owned by bot"} />
              <Tile label="CPU / RSS" value={pot.cpu_percent != null ? `${fmtPct(pot.cpu_percent)} / ${fmtBytes(pot.rss_bytes)}` : "n/a"} />
              <Tile label="Port" value={pot.port} hint={pot.host} />
              <Tile label="Uptime" value={fmtUptime(pot.uptime_seconds)} />
              <Tile label="Restarts" value={pot.restart_count} tone={pot.restart_count > 3 ? "warn" : undefined} />
              <Tile label="Watchdog" value={pot.watchdog_active ? "active" : pot.watchdog ? "idle" : "off"} />
            </Tiles>
            <KV items={[["URL", pot.url], ["Log file", <Mono key="l">{pot.log_path}</Mono>], ["Last error", pot.last_error ?? "none"]]} />
            <Sub title={`Log tail (${(pot.log_tail ?? []).length} lines)`}>
              <Pre lines={pot.log_tail ?? []} max="max-h-96" />
            </Sub>
          </>
        )}
      </Sub>

      <Sub title={`Background task loops (${loops.length})`}>
        <SortTable
          rows={loops}
          initialSort="cog"
          cols={[
            { key: "cog", label: "Cog", sort: (r) => r.cog },
            { key: "name", label: "Loop", sort: (r) => r.name, render: (r) => <Mono>{r.name}</Mono> },
            { key: "running", label: "Running", sort: (r) => r.running, render: (r) => (r.error ? r.error : <YesNo value={r.running} yes="running" no="stopped" />) },
            { key: "failed", label: "Failed", sort: (r) => r.failed, render: (r) => (r.failed ? <Badge variant="destructive">failed</Badge> : "no") },
            { key: "interval", label: "Interval", right: true, sort: (r) => r.interval_seconds, render: (r) => (r.interval_seconds ? fmtUptime(r.interval_seconds) : "n/a") },
            { key: "iterations", label: "Iterations", right: true, sort: (r) => r.iterations },
            { key: "next", label: "Next run", sort: (r) => r.next_run, render: (r) => (r.next_run ? fmtDate(r.next_run) : "n/a") },
          ]}
        />
      </Sub>

      <Sub title="Honeypot">
        {hp.error ? (
          <p className="text-sm text-danger">{hp.error}</p>
        ) : hp.available === false ? (
          <p className="text-sm text-muted-foreground">Honeypot cog is not loaded.</p>
        ) : (
          <>
            <Tiles>
              <Tile label="Configured" value={String(hp.configured)} />
              <Tile label="Enabled" value={hp.enabled ? "yes" : "no"} tone={hp.enabled ? "ok" : "warn"} />
              <Tile label="Action" value={hp.action} />
              <Tile label="Channel exists" value={String(hp.channel_exists)} tone={hp.channel_exists ? "ok" : "warn"} />
              <Tile label="Total catches" value={fmtNum(hp.total_catches)} />
            </Tiles>
            {hp.stats && !hp.stats.error ? <Dict data={hp.stats} fmt={(k, v) => (k === "as_of" ? fmtDate(v as string) : String(v))} /> : null}
          </>
        )}
      </Sub>
    </>
  )
}

// ---------------------------------------------------------------------------
// Cookies
// ---------------------------------------------------------------------------

export function CookiesBody({ s }: { s: Sec }) {
  if (!s.configured) return <p className="text-sm text-muted-foreground">No cookies file is configured (MUSIC_COOKIES_FILE).</p>
  if (!s.present) return <p className="text-sm text-warning">Configured cookies file is missing: <Mono>{s.path}</Mono></p>
  const cookies = arr(s.cookies)
  return (
    <>
      <Tiles>
        <Tile label="Cookies" value={s.cookie_count} hint={`${s.domain_count} domains`} />
        <Tile label="File age" value={`${((s.age_hours ?? 0) / 24).toFixed(1)}d`} tone={(s.age_hours ?? 0) > 336 ? "warn" : "ok"} hint={fmtDate(s.modified_at)} />
        <Tile label="Size" value={fmtBytes(s.size_bytes)} />
        <Tile label="Expired" value={s.expired_count} tone={s.expired_count ? "warn" : "ok"} hint={`${s.session_cookies} session cookies`} />
        <Tile label="Key auth cookies" value={s.key_auth_expired ? "EXPIRED" : s.key_auth_missing ? "missing" : "valid"} tone={s.key_auth_expired ? "bad" : s.key_auth_missing ? "warn" : "ok"} hint={`${arr(s.key_auth_cookies).length} present`} />
        <Tile label="Earliest expiry" value={fmtDate(s.earliest_expiry)} hint={s.earliest_expiry_cookie} />
        <Tile label="Latest expiry" value={fmtDate(s.latest_expiry)} hint={s.latest_expiry_cookie} />
      </Tiles>
      <KV items={[["File", <Mono key="p">{s.path}</Mono>], ["SHA-256", <Mono key="h">{s.sha256}</Mono>]]} />
      <p className="text-xs text-muted-foreground">Cookie values are never shown: only length and the first two characters.</p>

      <Sub title="Cookies per domain">
        <SortTable
          rows={entries(s.per_domain)}
          initialSort="v"
          desc
          maxHeight="max-h-60"
          limit={10}
          cols={[
            { key: "k", label: "Domain", sort: (r) => r.k, render: (r) => <Mono>{r.k}</Mono> },
            { key: "v", label: "Cookies", right: true, sort: (r) => r.v },
          ]}
        />
      </Sub>

      <Sub title={`All cookies (${cookies.length}${s.truncated ? ", truncated" : ""})`}>
        <SortTable
          rows={cookies}
          initialSort="name"
          maxHeight="max-h-[28rem]"
          limit={25}
          cols={[
            { key: "name", label: "Name", sort: (r) => r.name, render: (r) => <span className="inline-flex items-center gap-1"><Mono>{r.name}</Mono>{r.key_auth ? <Badge variant="info">auth</Badge> : null}</span> },
            { key: "domain", label: "Domain", sort: (r) => r.domain },
            { key: "path", label: "Path", sort: (r) => r.path },
            { key: "secure", label: "Secure", sort: (r) => r.secure, render: (r) => (r.secure ? "yes" : "no") },
            { key: "http_only", label: "HttpOnly", sort: (r) => r.http_only, render: (r) => (r.http_only ? "yes" : "no") },
            { key: "expires", label: "Expires", sort: (r) => r.expires_at, render: (r) => (r.session ? "session" : fmtDate(r.expires_at)) },
            { key: "expired", label: "State", sort: (r) => r.expired, render: (r) => (r.expired ? <Badge variant="destructive">expired</Badge> : <Badge variant="success">valid</Badge>) },
            { key: "len", label: "Value", right: true, sort: (r) => r.value_len, render: (r) => <Mono>{r.value_preview}</Mono> },
          ]}
        />
      </Sub>
    </>
  )
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export function ConfigBody({ s }: { s: Sec }) {
  const env = entries(s.env as Record<string, unknown>).map((e) => ({ name: e.k, value: e.v as unknown }))
  const versions = entries(s.versions as Record<string, unknown>).map((e) => ({ name: e.k, value: e.v as unknown }))
  return (
    <>
      <Sub title="Bot environment (allowlisted names; secrets show only set/unset)">
        <SortTable
          rows={env}
          initialSort="name"
          maxHeight="max-h-96"
          cols={[
            { key: "name", label: "Variable", sort: (r) => r.name, render: (r) => <Mono>{r.name}</Mono> },
            {
              key: "value",
              label: "Value",
              sort: (r) => String(r.value),
              render: (r) =>
                r.value === "unset" ? (
                  <span className="text-muted-foreground">unset</span>
                ) : typeof r.value === "boolean" ? (
                  <Badge variant={r.value ? "success" : "outline"}>{String(r.value)}</Badge>
                ) : r.value === "set" && /(TOKEN|SECRET)/.test(r.name) ? (
                  <Badge variant="info">set (hidden)</Badge>
                ) : (
                  <Mono>{String(r.value)}</Mono>
                ),
            },
          ]}
        />
      </Sub>
      <Sub title="Versions">
        <SortTable
          rows={versions}
          initialSort="name"
          maxHeight="max-h-80"
          cols={[
            { key: "name", label: "Package", sort: (r) => r.name },
            { key: "value", label: "Version", sort: (r) => String(r.value), render: (r) => (r.value ? <Mono>{String(r.value)}</Mono> : <span className="text-muted-foreground">not installed</span>) },
          ]}
        />
      </Sub>
      <KV items={[["Project root", <Mono key="r">{s.project_root}</Mono>]]} />
    </>
  )
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

const LOG_COLLAPSE_KEY = "kb-diag-logs-collapsed"

/** Per-viewer collapsed log panels (keys of `files[].key`), in localStorage. */
function useCollapsedLogs(): [Set<string>, (key: string) => void] {
  const [set, setSet] = useState<Set<string>>(() => {
    try {
      const parsed: unknown = JSON.parse(readStorage(LOG_COLLAPSE_KEY) ?? "[]")
      return new Set(Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : [])
    } catch {
      return new Set()
    }
  })
  const toggle = useCallback((key: string) => {
    setSet((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      writeStorage(LOG_COLLAPSE_KEY, JSON.stringify([...next]))
      return next
    })
  }, [])
  return [set, toggle]
}

/** Log tail that sticks to the bottom as lines arrive, unless the viewer scrolled up. */
function LogTail({ lines }: { lines: string[] }) {
  const ref = useRef<HTMLPreElement>(null)
  const stick = useRef(true)
  const text = lines.join("\n")
  useLayoutEffect(() => {
    const el = ref.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [text])
  if (!lines.length) return <p className="text-sm text-muted-foreground">Empty.</p>
  return (
    <pre
      ref={ref}
      tabIndex={0}
      aria-label="Log tail"
      onScroll={(e) => {
        const el = e.currentTarget
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
      }}
      className="max-h-72 overflow-auto rounded-lg border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed focus-visible:outline-2 focus-visible:outline-ring"
    >
      {text}
    </pre>
  )
}

export function LogsBody({ s }: { s: Sec }) {
  const [collapsedLogs, toggleLog] = useCollapsedLogs()
  return (
    <>
      {arr(s.files).map((f) => {
        const id = String(f.key ?? f.name)
        const collapsed = collapsedLogs.has(id)
        const bodyId = `log-body-${id}`
        const errors = (f.level_counts?.ERROR ?? 0) + (f.level_counts?.CRITICAL ?? 0)
        return (
          <div key={id} className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="min-w-0">
                <button
                  type="button"
                  onClick={() => toggleLog(id)}
                  aria-expanded={!collapsed}
                  aria-controls={bodyId}
                  aria-label={`${f.label ?? f.name} (${f.name}), ${collapsed ? "expand" : "minimize"}`}
                  title={collapsed ? "Expand this log" : "Minimize this log"}
                  className="-m-1 flex min-w-0 items-center gap-1.5 rounded-md p-1 text-left select-none hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring"
                >
                  {collapsed ? <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden /> : <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />}
                  <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{f.label ?? f.name}</span>
                  {f.label ? <Mono className="text-2xs">{f.name}</Mono> : null}
                </button>
              </h3>
              <div className="flex flex-wrap items-center gap-1.5">
                {f.exists ? (
                  <>
                    <span className="text-xs text-muted-foreground tabular-nums">
                      {fmtBytes(f.size_bytes)} · {fmtUptime(f.age_seconds)} ago
                    </span>
                    <Badge variant={f.level_counts?.WARNING ? "warning" : "outline"}>{f.level_counts?.WARNING ?? 0} warn</Badge>
                    <Badge variant={errors ? "destructive" : "outline"}>{errors} error</Badge>
                  </>
                ) : (
                  <Badge variant="outline">missing</Badge>
                )}
              </div>
            </div>
            {collapsed ? null : (
              <div id={bodyId} className="flex flex-col gap-2">
                {f.exists ? (
                  <>
                    <KV items={[["Path", <Mono key="p">{f.path}</Mono>], ["Modified", `${fmtDate(f.modified_at)} (${fmtUptime(f.age_seconds)} ago)`], ["Lines scanned", fmtNum(f.lines_scanned)]]} />
                    <LogTail lines={f.tail ?? []} />
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">Not found at <Mono>{f.path}</Mono>.</p>
                )}
              </div>
            )}
          </div>
        )
      })}
      {arr(s.other_files).length ? (
        <Sub title={`Other files in ${s.logs_dir}`}>
          <SortTable rows={arr(s.other_files)} initialSort="name" maxHeight="max-h-48" cols={[{ key: "name", label: "File", sort: (r) => r.name }, { key: "size", label: "Size", right: true, sort: (r) => r.size_bytes, render: (r) => fmtBytes(r.size_bytes) }, { key: "mod", label: "Modified", render: (r) => fmtDate(r.modified_at) }]} />
        </Sub>
      ) : null}
      <p className="text-xs text-muted-foreground">{s.note}</p>
    </>
  )
}

// ---------------------------------------------------------------------------
// Activity (collector cog)
// ---------------------------------------------------------------------------

function pairs(v: unknown): Array<{ name: string; n: number }> {
  return (Array.isArray(v) ? v : []).map((p: [string, number]) => ({ name: p[0], n: p[1] }))
}

export function ActivityBody({ s }: { s: Sec }) {
  if (s.available === false) return <p className="text-sm text-muted-foreground">{s.note}</p>
  const c = s.commands ?? {}
  const lag = s.loop_lag_stats
  const cols: Col<{ name: string; n: number }>[] = [
    { key: "name", label: "Command", sort: (r) => r.name, render: (r) => <Mono>{r.name}</Mono> },
    { key: "n", label: "Count", right: true, sort: (r) => r.n },
  ]
  return (
    <>
      <Tiles>
        <Tile label="Tracking for" value={fmtUptime(s.uptime_seconds)} hint="since collector loaded" />
        <Tile label="Slash commands run" value={fmtNum(c.slash_ok_total)} />
        <Tile label="Prefix commands run" value={fmtNum(c.prefix_ok_total)} />
        <Tile label="Prefix errors" value={fmtNum(c.prefix_error_total)} tone={c.prefix_error_total ? "warn" : "ok"} />
        <Tile label="Loop lag avg / p95" value={lag ? `${lag.avg} / ${lag.p95} ms` : "n/a"} hint={lag ? `max ${lag.max} ms over ${lag.samples} samples` : undefined} />
        <Tile label="Gateway resumes" value={s.gateway?.counts?.resumed ?? 0} hint={`${s.gateway?.counts?.disconnect ?? 0} disconnects`} />
      </Tiles>
      <div className="grid gap-5 lg:grid-cols-2">
        <Sub title="Top slash commands"><SortTable rows={pairs(c.top_slash)} cols={cols} initialSort="n" desc empty="No slash commands run since start." maxHeight="max-h-60" /></Sub>
        <Sub title="Top prefix commands"><SortTable rows={pairs(c.top_prefix)} cols={cols} initialSort="n" desc empty="No prefix commands run since start." maxHeight="max-h-60" /></Sub>
        <Sub title="Errors by command"><SortTable rows={pairs(c.errors_by_command)} cols={cols} initialSort="n" desc empty="No prefix-command errors." maxHeight="max-h-60" /></Sub>
        <Sub title="Interactions by type"><Dict data={s.interactions} /></Sub>
        <Sub title="Gateway events"><Dict data={s.gateway?.counts} /></Sub>
        <Sub title="Recent command errors (type only)">
          <SortTable rows={arr(c.recent_errors)} empty="None." maxHeight="max-h-60" cols={[{ key: "t", label: "When", render: (r) => fmtEpoch(r.t) }, { key: "command", label: "Command" }, { key: "error", label: "Error type" }]} />
        </Sub>
      </div>
      <p className="text-xs text-muted-foreground">Slash-command errors are not counted: discord.py does not dispatch an event for them.</p>
    </>
  )
}

// ---------------------------------------------------------------------------
// Dashboard process
// ---------------------------------------------------------------------------

export function DashboardBody({ s, botReachable }: { s: Sec; botReachable: boolean }) {
  const p = s.process ?? {}
  const ses = s.sessions ?? {}
  const rq = s.requests ?? {}
  const st = s.stack ?? {}
  const db = s.database ?? {}
  const ia = s.internal_api ?? {}
  return (
    <>
      <Tiles>
        <Tile label="PID" value={p.pid ?? "n/a"} hint={p.error ?? `parent ${p.ppid}`} />
        <Tile label="Uptime" value={fmtUptime(p.uptime_seconds)} />
        <Tile label="CPU" value={fmtPct(p.cpu_percent)} />
        <Tile label="RSS" value={fmtBytes(p.rss_bytes)} hint={`VMS ${fmtBytes(p.vms_bytes)}`} />
        <Tile label="Threads / handles" value={`${p.threads ?? "?"} / ${p.handles ?? "?"}`} />
        <Tile label="Bot round trip" value={botReachable ? fmtMs(ia.round_trip_ms) : "unreachable"} tone={botReachable ? undefined : "bad"} hint={ia.base_url} />
        <Tile label="Sessions" value={fmtNum(ses.total)} hint={`${fmtNum(ses.active_24h)} active in 24h`} />
        <Tile label="dashboard.db" value={fmtBytes(db.size_bytes)} hint={`WAL ${fmtBytes(db.wal_bytes)}`} />
        <Tile label="Requests served" value={fmtNum(rq.total_requests)} hint={`${rq.in_flight ?? 0} in flight`} />
        <Tile label="Latency p50 / p99" value={rq.latency_ms?.p50 != null ? `${rq.latency_ms.p50} / ${rq.latency_ms.p99} ms` : "n/a"} hint={`max ${rq.latency_ms?.max ?? "n/a"} ms`} />
        <Tile label="5xx responses" value={rq.status_classes?.["5xx"] ?? 0} tone={rq.status_classes?.["5xx"] ? "warn" : "ok"} />
        <Tile label="Cloudflare tunnel" value={st.error ? "unknown" : st.tunnel_running ? "running" : "not found"} tone={st.tunnel_running ? "ok" : "warn"} />
      </Tiles>

      {p.error ? null : (
        <Sub title="Process details">
          <KV
            items={[
              ["Executable", <Mono key="e">{p.exe}</Mono>],
              ["Working directory", <Mono key="c">{p.cwd}</Mono>],
              ["Command line", <Cmd key="cl" argv={p.cmdline} />],
              ["User / status", `${p.username} / ${p.status}`],
              ["Started", fmtDate(p.started_at)],
              ["CPU time user / system", `${p.cpu_times?.user}s / ${p.cpu_times?.system}s`],
              ["I/O read / write", `${fmtBytes(p.io?.read_bytes)} / ${fmtBytes(p.io?.write_bytes)}`],
              ["Open files", String(p.open_files?.total ?? 0)],
              ["Dashboard DB", <Mono key="d">{db.path}</Mono>],
              ["Python", s.versions?.python],
              ["Platform", s.versions?.platform],
            ]}
          />
        </Sub>
      )}

      <Sub title="Stack: listening ports">
        {st.error ? (
          <p className="text-sm text-danger">{st.error}</p>
        ) : (
          <SortTable
            rows={arr(st.ports)}
            initialSort="port"
            cols={[
              { key: "port", label: "Port", sort: (r) => r.port },
              { key: "label", label: "Service", sort: (r) => r.label },
              { key: "listening", label: "State", sort: (r) => r.listening, render: (r) => <YesNo value={r.listening} yes="listening" no="not listening" /> },
              { key: "bind", label: "Bind", render: (r) => r.bind ?? "-" },
              { key: "pid", label: "PID", sort: (r) => r.pid, render: (r) => r.pid ?? "-" },
              { key: "process", label: "Process", render: (r) => r.process ?? "-" },
            ]}
          />
        )}
      </Sub>

      {arr(st.tunnel_processes).length ? (
        <Sub title="Cloudflare tunnel processes">
          <SortTable
            rows={arr(st.tunnel_processes)}
            cols={[
              { key: "pid", label: "PID" },
              { key: "status", label: "Status" },
              { key: "up", label: "Uptime", right: true, render: (r) => fmtUptime(r.uptime_seconds) },
              { key: "rss", label: "RSS", right: true, render: (r) => fmtBytes(r.rss_bytes) },
              { key: "cpu", label: "CPU", right: true, render: (r) => fmtPct(r.cpu_percent) },
              { key: "cmd", label: "Command line", render: (r) => r.error ?? <Cmd argv={r.cmdline} />, className: "min-w-[18rem]" },
            ]}
          />
        </Sub>
      ) : null}

      <Sub title="Dashboard sign-ins (who and when; no session ids or tokens; sessions unused for 30 days are deleted)">
        {ses.error ? (
          <p className="text-sm text-danger">{ses.error}</p>
        ) : (
          <>
            <KV items={[["Total", fmtNum(ses.total)], ["Active in 24h", fmtNum(ses.active_24h)], ["Guild members", fmtNum(ses.guild_members)], ["Oldest session created", fmtDate(ses.oldest_created_at)], ["Max session age", fmtUptime(ses.max_age_seconds)]]} />
            <SortTable
              rows={arr(ses.by_user)}
              initialSort="last_seen"
              desc
              maxHeight="max-h-72"
              cols={[
                {
                  key: "username",
                  label: "User",
                  sort: (r) => r.username,
                  render: (r) => (
                    <span className="inline-flex flex-wrap items-center gap-x-2">
                      <ResolvedUser userId={String(r.user_id)} link />
                      {r.username ? <span className="text-xs text-muted-foreground">@{r.username}</span> : null}
                    </span>
                  ),
                },
                { key: "last_seen", label: "Last seen", sort: (r) => r.last_seen, render: (r) => fmtDate(r.last_seen) },
              ]}
            />
          </>
        )}
      </Sub>

      <Sub title="Requests since dashboard start (per route template)">
        {rq.error ? (
          <p className="text-sm text-danger">{rq.error}</p>
        ) : (
          <>
            <Dict data={rq.status_classes} />
            <SortTable
              rows={arr(rq.routes)}
              initialSort="count"
              desc
              maxHeight="max-h-72"
              limit={12}
              cols={[
                { key: "route", label: "Route", sort: (r) => r.route, render: (r) => <Mono>{r.route}</Mono> },
                { key: "count", label: "Count", right: true, sort: (r) => r.count },
                { key: "avg", label: "Avg ms", right: true, sort: (r) => r.avg_ms },
                { key: "max", label: "Max ms", right: true, sort: (r) => r.max_ms },
                { key: "errors", label: "5xx", right: true, sort: (r) => r.errors },
              ]}
            />
          </>
        )}
      </Sub>

      <SocketTable sockets={p.sockets} />

      <div className="grid gap-5 lg:grid-cols-2">
        <Sub title="Dashboard config (non-secret values)">
          <Dict data={s.config} fmt={(k, v) => (v === "set" && /(SECRET|TOKEN)/.test(k) ? "set (hidden)" : String(v))} />
        </Sub>
        <Sub title="Dashboard package versions">
          <Dict data={s.versions} />
        </Sub>
      </div>

      <Sub title={`Dashboard routes (${arr(s.routes).length})`}>
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">Show route list</summary>
          <div className="mt-2">
            <SortTable rows={arr(s.routes)} initialSort="path" maxHeight="max-h-72" cols={[{ key: "path", label: "Path", sort: (r) => r.path, render: (r) => <Mono>{r.path}</Mono> }, { key: "methods", label: "Methods", render: (r) => (r.methods as string[]).join(", ") }]} />
          </div>
        </details>
      </Sub>
    </>
  )
}
