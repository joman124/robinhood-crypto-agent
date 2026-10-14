# Starts `rhca run` (shadow mode: it never orders) at boot, before anyone signs
# in, and at sign-in if it is not running already. It replaces the Startup
# folder shortcut, which waited for a sign-in: a restart overnight left the
# loop down until morning, and each daily close it misses is a forward-test day
# with no Robinhood price, which cannot be read afterwards (docs/runbook.md).
#
# Run once, from PowerShell opened with "Run as administrator", in the repo:
#   powershell -ExecutionPolicy Bypass -File scripts\install-boot-task.ps1
# Undo: Unregister-ScheduledTask -TaskName "rhca run" -Confirm:$false

#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$name = "rhca run"
$user = "$env:USERDOMAIN\$env:USERNAME"

# No window to read without a sign-in, so its output goes to a file.
$action = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $repo `
    -Argument "/c `"`"$repo\scripts\run-forever.cmd`" >> `"$repo\data\rhca-run.log`" 2>&1`""
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn -User $user)
)
# S4U runs it as you, signed in or not, with no password stored. It reaches
# the internet but not network shares, which rhca run never uses.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U
# The default stops a task after 3 days and on battery. A trigger that fires
# while it runs is ignored, so boot and sign-in never start a second copy.
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings -Force | Out-Null
"Registered the '$name' task for $user."

$shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "rhca run.lnk"
if (Test-Path $shortcut) {
    Remove-Item $shortcut
    "Removed the Startup shortcut it replaces."
}
"Start it now with: Start-ScheduledTask -TaskName '$name'"
"Then close the old 'rhca run' window; the task's copy takes over within a minute."
