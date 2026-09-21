# Ensure every .ps1 in this folder is UTF-8 with BOM and CRLF line endings.
#
# Why: Windows PowerShell 5.1 decodes BOM-less script files using the system ANSI
# code page. Scripts containing non-ASCII text then fail to parse outright, while
# PowerShell 7 defaults to UTF-8 and would be fine. Keeping the BOM makes the same
# file work on both. This file is intentionally ASCII-only so it still runs when
# the convention has been broken.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/normalize-ps1.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$utf8WithBom = New-Object System.Text.UTF8Encoding $true

Get-ChildItem -Path $PSScriptRoot -Filter '*.ps1' -File | ForEach-Object {
    $text = [System.IO.File]::ReadAllText($_.FullName)
    $text = $text.TrimStart([char]0xFEFF)
    $text = $text.Replace("`r`n", "`n").Replace("`n", "`r`n")
    [System.IO.File]::WriteAllText($_.FullName, $text, $utf8WithBom)
    Write-Host ("normalized " + $_.Name)
}
