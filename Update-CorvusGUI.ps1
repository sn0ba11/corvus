param(
    [string]$UpdaterPath = (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "Update-Corvus.ps1")
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase

if (-not (Test-Path -LiteralPath $UpdaterPath -PathType Leaf)) {
    [System.Windows.MessageBox]::Show(
        "Corvus Update was not found at:`r`n$UpdaterPath",
        "Corvus Update",
        [System.Windows.MessageBoxButton]::OK,
        [System.Windows.MessageBoxImage]::Error
    ) | Out-Null
    exit 1
}

$root = Split-Path -Parent $UpdaterPath
# Must match $DefaultRepositoryUrl in Update-OSINTWorkbench.ps1 (a test keeps them equal).
$DefaultRepositoryUrl = "https://github.com/sn0ba11/corvus.git"
$iconPath = Join-Path $root "installer\OSINTWorkbench.ico"
$tempRoot = Join-Path $env:TEMP ("corvus-update-gui-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$stdoutPath = Join-Path $tempRoot "stdout.log"
$stderrPath = Join-Path $tempRoot "stderr.log"

[xml]$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Corvus Update" Width="820" Height="700" MinWidth="700" MinHeight="620"
        WindowStartupLocation="CenterScreen" Background="#0B0F0C" Foreground="#F4F7F2"
        FontFamily="Segoe UI" ResizeMode="CanResizeWithGrip">
  <Window.Resources>
    <SolidColorBrush x:Key="PanelBrush" Color="#151B17"/>
    <SolidColorBrush x:Key="PanelBorder" Color="#28342B"/>
    <SolidColorBrush x:Key="Muted" Color="#9CAE9F"/>
    <SolidColorBrush x:Key="Accent" Color="#8BE39B"/>
    <Style TargetType="Button">
      <Setter Property="Foreground" Value="#0B0F0C"/>
      <Setter Property="Background" Value="{StaticResource Accent}"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Padding" Value="18,10"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border Background="{TemplateBinding Background}" CornerRadius="8" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="SecondaryButton" TargetType="Button" BasedOn="{StaticResource {x:Type Button}}">
      <Setter Property="Foreground" Value="#E4EBE4"/>
      <Setter Property="Background" Value="#263029"/>
    </Style>
  </Window.Resources>

  <Grid Margin="28">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="18"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="16"/>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="18"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <Grid Grid.Row="0">
      <Grid.ColumnDefinitions><ColumnDefinition/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
      <StackPanel>
        <TextBlock Text="CORVUS" FontSize="13" FontWeight="Bold" Foreground="{StaticResource Accent}"/>
        <TextBlock Text="Update" FontSize="32" FontWeight="SemiBold" Margin="0,2,0,0"/>
        <TextBlock Text="Secure fast-forward update, rebuild and health verification" Foreground="{StaticResource Muted}" FontSize="13" Margin="0,6,0,0"/>
      </StackPanel>
      <Border Grid.Column="1" Background="#111713" BorderBrush="{StaticResource PanelBorder}" BorderThickness="1" CornerRadius="10" Padding="12,8" VerticalAlignment="Top">
        <TextBlock Text="main" Foreground="{StaticResource Muted}" FontFamily="Consolas" FontSize="12"/>
      </Border>
    </Grid>

    <Border Grid.Row="2" Background="{StaticResource PanelBrush}" BorderBrush="{StaticResource PanelBorder}" BorderThickness="1" CornerRadius="14" Padding="20">
      <Grid>
        <Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="12"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
        <Grid>
          <Grid.ColumnDefinitions><ColumnDefinition/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
          <StackPanel>
            <TextBlock x:Name="StatusTitle" Text="Ready to update" FontSize="18" FontWeight="SemiBold"/>
            <TextBlock x:Name="StatusDetail" Text="Corvus will check the configured source repository and apply a clean fast-forward update." Foreground="{StaticResource Muted}" Margin="0,5,0,0" TextWrapping="Wrap"/>
          </StackPanel>
          <Border x:Name="StateBadge" Grid.Column="1" Background="#263029" CornerRadius="10" Padding="12,6" VerticalAlignment="Top">
            <TextBlock x:Name="StateBadgeText" Text="READY" Foreground="#DDE9DE" FontSize="11" FontWeight="Bold"/>
          </Border>
        </Grid>
        <ProgressBar x:Name="Progress" Grid.Row="2" Height="6" IsIndeterminate="False" Value="0" Background="#263029" Foreground="{StaticResource Accent}" BorderThickness="0"/>
      </Grid>
    </Border>

    <StackPanel Grid.Row="4">
      <Border Margin="0,0,0,12" Background="#0F1411" BorderBrush="{StaticResource PanelBorder}" BorderThickness="1" CornerRadius="12" Padding="16,13">
        <StackPanel>
          <TextBlock Text="Update source" FontWeight="SemiBold"/>
          <Grid Margin="0,8,0,0">
            <Grid.ColumnDefinitions><ColumnDefinition/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
            <TextBox x:Name="SourceBox" Background="#0A0E0B" Foreground="#DCE7DD" BorderBrush="{StaticResource PanelBorder}" BorderThickness="1" Padding="10,7" FontFamily="Cascadia Mono, Consolas" FontSize="12" VerticalContentAlignment="Center"/>
            <Button x:Name="ResetSourceButton" Grid.Column="1" Content="Reset to default" Style="{StaticResource SecondaryButton}" Margin="10,0,0,0" Padding="14,8"/>
          </Grid>
          <TextBlock Text="Corvus fetches updates from this Git address. Change it only if the source has moved." Foreground="{StaticResource Muted}" FontSize="11" Margin="0,8,0,0" TextWrapping="Wrap"/>
        </StackPanel>
      </Border>
      <Border x:Name="ChangeLogPanel" Margin="0,0,0,12" Height="150" Background="#0F1411" BorderBrush="{StaticResource PanelBorder}" BorderThickness="1" CornerRadius="12">
        <Grid>
          <Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="*"/></Grid.RowDefinitions>
          <Grid Margin="16,13,16,10">
            <Grid.ColumnDefinitions><ColumnDefinition/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
            <TextBlock Text="What's new" FontWeight="SemiBold"/>
            <TextBlock Grid.Column="1" Text="last installed update" Foreground="{StaticResource Muted}" FontSize="11"/>
          </Grid>
          <TextBox x:Name="ChangeLogBox" Grid.Row="1" IsReadOnly="True" AcceptsReturn="True" TextWrapping="Wrap" VerticalScrollBarVisibility="Auto"
                   Background="#0A0E0B" Foreground="#DCE7DD" BorderThickness="0" Padding="16,10"/>
        </Grid>
      </Border>
      <Button x:Name="DetailsButton" Content="Technical details" Style="{StaticResource SecondaryButton}" HorizontalAlignment="Left" Padding="14,8"/>
      <Border x:Name="DetailsPanel" Visibility="Collapsed" Margin="0,10,0,0" Height="245" Background="#0F1411" BorderBrush="{StaticResource PanelBorder}" BorderThickness="1" CornerRadius="12">
        <Grid>
          <Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="*"/></Grid.RowDefinitions>
          <Grid Margin="16,13,16,10">
            <Grid.ColumnDefinitions><ColumnDefinition/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
            <TextBlock Text="Update details" FontWeight="SemiBold"/>
            <TextBlock Grid.Column="1" Text="live log" Foreground="{StaticResource Muted}" FontSize="11"/>
          </Grid>
          <TextBox x:Name="LogBox" Grid.Row="1" IsReadOnly="True" AcceptsReturn="True" TextWrapping="NoWrap" VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Auto"
                   FontFamily="Cascadia Mono, Consolas" FontSize="12" Background="#0A0E0B" Foreground="#C8D6C9" BorderThickness="0" Padding="16,12"/>
        </Grid>
      </Border>
    </StackPanel>

    <Grid Grid.Row="6">
      <Grid.ColumnDefinitions><ColumnDefinition Width="Auto"/><ColumnDefinition/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions>
      <Button x:Name="UpdateButton" Content="Run Corvus Update" MinWidth="170"/>
      <TextBlock Grid.Column="1" Text="No force-pushes · no dirty-tree overwrite · rollback state retained" Foreground="{StaticResource Muted}" FontSize="11" VerticalAlignment="Center" Margin="16,0"/>
      <Button x:Name="CloseButton" Grid.Column="2" Content="Close" Style="{StaticResource SecondaryButton}" MinWidth="90"/>
    </Grid>
  </Grid>
</Window>
'@

$reader = New-Object System.Xml.XmlNodeReader $xaml
$window = [Windows.Markup.XamlReader]::Load($reader)
if (Test-Path -LiteralPath $iconPath -PathType Leaf) {
    try {
        $window.Icon = [System.Windows.Media.Imaging.BitmapFrame]::Create([Uri]$iconPath)
    } catch {}
}

$statusTitle = $window.FindName("StatusTitle")
$statusDetail = $window.FindName("StatusDetail")
$stateBadge = $window.FindName("StateBadge")
$stateBadgeText = $window.FindName("StateBadgeText")
$progress = $window.FindName("Progress")
$logBox = $window.FindName("LogBox")
$changeLogBox = $window.FindName("ChangeLogBox")
$detailsButton = $window.FindName("DetailsButton")
$detailsPanel = $window.FindName("DetailsPanel")
$updateButton = $window.FindName("UpdateButton")
$sourceBox = $window.FindName("SourceBox")
$resetSourceButton = $window.FindName("ResetSourceButton")
$closeButton = $window.FindName("CloseButton")

$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(300)
$script:process = $null
$script:lastText = ""
$installedChangeLogPath = Join-Path $root ".last-update-changes.txt"
$runtimeChangeLogPath = Join-Path $root "runtime\last-update-changes.txt"
$pendingChangeLogPath = Join-Path $root ".pending-update-changes.txt"
$repositoryChangeLogPath = Join-Path $root "CHANGELOG.md"

function Get-UnreleasedChangeLog {
    if (-not (Test-Path -LiteralPath $repositoryChangeLogPath -PathType Leaf)) { return "" }
    $content = Get-Content -LiteralPath $repositoryChangeLogPath -Raw -ErrorAction SilentlyContinue
    if (-not $content) { return "" }
    $match = [regex]::Match($content, '(?ms)^## Unreleased\s*(.*?)(?=^##\s|\z)')
    if (-not $match.Success) { return "" }
    return $match.Groups[1].Value.Trim()
}

function Refresh-ChangeLog {
    foreach ($candidate in @($installedChangeLogPath, $runtimeChangeLogPath, $pendingChangeLogPath)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $content = (Get-Content -LiteralPath $candidate -Raw -ErrorAction SilentlyContinue).Trim()
            if ($content) {
                $changeLogBox.Text = $content
                return
            }
        }
    }
    $unreleased = Get-UnreleasedChangeLog
    if ($unreleased) {
        $changeLogBox.Text = "Current Corvus changes:`r`n`r`n$unreleased"
    } else {
        $changeLogBox.Text = "No Corvus changelog content could be loaded."
    }
}

Refresh-ChangeLog

function Get-CurrentOrigin {
    try {
        $value = & git -C $root remote get-url origin 2>$null
        if ($LASTEXITCODE -eq 0 -and $value) { return ([string]($value | Select-Object -First 1)).Trim() }
    } catch {}
    return $DefaultRepositoryUrl
}

# Same forms Update-OSINTWorkbench.ps1 accepts; a double quote would break the argument line.
function Test-RepositoryUrl([string]$value) {
    if (-not $value) { return $false }
    if ($value.Contains('"')) { return $false }
    if ($value.StartsWith('-')) { return $false }
    return ($value -match '^(https?|ssh)://[^\s]+$') -or ($value -match '^[^@\s]+@[^:\s]+:.+$')
}

$script:currentOrigin = Get-CurrentOrigin
$sourceBox.Text = $script:currentOrigin

function Read-CombinedLog {
    $parts = New-Object Collections.Generic.List[string]
    if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
        $parts.Add((Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue))
    }
    if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
        $stderr = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
        if ($stderr) { $parts.Add($stderr) }
    }
    return ($parts -join [Environment]::NewLine).TrimEnd()
}

