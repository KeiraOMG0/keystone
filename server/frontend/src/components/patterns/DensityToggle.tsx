import { Segmented } from "@/components/ui/segmented"
import { usePrefs } from "@/context/PrefsContext"

/** Comfortable / Compact table density, persisted per browser (applies to every table). */
export function DensityToggle() {
  const { density, setDensity } = usePrefs()
  return (
    <Segmented
      size="sm"
      label="Table density"
      value={density}
      onValueChange={setDensity}
      options={[
        { value: "comfortable", label: "Comfortable" },
        { value: "compact", label: "Compact" },
      ]}
    />
  )
}
