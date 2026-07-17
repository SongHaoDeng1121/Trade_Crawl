# Move This Project To A New Device

这份说明用于把当前 B2B outreach 程序、任务文件和本地工作记录迁移到一台干净设备上。

## 1. 在旧设备打包

默认不打包 `.env`，避免 API key、SMTP 密码泄露：

```bash
./scripts/make_transfer_bundle.sh
```

如果你明确确认新设备也要沿用相同密钥和 SMTP 设置，可以打包 `.env`：

```bash
./scripts/make_transfer_bundle.sh --include-env
```

生成的文件在 `transfer/` 目录。把这个 `.tar.gz` 复制到新设备并解压。

## 2. 在新 macOS 设备自动准备

进入解压后的项目目录，运行：

```bash
chmod +x scripts/bootstrap_macos.sh scripts/make_transfer_bundle.sh
./scripts/bootstrap_macos.sh
```

如果希望准备完成后直接打开 dashboard：

```bash
./scripts/bootstrap_macos.sh --start-dashboard
```

脚本会检查并处理：

- Python 3，不存在时用 Homebrew 安装。
- VS Code，不存在时尝试用 Homebrew 安装；程序运行不强依赖 VS Code。
- `.venv` 虚拟环境。
- `requirements.txt` 中的 Python 包。
- `.env`，不存在时从 `.env.example` 创建。
- `python -m compileall trade_outreach` 语法检查。
- `python -m trade_outreach.cli init-db` 数据库初始化。

## 3. 在新 Windows 设备自动准备

用 PowerShell 进入项目目录，运行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\bootstrap_windows.ps1
```

如果希望准备完成后直接打开 dashboard：

```powershell
.\scripts\bootstrap_windows.ps1 -StartDashboard
```

脚本会优先使用 `winget` 安装 Python 和 VS Code。没有 `winget` 时，需要先手动安装 Python 3。

## 4. 启动方式

macOS:

```bash
source .venv/bin/activate
python -m trade_outreach.dashboard
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m trade_outreach.dashboard
```

然后打开：

```text
http://127.0.0.1:8765
```

## 5. 重要安全点

- `.env` 里有 API key、SMTP 密码等敏感信息，不要发给别人，不要提交到 Git。
- 真实发信前先用 dashboard 或 CLI 做 test send 和 dry-run。
- 迁移包会包含 `data/` 下的本地任务、SQLite 数据库、草稿和导入文件。
- 脚本只会初始化缺失数据库，不会删除已有 leads、drafts 或任务文件。
