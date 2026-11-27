[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$sourceJpeg = Join-Path $PSScriptRoot "..\..\app\static\corvus-mark.jpg"
if (-not (Test-Path -LiteralPath $sourceJpeg -PathType Leaf)) {
    throw "Approved Corvus raster mark was not found at: $sourceJpeg"
}
$sourceBytes = [IO.File]::ReadAllBytes($sourceJpeg)
if (
    $sourceBytes.Length -lt 4 -or
    $sourceBytes[0] -ne 0xFF -or
    $sourceBytes[1] -ne 0xD8 -or
    $sourceBytes[$sourceBytes.Length - 2] -ne 0xFF -or
    $sourceBytes[$sourceBytes.Length - 1] -ne 0xD9
) {
    throw "Approved Corvus raster mark is not a complete JPEG payload: $sourceJpeg"
}
$sourceStream = [IO.MemoryStream]::new($sourceBytes, $false)
$sourceImage = [Drawing.Image]::FromStream($sourceStream, $true, $true)

function New-BrandedPng {
    param([Parameter(Mandatory = $true)][int]$Size)

    $bitmap = [Drawing.Bitmap]::new($Size, $Size, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $graphics.CompositingQuality = [Drawing.Drawing2D.CompositingQuality]::HighQuality
    $graphics.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.PixelOffsetMode = [Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::HighQuality
    $graphics.Clear([Drawing.Color]::Transparent)

    try {
        $graphics.DrawImage($sourceImage, [Drawing.Rectangle]::new(0, 0, $Size, $Size))
        $stream = [IO.MemoryStream]::new()
        $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
        return ,$stream.ToArray()
    }
    finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

try {
    $sizes = @(16, 32, 48, 256)
    $images = @($sizes | ForEach-Object { New-BrandedPng -Size $_ })
    $resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
    New-Item -ItemType Directory -Path (Split-Path -Parent $resolvedOutput) -Force | Out-Null
    $file = [IO.File]::Open($resolvedOutput, [IO.FileMode]::Create, [IO.FileAccess]::Write)
    $writer = [IO.BinaryWriter]::new($file)
    try {
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]$images.Count)
        $offset = 6 + (16 * $images.Count)
        for ($index = 0; $index -lt $images.Count; $index++) {
            $size = $sizes[$index]
            $bytes = $images[$index]
            $writer.Write([byte]$(if ($size -eq 256) { 0 } else { $size }))
            $writer.Write([byte]$(if ($size -eq 256) { 0 } else { $size }))
            $writer.Write([byte]0)
            $writer.Write([byte]0)
            $writer.Write([uint16]1)
            $writer.Write([uint16]32)
            $writer.Write([uint32]$bytes.Length)
            $writer.Write([uint32]$offset)
            $offset += $bytes.Length
        }
        foreach ($bytes in $images) {
            $writer.Write([byte[]]$bytes)
        }
    }
    finally {
        $writer.Dispose()
        $file.Dispose()
    }

    Write-Host "Generated Corvus icon from approved artwork: $resolvedOutput"
}
finally {
    $sourceImage.Dispose()
    $sourceStream.Dispose()
}
