# 怎么把它开源出去

> 这份文件是给**发布者**看的（你自己）。使用者请看 `README.md`。

## 0. 一句话

**只发这个目录**（`omr-pdf2xml/`，约 21 个文件 / 1.5 MB）。
**绝不要把它的上一级（工作目录）发出去** —— 那里有 1366 个文件，其中 292 个含
厂商逆向记录、派生数据路径、个人路径或第三方语料。

## 1. 推送前必过的两道

```bash
pytest -q
python tools/legal_preflight.py --path .      # 必须 0 命中，否则不许推
```

第二道扫五类：厂商逆向产物 / 第三方代码克隆 / 凭据 / 个人路径 / 版权素材。
**它不是形式** —— 实测它抓到过：JVM 崩溃日志混进仓库、裸写的厂商简称、
     一个"读厂商真值做评估"的函数被导出器连带引用进来。

## 2. 在 GitHub 上建仓库

网页 → New repository：
- 名字 `omr-pdf2xml`
- **不要**勾 Add a README / .gitignore / license（本地都有，勾了会冲突）
- Public 或 Private 都行（AGPL 的义务只在**分发**时触发）

## 3. 推上去

```bash
git config user.name  "你的名字"
git config user.email "你的邮箱"
git branch -M main
git remote add origin https://github.com/<你的用户名>/omr-pdf2xml.git
git push -u origin main
```
首次推送要登录（浏览器或 Personal Access Token）。
**这一步需要你的账号，任何人都替不了。**

## 4. 出一个"能下载、双击就能用"的版本

```bash
git tag v0.1.0
git push origin v0.1.0
```

打 tag 后 `.github/workflows/release.yml` 会在 Windows runner 上自动：
```
跑测试 → 法律预检 → 从 Audiveris 官方 release 取 Windows 版
→ 打进包里(vendor/audiveris) → PyInstaller 打包
→ 冒烟测试（并断言"包里确实有 Audiveris"）
→ 压成 omr-pdf2xml-v0.1.0-windows-x64.zip → 附到 Release
```
用户下载解压、双击 `omr-pdf2xml.exe` 即可 —— **不用装 Audiveris，也不用装 Java**。

> 第一次跑 CI 很可能要修一轮（依赖、路径、Audiveris 资产名都可能变）。
> 去仓库的 Actions 页面看日志。

## 5. AGPL 的三条义务（合规，不是可选项）

1. 仓库内有 **LICENSE 全文**（AGPL-3.0）—— 已放。
2. 发行包内带 **Audiveris 的出处与许可** —— `tools/bundle_audiveris.py`
   会自动写 `vendor/audiveris/NOTICE.txt`。
3. **向使用者提供完整对应源码** —— 本仓库即本项目源码；
   Audiveris 的源码地址写在 Release 说明里。

**不要删任何版权与许可声明。**

## 6. 本地想先试打包

```bash
python tools/bundle_audiveris.py --from "<你的 Audiveris 目录>"
pip install pyinstaller
pyinstaller omr-pdf2xml.spec --noconfirm
# 产物: dist/omr-pdf2xml/  （整个文件夹就是要发给别人的东西）
```
`vendor/audiveris/` 约 163 MB，**已被 .gitignore 排除**，不进仓库 —— 打包时现取现放。