function Refresh-Log {
    $text = Read-CombinedLog
    if ($text -ne $script:lastText) {
        $logBox.Text = $text
        $logBox.ScrollToEnd()
        $script:lastText = $text
    }
}

function Show-Details([bool]$show) {
    if ($show) {
        $detailsPanel.Visibility = [System.Windows.Visibility]::Visible
        $detailsButton.Content = "Hide technical details"
        if ($window.Height -lt 840) { $window.Height = 840 }
    } else {
        $detailsPanel.Visibility = [System.Windows.Visibility]::Collapsed
        $detailsButton.Content = "Technical details"
        if ($window.Height -gt 730) { $window.Height = 700 }
    }
}

function Set-State([string]$title, [string]$detail, [string]$badge, [string]$badgeColor) {
    $statusTitle.Text = $title
    $statusDetail.Text = $detail
    $stateBadgeText.Text = $badge
    $stateBadge.Background = New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($badgeColor))
}

function Finish-Update([Nullable[int]]$exitCode) {
    $timer.Stop()
    $progress.IsIndeterminate = $false
    $progress.Value = 100
    $updateButton.IsEnabled = $true
    $sourceBox.IsEnabled = $true
    $resetSourceButton.IsEnabled = $true
    $updateButton.Content = "Check again"
    if ($null -ne $exitCode -and $exitCode -eq 0) { $script:currentOrigin = Get-CurrentOrigin }
    Refresh-Log
    Refresh-ChangeLog
    if ($null -ne $exitCode -and $exitCode -eq 0) {
        Set-State "Corvus is current" "The updater completed successfully. The details show whether an update was needed." "COMPLETE" "#21492C"
    } else {
        Set-State "Update failed" "Corvus did not complete the health-gated update. Review the technical details before retrying." "FAILED" "#5A2424"
        Show-Details $true
    }
    $script:process = $null
}

