import type { DiscordEmbedJson } from "@/api/debugLab"

/** Plain-text-ish render of one Discord message (content + embeds as JSON from embed.to_dict()). */
export function MessagePreview({ content, embeds }: { content: string | null; embeds: DiscordEmbedJson[] }) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-muted/40 p-3">
      {content ? <p className="whitespace-pre-wrap break-words text-sm text-foreground">{content}</p> : null}
      {embeds.map((e, i) => (
        <EmbedPreview key={i} embed={e} />
      ))}
      {!content && embeds.length === 0 ? <p className="text-sm text-muted-foreground">(empty)</p> : null}
    </div>
  )
}

function EmbedPreview({ embed }: { embed: DiscordEmbedJson }) {
  // Discord's own embed colour is data, not a theme colour: shown as the stripe only.
  const stripe = typeof embed.color === "number" ? `#${embed.color.toString(16).padStart(6, "0")}` : undefined
  return (
    <div className="flex overflow-hidden rounded-md border border-border bg-card">
      <div className="w-1 shrink-0 bg-border" style={stripe ? { backgroundColor: stripe } : undefined} aria-hidden />
      <div className="flex min-w-0 flex-1 gap-3 p-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5 text-sm">
          {embed.author?.name ? (
            <div className="flex items-center gap-2 text-xs font-medium text-foreground">
              {embed.author.icon_url ? <img src={embed.author.icon_url} alt="" className="size-5 rounded-full" /> : null}
              {embed.author.name}
            </div>
          ) : null}
          {embed.title ? <p className="font-semibold text-foreground">{embed.title}</p> : null}
          {embed.description ? <p className="whitespace-pre-wrap break-words text-foreground">{embed.description}</p> : null}
          {embed.fields?.length ? (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              {embed.fields.map((f, i) => (
                <div key={i} className={f.inline ? "" : "sm:col-span-3"}>
                  <p className="text-xs font-semibold text-foreground">{f.name}</p>
                  <p className="whitespace-pre-wrap break-words text-xs text-muted-foreground">{f.value}</p>
                </div>
              ))}
            </div>
          ) : null}
          {embed.image?.url ? <img src={embed.image.url} alt="" className="mt-1 max-h-48 max-w-full rounded-md object-contain" /> : null}
          {embed.footer?.text || embed.timestamp ? (
            <p className="text-xs text-muted-foreground">
              {embed.footer?.text}
              {embed.footer?.text && embed.timestamp ? " • " : ""}
              {embed.timestamp ? new Date(embed.timestamp).toLocaleString() : ""}
            </p>
          ) : null}
        </div>
        {embed.thumbnail?.url ? <img src={embed.thumbnail.url} alt="" className="size-16 shrink-0 rounded-md object-cover" /> : null}
      </div>
    </div>
  )
}
