param([int]$Port=9014,[string]$KeyFile,[string]$Python,[string]$BaseUrl,[string]$Model='gpt-5.5')
$ErrorActionPreference='Stop'
$repoRoot=Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python=Join-Path $repoRoot '.venv\Scripts\python.exe' }
if ($BaseUrl) { $env:MODEL_API_BASE_URL=$BaseUrl }
if (-not $env:MODEL_API_BASE_URL) { $env:MODEL_API_BASE_URL=$env:VIMAX_LLM_BASE_URL }
if ($KeyFile) {
    $taskSecret=Get-Content -Raw -LiteralPath $KeyFile | ConvertTo-SecureString
    $env:MODEL_API_KEY=[System.Net.NetworkCredential]::new('', $taskSecret).Password
} elseif (-not $env:MODEL_API_KEY) { $env:MODEL_API_KEY=$env:VIMAX_LLM_API_KEY }
if (-not $env:MODEL_API_BASE_URL -or -not $env:MODEL_API_KEY) { throw '请提供模型接口地址和密钥环境变量，或指定 -BaseUrl 和 -KeyFile。' }
$env:MODEL_NAME=$Model
$env:MODEL_DIALOGUE_NAME=$Model
if (-not $env:MODEL_ANALYSIS_NAME) { $env:MODEL_ANALYSIS_NAME=$Model }
if (-not $env:MODEL_PERSONA_NAME) { $env:MODEL_PERSONA_NAME=$Model }
if (-not $env:MODEL_REVIEW_NAME) { $env:MODEL_REVIEW_NAME=$Model }
$env:PYTHONUTF8='1'
try { & $Python -m uvicorn app.main:app --app-dir (Join-Path $repoRoot 'dialogue\backend') --host 127.0.0.1 --port $Port }
finally { if ($KeyFile) { Remove-Item Env:MODEL_API_KEY -ErrorAction SilentlyContinue } }