$timer.Add_Tick({
    if ($script:process -eq $null) { return }
    Refresh-Log
    if ($script:process.HasExited) {
        Finish-Update $script:process.ExitCode
    }
})

$detailsButton.Add_Click({
    Show-Details ($detailsPanel.Visibility -ne [System.Windows.Visibility]::Visible)
})

$resetSourceButton.Add_Click({
    $sourceBox.Text = $DefaultRepositoryUrl
})

$updateButton.Add_Click({
    if ($script:process -ne $null) { return }
    $sourceValue = $sourceBox.Text.Trim()
    if (-not (Test-RepositoryUrl $sourceValue)) {
        Set-State "Check the update source" "The update source must be an HTTP(S) or SSH Git address, or Git's user@host:path form, with no spaces." "INVALID" "#5A2424"
        return
    }
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    $logBox.Clear()
    $script:lastText = ""
    Set-State "Checking for updates" "Checking the configured source repository and validating any applied update." "RUNNING" "#294733"
    $updateButton.IsEnabled = $false
    $sourceBox.IsEnabled = $false
    $resetSourceButton.IsEnabled = $false
    $progress.Value = 0
    $progress.IsIndeterminate = $true
    $powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $arguments = @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $UpdaterPath), "-OnlyIfChanged"
    )
    # Only a changed address is passed; the updater then saves it as this install's origin.
    if ($sourceValue -ne $script:currentOrigin) {
        $arguments += @("-RepositoryUrl", ('"{0}"' -f $sourceValue))
    }
    try {
        $script:process = Start-Process -FilePath $powershell -ArgumentList $arguments -WorkingDirectory $root -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru -WindowStyle Hidden
        # Windows PowerShell can lose ExitCode for an asynchronous Start-Process
        # unless its native handle is retained before the process exits.
        $script:processHandle = $script:process.Handle
        $timer.Start()
    } catch {
        $progress.IsIndeterminate = $false
        $updateButton.IsEnabled = $true
        $sourceBox.IsEnabled = $true
        $resetSourceButton.IsEnabled = $true
        Set-State "Could not start Corvus Update" $_.Exception.Message "ERROR" "#5A2424"
        $logBox.Text = $_.Exception.Message
        $script:process = $null
    }
})

$closeButton.Add_Click({
    if ($script:process -ne $null -and -not $script:process.HasExited) {
        $choice = [System.Windows.MessageBox]::Show(
            "A Corvus update is still running. Interrupting it may leave the currently installed build unchanged but incomplete rebuild work may still be in progress.",
            "Corvus Update",
            [System.Windows.MessageBoxButton]::OKCancel,
            [System.Windows.MessageBoxImage]::Warning
        )
        if ($choice -ne [System.Windows.MessageBoxResult]::OK) { return }
    }
    $window.Close()
})

$window.Add_Closed({
    $timer.Stop()
    try { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue } catch {}
})

[void]$window.ShowDialog()
