# 07-install-sv.ps1
# Optional shortcuts for sv.bat (the supervisor control menu), run by "sv install":
#   1. a Desktop shortcut "kick-bot control" that opens the menu
#   2. the repo folder on your USER PATH, so "sv" works in any new terminal
# Asks before each one; safe to run again. Undo: delete the shortcut, and remove the
# folder from Path under "Edit environment variables for your account".

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$sv = Join-Path $root 'sv.bat'

function Ask([string]$question) {
    (Read-Host "$question [y/N]") -match '^(y|yes)$'
}

if (Ask 'Create a Desktop shortcut "kick-bot control" that opens the sv menu?') {
    $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'kick-bot control.lnk'
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $shortcut.TargetPath = $sv
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = 'kick-bot supervisor menu (sv.bat)'
    $shortcut.Save()
    Write-Host "Created $lnk"
}

if (Ask "Add $root to your user PATH so 'sv' works in any new terminal?") {
    $key = Get-Item 'HKCU:\Environment'
    # Raw value, so entries like %USERPROFILE%\... stay unexpanded.
    $raw = [string]$key.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
    $parts = @($raw -split ';' | Where-Object { $_ })
    $already = $parts | Where-Object {
        [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $root.TrimEnd('\')
    }
    if ($already) {
        Write-Host 'Already on your PATH.'
    } else {
        $kind = if ($raw) { $key.GetValueKind('Path') } else { 'ExpandString' }
        Set-ItemProperty -Path 'HKCU:\Environment' -Name Path -Value (($parts + $root) -join ';') -Type $kind
        # Setting a user variable broadcasts the change, so Explorer (and every terminal
        # opened from now on) reloads the environment. The dummy is removed right away.
        [Environment]::SetEnvironmentVariable('KICKBOT_SV_REFRESH', '1', 'User')
        [Environment]::SetEnvironmentVariable('KICKBOT_SV_REFRESH', $null, 'User')
        Write-Host "Added. Open a NEW terminal, then type: sv"
    }
}
