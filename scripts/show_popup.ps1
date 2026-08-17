# Persistent notification popup for AI Researcher.
# Shows an always-on-top card in the bottom-right corner of the primary screen.
# It stays there until the user clicks it (opens the file and closes) or hits X.
#
# Usage:
#   powershell -STA -File scripts\show_popup.ps1 -Title "Morning Brief" -BodyFile logs\popup-body-0.txt -OpenPath output\x.md
#   -Slot N            stacks multiple popups above each other (0 = bottom-most)
#   -AutoCloseSeconds  close automatically (testing only; 0 = never)
#
# NOTE: ASCII-only on purpose (PowerShell 5.1 misreads BOM-less UTF-8 sources).
# Czech text is passed via -BodyFile (UTF-8) or as XML entities in the XAML.

param(
    [Parameter(Mandatory = $true)][string]$Title,
    [string]$Body = '',
    [string]$BodyFile,
    [string]$OpenPath,
    [int]$Slot = 0,
    [int]$AutoCloseSeconds = 0,
    [switch]$NoSound
)

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

if ($BodyFile -and (Test-Path $BodyFile)) {
    $Body = Get-Content -Raw -Encoding UTF8 $BodyFile
}
if (-not $Body) { $Body = '' }

$openLabel = 'Otev&#x159;&#xED;t soubor'
if ($Title -match 'Weekly') { $openLabel = 'Otev&#x159;&#xED;t digest' }
elseif ($Title -match 'Brief') { $openLabel = 'Otev&#x159;&#xED;t brief' }

[xml]$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="AI Researcher" WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="NoResize" Topmost="True"
        ShowInTaskbar="True" ShowActivated="False" SizeToContent="Height"
        Width="460" WindowStartupLocation="Manual" Left="-5000" Top="-5000">
  <Window.Resources>
    <Style x:Key="FlatButton" TargetType="Button">
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="Background" Value="#FF3B82F6"/>
      <Setter Property="Padding" Value="14,7"/>
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border Name="Bd" Background="{TemplateBinding Background}" CornerRadius="6"
                    Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="Bd" Property="Opacity" Value="0.85"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="CloseButton" TargetType="Button" BasedOn="{StaticResource FlatButton}">
      <Setter Property="Background" Value="Transparent"/>
      <Setter Property="Foreground" Value="#FF9AA0B0"/>
      <Setter Property="Padding" Value="8,2"/>
      <Setter Property="FontSize" Value="14"/>
    </Style>
  </Window.Resources>
  <Border Name="Card" Margin="12" CornerRadius="12" Background="#FF1B1D24"
          BorderBrush="#FF3B82F6" BorderThickness="2" Cursor="Hand">
    <Border.Effect>
      <DropShadowEffect BlurRadius="20" ShadowDepth="0" Opacity="0.7" Color="Black"/>
    </Border.Effect>
    <Grid>
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>
      <DockPanel Grid.Row="0" Margin="18,14,10,6" LastChildFill="True">
        <Button Name="CloseBtn" DockPanel.Dock="Right" Style="{StaticResource CloseButton}"
                Content="&#x2715;" VerticalAlignment="Top" ToolTip="Zav&#x159;&#xED;t"/>
        <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
          <Ellipse Width="10" Height="10" Fill="#FF3B82F6" Margin="0,0,10,0" VerticalAlignment="Center"/>
          <TextBlock Name="TitleText" FontSize="18" FontWeight="SemiBold" Foreground="White"
                     TextWrapping="Wrap" VerticalAlignment="Center"/>
        </StackPanel>
      </DockPanel>
      <TextBlock Grid.Row="1" Name="BodyText" Margin="18,0,18,14" FontSize="13.5"
                 Foreground="#FFD6D8E0" TextWrapping="Wrap" LineHeight="20"/>
      <Border Grid.Row="2" Background="#FF262933" CornerRadius="0,0,10,10" Padding="18,10">
        <DockPanel LastChildFill="True">
          <Button Name="OpenBtn" DockPanel.Dock="Right" Style="{StaticResource FlatButton}"/>
          <TextBlock Name="HintText" VerticalAlignment="Center" FontSize="11.5" Foreground="#FF9AA0B0"
                     Text="Kliknut&#xED;m kamkoli otev&#x159;e&#x161; soubor. Okno z&#x16F;stane, dokud ho nezav&#x159;e&#x161;."/>
        </DockPanel>
      </Border>
    </Grid>
  </Border>
</Window>
'@

# Inject the (entity-encoded) button label into the XAML before parsing.
$openBtn = $xaml.SelectSingleNode("//*[@Name='OpenBtn']")
$openBtn.SetAttribute('Content', [System.Net.WebUtility]::HtmlDecode($openLabel))

$reader = New-Object System.Xml.XmlNodeReader $xaml
$w = [Windows.Markup.XamlReader]::Load($reader)

$w.FindName('TitleText').Text = $Title
$w.FindName('BodyText').Text  = $Body.Trim()

$script:OpenTarget = $OpenPath
$script:LogFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'logs\popup.log'
function Write-PopupLog([string]$Message) {
    try { "$(Get-Date -Format s) $Message" | Add-Content -Encoding UTF8 $script:LogFile } catch { }
}

# Open the file: prefer VS Code (no reliance on the .md file association),
# fall back to the shell default handler.
function Open-Target {
    if (-not $script:OpenTarget) { Write-PopupLog 'Click: no OpenPath given, nothing to open.'; return }
    if (-not (Test-Path $script:OpenTarget)) { Write-PopupLog "Click: file not found: $($script:OpenTarget)"; return }
    $full = (Resolve-Path $script:OpenTarget).Path
    $codeExe = @(
        "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe",
        "$env:ProgramFiles\Microsoft VS Code\Code.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    try {
        if ($codeExe) {
            Start-Process -FilePath $codeExe -ArgumentList "`"$full`""
            Write-PopupLog "Click: opened in VS Code: $full"
        } else {
            Start-Process -FilePath $full
            Write-PopupLog "Click: opened with default app: $full"
        }
    } catch {
        Write-PopupLog "Click: open failed for ${full}: $_"
        try { Start-Process explorer.exe "/select,`"$full`"" } catch { }
    }
}

$openAction = {
    Open-Target
    $w.Close()
}
$w.FindName('Card').Add_MouseLeftButtonUp($openAction)
$w.FindName('OpenBtn').Add_Click($openAction)
$w.FindName('CloseBtn').Add_Click({ $w.Close() })

# Position bottom-right of the primary work area once the size is known.
$w.Add_ContentRendered({
    $wa = [System.Windows.SystemParameters]::WorkArea
    $w.Left = $wa.Right  - $w.ActualWidth
    $w.Top  = $wa.Bottom - $w.ActualHeight - ($Slot * ($w.ActualHeight - 12))
    if ($w.Top -lt $wa.Top) { $w.Top = $wa.Top }
})

if ($AutoCloseSeconds -gt 0) {
    $timer = New-Object System.Windows.Threading.DispatcherTimer
    $timer.Interval = [TimeSpan]::FromSeconds($AutoCloseSeconds)
    $timer.Add_Tick({ $timer.Stop(); $w.Close() })
    $timer.Start()
}

if (-not $NoSound) {
    try { [System.Media.SystemSounds]::Asterisk.Play() } catch { }
}

$w.ShowDialog() | Out-Null
