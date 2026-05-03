Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*mycelia_platform.py*" } |
  Select-Object ProcessId,CommandLine
