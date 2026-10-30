/**
 * Visually accurate dark-mode Discord embed preview. Reusable, standalone —
 * takes plain props (no page-specific coupling, no API calls) so it can be
 * driven by controlled form state in TemplateEditorPage and by a rendered
 * TemplateEmbed in TicketPanelBuilderPage's panel-message preview alike.
 *
 * Discord embed layout being matched:
 *   author row (icon + name, linked if author_url set)
 *   title (linked if url set, bold, larger)
 *   description
 *   fields grid (inline fields pack 3-per-row on wide layouts, 2-per-row on
 *     narrower ones, matching Discord's own responsive behavior; non-inline
 *     fields always take the full row)
 *   image (large, full width within the embed)
 *   footer row (icon + text [+ timestamp])
 * ...all indented next to a 4px colored left border, matching Discord's
 * actual embed chrome. Thumbnail floats top-right of the content column.
 */

export interface EmbedPreviewField {
  name: string
  value: string
  inline: boolean | number
}

export interface EmbedPreviewProps {
  title?: string | null
  description?: string | null
  /** Decimal integer color (Discord's `color` field format), or null/undefined for the default border color. */
  color?: number | null
  url?: string | null
  footer?: { text?: string | null; icon_url?: string | null } | null
  thumbnail_url?: string | null
  image_url?: string | null
  author?: { name?: string | null; icon_url?: string | null; url?: string | null } | null
  fields?: EmbedPreviewField[]
  timestamp_mode?: "none" | "now" | string | null
  className?: string
}

const DEFAULT_BORDER_COLOR = "#4e5058" // Discord's no-color embed border shade

function colorToHex(color: number | null | undefined): string {
  if (color === null || color === undefined || Number.isNaN(color) || color < 0) {
    return DEFAULT_BORDER_COLOR
  }
  const clamped = Math.min(color, 0xffffff)
  return `#${clamped.toString(16).padStart(6, "0")}`
}

function isEmpty(value: string | null | undefined): boolean {
  return !value || value.trim().length === 0
}

function hasAnyContent(props: EmbedPreviewProps): boolean {
  return Boolean(
    !isEmpty(props.title) ||
      !isEmpty(props.description) ||
      !isEmpty(props.image_url) ||
      !isEmpty(props.thumbnail_url) ||
      !isEmpty(props.author?.name) ||
      !isEmpty(props.footer?.text) ||
      (props.fields && props.fields.some((f) => !isEmpty(f.name) && !isEmpty(f.value)))
  )
}

/** Groups fields into visual rows the way Discord's client packs them: up to
 *  3 consecutive inline fields share a row; a non-inline field always starts
 *  a fresh row by itself. */
function groupFieldRows(fields: EmbedPreviewField[]): EmbedPreviewField[][] {
  const rows: EmbedPreviewField[][] = []
  let current: EmbedPreviewField[] = []

  for (const field of fields) {
    if (isEmpty(field.name) || isEmpty(field.value)) continue
    const inline = Boolean(field.inline)
    if (!inline) {
      if (current.length) rows.push(current)
      current = []
      rows.push([field])
      continue
    }
    current.push(field)
    if (current.length === 3) {
      rows.push(current)
      current = []
    }
  }
  if (current.length) rows.push(current)
  return rows
}

export function EmbedPreview(props: EmbedPreviewProps) {
  const { title, description, url, footer, thumbnail_url, image_url, author, fields = [], timestamp_mode, className } = props

  if (!hasAnyContent(props)) {
    return (
      <div className={`rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground ${className ?? ""}`}>
        Nothing to preview yet — add a title, description, or fields.
      </div>
    )
  }

  const borderColor = colorToHex(props.color)
  const fieldRows = groupFieldRows(fields)
  const showTimestamp = timestamp_mode === "now"

  return (
    <div
      className={`max-w-[520px] rounded-[3px] bg-embed font-sans text-embed-foreground ${className ?? ""}`}
      style={{ borderLeft: `4px solid ${borderColor}` }}
    >
      <div className="flex gap-4 px-4 py-3">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          {!isEmpty(author?.name) ? (
            <div className="flex items-center gap-2">
              {!isEmpty(author?.icon_url) ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={author!.icon_url as string}
                  alt=""
                  className="size-6 rounded-full object-cover"
                  onError={(e) => {
                    e.currentTarget.style.display = "none"
                  }}
                />
              ) : null}
              {author?.url ? (
                <a
                  href={author.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm font-semibold text-embed-foreground hover:underline"
                >
                  {author?.name}
                </a>
              ) : (
                <span className="text-sm font-semibold text-embed-foreground">{author?.name}</span>
              )}
            </div>
          ) : null}

          {!isEmpty(title) ? (
            url ? (
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                className="w-fit text-base font-semibold text-info hover:underline"
              >
                {title}
              </a>
            ) : (
              <p className="text-base font-semibold text-embed-foreground">{title}</p>
            )
          ) : null}

          {!isEmpty(description) ? (
            <p className="whitespace-pre-wrap text-sm leading-[1.375] text-embed-foreground">{description}</p>
          ) : null}

          {fieldRows.length > 0 ? (
            <div className="flex flex-col gap-2">
              {fieldRows.map((row, rowIdx) => (
                <div key={rowIdx} className="grid gap-3" style={{ gridTemplateColumns: `repeat(${row.length}, minmax(0, 1fr))` }}>
                  {row.map((field, fieldIdx) => (
                    <div key={fieldIdx} className="flex min-w-0 flex-col gap-0.5">
                      <p className="text-sm font-semibold text-embed-foreground">{field.name}</p>
                      <p className="whitespace-pre-wrap text-sm leading-[1.375] text-embed-foreground">{field.value}</p>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          ) : null}

          {!isEmpty(image_url) ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={image_url as string}
              alt=""
              className="mt-1 max-h-[300px] max-w-full rounded-[4px] object-contain"
              onError={(e) => {
                e.currentTarget.style.display = "none"
              }}
            />
          ) : null}

          {!isEmpty(footer?.text) || showTimestamp ? (
            <div className="mt-1 flex items-center gap-2">
              {!isEmpty(footer?.icon_url) ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={footer!.icon_url as string}
                  alt=""
                  className="size-5 rounded-full object-cover"
                  onError={(e) => {
                    e.currentTarget.style.display = "none"
                  }}
                />
              ) : null}
              <span className="text-xs text-embed-foreground">
                {!isEmpty(footer?.text) ? footer!.text : null}
                {!isEmpty(footer?.text) && showTimestamp ? " • " : null}
                {showTimestamp ? "Today at 12:00 PM" : null}
              </span>
            </div>
          ) : null}
        </div>

        {!isEmpty(thumbnail_url) ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={thumbnail_url as string}
            alt=""
            className="size-20 shrink-0 rounded-[4px] object-cover"
            onError={(e) => {
              e.currentTarget.style.display = "none"
            }}
          />
        ) : null}
      </div>
    </div>
  )
}

export default EmbedPreview
