# LimboWave: Windows Hello identity gate (Task 1.3).
#
# Why PowerShell: this project deliberately does not depend on the winrt/winsdk
# Python bindings. Windows ships Windows PowerShell 5.1, which projects WinRT
# types natively -- so a real Hello gate (PIN or biometrics, whichever the
# machine is configured for) is reachable with no new dependency.
#
# Contract with the caller:
#   LIMBOWAVE_HELLO_MODE        "check" or "verify"
#   LIMBOWAVE_HELLO_REASON      message shown in the system dialog (verify only)
#   LIMBOWAVE_HELLO_TIMEOUT_MS  how long to wait for the async operation
# Output: exactly one of
#   LIMBOWAVE_HELLO_RESULT <enum name>
#   LIMBOWAVE_HELLO_ERROR <reason>
#
# This script NEVER prints any authentication material. The PIN / biometric
# data never leaves the system components; only the result enum comes back.
# Keep this file pure ASCII: Windows PowerShell 5.1 parses a BOM-less non-ASCII
# script as system ANSI and fails on Chinese text (a hard-won fact from Phase 7).
$ErrorActionPreference = "Stop"
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $ucv = [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI, ContentType=WindowsRuntime]
  # In this projection the WinRT IAsyncOperation comes back as a bare COM object
  # whose properties read as null, so it must be wrapped by AsTask. The result
  # enum type is taken from the method signature by reflection instead of being
  # spelled out -- spelling it out got the name wrong once already.
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1" })[0]
  if ($null -eq $asTask) { Write-Output "LIMBOWAVE_HELLO_ERROR no-as-task"; exit 0 }

  $verify = $env:LIMBOWAVE_HELLO_MODE -eq "verify"
  $methodName = "CheckAvailabilityAsync"
  if ($verify) { $methodName = "RequestVerificationAsync" }
  $method = $ucv.GetMethods() |
      Where-Object { $_.Name -eq $methodName } | Select-Object -First 1
  if ($null -eq $method) { Write-Output "LIMBOWAVE_HELLO_ERROR no-method"; exit 0 }

  if ($verify) {
    $op = $ucv::RequestVerificationAsync($env:LIMBOWAVE_HELLO_REASON)
  } else {
    $op = $ucv::CheckAvailabilityAsync()
  }
  if ($null -eq $op) { Write-Output "LIMBOWAVE_HELLO_ERROR no-op"; exit 0 }

  $resultType = $method.ReturnType.GetGenericArguments()[0]
  $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($op))
  $timeoutMs = [int]$env:LIMBOWAVE_HELLO_TIMEOUT_MS
  if (-not $task.Wait($timeoutMs)) { Write-Output "LIMBOWAVE_HELLO_ERROR timeout"; exit 0 }
  Write-Output ("LIMBOWAVE_HELLO_RESULT " + $task.Result.ToString())
} catch {
  Write-Output ("LIMBOWAVE_HELLO_ERROR " + $_.Exception.GetType().Name)
}
