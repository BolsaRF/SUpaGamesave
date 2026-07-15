Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Win32 {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);
}
"@ 

$func = [Win32+EnumWindowsProc] { param($hWnd, $lParam) 
    $sb = New-Object System.Text.StringBuilder 1024
    [Win32]::GetWindowText($hWnd, $sb, $sb.Capacity) | Out-Null
    $title = $sb.ToString()
    if ([Win32]::IsWindowVisible($hWnd) -and $title) { Write-Output $title }
    return $true
}
[Win32]::EnumWindows($func, [IntPtr]::Zero)
