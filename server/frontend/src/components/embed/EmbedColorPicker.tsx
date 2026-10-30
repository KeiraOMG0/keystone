/**
 * Hex input + visual swatch + Discord-role-color-style presets. Emits color
 * changes as the decimal integer format template_embeds.color / discord.Color
 * expect (e.g. 0x5865F2 -> 5793266) — callers never need to convert
 * themselves. Standalone, no page coupling.
 */
import { useState, useEffect } from "react"

export interface EmbedColorPickerProps {
  /** Decimal integer color, or null/undefined for "no color set". */
  value: number | null | undefined
  onChange: (color: number | null) => void
  className?: string
}

const PRESETS: { label: string; hex: string }[] = [
  { label: "Blurple", hex: "#5865F2" },
  { label: "Green", hex: "#57F287" },
  { label: "Yellow", hex: "#FEE75C" },
  { label: "Fuchsia", hex: "#EB459E" },
  { label: "Red", hex: "#ED4245" },
  { label: "White", hex: "#FFFFFF" },
  { label: "Black", hex: "#23272A" },
  { label: "Dark Red", hex: "#992D22" },
  { label: "Dark Orange", hex: "#A84300" },
  { label: "Gold", hex: "#C27C0E" },
  { label: "Dark Green", hex: "#1F8B4C" },
  { label: "Dark Blue", hex: "#206694" },
  { label: "Dark Purple", hex: "#71368A" },
  { label: "Greyple", hex: "#99AAB5" },
]

function intToHex(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value) || value < 0) {
    return "#5865F2"
  }
  return `#${Math.min(value, 0xffffff).toString(16).padStart(6, "0")}`
}

function hexToInt(hex: string): number | null {
  const cleaned = hex.trim().replace(/^#/, "")
  if (!/^[0-9a-fA-F]{6}$/.test(cleaned)) return null
  return parseInt(cleaned, 16)
}

export function EmbedColorPicker({ value, onChange, className }: EmbedColorPickerProps) {
  const [hexInput, setHexInput] = useState(intToHex(value))

  // Keep the text input in sync when the value changes externally (e.g. a
  // preset click, or loading a different template into the form).
  useEffect(() => {
    setHexInput(intToHex(value))
  }, [value])

  function handleHexInputChange(next: string) {
    setHexInput(next)
    const parsed = hexToInt(next)
    if (parsed !== null) {
      onChange(parsed)
    }
  }

  function handlePresetClick(hex: string) {
    setHexInput(hex)
    onChange(hexToInt(hex))
  }

  const swatchColor = intToHex(value)
  const isValid = hexToInt(hexInput) !== null

  return (
    <div className={`flex flex-col gap-2 ${className ?? ""}`}>
      <div className="flex items-center gap-2">
        <label className="relative size-9 shrink-0 cursor-pointer overflow-hidden rounded-md border border-input">
          <input
            type="color"
            value={swatchColor}
            onChange={(e) => handlePresetClick(e.target.value)}
            className="absolute -left-1 -top-1 size-10 cursor-pointer border-none p-0"
            aria-label="Pick embed color"
          />
        </label>
        <input
          type="text"
          value={hexInput}
          onChange={(e) => handleHexInputChange(e.target.value)}
          placeholder="#5865F2"
          spellCheck={false}
          className={`h-9 w-28 rounded-lg border bg-card px-3 font-mono text-sm text-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/25 ${
            isValid ? "border-input focus-visible:border-ring" : "border-danger focus-visible:border-danger"
          }`}
        />
        {value !== null && value !== undefined ? (
          <button
            type="button"
            onClick={() => onChange(null)}
            className="text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
          >
            Clear
          </button>
        ) : null}
      </div>

      <div className="flex flex-wrap gap-1.5">
        {PRESETS.map((preset) => (
          <button
            key={preset.hex}
            type="button"
            title={preset.label}
            onClick={() => handlePresetClick(preset.hex)}
            className="size-6 rounded-full ring-1 ring-inset ring-black/15 transition-transform hover:scale-110"
            style={{ backgroundColor: preset.hex }}
            aria-label={preset.label}
          />
        ))}
      </div>
    </div>
  )
}

export default EmbedColorPicker
