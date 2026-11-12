# 初回セットアップ: 仮想環境の作成と依存パッケージのインストール
# 社内ネットワーク等で SSL 証明書エラーになる環境向けに、Windows の信頼済みルート証明書を
# 含む CA バンドル (.venv\ca-bundle.pem) を作って pip とアプリから使います（検証は無効化しません）。
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) { python -m venv .venv }

$pem = Join-Path $PSScriptRoot '.venv\ca-bundle.pem'
$sb = New-Object System.Text.StringBuilder
try {
    $certifi = & .\.venv\Scripts\python -c "import pip._vendor.certifi as c; print(c.where())"
    [void]$sb.Append((Get-Content $certifi -Raw))
} catch {}
Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root, Cert:\LocalMachine\CA | ForEach-Object {
    [void]$sb.AppendLine('-----BEGIN CERTIFICATE-----')
    [void]$sb.AppendLine([Convert]::ToBase64String($_.RawData, 'InsertLineBreaks'))
    [void]$sb.AppendLine('-----END CERTIFICATE-----')
}
[IO.File]::WriteAllText($pem, $sb.ToString())

.\.venv\Scripts\python -m pip install --cert $pem -r requirements.txt
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning 'ffmpeg が見つかりません。winget install Gyan.FFmpeg などでインストールしてください。'
}
Write-Host 'セットアップ完了'
