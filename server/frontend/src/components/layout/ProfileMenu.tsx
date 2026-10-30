import { Link } from "react-router-dom"
import { Check, CodeXml, ExternalLink, Keyboard, LogOut, Monitor, Moon, Sun, Terminal } from "lucide-react"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Kbd } from "@/components/ui/kbd"
import { useAuth } from "@/hooks/useAuth"
import { useLogout } from "@/api/auth"
import { useDeveloperMode } from "@/context/DeveloperModeContext"
import { useTheme } from "@/context/ThemeContext"
import { cn } from "@/lib/utils"
import { discordAvatarUrl } from "@/lib/format"
import { SOURCE_CODE_URL } from "@/lib/project"

const THEME_OPTIONS = [
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "system", label: "System", Icon: Monitor },
] as const

function initials(username: string): string {
  return username.slice(0, 2).toUpperCase()
}

/**
 * Avatar + name trigger fronting the profile dropdown: theme control
 * (Light / Dark / System), developer mode, keyboard shortcuts, service
 * status link and sign out.
 */
export function ProfileMenu({ onShowShortcuts }: { onShowShortcuts: () => void }) {
  const { user, isStaff } = useAuth()
  const logout = useLogout()
  const { enabled: devMode, toggle: toggleDevMode } = useDeveloperMode()
  const { theme, setTheme } = useTheme()

  if (!user) return null

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="Account menu"
          className="flex h-9 items-center gap-2 rounded-lg p-1 pr-2 transition-colors hover:bg-accent aria-expanded:bg-accent max-md:size-11 max-md:justify-center max-md:p-0"
        >
          <Avatar className="size-7">
            <AvatarImage src={discordAvatarUrl(user.id, user.avatar)} alt="" />
            <AvatarFallback className="text-xs">{initials(user.username)}</AvatarFallback>
          </Avatar>
          <span className="hidden max-w-32 truncate text-base font-medium text-foreground md:inline">{user.username}</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-64">
        <div className="flex items-center gap-2.5 px-2.5 py-2">
          <Avatar>
            <AvatarImage src={discordAvatarUrl(user.id, user.avatar)} alt="" />
            <AvatarFallback>{initials(user.username)}</AvatarFallback>
          </Avatar>
          <div className="min-w-0">
            <p className="truncate text-base font-semibold text-foreground">{user.username}</p>
            <p className="text-xs text-muted-foreground">{isStaff ? "Staff" : "Moderator"}</p>
          </div>
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        <div role="radiogroup" aria-label="Theme" className="mx-1.5 mb-1.5 flex gap-0.5 rounded-lg bg-muted p-0.5">
          {THEME_OPTIONS.map(({ value, label, Icon }) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={theme === value}
              onClick={() => setTheme(value)}
              className={cn(
                "inline-flex h-8 flex-1 items-center justify-center gap-1.5 rounded-md text-sm font-medium transition-colors duration-[120ms] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring",
                theme === value ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
              )}
            >
              <Icon className="size-3.5" aria-hidden />
              {label}
            </button>
          ))}
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={(e) => { e.preventDefault(); toggleDevMode() }}>
          <Terminal />
          Developer mode
          <Check className={cn("ml-auto", devMode ? "opacity-100" : "opacity-0")} />
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onShowShortcuts}>
          <Keyboard />
          Keyboard shortcuts
          <Kbd className="ml-auto">?</Kbd>
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <Link to="/status">
            <ExternalLink />
            Service status
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <a href={SOURCE_CODE_URL} target="_blank" rel="noopener noreferrer">
            <CodeXml />
            Source code
          </a>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <p className="px-2 py-1 text-2xs text-subtle-foreground" title={__BUILD_TIME__}>
          Build {new Date(__BUILD_TIME__).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}
        </p>
        <DropdownMenuItem onSelect={() => logout.mutate()}>
          <LogOut />
          {logout.isPending ? "Signing out…" : "Sign out"}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
