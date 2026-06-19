# Nexus_brain 安装脚本
# 用法: 右键此文件 → 使用 PowerShell 运行
#       或在终端: .\install.ps1

Write-Host "`n  🌟 Nexus_brain 安装脚本`n" -ForegroundColor Cyan

# 1. 找 AstrBot 插件目录
$pluginDir = $null
$possiblePaths = @(
    "$env:USERPROFILE\.astrbot_launcher\instances"
    "$env:USERPROFILE\AstrBot\data\plugins"
    ".\data\plugins"
)

foreach ($base in $possiblePaths) {
    if (Test-Path $base) {
        # Launcher 模式：找 instances/*/core/data/plugins
        if ($base -like "*instances*") {
            $instances = Get-ChildItem $base -Directory -ErrorAction SilentlyContinue
            foreach ($inst in $instances) {
                $test = Join-Path $inst.FullName "core\data\plugins"
                if (Test-Path $test) {
                    $pluginDir = $test
                    break
                }
            }
        }
        # 直接就是 plugins 目录
        elseif ($base -like "*plugins*") {
            $pluginDir = $base
        }
        if ($pluginDir) { break }
    }
}

if (-not $pluginDir) {
    Write-Host "  [?] 没自动找到 AstrBot 插件目录" -ForegroundColor Yellow
    $manual = Read-Host "  请手动输入 plugins 目录路径"
    if (Test-Path $manual) {
        $pluginDir = $manual
    } else {
        Write-Host "  [X] 路径不存在，退出" -ForegroundColor Red
        pause
        exit 1
    }
}

Write-Host "  [√] AstrBot 插件目录: $pluginDir`n" -ForegroundColor Green

# 2. 复制插件
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $pluginDir "Nexus_brain"

if (Test-Path $dest) {
    Write-Host "  [!] Nexus_brain 已存在，覆盖？(y/n)" -ForegroundColor Yellow
    $r = Read-Host
    if ($r -ne "y") { Write-Host "  取消"; pause; exit 0 }
    Remove-Item $dest -Recurse -Force
}

Write-Host "  复制中..."
Copy-Item $src $dest -Recurse -Exclude ".git",".gitignore","config.yaml","session.json",".desktop.lock","__pycache__"
Write-Host "  [√] 插件已复制到 $dest`n" -ForegroundColor Green

# 3. 配置文件
$cfgSrc = Join-Path $dest "config.example.yaml"
$cfgDst = Join-Path $dest "config.yaml"
if (-not (Test-Path $cfgDst)) {
    Copy-Item $cfgSrc $cfgDst
    Write-Host "  [√] config.yaml 已创建，记得编辑角色名和记忆路径`n" -ForegroundColor Green
} else {
    Write-Host "  [√] config.yaml 已存在，跳过`n" -ForegroundColor Green
}

# 4. 装依赖
Write-Host "  [..] 安装 Python 依赖..." -ForegroundColor Yellow
$pip = Get-Command pip -ErrorAction SilentlyContinue
if (-not $pip) {
    $pip = Get-Command pip3 -ErrorAction SilentlyContinue
}
if (-not $pip) {
    Write-Host "  [!] 找不到 pip，请手动安装依赖:`n  pip install -r requirements.txt" -ForegroundColor Yellow
} else {
    & $pip.Source install -r (Join-Path $dest "requirements.txt") -q 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [√] 依赖安装完成`n" -ForegroundColor Green
    } else {
        Write-Host "  [!] 部分依赖可能安装失败，请检查`n" -ForegroundColor Yellow
    }
}

# 5. 搞定
Write-Host " ══════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  安装完成！" -ForegroundColor Green
Write-Host "  1. 编辑 $dest\config.yaml 改角色名" -ForegroundColor White
Write-Host "  2. 在 AstrBot 面板启用 Nexus_brain 插件" -ForegroundColor White
Write-Host "  3. 右键悬浮窗 → 个性化设置" -ForegroundColor White
Write-Host " ══════════════════════════════════════`n" -ForegroundColor Cyan
pause
