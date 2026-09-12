# 一条命令走完发布流程里"不需要你账号"的部分。
#
#   .\publish.ps1 -RepoUrl https://github.com/<你>/omr-pdf2xml.git -Tag v0.1.0
#
# 它做四件事：① 测试 ② 法律预检 ③ 配远程 ④ 推 main + 打 tag 并推 tag。
# 第 ④ 步需要你的 GitHub 凭据（浏览器登录或 PAT）—— 这一步只能你本人完成。
param(
  [Parameter(Mandatory = $true)][string]$RepoUrl,
  [string]$Tag = "v0.1.0",
  [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== ① 测试 ===" -ForegroundColor Cyan
if (-not $SkipTests) {
  python -m pytest tests -q
  if ($LASTEXITCODE -ne 0) { throw "测试没过，停止发布" }
} else { Write-Host "(跳过)" }

Write-Host "`n=== ② 法律预检（必须 0 命中）===" -ForegroundColor Cyan
python tools/legal_preflight.py --path .
if ($LASTEXITCODE -ne 0) {
  throw "法律预检有命中，停止发布。看 results/legal_preflight.txt 的明细。"
}

Write-Host "`n=== ③ 配置远程 ===" -ForegroundColor Cyan
$existing = git remote
if ($existing -contains "origin") {
  git remote set-url origin $RepoUrl
  Write-Host "origin 已更新 -> $RepoUrl"
} else {
  git remote add origin $RepoUrl
  Write-Host "origin 已添加 -> $RepoUrl"
}
git branch -M main

Write-Host "`n=== ④ 推送（需要你的 GitHub 凭据）===" -ForegroundColor Cyan
Write-Host "如果这里卡住/报 403：说明本机没有凭据。" -ForegroundColor Yellow
Write-Host "  办法 A：浏览器登录（git 会弹窗）"
Write-Host "  办法 B：装 gh 后跑  gh auth login"
Write-Host "  办法 C：用 Personal Access Token 作为密码"
Write-Host ""
git push -u origin main
if ($LASTEXITCODE -ne 0) { throw "push 失败（多半是凭据问题）" }

git tag $Tag
git push origin $Tag
if ($LASTEXITCODE -ne 0) { throw "推送 tag 失败" }

Write-Host "`n=== 完成 ===" -ForegroundColor Green
Write-Host "代码在 $RepoUrl"
Write-Host "打上 $Tag 后，Actions 会自动构建【自带 Audiveris】的发行包并附到 Release。"
Write-Host "去 Actions 页面看进度：$RepoUrl/actions"
