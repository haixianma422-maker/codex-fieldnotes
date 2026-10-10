# 客户端诊断

从项目根目录运行：

```powershell
python tools/verify_obsidian.py
```

该命令检查当前专用 Vault 的可见窗口、双链解析和关系图谱，并更新
`docs/obsidian-runtime.json`、笔记截图和图谱截图。它会打开笔记和图谱视图。

`inspect_hooks.py` 是历史 app-server 诊断工具，使用固定 CLI 版本路径；
日常工作不需要运行。当前 Hooks 信任通过 Codex 的 `/hooks` 审核，不由脚本代写。

根目录保留 Hook、发布、检索、安装、状态摘要和自动验收入口，避免改变已信任的
Hook 命令或启动器路径。恢复演练文件保存在 `backups/restore-drills/`。
