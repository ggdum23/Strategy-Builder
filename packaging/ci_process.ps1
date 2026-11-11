# Wait for a Windows GUI executable.
# The call operator does not wait for a GUI-subsystem program and does not
# set $LASTEXITCODE. StrictMode then fails the step. Inno Setup's setup.exe
# and unins000.exe are GUI programs.

function Wait-GuiProcess {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $true)][string]$Arguments
    )
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $File
    $info.Arguments = $Arguments
    $info.UseShellExecute = $false
    $process = [System.Diagnostics.Process]::Start($info)
    if (-not $process) {
        throw "could not start $File"
    }
    $null = $process.WaitForExit()
    return [int]$process.ExitCode
}
