param(
    [Parameter(Mandatory = $true)]
    [int]$InstallerProcessId,
    [Parameter(Mandatory = $true)]
    [string]$ApplicationPath,
    [ValidateSet("en", "ar")]
    [string]$Language = "en"
)

while (Get-Process -Id $InstallerProcessId -ErrorAction SilentlyContinue) {
    Start-Sleep -Milliseconds 500
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$isArabic = $Language -eq "ar"
$form = New-Object System.Windows.Forms.Form
$form.Text = if ($isArabic) { "تم تحديث TimeTracker" } else { "TimeTracker updated" }
$form.ClientSize = New-Object System.Drawing.Size(420, 150)
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ShowInTaskbar = $true
$form.TopMost = $true

$message = New-Object System.Windows.Forms.Label
$message.AutoSize = $false
$message.Location = New-Object System.Drawing.Point(20, 20)
$message.Size = New-Object System.Drawing.Size(380, 50)
$message.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$message.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
$message.Text = if ($isArabic) {
    "تم تحديث البرنامج بنجاح. هل تريد فتحه الآن؟"
} else {
    "TimeTracker was updated successfully. Would you like to open it now?"
}
$form.Controls.Add($message)

$openButton = New-Object System.Windows.Forms.Button
$openButton.Location = New-Object System.Drawing.Point(95, 90)
$openButton.Size = New-Object System.Drawing.Size(130, 34)
$openButton.Text = if ($isArabic) { "افتح البرنامج" } else { "Open now" }
$openButton.Add_Click({
    try {
        Start-Process -FilePath $ApplicationPath -ErrorAction Stop
        $form.Close()
    } catch {
        [System.Windows.Forms.MessageBox]::Show(
            $_.Exception.Message,
            $form.Text,
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    }
})
$form.Controls.Add($openButton)
$form.AcceptButton = $openButton

$laterButton = New-Object System.Windows.Forms.Button
$laterButton.Location = New-Object System.Drawing.Point(235, 90)
$laterButton.Size = New-Object System.Drawing.Size(90, 34)
$laterButton.Text = if ($isArabic) { "لاحقًا" } else { "Later" }
$laterButton.Add_Click({ $form.Close() })
$form.Controls.Add($laterButton)
$form.CancelButton = $laterButton

if ($isArabic) {
    $form.RightToLeft = [System.Windows.Forms.RightToLeft]::Yes
    $form.RightToLeftLayout = $true
}

try {
    [void]$form.ShowDialog()
} finally {
    $form.Dispose()
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
